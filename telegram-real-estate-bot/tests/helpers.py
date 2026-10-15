"""Shared test helpers (plain functions - no live network, no real Telegram)."""

from __future__ import annotations

from dataclasses import replace

from real_estate_bot.config import AppConfig, SearchConfig, Settings
from real_estate_bot.models import Listing
from real_estate_bot.search.base import ListingProvider
from real_estate_bot.telegram_notifier import SendResult, TelegramError


def make_listing(**overrides) -> Listing:
    base = dict(
        source="yad2", source_id="L1", url="https://www.yad2.co.il/realestate/item/L1", title="דירת 4 חדרים",
        price_ils=1_600_000, city="חולון", neighborhood="קריית שרת", property_type="apartment", rooms=4.0,
        size_sqm=90.0, image_url="https://img.example/1.jpg", description="דירה יפה", seller_type=None,
    )
    base.update(overrides)
    return Listing(**base)


def make_search(**overrides) -> SearchConfig:
    base = dict(name="Holon", city="חולון", max_price_ils=1_750_000)
    base.update(overrides)
    return SearchConfig(**base)


def make_config(*searches: SearchConfig, **settings) -> AppConfig:
    base = dict(send_delay_seconds=0.0, request_delay_seconds=0.0)
    base.update(settings)
    return AppConfig(settings=Settings(**base), searches=tuple(searches) or (make_search(),))


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = headers if headers is not None else ({"Content-Type": "application/json"} if json_data is not None else {})

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Queue of canned responses (or exceptions). Records every call. Never touches the network."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[dict] = []

    def _next(self, kind, url, kwargs):
        self.calls.append({"kind": kind, "url": url, **kwargs})
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        return self._next("get", url, kwargs)

    def post(self, url, **kwargs):
        return self._next("post", url, kwargs)


class StaticProvider(ListingProvider):
    """Returns a fixed list per call (or raises)."""

    name = "static"

    def __init__(self, listings=None, error: Exception | None = None):
        self.listings = listings or []
        self.error = error
        self.calls = 0

    def search(self, search):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.listings)


class FakeNotifier:
    """Stands in for TelegramNotifier. `fail_keys` fail once per entry in `fail_times`."""

    def __init__(self, fail_keys=(), fatal=False):
        self.sent: list[Listing] = []
        self.errors: list[str] = []
        self.fail_keys = set(fail_keys)
        self.fatal = fatal

    def send_listing(self, listing):
        if self.fatal:
            raise TelegramError("bad token", status=401, fatal=True)
        if listing.key in self.fail_keys:
            raise TelegramError("telegram server error 500", status=500)
        self.sent.append(listing)
        return SendResult(used_photo=True)

    def send_error(self, message):
        self.errors.append(message)
        return True


def raw_item(**overrides) -> dict:
    """A synthetic feed item shaped like the (unverified) Yad2 legacy feed. NOT real Yad2 data."""
    item = {
        "id": "abc123", "link_token": "abc123", "price": "1,690,000 ₪", "city": "חולון", "neighborhood": "קריית שרת",
        "HomeTypeID_text": "דירה", "Rooms_text": "3.5", "square_meters": 82,
        "images_urls": ["https://img.example/abc123.jpg"], "date_added": "2026-10-14 10:21:04",
        "title_1": "דירה למכירה", "row_1": "קריית שרת, חולון",
    }
    item.update(overrides)
    return item


def feed(items, last_page=None) -> dict:
    payload = {"data": {"feed": {"feed_items": items}}}
    if last_page:
        payload["data"]["pagination"] = {"last_page": last_page}
    return payload
