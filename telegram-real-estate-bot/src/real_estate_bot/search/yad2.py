"""Yad2 (יד2) provider.

!!! STATUS: UNVERIFIED AGAINST THE LIVE SITE !!!
This module was written without network access to Yad2. The endpoint, query parameters and JSON field
names below come from general knowledge of Yad2's public feed and MUST be checked before you rely on them:

    python -m real_estate_bot.main --probe

--probe performs ONE request and prints exactly what happened: robots.txt result, HTTP status, whether the
response looked like a bot-protection page, which JSON keys exist and how many items could be parsed.

Policy:
  * Identify honestly (configurable User-Agent), no browser spoofing, no proxies, no CAPTCHA solving.
  * HTTP 401/403, CAPTCHA/bot-protection pages or a robots.txt "Disallow" raise ProviderBlockedError and the
    run fails loudly. They are never retried or bypassed.
  * HTTP 429 / 5xx are retried with backoff (Retry-After honored), then raise.
  * Unknown response shape raises ProviderMalformedError (distinct from "zero listings").
Check Yad2's terms of use before running this regularly.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests

from ..config import SearchConfig
from ..filters import is_valid_image_url, normalize_city
from ..logging_config import log_fields
from ..models import Listing
from .base import (
    ListingProvider,
    ProviderBlockedError,
    ProviderError,
    ProviderMalformedError,
    ProviderRateLimitedError,
)

logger = logging.getLogger(__name__)

SOURCE = "yad2"
BASE_URL = "https://gw.yad2.co.il/feed-search-legacy/realestate/forsale"  # UNVERIFIED
ITEM_URL = "https://www.yad2.co.il/realestate/item/{token}"  # UNVERIFIED (redirects are fine)

# Israeli CBS city codes, which Yad2 uses for its `city` parameter (UNVERIFIED). Unknown city -> set
# `city_code` in config.yaml (the number appears in the URL when you filter by city on yad2.co.il).
CITY_CODES = {
    "חולון": 6600,
    "בת ים": 6200,
    "תל אביב יפו": 5000,
    "ראשון לציון": 8300,
    "רמת גן": 8600,
    "פתח תקווה": 7900,
    "נתניה": 7400,
    "אשדוד": 70,
    "חיפה": 4000,
    "ירושלים": 3000,
}
_CITY_CODES_NORMALIZED = {normalize_city(k): v for k, v in CITY_CODES.items()}

PROPERTY_GROUPS = {"apartment": "apartments", "garden_apartment": "apartments", "penthouse": "apartments",
                   "duplex": "apartments", "house": "houses"}

MIN_VALID_PRICE = 1_000  # anything below is a parse artifact ("1.5 מיליון" -> 1), not a price

_BLOCK_MARKERS = re.compile(r"captcha|shieldsquare|radware|are you human|access denied|bot detection|hcaptcha|px-captcha", re.I)

_ID_KEYS = ("id", "ad_number", "adNumber", "token", "link_token", "orderId")
_ITEM_PATHS = (("data", "feed", "feed_items"), ("feed", "feed_items"), ("data", "items"), ("items",), ("data", "feed", "items"))
_LASTPAGE_PATHS = (("data", "pagination", "last_page"), ("pagination", "last_page"), ("data", "feed", "total_pages"), ("data", "pagination", "totalPages"))
_TEXT_KEYS = ("title", "title_1", "title_2", "row_1", "row_2", "row_3", "row_4", "description", "info_text", "search_text", "ad_text")
_BROKER_FLAGS = ("merchant", "is_merchant", "is_agency", "isAgency", "is_broker", "isBroker")
_PRIVATE_FLAGS = ("is_private", "isPrivate", "private_seller")


# --- parsing helpers (pure, unit-tested) -------------------------------------------------------------


def parse_price(value: Any) -> int | None:
    """Normalize a price to an integer number of ILS, or None if absent/invalid."""
    if value is None or isinstance(value, bool):
        return None
    number: float | None = None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        match = re.search(r"\d[\d,.\s]*", value)
        if not match:
            return None
        digits = match.group(0).strip().replace(",", "").replace(" ", "")
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", digits):  # European style 1.690.000
            digits = digits.replace(".", "")
        try:
            number = float(digits)
        except ValueError:
            return None
    if number is None or not math.isfinite(number) or number < MIN_VALID_PRICE:
        return None
    return int(round(number))


def parse_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        match = re.search(r"\d+(?:[.,]\d+)?", value)
        if not match:
            return None
        number = float(match.group(0).replace(",", "."))
    else:
        return None
    return number if math.isfinite(number) and number > 0 else None


def parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_property_type(text: Any) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    value = text.strip()
    if "דירת גן" in value:
        return "garden_apartment"
    if "פנטהאוז" in value or "גג" in value:
        return "penthouse"
    if "דופלקס" in value:
        return "duplex"
    if "בית פרטי" in value or "קוטג" in value or "וילה" in value:
        return "house"
    if value == "דירה" or value.lower() in ("apartment", "apartments"):
        return "apartment"
    return f"other:{value}"


def extract_listing_id(item: Mapping[str, Any]) -> str | None:
    """The source's own listing id: a dedicated id field, else the token inside the listing URL."""
    for key in _ID_KEYS:
        value = item.get(key)
        if value is None or isinstance(value, (bool, dict, list)):
            continue
        text = str(value).strip()
        if text:
            return text
    for key in ("link", "url"):
        value = item.get(key)
        if isinstance(value, str):
            match = re.search(r"/item/(?:[^?#]*/)?([A-Za-z0-9]+)(?:[?#]|$)", value)
            if match:
                return match.group(1)
    return None


