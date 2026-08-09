import { LangfuseClient } from "@langfuse/client";
import { LangfuseSpanProcessor } from "@langfuse/otel";
import {
  type LangfuseAgent,
  type LangfuseGeneration,
  type LangfuseTool,
  setLangfuseTracerProvider,
  startObservation,
} from "@langfuse/tracing";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const BOOKING_OPERATIONS = [
  "start_auth",
  "auth_status",
  "confirm_sms",
  "submit_sms_code",
  "gmail_status",
  "connect_gmail",
  "session_status",
  "open_home",
  "open_reservations",
  "open_messages",
  "open_calendar",
  "current_page",
  "go_back",
  "close_navigation",
  "list_messages",
  "read_message",
  "prepare_reply",
  "stage_reply",
  "confirm_reply",
  "list_reservations",
  "show_reservation",
  "list_unreplied",
  "list_arrivals",
  "list_pending_arrival_tasks",
  "refresh_gmail_matches",
  "list_gmail_matches",
  "preview_gmail_match",
  "review_gmail_match",
  "prepare_arrival_drafts",
  "identity_status",
  "arrival_dry_run",
  "view_availability",
  "view_pricing",
  "get_stats",
] as const;

const AUTH_EVENTS = [
  "pulse_approval_required",
  "pulse_failed",
  "email_code_requested",
  "email_code_found",
  "email_oauth_required",
  "email_failed",
  "sms_confirmation_required",
  "sms_code_required",
  "verified",
  "error",
] as const;

const SENSITIVE_KEY =
  /password|secret|api.?key|authorization|cookie|session.?state|otp|verification.?code|sms.?code|email|phone|guest|customer|identity|passport|afm|ΑΦΜ|reservation.?id|booking.?id|message.?body|reply.?text|draft|prompt/i;
const SECRET_TOKEN = /\b(?:sk|pk)-[A-Za-z0-9_-]{8,}\b/g;
const EMAIL_ADDRESS = /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi;
const PHONE_NUMBER = /\+?\d[\d\s().-]{7,}\d/g;
const SHORT_CODE = /\b\d{4,8}\b/g;
const MUTATION_CALL =
  /(?:reply_to_message|set_price|close_availability|open_availability|create_draft|download_attachment|messages\(\)\.send|drafts\(\)\.send)/i;
const MUTATION_CLI =
  /booking[\\\s"',]+(?:messages[\\\s"',]+reply|pricing[\\\s"',]+set|availability[\\\s"',]+(?:open|close)|arrivals[\\\s"',]+(?:run|review-match|refresh-matches)|identity[\\\s"',]+(?:record|verify))/i;

type BookingOperation = (typeof BOOKING_OPERATIONS)[number];
type AuthEvent = (typeof AUTH_EVENTS)[number];

type ToolState = {
  observation: LangfuseTool;
  toolName: string;
  operation: BookingOperation | null;
  readOnlyViolation: boolean;
  turnIndex: number | null;
  startedAt: number;
};

type RunState = {
  root: LangfuseAgent;
  currentTurn: LangfuseGeneration | null;
  currentTurnIndex: number | null;
  providerStatus: number | null;
  tools: Map<string, ToolState>;
  startedAt: number;
  toolCalls: number;
  toolErrors: number;
  readOnlyViolation: boolean;
  authAttempted: boolean;
  authVerified: boolean;
  lastStopReason: string | null;
  lastVisibleOutput: string | null;
  lastError: string | null;
};

type CapturedInput = {
  command: string;
  system: string;
  user: string;
};

function redactString(value: string): string {
  return value
    .replace(SECRET_TOKEN, "[REDACTED_TOKEN]")
    .replace(EMAIL_ADDRESS, "[REDACTED_EMAIL]")
    .replace(PHONE_NUMBER, "[REDACTED_PHONE]")
    .replace(SHORT_CODE, "[REDACTED_CODE]");
}

export function redactSensitiveData(value: unknown): unknown {
  if (typeof value === "string") {
    return redactString(value);
  }
  if (Array.isArray(value)) {
    return value.map(redactSensitiveData);
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, nested]) => [
        key,
        SENSITIVE_KEY.test(key) ? "[REDACTED]" : redactSensitiveData(nested),
      ]),
    );
  }
  return value;
}

