import pytest

from helpers import raw_item
from real_estate_bot.search.yad2 import (
    extract_listing_id, normalize_property_type, parse_datetime, parse_float, parse_item, parse_price,
)


@pytest.mark.parametrize("value,expected", [
    ("1,690,000 ₪", 1_690_000), ("₪ 1,690,000", 1_690_000), ("1690000", 1_690_000), (1_690_000, 1_690_000),
    (1_690_000.0, 1_690_000), ("₪1.690.000", 1_690_000), (" 2 500 000 ", 2_500_000),
    (None, None), ("", None), ("לא צוין מחיר", None), ("1.5 מיליון", None), (0, None), (-5, None), (True, None),
    (float("nan"), None), ("₪", None),
])
def test_parse_price(value, expected):
    assert parse_price(value) == expected


@pytest.mark.parametrize("value,expected", [("3.5", 3.5), ("4", 4.0), (3, 3.0), ("3,5 חדרים", 3.5), (None, None), ("", None), (0, None), ("x", None)])
def test_parse_float(value, expected):
    assert parse_float(value) == expected


def test_parse_datetime_formats():
    assert parse_datetime("2026-10-14 10:21:04").day == 14
    assert parse_datetime("14/10/2026").month == 10
    assert parse_datetime("2026-10-14T10:21:04+00:00").year == 2026
    assert parse_datetime("garbage") is None and parse_datetime(None) is None


@pytest.mark.parametrize("text,expected", [
    ("דירה", "apartment"), ("דירת גן", "garden_apartment"), ("גג/פנטהאוז", "penthouse"), ("דופלקס", "duplex"),
    ("בית פרטי/קוטג'", "house"), ("מגרש", "other:מגרש"), (None, None), ("", None),
])
def test_normalize_property_type(text, expected):
    assert normalize_property_type(text) == expected


def test_listing_id_prefers_id_field():
    assert extract_listing_id({"id": 12345, "link_token": "zzz"}) == "12345"


def test_listing_id_falls_back_to_token_then_url():
    assert extract_listing_id({"link_token": "tok9"}) == "tok9"
    assert extract_listing_id({"link": "https://www.yad2.co.il/realestate/item/tel-aviv-area/abc999?x=1"}) == "abc999"
    assert extract_listing_id({"url": "/item/abc777"}) == "abc777"


def test_listing_id_missing():
    assert extract_listing_id({}) is None
    assert extract_listing_id({"id": "  ", "foo": 1}) is None
    assert extract_listing_id({"id": {"nested": 1}}) is None


def test_parse_item_normalizes_fields():
    listing = parse_item(raw_item())
    assert listing.key == "yad2:abc123"
    assert listing.price_ils == 1_690_000
    assert listing.city == "חולון" and listing.neighborhood == "קריית שרת"
    assert listing.property_type == "apartment"
    assert listing.rooms == 3.5 and listing.size_sqm == 82
    assert listing.image_url == "https://img.example/abc123.jpg"
    assert listing.published_at.day == 14
    assert listing.url.endswith("/abc123")


def test_parse_item_missing_data_stays_none():
    listing = parse_item({"id": "x1"})
    assert listing.price_ils is None and listing.image_url is None and listing.city is None
    assert listing.rooms is None and listing.published_at is None and listing.property_type is None


def test_parse_item_rejects_junk_and_bad_images():
    assert parse_item("banner") is None
    assert parse_item({"type": "ad"}) is None
    assert parse_item(raw_item(images_urls=["javascript:alert(1)", "not a url"])).image_url is None
    assert parse_item(raw_item(images_urls=[{"src": "https://img.example/ok.jpg"}])).image_url == "https://img.example/ok.jpg"


def test_parse_item_structured_seller_flags():
    assert parse_item(raw_item(merchant=True)).seller_type == "broker"
    assert parse_item(raw_item(is_private=True)).seller_type == "private"
    assert parse_item(raw_item(merchant=False)).seller_type is None  # absence of a broker flag proves nothing
