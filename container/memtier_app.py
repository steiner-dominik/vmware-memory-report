#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VMware memory tiering report: container edition (Home Assistant app and standalone Docker)

Wraps python/memtier.py - it does not change how data is collected or reported:
  * runs the collector every hour and rebuilds the report after every run
  * serves the report and a small status page (Report, Status, Runs, Data, Settings)
  * under Home Assistant: reads /data/options.json, accepts ingress traffic only and
    publishes a few health entities to the Core API with the Supervisor token

Configuration:
  Home Assistant  /data/options.json (written by the Supervisor)
  standalone      MEMTIER_<OPTION> environment variables, e.g. MEMTIER_SERVERS

Commands:
  serve        run the scheduler and the web interface (default)
  healthcheck  exit 0 when the running instance answers /healthz (Docker HEALTHCHECK)
  version      print the version
  cli ...      run memtier.py directly, e.g. "cli report --config /data/memtier.ini"

Standard library only, like memtier.py.
"""

import argparse
import collections
import configparser
import datetime as _dt
import hmac
import http.client
import io
import json
import logging
import math
import os
import re
import shutil
import signal
import sys
import threading
import time
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
for candidate in (os.path.join(HERE, "..", "python"), os.path.join(HERE, "python")):
    if os.path.isfile(os.path.join(candidate, "memtier.py")):
        sys.path.insert(0, os.path.abspath(candidate))
        break
import memtier as mt  # noqa: E402

APP_VERSION = os.environ.get("MEMTIER_APP_VERSION", "dev")
LOG = logging.getLogger("memtier.app")

OPTIONS_FILE = "/data/options.json"
# The Supervisor proxies ingress traffic from this address; nothing else may reach the app under HA.
INGRESS_PEERS = ("172.30.32.2", "127.0.0.1", "::1")
SUPPORT_DEFAULT = "https://github.com/steiner-dominik/vmware-memory-report/issues"

# Hosts keep about one hour of 20-second samples, so a run may never cover more than that and each
# run covers the time since the previous one. The schedule follows the interval_minutes option.
# A manual run is allowed once the window has reached this many minutes.
MIN_WINDOW_MINUTES = 5
ENTITY_REFRESH_SECONDS = 600

# option name -> default. The type of the default decides how a value is parsed.
# Standalone: environment variable MEMTIER_<NAME>, lists comma separated.
DEFAULTS = collections.OrderedDict([
    ("servers", []),
    ("username", ""),
    ("password", ""),
    ("verify_tls", True),
    ("ca_file", ""),
    ("api_release", ""),
    ("timeout_seconds", 120),
    ("interval_minutes", 60),
    ("exclude_vm_pattern", "^vCLS-"),
    ("batch_size", 50),
    ("retention_months", 13),
    ("compress_old_months", True),
    ("report_days", 30),
    ("candidate_pct", 40.0),
    ("threshold_pct", 50.0),
    ("tier_ratio", 1.0),
    ("cold_pct", 40.0),
    ("hot_pct", 75.0),
    ("stretched_cluster", False),
    ("stretched_clusters", []),
    ("title", "VMware Memory Tiering Report"),
    ("support_contact", SUPPORT_DEFAULT),
    ("publish_entities", True),
    ("entity_prefix", "memtier"),
    ("language", "en"),
    ("log_level", "info"),
])
LANGUAGES = ("en", "de")
LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "warn": logging.WARNING,
              "error": logging.ERROR}
DATA_FILE_RE = re.compile(r"^(host|vm|run)-\d{4}-\d{2}\.csv(\.gz)?$")


def iso_epoch(value):
    if value is None:
        return None
    return _dt.datetime.fromtimestamp(value, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------
# settings
# ----------------------------------------------------------------------------

def parse_value(name, raw, default):
    """Parse an option to the type of its default; raises ValueError with a readable message."""
    if raw is None:
        return default
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False if text else default
        raise ValueError("%s: expected true or false, got %r" % (name, raw))
    if isinstance(default, list):
        items = raw if isinstance(raw, list) else str(raw).split(",")
        return [str(i).strip() for i in items if i is not None and str(i).strip()]
    if isinstance(default, int):
        if str(raw).strip() == "":
            return default
        try:
            return int(str(raw).strip())
        except ValueError:
            raise ValueError("%s: expected a whole number, got %r" % (name, raw))
    if isinstance(default, float):
        if str(raw).strip() == "":
            return default
        try:
            return float(str(raw).strip())
        except ValueError:
            raise ValueError("%s: expected a number, got %r" % (name, raw))
    return str(raw).strip() if name != "password" else str(raw)


class Settings(object):
    def __init__(self, values, source, data_dir, port=8080, ui_username="", ui_password="", errors=None):
        self.values = values
        self.source = source
        self.data_dir = data_dir
        self.port = port
        self.ui_username = ui_username
        self.ui_password = ui_password
        self.errors = errors or []

    def __getattr__(self, name):
        values = self.__dict__.get("values") or {}
        if name in values:
            return values[name]
        raise AttributeError(name)

    @property
    def home_assistant(self):
        return self.source == "home-assistant"

    @property
    def configured(self):
        return not self.missing() and not self.errors

    def missing(self):
        return [name for name in ("servers", "username", "password") if not self.values.get(name)]

    def public(self):
        """The running configuration without secrets, for the Settings tab."""
        shown = dict((k, v) for k, v in self.values.items() if k != "password")
        shown["password_set"] = bool(self.values.get("password"))
        shown["source"] = self.source
        shown["data_dir"] = self.data_dir
        shown["ui_auth"] = bool(self.ui_password)
        return shown


def load_settings(environ=None, options_file=OPTIONS_FILE):
    environ = os.environ if environ is None else environ
    errors = []
    values = collections.OrderedDict()
    if os.path.isfile(options_file):
        source = "home-assistant"
        data_dir = os.path.dirname(os.path.abspath(options_file))
        try:
            with io.open(options_file, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as exc:
            raise SystemExit("cannot read %s: %s" % (options_file, exc))
        lookup = lambda name: raw.get(name)  # noqa: E731
        port, ui_username, ui_password = 8080, "", ""
    else:
        source = "environment"
        data_dir = environ.get("MEMTIER_DATA_DIR", "").strip() or "/data"
        lookup = lambda name: environ.get("MEMTIER_" + name.upper())  # noqa: E731
        try:
            port = int(environ.get("MEMTIER_PORT", "").strip() or 8080)
        except ValueError:
            errors.append("port: expected a whole number, got %r" % environ.get("MEMTIER_PORT"))
            port = 8080
        ui_username = environ.get("MEMTIER_UI_USERNAME", "").strip() or "admin"
        ui_password = environ.get("MEMTIER_UI_PASSWORD", "")

    for name, default in DEFAULTS.items():
        try:
            values[name] = parse_value(name, lookup(name), default)
        except ValueError as exc:
            errors.append(str(exc))
            values[name] = default

    if source == "environment" and not values["password"] and environ.get("MEMTIER_PASSWORD_FILE"):
        try:
            with io.open(environ["MEMTIER_PASSWORD_FILE"], encoding="utf-8") as handle:
                values["password"] = handle.read().strip("\r\n")
        except OSError as exc:
            errors.append("password file: %s" % exc)

    if values["interval_minutes"] not in mt.INTERVAL_CHOICES:
        errors.append("interval_minutes: expected one of %s, got %r"
                      % (", ".join(str(i) for i in mt.INTERVAL_CHOICES), values["interval_minutes"]))
        values["interval_minutes"] = 60
    if values["language"] not in LANGUAGES:
        errors.append("language: expected one of %s, got %r" % (", ".join(LANGUAGES), values["language"]))
        values["language"] = "en"
    if values["log_level"].lower() not in LOG_LEVELS:
        errors.append("log_level: expected debug, info, warning or error, got %r" % values["log_level"])
        values["log_level"] = "info"
    if not re.match(r"^[a-z0-9_]+$", values["entity_prefix"]):
        errors.append("entity_prefix: use lowercase letters, digits and underscores only, got %r" % values["entity_prefix"])
        values["entity_prefix"] = "memtier"
    if values["exclude_vm_pattern"]:
        try:
            re.compile(values["exclude_vm_pattern"])
        except re.error as exc:
            errors.append("exclude_vm_pattern: invalid regular expression (%s)" % exc)
    if values["ca_file"] and not os.path.isfile(values["ca_file"]):
        errors.append("ca_file: %s does not exist" % values["ca_file"])
    for name, low, high in (("report_days", 1, 400), ("candidate_pct", 1, 100), ("threshold_pct", 1, 100),
                            ("tier_ratio", 0.1, 8), ("cold_pct", 0, 100),
                            ("hot_pct", 0, 100), ("retention_months", 0, 1200), ("timeout_seconds", 5, 3600),
                            ("batch_size", 1, 1000)):
        if not low <= values[name] <= high:
            errors.append("%s: must be between %s and %s, got %s" % (name, low, high, values[name]))
    return Settings(values, source, data_dir, port, ui_username, ui_password, errors)


def memtier_config(settings):
    """Translate the settings into memtier.py's Config, so collection and reporting stay identical."""
    s = settings.values
    flag = lambda v: "true" if v else "false"  # noqa: E731
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_dict({
        "vcenter": {"servers": ",".join(s["servers"]), "username": s["username"], "password": s["password"],
                    "verify_tls": flag(s["verify_tls"]), "ca_file": s["ca_file"], "api_release": s["api_release"],
                    "timeout_seconds": str(s["timeout_seconds"])},
        "collector": {"data_dir": "data", "log_dir": "", "interval_minutes": str(s["interval_minutes"]),
                      "window_minutes": "",
                      "exclude_vm_pattern": s["exclude_vm_pattern"], "batch_size": str(s["batch_size"]),
                      "retention_months": str(s["retention_months"]), "compress_old_months": flag(s["compress_old_months"])},
        "report": {"report_dir": "reports", "days": str(s["report_days"]), "language": s["language"],
                   "candidate_pct": str(s["candidate_pct"]), "threshold_pct": str(s["threshold_pct"]),
                   "tier_ratio": str(s["tier_ratio"]),
                   "stretched_cluster": flag(s["stretched_cluster"]), "cold_pct": str(s["cold_pct"]),
                   "hot_pct": str(s["hot_pct"]), "title": s["title"], "support_contact": s["support_contact"]},
    })
    cfg = mt.Config.from_parser(parser, settings.data_dir)
    # Lists are set directly: a cluster name may contain the comma the ini format splits on.
    cfg.servers = list(s["servers"])
    cfg.stretched_clusters = list(s["stretched_clusters"])
    return cfg


