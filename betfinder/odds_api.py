"""Thin client for The Odds API v4 (https://the-odds-api.com). Standard library only."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.the-odds-api.com/v4"


class OddsApiError(RuntimeError):
    pass


class OddsApiClient:
    def __init__(self, api_key: str | None = None, timeout: float = 20.0):
        self.api_key = api_key or os.environ.get("ODDS_API_KEY")
        if not self.api_key:
            raise OddsApiError(
                "No API key. Get a free key at https://the-odds-api.com and run:\n"
                "  export ODDS_API_KEY=your_key_here"
            )
        self.timeout = timeout
        self.requests_remaining: str | None = None
        self.requests_used: str | None = None

    def _get(self, path: str, params: dict[str, str]) -> object:
        query = urllib.parse.urlencode({"apiKey": self.api_key, **params})
        url = f"{BASE_URL}{path}?{query}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                self.requests_remaining = resp.headers.get("x-requests-remaining")
                self.requests_used = resp.headers.get("x-requests-used")
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise OddsApiError(f"HTTP {e.code} for {path}: {body}") from e
        except urllib.error.URLError as e:
            raise OddsApiError(f"Network error for {path}: {e.reason}") from e

    def sports(self, include_all: bool = False) -> list[dict]:
        """List sports. Free: does not count against the quota."""
        params = {"all": "true"} if include_all else {}
        return self._get("/sports", params)  # type: ignore[return-value]

    def odds(
        self,
        sport: str,
        regions: str = "us,eu",
        markets: str = "h2h",
        odds_format: str = "decimal",
    ) -> list[dict]:
        """Upcoming and live odds for a sport.

        Quota cost = number of markets x number of regions.
        """
        params = {
            "regions": regions,
            "markets": markets,
            "oddsFormat": odds_format,
            "dateFormat": "iso",
        }
        return self._get(f"/sports/{sport}/odds", params)  # type: ignore[return-value]

    def scores(self, sport: str, days_from: int = 3) -> list[dict]:
        """Recent and live scores. Costs 2 credits with days_from (1-3 days back)."""
        return self._get(f"/sports/{sport}/scores", {"daysFrom": str(days_from), "dateFormat": "iso"})  # type: ignore[return-value]

    def events(self, sport: str) -> list[dict]:
        """Upcoming events for a sport (id, teams, start time). Free: no credits used."""
        return self._get(f"/sports/{sport}/events", {"dateFormat": "iso"})  # type: ignore[return-value]

    def event_odds(
        self, sport: str, event_id: str, markets: str, regions: str = "us,us_ex,eu",
        odds_format: str = "decimal",
    ) -> dict:
        """All markets for one event, including props and alternate lines.

        Quota cost = number of markets returned x number of regions.
        """
        params = {"regions": regions, "markets": markets, "oddsFormat": odds_format, "dateFormat": "iso"}
        return self._get(f"/sports/{sport}/events/{event_id}/odds", params)  # type: ignore[return-value]
