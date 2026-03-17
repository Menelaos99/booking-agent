from __future__ import annotations

from playwright.async_api import Page

from booking_agent.config import Settings
from booking_agent.utils.selectors import (
    STATS_BOOKINGS_COUNT,
    STATS_CONTAINER,
    STATS_SCORE,
    STATS_VIEWS,
)
from booking_agent.utils.waits import human_delay

_ANALYTICS_PATH = "/hotel/hoteladmin/extranet_ng/manage/analytics.html"


def _analytics_url(settings: Settings) -> str:
    return (
        f"https://admin.booking.com{_ANALYTICS_PATH}"
        f"?hotel_id={settings.booking_hotel_id}"
    )


async def get_performance_stats(page: Page, settings: Settings) -> dict:
    """Scrape the performance / analytics dashboard."""
    await page.goto(_analytics_url(settings), wait_until="domcontentloaded", timeout=30_000)
    await human_delay(2000, 4000)

    stats: dict = {}

    try:
        await page.wait_for_selector(STATS_CONTAINER, timeout=15_000)
    except Exception:
        pass  # Still try to scrape individual elements

    selectors_map = {
        "review_score": STATS_SCORE,
        "page_views": STATS_VIEWS,
        "bookings_count": STATS_BOOKINGS_COUNT,
    }

    for key, selector in selectors_map.items():
        el = await page.query_selector(selector)
        stats[key] = (await el.inner_text()).strip() if el else "N/A"

    # Try to get additional stats from generic stat blocks
    stat_blocks = await page.query_selector_all(".stat-block, .metric, [data-testid='metric']")
    for block in stat_blocks:
        label_el = await block.query_selector(".label, .metric-label")
        value_el = await block.query_selector(".value, .metric-value")
        if label_el and value_el:
            label = (await label_el.inner_text()).strip().lower().replace(" ", "_")
            value = (await value_el.inner_text()).strip()
            if label not in stats:
                stats[label] = value

    return stats
