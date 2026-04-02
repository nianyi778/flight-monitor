"""
Flight data source Protocol — formal interface for all API modules.

All API modules (kiwi_api, google_flights_api, spring_api, etc.) follow this
convention-based interface. This Protocol documents the expected contract.
"""

from __future__ import annotations

from typing import Protocol, TypedDict, runtime_checkable


class FlightRecord(TypedDict, total=False):
    airline: str
    flight_no: str
    departure_time: str
    arrival_time: str
    price_cny: int
    original_price: float | None
    original_currency: str
    origin: str
    destination: str
    stops: int
    via: str


class SearchResult(TypedDict, total=False):
    flights: list[FlightRecord]
    lowest_price: int | None
    error: str | None
    source: str
    url: str
    flight_date: str
    status: str
    block_reason: str | None
    retryable: bool
    request_mode: str
    from_cache: bool
    proxy_id: str | None
    no_cache: bool


@runtime_checkable
class FlightSource(Protocol):
    def __call__(
        self,
        searches: list[dict],
        proxy_url: str | None = None,
        proxy_id: str | None = None,
    ) -> dict[str, SearchResult]:
        """
        Query flights for a batch of searches.

        Args:
            searches: list of search dicts, each containing:
                url, origin, destination, flight_date, source_type, max_stops
            proxy_url: optional proxy URL
            proxy_id: optional proxy identifier for tracking

        Returns:
            {url: SearchResult} mapping
        """
        ...
