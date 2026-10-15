from datetime import datetime, timedelta, timezone

from helpers import make_listing
from real_estate_bot.storage import Storage


def test_duplicate_detection_across_runs(tmp_path):
    db = tmp_path / "state" / "listings.db"
    first = Storage(db)
    assert first.register(make_listing()) is True
    first.close()

    second = Storage(db)  # a "new run" reopening the persisted file
    assert second.register(make_listing()) is False
    assert second.counts() == {"pending": 1}


def test_changed_url_is_same_listing():
    s = Storage(":memory:")
    assert s.register(make_listing(url="https://a/old")) is True
    assert s.register(make_listing(url="https://a/new")) is False
    assert s.pending()[0].url == "https://a/new"
    assert s.counts() == {"pending": 1}


def test_failed_notification_is_not_marked_sent():
    s = Storage(":memory:")
    s.register(make_listing())
    assert s.record_failure("yad2:L1", "boom", max_attempts=3) == "pending"
    assert s.status_of("yad2:L1") == "pending"
    assert [l.key for l in s.pending()] == ["yad2:L1"]  # will be retried next run


def test_gives_up_after_max_attempts():
    s = Storage(":memory:")
    s.register(make_listing())
    s.record_failure("yad2:L1", "e1", 2)
    assert s.record_failure("yad2:L1", "e2", 2) == "failed"
    assert s.pending() == []


def test_sent_listing_never_returns_to_pending():
    s = Storage(":memory:")
    s.register(make_listing())
    s.mark_sent("yad2:L1")
    assert s.register(make_listing()) is False
    assert s.pending() == [] and s.status_of("yad2:L1") == "sent"


def test_baseline_marks_existing_pending_as_seen():
    s = Storage(":memory:")
    s.register(make_listing())
    s.register(make_listing(), status="baseline")
    assert s.status_of("yad2:L1") == "baseline" and s.pending() == []


def test_retention_purges_old_but_keeps_pending():
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    s = Storage(":memory:", clock=lambda: now[0])
    s.register(make_listing(source_id="old"))
    s.mark_sent("yad2:old")
    s.register(make_listing(source_id="queued"))
    now[0] += timedelta(days=200)
    s.register(make_listing(source_id="fresh"))
    assert s.purge(retention_days=180) == 1
    assert s.status_of("yad2:old") is None
    assert s.status_of("yad2:queued") == "pending" and s.status_of("yad2:fresh") == "pending"


def test_stale_pending_expires():
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    s = Storage(":memory:", clock=lambda: now[0])
    s.register(make_listing())
    now[0] += timedelta(days=4)
    assert s.expire_pending(3) == 1 and s.status_of("yad2:L1") == "expired"


def test_roundtrip_preserves_listing():
    s = Storage(":memory:")
    original = make_listing(published_at=datetime(2026, 10, 14, 9, 30), rooms=3.5)
    s.register(original)
    assert s.pending()[0] == original


def test_pending_newest_first():
    s = Storage(":memory:")
    s.register(make_listing(source_id="a", published_at=datetime(2026, 10, 1)))
    s.register(make_listing(source_id="b", published_at=datetime(2026, 10, 9)))
    assert [l.source_id for l in s.pending()] == ["b", "a"]
