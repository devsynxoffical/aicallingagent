from datetime import datetime, time, timezone

from callagent.dialer.campaign_runner import in_calling_window, next_window_open


def test_calling_window_respects_lead_timezone():
    # 15:00 UTC == 08:00 Los Angeles (PDT) -> before 09:00 window; == 11:00 New York -> inside.
    now = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)  # a Thursday
    assert not in_calling_window(now, "America/Los_Angeles", time(9, 0), time(19, 0), "UTC")
    assert in_calling_window(now, "America/New_York", time(9, 0), time(19, 0), "UTC")
    assert in_calling_window(now, None, time(9, 0), time(19, 0), "America/New_York")


def test_sunday_is_skipped_and_next_window_is_monday():
    sunday = datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc)
    assert not in_calling_window(sunday, "America/New_York", time(9, 0), time(19, 0), "UTC")
    nxt = next_window_open(sunday, "America/New_York", time(9, 0), "UTC")
    local = nxt.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
    assert local.weekday() == 0 and local.hour == 9
