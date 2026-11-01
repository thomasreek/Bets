"""Client for Betting Data API (https://bettingdataapi.com). Standard library only.

Responses are converted to The Odds API v4 event shape so the scanner can use either source.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .odds_api import OddsApiError

BASE_URL = "https://api.bettingdataapi.com"


class BettingDataClient:
    def __init__(self, api_key: str | None = None, timeout: float = 20.0):
        self.api_key = api_key or os.environ.get("BETTINGDATA_API_KEY")
        if not self.api_key:
            raise OddsApiError(
                "No API key. Get one at https://bettingdataapi.com and run:\n"
                "  export BETTINGDATA_API_KEY=your_key_here"
            )
        self.timeout = timeout
        self.requests_remaining: str | None = None

    def _get(self, path: str, params: dict[str, str] | None = None) -> object:
        query = urllib.parse.urlencode(params or {})
        url = f"{BASE_URL}{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(url, headers={"x-api-key": self.api_key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            raise OddsApiError(f"HTTP {e.code} for {path}: {e.read().decode('utf-8', 'replace')}") from e
        except urllib.error.URLError as e:
            raise OddsApiError(f"Network error for {path}: {e.reason}") from e
        if not body.get("success"):
            raise OddsApiError(body.get("message") or f"request to {path} failed")
        return body["data"]

    def account(self) -> dict:
        data = self._get("/me/")
        self.requests_remaining = str(data.get("remaining"))
        return data  # type: ignore[return-value]

    def sports(self) -> list[dict]:
        """Sports this key can access, in the {key, group, title, active} shape the CLI expects."""
        return [
            {"key": s["key"], "group": s.get("sport", ""), "title": s.get("league", s["key"]),
             "active": True, "has_outrights": False}
            for s in self._get("/sports/")  # type: ignore[union-attr]
        ]

    def odds(self, sport: str, regions: str = "", markets: str = "h2h", odds_format: str = "decimal") -> list[dict]:
        params = {"sport_key": sport, "oddsFormat": "decimal"}
        if markets:
            params["markets"] = markets
        if regions:
            params["regions"] = regions
        try:
            data = self._get("/odds/", params)
        except OddsApiError as e:
            if "Cache is empty" in str(e):
                return []  # out of season
            raise
        return [to_odds_api_event(e, sport) for e in data]  # type: ignore[union-attr]


def to_odds_api_event(event: dict, sport_key: str) -> dict:
    """Convert one Betting Data API event to The Odds API v4 event shape."""
    books: dict[str, dict] = {}
    for entry in event.get("books", []):
        book = books.setdefault(
            entry["book"], {"key": entry["book"], "title": entry["book"], "markets": [], "last_update": ""}
        )
        updated = entry.get("updated_at", "")
        book["markets"].append({"key": entry["market"], "last_update": updated, "outcomes": entry["outcomes"]})
        if updated > book["last_update"]:
            book["last_update"] = updated
    return {
        "id": event.get("event_id"),
        "sport_key": sport_key,
        "sport_title": event.get("league") or sport_key,
        "commence_time": event.get("start_time", ""),
        "home_team": event.get("home_team", ""),
        "away_team": event.get("away_team", ""),
        "bookmakers": list(books.values()),
    }