def _first_image(item: Mapping[str, Any]) -> str | None:
    candidates: list[Any] = []
    for key in ("images_urls", "images", "img_url", "cover_image", "image", "coverImage"):
        candidates.append(item.get(key))
    meta = item.get("metaData")
    if isinstance(meta, Mapping):
        candidates += [meta.get("coverImage"), meta.get("images")]
    for candidate in candidates:
        for entry in candidate if isinstance(candidate, list) else [candidate]:
            if isinstance(entry, Mapping):
                entry = entry.get("src") or entry.get("url")
            if isinstance(entry, str) and is_valid_image_url(entry.strip()):
                return entry.strip()
    return None


def _first_str(item: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Mapping):  # e.g. {"text": "חולון"}
            inner = value.get("text") or value.get("name")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return None


def _truthy(value: Any) -> bool:
    return value is True or (isinstance(value, (int, str)) and str(value).strip().lower() in ("1", "true", "yes"))


def parse_item(item: Any) -> Listing | None:
    """Normalize one raw feed item. Returns None for things that are not real listings (ads, banners, junk)."""
    if not isinstance(item, Mapping):
        return None
    source_id = extract_listing_id(item)
    if not source_id:
        return None

    token = _first_str(item, ("link_token", "token")) or source_id
    link = _first_str(item, ("link", "url"))
    if link:
        url = urljoin("https://www.yad2.co.il", link)
        if urlparse(url).scheme not in ("http", "https"):
            return None
    else:
        url = ITEM_URL.format(token=token)

    description = " ".join(
        str(item[k]).strip() for k in _TEXT_KEYS if isinstance(item.get(k), str) and item[k].strip()
    )
    city = _first_str(item, ("city", "city_text", "City"))
    prop = normalize_property_type(_first_str(item, ("HomeTypeID_text", "property_text", "home_type", "propertyType", "AdType_text")))
    title = _first_str(item, ("title", "title_1")) or (
        f"{prop or 'Listing'} - {city}" if city and prop and not prop.startswith("other:") else None
    )

    seller_type = None
    if any(_truthy(item.get(k)) for k in _BROKER_FLAGS):
        seller_type = "broker"
    elif any(_truthy(item.get(k)) for k in _PRIVATE_FLAGS):
        seller_type = "private"

    published = None
    for key in ("date_added", "created_at", "createdAt", "publishedAt", "published_at"):
        published = parse_datetime(item.get(key))
        if published:
            break

    return Listing(
        source=SOURCE,
        source_id=source_id,
        url=url,
        title=title,
        price_ils=parse_price(item.get("price")),
        city=city,
        neighborhood=_first_str(item, ("neighborhood", "neighborhood_text", "Neighborhood")),
        street=_first_str(item, ("street", "street_text", "Street")),
        property_type=prop,
        rooms=parse_float(item.get("Rooms_text", item.get("rooms", item.get("Rooms")))),
        size_sqm=parse_float(item.get("square_meters", item.get("SquareMeter", item.get("size")))),
        image_url=_first_image(item),
        published_at=published,
        description=description,
        seller_type=seller_type,
    )


