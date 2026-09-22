"""Tests for the container edition (container/memtier_app.py). Standard library only:

    python3 -m unittest discover -s tests
"""

import base64
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "container"))
import memtier_app as app_mod  # noqa: E402
import memtier as mt  # noqa: E402


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def options(self, values):
        path = os.path.join(self.tmp, "options.json")
        with open(path, "w") as handle:
            json.dump(values, handle)
        return path

    def test_home_assistant_options(self):
        path = self.options({"servers": ["vc1.example.com", "vc2.example.com"], "username": "ro@vsphere.local",
                             "password": "secret", "verify_tls": False, "report_days": 45,
                             "stretched_clusters": ["Metro, Site A"], "support_contact": ""})
        s = app_mod.load_settings(environ={}, options_file=path)
        self.assertTrue(s.home_assistant)
        self.assertEqual(s.data_dir, self.tmp)
        self.assertTrue(s.configured)
        self.assertEqual(s.errors, [])
        self.assertEqual(s.report_days, 45)
        self.assertFalse(s.verify_tls)
        self.assertEqual(s.support_contact, "")  # an explicit empty value hides the contact
        self.assertNotIn("password", s.public())
        self.assertTrue(s.public()["password_set"])

        cfg = app_mod.memtier_config(s)
        self.assertEqual(cfg.servers, ["vc1.example.com", "vc2.example.com"])
        self.assertEqual(cfg.stretched_clusters, ["Metro, Site A"])  # comma inside a name survives
        self.assertEqual(cfg.data_dir, os.path.join(self.tmp, "data"))
        self.assertEqual(cfg.report_dir, os.path.join(self.tmp, "reports"))
        self.assertEqual(cfg.days, 45)
        self.assertFalse(cfg.verify_tls)

    def test_environment_defaults(self):
        s = app_mod.load_settings(environ={"MEMTIER_DATA_DIR": self.tmp}, options_file=os.path.join(self.tmp, "none.json"))
        self.assertEqual(s.source, "environment")
        self.assertFalse(s.configured)
        self.assertEqual(s.missing(), ["servers", "username", "password"])
        self.assertEqual(s.support_contact, app_mod.SUPPORT_DEFAULT)
        self.assertEqual(s.threshold_pct, 50.0)
        self.assertEqual(s.port, 8080)

    def test_environment_values_and_errors(self):
        env = {"MEMTIER_DATA_DIR": self.tmp, "MEMTIER_SERVERS": "vc1, vc2,", "MEMTIER_USERNAME": "u",
               "MEMTIER_PASSWORD_FILE": self.options({}), "MEMTIER_THRESHOLD_PCT": "abc",
               "MEMTIER_EXCLUDE_VM_PATTERN": "([", "MEMTIER_LANGUAGE": "fr", "MEMTIER_VERIFY_TLS": "maybe"}
        s = app_mod.load_settings(environ=env, options_file=os.path.join(self.tmp, "none.json"))
        self.assertEqual(s.servers, ["vc1", "vc2"])
        self.assertEqual(s.password, "{}")
        joined = "\n".join(s.errors)
        for name in ("threshold_pct", "exclude_vm_pattern", "language", "verify_tls"):
            self.assertIn(name, joined)
        self.assertFalse(s.configured)

    def test_window(self):
        self.assertEqual(app_mod.window_minutes(None, 1000), 60)
        self.assertEqual(app_mod.window_minutes(0, 3600), 60)
        self.assertEqual(app_mod.window_minutes(0, 7200), 60)  # hosts keep one hour only
        self.assertEqual(app_mod.window_minutes(0, 20 * 60 + 10), 20)
        self.assertEqual(app_mod.window_minutes(0, 60), app_mod.MIN_WINDOW_MINUTES)
        # A first run must not ask for more than the interval, or windows overlap.
        self.assertEqual(app_mod.window_minutes(None, 1000, 15), 15)

    def test_interval(self):
        env = {"MEMTIER_DATA_DIR": self.tmp, "MEMTIER_SERVERS": "vc", "MEMTIER_USERNAME": "u",
               "MEMTIER_PASSWORD": "p", "MEMTIER_INTERVAL_MINUTES": "15", "MEMTIER_CANDIDATE_PCT": "35",
               "MEMTIER_TIER_RATIO": "1.5", "MEMTIER_LANGUAGE": "de"}
        none = os.path.join(self.tmp, "none.json")
        s = app_mod.load_settings(environ=env, options_file=none)
        self.assertEqual(s.errors, [])
        cfg = app_mod.memtier_config(s)
        self.assertEqual((cfg.interval_minutes, cfg.window_minutes), (15, 15))
        self.assertEqual((cfg.candidate_pct, cfg.tier_ratio, cfg.language), (35.0, 1.5, "de"))
        app = app_mod.App(s)
        app.last_started = 1000
        self.assertEqual(app.next_run(), 1000 + 15 * 60)

        bad = app_mod.load_settings(environ=dict(env, MEMTIER_INTERVAL_MINUTES="7"), options_file=none)
        self.assertIn("interval_minutes", "\n".join(bad.errors))
        self.assertEqual(bad.interval_minutes, 60)


