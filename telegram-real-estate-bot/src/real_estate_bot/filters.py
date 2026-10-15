"""Pure filtering logic: does a normalized listing satisfy a search?

Rule of thumb: missing information is never treated as proof that a requirement is met.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .config import SearchConfig, Settings
from .models import Listing

_QUOTES = str.maketrans({"״": '"', "”": '"', "“": '"', "׳": "'", "’": "'", "‘": "'"})


def normalize_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").translate(_QUOTES)).strip().lower()


def normalize_city(text: str | None) -> str:
    value = re.sub(r"[\-–—־]", " ", normalize_text(text))
    value = value.replace('"', "").replace("'", "")
    return re.sub(r"\s+", " ", value).strip()


def is_valid_image_url(url: str | None) -> bool:
    if not url or not isinstance(url, str):
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def classify_seller(listing: Listing, settings: Settings) -> str:
    """Return "broker", "private" or "uncertain".

    1. A structured field from the source wins.
    2. Otherwise look at the text. "No broker" style phrases are removed first, so they cannot be
       mistaken for a broker mention; if they were present they count as a private-seller claim.
    3. Any remaining broker term means the listing is brokered.
    4. No evidence either way is "uncertain" - keyword filtering can never *prove* a private seller.
    """
    if listing.seller_type in ("broker", "private"):
        return listing.seller_type

    text = normalize_text(f"{listing.title or ''} {listing.description or ''}")
    phrases = [(normalize_text(p), True) for p in settings.private_claim_phrases]
    phrases += [(normalize_text(p), False) for p in settings.neutral_negations]
    private_claim = False
    for phrase, is_claim in sorted(phrases, key=lambda item: -len(item[0])):
        if phrase and phrase in text:
            private_claim = private_claim or is_claim
            text = text.replace(phrase, " ")

    if any(term and normalize_text(term) in text for term in settings.broker_terms):
        return "broker"
    return "private" if private_claim else "uncertain"


@dataclass(frozen=True)
class FilterResult:
    accepted: bool
    reason: str | None = None  # machine-readable rejection reason
    seller_status: str | None = None  # "private" | "uncertain" | None (broker filter off)


def _reject(reason: str) -> FilterResult:
    return FilterResult(False, reason)


def evaluate(listing: Listing, search: SearchConfig, settings: Settings) -> FilterResult:
    # Price
    if listing.price_ils is None or listing.price_ils <= 0:
        if search.require_price or search.max_price_ils is not None or search.min_price_ils is not None:
            return _reject("missing_price")
    else:
        if search.max_price_ils is not None and listing.price_ils > search.max_price_ils:
            return _reject("over_max_price")
        if search.min_price_ils is not None and listing.price_ils < search.min_price_ils:
            return _reject("under_min_price")

    # Image
    if search.require_image and not is_valid_image_url(listing.image_url):
        return _reject("missing_image")

    # City
    if not listing.city:
        return _reject("missing_city")
    if normalize_city(listing.city) != normalize_city(search.city):
        return _reject("city_mismatch")

    # Property type
    if not listing.property_type:
        return _reject("missing_property_type")
    if listing.property_type not in search.property_types:
        return _reject("property_type_mismatch")

    # Rooms / size
    for value, lo, hi, name in (
        (listing.rooms, search.min_rooms, search.max_rooms, "rooms"),
        (listing.size_sqm, search.min_size_sqm, search.max_size_sqm, "size"),
    ):
        if lo is None and hi is None:
            continue
        if value is None:
            return _reject(f"missing_{name}")
        if lo is not None and value < lo:
            return _reject(f"under_min_{name}")
        if hi is not None and value > hi:
            return _reject(f"over_max_{name}")

    # Broker
    seller_status = None
    if search.exclude_brokers:
        seller = classify_seller(listing, settings)
        if seller == "broker":
            return _reject("broker")
        if seller == "uncertain":
            if search.broker_uncertain == "exclude":
                return _reject("broker_uncertain")
            # "include": accept silently. "mark": accept but flag the notification as unverified.
            seller_status = "uncertain" if search.broker_uncertain == "mark" else None
        else:
            seller_status = "private"

    return FilterResult(True, None, seller_status)
