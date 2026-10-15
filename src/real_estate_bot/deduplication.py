"""In-run deduplication. Cross-run deduplication is handled by storage (listing key is the primary key)."""

from __future__ import annotations

from collections.abc import Iterable

from .models import Listing



def dedupe(listings: Iterable[Listing]) -> list[Listing]:
    """Keep the first occurrence of every listing key, preserving order.

    A listing returned by several searches (or twice by overlapping result pages) is one listing.
    The key is source + the source's own id, so a changed URL does not create a duplicate.
    """
    seen: set[str] = set()
    unique: list[Listing] = []
    for listing in listings:
        if listing.key in seen:
            continue
        seen.add(listing.key)
        unique.append(listing)
    return unique