class MockDataTest(unittest.TestCase):
    """Runs the web interface against generated mock data."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        subprocess.check_call([sys.executable, os.path.join(ROOT, "examples", "generate-mock-data.py"),
                               os.path.join(cls.tmp, "data"), "--days", "3"], stdout=subprocess.DEVNULL)
        env = {"MEMTIER_DATA_DIR": cls.tmp, "MEMTIER_UI_PASSWORD": "pw"}
        cls.settings = app_mod.load_settings(environ=env, options_file=os.path.join(cls.tmp, "none.json"))
        cls.app = app_mod.App(cls.settings)
        cls.app.rebuild_report()
        handler = app_mod.make_handler(cls.app, os.path.join(ROOT, "container", "ui", "index.html"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp)

    def request(self, method, path, auth=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if auth:
            h["Authorization"] = "Basic " + base64.b64encode(b"admin:pw").decode()
        conn.request(method, path, headers=h)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def test_latest_collection(self):
        latest = self.app.latest
        self.assertEqual(latest["status"], "ok")
        self.assertEqual(latest["hosts"], 19)
        self.assertEqual(len(latest["vcenters"]), 2)
        self.assertGreater(latest["peak"]["pct"], 0)

    def test_auth_required_except_health(self):
        resp, _ = self.request("GET", "/api/status", auth=False)
        self.assertEqual(resp.status, 401)
        resp, _ = self.request("GET", "/healthz", auth=False)
        self.assertEqual(resp.status, 503)  # scheduler not started in this test

    def test_pages(self):
        resp, body = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"api/status", body)
        self.assertEqual(resp.getheader("Content-Security-Policy"), "frame-ancestors 'self'")
        resp, body = self.request("GET", "/report")
        self.assertEqual(resp.status, 200)
        self.assertNotIn(mt.DATA_PLACEHOLDER.encode(), body)
        status = json.loads(self.request("GET", "/api/status")[1])
        self.assertEqual(status["status"], "unconfigured")
        self.assertIsNotNone(status["report"])
        self.assertNotIn("password", status["config"])
        runs = json.loads(self.request("GET", "/api/runs")[1])["runs"]
        self.assertTrue(runs and runs[0]["timestamp"] >= runs[-1]["timestamp"])

    def test_files(self):
        files = json.loads(self.request("GET", "/api/files")[1])["files"]
        self.assertTrue(files)
        resp, body = self.request("GET", "/files/" + files[0]["name"])
        self.assertEqual(resp.status, 200)
        for bad in ("/files/app-state.json", "/files/..%2Fapp-state.json", "/files/../app-state.json"):
            self.assertEqual(self.request("GET", bad)[0].status, 404)

    def test_collect_requires_header_and_config(self):
        self.assertEqual(self.request("POST", "/api/collect")[0].status, 403)
        resp, body = self.request("POST", "/api/collect", headers={"X-MemTier": "1"})
        self.assertEqual(resp.status, 409)
        self.assertEqual(json.loads(body)["reason"], "not_configured")


class CollectTest(unittest.TestCase):
    """A collection against an unreachable vCenter must record a failed run, not crash the app."""

    def test_failed_run_is_recorded(self):
        tmp = tempfile.mkdtemp()
        try:
            env = {"MEMTIER_DATA_DIR": tmp, "MEMTIER_SERVERS": "127.0.0.1:9", "MEMTIER_USERNAME": "u",
                   "MEMTIER_PASSWORD": "p", "MEMTIER_TIMEOUT_SECONDS": "5"}
            settings = app_mod.load_settings(environ=env, options_file=os.path.join(tmp, "none.json"))
            app = app_mod.App(settings)
            started, reason = app.try_start("manual")
            self.assertTrue(started, reason)
            self.assertEqual(app.try_start("manual"), (False, "running"))
            deadline = time.time() + 30
            while app.running and time.time() < deadline:
                time.sleep(0.1)
            self.assertFalse(app.running)
            self.assertEqual(app.last_exit, 1)
            self.assertEqual(app.latest["status"], "failed")
            self.assertEqual(app.status_word, "failed")
            # The next manual run waits for the minimum window, and the start time survives a restart.
            self.assertEqual(app.try_start("manual"), (False, "too_soon"))
            self.assertEqual(app_mod.App(settings).last_started, app.last_started)
        finally:
            shutil.rmtree(tmp)


class EntityTest(unittest.TestCase):
    def test_states(self):
        pub = app_mod.EntityPublisher("token", "memtier")
        states = dict((e, (s, a)) for e, s, a in pub.states("unconfigured", None))
        self.assertEqual(states["sensor.memtier_status"][0], "unconfigured")
        self.assertEqual(states["sensor.memtier_last_collection"][1]["device_class"], "timestamp")
        latest = {"timestamp": "2026-09-15T10:05:00Z", "hosts": 3, "hostsConnected": 2, "vmsOn": 7, "thresholdPct": 50,
                  "activeOverConsumedPct": 28.0, "coldInDramMB": 4096,
                  "peak": {"pct": 41.5, "host": "esx1", "cluster": "C", "vcenter": "vc"},
                  "vcenters": [{"vcenter": "vc", "message": "perf query failed"}]}
        states = dict((e, (s, a)) for e, s, a in pub.states("partial", latest))
        self.assertEqual(states["sensor.memtier_hosts"][0], 2)
        self.assertEqual(states["sensor.memtier_peak_host_active"][0], 41.5)
        self.assertEqual(states["sensor.memtier_active_of_consumed"][0], 28.0)
        self.assertEqual(states["sensor.memtier_cold_in_dram"][0], 4096)
        self.assertIn("perf query failed", states["sensor.memtier_status"][1]["message"])

    def test_zero_is_a_reading_not_unknown(self):
        pub = app_mod.EntityPublisher("token", "memtier")
        latest = {"timestamp": "2026-09-15T10:05:00Z", "hostsConnected": 0, "vmsOn": 0,
                  "activeOverConsumedPct": 0.0, "coldInDramMB": 0, "vcenters": []}
        states = dict((e, s) for e, s, a in pub.states("ok", latest))
        self.assertEqual(states["sensor.memtier_cold_in_dram"], 0)
        self.assertEqual(states["sensor.memtier_active_of_consumed"], 0.0)
        self.assertEqual(states["sensor.memtier_hosts"], 0)
        states = dict((e, s) for e, s, a in pub.states("ok", {"coldInDramMB": None}))
        self.assertEqual(states["sensor.memtier_cold_in_dram"], "unknown")


if __name__ == "__main__":
    unittest.main()