def _dig(payload: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(payload, Mapping) or key not in payload:
            return None
        payload = payload[key]
    return payload


def extract_items(payload: Any) -> list[Any]:
    """Locate the item list. Raises ProviderMalformedError if no known location exists (never returns [] for that)."""
    for path in _ITEM_PATHS:
        found = _dig(payload, path)
        if isinstance(found, list):
            return found
    keys = sorted(payload.keys()) if isinstance(payload, Mapping) else type(payload).__name__
    raise ProviderMalformedError(f"unrecognized Yad2 response structure (top-level: {keys}); run --probe and update the parser")


def extract_last_page(payload: Any) -> int | None:
    for path in _LASTPAGE_PATHS:
        value = _dig(payload, path)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


# --- provider --------------------------------------------------------------------------------------


class Yad2Provider(ListingProvider):
    name = "yad2"

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        user_agent: str = "real-estate-alert-bot/0.1 (personal, low-volume)",
        request_delay: float = 3.0,
        timeout: float = 20,
        max_retries: int = 3,
        respect_robots: bool = True,
        base_url: str = BASE_URL,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.session = session or requests.Session()
        self.user_agent = user_agent
        self.request_delay = request_delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.respect_robots = respect_robots
        self.base_url = base_url
        self._sleep = sleep
        self._robots_ok = False
        self.skipped_items = 0

    # request construction
    def city_code(self, search: SearchConfig) -> int:
        code = search.city_code or _CITY_CODES_NORMALIZED.get(normalize_city(search.city))
        if not code:
            raise ProviderError(f"no Yad2 city code known for '{search.city}'; add `city_code:` to this search in config.yaml")
        return code

    def build_params(self, search: SearchConfig, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {"city": self.city_code(search), "page": page}
        group = PROPERTY_GROUPS.get(search.property_type)
        if group:
            params["propertyGroup"] = group
        if search.max_price_ils is not None:
            params["price"] = f"{search.min_price_ils or 0}-{search.max_price_ils}"
        params.update(search.extra_params)
        return params

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept": "application/json"}

    # robots.txt
    def check_robots(self) -> str:
        """Returns a human-readable result; raises ProviderBlockedError if disallowed."""
        if not self.respect_robots:
            return "skipped (respect_robots_txt: false)"
        parts = urlparse(self.base_url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            response = self.session.get(robots_url, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            raise ProviderError(f"could not fetch robots.txt ({type(exc).__name__})") from exc
        if response.status_code >= 500:
            raise ProviderError(f"robots.txt unavailable (HTTP {response.status_code}); not scraping without it")
        if 400 <= response.status_code < 500:
            return f"no robots.txt (HTTP {response.status_code}) -> allowed"
        parser = robotparser.RobotFileParser()
        parser.parse(response.text.splitlines())
        if not parser.can_fetch(self.user_agent, self.base_url):
            raise ProviderBlockedError(f"robots.txt on {parts.netloc} disallows this request; not fetching")
        return "allowed by robots.txt"

    def _ensure_robots(self) -> None:
        if not self._robots_ok:
            self.check_robots()
            self._robots_ok = True

    # HTTP
    def fetch_page(self, params: Mapping[str, Any]) -> Any:
        last: ProviderError | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(self.base_url, params=params, headers=self._headers(), timeout=self.timeout)
            except requests.RequestException as exc:
                last = ProviderError(f"network error: {type(exc).__name__}")
                self._sleep(min(2**attempt, 30))
                continue

            status = response.status_code
            if status in (401, 403):
                raise ProviderBlockedError(f"Yad2 refused automated access (HTTP {status}). Not bypassing; see README 'Known limitations'.")
            if status == 429:
                wait = _retry_after(response)
                last = ProviderRateLimitedError("Yad2 answered HTTP 429 (rate limited)")
                logger.warning("yad2 rate limited", extra=log_fields(attempt=attempt, wait=wait))
                if attempt < self.max_retries:
                    self._sleep(wait)
                continue
            if status >= 500:
                last = ProviderError(f"Yad2 server error HTTP {status}")
                if attempt < self.max_retries:
                    self._sleep(min(2**attempt, 30))
                continue
            if status != 200:
                raise ProviderError(f"unexpected HTTP {status} from Yad2")

            content_type = response.headers.get("Content-Type", "")
            if "json" not in content_type.lower():
                if _BLOCK_MARKERS.search(response.text[:5000]):
                    raise ProviderBlockedError("Yad2 returned a bot-protection / CAPTCHA page. Not bypassing.")
                raise ProviderMalformedError(f"expected JSON but got Content-Type '{content_type}'")
            try:
                return response.json()
            except ValueError as exc:
                raise ProviderMalformedError("response is not valid JSON") from exc
        assert last is not None
        raise last

    # search
    def search(self, search: SearchConfig) -> list[Listing]:
        self._ensure_robots()
        listings: list[Listing] = []
        seen: set[str] = set()
        for page in range(1, search.max_pages + 1):
            if page > 1:
                self._sleep(self.request_delay)
            payload = self.fetch_page(self.build_params(search, page))
            raw_items = extract_items(payload)
            if not raw_items:
                break
            for raw in raw_items:
                listing = parse_item(raw)
                if listing is None:
                    self.skipped_items += 1
                    continue
                if listing.key not in seen:
                    seen.add(listing.key)
                    listings.append(listing)
            last_page = extract_last_page(payload)
            if last_page is not None and page >= last_page:
                break
        listings.sort(key=lambda l: l.sort_key(), reverse=True)  # stable: undated items keep source order
        return listings

    # diagnostics
    def probe(self, search: SearchConfig) -> dict[str, Any]:
        """One request, full diagnostics. Used by `--probe` to verify the integration for real."""
        report: dict[str, Any] = {"endpoint": self.base_url, "user_agent": self.user_agent}
        try:
            report["robots"] = self.check_robots()
            params = self.build_params(search, 1)
            report["params"] = params
            payload = self.fetch_page(params)
            report["top_level_keys"] = sorted(payload.keys()) if isinstance(payload, Mapping) else type(payload).__name__
            items = extract_items(payload)
            parsed = [l for l in (parse_item(i) for i in items) if l]
            report["raw_items"] = len(items)
            report["parsed_items"] = len(parsed)
            report["fields_present"] = {
                name: sum(1 for l in parsed if getattr(l, name) not in (None, ""))
                for name in ("price_ils", "city", "neighborhood", "property_type", "rooms", "size_sqm", "image_url", "published_at", "seller_type")
            }
            if parsed:
                report["sample"] = parsed[0].to_dict()
            report["verdict"] = "OK - response parsed" if parsed else "Response had no parsable listings - check field names"
        except ProviderError as exc:
            report["verdict"] = f"FAILED: {type(exc).__name__}: {exc}"
        return report


def _retry_after(response: requests.Response) -> float:
    try:
        return min(max(float(response.headers.get("Retry-After", "")), 1.0), 120.0)
    except ValueError:
        return 10.0
