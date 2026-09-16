"""Tests for the report data the builders produce and for the template that reads it.

The PowerShell and Python editions must emit the same JSON shape, and the template must be
able to look up every string it asks for. Standard library only:

    python3 -m unittest discover -s tests
"""

import configparser
import datetime as _dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "python"))
import memtier as mt  # noqa: E402

TEMPLATE = os.path.join(ROOT, "template", "memtier-report.template.html")


def config(tmp, **report):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_dict({"vcenter": {}, "collector": {"data_dir": "data"},
                      "report": dict({"report_dir": "reports"}, **report)})
    return mt.Config.from_parser(parser, tmp)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_window_follows_the_interval(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_dict({"collector": {"interval_minutes": "15"}})
        cfg = mt.Config.from_parser(parser, self.tmp)
        self.assertEqual(cfg.interval_minutes, 15)
        self.assertEqual(cfg.window_minutes, 15)   # an unset window follows the schedule

    def test_explicit_window_wins_and_is_capped(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_dict({"collector": {"interval_minutes": "15", "window_minutes": "90"}})
        cfg = mt.Config.from_parser(parser, self.tmp)
        self.assertEqual(cfg.window_minutes, 60)   # hosts keep one hour of real-time data

    def test_unsupported_interval_falls_back(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_dict({"collector": {"interval_minutes": "7"}})
        self.assertEqual(mt.Config.from_parser(parser, self.tmp).interval_minutes, 60)

    def test_report_defaults(self):
        cfg = config(self.tmp)
        self.assertEqual(cfg.candidate_pct, 40.0)
        self.assertEqual(cfg.threshold_pct, 50.0)
        self.assertEqual(cfg.tier_ratio, 1.0)
        self.assertEqual(cfg.language, "en")

    def test_unsupported_language_falls_back(self):
        self.assertEqual(config(self.tmp, language="fr").language, "en")


class ReportDataTest(unittest.TestCase):
    """Two hosts in one cluster: one plain, one with an NVMe tier already in use."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        data = os.path.join(cls.tmp, "data")
        os.makedirs(data)
        now = _dt.datetime(2026, 9, 15, 12, 5, 0)
        hosts, vms = [], []
        for hour in range(4):
            ts = mt.iso(now - _dt.timedelta(hours=hour))
            start = mt.iso(now - _dt.timedelta(hours=hour + 1))
            common = {"Timestamp": ts, "WindowStart": start, "VCenter": "vc", "Cluster": "C",
                      "ConnectionState": "connected", "MaintenanceMode": "false", "VMsOn": 2, "Samples": 180,
                      "BalloonMaxMB": 0, "SwapUsedMaxMB": 0}
            # plain host: 100 GB DRAM, 60 GB consumed, 12 GB active
            hosts.append(dict(common, VMHost="esx1", HostId="h1", TieringType="", PhysicalMB=102400,
                              DramMB=102400, NvmeTierMB=0, AssignedMB=81920, ActiveAvgMB=12288,
                              ActiveP95MB=13312, ActiveMaxMB=14336, ConsumedAvgMB=61440, ConsumedMaxMB=62464))
            # tiered host: 50 GB DRAM + 50 GB NVMe, 80 GB consumed -> 30 GB already on NVMe
            hosts.append(dict(common, VMHost="esx2", HostId="h2", TieringType="softwareTiering", PhysicalMB=102400,
                              DramMB=51200, NvmeTierMB=51200, AssignedMB=92160, ActiveAvgMB=10240,
                              ActiveP95MB=11264, ActiveMaxMB=12288, ConsumedAvgMB=81920, ConsumedMaxMB=81920))
            vms.append({"Timestamp": ts, "WindowStart": start, "VCenter": "vc", "Cluster": "C", "VMHost": "esx1",
                        "VM": "vm1", "VMId": "v1", "AssignedMB": 16384, "ReservationMB": 0,
                        "LatencySensitivity": "normal", "Samples": 180, "ActiveAvgMB": 4096, "ActiveP95MB": 4608,
                        "ActiveMaxMB": 5120, "ConsumedAvgMB": 12288, "ConsumedMaxMB": 12288,
                        "BalloonMaxMB": 0, "SwappedMaxMB": 0})
        mt.append_csv(os.path.join(data, "host-2026-09.csv"), mt.HOST_FIELDS, hosts)
        mt.append_csv(os.path.join(data, "vm-2026-09.csv"), mt.VM_FIELDS, vms)
        cls.data = mt.build_report_data(config(cls.tmp), now, 30, "test")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_meta_carries_the_decision_thresholds(self):
        meta = self.data["meta"]
        self.assertEqual(self.data["schema"], 4)
        for key in ("candidatePct", "thresholdPct", "tierRatio", "intervalMinutes", "lang",
                    "ramBoundPct", "cpuIdlePct"):
            self.assertIn(key, meta)
        self.assertEqual(meta["candidatePct"], 40)
        self.assertEqual(meta["intervalMinutes"], 60)

    def test_host_series_carries_the_nvme_tier_size(self):
        by_name = dict((h["name"], h) for h in self.data["hosts"])
        self.assertEqual(len(by_name["esx1"]["s"][0]), 17)
        self.assertEqual(by_name["esx1"]["s"][0][11], 0)
        self.assertEqual(by_name["esx2"]["s"][0][11], 51200)   # per bucket, not only per host

    def test_measured_tier_split_is_optional(self):
        """Before vSphere 9 there is no per-tier counter, so those columns stay null."""
        by_name = dict((h["name"], h) for h in self.data["hosts"])
        for name in ("esx1", "esx2"):
            self.assertIsNone(by_name[name]["s"][0][15])
            self.assertIsNone(by_name[name]["s"][0][16])

    def test_vm_days_are_weighted_by_minutes(self):
        day = [d for d in self.data["vms"][0]["d"] if d][0]
        # Four rows of 180 samples x 20 s = 4 x 60 minutes. The weight follows the samples, so
        # four 15-minute runs of one hour weigh the same as one hourly run - counting rows did not.
        self.assertEqual(day[0], 240)

    def test_minute_weight_is_independent_of_the_interval(self):
        tmp = tempfile.mkdtemp()
        try:
            data = os.path.join(tmp, "data")
            os.makedirs(data)
            now = _dt.datetime(2026, 9, 15, 12, 5, 0)
            rows = []
            for quarter in range(4):   # four 15-minute runs covering the same hour
                ts = now - _dt.timedelta(minutes=15 * quarter)
                rows.append({"Timestamp": mt.iso(ts), "WindowStart": mt.iso(ts - _dt.timedelta(minutes=15)),
                             "VCenter": "vc", "Cluster": "C", "VMHost": "esx1", "VM": "vm1", "VMId": "v1",
                             "AssignedMB": 16384, "ReservationMB": 0, "LatencySensitivity": "normal",
                             "Samples": 45, "ActiveAvgMB": 4096, "ActiveP95MB": 4608, "ActiveMaxMB": 5120,
                             "ConsumedAvgMB": 12288, "ConsumedMaxMB": 12288, "BalloonMaxMB": 0, "SwappedMaxMB": 0})
            mt.append_csv(os.path.join(data, "vm-2026-09.csv"), mt.VM_FIELDS, rows)
            built = mt.build_report_data(config(tmp), now, 30, "test")
            day = [d for d in built["vms"][0]["d"] if d][0]
            self.assertEqual(day[0], 60)   # 4 x 45 samples x 20 s = one hour
        finally:
            shutil.rmtree(tmp)

    def test_json_is_script_safe(self):
        text = mt.script_safe_json(self.data)
        for ch in ("<", ">", "&"):
            self.assertNotIn(ch, text)
        json.loads(text)


class TemplateTest(unittest.TestCase):
    """Every string the report asks for has to exist in English and in every translation."""

    @classmethod
    def setUpClass(cls):
        with io.open(TEMPLATE, encoding="utf-8") as handle:
            cls.html = handle.read()
        cls.js = re.findall(r"<script>(.*?)</script>", cls.html, re.S)[-1]

    @staticmethod
    def strip_strings(text):
        """Blank out double-quoted literals so a colon inside a sentence is not read as a key."""
        return re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', text)

    def dictionaries(self):
        """Parse the I18N literal well enough to list the keys of each language."""
        opening = self.js.index("{", self.js.index("var I18N = {"))
        depth, end = 0, opening
        for i in range(opening, len(self.js)):
            if self.js[i] == "{":
                depth += 1
            elif self.js[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        body = self.strip_strings(self.js[opening + 1:end])
        out = {}
        for lang in re.finditer(r"(?m)^    ([a-z]{2}): \{$", body):
            block = []
            for line in body[lang.end():].split("\n"):
                if re.match(r"^    \}", line):
                    break
                block.append(line)
            out[lang.group(1)] = set(re.findall(r"(?:^|[{,]|\s)\s*([A-Za-z][A-Za-z0-9_]*):\s",
                                                "\n".join(block), re.M))
        return out

    def test_every_key_used_exists_in_every_language(self):
        used = set(re.findall(r'\bt\("([A-Za-z0-9_]+)"', self.js))
        used |= set(re.findall(r'data-i18n="([A-Za-z0-9_]+)"', self.html))
        dicts = self.dictionaries()
        self.assertIn("en", dicts)
        self.assertGreater(len(dicts), 1, "expected at least one translation besides English")
        missing = sorted(used - dicts["en"])
        self.assertEqual(missing, [], "keys used but not defined in English: %s" % missing)
        for lang, keys in dicts.items():
            if lang == "en":
                continue
            self.assertEqual(sorted(dicts["en"] - keys), [], "%s is missing keys" % lang)
            self.assertEqual(sorted(keys - dicts["en"]), [], "%s defines unknown keys" % lang)

    def test_data_placeholder_is_present_exactly_once(self):
        self.assertEqual(self.html.count(mt.DATA_PLACEHOLDER), 1)

    def test_no_external_resources(self):
        """Nothing may be fetched at view time: the report is opened offline and from e-mail."""
        refs = re.findall(r'(?:src|href)="([^"]+)"', self.html)
        remote = [r for r in refs if re.match(r"(?:https?:)?//", r) and "dominik.st" not in r]
        self.assertEqual(remote, [], "the report must stay self-contained: %s" % remote)
        self.assertNotIn("<link rel=\"stylesheet\"", self.html)


class RenderTest(unittest.TestCase):
    """Runs the report's own JavaScript against a DOM shim.

    Not a browser - it proves the render pass completes and the figures are sane, not that
    anything is laid out correctly. Skipped where node is unavailable.
    """

    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node is not installed")
        cls.tmp = tempfile.mkdtemp()
        data = os.path.join(cls.tmp, "data")
        os.makedirs(data)
        # Two hosts that already run a tier, with a very cold working set.
        now = _dt.datetime(2026, 9, 15, 12, 5, 0)
        rows, runs = [], []
        for quarter in range(8):
            ts = now - _dt.timedelta(minutes=15 * quarter)
            for n in (1, 2):
                rows.append({
                    "Timestamp": mt.iso(ts), "WindowStart": mt.iso(ts - _dt.timedelta(minutes=15)),
                    "VCenter": "vc", "Cluster": "C", "VMHost": "esx%d" % n, "HostId": "h%d" % n,
                    "ConnectionState": "connected", "MaintenanceMode": "false", "TieringType": "softwareTiering",
                    "PhysicalMB": 196608, "DramMB": 98304, "NvmeTierMB": 98304, "VMsOn": 10, "AssignedMB": 90000,
                    "Samples": 45, "ActiveAvgMB": 7700, "ActiveP95MB": 8316, "ActiveMaxMB": 9240,
                    "ConsumedAvgMB": 76000, "ConsumedMaxMB": 77520, "BalloonMaxMB": 0, "SwapUsedMaxMB": 0})
            runs.append({"Timestamp": mt.iso(ts), "VCenter": "vc", "Status": "ok", "Hosts": 2, "HostsConnected": 2,
                         "VMsTotal": 24, "VMsOn": 20, "VMsOff": 4, "VMsSuspended": 0, "Templates": 2,
                         "VMsExcluded": 0, "VMsWithoutStats": 0, "HostsWithoutStats": 0, "DurationSec": 4, "Message": ""})
        mt.append_csv(os.path.join(data, "host-2026-09.csv"), mt.HOST_FIELDS, rows)
        mt.append_csv(os.path.join(data, "run-2026-09.csv"), mt.RUN_FIELDS, runs)
        cls.report = os.path.join(cls.tmp, "report.html")
        cfg = config(cls.tmp)
        with io.open(TEMPLATE, encoding="utf-8") as handle:
            template = handle.read()
        payload = mt.script_safe_json(mt.build_report_data(cfg, now, 30, "test"))
        with io.open(cls.report, "w", encoding="utf-8") as handle:
            handle.write(template.replace(mt.DATA_PLACEHOLDER, payload, 1))

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "tmp"):
            shutil.rmtree(cls.tmp)

    def render(self, mode="simple", tier="100", lang="en"):
        out = subprocess.run(
            ["node", os.path.join(ROOT, "tests", "run_report.js"), self.report, mode, tier, lang],
            capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, "render failed (%s/%s/%s):\n%s" % (mode, tier, lang, out.stderr))
        return json.loads(out.stdout)

    def test_every_mode_language_and_ratio_renders(self):
        for mode in ("simple", "expert"):
            for tier in ("50", "100", "200", "400"):
                for lang in ("en", "de"):
                    r = self.render(mode, tier, lang)
                    self.assertTrue(r["kpis"], "no figures for %s/%s/%s" % (mode, tier, lang))
                    joined = " ".join(r["kpis"]) + r["verdict"]
                    for bad in ("NaN", "undefined", "[object"):
                        self.assertNotIn(bad, joined, "%s in %s/%s/%s" % (bad, mode, tier, lang))

    def test_simple_mode_hides_the_detail(self):
        simple, expert = self.render("simple"), self.render("expert")
        self.assertGreater(simple["advTotal"], 0)
        self.assertEqual(simple["advHidden"], simple["advTotal"])
        self.assertEqual(expert["advHidden"], 0)
        self.assertLess(len(simple["kpis"]), len(expert["kpis"]))
        self.assertLess(len(simple["sizing"][0]), len(expert["sizing"][0]))

    def test_no_javascript_reaches_for_a_removed_element(self):
        """Every $("id") must exist in the markup.

        Removing a section and leaving a reference behind returns null in the browser, and the
        next property access throws - which silently blanks the whole report, because the
        failure happens before the first render. Cheap to check, expensive to miss.
        """
        with io.open(TEMPLATE, encoding="utf-8") as handle:
            html = handle.read()
        js = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
        used = set(re.findall(r'\$\("([A-Za-z0-9_]+)"\)', js))
        present = set(re.findall(r'\bid="([A-Za-z0-9_]+)"', html))
        dangling = sorted(used - present)
        self.assertEqual(dangling, [], "JavaScript references elements that do not exist: %s" % dangling)

    def test_verdict_names_the_decision_metric(self):
        r = self.render("simple")
        # active 7700 of consumed 76000 = 10.1%, so ~90% of what the hosts back is cold
        self.assertIn("Strong candidate", r["verdict"])
        self.assertIn("10.1%", r["verdict"])
        self.assertFalse(r["verdictHidden"])

    def test_a_bigger_tier_only_moves_the_sizing(self):
        def figures(tier):
            """The one data row, keyed by its full column heading."""
            rows = self.render("simple", tier)["sizing"]
            self.assertEqual(len(rows), 2, "expected one cluster row")
            out = {}
            for head, cell in zip(rows[0][1:], rows[1][1:]):
                try:
                    out[head] = float(cell.replace(",", ""))
                except ValueError:
                    out[head] = cell          # e.g. the DIMM population, "8 x 16 GB"
            return out
        small, big = figures("50"), figures("400")
        needed = [k for k in small if k.startswith("DRAM needed")][0]
        saved = [k for k in small if k.startswith("DRAM saved")][0]
        extra = [k for k in small if k.startswith("Extra")][0]
        # A bigger tier backs more memory per GB of DRAM: less DRAM needed, more saved, more capacity.
        self.assertLess(big[needed], small[needed])
        self.assertGreater(big[saved], small[saved])
        self.assertGreater(big[extra], small[extra])

    def test_the_verdict_does_not_depend_on_the_tier_size(self):
        """Active over consumed memory is the decision, and no ratio can change it."""
        verdicts = [self.render("simple", tier) for tier in ("50", "100", "200", "400")]
        for r in verdicts:
            self.assertIn("Strong candidate", r["verdict"])
            self.assertIn("10.1%", r["verdict"])
        decision = [k for k in verdicts[0]["kpis"] if k.startswith("Active vs. consumed")]
        self.assertEqual(len(decision), 1)
        for r in verdicts[1:]:
            self.assertEqual([k for k in r["kpis"] if k.startswith("Active vs. consumed")], decision)


if __name__ == "__main__":
    unittest.main()
