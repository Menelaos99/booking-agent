from pydantic import BaseModel


class PerformanceStats(BaseModel):
    review_score: str = ""
    total_reviews: str = ""
    page_views: str = ""
    bookings_count: str = ""
    cancellation_rate: str = ""
    occupancy_rate: str = ""
    average_daily_rate: str = ""
    revenue: str = ""
