import json
from datetime import datetime, timedelta, timezone

from helpers import FakeNotifier, StaticProvider, make_config, make_listing, make_search
from real_estate_bot.main import main, maybe_send_error_notice, run
from real_estate_bot.search.base import ProviderBlockedError, ProviderError
from real_estate_bot.storage import Storage


def test_listing_matching_several_searches_is_sent_once():
    cfg = make_config(make_search(name="A"), make_search(name="B", max_price_ils=2_000_000))
    store, notifier = Storage(":memory:"), FakeNotifier()
    result = run(cfg, StaticProvider([make_listing()]), store, notifier)
    assert result.matched == 1 and result.sent == 1 and len(notifier.sent) == 1


def test_no_duplicates_across_runs():
    cfg, store, notifier = make_config(), Storage(":memory:"), FakeNotifier()
    provider = StaticProvider([make_listing(source_id="1"), make_listing(source_id="2")])
    run(cfg, provider, store, notifier)
    second = run(cfg, provider, store, notifier)
    assert len(notifier.sent) == 2 and second.new == 0 and second.sent == 0
    run(cfg, StaticProvider([make_listing(source_id="1"), make_listing(source_id="3")]), store, notifier)
    assert sorted(l.source_id for l in notifier.sent) == ["1", "2", "3"]


def test_non_matching_listings_are_not_sent():
    cfg, store, notifier = make_config(), Storage(":memory:"), FakeNotifier()
    bad = [make_listing(source_id="x1", price_ils=2_500_000), make_listing(source_id="x2", image_url=None),
           make_listing(source_id="x3", description="משרד תיווך"), make_listing(source_id="x4", city="בת ים")]
    result = run(cfg, StaticProvider(bad), store, notifier)
    assert result.matched == 0 and notifier.sent == [] and result.exit_code == 0


def test_failed_telegram_send_is_retried_next_run_and_exit_code_is_nonzero():
    cfg, store = make_config(), Storage(":memory:")
    provider = StaticProvider([make_listing(source_id="1"), make_listing(source_id="2")])
    flaky = FakeNotifier(fail_keys={"yad2:1"})
    first = run(cfg, provider, store, flaky)
    assert first.exit_code == 1 and first.sent == 1
    assert store.status_of("yad2:1") == "pending" and store.status_of("yad2:2") == "sent"

    healthy = FakeNotifier()
    second = run(cfg, provider, store, healthy)
    assert second.exit_code == 0 and [l.source_id for l in healthy.sent] == ["1"]  # only the failed one, no duplicate of "2"
    assert store.status_of("yad2:1") == "sent"


def test_listing_parked_after_max_attempts():
    cfg, store = make_config(max_send_attempts=2), Storage(":memory:")
    provider, flaky = StaticProvider([make_listing()]), FakeNotifier(fail_keys={"yad2:L1"})
    run(cfg, provider, store, flaky)
    run(cfg, provider, store, flaky)
    assert store.status_of("yad2:L1") == "failed"
    healthy = FakeNotifier()
    run(cfg, provider, store, healthy)
    assert healthy.sent == []


def test_fatal_telegram_error_aborts_sending_but_keeps_listings_pending():
    cfg, store = make_config(), Storage(":memory:")
    result = run(cfg, StaticProvider([make_listing(source_id="1"), make_listing(source_id="2")]), store, FakeNotifier(fatal=True))
    assert result.exit_code == 1 and result.sent == 0 and store.pending_count() == 2


def test_failed_search_is_a_failure_but_other_searches_still_run():
    class Mixed(StaticProvider):
        def search(self, search):
            if search.name == "bad":
                raise ProviderBlockedError("403")
            return super().search(search)

    cfg = make_config(make_search(name="bad"), make_search(name="good"))
    notifier = Storage(":memory:"), FakeNotifier()
    result = run(cfg, Mixed([make_listing()]), notifier[0], notifier[1])
    assert result.exit_code == 1 and result.sent == 1 and "bad" in result.failures[0]


def test_zero_matches_is_success_but_failed_search_is_not():
    cfg, store = make_config(), Storage(":memory:")
    assert run(cfg, StaticProvider([]), store, FakeNotifier()).exit_code == 0
    assert run(cfg, StaticProvider(error=ProviderError("boom")), store, FakeNotifier()).exit_code == 1


def test_per_run_cap_defers_the_rest():
    cfg, store, notifier = make_config(max_notifications_per_run=2), Storage(":memory:"), FakeNotifier()
    provider = StaticProvider([make_listing(source_id=str(i)) for i in range(5)])
    run(cfg, provider, store, notifier)
    assert len(notifier.sent) == 2 and store.pending_count() == 3
    run(cfg, provider, store, notifier)
    run(cfg, provider, store, notifier)
    assert sorted(l.source_id for l in notifier.sent) == ["0", "1", "2", "3", "4"]


def test_baseline_records_but_sends_nothing_and_later_runs_only_send_new():
    cfg, store, notifier = make_config(), Storage(":memory:"), FakeNotifier()
    run(cfg, StaticProvider([make_listing(source_id="1")]), store, notifier, baseline=True)
    assert notifier.sent == [] and store.status_of("yad2:1") == "baseline"
    run(cfg, StaticProvider([make_listing(source_id="1"), make_listing(source_id="2")]), store, notifier)
    assert [l.source_id for l in notifier.sent] == ["2"]


def test_uncertain_seller_flag_reaches_notifier():
    cfg, store, notifier = make_config(), Storage(":memory:"), FakeNotifier()
    run(cfg, StaticProvider([make_listing(description="דירה יפה")]), store, notifier)
    assert notifier.sent[0].seller_status == "uncertain"


def test_error_notice_is_rate_limited_per_problem():
    store, notifier = Storage(":memory:"), FakeNotifier()
    t0 = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
    assert maybe_send_error_notice(store, notifier, ["boom"], now=t0) is True
    assert maybe_send_error_notice(store, notifier, ["boom"], now=t0 + timedelta(hours=6)) is False
    assert maybe_send_error_notice(store, notifier, ["different"], now=t0 + timedelta(hours=6)) is True
    assert maybe_send_error_notice(store, notifier, ["boom"], now=t0 + timedelta(hours=30)) is True
    assert len(notifier.errors) == 3


def test_cli_dry_run_end_to_end_and_exit_codes(tmp_path, capsys):
    mock = tmp_path / "l.json"
    mock.write_text(json.dumps([make_listing().to_dict()]), encoding="utf-8")
    cfg = tmp_path / "c.yaml"
    cfg.write_text('searches:\n  - name: T\n    city: "חולון"\n    max_price_ils: 1750000\n', encoding="utf-8")
    args = ["--config", str(cfg), "--db", str(tmp_path / "d.db"), "--provider", "mock", "--mock-file", str(mock), "--dry-run"]
    assert main(args) == 0
    assert "would send" in capsys.readouterr().out
    assert main(args) == 0
    assert "would send" not in capsys.readouterr().out  # duplicate suppressed on the second invocation
    assert main(["--config", str(tmp_path / "nope.yaml"), "--dry-run"]) == 2