def window_minutes(last_started, now, interval=60):
    """Minutes since the previous run, so consecutive windows neither overlap nor leave a gap.

    Capped at 60: that is all the real-time data a host keeps, so a longer gap cannot be
    recovered and asking for it would only make the query fail.
    """
    if last_started is None:
        return min(60, interval)
    elapsed = int(round((now - last_started) / 60.0))
    return max(MIN_WINDOW_MINUTES, min(60, elapsed))


# ----------------------------------------------------------------------------
# collected data
# ----------------------------------------------------------------------------

def latest_collection(data_dir, threshold_pct):  # noqa: C901
    """Summary of the most recent collection: one run row per vCenter plus the busiest host."""
    runs = mt.data_files(data_dir, "run")
    if not runs:
        return None
    month = max(runs)
    rows = list(mt.read_csv(runs[month]))
    if not rows:
        return None
    stamp = max(r.get("Timestamp") or "" for r in rows)
    latest = [r for r in rows if r.get("Timestamp") == stamp]
    statuses = [r.get("Status") or "failed" for r in latest]
    status = "failed" if "failed" in statuses else ("partial" if "partial" in statuses else "ok")

    peak = None
    active_sum = cons_sum = cold_sum = 0
    hosts = mt.data_files(data_dir, "host")
    if month in hosts:
        for r in mt.read_csv(hosts[month]):
            if r.get("Timestamp") != stamp:
                continue
            dram = mt.as_int(r.get("DramMB")) or mt.as_int(r.get("PhysicalMB"))
            active = mt.as_int(r.get("ActiveP95MB"))
            avg = mt.as_int(r.get("ActiveAvgMB"))
            cons = mt.as_int(r.get("ConsumedAvgMB"))
            if dram and avg is not None and cons:
                active_sum += avg
                cons_sum += cons
                # Cold memory still held in DRAM: on a tiered host the part above DRAM is
                # already on NVMe and must not be counted as a saving again.
                cold_sum += max(0, min(cons, dram) - avg)
            if not dram or active is None:
                continue
            pct = round(active * 100.0 / dram, 1)
            if peak is None or pct > peak["pct"]:
                peak = {"pct": pct, "host": r.get("VMHost"), "cluster": r.get("Cluster"), "vcenter": r.get("VCenter")}

    def total(field):
        return sum(mt.as_int(r.get(field)) or 0 for r in latest)

    return {
        "timestamp": stamp,
        "status": status,
        "hosts": total("Hosts"),
        "hostsConnected": total("HostsConnected"),
        "vmsOn": total("VMsOn"),
        "vmsTotal": total("VMsTotal"),
        "durationSec": total("DurationSec"),
        "peak": peak,
        "activeOverConsumedPct": round(active_sum * 100.0 / cons_sum, 1) if cons_sum else None,
        "coldInDramMB": cold_sum if cons_sum else None,
        "thresholdPct": threshold_pct,
        "vcenters": [{"vcenter": r.get("VCenter"), "status": r.get("Status"), "hosts": mt.as_int(r.get("Hosts")),
                      "hostsConnected": mt.as_int(r.get("HostsConnected")), "vmsOn": mt.as_int(r.get("VMsOn")),
                      "vmsWithoutStats": mt.as_int(r.get("VMsWithoutStats")),
                      "hostsWithoutStats": mt.as_int(r.get("HostsWithoutStats")),
                      "durationSec": mt.as_int(r.get("DurationSec")), "message": r.get("Message") or ""} for r in latest],
    }


