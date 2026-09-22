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

    def test_invalid_numbers_fall_back_instead_of_breaking_the_report(self):
        # threshold 0 divides by zero in the sizing; a typo must not end in a traceback
        cfg = config(self.tmp, threshold_pct="0", tier_ratio="abc", cpu_idle_pct="150", candidate_pct="35")
        self.assertEqual((cfg.threshold_pct, cfg.tier_ratio, cfg.cpu_idle_pct, cfg.candidate_pct), (50.0, 1.0, 50.0, 35.0))
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_dict({"collector": {"interval_minutes": "15", "window_minutes": "soon"}})
        self.assertEqual(mt.Config.from_parser(parser, self.tmp).window_minutes, 15)


class QueryWindowTest(unittest.TestCase):
    def test_every_query_ends_at_the_run_timestamp(self):
        """startTime is exclusive and endTime inclusive, so consecutive windows tile exactly."""
        client = mt.ViJsonClient("vc", "u", "p")
        client.content = {"perfManager": {"value": "PerfMgr"}}
        sent = []
        client.invoke = lambda *args: sent.append(args[3]) or []
        start, end = _dt.datetime(2026, 9, 15, 10, 0), _dt.datetime(2026, 9, 15, 11, 0)
        mt.perf_batches(client, "HostSystem", ["h1", "h2", "h3"], {"mem.active.average": 1}, start, 2, end=end)
        specs = [spec for body in sent for spec in body["querySpec"]]
        self.assertEqual(len(specs), 3)
        for spec in specs:
            self.assertEqual((spec["startTime"], spec["endTime"]), ("2026-09-15T10:00:00Z", "2026-09-15T11:00:00Z"))


