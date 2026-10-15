"""Telegram delivery via the Bot API (plain HTTPS, no extra dependency)."""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass, replace
from typing import Callable

import requests

from .filters import is_valid_image_url
from .logging_config import log_fields, redact, register_secret
from .models import Listing

logger = logging.getLogger(__name__)

CAPTION_LIMIT = 1024  # Telegram's limit for photo captions

LABELS = {
    "en": {
        "header_apartment": "🏠 New apartment for sale",
        "header_other": "🏠 New property for sale",
        "price": "Price",
        "rooms": "Rooms",
        "size": "Size",
        "published": "Published",
        "link": "View listing",
        "uncertain": "⚠️ Could not verify that the seller is private (no broker)",
        "error_header": "⚠️ Real-estate bot problem",
    },
    "he": {
        "header_apartment": "🏠 דירה חדשה למכירה",
        "header_other": "🏠 נכס חדש למכירה",
        "price": "מחיר",
        "rooms": "חדרים",
        "size": "שטח",
        "published": "פורסם",
        "link": "לצפייה במודעה",
        "uncertain": "⚠️ לא ניתן לוודא שהמוכר פרטי (ללא תיווך)",
        "error_header": "⚠️ תקלה בבוט הנדל\"ן",
    },
}


class TelegramError(Exception):
    def __init__(self, message: str, *, status: int | None = None, fatal: bool = False):
        super().__init__(message)
        self.status = status
        self.fatal = fatal  # fatal = retrying other listings is pointless (bad token, chat not found, ...)


@dataclass(frozen=True)
class SendResult:
    used_photo: bool
    photo_error: str | None = None  # set when the image failed and the text fallback was used


def build_message(listing: Listing, language: str = "en") -> str:
    """HTML caption/message. Only fields that really exist are included."""
    t = LABELS.get(language, LABELS["en"])
    esc = html.escape
    lines = [t["header_apartment"] if listing.property_type == "apartment" else t["header_other"]]
    if listing.title:
        lines.append(f"<b>{esc(listing.title.strip())}</b>")
    lines.append("")

    place = " — ".join(p for p in (listing.city, listing.neighborhood) if p)
    if place:
        lines.append(f"📍 {esc(place)}")
    if listing.price_ils:
        lines.append(f"💰 {t['price']}: ₪{listing.price_ils:,}")
    if listing.rooms is not None:
        lines.append(f"🛏️ {t['rooms']}: {listing.rooms:g}")
    if listing.size_sqm is not None:
        lines.append(f"📐 {t['size']}: {listing.size_sqm:g} m²")
    if listing.published_at:
        lines.append(f"🆕 {t['published']}: {listing.published_at.strftime('%d/%m/%Y')}")
    if listing.seller_status == "uncertain":
        lines.append("")
        lines.append(t["uncertain"])
    lines.append("")
    link = f"🔗 {t['link']}: {esc(listing.url)}"
    lines.append(link)

    text = "\n".join(lines)
    if len(text) > CAPTION_LIMIT and listing.title:  # shrink the only free-text field
        overflow = len(text) - CAPTION_LIMIT + 1
        short = listing.title.strip()[: max(10, len(listing.title.strip()) - overflow)] + "…"
        return build_message(replace(listing, title=short), language)
    return text[:CAPTION_LIMIT]


class TelegramNotifier:
    API = "https://api.telegram.org"

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        session: requests.Session | None = None,
        language: str = "en",
        send_delay: float = 1.5,
        max_retries: int = 3,
        timeout: float = 20,
        sleep: Callable[[float], None] = time.sleep,
    ):
        register_secret(token)
        self._token = token
        self.chat_id = chat_id
        self.session = session or requests.Session()
        self.language = language
        self.send_delay = send_delay
        self.max_retries = max_retries
        self.timeout = timeout
        self._sleep = sleep

    # -- low level ------------------------------------------------------------------------------

    def _call(self, method: str, payload: dict) -> dict:
        url = f"{self.API}/bot{self._token}/{method}"
        last: TelegramError | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                # Never include str(exc): requests embeds the full URL (with the token).
                last = TelegramError(f"network error: {type(exc).__name__}")
                self._sleep(min(2**attempt, 30))
                continue

            try:
                data = response.json()
            except ValueError:
                data = {}
            if response.status_code == 200 and data.get("ok"):
                return data

            description = redact(str(data.get("description", "")))
            status = response.status_code
            if status == 429:
                retry_after = float((data.get("parameters") or {}).get("retry_after", 5))
                last = TelegramError(f"rate limited by Telegram (retry_after={retry_after:g}s)", status=status)
                if retry_after > 120:
                    raise last
                logger.warning("telegram rate limited, waiting", extra=log_fields(retry_after=retry_after, attempt=attempt))
                self._sleep(retry_after + 1)
                continue
            if status >= 500:
                last = TelegramError(f"telegram server error {status}", status=status)
                self._sleep(min(2**attempt, 30))
                continue
            fatal = status in (401, 403, 404) or "chat not found" in description.lower()
            raise TelegramError(f"telegram rejected {method}: HTTP {status} {description}".strip(), status=status, fatal=fatal)

        assert last is not None
        raise last

    # -- public API -----------------------------------------------------------------------------

    def send_listing(self, listing: Listing) -> SendResult:
        text = build_message(listing, self.language)
        photo_error: str | None = None
        if is_valid_image_url(listing.image_url):
            try:
                self._call("sendPhoto", {"chat_id": self.chat_id, "photo": listing.image_url, "caption": text, "parse_mode": "HTML"})
                self._sleep(self.send_delay)
                return SendResult(used_photo=True)
            except TelegramError as exc:
                if exc.fatal:
                    raise
                photo_error = f"photo failed ({exc}); sent text only"
                logger.warning("sending photo failed, falling back to text", extra=log_fields(listing=listing.key, error=str(exc)))
        self._call("sendMessage", {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"})
        self._sleep(self.send_delay)
        return SendResult(used_photo=False, photo_error=photo_error)

    def send_error(self, message: str) -> bool:
        """Best-effort alert about a failed run. Never raises, never contains credentials."""
        t = LABELS.get(self.language, LABELS["en"])
        text = f"{t['error_header']}\n{html.escape(redact(message))}"[:3500]
        try:
            self._call("sendMessage", {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"})
            return True
        except TelegramError as exc:
            logger.error("could not send error notification", extra=log_fields(error=str(exc)))
            return False


class DryRunNotifier:
    """Prints what would be sent. Used by --dry-run; sends nothing to Telegram."""

    def __init__(self, language: str = "en", out=print):
        self.language = language
        self._out = out
        self.sent: list[Listing] = []

    def send_listing(self, listing: Listing) -> SendResult:
        self._out("----- [dry-run] would send -----")
        self._out(f"(photo: {listing.image_url})" if listing.image_url else "(no photo)")
        self._out(build_message(listing, self.language))
        self.sent.append(listing)
        return SendResult(used_photo=bool(listing.image_url))

    def send_error(self, message: str) -> bool:
        self._out(f"----- [dry-run] would send error alert: {message}")
        return True
