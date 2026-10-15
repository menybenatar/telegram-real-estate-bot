from datetime import datetime

import pytest

from helpers import FakeResponse, FakeSession, make_listing
from real_estate_bot.telegram_notifier import CAPTION_LIMIT, TelegramError, TelegramNotifier, build_message

TOKEN = "123456789:AAH-secret-token-abcdefghijklmnop"
OK = FakeResponse(200, {"ok": True, "result": {}})


def notifier(*responses, **kw):
    session = FakeSession(*responses)
    sleeps: list[float] = []
    n = TelegramNotifier(TOKEN, "42", session=session, send_delay=0, sleep=sleeps.append, **kw)
    return n, session, sleeps


def test_message_contains_available_fields_only():
    full = make_listing(published_at=datetime(2026, 10, 14), rooms=3.5, size_sqm=82, price_ils=1_690_000)
    text = build_message(full)
    for part in ("חולון — קריית שרת", "₪1,690,000", "Rooms: 3.5", "82 m²", "14/10/2026", full.url, "New apartment for sale"):
        assert part in text
    sparse = build_message(make_listing(neighborhood=None, rooms=None, size_sqm=None, published_at=None, title=None))
    assert "Rooms" not in sparse and "Size" not in sparse and "Published" not in sparse
    assert "📍 חולון\n" in sparse + "\n"


def test_message_is_html_escaped_and_within_caption_limit():
    text = build_message(make_listing(title="<b>x</b> & " + "y" * 3000))
    assert "<b>x</b>" not in text.replace("<b>&lt;b&gt;", "") and len(text) <= CAPTION_LIMIT


def test_uncertain_seller_is_flagged():
    assert "Could not verify" in build_message(make_listing(seller_status="uncertain"))
    assert "Could not verify" not in build_message(make_listing(seller_status="private"))


def test_hebrew_labels():
    assert "מחיר" in build_message(make_listing(), language="he")


def test_sends_photo_with_caption():
    n, session, _ = notifier(OK)
    result = n.send_listing(make_listing())
    call = session.calls[0]
    assert result.used_photo and call["url"].endswith("/sendPhoto")
    assert call["json"]["photo"] == "https://img.example/1.jpg" and "caption" in call["json"]


def test_photo_failure_falls_back_to_text_and_records_error():
    bad = FakeResponse(400, {"ok": False, "description": "Bad Request: wrong file identifier/HTTP URL specified"})
    n, session, _ = notifier(bad, OK)
    result = n.send_listing(make_listing())
    assert not result.used_photo and "photo failed" in result.photo_error
    assert session.calls[1]["url"].endswith("/sendMessage")
    assert make_listing().url in session.calls[1]["json"]["text"]


def test_listing_without_image_sends_text_directly():
    n, session, _ = notifier(OK)
    n.send_listing(make_listing(image_url=None))
    assert session.calls[0]["url"].endswith("/sendMessage") and len(session.calls) == 1


def test_retries_on_429_using_retry_after():
    limited = FakeResponse(429, {"ok": False, "parameters": {"retry_after": 7}})
    n, session, sleeps = notifier(limited, OK)
    assert n.send_listing(make_listing()).used_photo
    assert len(session.calls) == 2 and sleeps[0] >= 7


def test_retries_on_server_error_then_succeeds():
    n, session, _ = notifier(FakeResponse(502, {}), OK)
    n.send_listing(make_listing())
    assert len(session.calls) == 2


def test_gives_up_after_max_retries():
    n, session, _ = notifier(*[FakeResponse(500, {}) for _ in range(3)], max_retries=3)
    with pytest.raises(TelegramError) as err:
        n.send_listing(make_listing(image_url=None))
    assert not err.value.fatal and len(session.calls) == 3


@pytest.mark.parametrize("status,desc", [(401, "Unauthorized"), (403, "bot was blocked by the user"), (400, "Bad Request: chat not found")])
def test_fatal_errors_stop_immediately(status, desc):
    n, session, _ = notifier(FakeResponse(status, {"ok": False, "description": desc}))
    with pytest.raises(TelegramError) as err:
        n.send_listing(make_listing())
    assert err.value.fatal and len(session.calls) == 1  # no pointless text fallback


def test_network_error_does_not_leak_token():
    import requests

    leaky = requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: /bot{TOKEN}/sendPhoto")
    n, _, _ = notifier(leaky, leaky, leaky, max_retries=3)
    with pytest.raises(TelegramError) as err:
        n.send_listing(make_listing(image_url=None))
    assert TOKEN not in str(err.value) and "AAH-secret" not in str(err.value)


def test_send_error_never_raises_and_redacts():
    n, session, _ = notifier(OK)
    assert n.send_error(f"boom while calling /bot{TOKEN}/getMe") is True
    assert TOKEN not in session.calls[0]["json"]["text"]
    failing, _, _ = notifier(*[FakeResponse(401, {"ok": False})])
    assert failing.send_error("x") is False
