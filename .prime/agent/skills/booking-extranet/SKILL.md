---
name: booking-extranet
description: Navigate and authenticate to the Booking.com Extranet and run controlled, retry-aware reservation, Gmail-correlation, arrival-draft, identity-status, and explicitly confirmed guest-reply workflows. Use for semantic movement between Extranet home, reservations, messages, and calendar sections; login; pre-arrival operations; transient or authentication failures; availability; pricing; and performance statistics.
---

# Booking Extranet

Use the Python-backed `booking_extranet` module. Never read `.env`, `state/session.json`, Gmail tokens, cookies, screenshots, or credential files.

## Authentication

1. Check `await booking_extranet.gmail_status()` and direct the user to `await booking_extranet.connect_gmail()` if disconnected.
2. Call `await booking_extranet.start_auth()` to begin Pulse → email → SMS authentication.
3. When status is `pulse_approval_required`, tell the user to approve in Pulse. After confirmation, call `await booking_extranet.auth_status(wait_seconds=30)`.
4. When status is `sms_confirmation_required`, ask permission. Call `await booking_extranet.confirm_sms(True)` only after approval.
5. When status is `sms_code_required`, ask for the code and pass it once to `await booking_extranet.submit_sms_code(code)`.
6. Never repeat, quote, store, or log a supplied code.

## Retry decisions

Read `result["retry"]["decision"]` after every tool call and follow it exactly:

- `complete`: continue; the tool may already have completed bounded internal retries.
- `poll_status`: poll the existing authentication process as instructed; never start a second one.
- `retry_original_once`: retry the original operation once after authentication becomes `verified`.
- `authenticate_then_retry`: call `start_auth()`, finish authentication, then retry the original operation once.
- `wait_for_user`: wait for the requested OAuth, Pulse, SMS, CAPTCHA, template, or other human action. Do not repeat the operation first.
- `inspect_before_retry`: a write may have succeeded despite a timeout. Inspect state with read-only tools and ask the user before trying again.
- `retry_exhausted`: stop retrying in the current turn and report the transient failure.
- `do_not_retry`: correct the input/configuration or report the terminal failure.

Safe and idempotent reads retry internally at most three times with short backoff. Never manually add retries around tool calls. Never reuse an OTP. Never automatically retry `confirm_reply`, `prepare_arrival_drafts`, or `review_gmail_match`.

## Semantic navigation

Use the persistent, read-only navigator for moving between known Extranet sections:

- `await booking_extranet.open_home()`
- `await booking_extranet.open_reservations(status="upcoming")`
- `await booking_extranet.open_messages(unread=False)`
- `await booking_extranet.open_calendar(month=None)`
- `await booking_extranet.current_page()`
- `await booking_extranet.go_back()`
- `await booking_extranet.close_navigation()`

Prefer an explicit `open_*` destination over `go_back()`. Use `go_back()` only when the previous semantic destination is intentional. If it returns `inspect_before_retry`, call `current_page()` before any further transition. Close navigation when finished; authentication and legacy operations also close it automatically.

Never invent or expose raw URLs, selectors, clicks, fills, DOM evaluation, HTML, screenshots, cookies, or session values. Navigation cannot change messages, prices, or availability.

## Property operations

Use only these functions:

- `await booking_extranet.list_messages(unread=False)`
- `await booking_extranet.read_message(message_id)`
- `await booking_extranet.list_reservations(status="upcoming")`
- `await booking_extranet.show_reservation(booking_id)`
- `await booking_extranet.list_unreplied()`
- `await booking_extranet.list_arrivals(arrival_date=None)`
- `await booking_extranet.list_pending_arrival_tasks(arrival_date=None, status="action_required")`
- `await booking_extranet.refresh_gmail_matches(booking_id)`
- `await booking_extranet.list_gmail_matches(booking_id=None, status="review_required")`
- `await booking_extranet.preview_gmail_match(match_id)`
- `await booking_extranet.review_gmail_match(match_id, approved)`
- `await booking_extranet.prepare_arrival_drafts(reference_date=None)`
- `await booking_extranet.identity_status(booking_id)`
- `await booking_extranet.arrival_dry_run(reference_date=None)`
- `await booking_extranet.view_availability(month=None)`
- `await booking_extranet.view_pricing(month=None)`
- `await booking_extranet.get_stats()`

Run one operation at a time. If authentication expires, restart the authentication workflow. Price and availability mutations remain manual CLI operations.

## Arrival workflow

1. Call `list_pending_arrival_tasks()` to identify reservations requiring action.
2. Call `refresh_gmail_matches(booking_id)` only for the selected stored reservation.
3. Inspect candidates with `list_gmail_matches()` and `preview_gmail_match(match_id)`. Previews contain masked addresses and a short redacted excerpt; never seek raw Gmail content or attachments.
4. For a weak match, show the preview and wait for the user's decision. Call `review_gmail_match(match_id, approved)` only after that explicit decision.
5. Call `arrival_dry_run()` before `prepare_arrival_drafts()`. Call `prepare_arrival_drafts()` only when the user asks to create drafts; it may create only the two approved-template drafts for the configured four-days-before arrivals and never sends them.
6. Use `identity_status(booking_id)` only for workflow state. Never request or expose passport numbers, ΑΦΜ values, nationality, attachments, or OCR text.

Do not use Gmail outside these reservation-scoped functions. Do not search arbitrary terms, return raw message or thread IDs, download attachments, create arbitrary drafts, or send Gmail messages.

## Confirmed Booking replies

1. Call `list_messages()` and select only a result whose `stable_ref` is true.
2. Call `prepare_reply(thread_ref)` to read and re-check that thread.
3. Draft a concise reply using the configured DeepSeek model. Never include passport, ΑΦΜ, credentials, or unrelated customer data.
4. Call `stage_reply(thread_ref, expected_guest, exact_text)`. This does not send anything.
5. Show the user the recipient and exact reply text, ask whether to post it, and wait.
6. Only after an explicit yes, call `confirm_reply(pending_id, True)`. For rejection, call it with `False`.

Never call `confirm_reply(..., True)` based on an earlier or implied approval. Pending replies expire and are single-use.
