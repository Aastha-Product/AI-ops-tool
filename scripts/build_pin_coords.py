"""One-time builder for data/pin_coords.csv.

Source: All India Pincode Directory (Department of Posts, data.gov.in, Government
Open Data Licence), via the GitHub mirror below. Run once and commit the output;
the app never calls the internet for coordinates at runtime.

Cleaning:
  1. Keep pins whose prefix is one of our 7 cities; drop rows with bad coordinates.
  2. If any of a pin's rows lie within CITY_CORE_KM (30 km) of the city centre,
     keep only those rows. This removes the known bad 560066 row near
     12.15, 77.13 (Whitefield misplaced ~100 km away).
  3. Otherwise (pin genuinely outside the city core: pin prefixes cover whole
     postal districts, 577 alone has ~160 pins) keep rows within
     MAX_KM_FROM_CENTRE, then drop rows more than PIN_OUTLIER_KM from that
     pin's median point.
  4. Pin centre = median lat / lon of the remaining rows.
  `in_city_core` marks pins whose centre is within CITY_CORE_KM of the centre.

Usage:
    python scripts/build_pin_coords.py                 # downloads the CSV
    python scripts/build_pin_coords.py path/to/pincode.csv
"""
from __future__ import annotations

import csv
import io
import math
import statistics
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

SOURCE_URL = "https://raw.githubusercontent.com/dropdevrahul/pincodes-india/main/pincode.csv"
ROOT = Path(__file__).resolve().parent.parent
MAX_KM_FROM_CENTRE = 150.0
PIN_OUTLIER_KM = 15.0
CITY_CORE_KM = 30.0


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_cities():
    with open(ROOT / "data" / "city_reference.csv", encoding="utf-8") as f:
        return {row["pin_prefix"]: row for row in csv.DictReader(f)}


def read_source(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    with urllib.request.urlopen(SOURCE_URL, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main(path: str | None = None):
    cities = load_cities()
    text = read_source(path)
    points = defaultdict(list)
    dropped_far = 0
    for row in csv.DictReader(io.StringIO(text)):
        pin = (row.get("Pincode") or "").strip()
        if len(pin) != 6 or pin[:3] not in cities:
            continue
        try:
            lat, lon = float(row["Latitude"]), float(row["Longitude"])
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)) or lat == 0 or lon == 0:
            continue
        city = cities[pin[:3]]
        dist = haversine_km(lat, lon, float(city["lat"]), float(city["lon"]))
        if dist > MAX_KM_FROM_CENTRE:
            dropped_far += 1
            continue
        points[pin].append((lat, lon, dist))

    out = ROOT / "data" / "pin_coords.csv"
    dropped_outlier = 0
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["pin", "lat", "lon", "n_offices", "in_city_core"])
        for pin in sorted(points):
            rows = points[pin]
            core_rows = [p for p in rows if p[2] <= CITY_CORE_KM]
            if core_rows:
                kept = core_rows
            else:
                mlat = statistics.median(p[0] for p in rows)
                mlon = statistics.median(p[1] for p in rows)
                kept = [p for p in rows if haversine_km(p[0], p[1], mlat, mlon) <= PIN_OUTLIER_KM] or rows
            dropped_outlier += len(rows) - len(kept)
            lat = statistics.median(p[0] for p in kept)
            lon = statistics.median(p[1] for p in kept)
            city = cities[pin[:3]]
            core = haversine_km(lat, lon, float(city["lat"]), float(city["lon"])) <= CITY_CORE_KM
            w.writerow([pin, round(lat, 5), round(lon, 5), len(kept), int(core)])

    with open(ROOT / "data" / "existing_stores.csv", encoding="utf-8") as f:
        store_pins = {r["pin"] for r in csv.DictReader(f)}
    missing = store_pins - set(points)
    assert not missing, f"Existing-store pins missing coordinates: {sorted(missing)}"
    print(f"Wrote {len(points)} pins to {out} (dropped {dropped_far} rows > {MAX_KM_FROM_CENTRE} km "
          f"from city centre, {dropped_outlier} per-pin outlier rows)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
