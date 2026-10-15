"""Domain model shared by providers, filters, storage and the notifier."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Listing:
    """A normalized listing. Missing information is None - never a guessed value."""

    source: str
    source_id: str
    url: str
    title: str | None = None
    price_ils: int | None = None
    city: str | None = None
    neighborhood: str | None = None
    street: str | None = None
    property_type: str | None = None  # normalized: "apartment", "house", ...
    rooms: float | None = None
    size_sqm: float | None = None
    image_url: str | None = None
    published_at: datetime | None = None
    description: str = ""
    seller_type: str | None = None  # "private" | "broker" | None (unknown), from structured data
    seller_status: str | None = None  # set by filters: "private" | "uncertain" | None

    @property
    def key(self) -> str:
        """Stable identity used for deduplication (source + the source's own listing id)."""
        return f"{self.source}:{self.source_id}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["published_at"] = self.published_at.isoformat() if self.published_at else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Listing":
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        published = values.get("published_at")
        if isinstance(published, str):
            values["published_at"] = datetime.fromisoformat(published)
        return cls(**values)

    def sort_key(self) -> datetime:
        """Newest first sorting helper; naive/aware datetimes are compared as naive."""
        if self.published_at is None:
            return datetime.min
        return self.published_at.replace(tzinfo=None)
