"""Vision-based browser agent using Qwen2.5-VL via HuggingFace Inference API.

The agent sees a screenshot, decides what action to take, and returns it.
Our Playwright code executes the action.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime

from playwright.async_api import Page
from rich.console import Console

console = Console()


@dataclass
class AgentAction:
    """An action decided by the vision agent."""
    action: str        # type_email, click_next, type_password, click_submit, wait_human, navigate_extranet, done, wait
    reason: str = ""   # Why the agent chose this action


VALID_ACTIONS = [
    "enter_email",
    "enter_password",
    "fetch_otp",
    "verify_identity",
    "wait_human",
    "navigate_extranet",
    "done",
    "wait",
]

# Map actions back to states for compatibility with the state machine
ACTION_TO_STATE = {
    "enter_email": "email_form",
    "enter_password": "password_form",
    "fetch_otp": "2fa",
    "verify_identity": "2fa",
    "wait_human": "captcha",
    "navigate_extranet": "logged_in",
    "done": "extranet",
    "wait": "unknown",
}

PROMPT = """You are a browser automation agent logging into Booking.com's extranet.

Look at this screenshot and decide what to do next. Respond with EXACTLY one action and a short reason.

Format: ACTION | reason

Actions:
- enter_email | I see a login form with an email/username field (I will type the email and click Next)
- enter_password | I see a login form with a password field (I will type the password and click Sign in)
- fetch_otp | I see a verification code / OTP input field — I will fetch the code from email and type it
- verify_identity | I see a "verify your identity" page with options like SMS, Pulse app, or phone call
- wait_human | There is a CAPTCHA or visual challenge that needs human intervention
- navigate_extranet | I'm on a Booking.com page (not the sign-in page), need to go to the extranet
- done | I'm on the extranet dashboard (admin.booking.com)
- wait | Page is loading or I can't determine what to do

Example responses:
enter_email | I see a Username field on the login page
enter_password | I see a password field on the login page
fetch_otp | There is a verification code input asking for a code sent by email
verify_identity | I see "Please verify your identity" with SMS/Pulse/Phone options
wait_human | There is a CAPTCHA challenge on the page
done | I can see the extranet dashboard"""


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    console.print(f"[dim][{ts}][/dim] [magenta][AGENT][/magenta] {msg}")


async def get_agent_action(page: Page, hf_token: str = "") -> AgentAction:
    """Take a screenshot and ask the vision agent what action to take.

    Returns an AgentAction with the decided action and reasoning.
    """
    from huggingface_hub import InferenceClient

    screenshot_bytes = await page.screenshot()
    image_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

    client = InferenceClient(
        model="Qwen/Qwen2.5-VL-7B-Instruct",
        token=hf_token or None,
    )

    response = client.chat_completion(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
        max_tokens=50,
    )

    raw = response.choices[0].message.content.strip()

    # Parse "ACTION | reason" format
    parts = raw.split("|", 1)
    action_str = parts[0].strip().lower()
    reason = parts[1].strip() if len(parts) > 1 else ""

    # Match against valid actions
    for valid in VALID_ACTIONS:
        if valid in action_str:
            _log(f"[bold]{valid}[/bold] — {reason}")
            return AgentAction(action=valid, reason=reason)

    _log(f"Could not parse: {raw!r}")
    return AgentAction(action="wait", reason=f"Unparseable response: {raw}")


def action_to_state(agent_action: AgentAction) -> str:
    """Convert an AgentAction to a state machine state for compatibility."""
    return ACTION_TO_STATE.get(agent_action.action, "unknown")


# Keep backward compatibility
async def detect_page_state_vision(page: Page, hf_token: str = "") -> str:
    """Take a screenshot and classify the page state using the vision agent.

    Returns a state string compatible with the state machine.
    """
    agent_action = await get_agent_action(page, hf_token=hf_token)
    return action_to_state(agent_action)