def recent_runs(data_dir, limit=200):
    files = mt.data_files(data_dir, "run")
    rows = []
    for month in sorted(files, reverse=True):
        month_rows = list(mt.read_csv(files[month]))
        rows = month_rows + rows
        if len(rows) >= limit:
            break
    rows.sort(key=lambda r: r.get("Timestamp") or "")
    return [{"timestamp": r.get("Timestamp"), "vcenter": r.get("VCenter"), "status": r.get("Status"),
             "hosts": mt.as_int(r.get("Hosts")), "hostsConnected": mt.as_int(r.get("HostsConnected")),
             "vmsOn": mt.as_int(r.get("VMsOn")), "durationSec": mt.as_int(r.get("DurationSec")),
             "message": r.get("Message") or ""} for r in rows[-limit:]][::-1]


def data_file_list(data_dir):
    result = []
    try:
        names = sorted(os.listdir(data_dir), reverse=True)
    except OSError:
        return result
    for name in names:
        if DATA_FILE_RE.match(name):
            st = os.stat(os.path.join(data_dir, name))
            result.append({"name": name, "size": st.st_size, "modified": iso_epoch(st.st_mtime)})
    return result


# ----------------------------------------------------------------------------
# Home Assistant entities
# ----------------------------------------------------------------------------