class ConcurrentReportTest(unittest.TestCase):
    def test_two_writers_do_not_share_a_temporary_file(self):
        """The container can rebuild the report while a collection writes it."""
        import threading
        tmp = tempfile.mkdtemp()
        try:
            cfg = config(tmp)
            os.makedirs(cfg.data_dir)
            ts = mt.iso(mt.utcnow() - _dt.timedelta(hours=1))
            mt.append_csv(os.path.join(cfg.data_dir, "run-%s.csv" % ts[:7]), mt.RUN_FIELDS,
                          [{"Timestamp": ts, "VCenter": "vc", "Status": "ok"}])
            errors = []

            def build():
                try:
                    for _ in range(5):
                        mt.write_report(cfg, 30)
                except Exception as exc:  # noqa: BLE001 - any failure is the finding
                    errors.append(exc)
            threads = [threading.Thread(target=build) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            with io.open(os.path.join(cfg.report_dir, mt.REPORT_NAME), encoding="utf-8") as handle:
                self.assertTrue(handle.read().rstrip().endswith("</html>"))
            self.assertEqual([n for n in os.listdir(cfg.report_dir) if n.endswith(".tmp")], [])
        finally:
            shutil.rmtree(tmp)


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
        # Cluster "C": two hosts that already run a tier, with a very cold working set.
        # Cluster "D": two hosts without a tier, memory full and CPU idle - a retrofit case, and
        # the only hosts a smaller DRAM purchase can still save on.
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
                    "ConsumedAvgMB": 76000, "ConsumedMaxMB": 77520, "BalloonMaxMB": 0, "SwapUsedMaxMB": 0,
                    "CpuCores": 16, "CpuThreads": 32, "CpuMhz": 2600, "CpuAvgPct": 30, "CpuP95Pct": 40, "CpuMaxPct": 50})
                rows.append({
                    "Timestamp": mt.iso(ts), "WindowStart": mt.iso(ts - _dt.timedelta(minutes=15)),
                    "VCenter": "vc", "Cluster": "D", "VMHost": "esx-d%d" % n, "HostId": "d%d" % n,
                    "ConnectionState": "connected", "MaintenanceMode": "false", "TieringType": "noTiering",
                    "PhysicalMB": 98304, "DramMB": 98304, "NvmeTierMB": 0, "VMsOn": 10, "AssignedMB": 90000,
                    "Samples": 45, "ActiveAvgMB": 7000, "ActiveP95MB": 7560, "ActiveMaxMB": 8400,
                    "ConsumedAvgMB": 69091, "ConsumedMaxMB": 70473, "BalloonMaxMB": 0, "SwapUsedMaxMB": 0,
                    "CpuCores": 16, "CpuThreads": 32, "CpuMhz": 2600, "CpuAvgPct": 20, "CpuP95Pct": 45, "CpuMaxPct": 60})
            runs.append({"Timestamp": mt.iso(ts), "VCenter": "vc", "Status": "ok", "Hosts": 4, "HostsConnected": 4,
                         "VMsTotal": 44, "VMsOn": 40, "VMsOff": 4, "VMsSuspended": 0, "Templates": 2,
                         "VMsExcluded": 0, "VMsWithoutStats": 0, "HostsWithoutStats": 0, "DurationSec": 4, "Message": ""})
        mt.append_csv(os.path.join(data, "host-2026-09.csv"), mt.HOST_FIELDS, rows)
        mt.append_csv(os.path.join(data, "run-2026-09.csv"), mt.RUN_FIELDS, runs)
        cls.report = os.path.join(cls.tmp, "report.html")
        cfg = config(cls.tmp, support_contact="https://example.com/help")
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
        for mode in ("summary", "simple", "expert"):
            for tier in ("50", "100", "200", "400"):
                for lang in ("en", "de"):
                    r = self.render(mode, tier, lang)
                    self.assertEqual(len(r["cases"]), 2, "no cases for %s/%s/%s" % (mode, tier, lang))
                    joined = " ".join(r["kpis"] + r["cases"]) + r["verdict"] + r["funnel"] + r["confidence"]
                    joined += " ".join(" ".join(row) for row in r["decision"] + r["hostTable"] + r["sizing"])
                    for bad in ("NaN", "undefined", "[object", "Infinity"):
                        self.assertNotIn(bad, joined, "%s in %s/%s/%s" % (bad, mode, tier, lang))

    def test_three_views(self):
        summary, simple, expert = self.render("summary"), self.render("simple"), self.render("expert")
        self.assertGreater(simple["advTotal"], 0)
        self.assertGreater(simple["stdTotal"], 0)
        # Summary: the verdict, the two cases and where the memory goes - nothing else.
        self.assertEqual(summary["stdHidden"], summary["stdTotal"])
        self.assertEqual(summary["advHidden"], summary["advTotal"])
        self.assertEqual(summary["decision"], [])
        # Simple: the decision board, without the expert detail.
        self.assertEqual(simple["stdHidden"], 0)
        self.assertEqual(simple["advHidden"], simple["advTotal"])
        self.assertTrue(simple["decision"])
        self.assertEqual(expert["advHidden"], 0)
        self.assertEqual(expert["stdHidden"], 0)
        self.assertLess(len(simple["kpis"]), len(expert["kpis"]))
        self.assertLess(len(simple["sizing"][0]), len(expert["sizing"][0]))
        self.assertTrue(expert["quality"])

    def test_tiered_hosts_are_left_out_of_the_new_server_saving(self):
        r = self.render("simple")
        new = re.sub(r"\s+", " ", r["cases"][0])
        self.assertIn("Full saving", new)
        self.assertIn("2 host(s) already run an NVMe tier", new)
        verdicts = {row[0].split(" ")[0]: row for row in r["decision"][1:]}
        self.assertIn("Already tiered", " ".join(verdicts["C"]))
        self.assertIn("Full saving", " ".join(verdicts["D"]))
        sized = {row[0]: row for row in r["sizing"][1:]}
        self.assertEqual(sized["C"][1], "0 / 2")
        self.assertEqual(sized["D"][1], "2 / 2")

    def test_retrofit_needs_memory_full_and_idle_cpu(self):
        r = self.render("simple")
        retro = re.sub(r"\s+", " ", r["cases"][1])
        self.assertIn("Tier instead of a host", retro)
        # the tiered hosts are not retrofit candidates and not counted
        self.assertIn("2 of 2 hosts", retro)

    def test_support_contact_is_shown(self):
        self.assertIn("Support: https://example.com/help", self.render("simple")["subtitle"])

    def test_tiering_type_is_translated(self):
        cells = " ".join(" ".join(row) for row in self.render("expert")["hostTable"])
        self.assertNotIn("noTiering", cells)
        self.assertNotIn("softwareTiering", cells)

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
            """The row of the untiered cluster, keyed by its full column heading."""
            rows = self.render("simple", tier)["sizing"]
            row = [r for r in rows[1:] if r[0] == "D"][0]
            out = {}
            for head, cell in zip(rows[0][1:], row[1:]):
                try:
                    out[head] = float(cell.replace(",", ""))
                except ValueError:
                    out[head] = cell          # e.g. the DIMM population, "8 x 16 GB"
            return out
        small, big = figures("50"), figures("400")
        needed = [k for k in small if k.startswith("DRAM needed")][0]
        saved = [k for k in small if k.startswith("DRAM saved") and "measured" not in k][0]
        usable = [k for k in small if k.startswith("Usable")][0]
        # A bigger tier backs more memory per GB of DRAM: less DRAM needed, more saved, more capacity.
        self.assertLess(big[needed], small[needed])
        self.assertGreater(big[saved], small[saved])
        self.assertGreater(big[usable], small[usable])

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


