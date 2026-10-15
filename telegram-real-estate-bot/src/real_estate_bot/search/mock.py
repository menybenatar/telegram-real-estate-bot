"""File-backed provider for dry-runs and demos. Reads already-normalized listings from a JSON file."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import SearchConfig
from ..models import Listing
from .base import ListingProvider, ProviderError


class MockProvider(ListingProvider):
    name = "mock"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def search(self, search: SearchConfig) -> list[Listing]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProviderError(f"cannot read mock listings from {self.path}: {exc}") from exc
        return [Listing.from_dict(item) for item in raw]