class EntityPublisher(object):
    """Writes states straight to the Core API through the Supervisor proxy. No MQTT, no template sensors."""

    def __init__(self, token, prefix, host="supervisor", port=80):
        self.token = token
        self.prefix = prefix
        self.host = host
        self.port = port
        self.failing = False

    def states(self, status, latest):
        p = self.prefix
        peak = (latest or {}).get("peak") or {}
        message = "; ".join("%s: %s" % (v["vcenter"], v["message"]) for v in (latest or {}).get("vcenters", []) if v["message"])
        return [
            ("sensor.%s_status" % p, status, {
                "friendly_name": "Memory tiering collector status", "icon": "mdi:memory",
                "message": message, "vcenters": [v["vcenter"] for v in (latest or {}).get("vcenters", [])]}),
            ("sensor.%s_last_collection" % p, (latest or {}).get("timestamp") or "unknown", {
                "friendly_name": "Memory tiering last collection", "device_class": "timestamp", "icon": "mdi:clock-check-outline"}),
            ("sensor.%s_hosts" % p, (latest or {}).get("hostsConnected", "unknown"), {
                "friendly_name": "Memory tiering hosts collected", "unit_of_measurement": "hosts",
                "state_class": "measurement", "icon": "mdi:server", "hosts_total": (latest or {}).get("hosts")}),
            ("sensor.%s_vms" % p, (latest or {}).get("vmsOn", "unknown"), {
                "friendly_name": "Memory tiering VMs powered on", "unit_of_measurement": "VMs",
                "state_class": "measurement", "icon": "mdi:monitor-multiple"}),
            ("sensor.%s_peak_host_active" % p, peak.get("pct", "unknown"), {
                "friendly_name": "Memory tiering busiest host active memory", "unit_of_measurement": "%",
                "state_class": "measurement", "icon": "mdi:memory-arrow-down",
                "host": peak.get("host"), "cluster": peak.get("cluster"), "vcenter": peak.get("vcenter"),
                "threshold_pct": (latest or {}).get("thresholdPct"),
                "description": "P95 active memory of the busiest host in the last collection, as % of its DRAM"}),
            # The metric the tiering decision is actually made on.
            ("sensor.%s_active_of_consumed" % p, (latest or {}).get("activeOverConsumedPct") or "unknown", {
                "friendly_name": "Memory tiering active of consumed memory", "unit_of_measurement": "%",
                "state_class": "measurement", "icon": "mdi:fire",
                "description": "Active over consumed memory across all hosts - at or below the candidate "
                               "threshold most of the memory the hosts back is cold and an NVMe tier can absorb it"}),
            ("sensor.%s_cold_in_dram" % p, (latest or {}).get("coldInDramMB") or "unknown", {
                "friendly_name": "Memory tiering cold memory in DRAM", "unit_of_measurement": "MB",
                "device_class": "data_size", "state_class": "measurement", "icon": "mdi:snowflake",
                "description": "Consumed minus active memory that is still held in DRAM - what an NVMe tier would move"}),
        ]

    def publish(self, status, latest):
        for entity_id, state, attributes in self.states(status, latest):
            body = json.dumps({"state": state, "attributes": attributes})
            try:
                conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
                conn.request("POST", "/core/api/states/%s" % entity_id, body,
                             {"Authorization": "Bearer %s" % self.token, "Content-Type": "application/json"})
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status >= 300:
                    raise OSError("HTTP %d" % resp.status)
            except (OSError, http.client.HTTPException) as exc:
                if not self.failing:
                    LOG.warning("cannot publish Home Assistant entities (%s: %s) - will keep retrying quietly", entity_id, exc)
                self.failing = True
                return False
        if self.failing:
            LOG.info("Home Assistant entities are being published again")
        self.failing = False
        return True


