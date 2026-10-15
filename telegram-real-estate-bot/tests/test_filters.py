import pytest

from helpers import make_listing, make_search
from real_estate_bot.config import Settings
from real_estate_bot.filters import classify_seller, evaluate

S = Settings()


def check(listing=None, search=None):
    return evaluate(listing or make_listing(), search or make_search(), S)


def test_matching_listing_is_accepted():
    assert check().accepted


def test_max_price_enforced_inclusive():
    assert check(make_listing(price_ils=1_750_000)).accepted
    r = check(make_listing(price_ils=1_750_001))
    assert not r.accepted and r.reason == "over_max_price"


def test_min_price():
    r = check(make_listing(price_ils=900_000), make_search(min_price_ils=1_000_000))
    assert r.reason == "under_min_price"


@pytest.mark.parametrize("price", [None, 0, -1])
def test_missing_or_invalid_price_rejected(price):
    assert check(make_listing(price_ils=price)).reason == "missing_price"


def test_missing_price_rejected_even_when_require_price_false_if_max_price_set():
    # No price cannot prove "under the maximum".
    assert check(make_listing(price_ils=None), make_search(require_price=False)).reason == "missing_price"


def test_missing_price_allowed_without_any_price_constraint():
    assert check(make_listing(price_ils=None), make_search(require_price=False, max_price_ils=None)).accepted


@pytest.mark.parametrize("url", [None, "", "not a url", "ftp://x/y.jpg", "javascript:alert(1)"])
def test_image_required(url):
    assert check(make_listing(image_url=url)).reason == "missing_image"


def test_image_optional_when_not_required():
    assert check(make_listing(image_url=None), make_search(require_image=False)).accepted


def test_city_filter_exact_after_normalization():
    assert check(make_listing(city="חולון ")).accepted
    assert check(make_listing(city="תל-אביב יפו"), make_search(city="תל אביב יפו")).accepted
    assert check(make_listing(city="בת ים")).reason == "city_mismatch"
    assert check(make_listing(city=None)).reason == "missing_city"


def test_property_type_filter():
    assert check(make_listing(property_type="house")).reason == "property_type_mismatch"
    assert check(make_listing(property_type=None)).reason == "missing_property_type"
    assert check(make_listing(property_type="house"), make_search(property_types=("apartment", "house"))).accepted


def test_rooms_and_size_bounds_and_missing_values():
    s = make_search(min_rooms=3, max_rooms=4, min_size_sqm=70, max_size_sqm=100)
    assert check(make_listing(rooms=3.5, size_sqm=80), s).accepted
    assert check(make_listing(rooms=2.5), s).reason == "under_min_rooms"
    assert check(make_listing(rooms=5), s).reason == "over_max_rooms"
    assert check(make_listing(rooms=None), s).reason == "missing_rooms"
    assert check(make_listing(size_sqm=60), s).reason == "under_min_size"
    assert check(make_listing(size_sqm=120), s).reason == "over_max_size"
    assert check(make_listing(size_sqm=None), s).reason == "missing_size"


# --- broker logic ---------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "למכירה דרך משרד תיווך מוביל", "המתווך יחזור אליכם", "מתווכת מטעם המשרד", 'סוכנות נדל"ן בע"מ', "עמלת תיווך 2%",
])
def test_explicit_broker_text_is_excluded(text):
    r = check(make_listing(description=text))
    assert not r.accepted and r.reason == "broker"


@pytest.mark.parametrize("text", ["דירה ללא תיווך", "בלי תיווך!", "מבלי תיווך", "אין צורך במתווך", "ישירות מהבעלים"])
def test_no_broker_statement_is_not_mistaken_for_broker(text):
    assert classify_seller(make_listing(description=text), S) == "private"
    assert check(make_listing(description=text)).seller_status == "private"


def test_neutral_fee_phrase_is_neither_broker_nor_proof_of_private():
    assert classify_seller(make_listing(description="ללא עמלת תיווך לקונה"), S) == "uncertain"


def test_broker_mentioned_alongside_no_broker_claim_is_still_excluded():
    assert check(make_listing(description="ללא תיווך. לפרטים משרד תיווך כהן")).reason == "broker"


def test_structured_field_beats_text():
    assert check(make_listing(seller_type="broker", description="ללא תיווך")).reason == "broker"
    assert check(make_listing(seller_type="private", description="משרד תיווך")).accepted


def test_uncertain_policies():
    plain = make_listing(description="דירה יפה")
    marked = check(plain, make_search(broker_uncertain="mark"))
    assert marked.accepted and marked.seller_status == "uncertain"
    included = check(plain, make_search(broker_uncertain="include"))
    assert included.accepted and included.seller_status is None
    assert check(plain, make_search(broker_uncertain="exclude")).reason == "broker_uncertain"


def test_broker_filter_can_be_disabled():
    assert check(make_listing(description="משרד תיווך"), make_search(exclude_brokers=False)).accepted
