"""Tests for server.py. Run with:  python3 -m unittest discover -s tests"""

import gzip
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server  # noqa: E402


def readsb_payload(now_s, aircraft):
    return {"now": now_s * 1000, "ac": aircraft}


class ParsingTests(unittest.TestCase):
    def test_parse_readsb(self):
        now = 1_700_000_000
        rows = server.parse_readsb(readsb_payload(now, [
            {"hex": "4CA123", "flight": "RYR1AB  ", "r": "EI-ABC", "t": "B738", "lat": 51.5, "lon": -0.4,
             "alt_baro": 35000, "gs": 450.2, "track": 90.0, "baro_rate": -64, "squawk": "1234", "category": "A3",
             "seen_pos": 1.5},
            {"hex": "406abc", "lat": 51.47, "lon": -0.45, "alt_baro": "ground", "gs": 12},
            {"hex": "nopos"},
            {"hex": "stale", "lat": 1, "lon": 1, "seen_pos": 120},
            {"hex": "lastpos", "lastPosition": {"lat": 50.0, "lon": 1.0, "seen_pos": 10}, "alt_baro": 1000},
        ]))
        by_hex = {r["hex"]: r for r in rows}
        self.assertEqual(set(by_hex), {"4ca123", "406abc", "lastpos"})
        a = by_hex["4ca123"]
        self.assertEqual(a["cs"], "RYR1AB")
        self.assertEqual(a["alt"], 35000)
        self.assertFalse(a["gnd"])
        self.assertAlmostEqual(a["ts"], now - 1.5)
        self.assertTrue(by_hex["406abc"]["gnd"])
        self.assertEqual(by_hex["406abc"]["alt"], 0)
        self.assertAlmostEqual(by_hex["lastpos"]["ts"], now - 10)

    def test_parse_opensky(self):
        rows = server.parse_opensky({"time": 100, "states": [
            ["abc123", "DLH4  ", "Germany", 99, 100, 8.5, 50.0, 10000.0, False, 200.0, 45.0, 5.0, None, 10100.0, "1000", False, 0],
            ["def456", None, "X", 99, 100, None, None, None, True, 0, 0, 0, None, None, None, False, 0],
        ]})
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["cs"], "DLH4")
        self.assertAlmostEqual(r["alt"], 32808.4, places=0)
        self.assertAlmostEqual(r["gs"], 388.8, places=0)


class GeoTests(unittest.TestCase):
    def test_bbox_antimeridian(self):
        bbox = server.bbox_for_circle(0, 179.5, 120)
        self.assertTrue(server.in_bbox(0, -179.5, bbox))
        self.assertTrue(server.in_bbox(0, 179.9, bbox))
        self.assertFalse(server.in_bbox(0, 170, bbox))

    def test_lerp_angle_wraps(self):
        self.assertAlmostEqual(server.lerp_angle(350, 10, 0.5), 0)

    def test_interpolate_snapshot(self):
        base = dict(cs="X", reg=None, type=None, gnd=False, gs=400, vr=0, sq=None, cat=None)
        rows = [
            dict(base, hex="a", ts=100, lat=10, lon=179.9, alt=1000, trk=350),
            dict(base, hex="a", ts=110, lat=11, lon=-179.9, alt=2000, trk=10),
            dict(base, hex="b", ts=0, lat=10, lon=179.9, alt=1000, trk=0),  # too old
        ]
        out = server.interpolate_snapshot(rows, 105, (0, 170, 20, 190), max_gap=60, max_age=30)
        self.assertEqual([r["hex"] for r in out], ["a"])
        a = out[0]
        self.assertAlmostEqual(a["lat"], 10.5)
        self.assertAlmostEqual(abs(a["lon"]), 180, places=5)
        self.assertAlmostEqual(a["alt"], 1500)
        self.assertAlmostEqual(a["trk"] % 360, 0)

    def test_last_segment(self):
        pts = [{"ts": t} for t in (0, 10, 2000, 2010, 2020)]
        self.assertEqual([p["ts"] for p in server.last_segment(pts)], [2000, 2010, 2020])


class DemoTests(unittest.TestCase):
    def test_demo_is_deterministic_and_moves(self):
        d = server.DemoProvider()
        bbox = (-90, -180, 90, 180)
        a = {x["hex"]: x for x in d.snapshot(1_000_000, bbox)}
        b = {x["hex"]: x for x in d.snapshot(1_000_000, bbox)}
        c = {x["hex"]: x for x in d.snapshot(1_000_010, bbox)}
        self.assertGreater(len(a), 100)
        self.assertEqual(a, b)
        common = set(a) & set(c)
        self.assertTrue(any(a[h]["lat"] != c[h]["lat"] for h in common))