# ----------------------------------------------------------------------------
# application
# ----------------------------------------------------------------------------

class RingHandler(logging.Handler):
    def __init__(self, size=300):
        logging.Handler.__init__(self)
        self.lines = collections.deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:
            pass


class App(object):
    def __init__(self, settings, ring=None, clock=time.time):
        self.settings = settings
        self.ring = ring or RingHandler()
        self.clock = clock
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.running = False
        self.last_trigger = None
        self.last_exit = None
        self.last_finished = None
        self.started_at = clock()
        self.state_file = os.path.join(settings.data_dir, "app-state.json")
        self.last_started = self._read_state().get("last_started")
        self.cfg = memtier_config(settings)
        self.latest = None
        self.publisher = None
        token = os.environ.get("SUPERVISOR_TOKEN")
        if settings.home_assistant and settings.publish_entities and token:
            self.publisher = EntityPublisher(token, settings.entity_prefix)
        self.scheduler_thread = None
        self._refresh_latest()

    # state -----------------------------------------------------------------
    def _read_state(self):
        try:
            with io.open(self.state_file, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def _write_state(self):
        mt.ensure_dir(self.settings.data_dir)
        tmp = self.state_file + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"last_started": self.last_started, "app_version": APP_VERSION}, handle)
        os.replace(tmp, self.state_file)

    def _refresh_latest(self):
        try:
            self.latest = latest_collection(self.cfg.data_dir, self.settings.threshold_pct)
        except Exception as exc:
            LOG.warning("cannot read the latest collection: %s", exc)

    @property
    def status_word(self):
        if self.settings.errors or not self.settings.configured:
            return "unconfigured"
        if self.running:
            return "collecting"
        return (self.latest or {}).get("status") or "waiting"

    def next_run(self):
        if not self.settings.configured:
            return None
        if self.last_started is None:
            return self.started_at
        return self.last_started + self.settings.interval_minutes * 60

    def manual_allowed_at(self):
        if self.last_started is None:
            return self.started_at
        return self.last_started + MIN_WINDOW_MINUTES * 60

    # collection ------------------------------------------------------------
    def try_start(self, trigger):
        """Start a collection in the background; returns (started, reason)."""
        if not self.settings.configured:
            return False, "not_configured"
        with self.lock:
            if self.running:
                return False, "running"
            now = self.clock()
            if trigger == "manual" and now < self.manual_allowed_at():
                return False, "too_soon"
            self.running = True
            window = window_minutes(self.last_started, now, self.settings.interval_minutes)
            # Recorded before the run: a crash mid-run must not make the next window overlap this one.
            self.last_started = now
            self.last_trigger = trigger
            try:
                self._write_state()
            except OSError as exc:
                LOG.error("cannot write %s: %s", self.state_file, exc)
        thread = threading.Thread(target=self._collect, args=(window, trigger), name="collect", daemon=True)
        thread.start()
        return True, "started"

    def _collect(self, window, trigger):
        code = 1
        try:
            LOG.info("collection started (%s, window %d min)", trigger, window)
            self.cfg.window_minutes = window
            code = mt.cmd_collect(self.cfg, argparse.Namespace(no_report=False))
            LOG.info("collection finished: %s", {0: "ok", 1: "failed", 2: "partial"}.get(code, code))
        except mt.MemTierError as exc:
            LOG.error("collection failed: %s", exc)
        except Exception:
            LOG.exception("collection failed unexpectedly")
        finally:
            with self.lock:
                self.running = False
                self.last_exit = code
                self.last_finished = self.clock()
            self._refresh_latest()
            self.publish_entities()

    def rebuild_report(self):
        """Options may have changed since the last run (the app restarts on every change): rebuild once."""
        if not mt.data_files(self.cfg.data_dir, "host"):
            return
        try:
            mt.write_report(self.cfg, self.cfg.days)
        except Exception as exc:
            LOG.error("report rebuild failed: %s", exc)

    def publish_entities(self):
        if self.publisher:
            self.publisher.publish(self.status_word, self.latest)

    def scheduler(self):
        last_entities = 0
        while not self.stop.is_set():
            due = self.next_run()
            now = self.clock()
            if due is not None and now >= due and not self.running:
                self.try_start("schedule")
            if self.publisher and now - last_entities >= ENTITY_REFRESH_SECONDS:
                last_entities = now
                self.publish_entities()
            wait = 30 if due is None else max(1, min(30, due - now))
            self.stop.wait(wait)

    def start(self):
        s = self.settings
        for problem in s.errors:
            LOG.error("configuration: %s", problem)
        if not s.errors and s.missing():
            LOG.warning("not collecting yet: set %s (%s)", ", ".join(s.missing()),
                        "app configuration tab" if s.home_assistant else "MEMTIER_* environment variables")
        elif s.configured:
            nxt = self.next_run()
            LOG.info("collecting from %s as %s, next run %s", ", ".join(s.servers), s.username,
                     "now" if nxt <= self.clock() else iso_epoch(nxt))
            if not s.verify_tls:
                LOG.warning("TLS certificate verification is disabled")
        threading.Thread(target=self.rebuild_report, name="report", daemon=True).start()
        self.scheduler_thread = threading.Thread(target=self.scheduler, name="scheduler", daemon=True)
        self.scheduler_thread.start()

    def healthy(self):
        return self.scheduler_thread is not None and self.scheduler_thread.is_alive()

    # views -----------------------------------------------------------------
    def report_path(self):
        return os.path.join(self.cfg.report_dir, mt.REPORT_NAME)

    def status(self):
        report = self.report_path()
        report_info = None
        if os.path.isfile(report):
            st = os.stat(report)
            report_info = {"updated": iso_epoch(st.st_mtime), "size": st.st_size}
        nxt = self.next_run()
        return {
            "version": APP_VERSION,
            "collectorVersion": mt.VERSION,
            "status": self.status_word,
            "source": self.settings.source,
            "language": self.settings.language,
            "intervalMinutes": self.settings.interval_minutes,
            "configured": self.settings.configured,
            "errors": self.settings.errors,
            "missing": self.settings.missing(),
            "running": self.running,
            "lastStarted": iso_epoch(self.last_started),
            "lastFinished": iso_epoch(self.last_finished),
            "lastExit": self.last_exit,
            "nextRun": iso_epoch(max(nxt, self.clock())) if nxt is not None else None,
            "manualAllowedAt": iso_epoch(self.manual_allowed_at()),
            "latest": self.latest,
            "report": report_info,
            "entities": bool(self.publisher),
            "config": self.settings.public(),
            "log": list(self.ring.lines)[-150:],
        }


