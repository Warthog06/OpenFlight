#!/usr/bin/env python3
"""OpenFlight server: a free, self-hosted flight radar.

Only the Python standard library is used. The server:

* serves the web app in ./public
* fetches live aircraft positions from free, keyless ADS-B feeds
  (adsb.lol, airplanes.live, adsb.fi, OpenSky) and normalises them
* records every position it sees into a local SQLite database so any
  date and time it has seen can be replayed later
* falls back to ADS-B Exchange's free historical samples (1st day of
  each month, whole world) when the database has nothing for a time
* has a built-in "demo" source with simulated traffic that needs no
  network at all

Run:  python3 openflight.py            then open http://localhost:8080
Help: python3 openflight.py --help
"""

from __future__ import annotations

import argparse
import array
import gzip
import json
import math
import os
import random
import sqlite3
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
USER_AGENT = "OpenFlight/0.1 (+https://github.com/warthog06/openflight)"

NM_PER_DEG_LAT = 60.0
MAX_RADIUS_NM = 250  # the readsb-style APIs cap point queries at 250 nm
EARTH_RADIUS_NM = 3440.065


# --------------------------------------------------------------------------
# Small geo helpers
# --------------------------------------------------------------------------

def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def haversine_nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(a)))


def bbox_for_circle(lat, lon, radius_nm):
    dlat = radius_nm / NM_PER_DEG_LAT
    coslat = max(0.01, math.cos(math.radians(lat)))
    dlon = min(180.0, radius_nm / (NM_PER_DEG_LAT * coslat))
    return (clamp(lat - dlat, -90, 90), lon - dlon, clamp(lat + dlat, -90, 90), lon + dlon)


def wrap_lon(lon):
    return ((lon + 180.0) % 360.0) - 180.0


def in_bbox(lat, lon, bbox):
    lamin, lomin, lamax, lomax = bbox
    if lat is None or lon is None or not (lamin <= lat <= lamax):
        return False
    if lomax - lomin >= 360:
        return True
    lomin_w, lomax_w = wrap_lon(lomin), wrap_lon(lomax)
    if lomin_w <= lomax_w:
        return lomin_w <= lon <= lomax_w
    return lon >= lomin_w or lon <= lomax_w  # crosses the antimeridian


def lerp_angle(a, b, f):
    if a is None or b is None:
        return a if b is None else b
    d = ((b - a + 540.0) % 360.0) - 180.0
    return (a + d * f) % 360.0


def lerp(a, b, f):
    if a is None or b is None:
        return a if b is None else b
    return a + (b - a) * f


# --------------------------------------------------------------------------
# Normalised aircraft record
#
#   hex    ICAO 24-bit address (lowercase hex)       always present
#   cs     callsign / flight number                  may be None
#   reg    registration                              may be None
#   type   ICAO aircraft type (e.g. A320)            may be None
#   lat, lon                                         degrees
#   alt    barometric altitude, feet                 None if unknown
#   gnd    True when on the ground
#   gs     ground speed, knots
#   trk    track over ground, degrees true
#   vr     vertical rate, feet / minute
#   sq     squawk
#   cat    emitter category (A1..C3)
#   ts     unix time of the position fix (seconds, float)
# --------------------------------------------------------------------------

