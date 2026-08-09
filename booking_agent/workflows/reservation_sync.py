from __future__ import annotations

import logging
from dataclasses import dataclass

from playwright.async_api import Page

from booking_agent.config import Settings
from booking_agent.modules.reservations import list_reservations, show_reservation
from booking_agent.storage.database import BookingDatabase
from booking_agent.utils.logging_utils import log_group

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReservationSyncResult:
    discovered: int
    stored: int
    review_required: int
    failed: int


async def sync_reservations(
    page: Page,
    settings: Settings,
    database: BookingDatabase,
    *,
    status: str = "upcoming",
) -> ReservationSyncResult:
    """Scrape reservation summaries/details and upsert them into SQLite."""

    summaries = await list_reservations(page, settings, status=status)
    stored = 0
    review_required = 0
    failed = 0
    for summary in summaries:
        booking_id = str(summary.get("booking_id", "")).strip()
        if not booking_id:
            failed += 1
            logger.warning("Skipping reservation without a Booking ID")
            continue
        with log_group(f"reservation {booking_id}", logger=logger):
            try:
                detail = await show_reservation(page, settings, booking_id)
                record = {**summary, **{key: value for key, value in detail.items() if value not in (None, "")}}
                resolution = database.upsert_reservation(record)
                stored += 1
                if resolution.needs_review:
                    review_required += 1
                    logger.warning("Customer contact conflict requires review")
                else:
                    logger.info("Stored (%s)", resolution.method)
            except Exception as exc:
                failed += 1
                logger.error("Failed to sync reservation: %s", exc)

    return ReservationSyncResult(
        discovered=len(summaries),
        stored=stored,
        review_required=review_required,
        failed=failed,
    )

