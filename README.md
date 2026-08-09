# Booking Agent

Local Booking.com and Gmail workflow for reservations, guest messages, pre-arrival emails, and identity-document tracking. It stores operational metadata in SQLite, prepares Gmail drafts four days before arrival, and keeps all sends behind human review.

## Safety model

- Arrival emails are created as Gmail drafts; this project never sends them automatically.
- Prime/DeepSeek can draft a Booking.com reply, but posting requires a separate, explicit confirmation.
- Passport and ΑΦΜ values are stored locally for manual verification. Images, PDFs, email bodies, and OCR text are not retained.
- Booking cookies, Gmail OAuth tokens, the SQLite database, logs, and `.env` stay under ignored local paths.
- Customer records are joined automatically only by Booking reservation ID, exact email, or exact phone. Name-only Gmail matches require review.

## Install

```bash
uv sync --dev
uv run playwright install chromium
brew install tesseract
git clone https://github.com/PrimeIntellect-ai/prime-agent src/prime-agent
npm --prefix src/prime-agent install
```

Copy `.env.example` to `.env` and fill in the Booking and DeepSeek values. Langfuse is optional. Put the Google OAuth desktop-app credentials at `state/credentials.json`, then protect local state:

```bash
mkdir -p state
chmod 700 state
chmod 600 .env state/credentials.json
```

The workflow settings live in `config/arrivals.yaml`. The default database is `state/booking_agent.sqlite3`, the property timezone is `Europe/Athens`, and the target is four days before arrival.

## One-time authentication

Open a normal Chrome window and sign in to Booking.com:

```bash
uv run booking login
```

If Booking asks for a sensitive-page verification method, use one of:

```bash
uv run booking auth ensure --method email
uv run booking auth ensure --method sms
```

The verified Booking browser session is saved locally. Check it without starting a login:

```bash
uv run booking login --check
```

Authorize the configured Gmail account for read access and draft creation. An older read-only token must be reconnected once so it receives the compose scope:

```bash
uv run booking auth gmail-connect
uv run booking auth gmail-status
```

Google's `gmail.compose` OAuth scope technically permits both draft management and sending. This code intentionally exposes draft creation only and contains no Gmail send call.

## Initialize and inspect the database

```bash
uv run booking db status --config config/arrivals.yaml
uv run booking reservations sync --config config/arrivals.yaml
uv run booking arrivals list --config config/arrivals.yaml
```

The reservation/customer view includes customer name, email, phone, arrival, checkout, nights, guest count, room type, amount/currency, Booking ID, identity status, Gmail match status, and both pre-arrival email states. The database also keeps booking date, arrival time, language, country, payment status, special requests, commission, net amount, communication metadata, and sync history when Booking exposes them.

## Import the two historical email templates

This searches recent Sent mail, lets you select one apartment-instructions email and one recommendations email, opens each in `$EDITOR`, and asks for approval before versioning it locally:

```bash
uv run booking email-templates import --config config/arrivals.yaml
```

Supported placeholders are:

```text
{{ customer_name }}  {{ email }}         {{ arrival_date }}
{{ checkout_date }}  {{ nights }}        {{ room_type }}
{{ guest_count }}    {{ booking_id }}    {{ amount }}
{{ currency }}       {{ identity_request }}
```

The instructions template always includes a request for Greek guests to reply with their ΑΦΜ and non-Greek guests to reply with passport details or attach a passport image. The workflow does not infer nationality.

## Run the four-days-before workflow

Always inspect a dry run first:

```bash
uv run booking arrivals run --config config/arrivals.yaml --dry-run
```

Then create the two Gmail drafts for each eligible arrival:

```bash
uv run booking arrivals run --config config/arrivals.yaml
```

The run is idempotent: it will not create another draft for the same reservation, template kind, and template version. If a draft was manually sent in Gmail, the next run reconciles it as sent.

Review ambiguous name-based Gmail correlations before they can affect a customer:

```bash
uv run booking arrivals matches --config config/arrivals.yaml
uv run booking arrivals review-match MATCH_ID --config config/arrivals.yaml
# Or reject it:
uv run booking arrivals review-match MATCH_ID --reject --config config/arrivals.yaml
```

## Passport and ΑΦΜ tracking

Incoming Gmail images/PDFs are OCRed locally with Tesseract. Only the minimal extracted passport number/nationality or ΑΦΜ is stored, always as `needs_review`. ΑΦΜ values are checksum-validated; passport MRZ check digits are validated when an MRZ is available.

For information received through WhatsApp, enter it manually:

