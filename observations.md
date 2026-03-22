# Login Flow Debug Observations

## Iteration 1 — OTP field selector fix + broader input search

**Problem:** Agent found the code (`TTKCMT`) but `Could not fill OTP field`.

**Root cause:** OTP selector `input[name="otp"], input[name="code"]` didn't match Booking.com's actual input field.

**Fix applied:**
1. Added broader selector fallback: tries `input[type="text"]`, `input[autocomplete="one-time-code"]`, generic visible inputs
2. Added debug screenshot + input dump when filling fails

**Result:** Agent found the input via broader selector and typed the code successfully.

## Iteration 2 — Full login flow test

**Run at 13:49** — SUCCESSFUL LOGIN!

**Full flow observed:**
```
1. [AGENT] enter_email — typed email, clicked Next           ✓
2. [AGENT] wait_human — CAPTCHA detected                     ✓ (auto-resolved)
3. [AGENT] fetch_otp — found code QMG4QR from Gmail          ✓
4. [AGENT] typed code into verification field                 ✓
5. [AGENT] clicked Verify                                    ✓
6. [AGENT] navigate_extranet — tried to go to extranet       ✗ (redirected to sign-in)
7. [AGENT] enter_password — typed password, clicked Submit    ✓
8. [AGENT] enter_email — form reset, re-entered email        ✓
9. [AGENT] enter_password — typed password again              ✓
10. [AGENT] done — reached extranet!                          ✓
```

**Key observations:**
- The CAPTCHA auto-resolved (no human intervention needed!)
- OTP code fetched and typed automatically
- After OTP verification, Booking.com redirected to sign-in requiring email+password again
- The state machine correctly re-entered credentials after the redirect
- Login succeeded after ~2.5 minutes total
- The `navigate_extranet` tool failed because we weren't actually logged in yet — the agent correctly fell back to re-entering credentials

**Issues found (non-blocking):**
1. `wait_human` triggered twice for CAPTCHAs that auto-resolved — the `wait_for_challenge_cleared` could have shorter initial wait
2. Multiple `wait` states between actions — the agent is unsure when page is loading
3. The password typing took 34 seconds (13:51:28 → 13:52:02) — seems too slow for `human_type`

**Status: LOGIN FLOW WORKING END-TO-END WITH HF AGENT**

## Iteration 3 — Messages page investigation

**Problem:** `uv run booking messages list` returns "No messages found" — the messaging page redirects to sign-in.

**Root cause:** The login succeeds (agent sees extranet momentarily) but the session cookies aren't fully established. When navigating to a different extranet page (messaging), Booking.com requires re-authentication. The session saved from the login context doesn't carry over properly.

**Evidence:**
- `login --check` passed (extranet home loads)
- But navigating to `/messaging.html` within the same authenticated context redirects to sign-in
- Screenshot confirms: sign-in page with "Username" field

**Hypothesis:** The login flow reaches the extranet briefly (during a redirect chain) but the auth cookies need time to propagate. The DOM fallback detects "extranet" from URL matching during a redirect, before cookies are fully set.

**Iteration 3 result:** Session verification added. Login reaches extranet. Session IS valid for extranet home page.

## Iteration 4 — Messages page always redirects to sign-in

**Critical finding:** The session is valid for `admin.booking.com/.../home.html` but NOT for `admin.booking.com/.../messaging.html`. Navigating to the messaging page within the same authenticated browser context still redirects to sign-in.

This means:
1. The extranet home page and messaging page have DIFFERENT auth requirements
2. OR the messaging URL path (`/hotel/hoteladmin/extranet_ng/manage/messaging.html`) doesn't exist / has changed
3. OR the messaging page requires clicking through the extranet UI (not direct URL navigation)

**Next step:** Instead of navigating directly to the messaging URL, try clicking the "Messages" link/button within the extranet dashboard. The extranet likely uses internal navigation that carries auth tokens in the URL params (like `ses=...`).

## Iteration 5 — Correct URL found, but auth-assurance blocks access

**Discovery 1:** Correct messages URL is `/messaging/inbox.html` (not `messaging.html`). Needs `ses=` param from extranet session.

**Discovery 2:** Even with correct URL + ses param, Booking.com redirects to `auth-assurance` page — **"Please verify your identity"** with options:
- via Pulse app
- via Text message (SMS)
- via Phone call
- Unable to verify?

This is a SECONDARY verification (not the login OTP). Booking.com requires identity re-verification to access the messaging inbox. This is a security feature specifically for sensitive pages.

**Screenshot:** `state/debug_auth_assurance.png`

**Options to handle this:**
1. Add SMS verification as an agent tool (receive SMS → extract code → type it)
2. Use the Pulse app option (requires the Booking.com Pulse app)
3. Click "Unable to verify?" to see alternatives
4. Check if there's an API endpoint for messages that doesn't require auth-assurance

**Status: BLOCKED by auth-assurance — need user input on which verification method to use**

## Iteration 6 — SMS verification flow working

**Problem:** Agent clicked wrong element (root `<HTML>` containing "+49" text) instead of the dropdown option.

**Root cause:** The page uses a native `<SELECT>` dropdown for phone number selection, not clickable buttons. Searching all `*` elements for "+49" text matched the page root element.

**Fix:** Use Playwright's `select_option()` for native `<SELECT>` elements. Found the dropdown via `select` selector, read options, selected the +49 value.

**Working flow:**
```
1. Agent clicks "Text message (SMS)" on auth-assurance page         ✓
2. Page shows "Select phone number" dropdown                        ✓
3. Agent finds <SELECT> with options: +49*******5136, +30******4994  ✓
4. Agent selects +49 (German) via select_option()                   ✓
5. Agent clicks "Send verification code"                            ✓
6. Terminal prompts: "Enter SMS code: "                             ✓
7. User types code → agent fills it in browser → clicks verify      (pending test)
```

**Status: SMS FLOW WORKING — awaiting user test with actual SMS code**

## Iteration 7 — Messages retrieved successfully!

**Fixes applied:**
1. Correct inbox URL: `/messaging/inbox.html` (not `messaging.html`)
2. Session parameter `ses=` required in all extranet URLs
3. Auth-assurance handling: SMS → select +49 → enter code → verify
4. New selectors matching actual DOM: `.list-item__title-text` for guest names, `<button>` containers for message items

**Result:**
```
┃ ID ┃ Guest         ┃ Subject                           ┃ Date       ┃
┃ 0  ┃ Anna Petta    ┃ I want to request check-in at...  ┃ 4 Mar 2026 ┃
┃ 1  ┃ Sabbas Salias ┃ καλησπέρα έχουμε ένα σκυλάκι...   ┃ 2 Mar 2026 ┃
```

**FULL PIPELINE WORKING: login → stealth → auth-assurance → SMS → inbox → messages**
