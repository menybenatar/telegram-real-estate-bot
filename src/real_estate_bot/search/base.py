"""Provider interface: anything that can return normalized listings for a search.

To plug in an authorized API or another permitted data source, implement ListingProvider.search()
and register it in main.build_provider(). Providers must raise a ProviderError subclass when a search
*fails* - returning an empty list means "the search worked and found nothing".
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import SearchConfig
from ..models import Listing


class ProviderError(Exception):
    """The search failed (as opposed to finding zero listings)."""


class ProviderBlockedError(ProviderError):
    """The source refuses automated access (HTTP 401/403, CAPTCHA, robots.txt). Never worked around."""


class ProviderRateLimitedError(ProviderError):
    """The source kept answering HTTP 429 after the allowed retries."""


class ProviderMalformedError(ProviderError):
    """The response did not have the expected shape (the source probably changed)."""


class ListingProvider(ABC):
    name: str

    @abstractmethod
    def search(self, search: SearchConfig) -> list[Listing]:
        """Return normalized listings, newest first when possible. Filtering by criteria happens later."""