class FakeUpstream(BaseHTTPRequestHandler):
    """Pretends to be adsb.lol: serves gzipped readsb JSON with two aircraft moving east."""

    def do_GET(self):
        now = time.time()
        k = (now % 1000) / 1000.0
        body = json.dumps(readsb_payload(now, [
            {"hex": "aaaaaa", "flight": "TEST1", "lat": 51.5, "lon": -0.5 + k, "alt_baro": 30000, "gs": 400,
             "track": 90, "seen_pos": 0.1},
            {"hex": "bbbbbb", "flight": "TEST2", "lat": 51.6, "lon": -0.2, "alt_baro": "ground", "gs": 0,
             "track": 0, "seen_pos": 0.1},
        ])).encode()
        body = gzip.compress(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        up = "http://127.0.0.1:%d" % cls.upstream.server_address[1]
        server.PROVIDERS["fake"] = server._readsb("fake", up, "/v2/point/{lat}/{lon}/{r}")
        cls.tmp = tempfile.TemporaryDirectory()
        args = server.build_arg_parser().parse_args(
            ["--provider", "fake", "--db", os.path.join(cls.tmp.name, "t.db"), "--record-interval", "0", "--no-samples"])
        cls.app = server.App(args)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cls.app))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.upstream.shutdown()
        cls.app.store.db.close()
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read())

    def test_live_record_and_replay(self):
        t0 = time.time()
        live = self.get("/api/live?lat=51.5&lon=-0.3&radius=50")
        self.assertEqual(live["source"], "fake")
        self.assertEqual({a["hex"] for a in live["aircraft"]}, {"aaaaaa", "bbbbbb"})
        time.sleep(1.1)  # past the 1 s shared cache
        self.get("/api/live?lat=51.5&lon=-0.3&radius=50")
        t1 = time.time()

        hist = self.get("/api/history?t=%f&lat=51.5&lon=-0.3&radius=50" % ((t0 + t1) / 2))
        self.assertEqual(hist["source"], "recorded")
        self.assertEqual({a["hex"] for a in hist["aircraft"]}, {"aaaaaa", "bbbbbb"})

        empty = self.get("/api/history?t=%f&lat=51.5&lon=-0.3&radius=50" % (t0 - 86400))
        self.assertEqual(empty["source"], "none")
        self.assertEqual(empty["aircraft"], [])

        trail = self.get("/api/trail?hex=aaaaaa&from=%f&to=%f" % (t0 - 10, t1 + 10))
        self.assertGreaterEqual(len(trail["points"]), 2)

        cfg = self.get("/api/config")
        self.assertIsNotNone(cfg["recorded"]["from"])

        found = self.get("/api/find?q=aaaaaa")
        self.assertEqual(found["aircraft"][0]["hex"], "aaaaaa")

    def test_bad_request(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/api/live?lat=abc&lon=0")
        self.assertEqual(cm.exception.code, 400)

    def test_static_files(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as r:
            self.assertIn(b"OpenFlight", r.read())


class VercelFunctionTests(unittest.TestCase):
    """Load api/*.py the way Vercel does and check each answers on its own route."""

    def test_functions(self):
        import importlib.util
        os.environ["OPENFLIGHT_DEMO"] = "1"
        server._serverless_app = None
        root = os.path.join(os.path.dirname(__file__), "..")
        try:
            for name in ("config", "live", "history", "trail", "find", "route"):
                spec = importlib.util.spec_from_file_location("vercel_" + name, os.path.join(root, "api", name + ".py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), mod.handler)
                threading.Thread(target=httpd.serve_forever, daemon=True).start()
                # Vercel may hand the function the original URL or its own path; both must work.
                url = "http://127.0.0.1:%d/api/%s?lat=51.5&lon=-0.2&radius=100&t=%d&hex=d00001&q=d00001&callsign=X" % (
                    httpd.server_address[1], name, time.time() - 3600)
                with urllib.request.urlopen(url, timeout=10) as r:
                    body = json.loads(r.read())
                    self.assertNotIn("Content-Encoding", r.headers)
                httpd.shutdown()
                httpd.server_close()
                if name == "config":
                    self.assertTrue(body["serverless"])
                    self.assertTrue(body["demo"])
                if name in ("live", "history"):
                    self.assertGreater(len(body["aircraft"]), 0)
        finally:
            del os.environ["OPENFLIGHT_DEMO"]
            server._serverless_app = None


if __name__ == "__main__":
    unittest.main()
