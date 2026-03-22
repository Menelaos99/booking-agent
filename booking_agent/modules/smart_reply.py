"""Smart reply — draft personalized replies using prokat templates + LLM."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from rich.console import Console

console = Console()

PROKAT_FILE = Path("/Users/menelaos/Documents/obsidian_sync/Random/Booking prokat texts.md")
PAST_REPLIES_CACHE = Path("/Users/menelaos/Projects/booking-agent/state/past_replies.json")

REPLY_PROMPT = """You are Menelaos, a hotel host replying to a guest on Booking.com for the property "Blue Door (in the castle of Monembasia)".

Guest name: {guest_name}
Guest message:
{guest_message}

=== PRIORITY 1: Your past replies to similar guests ===
Study these real conversations you've had before. Match the tone, style, and level of detail.
{past_replies}

=== PRIORITY 2: Your template responses (prokat texts) ===
Use these as a base if no past reply is a good match.
{templates}

Instructions:
- This is an INITIAL message (first contact with this guest — no prior conversation)
- FIRST check your past replies for a similar situation — if you find one, use it as the primary basis and adapt it
- If no past reply fits, fall back to the prokat templates
- Match the language of the guest's message (Greek → Greek, English → English)
- Personalize: replace placeholders with the guest's actual name
- If the guest asks something specific (late check-in, pets, etc.), address that FIRST, then include relevant info
- Keep the tone warm, professional, and hospitable — match the style of your past replies
- Sign as "Menelaos" (English) or "Μενέλαος" (Greek)
- Do NOT include subject lines or "Re:" — just the message body