export function extractVisibleAssistantText(value: unknown): string {
  if (!value || typeof value !== "object" || !("role" in value) || value.role !== "assistant") {
    return "";
  }
  if (!("content" in value) || !Array.isArray(value.content)) {
    return "";
  }
  return value.content
    .filter(
      (part): part is { type: "text"; text: string } =>
        Boolean(
          part &&
            typeof part === "object" &&
            "type" in part &&
            part.type === "text" &&
            "text" in part &&
            typeof part.text === "string",
        ),
    )
    .map((part) => part.text)
    .join("\n")
    .trim();
}

export function extractAssistantToolNames(value: unknown): string[] {
  if (!value || typeof value !== "object" || !("role" in value) || value.role !== "assistant") {
    return [];
  }
  if (!("content" in value) || !Array.isArray(value.content)) {
    return [];
  }
  return value.content.flatMap((part) => {
    if (
      part &&
      typeof part === "object" &&
      "type" in part &&
      part.type === "toolCall" &&
      "name" in part &&
      typeof part.name === "string"
    ) {
      return [part.name];
    }
    return [];
  });
}

function quoteCommandArgument(value: string): string {
  return /^[A-Za-z0-9_./:=,@+<>-]+$/.test(value) ? value : JSON.stringify(value);
}

export function describePrimeCommand(argv: string[], prompt: string): string {
  const described = ["./scripts/prime-agent.sh"];
  const args = argv.slice(2);
  const valueFlags = new Set(["--api-key", "--system-prompt", "--append-system-prompt"]);
  let redactNext = false;

  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (redactNext) {
      described.push("<redacted>");
      redactNext = false;
      continue;
    }
    if (valueFlags.has(argument)) {
      described.push(argument);
      redactNext = true;
      continue;
    }
    if (argument === "--") {
      described.push("--", "<prompt in trace input>");
      break;
    }
    if (argument === prompt) {
      described.push("<prompt in trace input>");
      continue;
    }
    described.push(redactString(argument));
  }

  return described.map(quoteCommandArgument).join(" ");
}