FIELDS = ("hex", "cs", "reg", "type", "lat", "lon", "alt", "gnd", "gs", "trk", "vr", "sq", "cat", "ts")


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _str(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def parse_readsb(payload, now=None):
    """Parse readsb / tar1090 style JSON (adsb.lol, airplanes.live, adsb.fi, ADSBx)."""
    if now is None:
        now = _num(payload.get("now"))
        if now and now > 1e11:  # the v2 APIs send milliseconds
            now /= 1000.0
        now = now or time.time()
    out = []
    for a in payload.get("ac") or payload.get("aircraft") or []:
        lat, lon = _num(a.get("lat")), _num(a.get("lon"))
        if lat is None or lon is None:
            lastpos = a.get("lastPosition") or {}
            lat, lon = _num(lastpos.get("lat")), _num(lastpos.get("lon"))
            if lat is None or lon is None:
                continue
            seen_pos = _num(lastpos.get("seen_pos")) or 0
        else:
            seen_pos = _num(a.get("seen_pos")) or _num(a.get("seen")) or 0
        if seen_pos > 60:
            continue
        hexid = str(a.get("hex", "")).strip().lower()
        if not hexid:
            continue
        alt_raw = a.get("alt_baro")
        gnd = alt_raw == "ground"
        alt = 0.0 if gnd else _num(alt_raw)
        if alt is None:
            alt = _num(a.get("alt_geom"))
        out.append({
            "hex": hexid,
            "cs": _str(a.get("flight")),
            "reg": _str(a.get("r")),
            "type": _str(a.get("t")),
            "lat": lat,
            "lon": lon,
            "alt": alt,
            "gnd": gnd,
            "gs": _num(a.get("gs")),
            "trk": _num(a.get("track")) if a.get("track") is not None else _num(a.get("true_heading")),
            "vr": _num(a.get("baro_rate")) if a.get("baro_rate") is not None else _num(a.get("geom_rate")),
            "sq": _str(a.get("squawk")),
            "cat": _str(a.get("category")),
            "ts": now - seen_pos,
        })
    return out


def parse_opensky(payload):
    """Parse the OpenSky Network /states/all response."""
    out = []
    now = _num(payload.get("time")) or time.time()
    for s in payload.get("states") or []:
        if len(s) < 17 or s[5] is None or s[6] is None:
            continue
        alt_m = s[7] if s[7] is not None else s[13]
        out.append({
            "hex": str(s[0]).strip().lower(),
            "cs": _str(s[1]),
            "reg": None,
            "type": None,
            "lat": float(s[6]),
            "lon": float(s[5]),
            "alt": None if alt_m is None else alt_m * 3.28084,
            "gnd": bool(s[8]),
            "gs": None if s[9] is None else s[9] * 1.943844,
            "trk": _num(s[10]),
            "vr": None if s[11] is None else s[11] * 196.8504,
            "sq": _str(s[14]),
            "cat": None,
            "ts": _num(s[3]) or now,
        })
    return out


# --------------------------------------------------------------------------
# Data providers
# --------------------------------------------------------------------------

HTTP_TIMEOUT = float(os.environ.get("OPENFLIGHT_HTTP_TIMEOUT", "10"))


def http_get_json(url, timeout=None, data=None, headers=None):
    timeout = HTTP_TIMEOUT if timeout is None else min(timeout, HTTP_TIMEOUT * 2)
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def http_get_bytes(url, timeout=None):
    timeout = HTTP_TIMEOUT * 3 if timeout is None else timeout
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


class ReadsbProvider:
    """adsb.lol, airplanes.live and adsb.fi all serve the same JSON shape."""

    def __init__(self, name, url_template, find_templates=None, min_interval=1.0):
        self.name = name
        self.url_template = url_template
        self.find_templates = find_templates or {}
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def fetch(self, lat, lon, radius_nm):
        radius_nm = int(math.ceil(clamp(radius_nm, 1, MAX_RADIUS_NM)))
        url = self.url_template.format(lat=round(lat, 4), lon=round(lon, 4), r=radius_nm)
        return self._get(url)

    def _get(self, url):
        with self._lock:  # respect the provider's rate limit (about 1 request / second)
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
        return parse_readsb(http_get_json(url))

    def find(self, query):
        """Look an aircraft up worldwide by callsign, registration or ICAO hex."""
        q = query.strip()
        kinds = ["callsign", "reg"]
        if len(q) == 6 and all(c in "0123456789abcdefABCDEF" for c in q):
            kinds.insert(0, "hex")
        for kind in kinds:
            tpl = self.find_templates.get(kind)
            if not tpl:
                continue
            res = self._get(tpl.format(q=urllib.parse.quote(q.upper() if kind != "hex" else q.lower())))
            if res:
                return res
        return []


class OpenSkyProvider:
    """Anonymous OpenSky access: 10 s resolution, limited daily credits."""

    name = "opensky"

    def fetch(self, lat, lon, radius_nm):
        lamin, lomin, lamax, lomax = bbox_for_circle(lat, lon, clamp(radius_nm, 1, MAX_RADIUS_NM))
        q = urllib.parse.urlencode({"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax})
        return parse_opensky(http_get_json("https://opensky-network.org/api/states/all?" + q, timeout=15))


# A few well known airports used by the demo simulator.
DEMO_AIRPORTS = [
    ("EGLL", 51.4700, -0.4543), ("EGKK", 51.1537, -0.1821), ("EHAM", 52.3105, 4.7683),
    ("LFPG", 49.0097, 2.5479), ("EDDF", 50.0379, 8.5622), ("EIDW", 53.4264, -6.2499),
    ("EGCC", 53.3537, -2.2750), ("EBBR", 50.9010, 4.4844), ("KJFK", 40.6413, -73.7781),
    ("KBOS", 42.3656, -71.0096), ("KORD", 41.9742, -87.9073), ("KLAX", 33.9416, -118.4085),
    ("KSFO", 37.6213, -122.3790), ("KATL", 33.6407, -84.4277), ("KDFW", 32.8998, -97.0403),
    ("CYYZ", 43.6777, -79.6248), ("OMDB", 25.2532, 55.3657), ("VIDP", 28.5562, 77.1000),
    ("VABB", 19.0896, 72.8656), ("WSSS", 1.3644, 103.9915), ("RJTT", 35.5494, 139.7798),
    ("YSSY", -33.9399, 151.1753), ("ZBAA", 40.0799, 116.6031), ("FAOR", -26.1392, 28.2460),
    ("SBGR", -23.4356, -46.4731), ("LEMD", 40.4983, -3.5676), ("LIRF", 41.8003, 12.2389),
    ("LTFM", 41.2753, 28.7519), ("OTHH", 25.2731, 51.6081), ("VHHH", 22.3080, 113.9185),
]
DEMO_AIRLINES = ["BAW", "AFR", "DLH", "KLM", "UAL", "AAL", "DAL", "EZY", "RYR", "UAE", "QTR", "SIA", "ANA", "QFA", "AIC"]
DEMO_TYPES = ["A320", "A321", "B738", "B77W", "A359", "B789", "A333", "E190", "A20N", "B38M"]


def _gc_point(lat1, lon1, lat2, lon2, f):
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    d = 2 * math.asin(math.sqrt(math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2))
    if d == 0:
        return lat1, lon1
    a = math.sin((1 - f) * d) / math.sin(d)
    b = math.sin(f * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def _bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


class DemoProvider:
    """Simulated traffic. Deterministic in time, so it also works for any past date."""

    name = "demo"

    def __init__(self, flights_per_route=6):
        rnd = random.Random(42)
        self.flights = []
        n = 0
        for i, (code1, la1, lo1) in enumerate(DEMO_AIRPORTS):
            for code2, la2, lo2 in DEMO_AIRPORTS[i + 1:]:
                dist = haversine_nm(la1, lo1, la2, lo2)
                if dist < 150:
                    continue
                for k in range(flights_per_route):
                    n += 1
                    outbound = k % 2 == 0
                    src, dst = ((la1, lo1), (la2, lo2)) if outbound else ((la2, lo2), (la1, lo1))
                    gs = rnd.uniform(420, 500)
                    cruise = rnd.choice(range(30000, 41001, 1000))
                    duration = dist / gs * 3600.0
                    period = duration + rnd.uniform(1800, 7200)
                    self.flights.append({
                        "hex": "%06x" % (0xD00000 + n),
                        "cs": "%s%d" % (rnd.choice(DEMO_AIRLINES), rnd.randint(10, 9999)),
                        "reg": "D-%s" % "".join(rnd.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4)),
                        "type": rnd.choice(DEMO_TYPES),
                        "src": src, "dst": dst, "dist": dist, "gs": gs, "cruise": cruise,
                        "duration": duration, "period": period, "offset": rnd.uniform(0, period),
                    })

    def state_at(self, f, t):
        phase = (t + f["offset"]) % f["period"]
        if phase > f["duration"]:
            return None
        frac = phase / f["duration"]
        lat, lon = _gc_point(f["src"][0], f["src"][1], f["dst"][0], f["dst"][1], frac)
        lat2, lon2 = _gc_point(f["src"][0], f["src"][1], f["dst"][0], f["dst"][1], min(1.0, frac + 1e-4))
        flown = frac * f["dist"]
        remaining = f["dist"] - flown
        climb_nm = 120.0
        if flown < climb_nm:
            alt, vr = f["cruise"] * flown / climb_nm, f["cruise"] / (climb_nm / f["gs"] * 60)
        elif remaining < climb_nm:
            alt, vr = f["cruise"] * remaining / climb_nm, -f["cruise"] / (climb_nm / f["gs"] * 60)
        else:
            alt, vr = f["cruise"], 0.0
        gs = f["gs"] * (0.45 + 0.55 * min(1.0, alt / 10000.0))
        return {
            "hex": f["hex"], "cs": f["cs"], "reg": f["reg"], "type": f["type"],
            "lat": lat, "lon": lon, "alt": round(alt / 25) * 25, "gnd": False,
            "gs": round(gs, 1), "trk": round(_bearing(lat, lon, lat2, lon2), 1), "vr": round(vr / 64) * 64,
            "sq": "%04o" % (int(f["hex"], 16) % 4096), "cat": "A3", "ts": t,
        }

    def snapshot(self, t, bbox):
        out = []
        for f in self.flights:
            s = self.state_at(f, t)
            if s and in_bbox(s["lat"], s["lon"], bbox):
                out.append(s)
        return out

    def fetch(self, lat, lon, radius_nm):
        return self.snapshot(time.time(), bbox_for_circle(lat, lon, radius_nm))

    def find(self, query, t=None):
        q = query.strip().lower()
        t = time.time() if t is None else t
        out = []
        for f in self.flights:
            if q in (f["hex"], f["cs"].lower(), f["reg"].lower()):
                s = self.state_at(f, t)
                if s:
                    out.append(s)
        return out


def _readsb(name, base, point, names=("hex", "callsign", "reg")):
    return lambda: ReadsbProvider(name, base + point, {
        "hex": base + "/v2/%s/{q}" % names[0],
        "callsign": base + "/v2/%s/{q}" % names[1],
        "reg": base + "/v2/%s/{q}" % names[2],
    })


PROVIDERS = {
    "adsb.lol": _readsb("adsb.lol", "https://api.adsb.lol", "/v2/point/{lat}/{lon}/{r}"),
    "airplanes.live": _readsb("airplanes.live", "https://api.airplanes.live", "/v2/point/{lat}/{lon}/{r}"),
    "adsb.fi": _readsb("adsb.fi", "https://opendata.adsb.fi/api", "/v2/lat/{lat}/lon/{lon}/dist/{r}",
                       ("hex", "callsign", "registration")),
    "opensky": OpenSkyProvider,
    "demo": DemoProvider,
}


class LiveSource:
    """Tries each configured provider in order and remembers the one that works."""

    def __init__(self, names):
        self.providers = [PROVIDERS[n]() for n in names]
        self.active = 0
        self.last_error = None
        self._cache = OrderedDict()
        self._cache_lock = threading.Lock()
        self._fetch_lock = threading.Lock()

    def fetch(self, lat, lon, radius_nm):
        # Viewers looking at roughly the same area share one upstream request per second.
        key = (round(lat, 1), round(lon, 1), int(math.ceil(radius_nm / 25.0) * 25))
        with self._fetch_lock:
            with self._cache_lock:
                hit = self._cache.get(key)
                if hit and time.time() - hit[0] < 1.0:
                    return hit[1], hit[2]
            errors = []
            n = len(self.providers)
            for i in range(n):
                idx = (self.active + i) % n
                p = self.providers[idx]
                try:
                    aircraft = p.fetch(lat, lon, key[2])
                except Exception as e:  # network errors, HTTP 429, bad JSON...
                    errors.append("%s: %s" % (p.name, e))
                    continue
                self.active = idx
                self.last_error = None
                with self._cache_lock:
                    self._cache[key] = (time.time(), aircraft, p.name)
                    while len(self._cache) > 64:
                        self._cache.popitem(last=False)
                return aircraft, p.name
            self.last_error = "; ".join(errors)
            raise RuntimeError(self.last_error)

    def find(self, query):
        errors = []
        for p in self.providers:
            if not hasattr(p, "find"):
                continue
            try:
                return p.find(query)
            except Exception as e:
                errors.append("%s: %s" % (p.name, e))
        if errors:
            raise RuntimeError("; ".join(errors))
        return []

    @property
    def name(self):
        return self.providers[self.active].name


# --------------------------------------------------------------------------
# Recording and replay
# --------------------------------------------------------------------------

class Store:
    """SQLite store of every position seen, used to replay any recorded time."""

    def __init__(self, path, record_interval=5.0, retention_days=7.0):
        self.path = path
        self.record_interval = record_interval
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._last_saved = {}
        self._last_prune = 0.0
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS pos (
                ts REAL NOT NULL, hex TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
                alt REAL, gnd INTEGER, gs REAL, trk REAL, vr REAL,
                cs TEXT, reg TEXT, type TEXT, sq TEXT, cat TEXT,
                PRIMARY KEY (hex, ts)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS pos_ts ON pos (ts);
        """)
        self.db.commit()

    def record(self, aircraft):
        rows = []
        for a in aircraft:
            last = self._last_saved.get(a["hex"])
            if last is not None and a["ts"] - last < self.record_interval:
                continue
            self._last_saved[a["hex"]] = a["ts"]
            rows.append((round(a["ts"], 1), a["hex"], a["lat"], a["lon"], a["alt"], int(bool(a["gnd"])), a["gs"],
                         a["trk"], a["vr"], a["cs"], a["reg"], a["type"], a["sq"], a["cat"]))
        if not rows:
            return 0
        with self._lock:
            self.db.executemany("INSERT OR IGNORE INTO pos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.db.commit()
        if time.time() - self._last_prune > 3600:
            self.prune()
        if len(self._last_saved) > 200000:
            cutoff = time.time() - 3600
            self._last_saved = {k: v for k, v in self._last_saved.items() if v > cutoff}
        return len(rows)

    def prune(self):
        self._last_prune = time.time()
        cutoff = time.time() - self.retention_days * 86400
        with self._lock:
            self.db.execute("DELETE FROM pos WHERE ts < ?", (cutoff,))
            self.db.commit()

    def coverage(self):
        with self._lock:
            row = self.db.execute("SELECT MIN(ts), MAX(ts) FROM pos").fetchone()
        return {"from": row[0], "to": row[1]}

    def _rows(self, sql, args):
        with self._lock:
            cur = self.db.execute(sql, args)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def window(self, t, bbox, before, after):
        lamin, lomin, lamax, lomax = bbox
        sql = "SELECT * FROM pos WHERE ts BETWEEN ? AND ? AND lat BETWEEN ? AND ?"
        args = [t - before, t + after, lamin - 1, lamax + 1]
        rows = self._rows(sql, args)
        for r in rows:
            r["gnd"] = bool(r["gnd"])
        return rows

    def trail(self, hexid, t_from, t_to):
        rows = self._rows("SELECT ts, lat, lon, alt, gnd FROM pos WHERE hex = ? AND ts BETWEEN ? AND ? ORDER BY ts",
                          (hexid.lower(), t_from, t_to))
        for r in rows:
            r["gnd"] = bool(r["gnd"])
        return rows


def last_segment(points, max_gap=600.0):
    """Keep only the current flight: drop everything before the last gap in coverage."""
    for i in range(len(points) - 1, 0, -1):
        if points[i]["ts"] - points[i - 1]["ts"] > max_gap:
            return points[i:]
    return points


def interpolate_snapshot(rows, t, bbox, max_gap=180.0, max_age=60.0):
    """Turn raw recorded positions around time t into one position per aircraft at t."""
    by_hex = {}
    for r in rows:
        by_hex.setdefault(r["hex"], []).append(r)
    out = []
    for hexid, pts in by_hex.items():
        pts.sort(key=lambda p: p["ts"])
        before = after = None
        for p in pts:
            if p["ts"] <= t:
                before = p
            else:
                after = p
                break
        if before is None:
            continue
        if after is not None and after["ts"] - before["ts"] <= max_gap:
            f = (t - before["ts"]) / (after["ts"] - before["ts"])
            s = dict(before)
            s["lat"] = lerp(before["lat"], after["lat"], f)
            dlon = ((after["lon"] - before["lon"] + 540.0) % 360.0) - 180.0
            s["lon"] = wrap_lon(before["lon"] + dlon * f)
            s["alt"] = lerp(before["alt"], after["alt"], f)
            s["gs"] = lerp(before["gs"], after["gs"], f)
            s["trk"] = lerp_angle(before["trk"], after["trk"], f)
            s["vr"] = lerp(before["vr"], after["vr"], f)
            s["gnd"] = before["gnd"] and after["gnd"]
            s["ts"] = t
        elif t - before["ts"] <= max_age:
            s = dict(before)
        else:
            continue
        if in_bbox(s["lat"], s["lon"], bbox):
            out.append({k: s.get(k) for k in FIELDS})
    return out


class AdsbLolArchive:
    """Worldwide replay from adsb.lol's public history (the files behind its own replay feature).

    adsb.lol writes one file per half hour (UTC) holding every aircraft position it saw, in
    slices every few seconds. Files appear once the half hour is over and are kept for about
    45 days. The data is open (ODbL): https://adsb.lol

    File layout (tar1090 "heatmap" format, little-endian int32, 4 per 16-byte record):
      slice header : MAGIC, time_ms_high, time_ms_low, interval_ms & 0xFFFF
      position     : flags<<24 | icao, lat*1e6, lon*1e6, (gs*10)<<16 | (alt/25 & 0xFFFF)
      info         : flags<<24 | icao, 1<<30 | squawk, callsign (8 ASCII bytes over 2 ints)
    """

    BASE = "https://globe.adsb.lol/globe_history/{y:04d}/{m:02d}/{d:02d}/heatmap/{idx:02d}.bin.ttf"
    MAGIC = 0xE7F7C9D
    CHUNK = 1800
    RETENTION_DAYS = 45

    def __init__(self, enabled=True, cache_size=3):
        self.enabled = enabled
        self.cache_size = cache_size
        self._cache = OrderedDict()
        self._missing = {}
        self._lock = threading.Lock()
        self._loading = {}

    def covers(self, t):
        now = time.time()
        return self.enabled and now - self.RETENTION_DAYS * 86400 <= t < (now // self.CHUNK) * self.CHUNK

    def _download(self, start):
        tm = time.gmtime(start)
        url = self.BASE.format(y=tm.tm_year, m=tm.tm_mon, d=tm.tm_mday, idx=tm.tm_hour * 2 + tm.tm_min // 30)
        raw = http_get_bytes(url)
        if len(raw) % 16:
            raise ValueError("bad archive file size")
        ints = array.array("i")
        ints.frombytes(raw)
        if sys.byteorder != "little":
            ints.byteswap()
        magic = struct.pack("<I", self.MAGIC)
        slices = []  # (int index of header, unix time)
        pos = raw.find(magic)
        while pos != -1:
            if pos % 16 == 0:
                i = pos // 4
                slices.append((i, ((ints[i + 1] & 0xFFFFFFFF) * 4294967296 + (ints[i + 2] & 0xFFFFFFFF)) / 1000.0))
            pos = raw.find(magic, pos + 4)
        if not slices:
            raise ValueError("no data in archive file")
        info = {}
        for i in range(0, len(ints), 4):
            if ints[i + 1] >= 1073741824 and ints[i] != self.MAGIC:
                cs = raw[4 * (i + 2):4 * (i + 4)].split(b"\0", 1)[0].decode("ascii", "replace").strip()
                info[self._hex(ints[i])] = (cs or None, "%04d" % (ints[i + 1] & 0xFFFF))
        return {"ints": ints, "slices": slices, "info": info}

    @staticmethod
    def _hex(word):
        h = "%06x" % (word & 0xFFFFFF)
        return "~" + h if word & 0x1000000 else h

    def _chunk(self, start):
        """Load (or reuse) the half-hour file starting at `start`; None if unavailable."""
        while True:
            with self._lock:
                if start in self._cache:
                    self._cache.move_to_end(start)
                    return self._cache[start]
                if time.time() - self._missing.get(start, 0) < 120:
                    return None
                ev = self._loading.get(start)
                if ev is None:
                    ev = self._loading[start] = threading.Event()
                    break
            ev.wait(60)  # another request is downloading this file
        try:
            chunk = self._download(start)
        except Exception as e:
            print("adsb.lol archive %s: %s" % (time.strftime("%Y-%m-%d %H:%M", time.gmtime(start)), e), file=sys.stderr)
            chunk = None
        with self._lock:
            del self._loading[start]
            if chunk is None:
                self._missing[start] = time.time()
            else:
                self._cache[start] = chunk
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
        ev.set()
        return chunk

    def _rows(self, t_from, t_to, bbox=None, hexid=None):
        """Decoded positions between two times, optionally only in bbox or for one aircraft."""
        rows = []
        start = (t_from // self.CHUNK) * self.CHUNK
        while start <= t_to:
            if self.covers(start):
                chunk = self._chunk(start)
            else:
                chunk = None
            start += self.CHUNK
            if chunk is None:
                continue
            ints, slices, info = chunk["ints"], chunk["slices"], chunk["info"]
            if bbox:
                lamin, lamax = int((bbox[0] - 0.5) * 1e6), int((bbox[2] + 0.5) * 1e6)
                wide = (bbox[0] - 0.5, bbox[1] - 0.5, bbox[2] + 0.5, bbox[3] + 0.5)
            for n, (i, ts) in enumerate(slices):
                if ts < t_from or ts > t_to:
                    continue
                end = slices[n + 1][0] if n + 1 < len(slices) else len(ints)
                for j in range(i + 4, end, 4):
                    lat = ints[j + 1]
                    if lat >= 1073741824:
                        continue
                    if bbox and not (lamin <= lat <= lamax):
                        continue
                    h = self._hex(ints[j])
                    if hexid and h != hexid:
                        continue
                    lon = ints[j + 2] / 1e6
                    if bbox and not in_bbox(lat / 1e6, lon, wide):
                        continue
                    packed = ints[j + 3]
                    alt = packed & 0xFFFF
                    if alt & 0x8000:
                        alt -= 0x10000
                    gs = packed >> 16
                    cs, sq = info.get(h, (None, None))
                    rows.append({
                        "hex": h, "cs": cs, "reg": None, "type": None, "lat": lat / 1e6, "lon": lon,
                        "alt": 0.0 if alt == -123 else None if alt == -124 else alt * 25.0, "gnd": alt == -123,
                        "gs": None if gs == -1 else gs / 10.0, "trk": None, "vr": None, "sq": sq, "cat": None, "ts": ts,
                    })
        return rows

    @staticmethod
    def _add_motion(rows):
        """The archive has no heading or climb rate, so work them out from consecutive positions."""
        by_hex = {}
        for r in rows:
            by_hex.setdefault(r["hex"], []).append(r)
        for pts in by_hex.values():
            pts.sort(key=lambda p: p["ts"])
            for k, p in enumerate(pts):
                a, b = (p, pts[k + 1]) if k + 1 < len(pts) else (pts[k - 1], p) if k else (None, None)
                if a is None or b["ts"] <= a["ts"]:
                    continue
                if haversine_nm(a["lat"], a["lon"], b["lat"], b["lon"]) > 0.02:
                    p["trk"] = _bearing(a["lat"], a["lon"], b["lat"], b["lon"])
                if a["alt"] is not None and b["alt"] is not None and not (a["gnd"] or b["gnd"]):
                    p["vr"] = round((b["alt"] - a["alt"]) / (b["ts"] - a["ts"]) * 60 / 64) * 64
        return rows

    def snapshot(self, t, bbox):
        if not self.covers(t):
            return None
        rows = self._add_motion(self._rows(t - 60, t + 60, bbox=bbox))
        return interpolate_snapshot(rows, t, bbox, max_gap=90.0, max_age=40.0)

    def trail(self, hexid, t_from, t_to):
        # Only the half hours that are already loaded or needed for t_to, to keep downloads small.
        t_from = max(t_from, (t_to // self.CHUNK) * self.CHUNK - self.CHUNK)
        return [{k: r[k] for k in ("ts", "lat", "lon", "alt", "gnd")} for r in self._rows(t_from, t_to, hexid=hexid)]


class AdsbxSamples:
    """ADS-B Exchange publishes free whole-world snapshots every 5 s for the 1st of each month.

    https://www.adsbexchange.com/products/historical-data/  (check their terms before heavy use)
    """

    BASE = "https://samples.adsbexchange.com/readsb-hist/{y:04d}/{m:02d}/01/{hh:02d}{mm:02d}{ss:02d}Z.json.gz"

    def __init__(self, enabled=True):
        self.enabled = enabled
        self._cache = OrderedDict()
        self._lock = threading.Lock()
        self._missing = {}

    @staticmethod
    def covers(t):
        tm = time.gmtime(t)
        return tm.tm_mday == 1 and t < time.time() - 86400

    def _load(self, slot):
        with self._lock:
            if slot in self._cache:
                self._cache.move_to_end(slot)
                return self._cache[slot]
            if time.time() - self._missing.get(slot, 0) < 300:
                return None
        tm = time.gmtime(slot)
        url = self.BASE.format(y=tm.tm_year, m=tm.tm_mon, hh=tm.tm_hour, mm=tm.tm_min, ss=tm.tm_sec)
        try:
            payload = http_get_json(url, timeout=20)
            aircraft = parse_readsb(payload, now=_num(payload.get("now")) or slot)
        except Exception:
            with self._lock:
                self._missing[slot] = time.time()
            return None
        with self._lock:
            self._cache[slot] = aircraft
            while len(self._cache) > 24:
                self._cache.popitem(last=False)
        return aircraft

    def snapshot(self, t, bbox):
        if not (self.enabled and self.covers(t)):
            return None
        slot = int(t // 5 * 5)
        a = self._load(slot)
        if a is None:
            return None
        b = self._load(slot + 5) or []
        rows = [r for r in a + b if in_bbox(r["lat"], r["lon"], (bbox[0] - 1, bbox[1] - 1, bbox[2] + 1, bbox[3] + 1))]
        return interpolate_snapshot(rows, t, bbox, max_gap=30.0, max_age=30.0)


# --------------------------------------------------------------------------
# Background recorder for fixed areas
# --------------------------------------------------------------------------

class Recorder(threading.Thread):
    def __init__(self, live, store, areas, interval):
        super().__init__(daemon=True)
        self.live, self.store, self.areas, self.interval = live, store, areas, interval

    def run(self):
        while True:
            start = time.time()
            for lat, lon, r in self.areas:
                try:
                    aircraft, _ = self.live.fetch(lat, lon, r)
                    self.store.record(aircraft)
                except Exception as e:
                    print("recorder: %s" % e, file=sys.stderr)
            time.sleep(max(0.5, self.interval - (time.time() - start)))


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------

class App:
    def __init__(self, args, serverless=False):
        self.args = args
        # On serverless hosts (Vercel) there is no lasting disk or background thread,
        # so nothing is recorded: replay falls back to the 1st-of-month samples.
        self.serverless = serverless
        self.live = LiveSource(args.providers)
        self.demo = next((p for p in self.live.providers if isinstance(p, DemoProvider)), None)
        self.store = Store(args.db, args.record_interval, args.retention_days)
        self.samples = AdsbxSamples(enabled=not args.no_samples and self.demo is None)
        self.archive = AdsbLolArchive(enabled=not args.no_archive and self.demo is None)


def make_handler(app, fixed_route=None):
    """Build the request handler. fixed_route pins it to one API route (used by Vercel functions)."""

    class Handler(SimpleHTTPRequestHandler):
        server_version = "OpenFlight/0.1"

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=PUBLIC, **kw)

        def log_message(self, fmt, *args):
            if app.args.verbose:
                super().log_message(fmt, *args)

        def end_headers(self):
            if self.path.startswith("/api/"):
                self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def send_json(self, obj, status=200):
            body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
            if not app.serverless and "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 1024:
                body = gzip.compress(body, 5)
                gz = True
            else:
                gz = False
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            path = fixed_route or url.path
            if not path.startswith("/api/"):
                return super().do_GET()
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            route = {
                "/api/config": self.api_config,
                "/api/live": self.api_live,
                "/api/history": self.api_history,
                "/api/trail": self.api_trail,
                "/api/route": self.api_route,
                "/api/find": self.api_find,
            }.get(path)
            if route is None:
                return self.send_json({"error": "not found"}, 404)
            try:
                route(q)
            except (KeyError, ValueError) as e:
                self.send_json({"error": "bad request: %s" % e}, 400)
            except Exception as e:
                self.send_json({"error": str(e)}, 502)

        # GET /api/config
        def api_config(self, q):
            self.send_json({
                "provider": app.live.name,
                "providers": [p.name for p in app.live.providers],
                "tiles": app.args.tiles,
                "tileAttribution": app.args.tile_attribution,
                "recordInterval": app.store.record_interval,
                "retentionDays": app.store.retention_days,
                "recorded": app.store.coverage(),
                "samples": app.samples.enabled,
                "archive": {"from": time.time() - AdsbLolArchive.RETENTION_DAYS * 86400,
                            "to": (time.time() // AdsbLolArchive.CHUNK) * AdsbLolArchive.CHUNK}
                if app.archive.enabled else None,
                "demo": app.demo is not None,
                "serverless": app.serverless,
                "lastError": app.live.last_error,
                "now": time.time(),
            })

        # GET /api/live?lat=..&lon=..&radius=nm
        def api_live(self, q):
            lat, lon = float(q["lat"]), wrap_lon(float(q["lon"]))
            radius = clamp(float(q.get("radius", 100)), 1, MAX_RADIUS_NM)
            aircraft, source = app.live.fetch(lat, lon, radius)
            if not app.serverless and not isinstance(app.live.providers[app.live.active], DemoProvider):
                app.store.record(aircraft)
            self.send_json({"now": time.time(), "source": source, "aircraft": aircraft})

        # GET /api/history?t=unix&lat=..&lon=..&radius=nm
        def api_history(self, q):
            t = float(q["t"])
            radius = clamp(float(q.get("radius", 100)), 1, MAX_RADIUS_NM)
            bbox = bbox_for_circle(float(q["lat"]), wrap_lon(float(q["lon"])), radius)
            if app.demo is not None:
                return self.send_json({"t": t, "source": "demo", "aircraft": app.demo.snapshot(t, bbox)})
            gap = max(180.0, app.store.record_interval * 4)
            rows = app.store.window(t, bbox, before=gap, after=gap)
            aircraft = interpolate_snapshot(rows, t, bbox, max_gap=gap, max_age=max(60.0, app.store.record_interval * 3))
            source = "recorded"
            if not aircraft:
                archived = app.archive.snapshot(t, bbox)
                if archived:
                    aircraft, source = archived, "adsb.lol archive"
            if not aircraft:
                sample = app.samples.snapshot(t, bbox)
                if sample is not None:
                    aircraft, source = sample, "adsbexchange-samples"
            self.send_json({"t": t, "source": source if aircraft else "none", "aircraft": aircraft})

        # GET /api/trail?hex=..&from=unix&to=unix
        def api_trail(self, q):
            hexid = q["hex"].lower()
            t_to = float(q.get("to", time.time()))
            t_from = float(q.get("from", t_to - 3600))
            if app.demo is not None:
                f = next((f for f in app.demo.flights if f["hex"] == hexid), None)
                pts = []
                if f:
                    step = max(10.0, (t_to - t_from) / 400)
                    t = t_from
                    while t <= t_to:
                        s = app.demo.state_at(f, t)
                        if s:
                            pts.append({k: s[k] for k in ("ts", "lat", "lon", "alt", "gnd")})
                        t += step
                return self.send_json({"hex": hexid, "points": last_segment(pts, max_gap=step * 3)})
            points = app.store.trail(hexid, t_from, t_to)
            if not points and app.archive.covers(t_to):
                points = app.archive.trail(hexid, t_from, t_to)
            self.send_json({"hex": hexid, "points": last_segment(points)})

        # GET /api/find?q=BAW123   (worldwide lookup by callsign, registration or hex)
        def api_find(self, q):
            query = q["q"].strip()
            if not query or len(query) > 12:
                raise ValueError("query must be 1-12 characters")
            self.send_json({"q": query, "aircraft": app.live.find(query)})

        # GET /api/route?callsign=..&lat=..&lon=..   (origin / destination guess from adsb.lol)
        def api_route(self, q):
            cs = q["callsign"].strip().upper()
            if app.demo is not None:
                return self.send_json({"callsign": cs, "route": None})
            body = json.dumps({"planes": [{"callsign": cs, "lat": float(q.get("lat", 0)), "lng": float(q.get("lon", 0))}]})
            try:
                res = http_get_json("https://api.adsb.lol/api/0/routeset", timeout=8, data=body.encode(),
                                    headers={"Content-Type": "application/json"})
            except Exception as e:
                return self.send_json({"callsign": cs, "route": None, "error": str(e)})
            route = None
            if isinstance(res, list) and res:
                r = res[0]
                airports = [{"icao": a.get("icao"), "iata": a.get("iata"), "name": a.get("name"),
                             "city": a.get("location"), "lat": a.get("lat"), "lon": a.get("lon")}
                            for a in r.get("_airports") or []]
                route = {"airports": airports, "plausible": r.get("plausible")} if airports else None
            self.send_json({"callsign": cs, "route": route})

    return Handler


def parse_area(s):
    try:
        lat, lon, r = (float(x) for x in s.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("area must look like LAT,LON,RADIUS_NM e.g. 51.47,-0.45,150")
    return lat, lon, clamp(r, 1, MAX_RADIUS_NM)


def build_arg_parser():
    p = argparse.ArgumentParser(description="OpenFlight: a free, self-hosted flight radar.")
    p.add_argument("--host", default=os.environ.get("OPENFLIGHT_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("OPENFLIGHT_PORT", "8080")))
    p.add_argument("--provider", dest="providers", action="append", choices=sorted(PROVIDERS),
                   help="live data source; repeat to set fallbacks in order "
                        "(default: adsb.lol, then airplanes.live, then adsb.fi)")
    p.add_argument("--demo", action="store_true", help="use simulated traffic (no internet needed)")
    p.add_argument("--db", default=os.environ.get("OPENFLIGHT_DB", os.path.join(ROOT, "openflight.db")),
                   help="SQLite file for recorded positions")
    p.add_argument("--record-interval", type=float, default=5.0,
                   help="seconds between stored positions of the same aircraft (default 5)")
    p.add_argument("--retention-days", type=float, default=7.0, help="delete recordings older than this (default 7)")
    p.add_argument("--record", dest="areas", action="append", type=parse_area, default=[],
                   metavar="LAT,LON,NM", help="keep recording this area even with no browser open; repeatable")
    p.add_argument("--no-archive", action="store_true",
                   help="never fetch replay data from the adsb.lol history archive")
    p.add_argument("--no-samples", action="store_true",
                   help="never fetch ADS-B Exchange 1st-of-month history samples")
    p.add_argument("--tiles", default=os.environ.get("OPENFLIGHT_TILES", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
                   help="raster tile URL template")
    p.add_argument("--tile-attribution",
                   default='&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a> contributors')
    p.add_argument("-v", "--verbose", action="store_true", help="log every HTTP request")
    return p


def resolve_providers(args):
    if args.demo:
        args.providers = ["demo"]
    elif not args.providers:
        args.providers = ["adsb.lol", "airplanes.live", "adsb.fi"]
    return args


_serverless_app = None
_serverless_lock = threading.Lock()


def serverless_app():
    """One shared App per serverless instance, configured from environment variables:

    OPENFLIGHT_DEMO=1                      simulated traffic
    OPENFLIGHT_PROVIDERS=adsb.lol,adsb.fi  live sources in fallback order
    OPENFLIGHT_TILES=https://...           raster tile URL
    OPENFLIGHT_NO_SAMPLES=1                skip ADS-B Exchange 1st-of-month samples
    OPENFLIGHT_NO_ARCHIVE=1                skip the adsb.lol history archive
    """
    global _serverless_app, HTTP_TIMEOUT
    with _serverless_lock:
        if _serverless_app is None:
            argv = ["--db", ":memory:"]
            if os.environ.get("OPENFLIGHT_DEMO", "").lower() in ("1", "true", "yes"):
                argv.append("--demo")
            for name in filter(None, (x.strip() for x in os.environ.get("OPENFLIGHT_PROVIDERS", "").split(","))):
                argv += ["--provider", name]
            if os.environ.get("OPENFLIGHT_NO_SAMPLES", "").lower() in ("1", "true", "yes"):
                argv.append("--no-samples")
            if os.environ.get("OPENFLIGHT_NO_ARCHIVE", "").lower() in ("1", "true", "yes"):
                argv.append("--no-archive")
            if "OPENFLIGHT_HTTP_TIMEOUT" not in os.environ:
                HTTP_TIMEOUT = 6.0  # leave room to try the fallbacks inside the function time limit
            _serverless_app = App(resolve_providers(build_arg_parser().parse_args(argv)), serverless=True)
        return _serverless_app


def main(argv=None):
    args = resolve_providers(build_arg_parser().parse_args(argv))
    app = App(args)
    if args.areas:
        Recorder(app.live, app.store, args.areas, max(1.0, args.record_interval)).start()
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    httpd.daemon_threads = True
    print("OpenFlight running on http://%s:%d  (data: %s)" % (
        "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host, args.port, ", ".join(args.providers)))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