```bash
uv run booking identity record BOOKING_ID --type passport --number NUMBER --nationality COUNTRY --source whatsapp
uv run booking identity record BOOKING_ID --type afm --number NINE_DIGIT_AFM --source whatsapp
```

After comparing the value with the original message or image:

```bash
uv run booking identity verify BOOKING_ID
# Or reject it:
uv run booking identity verify BOOKING_ID --reject
```

## Reply to Booking.com messages

List messages and run the interactive DeepSeek-assisted reply flow:

```bash
uv run booking messages list
uv run booking messages smart-reply
```

The flow opens the selected conversation, redacts email addresses, phone numbers, and long identifiers before prompting DeepSeek through Prime Agent, shows the exact recipient and reply, and asks before posting it.

The Prime skill exposes the same protection as two separate operations: `stage_reply` only keeps the proposed text in process memory for ten minutes; `confirm_reply` is one-use and sends only after the user explicitly approves it.

## Scheduling on macOS

Only install the schedule after Booking login, Gmail compose authorization, template import, and a successful dry run:

```bash
./scripts/install-arrival-schedule.sh
```

The LaunchAgent checks hourly and runs at most once per Athens calendar day after 09:00. It does not attempt an interactive Booking login when the saved session has expired; the failure is recorded and the operator must run `uv run booking login`.

Inspect its status and log:

```bash
launchctl print "gui/$(id -u)/com.menelaos.booking-agent.arrivals"
tail -n 100 state/arrival-scheduler.log
```

## Prime Agent and optional Langfuse

Run the checked-in wrapper so Prime uses the local DeepSeek and optional Langfuse settings without printing them:

```bash
./scripts/prime-agent.sh
```

The `booking-extranet` skill exposes reservation-scoped workflow tools:

- `list_pending_arrival_tasks` returns action state without email, phone, passport, or ΑΦΜ values.
- `refresh_gmail_matches` searches only with one stored reservation's fixed identifiers.
- `list_gmail_matches` returns local match metadata; `preview_gmail_match` adds only masked addresses and a redacted 240-character excerpt.
- `review_gmail_match` accepts or rejects one pending match after an explicit user decision.
- `prepare_arrival_drafts` creates only the two approved-template drafts and cannot send them.
- `identity_status` returns workflow state and source channel without document values, nationality, attachments, or OCR text.

The skill does not expose arbitrary Gmail search, raw message/thread identifiers, attachment download, arbitrary recipients, delete/archive operations, or Gmail sending.

It also exposes one persistent, read-only semantic navigator backed by Playwright. The implementation lives under `booking_agent/tools/`; the existing Prime skill is the only agent-facing skill. From Prime's persistent Python kernel:

```python
await booking_extranet.open_home()
await booking_extranet.open_reservations(status="upcoming")
await booking_extranet.open_messages(unread=True)
await booking_extranet.open_calendar(month="2026-08")
await booking_extranet.current_page()
await booking_extranet.go_back()
await booking_extranet.close_navigation()
```

The navigator keeps one headless Chromium page alive during the agent session and returns only structured section data and semantic history. It exposes no arbitrary URL, selector, click, fill, DOM, cookie, or screenshot capability, and it cannot change Booking data. Explicit authentication or a legacy CLI-backed operation closes the navigator first so the saved-session lock remains exclusive.

Prime tool results include a structured `retry.decision`. Safe/idempotent reads retry internally up to three times with short backoff. Authentication failures switch to the explicit authentication flow; after `verified`, Prime retries the original read once. Human challenges pause without retrying, exhausted transient failures stop for the current turn, and ambiguous write failures require state inspection. Reply posting, draft creation, and match approval are never retried automatically.

Langfuse is optional observability for allowlisted operations. The extension redacts configured sensitive fields; reply text and identity data must not be attached to traces.

To capture one reviewable end-to-end CLI run with redacted prompt and visible output, opt in for that invocation:

```bash
LANGFUSE_CAPTURE_CONTENT=true ./scripts/prime-agent.sh --print --no-session -- \
  "Run one read-only Booking check and summarize the result."
```

In Langfuse, open **Observability → Tracing** and filter for `booking-agent-cli-run`. The root agent observation contains the sanitized command, timestamp, input prompt, and final output or failure. Child `generate-agent-turn` and `booking:*`/`tool:*` observations show the model and tool sequence, retry status, token usage, cost, and errors. Hidden model reasoning and raw tool arguments/results are never attached; ask for a short visible decision summary when that context is useful. Keep content capture off for guest replies, identity workflows, or prompts containing customer data.

## Tests

```bash
uv run pytest -q
npm --prefix .prime/agent/extensions/booking-observability test
uv run python -m compileall -q booking_agent
```
