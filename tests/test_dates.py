from datetime import datetime, timezone

from applypilot.discovery.dates import normalize_posted_at


NOW = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)


def test_normalize_iso_datetime():
    assert normalize_posted_at("2026-04-10T09:30:00Z", now=NOW) == "2026-04-10T09:30:00+00:00"


def test_normalize_avature_date():
    assert normalize_posted_at("09-Apr-2026", now=NOW) == "2026-04-09T00:00:00+00:00"


def test_normalize_workday_relative_dates():
    assert normalize_posted_at("Posted Today", now=NOW) == "2026-04-11T12:00:00+00:00"
    assert normalize_posted_at("Posted Yesterday", now=NOW) == "2026-04-10T12:00:00+00:00"
    assert normalize_posted_at("Posted 3 Days Ago", now=NOW) == "2026-04-08T12:00:00+00:00"


def test_normalize_timestamp_milliseconds():
    assert normalize_posted_at(1775908800000, now=NOW) == "2026-04-11T12:00:00+00:00"


if __name__ == "__main__":
    test_normalize_iso_datetime()
    test_normalize_avature_date()
    test_normalize_workday_relative_dates()
    test_normalize_timestamp_milliseconds()
