from booking_agent.modules.reservations import (
    parse_booking_date,
    parse_guest_count,
    parse_money,
)


def test_parse_localized_money() -> None:
    assert parse_money("€1,234.56") == (123456, "EUR")
    assert parse_money("1.234,56 EUR") == (123456, "EUR")
    assert parse_money("€345") == (34500, "EUR")
    assert parse_money("") == (None, None)


def test_parse_dates_and_guest_count() -> None:
    assert parse_booking_date("13 Aug 2026") == "2026-08-13"
    assert parse_booking_date("13/08/2026") == "2026-08-13"
    assert parse_booking_date("not a date") is None
    assert parse_guest_count("2 adults, 1 child") == 2
