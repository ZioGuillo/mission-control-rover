"""NASA data for the brain's tools: Astronomy Picture of the Day and Mars rover status.

The Mars Rover Photos API (api.nasa.gov/mars-photos, manifests) is gone (404 "No such app",
checked 2026-10-28), so rover status comes from the official raw-image feeds on mars.nasa.gov:
newest image -> latest sol, feed total -> photos taken. Those feeds need no API key.
"""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

APOD_BASE = "https://api.nasa.gov"
MARS_BASE = "https://mars.nasa.gov"
_TIMEOUT_S = 8.0
_RECENT = timedelta(days=30)
_MAX_EXPLANATION = 600


class NasaError(Exception):
    pass


def key(path: Path) -> str:
    """The api.nasa.gov key from `nasa_key_file`, or the rate-limited DEMO_KEY."""
    try:
        return Path(path).read_text().strip() or "DEMO_KEY"
    except OSError:
        return "DEMO_KEY"


def _get(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as resp:
            return json.load(resp)
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
        raise NasaError(f"{urllib.parse.urlsplit(url).netloc}: {e}") from e


def apod(key: str, base: str = APOD_BASE) -> dict:
    data = _get(f"{base}/planetary/apod?" + urllib.parse.urlencode({"api_key": key}))
    try:
        return {"title": data["title"], "date": data["date"], "media_type": data.get("media_type", ""),
                "explanation": str(data.get("explanation", ""))[:_MAX_EXPLANATION]}
    except (KeyError, TypeError) as e:
        raise NasaError(f"apod: unexpected reply {e}") from e


_FEEDS = {
    # rover -> (path + query, list key, total key, date key)
    "perseverance": ("/rss/api/?" + urllib.parse.urlencode(
        {"feed": "raw_images", "category": "mars2020", "feedtype": "json", "num": 1, "page": 0,
         "order": "sol desc"}), "images", "total_images", "date_taken_utc"),
    "curiosity": ("/api/v1/raw_image_items/?" + urllib.parse.urlencode(
        {"order": "sol desc", "per_page": 1, "page": 0, "condition_1": "msl:mission"}),
        "items", "total", "date_taken"),
}


def rover_status(rover: str, base: str = MARS_BASE) -> dict:
    """{"sol", "total_photos", "status"} for "perseverance" or "curiosity"."""
    try:
        query, items, total, date = _FEEDS[rover.strip().lower()]
    except KeyError:
        raise NasaError(f"unknown rover {rover!r} (perseverance or curiosity)") from None
    data = _get(base + query)
    try:
        newest = data[items][0]
        taken = datetime.fromisoformat(newest[date].removesuffix("Z")).replace(tzinfo=timezone.utc)
        recent = datetime.now(timezone.utc) - taken < _RECENT
        return {"sol": int(newest["sol"]), "total_photos": int(data[total]),
                "status": "activo" if recent else "sin fotos en los últimos 30 días"}
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise NasaError(f"{rover} feed: unexpected reply {e!r}") from e
