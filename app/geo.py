import difflib
import json
import logging
import math
import re

import httpx

from .config import DATA_DIR

log = logging.getLogger(__name__)

WARDS: list[dict] = json.loads((DATA_DIR / "wards.json").read_text())
WARD_BY_NAME = {w["ward"].lower(): w for w in WARDS}

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
# rough box around Ahmedabad, biases Geocoding results to the city
AHMEDABAD_BOUNDS = "22.90,72.45|23.15,72.72"


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_ward(lat: float, lng: float, max_km: float) -> dict | None:
    best = min(WARDS, key=lambda w: haversine_km(lat, lng, w["lat"], w["lng"]))
    dist = haversine_km(lat, lng, best["lat"], best["lng"])
    if dist > max_km:
        return None
    return {"ward": best["ward"], "zone": best["zone"], "distance_km": round(dist, 2)}


def _normalise(text: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    text = re.sub(r"\b(near|opp|opposite|behind|ahmedabad|amdavad|area|road side)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def match_area(text: str) -> dict | None:
    q = _normalise(text)
    if not q:
        return None
    names = {}
    for w in WARDS:
        names[w["ward"].lower()] = w
        for alias in w["aliases"]:
            names[alias] = w
    # exact name or alias contained in what the user typed
    for key in sorted(names, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", q):
            w = names[key]
            return {"ward": w["ward"], "zone": w["zone"], "matched": key}
    close = difflib.get_close_matches(q, names.keys(), n=1, cutoff=0.75)
    if close:
        w = names[close[0]]
        return {"ward": w["ward"], "zone": w["zone"], "matched": close[0]}
    return None


def suggestions(text: str, n: int = 3) -> list[str]:
    q = _normalise(text)
    return [w.title() for w in difflib.get_close_matches(q, WARD_BY_NAME.keys(), n=n, cutoff=0.4)]


def geocode(text: str, api_key: str, timeout: float = 5.0) -> tuple[float, float] | None:
    if not api_key:
        return None
    params = {
        "address": f"{text}, Ahmedabad, Gujarat",
        "components": "country:IN",
        "bounds": AHMEDABAD_BOUNDS,
        "key": api_key,
    }
    try:
        resp = httpx.get(GEOCODE_URL, params=params, timeout=timeout)
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        log.warning("geocoding request failed")
        return None
    if data.get("status") != "OK" or not data.get("results"):
        return None
    loc = data["results"][0]["geometry"]["location"]
    return loc["lat"], loc["lng"]
