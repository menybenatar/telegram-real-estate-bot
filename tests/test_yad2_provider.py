import pytest

from helpers import FakeResponse, FakeSession, feed, make_search, raw_item
from real_estate_bot.search.base import (
    ProviderBlockedError, ProviderError, ProviderMalformedError, ProviderRateLimitedError,
)
from real_estate_bot.search.yad2 import Yad2Provider, extract_items

ROBOTS_OK = FakeResponse(200, text="User-agent: *\nAllow: /\n", headers={"Content-Type": "text/plain"})
ROBOTS_DENY = FakeResponse(200, text="User-agent: *\nDisallow: /\n", headers={"Content-Type": "text/plain"})


def provider(*responses, **kw):
    session = FakeSession(*responses)
    sleeps: list[float] = []
    kw.setdefault("max_retries", 3)
    return Yad2Provider(session=session, sleep=sleeps.append, request_delay=0, **kw), session, sleeps


def test_search_returns_normalized_listings_and_builds_params():
    p, session, _ = provider(ROBOTS_OK, FakeResponse(200, feed([raw_item(), raw_item(id="def456", link_token="def456")])))
    found = p.search(make_search(max_pages=1))
    assert [l.source_id for l in found] == ["abc123", "def456"]
    params = session.calls[1]["params"]
    assert params["city"] == 6600 and params["page"] == 1 and params["price"] == "0-1750000"


def test_extra_params_and_unknown_city_code():
    p, session, _ = provider(ROBOTS_OK, FakeResponse(200, feed([raw_item()])))
    p.search(make_search(max_pages=1, extra_params={"orderBy": 1}, city_code=1234))
    assert session.calls[1]["params"]["orderBy"] == 1 and session.calls[1]["params"]["city"] == 1234
    q, _, _ = provider()
    with pytest.raises(ProviderError, match="city_code"):
        q.build_params(make_search(city="עיר לא ידועה"), 1)


def test_pagination_stops_at_last_page_and_dedupes_overlap():
    p1 = FakeResponse(200, feed([raw_item(id="a1"), raw_item(id="a2")], last_page=2))
    p2 = FakeResponse(200, feed([raw_item(id="a2"), raw_item(id="a3")], last_page=2))
    p, session, sleeps = provider(ROBOTS_OK, p1, p2)
    found = p.search(make_search(max_pages=5))
    assert sorted(l.source_id for l in found) == ["a1", "a2", "a3"]
    assert len(session.calls) == 3  # robots + 2 pages, never asked for page 3


def test_stops_on_empty_page_and_respects_max_pages():
    p, session, _ = provider(ROBOTS_OK, FakeResponse(200, feed([raw_item(id="x")])), FakeResponse(200, feed([])))
    assert len(p.search(make_search(max_pages=5))) == 1
    q, qs, _ = provider(ROBOTS_OK, FakeResponse(200, feed([raw_item(id="1")])), FakeResponse(200, feed([raw_item(id="2")])))
    assert len(q.search(make_search(max_pages=2))) == 2 and len(qs.calls) == 3


def test_newest_listing_first():
    items = [raw_item(id="old", date_added="2026-10-01 10:00:00"), raw_item(id="new", date_added="2026-10-14 10:00:00")]
    p, _, _ = provider(ROBOTS_OK, FakeResponse(200, feed(items)))
    assert [l.source_id for l in p.search(make_search(max_pages=1))] == ["new", "old"]


def test_zero_results_is_not_a_failure():
    p, _, _ = provider(ROBOTS_OK, FakeResponse(200, feed([])))
    assert p.search(make_search()) == []


def test_unknown_structure_is_a_failure_not_zero_results():
    p, _, _ = provider(ROBOTS_OK, FakeResponse(200, {"something": "else"}))
    with pytest.raises(ProviderMalformedError, match="unrecognized"):
        p.search(make_search())
    with pytest.raises(ProviderMalformedError):
        extract_items(["not", "a", "mapping"])


