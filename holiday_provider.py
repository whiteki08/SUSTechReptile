from __future__ import annotations

from datetime import date, datetime
from threading import Lock

import requests

DEFAULT_API_TEMPLATE = "https://date.nager.at/api/v3/PublicHolidays/{year}/CN"

# Keep a tiny static fallback for well-known national holidays.
STATIC_CN_HOLIDAYS: dict[int, dict[str, str]] = {
    2025: {
        "2025-01-01": "New Year",
        "2025-05-01": "Labour Day",
        "2025-10-01": "National Day",
    },
    2026: {
        "2026-01-01": "New Year",
        "2026-05-01": "Labour Day",
        "2026-10-01": "National Day",
    },
    2027: {
        "2027-01-01": "New Year",
        "2027-05-01": "Labour Day",
        "2027-10-01": "National Day",
    },
}


def _normalize_day(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


class HolidayProvider:
    """Holiday lookup backed by Nager public holiday API with in-memory cache."""

    def __init__(
        self,
        *,
        api_template: str = DEFAULT_API_TEMPLATE,
        timeout_seconds: float = 8.0,
        static_holidays: dict[int, dict[str, str]] | None = None,
    ) -> None:
        self.api_template = api_template
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.static_holidays = static_holidays or STATIC_CN_HOLIDAYS

        self._year_cache: dict[int, dict[str, str]] = {}
        self._lock = Lock()

    def holiday_name(self, value: date | datetime | str) -> str | None:
        day = _normalize_day(value)
        year_holidays = self._load_year(day.year)
        return year_holidays.get(day.isoformat())

    def is_holiday(self, value: date | datetime | str) -> bool:
        return self.holiday_name(value) is not None

    def _load_year(self, year: int) -> dict[str, str]:
        with self._lock:
            cached = self._year_cache.get(year)
            if cached is not None:
                return cached

        api_mapping = self._fetch_year_from_api(year) or {}
        merged = dict(api_mapping)

        static_mapping = self.static_holidays.get(year, {})
        if static_mapping:
            merged.update(static_mapping)

        with self._lock:
            self._year_cache[year] = merged
            return self._year_cache[year]

    def _fetch_year_from_api(self, year: int) -> dict[str, str] | None:
        url = self.api_template.format(year=year)
        try:
            response = requests.get(url, timeout=self.timeout_seconds)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            return None

        if not isinstance(payload, list):
            return None

        holidays: dict[str, str] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            day = item.get("date")
            if not isinstance(day, str):
                continue
            holiday_name = item.get("localName") or item.get("name") or "Holiday"
            holidays[day] = str(holiday_name)

        return holidays or None