export function extractRetryDecision(value: unknown): string | null {
  if (value && typeof value === "object") {
    const retry = "retry" in value ? value.retry : null;
    if (retry && typeof retry === "object" && "decision" in retry) {
      return typeof retry.decision === "string" ? retry.decision : null;
    }
  }
  const serialized = inspectLocally(value);
  const match = serialized.match(/["']decision["']\s*:\s*["']([a-z_]+)["']/i);
  return match?.[1] ?? null;
}

function inspectLocally(value: unknown): string {
  try {
    return JSON.stringify(value) ?? "";
  } catch {
    return "";
  }
}

export function classifyBookingOperation(value: unknown): BookingOperation | null {
  const serialized = inspectLocally(value);
  return (
    BOOKING_OPERATIONS.find((operation) =>
      serialized.includes(`booking_extranet.${operation}`),
    ) ?? null
  );
}

export function detectReadOnlyViolation(value: unknown): boolean {
  const serialized = inspectLocally(value);
  return MUTATION_CALL.test(serialized) || MUTATION_CLI.test(serialized);
}

export function extractAuthEvent(value: unknown): AuthEvent | null {
  const serialized = inspectLocally(value);
  return AUTH_EVENTS.find((event) => serialized.includes(`"${event}"`)) ?? null;
}

function isAuthenticationOperation(operation: BookingOperation | null): boolean {
  return (
    operation === "start_auth" ||
    operation === "auth_status" ||
    operation === "confirm_sms" ||
    operation === "submit_sms_code"
  );
}

function warn(ctx: ExtensionContext, message: string): void {
  if (ctx.hasUI) {
    ctx.ui.notify(message, "warning");
  } else {
    process.stderr.write(`WARNING: ${message}\n`);
  }
}

export default function bookingObservability(pi: ExtensionAPI): void {
  const publicKey = process.env.LANGFUSE_PUBLIC_KEY;
  const secretKey = process.env.LANGFUSE_SECRET_KEY;
  if (!publicKey && !secretKey) {
    return;
  }
  if (!publicKey || !secretKey) {
    process.stderr.write(
      "WARNING: Langfuse tracing disabled because its key pair is incomplete\n",
    );
    return;
  }

  const processor = new LangfuseSpanProcessor({
    publicKey,
    secretKey,
    baseUrl: process.env.LANGFUSE_BASE_URL,
    environment: "development",
    mediaUploadEnabled: false,
    timeout: 5,
    mask: ({ data }) => redactSensitiveData(data),
  });
  const provider = new NodeTracerProvider({ spanProcessors: [processor] });
  setLangfuseTracerProvider(provider);
  const client = new LangfuseClient({
    publicKey,
    secretKey,
    baseUrl: process.env.LANGFUSE_BASE_URL,
  });

  const captureContent = process.env.LANGFUSE_CAPTURE_CONTENT === "true";

  let run: RunState | null = null;
  let capturedInput: CapturedInput | null = null;

  async function finishRun(ctx: ExtensionContext, reason: string): Promise<void> {
    if (!run) {
      return;
    }
    const completed = !["error", "aborted"].includes(run.lastStopReason ?? "");

    for (const tool of run.tools.values()) {
      tool.observation.update({
        level: "WARNING",
        statusMessage: "Tool observation ended before a result event",
        metadata: {
          toolName: tool.toolName,
          bookingOperation: tool.operation ?? "other",
          turnIndex: tool.turnIndex,
          argsCaptured: false,
          resultCaptured: false,
          readOnlyViolation: tool.readOnlyViolation,
          durationMs: Date.now() - tool.startedAt,
          success: false,
        },
      });
      tool.observation.end();
    }
    run.tools.clear();

    if (run.currentTurn) {
      run.currentTurn.update({
        level: "WARNING",
        statusMessage: "Turn observation ended with the agent run",
        output: captureContent
          ? [{ role: "assistant", content: "Run ended before this model turn completed." }]
          : undefined,
        metadata: {
          turnIndex: run.currentTurnIndex,
          providerStatus: run.providerStatus,
          contentCaptured: captureContent,
          hiddenReasoningCaptured: false,
        },
      });
      run.currentTurn.end();
    }

    const failureSummary = redactString(
      run.lastError ?? `Agent stopped with reason: ${run.lastStopReason ?? "unknown"}`,
    );
    run.root.update({
      level: completed && run.toolErrors === 0 ? "DEFAULT" : "ERROR",
      statusMessage: completed ? "Agent run completed" : "Agent run did not complete",
      output:
        captureContent && run.lastVisibleOutput
          ? [{ role: "assistant", content: redactString(run.lastVisibleOutput) }]
          : captureContent && !completed
            ? [{ role: "assistant", content: `Run failed: ${failureSummary}` }]
            : undefined,
      metadata: {
        command: capturedInput?.command ?? "./scripts/prime-agent.sh",
        runStatus: completed ? "completed" : "failed",
        failure: completed ? null : failureSummary,
        reason,
        durationMs: Date.now() - run.startedAt,
        toolCalls: run.toolCalls,
        toolErrors: run.toolErrors,
        readOnlyViolation: run.readOnlyViolation,
        authAttempted: run.authAttempted,
        authVerified: run.authVerified,
        lastStopReason: run.lastStopReason,
        contentCaptured: captureContent,
        hiddenReasoningCaptured: false,
        langfuse_tags: ["booking-agent", "prime-agent", "cli-run"],
      },
    });

    client.score.trace(run.root, {
      name: "agent_completed",
      value: completed ? 1 : 0,
      dataType: "BOOLEAN",
    });
    client.score.trace(run.root, {
      name: "tool_success",
      value: run.toolErrors === 0 ? 1 : 0,
      dataType: "BOOLEAN",
    });
    client.score.trace(run.root, {
      name: "read_only_policy_respected",
      value: run.readOnlyViolation ? 0 : 1,
      dataType: "BOOLEAN",
    });
    if (run.authAttempted) {
      client.score.trace(run.root, {
        name: "auth_verified",
        value: run.authVerified ? 1 : 0,
        dataType: "BOOLEAN",
      });
    }

    run.root.end();
    run = null;
    try {
      await Promise.all([processor.forceFlush(), client.flush()]);
    } catch {
      warn(ctx, "Langfuse could not flush this run; agent execution was unaffected");
    }
  }

  pi.on("session_start", (_event, ctx) => {
    if (ctx.hasUI) {
      ctx.ui.setStatus(
        "langfuse",
        captureContent
          ? "Langfuse redacted input/output tracing"
          : "Langfuse metadata-only tracing",
      );
    }
  });

  pi.on("before_agent_start", (event) => {
    const command = redactString(
      process.env.LANGFUSE_RUN_COMMAND ?? describePrimeCommand(process.argv, event.prompt),
    );
    capturedInput = captureContent
      ? {
          command,
          system: redactString(event.systemPrompt),
          user: redactString(event.prompt),
        }
      : {
          command,
          system: "",
          user: "",
        };
  });

  pi.on("agent_start", (_event, ctx) => {
    if (run) {
      void finishRun(ctx, "superseded");
    }
    run = {
      root: startObservation(
        "booking-agent-cli-run",
        {
          version: "0.1.0",
          input:
            captureContent && capturedInput
              ? [
                  { role: "system", content: capturedInput.system },
                  { role: "user", content: capturedInput.user },
                ]
              : undefined,
          metadata: {
            command: capturedInput?.command ?? "./scripts/prime-agent.sh",
            runStatus: "running",
            harness: "prime-agent",
            provider: ctx.model?.provider ?? "unknown",
            model: ctx.model?.id ?? "unknown",
            contentCaptured: captureContent,
            hiddenReasoningCaptured: false,
            langfuse_tags: ["booking-agent", "prime-agent", "cli-run"],
          },
        },
        { asType: "agent" },
      ),
      currentTurn: null,
      currentTurnIndex: null,
      providerStatus: null,
      tools: new Map(),
      startedAt: Date.now(),
      toolCalls: 0,
      toolErrors: 0,
      readOnlyViolation: false,
      authAttempted: false,
      authVerified: false,
      lastStopReason: null,
      lastVisibleOutput: null,
      lastError: null,
    };
  });

  pi.on("turn_start", (event, ctx) => {
    if (!run) {
      return;
    }
    run.currentTurnIndex = event.turnIndex;
    run.providerStatus = null;
    run.currentTurn = run.root.startObservation(
      "generate-agent-turn",
      {
        model: ctx.model?.id ?? "unknown",
        input:
          captureContent && capturedInput
            ? [
                { role: "system", content: capturedInput.system },
                { role: "user", content: capturedInput.user },
              ]
            : undefined,
        metadata: {
          provider: ctx.model?.provider ?? "unknown",
          turnIndex: event.turnIndex,
          contentCaptured: captureContent,
          hiddenReasoningCaptured: false,
        },
      },
      { asType: "generation" },
    );
  });

  pi.on("after_provider_response", (event) => {
    if (run) {
      run.providerStatus = event.status;
    }
  });

  pi.on("tool_execution_start", (event) => {
    if (!run) {
      return;
    }
    const operation = classifyBookingOperation(event.args);
    const readOnlyViolation = detectReadOnlyViolation(event.args);
    run.toolCalls += 1;
    run.readOnlyViolation ||= readOnlyViolation;
    run.authAttempted ||= isAuthenticationOperation(operation);

    const observation = run.root.startObservation(
      operation ? `booking:${operation}` : `tool:${event.toolName}`,
      {
        level: readOnlyViolation ? "WARNING" : "DEFAULT",
        statusMessage: readOnlyViolation
          ? "Potential write operation detected"
          : "Tool execution started",
        metadata: {
          toolName: event.toolName,
          bookingOperation: operation ?? "other",
          turnIndex: run.currentTurnIndex,
          argsCaptured: false,
          resultCaptured: false,
          readOnlyViolation,
        },
      },
      { asType: "tool" },
    );
    run.tools.set(event.toolCallId, {
      observation,
      toolName: event.toolName,
      operation,
      readOnlyViolation,
      turnIndex: run.currentTurnIndex,
      startedAt: Date.now(),
    });
  });

  pi.on("tool_execution_end", (event) => {
    if (!run) {
      return;
    }
    const tool = run.tools.get(event.toolCallId);
    if (!tool) {
      return;
    }
    const authEvent = extractAuthEvent(event.result);
    const retryDecision = extractRetryDecision(event.result);
    run.authVerified ||= authEvent === "verified";
    run.toolErrors += event.isError ? 1 : 0;

    tool.observation.update({
      level: event.isError ? "ERROR" : tool.readOnlyViolation ? "WARNING" : "DEFAULT",
      statusMessage: event.isError ? "Tool execution failed" : "Tool execution completed",
      input: {
        operation: tool.operation ?? tool.toolName,
        argumentsCaptured: false,
      },
      output: {
        status: event.isError ? "failed" : "completed",
        retryDecision: retryDecision ?? "not_available",
        resultCaptured: false,
      },
      metadata: {
        toolName: tool.toolName,
        bookingOperation: tool.operation ?? "other",
        turnIndex: tool.turnIndex,
        authEvent: authEvent ?? "none",
        retryDecision: retryDecision ?? "not_available",
        argsCaptured: false,
        resultCaptured: false,
        readOnlyViolation: tool.readOnlyViolation,
        durationMs: Date.now() - tool.startedAt,
        success: !event.isError,
      },
    });
    tool.observation.end();
    run.tools.delete(event.toolCallId);
  });

  pi.on("turn_end", (event) => {
    if (!run || !run.currentTurn) {
      return;
    }
    const message = event.message;
    const isError = message.stopReason === "error" || message.stopReason === "aborted";
    const visibleOutput = extractVisibleAssistantText(message);
    const assistantToolNames = extractAssistantToolNames(message);
    const errorMessage = isError
      ? redactString(message.errorMessage || `Model stopped with reason: ${message.stopReason}`)
      : null;
    if (visibleOutput) {
      run.lastVisibleOutput = visibleOutput;
    }
    if (errorMessage) {
      run.lastError = errorMessage;
    }
    run.lastStopReason = message.stopReason;
    run.currentTurn.update({
      level: isError ? "ERROR" : "DEFAULT",
      statusMessage: isError ? "Model turn failed" : "Model turn completed",
      model: message.model,
      output:
        captureContent && visibleOutput
          ? [{ role: "assistant", content: redactString(visibleOutput) }]
          : captureContent && assistantToolNames.length > 0
            ? [
                {
                  role: "assistant",
                  content: "",
                  tool_calls: assistantToolNames.map((name, index) => ({
                    id: `redacted-tool-call-${index + 1}`,
                    type: "function",
                    function: {
                      name,
                      arguments: JSON.stringify({ captured: false }),
                    },
                  })),
                },
              ]
          : captureContent && errorMessage
            ? [{ role: "assistant", content: `Model turn failed: ${errorMessage}` }]
            : undefined,
      usageDetails: {
        input: message.usage.input,
        output: message.usage.output,
        cacheRead: message.usage.cacheRead,
        cacheWrite: message.usage.cacheWrite,
        total: message.usage.totalTokens,
      },
      costDetails: {
        input: message.usage.cost.input,
        output: message.usage.cost.output,
        cacheRead: message.usage.cost.cacheRead,
        cacheWrite: message.usage.cost.cacheWrite,
        total: message.usage.cost.total,
      },
      metadata: {
        provider: message.provider,
        turnIndex: event.turnIndex,
        providerStatus: run.providerStatus,
        stopReason: message.stopReason,
        failure: errorMessage,
        toolCallNames: assistantToolNames,
        toolResultCount: event.toolResults.length,
        contentCaptured: captureContent,
        hiddenReasoningCaptured: false,
      },
    });
    run.currentTurn.end();
    run.currentTurn = null;
    run.currentTurnIndex = null;
  });

  pi.on("agent_end", async (_event, ctx) => {
    await finishRun(ctx, "agent_end");
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    await finishRun(ctx, "session_shutdown");
    try {
      await Promise.all([provider.shutdown(), client.shutdown()]);
    } catch {
      warn(ctx, "Langfuse shutdown did not flush cleanly");
    } finally {
      setLangfuseTracerProvider(null);
    }
  });
}
