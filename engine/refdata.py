"""Config and bundled reference data (cities, existing stores, pin coordinates)."""
from __future__ import annotations

import csv
import math
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


@lru_cache(maxsize=1)
def config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def cities() -> dict[str, dict]:
    """City name -> {city, state, pin_prefix, tier, lat, lon}."""
    with open(DATA / "city_reference.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["lat"], r["lon"] = float(r["lat"]), float(r["lon"])
    return {r["city"]: r for r in rows}


@lru_cache(maxsize=1)
def prefix_to_city() -> dict[str, str]:
    return {r["pin_prefix"]: name for name, r in cities().items()}


@lru_cache(maxsize=1)
def bundled_stores() -> list[dict]:
    with open(DATA / "existing_stores.csv", encoding="utf-8") as f:
        return [
            {"store_id": r["store_id"], "city": r["city"], "pin": r["pin"], "opened": int(r["opened"])}
            for r in csv.DictReader(f)
        ]


@lru_cache(maxsize=1)
def pin_coords() -> dict[str, tuple[float, float]]:
    path = DATA / "pin_coords.csv"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return {r["pin"]: (float(r["lat"]), float(r["lon"])) for r in csv.DictReader(f)}


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def coords_for_pin(pin: str | None) -> tuple[float, float] | None:
    return pin_coords().get(pin) if pin else None


def lakh(rupees: float) -> float:
    return rupees / 100000.0