class VerdictLogicTest(unittest.TestCase):
    """Fleets built directly as report data, one per rule the two buying decisions follow."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node is not installed")
        cls.tmp = tempfile.mkdtemp()
        with io.open(TEMPLATE, encoding="utf-8") as handle:
            cls.template = handle.read()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "tmp"):
            shutil.rmtree(cls.tmp)

    @staticmethod
    def host(name, cluster, dram, assigned, active, consumed, cpu):
        """A steady host over 30 days, sizes in GB."""
        t0 = 1780000000 // 3600 * 3600
        gb = 1024
        s = [[t0 + i * 3600, 10, assigned * gb, active * gb, active * gb, active * gb, consumed * gb, 0, 0, dram * gb,
              consumed * gb, 0, cpu, cpu, cpu, None, None] for i in range(30 * 24)]
        return {"key": "vc|" + name, "vc": "vc", "name": name, "cluster": cluster, "tiering": "", "dramMB": dram * gb,
                "nvmeMB": 0, "physMB": dram * gb, "cores": 32, "threads": 64, "mhz": 2500, "s": s}

    def render(self, name, hosts, mode="simple"):
        meta = {"title": "t", "support": "", "generatedUtc": "2026-06-01T00:00:00Z", "fromUtc": "2026-05-01T00:00:00Z",
                "toUtc": "2026-06-01T00:00:00Z", "days": 30, "lang": "en", "candidatePct": 40, "thresholdPct": 50,
                "tierRatio": 1, "coldPct": 40, "hotPct": 75, "ramBoundPct": 70, "cpuIdlePct": 50, "bucketHours": 1,
                "intervalMinutes": 60, "vcenters": ["vc"], "builder": "test",
                "failover": {"stretched": False, "stretchedClusters": []}}
        path = os.path.join(self.tmp, name + ".html")
        payload = mt.script_safe_json({"schema": 4, "meta": meta, "runs": [], "hosts": hosts, "vms": []})
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(self.template.replace(mt.DATA_PLACEHOLDER, payload, 1))
        out = subprocess.run(["node", os.path.join(ROOT, "tests", "run_report.js"), path, mode, "100", "en"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        r = json.loads(out.stdout)
        r["new"], r["retro"] = [re.sub(r"\s+", " ", c) for c in r["cases"]]
        return r

    def test_usable_extra_memory_keeps_the_hot_set_within_the_limit(self):
        # The README example: 384 GB DRAM, 336 consumed, 101 active, 40% CPU. The CPU would allow
        # 336 GB more, but at 30% hot that takes the hot set to 202 GB - past 50% of 384 GB.
        r = self.render("retro", [self.host("r1", "R", 384, 512, 101, 336, 40)])
        self.assertIn("303 GB of usable extra memory", r["retro"])
        usable = r["sizing"][0].index("Usable extra memory GB")
        self.assertEqual(r["sizing"][1][usable], "303")

    def test_standalone_hosts_are_sized_one_by_one(self):
        r = self.render("standalone", [self.host("esx1", "(standalone)", 256, 300, 30, 200, 20),
                                       self.host("esx2", "(standalone)", 256, 300, 30, 200, 20)])
        self.assertIn("Full saving", r["new"])
        self.assertNotIn("No workload", r["new"])
        self.assertEqual(sorted(row[0] for row in r["sizing"][1:]), ["esx1 (standalone)", "esx2 (standalone)"])

    def test_the_badge_follows_the_clusters_that_have_a_saving(self):
        # A big hot cluster pulls the fleet ratio above 40%; the small cold one still saves DRAM.
        hosts = [self.host("h%d" % i, "Hot", 1024, 1200, 500, 700, 30) for i in range(4)]
        hosts += [self.host("c%d" % i, "Cold", 512, 800, 40, 400, 30) for i in range(2)]
        r = self.render("mixed", hosts)
        self.assertIn("Full saving", r["new"])
        self.assertNotIn("Little to gain", r["new"])
        self.assertNotIn("Too much of the memory", r["new"])
        self.assertIn("5.0%", r["new"])      # active P95 / assigned of the cold cluster alone

    def test_a_saving_of_nothing_is_little_to_gain(self):
        # Overcommitted: assigned / 2 is already all the DRAM these hosts have.
        r = self.render("overcommitted", [self.host("o%d" % i, "O", 256, 600, 30, 200, 20) for i in range(2)])
        self.assertIn("Little to gain", r["new"])
        self.assertIn("no more DRAM than they would need", r["new"])
        self.assertNotIn("Full saving", r["new"])

    def test_same_figure_after_dimm_rounding_is_not_blamed_on_the_hot_set(self):
        r = self.render("rounded", [self.host("r1", "R", 384, 512, 101, 336, 40)])
        self.assertIn("rounds up to the same buildable DIMM population", r["new"])
        self.assertNotIn("The hot set sets the DRAM", r["new"])


if __name__ == "__main__":
    unittest.main()