# ----------------------------------------------------------------------------
# web interface
# ----------------------------------------------------------------------------

def make_handler(app, ui_file):
    class Handler(BaseHTTPRequestHandler):
        server_version = "memtier/%s" % APP_VERSION
        sys_version = ""

        def log_message(self, fmt, *args):
            pass

        def log_request(self, code="-", size="-"):
            # Failed requests are logged so a browser that got an error can be told apart from one that never asked.
            try:
                status = int(code)
            except (TypeError, ValueError):
                status = 0
            level = logging.WARNING if status >= 500 else (logging.INFO if status >= 400 and status != 401 else logging.DEBUG)
            LOG.log(level, "%s %s -> %s (%.0f ms)", self.command, urlsplit(self.path).path, code,
                    (time.time() - getattr(self, "_t0", time.time())) * 1000)

        # plumbing ----------------------------------------------------------
        def _headers(self, status, content_type, length, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            # Ingress serves the panel from Home Assistant's own origin, so 'self' allows the HA panel
            # and the status page's report tab while refusing every other site.
            self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()

        def _send(self, status, body, content_type, extra=None):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self._headers(status, content_type, len(data), extra)
            if self.command != "HEAD":
                self.wfile.write(data)

        def _send_file(self, path, content_type, extra=None):
            # Streamed: a month of VM data can be tens of megabytes.
            with io.open(path, "rb") as handle:
                self._headers(200, content_type, os.fstat(handle.fileno()).st_size, extra)
                if self.command != "HEAD":
                    shutil.copyfileobj(handle, self.wfile, 256 * 1024)

        def _json(self, status, payload):
            self._send(status, json.dumps(payload), "application/json; charset=utf-8")

        def _allowed(self):
            path = urlsplit(self.path).path
            if path == "/healthz":
                return True
            if app.settings.home_assistant:
                if self.client_address[0] not in INGRESS_PEERS:
                    self._send(403, "Forbidden: use the panel in Home Assistant.\n", "text/plain; charset=utf-8")
                    return False
                return True
            if app.settings.ui_password:
                header = self.headers.get("Authorization") or ""
                ok = False
                if header.startswith("Basic "):
                    try:
                        user, _, password = b64decode(header[6:]).decode("utf-8").partition(":")
                        ok = (hmac.compare_digest(user.encode("utf-8"), app.settings.ui_username.encode("utf-8"))
                              & hmac.compare_digest(password.encode("utf-8"), app.settings.ui_password.encode("utf-8")))
                    except (ValueError, UnicodeDecodeError):
                        ok = False
                if not ok:
                    self._send(401, "Authentication required.\n", "text/plain; charset=utf-8",
                               {"WWW-Authenticate": 'Basic realm="memtier", charset="UTF-8"'})
                    return False
            return True

        # routes ------------------------------------------------------------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            self._t0 = time.time()
            if not self._allowed():
                return
            url = urlsplit(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                with io.open(ui_file, "rb") as handle:
                    return self._send(200, handle.read(), "text/html; charset=utf-8")
            if path == "/healthz":
                if app.healthy():
                    return self._send(200, "ok\n", "text/plain; charset=utf-8")
                return self._send(503, "scheduler stopped\n", "text/plain; charset=utf-8")
            if path == "/api/status":
                return self._json(200, app.status())
            if path == "/api/runs":
                return self._json(200, {"runs": recent_runs(app.cfg.data_dir)})
            if path == "/api/files":
                return self._json(200, {"files": data_file_list(app.cfg.data_dir)})
            if path == "/report":
                return self._report(parse_qs(url.query))
            if path.startswith("/files/"):
                return self._file(path[len("/files/"):])
            return self._send(404, "Not found\n", "text/plain; charset=utf-8")

        def do_POST(self):
            self._t0 = time.time()
            if not self._allowed():
                return
            # A custom header cannot be sent cross-site without a CORS preflight, which is never granted.
            if self.headers.get("X-MemTier") != "1":
                return self._json(403, {"error": "missing_header"})
            if urlsplit(self.path).path == "/api/collect":
                started, reason = app.try_start("manual")
                return self._json(202 if started else 409, {"started": started, "reason": reason, "status": app.status()})
            return self._json(404, {"error": "not_found"})

        def _report(self, query):
            report = app.report_path()
            if not os.path.isfile(report):
                return self._send(404, EMPTY_REPORT, "text/html; charset=utf-8")
            extra = {}
            if query.get("download"):
                extra["Content-Disposition"] = 'attachment; filename="%s"' % mt.REPORT_NAME
            return self._send_file(report, "text/html; charset=utf-8", extra)

        def _file(self, name):
            if not DATA_FILE_RE.match(name):
                return self._send(404, "Not found\n", "text/plain; charset=utf-8")
            path = os.path.join(app.cfg.data_dir, name)
            if not os.path.isfile(path):
                return self._send(404, "Not found\n", "text/plain; charset=utf-8")
            kind = "application/gzip" if name.endswith(".gz") else "text/csv; charset=utf-8"
            return self._send_file(path, kind, {"Content-Disposition": 'attachment; filename="%s"' % name})

    return Handler


EMPTY_REPORT = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font:14px/1.5 system-ui,sans-serif;margin:0;padding:48px 24px;color:#52514e;background:transparent;text-align:center}
@media (prefers-color-scheme:dark){body{color:#c3c2b7}}</style></head>
<body><p>No report yet. It is built after the first collection.</p></body></html>
"""


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def setup_logging(level_name):
    ring = RingHandler()
    mt.setup_logging(None, "app", False)
    mt.LOG.addHandler(ring)
    mt.LOG.setLevel(LOG_LEVELS.get(level_name.lower(), logging.INFO))
    return ring


def cmd_serve(args):
    settings = load_settings()
    ring = setup_logging(settings.log_level)
    LOG.info("VMware memory tiering report %s (collector %s), configuration from %s", APP_VERSION, mt.VERSION,
             "Home Assistant options" if settings.home_assistant else "environment")
    try:
        mt.ensure_dir(settings.data_dir)
        probe = os.path.join(settings.data_dir, ".write-test")
        with open(probe, "w") as handle:
            handle.write("ok")
        os.remove(probe)
    except OSError as exc:
        LOG.error("data directory %s is not writable (%s) - mount a volume the container user can write to", settings.data_dir, exc)
        return 1

    app = App(settings, ring)
    ui_file = os.path.join(HERE, "ui", "index.html")
    server = ThreadingHTTPServer(("", settings.port), make_handler(app, ui_file))
    server.daemon_threads = True

    def shutdown(signum, frame):
        LOG.info("stopping (signal %d)", signum)
        app.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    # PID 1 in a container ignores SIGTERM unless a handler is installed.
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    app.start()
    LOG.info("web interface listening on port %d%s", settings.port,
             " (ingress only)" if settings.home_assistant else (" (basic auth)" if settings.ui_password else ""))
    server.serve_forever(poll_interval=1)
    server.server_close()
    return 0


def cmd_healthcheck(args):
    port = 8080
    if not os.path.isfile(OPTIONS_FILE):
        try:
            port = int(os.environ.get("MEMTIER_PORT", "").strip() or 8080)
        except ValueError:
            pass
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/healthz")
        ok = conn.getresponse().status == 200
        conn.close()
    except (OSError, http.client.HTTPException):
        ok = False
    return 0 if ok else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else "serve"
    if command == "serve":
        return cmd_serve(argv[1:])
    if command == "healthcheck":
        return cmd_healthcheck(argv[1:])
    if command == "version":
        print("%s (collector %s)" % (APP_VERSION, mt.VERSION))
        return 0
    if command == "cli":
        return mt.main(argv[1:])
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
