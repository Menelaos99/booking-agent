import base64

from booking_agent.integrations.gmail import (
    GmailAttachment,
    GmailMessage,
    _extract_body_and_attachments,
    build_gmail_match_preview,
)


def encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode()


def test_extracts_plain_body_and_attachment_metadata() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": encoded("Hello guest")}},
            {
                "mimeType": "image/jpeg",
                "filename": "passport.jpg",
                "body": {"attachmentId": "attachment-1", "size": 42},
            },
        ],
    }

    body, attachments = _extract_body_and_attachments(payload)

    assert body == "Hello guest"
    assert attachments[0].attachment_id == "attachment-1"
    assert attachments[0].filename == "passport.jpg"


def test_match_preview_masks_contacts_and_identity_data() -> None:
    message = GmailMessage(
        id="message-1",
        thread_id="thread-1",
        from_address="alice@example.com",
        to_addresses=["host@example.com"],
        subject="Question from alice@example.com",
        body=(
            "Call me on +30 690 000 0000.\n"
            "Passport AB1234567 is attached.\n"
            + "x" * 400
        ),
        occurred_at="2026-08-09T08:00:00+00:00",
        attachments=[
            GmailAttachment(
                attachment_id="attachment-1",
                filename="passport.jpg",
                mime_type="image/jpeg",
            )
        ],
    )
    preview = build_gmail_match_preview(
        {
            "id": 1,
            "reservation_id": "B-100",
            "customer_name": "Alice Example",
            "check_in": "2026-08-13",
            "match_method": "name_date",
            "confidence": 0.5,
            "status": "review_required",
        },
        message,
    )

    assert preview.masked_from == "a***@example.com"
    assert preview.masked_to == ["h***@example.com"]
    assert "alice@example.com" not in preview.subject
    assert "+30 690" not in preview.excerpt
    assert "AB1234567" not in preview.excerpt
    assert len(preview.excerpt) <= 240
    assert preview.attachment_count == 1
