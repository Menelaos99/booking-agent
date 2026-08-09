#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"
HARNESS="$PROJECT_ROOT/src/prime-agent/prime-agent.sh"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE" >&2
  exit 1
fi

if [[ ! -x "$HARNESS" ]]; then
  echo "Prime Agent is not installed at $HARNESS" >&2
  exit 1
fi

read_env_value() {
  local name="$1"
  local value
  value="$(sed -n "s/^${name}=//p" "$ENV_FILE" | tail -n 1)"
  value="${value%$'\r'}"
  if [[ ${#value} -ge 2 ]]; then
    if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi
  printf '%s' "$value"
}

describe_langfuse_command() {
  local -a command_parts=("./scripts/prime-agent.sh")
  local argument
  local redact_next=false
  local prompt_started=false

  for argument in "$@"; do
    if [[ "$prompt_started" == "true" ]]; then
      continue
    fi
    if [[ "$redact_next" == "true" ]]; then
      command_parts+=("<redacted>")
      redact_next=false
      continue
    fi
    case "$argument" in
      --api-key|--system-prompt|--append-system-prompt|--goal)
        command_parts+=("$argument")
        redact_next=true
        ;;
      --)
        command_parts+=("--" "<prompt in trace input>")
        prompt_started=true
        ;;
      *)
        command_parts+=("$argument")
        ;;
    esac
  done

  local rendered=""
  local quoted
  for argument in "${command_parts[@]}"; do
    if [[ "$argument" == "<prompt in trace input>" ]]; then
      quoted='"<prompt in trace input>"'
    elif [[ "$argument" == "<redacted>" ]]; then
      quoted='<redacted>'
    else
      printf -v quoted '%q' "$argument"
    fi
    rendered+="${rendered:+ }${quoted}"
  done
  printf '%s' "$rendered"
}

DEEPSEEK_API_KEY="$(read_env_value DEEPSEEK_API_KEY)"
if [[ -z "$DEEPSEEK_API_KEY" ]]; then
  echo "DEEPSEEK_API_KEY is missing from $ENV_FILE" >&2
  exit 1
fi

LANGFUSE_PUBLIC_KEY="$(read_env_value LANGFUSE_PUBLIC_KEY)"
LANGFUSE_SECRET_KEY="$(read_env_value LANGFUSE_SECRET_KEY)"
LANGFUSE_BASE_URL="$(read_env_value LANGFUSE_BASE_URL)"

if [[ -n "$LANGFUSE_PUBLIC_KEY" || -n "$LANGFUSE_SECRET_KEY" ]]; then
  if [[ -z "$LANGFUSE_PUBLIC_KEY" || -z "$LANGFUSE_SECRET_KEY" ]]; then
    echo "Both LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required" >&2
    exit 1
  fi
  LANGFUSE_BASE_URL="${LANGFUSE_BASE_URL:-https://cloud.langfuse.com}"
  LANGFUSE_RUN_COMMAND="$(describe_langfuse_command "$@")"
  export LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY LANGFUSE_BASE_URL LANGFUSE_RUN_COMMAND
fi

export DEEPSEEK_API_KEY
export TSX_TSCONFIG_PATH="$PROJECT_ROOT/src/prime-agent/tsconfig.json"

cd "$PROJECT_ROOT"
exec "$HARNESS" "$@"
