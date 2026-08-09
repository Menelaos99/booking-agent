import assert from "node:assert/strict";
import test from "node:test";

import {
  classifyBookingOperation,
  describePrimeCommand,
  detectReadOnlyViolation,
  extractAssistantToolNames,
  extractAuthEvent,
  extractRetryDecision,
  extractVisibleAssistantText,
  redactSensitiveData,
} from "./index.ts";

test("classifies only allowlisted Booking operations", () => {
  assert.equal(
    classifyBookingOperation({ code: "await booking_extranet.list_messages()" }),
    "list_messages",
  );
  assert.equal(classifyBookingOperation({ code: "print('hello')" }), null);
});

test("classifies semantic navigation operations", () => {
  assert.equal(
    classifyBookingOperation({ code: "await booking_extranet.open_calendar('2026-08')" }),
    "open_calendar",
  );
  assert.equal(
    classifyBookingOperation({ code: "await booking_extranet.go_back()" }),
    "go_back",
  );
  assert.equal(
    detectReadOnlyViolation({ code: "await booking_extranet.open_reservations()" }),
    false,
  );
});

test("detects write attempts without flagging reads", () => {
  assert.equal(
    detectReadOnlyViolation({ code: "await booking_extranet.list_messages()" }),
    false,
  );
  assert.equal(
    detectReadOnlyViolation({ code: "await reply_to_message(page, '0', text)" }),
    true,
  );
  assert.equal(
    detectReadOnlyViolation({ code: "await booking_extranet.confirm_reply(id, true)" }),
    false,
  );
});

test("classifies the confirmed reply operations", () => {
  assert.equal(
    classifyBookingOperation({ code: "await booking_extranet.stage_reply(ref, guest, text)" }),
    "stage_reply",
  );
  assert.equal(
    classifyBookingOperation({ code: "await booking_extranet.prepare_arrival_drafts()" }),
    "prepare_arrival_drafts",
  );
});

test("flags direct Gmail and arrival workflow bypasses", () => {
  assert.equal(
    detectReadOnlyViolation({ code: "gmail.download_attachment(messageId, attachmentId)" }),
    true,
  );
  assert.equal(
    detectReadOnlyViolation({ code: "uv run booking arrivals run" }),
    true,
  );
  assert.equal(
    detectReadOnlyViolation({ code: "await booking_extranet.prepare_arrival_drafts()" }),
    false,
  );
});

test("extracts only allowlisted authentication events", () => {
  assert.equal(extractAuthEvent({ event: "sms_code_required" }), "sms_code_required");
  assert.equal(extractAuthEvent({ event: "unrelated" }), null);
});

test("redacts sensitive fields and patterns at the source", () => {
  assert.deepEqual(
    redactSensitiveData({
      email: "person@example.com",
      details: {
        note: "token sk-1234567890",
        code: "123456",
        customer_name: "Alice Example",
        identity_status: "needs_review",
      },
    }),
    {
      email: "[REDACTED]",
      details: {
        note: "token [REDACTED_TOKEN]",
        code: "[REDACTED_CODE]",
        customer_name: "[REDACTED]",
        identity_status: "[REDACTED]",
      },
    },
  );
});

test("captures visible assistant output without hidden reasoning or tool arguments", () => {
  assert.equal(
    extractVisibleAssistantText({
      role: "assistant",
      content: [
        { type: "thinking", thinking: "private reasoning" },
        { type: "text", text: "Visible answer" },
        { type: "toolCall", name: "ipython", arguments: { code: "secret" } },
      ],
    }),
    "Visible answer",
  );
  assert.deepEqual(
    extractAssistantToolNames({
      role: "assistant",
      content: [
        { type: "thinking", thinking: "private reasoning" },
        { type: "toolCall", name: "ipython", arguments: { code: "secret" } },
      ],
    }),
    ["ipython"],
  );
});

test("describes a CLI run without duplicating prompt or API-key values", () => {
  assert.equal(
    describePrimeCommand(
      [
        "/usr/local/bin/node",
        "/project/cli.ts",
        "--print",
        "--api-key",
        "sk-sensitive-value",
        "--",
        "List reservations",
      ],
      "List reservations",
    ),
    "./scripts/prime-agent.sh --print --api-key <redacted> -- \"<prompt in trace input>\"",
  );
});

test("extracts safe retry decisions for tool summaries", () => {
  assert.equal(
    extractRetryDecision({ retry: { decision: "retry_exhausted" } }),
    "retry_exhausted",
  );
  assert.equal(extractRetryDecision({ output: "private result" }), null);
  assert.equal(
    extractRetryDecision("{'retry': {'decision': 'complete'}, 'private': 'hidden'}"),
    "complete",
  );
});