def test_junk_items_are_skipped_not_fatal():
    p, _, _ = provider(ROBOTS_OK, FakeResponse(200, feed([raw_item(), {"type": "banner"}, "x", None])))
    assert len(p.search(make_search(max_pages=1))) == 1 and p.skipped_items == 3


def test_429_is_retried_with_retry_after_then_succeeds():
    limited = FakeResponse(429, {}, headers={"Retry-After": "9", "Content-Type": "application/json"})
    p, session, sleeps = provider(ROBOTS_OK, limited, FakeResponse(200, feed([raw_item()])))
    assert len(p.search(make_search(max_pages=1))) == 1
    assert 9 in sleeps


def test_persistent_429_raises_rate_limited():
    p, _, _ = provider(ROBOTS_OK, *[FakeResponse(429, {}) for _ in range(3)])
    with pytest.raises(ProviderRateLimitedError):
        p.search(make_search())


def test_temporary_5xx_retried_then_succeeds_and_gives_up():
    p, _, _ = provider(ROBOTS_OK, FakeResponse(503, {}), FakeResponse(200, feed([raw_item()])))
    assert len(p.search(make_search(max_pages=1))) == 1
    q, _, _ = provider(ROBOTS_OK, *[FakeResponse(500, {}) for _ in range(3)])
    with pytest.raises(ProviderError, match="500"):
        q.search(make_search())


@pytest.mark.parametrize("status", [401, 403])
def test_http_block_raises_and_is_never_retried(status):
    p, session, _ = provider(ROBOTS_OK, FakeResponse(status, {}))
    with pytest.raises(ProviderBlockedError):
        p.search(make_search())
    assert len(session.calls) == 2


def test_captcha_page_is_reported_as_blocked():
    page = FakeResponse(200, text="<html><title>ShieldSquare Captcha</title></html>", headers={"Content-Type": "text/html"})
    p, _, _ = provider(ROBOTS_OK, page)
    with pytest.raises(ProviderBlockedError):
        p.search(make_search())


def test_non_json_non_captcha_is_malformed():
    p, _, _ = provider(ROBOTS_OK, FakeResponse(200, text="<html>hello</html>", headers={"Content-Type": "text/html"}))
    with pytest.raises(ProviderMalformedError):
        p.search(make_search())


def test_network_errors_retried_without_leaking_details():
    import requests

    p, _, _ = provider(ROBOTS_OK, *[requests.ConnectionError("secret-host") for _ in range(3)])
    with pytest.raises(ProviderError) as err:
        p.search(make_search())
    assert "secret-host" not in str(err.value)


def test_robots_disallow_blocks_without_fetching_data():
    p, session, _ = provider(ROBOTS_DENY)
    with pytest.raises(ProviderBlockedError, match="robots.txt"):
        p.search(make_search())
    assert len(session.calls) == 1


def test_robots_missing_is_allowed_and_server_error_is_not():
    p, _, _ = provider(FakeResponse(404, text=""), FakeResponse(200, feed([raw_item()])))
    assert len(p.search(make_search(max_pages=1))) == 1
    q, _, _ = provider(FakeResponse(503, text=""))
    with pytest.raises(ProviderError, match="robots"):
        q.search(make_search())


def test_robots_can_be_disabled():
    p, session, _ = provider(FakeResponse(200, feed([raw_item()])), respect_robots=False)
    assert len(p.search(make_search(max_pages=1))) == 1 and len(session.calls) == 1


def test_probe_reports_success_and_failure():
    p, _, _ = provider(ROBOTS_OK, FakeResponse(200, feed([raw_item()])))
    report = p.probe(make_search())
    assert report["verdict"].startswith("OK") and report["parsed_items"] == 1 and report["fields_present"]["price_ils"] == 1
    q, _, _ = provider(ROBOTS_OK, FakeResponse(403, {}))
    assert q.probe(make_search())["verdict"].startswith("FAILED: ProviderBlockedError")