Reply ONLY with the message text, nothing else."""


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    console.print(f"[dim][{ts}][/dim] [bold cyan][AGENT][/bold cyan] {msg}")


def load_prokat_templates() -> str:
    """Load prokat templates from the Obsidian vault."""
    if not PROKAT_FILE.exists():
        raise FileNotFoundError(f"Prokat templates not found at {PROKAT_FILE}")
    return PROKAT_FILE.read_text()


def load_past_replies() -> str:
    """Load cached past replies. Returns formatted string or empty."""
    if not PAST_REPLIES_CACHE.exists():
        _log("[yellow]past_replies.json not found — run 'booking messages learn' to scrape past conversations[/yellow]")
        return "(No past replies cached yet.)"
    try:
        data = json.loads(PAST_REPLIES_CACHE.read_text())
        parts = []
        for conv in data:
            parts.append(f"--- Conversation with {conv['guest_name']} ---\n{conv['conversation']}\n")
        if parts:
            _log(f"Loaded past_replies.json ({len(data)} conversations)")
            return "\n".join(parts)
        _log("[yellow]past_replies.json is empty[/yellow]")
        return "(No past replies found)"
    except Exception:
        _log("[red]Error loading past_replies.json[/red]")
        return "(Error loading past replies)"


def save_past_replies(conversations: list[dict]) -> None:
    """Save scraped conversations to cache."""
    PAST_REPLIES_CACHE.parent.mkdir(parents=True, exist_ok=True)

    # Merge with existing cache
    existing = []
    if PAST_REPLIES_CACHE.exists():
        try:
            existing = json.loads(PAST_REPLIES_CACHE.read_text())
        except Exception:
            pass

    # Deduplicate by guest name
    seen = {c["guest_name"] for c in existing}
    for conv in conversations:
        if conv["guest_name"] not in seen:
            existing.append(conv)
            seen.add(conv["guest_name"])

    PAST_REPLIES_CACHE.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
    _log(f"Cached {len(existing)} past conversations to {PAST_REPLIES_CACHE}")


async def generate_reply(guest_message: str, guest_name: str, hf_token: str = "") -> str:
    """Generate a personalized reply using past replies + prokat templates + HF LLM."""
    from huggingface_hub import InferenceClient

    templates = load_prokat_templates()
    past_replies = load_past_replies()

    prompt = REPLY_PROMPT.format(
        guest_name=guest_name,
        guest_message=guest_message,
        past_replies=past_replies,
        templates=templates,
    )

    _log("Generating reply (past replies + prokat templates)...")

    client = InferenceClient(
        model="Qwen/Qwen2.5-7B-Instruct",
        token=hf_token or None,
    )

    response = client.chat_completion(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1000,
    )

    reply = response.choices[0].message.content.strip()
    return reply


def edit_in_editor(draft: str, guest_message: str = "") -> str:
    """Open the draft in the user's $EDITOR for editing.

    Shows the guest's message as a read-only reference above the editable draft.
    Returns the edited text (without the guest message header).
    """
    editor = os.environ.get("EDITOR", "nano")

    # Build the file content: guest message as reference + draft to edit
    content = ""
    if guest_message:
        content += "# ─── Guest's message (DO NOT EDIT — reference only) ───\n"
        for line in guest_message.strip().split("\n"):
            content += f"# {line}\n"
        content += "# ─────────────────────────────────────────────────────\n\n"
    content += draft

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, prefix="booking_reply_") as f:
        f.write(content)
        tmp_path = f.name

    try:
        subprocess.run([editor, tmp_path], check=True)
        with open(tmp_path) as f:
            edited = f.read()
        # Strip out the guest message reference lines (lines starting with #)
        lines = edited.split("\n")
        reply_lines = [l for l in lines if not l.startswith("#")]
        return "\n".join(reply_lines).strip()
    finally:
        os.unlink(tmp_path)


def _is_greek(text: str) -> bool:
    """Check if text contains Greek characters."""
    return any("\u0370" <= c <= "\u03FF" or "\u1F00" <= c <= "\u1FFF" for c in text)


def append_to_prokat(text: str, label: str) -> None:
    """Append a reply to the prokat templates file."""
    content = PROKAT_FILE.read_text()
    lang = "Greek" if _is_greek(text) else "English"

    entry = f"\n**{label}:**\n{text}\n"

    if lang == "Greek":
        # Insert before the English section (### English)
        if "### English" in content:
            content = content.replace("### English", f"{entry}\n### English")
        else:
            content += entry
    else:
        # Append at the end
        content += entry

    PROKAT_FILE.write_text(content)
    _log(f"Saved to prokat texts under: [bold]{label}[/bold] ({lang})")


def _ask_save_to_learned(text: str, guest_name: str, guest_message: str = "") -> None:
    """Ask if the user wants to save the reply to past_replies.json for future learning."""
    save = input("  Save this reply for future learning? (y/n): ")

    if save.strip().lower() not in ("y", "yes"):
        return

    # Build a conversation entry matching the scrape format
    conversation = ""
    if guest_message:
        conversation += f"{guest_name}:\n{guest_message}\n\nYour reply:\n{text}"
    else:
        conversation = text

    save_past_replies([{
        "guest_name": guest_name,
        "conversation": conversation[:1500],
    }])
    _log(f"Saved reply to {guest_name} in past_replies.json")


async def edit_in_terminal(draft: str, guest_name: str = "", guest_message: str = "", hf_token: str = "") -> str | None:
    """Show draft and let user edit it. Returns final text or None to cancel."""
    console.print()
    console.print("[bold green]─── Draft Reply ───[/bold green]")
    console.print(draft)
    console.print("[bold green]───────────────────[/bold green]")
    console.print()

    import sys
    sys.stdout.flush()

    answer = input("  Send this reply? (y)es / (e)dit / (c)ancel: ")
    answer = answer.strip().lower()

    if answer in ("y", "yes", ""):
        return draft
    elif answer in ("e", "edit"):
        _log("Opening editor...")
        edited = edit_in_editor(draft, guest_message=guest_message)
        if not edited:
            _log("[yellow]Empty text — cancelled[/yellow]")
            return None
        console.print()
        console.print("[bold cyan]─── Edited Reply ───[/bold cyan]")
        console.print(edited)
        console.print("[bold cyan]────────────────────[/bold cyan]")
        console.print()
        sys.stdout.flush()
        confirm = input("  Send this? (y/n): ")
        if confirm.strip().lower() in ("y", "yes", ""):
            _ask_save_to_learned(edited, guest_name, guest_message)
            return edited
        _log("Cancelled.")
        return None
    else:
        _log("Cancelled.")
        return None
