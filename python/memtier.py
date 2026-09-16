#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VMware NVMe Memory Tiering: collector and trend report (Python edition)

Runs anywhere with Python 3.6+ and no third-party packages. Run it on a separate
management VM or jump host, not on the vCenter Server Appliance. It talks to vCenter
through the VI/JSON API (vCenter 8.0 U1 or later, which every memory-tiering-capable
vCenter is).

Commands:
  setup    create the config file interactively, test the login, optionally install the cron job
  collect  pull the last hour of 20-second real-time memory samples, append hourly
           avg/P95/max per host and VM to monthly CSV files, then rebuild the report
  report   build the self-contained HTML trend report from the CSV files

The CSV format and the HTML template are shared with the PowerShell edition, so
data collected by either edition can be reported by either edition.
"""

from __future__ import print_function

import argparse
import base64
import configparser
import csv
import datetime as _dt
import errno
import getpass
import glob
import gzip
import http.client
import io
import json
import logging
import logging.handlers
import math
import os
import re
import shutil
import socket
import ssl
import stat
import sys
import time
from urllib.parse import quote, unquote

VERSION = "1.1.0"
LOG = logging.getLogger("memtier")

HOST_FIELDS = [
    "Timestamp", "WindowStart", "VCenter", "Cluster", "VMHost", "HostId", "ConnectionState", "MaintenanceMode",
    "TieringType", "PhysicalMB", "DramMB", "NvmeTierMB", "VMsOn", "AssignedMB", "Samples",
    "ActiveAvgMB", "ActiveP95MB", "ActiveMaxMB", "ConsumedAvgMB", "ConsumedMaxMB", "BalloonMaxMB", "SwapUsedMaxMB",
    "CpuCores", "CpuThreads", "CpuMhz", "CpuAvgPct", "CpuP95Pct", "CpuMaxPct",
    "TierDramMB", "TierNvmeMB",
]
VM_FIELDS = [
    "Timestamp", "WindowStart", "VCenter", "Cluster", "VMHost", "VM", "VMId", "AssignedMB", "ReservationMB",
    "LatencySensitivity", "Samples", "ActiveAvgMB", "ActiveP95MB", "ActiveMaxMB", "ConsumedAvgMB", "ConsumedMaxMB",
    "BalloonMaxMB", "SwappedMaxMB",
]
RUN_FIELDS = [
    "Timestamp", "VCenter", "Status", "Hosts", "HostsConnected", "VMsTotal", "VMsOn", "VMsOff", "VMsSuspended",
    "Templates", "VMsExcluded", "VMsWithoutStats", "HostsWithoutStats", "DurationSec", "Message", "TierCounters",
]

HOST_COUNTERS = ["mem.active.average", "mem.consumed.average", "mem.vmmemctl.average", "mem.swapused.average"]
# CPU is collected to answer one question: is a host out of memory while its CPUs idle? That host
# gains capacity from a tier instead of from another socket. Optional on purpose - a vCenter that
# does not publish them must still produce a memory report.
HOST_CPU_COUNTERS = ["cpu.usage.average"]
# How much machine memory each tier actually holds. Published from vSphere 9.0 onwards only
# (8.0 U3 has no counter with "tier" in its name at all), keyed per instance by the tier's name
# as memoryTierInfo reports it - "DRAM", "NVMe". Unit is MB already, and it is a level 2 counter,
# so it arrives with the default statistics settings. Where it is missing the report falls back
# to deriving the split from consumed memory and DRAM size.
HOST_TIER_COUNTERS = ["mem.tier.consumed.latest"]
VM_COUNTERS = ["mem.active.average", "mem.consumed.average", "mem.vmmemctl.average", "mem.swapped.average"]
REALTIME_INTERVAL = 20
# Supported collection intervals in minutes. 60 is plenty for a sizing decision; 15 and 30
# give a finer time resolution and lose less data when a single run fails.
INTERVAL_CHOICES = (15, 30, 60)
LANGUAGES = ("en", "de")
DATA_PLACEHOLDER = "/*__MEMTIER_DATA__*/null"
# Rebuilt and overwritten after every collection; the CSV files keep the full history.
REPORT_NAME = "MemTier_Report.html"

DEFAULT_CONFIG = """\
# VMware memory tiering collector - configuration
# Keep this file readable by its owner only (chmod 600): it contains a password.

[vcenter]
# One or more vCenter FQDNs, comma separated
servers = {servers}
# Use a dedicated read-only SSO account (the built-in Read-only role is sufficient)
username = {username}
# Leave empty to use the MEMTIER_PASSWORD environment variable instead
password = {password}
verify_tls = {verify_tls}
# PEM file with the vCenter/VMCA root certificate (empty = system trust store)
ca_file =
# VI/JSON release, e.g. 8.0.3.0 (empty = detect automatically)
api_release =
timeout_seconds = 120

[collector]
data_dir = {data_dir}
log_dir = {log_dir}
# Collection interval in minutes: 60, 30 or 15. The schedule must match this value.
# Every run reads all 20-second samples of its window, so a shorter interval does not
# find peaks that an hourly run misses - it gives a finer time resolution and loses
# less data when a run fails (hosts keep only about one hour of real-time samples).
interval_minutes = {interval_minutes}
# Minutes of real-time data to read per run (max. 60). Empty = the interval above.
window_minutes =
# VMs whose name matches this regular expression are ignored (empty = none)
exclude_vm_pattern = ^vCLS-
# Entities per performance query; halved automatically if a query fails
batch_size = 50
# Delete CSV files older than this many months (0 = keep forever)
retention_months = 13
# gzip CSV files of completed months (saves ~90% disk space)
compress_old_months = true

[report]
report_dir = {report_dir}
days = 30
# Report language: en or de (every reader can switch it in the report itself)
language = en
# THE decision metric: active memory as % of consumed memory. At or below this share the
# memory that hosts actually back is mostly cold, which is exactly what an NVMe tier absorbs.
candidate_pct = 40
# Feasibility: the hot working set must still fit DRAM. Broadcom's guidance for the default
# 1:1 DRAM:NVMe ratio is to keep host active memory at or below 50% of DRAM.
threshold_pct = 50
# NVMe tier size relative to DRAM used for sizing (1.0 = the supported 1:1 maximum)
tier_ratio = 1.0
# A host is "out of memory but not out of CPU" when consumed memory is at or above
# ram_bound_pct of its DRAM while CPU stays at or below cpu_idle_pct. Those hosts gain
# capacity from an NVMe tier instead of from another socket.
ram_bound_pct = 70
cpu_idle_pct = 50
# Stretched clusters (two sites): capacity after a failure is 50% of the cluster instead of N+1.
# true = all clusters are stretched; or list the stretched clusters by name (comma separated)
stretched_cluster = false
stretched_clusters =
# Per-VM hints (worst-day P95 of active vs. assigned memory)
cold_pct = 40
hot_pct = 75
title = VMware Memory Tiering Report
support_contact = https://github.com/steiner-dominik/vmware-memory-report/issues
# Empty = look next to this script and in ../template
template =
"""


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------

class MemTierError(Exception):
    pass


def utcnow():
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    """Parse 2026-09-15T10:00:00Z / 2026-09-15T10:00:00.123+00:00 into a naive UTC datetime."""
    text = text.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$", text)
    if not m:
        raise ValueError("invalid timestamp: %r" % text)
    value = _dt.datetime(*[int(x) for x in m.groups()[:6]])
    tz = m.group(7)
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        hours, minutes = int(tz[1:3]), int(tz[-2:])
        value -= sign * _dt.timedelta(hours=hours, minutes=minutes)
    return value


def epoch(ts):
    return int((ts - _dt.datetime(1970, 1, 1)).total_seconds())


def round_half_up(value):
    return int(math.floor(value + 0.5))


def round1(value):
    scaled = math.floor(value * 10 + 0.5)
    # PowerShell's "R" format prints an integral double without the ".0"; match it so both
    # builders emit byte-identical JSON.
    return scaled // 10 if scaled % 10 == 0 else scaled / 10.0


def json_num(value):
    """Integral floats serialise as integers, as the PowerShell builder does."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def p95(values):
    """Nearest-rank 95th percentile (identical in the PowerShell edition)."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[int(math.ceil(0.95 * len(ordered))) - 1]


def tier_consumed(series, tier_names):
    """Average machine memory consumed per tier, in MB, from the per-instance tier counter.

    The instance is the tier's name as memoryTierInfo reports it. Anything that is not the DRAM
    tier is the tier being added, whatever it is called. Returns (dram, nvme), each None when the
    counter is absent - which is every vCenter before 9.0.
    """
    dram_names = set(n.lower() for n, kind in tier_names if kind.lower() == "dram")
    totals = {}
    for key, values in series.items():
        name, _, instance = key.partition("|")
        if name not in HOST_TIER_COUNTERS or not instance:
            continue
        valid = [v for v in values if v is not None and v >= 0]
        if not valid:
            continue
        is_dram = instance.lower() in dram_names or (not dram_names and instance.lower() == "dram")
        bucket = "dram" if is_dram else "nvme"
        totals[bucket] = totals.get(bucket, 0) + sum(valid) / float(len(valid))
    if not totals:
        return None, None
    return round_half_up(totals.get("dram", 0)), round_half_up(totals.get("nvme", 0))


def summarize_pct(values):
    """Reduce hundredth-of-a-percent CPU samples to (avg, p95, max) in percent."""
    valid = [v for v in values if v is not None and v >= 0]
    if not valid:
        return None, None, None
    return (round1(sum(valid) / float(len(valid)) / 100.0),
            round1(p95(valid) / 100.0),
            round1(max(valid) / 100.0))


def summarize_kb(values):
    """Reduce KB samples to (avg, p95, max) in MB; negative samples mean 'no data'."""
    valid = [v for v in values if v is not None and v >= 0]
    if not valid:
        return None, None, None, 0
    return (round_half_up(sum(valid) / float(len(valid)) / 1024.0),
            round_half_up(p95(valid) / 1024.0),
            round_half_up(max(valid) / 1024.0),
            len(valid))


def to_bool(text, default=False):
    if text is None or str(text).strip() == "":
        return default
    return str(text).strip().lower() in ("1", "true", "yes", "on")


def month_key(ts):
    return ts.strftime("%Y-%m")


def ensure_dir(path):
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise


def as_float(text):
    if text is None:
        return None
    text = str(text).strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def as_int(text):
    if text is None:
        return None
    text = str(text).strip()
    if text == "":
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return int(math.floor(float(text) + 0.5))
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# configuration
# ----------------------------------------------------------------------------

class Config(object):
    def __init__(self, path):
        self.path = os.path.abspath(path)
        if not os.path.isfile(self.path):
            raise MemTierError("config file not found: %s (run 'memtier.py setup' first)" % self.path)
        parser = configparser.ConfigParser(interpolation=None)
        with io.open(self.path, encoding="utf-8") as handle:
            parser.read_file(handle)
        mode = os.stat(self.path).st_mode
        if (os.name == "posix" and mode & (stat.S_IRWXG | stat.S_IRWXO)
                and parser.get("vcenter", "password", fallback="").strip()):
            LOG.warning("config file %s contains a password and is readable by other users - run: chmod 600 %s", self.path, self.path)
        self._load(parser, os.path.dirname(self.path))

    @classmethod
    def from_parser(cls, parser, base):
        """Build a config without a file, e.g. from container options; relative paths resolve against base."""
        cfg = cls.__new__(cls)
        cfg.path = None
        cfg._load(parser, os.path.abspath(base))
        return cfg

    def _load(self, parser, base):
        self.p = parser

        def path_opt(section, key, default):
            value = parser.get(section, key, fallback="").strip() or default
            return value if os.path.isabs(value) else os.path.normpath(os.path.join(base, value))

        self.servers = [s.strip() for s in parser.get("vcenter", "servers", fallback="").split(",") if s.strip()]
        self.username = parser.get("vcenter", "username", fallback="").strip()
        self.password = parser.get("vcenter", "password", fallback="") or os.environ.get("MEMTIER_PASSWORD", "")
        self.verify_tls = to_bool(parser.get("vcenter", "verify_tls", fallback="true"), True)
        self.ca_file = parser.get("vcenter", "ca_file", fallback="").strip() or None
        self.api_release = parser.get("vcenter", "api_release", fallback="").strip() or None
        self.timeout = parser.getint("vcenter", "timeout_seconds", fallback=120)

        self.data_dir = path_opt("collector", "data_dir", "data")
        self.log_dir = parser.get("collector", "log_dir", fallback="").strip()
        if self.log_dir and not os.path.isabs(self.log_dir):
            self.log_dir = os.path.normpath(os.path.join(base, self.log_dir))
        self.interval_minutes = parser.getint("collector", "interval_minutes", fallback=60)
        if self.interval_minutes not in INTERVAL_CHOICES:
            LOG.warning("interval_minutes = %s is not one of %s - using 60",
                        self.interval_minutes, ", ".join(str(i) for i in INTERVAL_CHOICES))
            self.interval_minutes = 60
        window = parser.get("collector", "window_minutes", fallback="").strip()
        self.window_minutes = max(5, min(60, int(window))) if window else self.interval_minutes
        self.exclude_vm_pattern = parser.get("collector", "exclude_vm_pattern", fallback="^vCLS-").strip()
        self.batch_size = max(1, parser.getint("collector", "batch_size", fallback=50))
        self.retention_months = parser.getint("collector", "retention_months", fallback=13)
        self.compress_old_months = to_bool(parser.get("collector", "compress_old_months", fallback="true"), True)

        self.report_dir = path_opt("report", "report_dir", "reports")
        self.days = max(1, min(400, parser.getint("report", "days", fallback=30)))
        self.language = parser.get("report", "language", fallback="en").strip().lower() or "en"
        if self.language not in LANGUAGES:
            LOG.warning("language = %s is not supported (%s) - using en", self.language, ", ".join(LANGUAGES))
            self.language = "en"
        self.candidate_pct = parser.getfloat("report", "candidate_pct", fallback=40.0)
        self.threshold_pct = parser.getfloat("report", "threshold_pct", fallback=50.0)
        self.tier_ratio = parser.getfloat("report", "tier_ratio", fallback=1.0)
        self.ram_bound_pct = parser.getfloat("report", "ram_bound_pct", fallback=70.0)
        self.cpu_idle_pct = parser.getfloat("report", "cpu_idle_pct", fallback=50.0)
        self.stretched_cluster = to_bool(parser.get("report", "stretched_cluster", fallback="false"), False)
        self.stretched_clusters = [c.strip() for c in parser.get("report", "stretched_clusters", fallback="").split(",") if c.strip()]
        self.cold_pct = parser.getfloat("report", "cold_pct", fallback=40.0)
        self.hot_pct = parser.getfloat("report", "hot_pct", fallback=75.0)
        self.title = parser.get("report", "title", fallback="VMware Memory Tiering Report").strip()
        self.support_contact = parser.get("report", "support_contact", fallback="https://github.com/steiner-dominik/vmware-memory-report/issues").strip()
        self.template = parser.get("report", "template", fallback="").strip() or None


def setup_logging(log_dir, name, verbose):
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    LOG.addHandler(console)
    if log_dir:
        ensure_dir(log_dir)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "memtier-%s.log" % name), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        handler.setFormatter(fmt)
        LOG.addHandler(handler)


class RunLock(object):
    """Prevents overlapping runs (e.g. a slow collection still running when cron fires again)."""

    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        ensure_dir(os.path.dirname(self.path))
        self.handle = open(self.path, "a+")
        try:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            pass  # Windows: no flock, rely on the scheduler
        except (IOError, OSError):
            self.handle.close()
            raise MemTierError("another run holds the lock %s - exiting" % self.path)
        return self

    def __exit__(self, *exc):
        if self.handle:
            self.handle.close()


# ----------------------------------------------------------------------------
# VI/JSON client (stdlib only)
# ----------------------------------------------------------------------------

def unbox(value):
    """VI/JSON boxes values in 'anyType' positions as {"_typeName": ..., "_value": ...}."""
    if isinstance(value, dict):
        if "_value" in value and set(value.keys()) <= {"_typeName", "_value"}:
            return unbox(value["_value"])
        return value
    if isinstance(value, list):
        return [unbox(v) for v in value]
    return value


def moref(obj_type, value):
    return {"_typeName": "ManagedObjectReference", "type": obj_type, "value": value}


class ViJsonClient(object):
    def __init__(self, server, username, password, verify_tls=True, ca_file=None, api_release=None, timeout=120):
        self.server = server
        self.username = username
        self.password = password
        self.timeout = timeout
        self.release = api_release
        self.session_id = None
        self.content = None
        self._counters = None
        if verify_tls:
            self.ssl_context = ssl.create_default_context(cafile=ca_file)
        else:
            self.ssl_context = ssl.create_default_context()
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE
        self._conn = None

    # --- transport -----------------------------------------------------------
    def _connection(self):
        if self._conn is None:
            self._conn = http.client.HTTPSConnection(self.server, timeout=self.timeout, context=self.ssl_context)
        return self._conn

    def _raw(self, method, path, body=None, headers=None):
        payload = None if body is None else json.dumps(body).encode("utf-8")
        hdrs = {"Accept": "application/json"}
        if payload is not None:
            hdrs["Content-Type"] = "application/json"
        if self.session_id:
            hdrs["vmware-api-session-id"] = self.session_id
        if headers:
            hdrs.update(headers)
        for attempt in (1, 2):
            conn = self._connection()
            try:
                conn.request(method, path, body=payload, headers=hdrs)
                resp = conn.getresponse()
                data = resp.read()
                return resp.status, resp, data
            except ssl.SSLError as exc:
                self.close()
                raise MemTierError(
                    "TLS error talking to %s: %s. Point [vcenter] ca_file at the vCenter root CA, "
                    "or set verify_tls = false." % (self.server, exc))
            except (http.client.HTTPException, socket.error) as exc:
                self.close()
                if attempt == 2:
                    raise MemTierError("connection to %s failed: %s" % (self.server, exc))
                LOG.debug("connection reset (%s), retrying", exc)

    def _vim_path(self, obj_type, obj_id, member):
        # vCenter does not decode %5B/%5D: view ids like "session[...]..." must keep their brackets
        return "/sdk/vim25/%s/%s/%s/%s" % (self.release, obj_type, quote(obj_id, safe="[]"), member)

    @staticmethod
    def _fault_text(status, data):
        try:
            fault = json.loads(data.decode("utf-8"))
            name = fault.get("_typeName", "Fault")
            messages = [m.get("message", "") for m in fault.get("faultMessage", []) or [] if isinstance(m, dict)]
            detail = fault.get("message") or "; ".join(x for x in messages if x)
            return "%s%s" % (name, ": " + detail if detail else ""), name
        except Exception:
            return "HTTP %s: %s" % (status, data[:300].decode("utf-8", "replace")), "HTTP%s" % status

    def invoke(self, obj_type, obj_id, method, body=None):
        status, _, data = self._raw("POST", self._vim_path(obj_type, obj_id, method), body if body is not None else {})
        if status >= 300:
            text, name = self._fault_text(status, data)
            err = MemTierError("%s.%s failed: %s" % (obj_type, method, text))
            err.fault = name
            raise err
        if not data or not data.strip():
            return None
        return json.loads(data.decode("utf-8"))

    def get_property(self, obj_type, obj_id, prop):
        status, _, data = self._raw("GET", self._vim_path(obj_type, obj_id, prop))
        if status >= 300:
            text, name = self._fault_text(status, data)
            err = MemTierError("GET %s/%s failed: %s" % (obj_type, prop, text))
            err.fault = name
            raise err
        return json.loads(data.decode("utf-8")) if data.strip() else None

    # --- session -------------------------------------------------------------
    def detect_release(self):
        if self.release:
            return self.release
        status, _, data = self._raw("GET", "/sdk/vimServiceVersions.xml")
        text = data.decode("utf-8", "replace") if status == 200 else ""
        match = re.search(r"<name>\s*urn:vim25\s*</name>\s*<version>\s*([\d.]+)\s*</version>", text)
        if not match:
            raise MemTierError("could not detect the vSphere API release of %s (set [vcenter] api_release)" % self.server)
        release = match.group(1)
        parts = [int(x) for x in release.split(".")[:3]] + [0, 0, 0]
        if parts[:3] < [8, 0, 1]:
            raise MemTierError("%s reports API %s - VI/JSON needs vCenter 8.0 U1 or later. "
                               "Use the PowerShell edition for older vCenters." % (self.server, release))
        self.release = release
        return release

    def login(self):
        self.detect_release()
        if not self.password:
            raise MemTierError("no password configured (config file or MEMTIER_PASSWORD)")
        status, resp, data = self._raw("POST", self._vim_path("SessionManager", "SessionManager", "Login"),
                                       {"userName": self.username, "password": self.password})
        if status < 300 and resp.getheader("vmware-api-session-id"):
            self.session_id = resp.getheader("vmware-api-session-id")
        else:
            text, name = self._fault_text(status, data)
            if name == "InvalidLogin" or status in (401, 403):
                raise MemTierError("login to %s as %s failed: %s" % (self.server, self.username, text))
            # fall back to the Automation API session endpoint (same session token)
            token = base64.b64encode(("%s:%s" % (self.username, self.password)).encode("utf-8")).decode("ascii")
            status2, _, data2 = self._raw("POST", "/api/session", None, {"Authorization": "Basic " + token})
            if status2 >= 300:
                raise MemTierError("login to %s failed: %s / HTTP %s" % (self.server, text, status2))
            self.session_id = json.loads(data2.decode("utf-8"))
        self.content = unbox(self.get_property("ServiceInstance", "ServiceInstance", "content"))
        LOG.debug("logged in to %s (API %s)", self.server, self.release)

    def logout(self):
        if self.session_id and self.content:
            try:
                sm = self.content.get("sessionManager", {}).get("value", "SessionManager")
                self.invoke("SessionManager", sm, "Logout")
            except Exception as exc:  # logout failures must never fail the run
                LOG.debug("logout failed: %s", exc)
        self.session_id = None
        self.close()

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # --- inventory -----------------------------------------------------------
    def retrieve(self, obj_type, paths):
        """Return [(moref_value, {path: value})] for all objects of a type, paging through the results."""
        view_mgr = self.content["viewManager"]
        view = self.invoke("ViewManager", view_mgr["value"], "CreateContainerView", {
            "container": self.content["rootFolder"], "type": [obj_type], "recursive": True})
        pc = self.content["propertyCollector"]["value"]
        try:
            spec = {
                "specSet": [{
                    "_typeName": "PropertyFilterSpec",
                    "propSet": [{"_typeName": "PropertySpec", "type": obj_type, "all": False, "pathSet": paths}],
                    "objectSet": [{
                        "_typeName": "ObjectSpec", "obj": view, "skip": True,
                        "selectSet": [{"_typeName": "TraversalSpec", "name": "traverseView",
                                       "type": "ContainerView", "path": "view", "skip": False}],
                    }],
                }],
                "options": {"_typeName": "RetrieveOptions", "maxObjects": 1000},
            }
            out = []
            result = self.invoke("PropertyCollector", pc, "RetrievePropertiesEx", spec)
            while result:
                for obj in result.get("objects", []) or []:
                    props = {}
                    for prop in obj.get("propSet", []) or []:
                        props[prop["name"]] = unbox(prop.get("val"))
                    out.append((obj["obj"]["value"], props))
                token = result.get("token")
                if not token:
                    break
                result = self.invoke("PropertyCollector", pc, "ContinueRetrievePropertiesEx", {"token": token})
            return out
        finally:
            try:
                self.invoke("ContainerView", view["value"], "DestroyView")
            except Exception as exc:
                LOG.debug("DestroyView failed: %s", exc)

    # --- performance ---------------------------------------------------------
    def counter_ids(self, names):
        lookup = self.counter_lookup()
        missing = [n for n in names if n not in lookup]
        if missing:
            raise MemTierError("performance counters not found on %s: %s" % (self.server, ", ".join(missing)))
        return dict((n, lookup[n]) for n in names)

    def optional_counter_ids(self, names):
        """Like counter_ids, but silently drops counters this vCenter does not publish."""
        lookup = self.counter_lookup()
        return dict((n, lookup[n]) for n in names if n in lookup)

    def counter_lookup(self):
        """{"group.name.rollup": counterId} for every counter this vCenter publishes, cached."""
        if self._counters is None:
            perf = self.content["perfManager"]["value"]
            counters = unbox(self.get_property("PerformanceManager", perf, "perfCounter")) or []
            lookup = {}
            for c in counters:
                key = "%s.%s.%s" % (c["groupInfo"]["key"], c["nameInfo"]["key"], unbox(c["rollupType"]))
                lookup[key] = c["key"]
            self._counters = lookup
        return self._counters

    def tier_counters(self):
        """Memory-tiering counters this vCenter offers.

        How much memory a host currently keeps on its NVMe tier is not part of the inventory:
        hardware.memoryTierInfo only gives the tier sizes. vSphere 8.0 U3 and later publish
        per-tier performance counters, but their names have moved between releases, so they are
        discovered rather than assumed. What is found is logged and reported, which is how the
        next release learns which ones to collect.
        """
        # Any group, not just mem.*: if a release files them elsewhere, a narrow filter would
        # report "none available" when the counters are simply somewhere else.
        return sorted(n for n in self.counter_lookup() if "tier" in n.lower())

    def query_perf(self, entity_type, counter_map, start, per_instance=()):
        """Returns {entity_id: {counter_name: [values]}}; splits batches that fault and skips entities that fail alone.

        Counters named in per_instance are kept per instance under "name|instance"; every other
        counter keeps only the aggregate, because its per-device rollups say nothing useful here.
        """
        perf = self.content["perfManager"]["value"]
        by_id = dict((v, k) for k, v in counter_map.items())
        per_instance = set(per_instance)
        metric_ids = [{"_typeName": "PerfMetricId", "counterId": cid,
                       "instance": "*" if by_id[cid] in per_instance else ""}
                      for cid in counter_map.values()]
        results, failed = {}, []

        def run(ids):
            specs = [{
                "_typeName": "PerfQuerySpec", "entity": moref(entity_type, eid), "startTime": iso(start),
                "intervalId": REALTIME_INTERVAL, "format": "normal", "metricId": metric_ids,
            } for eid in ids]
            try:
                answer = self.invoke("PerformanceManager", perf, "QueryPerf", {"querySpec": specs}) or []
            except MemTierError as exc:
                if len(ids) == 1:
                    LOG.warning("no performance data for %s %s: %s", entity_type, ids[0], exc)
                    failed.append(ids[0])
                    return
                LOG.debug("perf batch of %d failed (%s) - splitting", len(ids), exc)
                half = len(ids) // 2
                run(ids[:half])
                run(ids[half:])
                return
            for item in unbox(answer):
                eid = item["entity"]["value"]
                series = results.setdefault(eid, {})
                for s in item.get("value", []) or []:
                    name = by_id.get(s["id"]["counterId"])
                    if not name:
                        continue
                    instance = s["id"].get("instance") or ""
                    if instance and name not in per_instance:
                        continue        # instance rollups of ordinary counters are noise
                    key = "%s|%s" % (name, instance) if name in per_instance else name
                    if instance or name not in per_instance:
                        series.setdefault(key, []).extend(unbox(s.get("value")) or [])

        return results, failed, run


def perf_batches(client, entity_type, ids, counter_map, start, batch_size, per_instance=()):
    results, failed, run = client.query_perf(entity_type, counter_map, start, per_instance)
    for i in range(0, len(ids), batch_size):
        run(ids[i:i + batch_size])
    return results, failed


# ----------------------------------------------------------------------------
# CSV storage
# ----------------------------------------------------------------------------

def widen_csv(path, header, fields):
    """Add new trailing columns to an existing monthly file, in place.

    Releases add columns. Refusing to append would strand a month of history on the day of an
    upgrade, so a file whose header is a prefix of the current one is rewritten with the new
    header and empty values for the new columns. Anything else is still refused: that is a file
    this tool did not write.
    """
    LOG.info("adding %d new column(s) to %s", len(fields) - len(header), path)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with io.open(path, "r", encoding="utf-8-sig", newline="") as src, \
            io.open(tmp, "w", encoding="utf-8", newline="") as dst:
        reader = csv.reader(src)
        writer = csv.writer(dst, lineterminator="\r\n")
        next(reader, None)
        writer.writerow(fields)
        pad = [""] * (len(fields) - len(header))
        for row in reader:
            if row:
                writer.writerow(row + pad)
    os.replace(tmp, path)


def append_csv(path, fields, rows):
    if not rows:
        return
    exists = os.path.isfile(path) and os.path.getsize(path) > 0
    if exists:
        with io.open(path, "r", encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle), [])
        if header != fields:
            if len(header) < len(fields) and header == fields[:len(header)]:
                widen_csv(path, header, fields)
            else:
                raise MemTierError("%s has an unexpected header - move it away and rerun" % path)
    with io.open(path, "a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\r\n")
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(dict((k, "" if row.get(k) is None else row.get(k)) for k in fields))


def data_files(data_dir, kind):
    """{month: path} preferring .csv over .csv.gz if both exist."""
    files = {}
    for path in glob.glob(os.path.join(data_dir, "%s-*.csv*" % kind)):
        m = re.search(r"%s-(\d{4}-\d{2})\.csv(\.gz)?$" % kind, os.path.basename(path))
        if m and (m.group(1) not in files or not path.endswith(".gz")):
            files[m.group(1)] = path
    return files


def maintain_files(data_dir, now, compress, retention_months):
    current = month_key(now)
    cutoff = None
    if retention_months and retention_months > 0:
        y, mo = now.year, now.month - retention_months
        while mo <= 0:
            mo += 12
            y -= 1
        cutoff = "%04d-%02d" % (y, mo)
    for kind in ("host", "vm", "run"):
        for path in glob.glob(os.path.join(data_dir, "%s-*.csv*" % kind)):
            m = re.search(r"-(\d{4}-\d{2})\.csv(\.gz)?$", path)
            if not m:
                continue
            month = m.group(1)
            if cutoff and month < cutoff:
                LOG.info("retention: deleting %s", path)
                os.remove(path)
            elif compress and month < current and not path.endswith(".gz"):
                LOG.info("compressing %s", path)
                with open(path, "rb") as src, gzip.open(path + ".gz", "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.remove(path)


def read_csv(path):
    opener = gzip.open if path.endswith(".gz") else io.open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            yield row


# ----------------------------------------------------------------------------
# collect
# ----------------------------------------------------------------------------

def collect_vcenter(cfg, server, now):
    started = time.time()
    run = dict((k, None) for k in RUN_FIELDS)
    run.update({"Timestamp": iso(now), "VCenter": server, "Status": "failed"})
    host_rows, vm_rows = [], []
    client = ViJsonClient(server, cfg.username, cfg.password, cfg.verify_tls, cfg.ca_file, cfg.api_release, cfg.timeout)
    try:
        client.login()
        start = now - _dt.timedelta(minutes=cfg.window_minutes)

        clusters = dict((mid, p.get("name")) for mid, p in client.retrieve("ClusterComputeResource", ["name"]))
        hosts = client.retrieve("HostSystem", ["name", "parent", "hardware.memorySize",
                                               "runtime.connectionState", "runtime.inMaintenanceMode",
                                               "hardware.cpuInfo.numCpuCores", "hardware.cpuInfo.numCpuThreads",
                                               "hardware.cpuInfo.hz"])
        tier_info = {}
        try:
            for mid, p in client.retrieve("HostSystem", ["hardware.memoryTieringType", "hardware.memoryTierInfo"]):
                tier_info[mid] = p
        except MemTierError as exc:
            LOG.info("memory tier properties not available on %s (%s) - using hardware.memorySize as DRAM", server, exc)

        vms = client.retrieve("VirtualMachine", [
            "name", "runtime.powerState", "runtime.host", "runtime.connectionState", "config.template",
            "config.hardware.memoryMB", "config.memoryAllocation.reservation", "config.latencySensitivity.level"])

        host_meta = {}
        for mid, p in hosts:
            parent = p.get("parent") or {}
            cluster = clusters.get(parent.get("value"), "(standalone)")
            phys_mb = round_half_up((p.get("hardware.memorySize") or 0) / 1048576.0)
            dram_mb, nvme_mb = phys_mb, 0
            tiers = (tier_info.get(mid) or {}).get("hardware.memoryTierInfo") or []
            if tiers:
                dram = sum(t.get("size") or 0 for t in tiers if str(t.get("type", "")).lower() == "dram")
                nvme = sum(t.get("size") or 0 for t in tiers if str(t.get("type", "")).lower() != "dram")
                if dram:
                    dram_mb = round_half_up(dram / 1048576.0)
                nvme_mb = round_half_up(nvme / 1048576.0)
            tier_names = [(str(t.get("name") or ""), str(t.get("type") or "")) for t in tiers]
            host_meta[mid] = {
                "tiers": tier_names,
                "name": p.get("name"), "cluster": cluster, "state": p.get("runtime.connectionState"),
                "maint": bool(p.get("runtime.inMaintenanceMode")), "phys": phys_mb, "dram": dram_mb, "nvme": nvme_mb,
                "tiering": (tier_info.get(mid) or {}).get("hardware.memoryTieringType") or "",
                "cores": as_int(p.get("hardware.cpuInfo.numCpuCores")),
                "threads": as_int(p.get("hardware.cpuInfo.numCpuThreads")),
                "mhz": round_half_up((p.get("hardware.cpuInfo.hz") or 0) / 1000000.0) or None,
                "vms_on": 0, "assigned": 0,
            }

        exclude = re.compile(cfg.exclude_vm_pattern) if cfg.exclude_vm_pattern else None
        counts = {"total": 0, "on": 0, "off": 0, "suspended": 0, "templates": 0, "excluded": 0, "unreachable": 0}
        vm_meta = {}
        for mid, p in vms:
            name = unquote(p.get("name") or mid)  # the API escapes / \ % in names
            if p.get("config.template"):
                counts["templates"] += 1
                continue
            counts["total"] += 1
            power = p.get("runtime.powerState")
            if power == "poweredOn":
                counts["on"] += 1
            elif power == "suspended":
                counts["suspended"] += 1
            else:
                counts["off"] += 1
            if power != "poweredOn":
                continue
            if p.get("runtime.connectionState") not in (None, "connected"):
                counts["unreachable"] += 1  # e.g. VM on a disconnected host: no statistics possible
                continue
            if exclude and exclude.search(name):
                counts["excluded"] += 1
                continue
            host_id = (p.get("runtime.host") or {}).get("value")
            hm = host_meta.get(host_id)
            assigned = p.get("config.hardware.memoryMB") or 0
            if hm:
                hm["vms_on"] += 1
                hm["assigned"] += assigned
            # "(unknown)": the VM names a host that is not in the inventory; "(none)": no host at all
            missing = "(unknown)" if host_id else "(none)"
            vm_meta[mid] = {"name": name, "host": hm["name"] if hm else missing, "cluster": hm["cluster"] if hm else missing,
                            "assigned": assigned, "reservation": p.get("config.memoryAllocation.reservation"),
                            "latency": p.get("config.latencySensitivity.level") or ""}

        live_hosts = [mid for mid, h in host_meta.items() if h["state"] == "connected"]
        host_counters = client.counter_ids(HOST_COUNTERS)
        vm_counters = client.counter_ids(VM_COUNTERS)
        tier_counters = client.tier_counters()
        if tier_counters:
            LOG.info("%s publishes tiering counters: %s", server, ", ".join(tier_counters))
        elif any(h["nvme"] for h in host_meta.values()):
            LOG.info("%s has hosts with an NVMe tier but publishes no counter with 'tier' in its name "
                     "(%d counters offered) - tier sizes are reported, current tier usage is not",
                     server, len(client.counter_lookup()))
        cpu_counters = client.optional_counter_ids(HOST_CPU_COUNTERS)
        if not cpu_counters:
            LOG.info("%s does not publish %s - the CPU columns stay empty",
                     server, ", ".join(HOST_CPU_COUNTERS))
        tier_perf = client.optional_counter_ids(HOST_TIER_COUNTERS)
        if tier_perf:
            LOG.info("%s reports memory consumed per tier (%s)", server, ", ".join(sorted(tier_perf)))
        elif any(h["nvme"] for h in host_meta.values()):
            LOG.info("%s has hosts with an NVMe tier but does not publish %s (vSphere 9 and later do) - "
                     "the split between DRAM and NVMe is derived from consumed memory instead",
                     server, ", ".join(HOST_TIER_COUNTERS))
        host_counters = dict(host_counters, **cpu_counters)
        host_counters.update(tier_perf)
        host_stats, host_failed = perf_batches(client, "HostSystem", live_hosts, host_counters, start,
                                               cfg.batch_size, per_instance=tier_perf.keys())
        vm_stats, vm_failed = perf_batches(client, "VirtualMachine", sorted(vm_meta), vm_counters, start, cfg.batch_size)

        hosts_without = 0
        for mid in sorted(host_meta, key=lambda k: (host_meta[k]["cluster"], host_meta[k]["name"])):
            h = host_meta[mid]
            series = host_stats.get(mid, {})
            a_avg, a_p95, a_max, samples = summarize_kb(series.get("mem.active.average", []))
            c_avg, _, c_max, _ = summarize_kb(series.get("mem.consumed.average", []))
            _, _, b_max, _ = summarize_kb(series.get("mem.vmmemctl.average", []))
            _, _, s_max, _ = summarize_kb(series.get("mem.swapused.average", []))
            cpu_avg, cpu_p95, cpu_max = summarize_pct(series.get("cpu.usage.average", []))
            tier_dram, tier_nvme = tier_consumed(series, h["tiers"])
            if h["state"] == "connected" and not samples:
                hosts_without += 1
            host_rows.append({
                "Timestamp": iso(now), "WindowStart": iso(start), "VCenter": server, "Cluster": h["cluster"],
                "VMHost": h["name"], "HostId": mid, "ConnectionState": h["state"], "MaintenanceMode": str(h["maint"]).lower(),
                "TieringType": h["tiering"], "PhysicalMB": h["phys"], "DramMB": h["dram"], "NvmeTierMB": h["nvme"],
                "VMsOn": h["vms_on"], "AssignedMB": h["assigned"], "Samples": samples,
                "ActiveAvgMB": a_avg, "ActiveP95MB": a_p95, "ActiveMaxMB": a_max, "ConsumedAvgMB": c_avg,
                "ConsumedMaxMB": c_max, "BalloonMaxMB": b_max, "SwapUsedMaxMB": s_max,
                "CpuCores": h["cores"], "CpuThreads": h["threads"], "CpuMhz": h["mhz"],
                "CpuAvgPct": cpu_avg, "CpuP95Pct": cpu_p95, "CpuMaxPct": cpu_max,
                "TierDramMB": tier_dram, "TierNvmeMB": tier_nvme,
            })

        vms_without = counts["unreachable"]
        for mid in sorted(vm_meta, key=lambda k: vm_meta[k]["name"].lower()):
            v = vm_meta[mid]
            series = vm_stats.get(mid, {})
            a_avg, a_p95, a_max, samples = summarize_kb(series.get("mem.active.average", []))
            if not samples:
                vms_without += 1
                continue
            c_avg, _, c_max, _ = summarize_kb(series.get("mem.consumed.average", []))
            _, _, b_max, _ = summarize_kb(series.get("mem.vmmemctl.average", []))
            _, _, s_max, _ = summarize_kb(series.get("mem.swapped.average", []))
            vm_rows.append({
                "Timestamp": iso(now), "WindowStart": iso(start), "VCenter": server, "Cluster": v["cluster"],
                "VMHost": v["host"], "VM": v["name"], "VMId": mid, "AssignedMB": v["assigned"],
                "ReservationMB": v["reservation"], "LatencySensitivity": v["latency"], "Samples": samples,
                "ActiveAvgMB": a_avg, "ActiveP95MB": a_p95, "ActiveMaxMB": a_max, "ConsumedAvgMB": c_avg,
                "ConsumedMaxMB": c_max, "BalloonMaxMB": b_max, "SwappedMaxMB": s_max,
            })

        partial = bool(host_failed or vm_failed or hosts_without)
        run.update({
            "Status": "partial" if partial else "ok", "Hosts": len(host_meta), "HostsConnected": len(live_hosts),
            "VMsTotal": counts["total"], "VMsOn": counts["on"], "VMsOff": counts["off"], "VMsSuspended": counts["suspended"],
            "Templates": counts["templates"], "VMsExcluded": counts["excluded"], "VMsWithoutStats": vms_without,
            "HostsWithoutStats": hosts_without,
            "Message": ("perf query failed for %d hosts, %d VMs" % (len(host_failed), len(vm_failed))) if partial else "",
        })
        run["TierCounters"] = ";".join(tier_counters)
        LOG.info("%s: %d hosts (%d connected), %d VMs (%d powered on, %d with stats), %d templates",
                 server, len(host_meta), len(live_hosts), counts["total"], counts["on"], len(vm_rows), counts["templates"])
    except Exception as exc:
        LOG.error("%s: collection failed: %s", server, exc)
        LOG.debug("details", exc_info=True)
        run["Message"] = str(exc)[:500]
        host_rows, vm_rows = [], []
    finally:
        client.logout()
        run["DurationSec"] = int(round(time.time() - started))
    return host_rows, vm_rows, run


def cmd_collect(cfg, args):
    if not cfg.servers:
        raise MemTierError("no vCenter configured ([vcenter] servers)")
    ensure_dir(cfg.data_dir)
    exit_code = 0
    with RunLock(os.path.join(cfg.data_dir, ".collect.lock")):
        now = utcnow()
        month = month_key(now)
        for server in cfg.servers:
            host_rows, vm_rows, run = collect_vcenter(cfg, server, now)
            append_csv(os.path.join(cfg.data_dir, "host-%s.csv" % month), HOST_FIELDS, host_rows)
            append_csv(os.path.join(cfg.data_dir, "vm-%s.csv" % month), VM_FIELDS, vm_rows)
            append_csv(os.path.join(cfg.data_dir, "run-%s.csv" % month), RUN_FIELDS, [run])
            if run["Status"] == "failed":
                exit_code = 1
            elif run["Status"] == "partial" and exit_code == 0:
                exit_code = 2
        maintain_files(cfg.data_dir, now, cfg.compress_old_months, cfg.retention_months)
    # Outside the lock: a slow report must not block the next collection.
    if not args.no_report:
        try:
            write_report(cfg, cfg.days)
        except Exception as exc:
            LOG.error("report failed: %s", exc)
            exit_code = 1
    return exit_code


# ----------------------------------------------------------------------------
# report
# ----------------------------------------------------------------------------

def load_rows(data_dir, kind, cutoff_ts):
    cutoff_month = cutoff_ts.strftime("%Y-%m")
    rows = []
    files = data_files(data_dir, kind)
    for month in sorted(files):
        if month < cutoff_month:
            continue
        for row in read_csv(files[month]):
            try:
                ts = epoch(parse_iso(row["Timestamp"]))
            except (KeyError, ValueError):
                continue
            if ts >= epoch(cutoff_ts):
                row["_ts"] = ts
                rows.append(row)
    rows.sort(key=lambda r: r["_ts"])  # stable: keeps file order for equal timestamps
    return rows


def build_report_data(cfg, now, days, builder):
    cutoff = now - _dt.timedelta(days=days)
    cutoff_ts, now_ts = epoch(cutoff), epoch(now)
    bucket_hours = max(1, int(math.ceil(days * 24 / 1500.0)))
    bucket = bucket_hours * 3600

    # hosts ---------------------------------------------------------------
    hosts = {}
    for r in load_rows(cfg.data_dir, "host", cutoff):
        key = "%s|%s" % (r["VCenter"], r["HostId"])
        h = hosts.get(key)
        if h is None:
            h = hosts[key] = {"key": key, "vc": r["VCenter"], "buckets": {}, "order": []}
        # latest metadata wins
        h.update({"name": r["VMHost"], "cluster": r["Cluster"], "tiering": r.get("TieringType") or "",
                  "dramMB": as_int(r.get("DramMB")) or as_int(r.get("PhysicalMB")) or 0,
                  "nvmeMB": as_int(r.get("NvmeTierMB")) or 0, "physMB": as_int(r.get("PhysicalMB")) or 0,
                  "cores": as_int(r.get("CpuCores")), "threads": as_int(r.get("CpuThreads")),
                  "mhz": as_int(r.get("CpuMhz"))})
        avg = as_int(r.get("ActiveAvgMB"))
        if avg is None:
            continue
        samples = max(1, as_int(r.get("Samples")) or 1)
        b = r["_ts"] // bucket * bucket
        acc = h["buckets"].get(b)
        if acc is None:
            acc = h["buckets"][b] = {"w": 0, "avg": 0.0, "cons": 0.0, "cons_w": 0, "cons_max": 0, "cons_max_n": 0,
                                     "vms": 0, "assigned": 0, "p95": 0, "max": 0, "balloon": 0, "swap": 0,
                                     "dram": 0, "nvme": 0,
                                     "cpu": 0.0, "cpu_w": 0, "cpu_p95": 0.0, "cpu_max": 0.0,
                                     "t_dram": 0.0, "t_nvme": 0.0, "t_w": 0}
            h["order"].append(b)
        acc["w"] += samples
        acc["avg"] += avg * samples
        cons = as_int(r.get("ConsumedAvgMB"))
        if cons is not None:
            acc["cons"] += cons * samples
            acc["cons_w"] += samples
        cons_max = as_int(r.get("ConsumedMaxMB"))
        if cons_max is not None:
            acc["cons_max"] = max(acc["cons_max"], cons_max)
            acc["cons_max_n"] += 1
        acc["vms"] = max(acc["vms"], as_int(r.get("VMsOn")) or 0)
        acc["assigned"] = max(acc["assigned"], as_int(r.get("AssignedMB")) or 0)
        acc["p95"] = max(acc["p95"], as_int(r.get("ActiveP95MB")) or 0)
        acc["max"] = max(acc["max"], as_int(r.get("ActiveMaxMB")) or 0)
        acc["balloon"] = max(acc["balloon"], as_int(r.get("BalloonMaxMB")) or 0)
        acc["swap"] = max(acc["swap"], as_int(r.get("SwapUsedMaxMB")) or 0)
        acc["dram"] = as_int(r.get("DramMB")) or as_int(r.get("PhysicalMB")) or 0
        # Per bucket, not per host: a host that gets a tier mid-range must not look tiered all along.
        acc["nvme"] = as_int(r.get("NvmeTierMB")) or 0
        t_dram, t_nvme = as_int(r.get("TierDramMB")), as_int(r.get("TierNvmeMB"))
        if t_dram is not None or t_nvme is not None:
            acc["t_dram"] += (t_dram or 0) * samples
            acc["t_nvme"] += (t_nvme or 0) * samples
            acc["t_w"] += samples
        cpu = as_float(r.get("CpuAvgPct"))
        if cpu is not None:
            acc["cpu"] += cpu * samples
            acc["cpu_w"] += samples
            acc["cpu_p95"] = max(acc["cpu_p95"], as_float(r.get("CpuP95Pct")) or 0.0)
            acc["cpu_max"] = max(acc["cpu_max"], as_float(r.get("CpuMaxPct")) or 0.0)

    host_list = []
    for key in sorted(hosts, key=lambda k: (hosts[k]["vc"], hosts[k]["cluster"], hosts[k]["name"])):
        h = hosts[key]
        series = []
        for b in sorted(h["buckets"]):
            a = h["buckets"][b]
            series.append([b, a["vms"], a["assigned"], round_half_up(a["avg"] / a["w"]), a["p95"], a["max"],
                           round_half_up(a["cons"] / a["cons_w"]) if a["cons_w"] else None,
                           a["balloon"], a["swap"], a["dram"],
                           a["cons_max"] if a["cons_max_n"] else None, a["nvme"],
                           round1(a["cpu"] / a["cpu_w"]) if a["cpu_w"] else None,
                           round1(a["cpu_p95"]) if a["cpu_w"] else None,
                           round1(a["cpu_max"]) if a["cpu_w"] else None,
                           round_half_up(a["t_dram"] / a["t_w"]) if a["t_w"] else None,
                           round_half_up(a["t_nvme"] / a["t_w"]) if a["t_w"] else None])
        host_list.append({"key": key, "vc": h["vc"], "name": h["name"], "cluster": h["cluster"], "tiering": h["tiering"],
                          "dramMB": h["dramMB"], "nvmeMB": h["nvmeMB"], "physMB": h["physMB"],
                          "cores": h.get("cores"), "threads": h.get("threads"), "mhz": h.get("mhz"), "s": series})

    # VMs -----------------------------------------------------------------
    day0 = cutoff_ts // 86400 * 86400
    ndays = now_ts // 86400 - cutoff_ts // 86400 + 1
    vms = {}
    for r in load_rows(cfg.data_dir, "vm", cutoff):
        assigned = as_int(r.get("AssignedMB"))
        avg = as_int(r.get("ActiveAvgMB"))
        if not assigned or avg is None:
            continue
        key = "%s|%s" % (r["VCenter"], r["VMId"])
        v = vms.get(key)
        if v is None:
            v = vms[key] = {"days": {}}
        v.update({"vc": r["VCenter"], "name": r["VM"], "cluster": r["Cluster"], "host": r["VMHost"],
                  "assignedMB": assigned, "reservationMB": as_int(r.get("ReservationMB")),
                  "latency": r.get("LatencySensitivity") or "", "lastTs": r["_ts"]})
        di = (r["_ts"] // 86400 * 86400 - day0) // 86400
        if di < 0 or di >= ndays:
            continue
        samples = max(1, as_int(r.get("Samples")) or 1)
        d = v["days"].get(di)
        if d is None:
            d = v["days"][di] = {"minutes": 0.0, "num": 0.0, "den": 0.0, "p95": [], "max": 0.0, "balloon": 0, "swap": 0,
                                 "cnum": 0.0, "cden": 0.0, "cmax": None}
        # Minutes covered, derived from the 20-second samples: correct for any collection
        # interval, where counting rows silently quadrupled the weight at 15-minute runs.
        d["minutes"] += samples * REALTIME_INTERVAL / 60.0
        d["num"] += avg * samples
        d["den"] += assigned * samples
        d["p95"].append((as_int(r.get("ActiveP95MB")) or 0) * 100.0 / assigned)
        d["max"] = max(d["max"], (as_int(r.get("ActiveMaxMB")) or 0) * 100.0 / assigned)
        d["balloon"] = max(d["balloon"], as_int(r.get("BalloonMaxMB")) or 0)
        d["swap"] = max(d["swap"], as_int(r.get("SwappedMaxMB")) or 0)
        cons = as_int(r.get("ConsumedAvgMB"))
        if cons is not None:
            d["cnum"] += cons * samples
            d["cden"] += assigned * samples
        cons_max = as_int(r.get("ConsumedMaxMB"))
        if cons_max is not None:
            pct_max = cons_max * 100.0 / assigned
            d["cmax"] = pct_max if d["cmax"] is None else max(d["cmax"], pct_max)

    vm_list = []
    for key in sorted(vms, key=lambda k: (vms[k]["vc"], vms[k]["name"].lower(), k)):
        v = vms[key]
        daily = [None] * ndays
        for di, d in v["days"].items():
            daily[di] = [round1(d["minutes"]), round1(d["num"] * 100.0 / d["den"]), round1(p95(d["p95"])), round1(d["max"]),
                         d["balloon"], d["swap"],
                         round1(d["cnum"] * 100.0 / d["cden"]) if d["cden"] else None,
                         round1(d["cmax"]) if d["cmax"] is not None else None]
        vm_list.append({"id": key, "vc": v["vc"], "name": v["name"], "cluster": v["cluster"], "host": v["host"],
                        "assignedMB": v["assignedMB"], "reservationMB": v["reservationMB"], "latency": v["latency"],
                        "lastTs": v["lastTs"], "day0": day0, "d": daily})

    # runs ----------------------------------------------------------------
    runs = []
    for r in load_rows(cfg.data_dir, "run", cutoff):
        runs.append([r["_ts"], r["VCenter"], r.get("Status") or "", as_int(r.get("Hosts")), as_int(r.get("HostsConnected")),
                     as_int(r.get("VMsTotal")), as_int(r.get("VMsOn")), as_int(r.get("Templates")), as_int(r.get("DurationSec")),
                     r.get("Message") or ""])

    vcenters = sorted(set([h["vc"] for h in host_list] + [v["vc"] for v in vm_list] + [r[1] for r in runs]))
    return {
        "schema": 4,
        "meta": {"title": cfg.title, "support": cfg.support_contact, "generatedUtc": iso(now), "fromUtc": iso(cutoff),
                 "toUtc": iso(now), "days": days, "lang": cfg.language, "candidatePct": json_num(cfg.candidate_pct),
                 "thresholdPct": json_num(cfg.threshold_pct), "tierRatio": json_num(cfg.tier_ratio),
                 "coldPct": json_num(cfg.cold_pct), "hotPct": json_num(cfg.hot_pct), "ramBoundPct": json_num(cfg.ram_bound_pct),
                 "cpuIdlePct": json_num(cfg.cpu_idle_pct), "bucketHours": bucket_hours,
                 "intervalMinutes": cfg.interval_minutes, "vcenters": vcenters, "builder": builder,
                 "failover": {"stretched": cfg.stretched_cluster, "stretchedClusters": cfg.stretched_clusters}},
        "runs": runs, "hosts": host_list, "vms": vm_list,
    }


def script_safe_json(data):
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return (text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace(u"\u2028", "\\u2028").replace(u"\u2029", "\\u2029"))


def find_template(cfg):
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [cfg.template] if cfg.template else [
        os.path.join(here, "memtier-report.template.html"),
        os.path.join(here, "..", "template", "memtier-report.template.html"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise MemTierError("report template not found (looked in: %s)" % ", ".join(c for c in candidates if c))


def cmd_report(cfg, args):
    days = args.days or cfg.days
    if args.stretched_cluster:
        cfg.stretched_cluster = True
    if args.stretched_clusters:
        cfg.stretched_clusters = [c.strip() for c in args.stretched_clusters.split(",") if c.strip()]
    for name in ("threshold_pct", "candidate_pct", "tier_ratio", "ram_bound_pct", "cpu_idle_pct",
                 "cold_pct", "hot_pct", "language", "title", "support_contact", "template"):
        value = getattr(args, name, None)
        if value is not None:
            setattr(cfg, name, value)
    write_report(cfg, days, args.output)
    return 0


def write_report(cfg, days, output=None):
    now = utcnow()
    data = build_report_data(cfg, now, days, "memtier.py %s" % VERSION)
    with io.open(find_template(cfg), encoding="utf-8") as handle:
        template = handle.read()
    if DATA_PLACEHOLDER not in template:
        raise MemTierError("template does not contain the data placeholder %s" % DATA_PLACEHOLDER)
    html = template.replace(DATA_PLACEHOLDER, script_safe_json(data), 1)

    target = os.path.abspath(output or os.path.join(cfg.report_dir, REPORT_NAME))
    ensure_dir(os.path.dirname(target))
    # Write aside and swap in, so a reader never sees a half-written report
    tmp = "%s.%d.tmp" % (target, os.getpid())
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(html)
    os.replace(tmp, target)
    LOG.info("report written: %s (%d hosts, %d VMs, %d runs, %.1f MB)", target, len(data["hosts"]), len(data["vms"]),
             len(data["runs"]), len(html.encode("utf-8")) / 1048576.0)


# ----------------------------------------------------------------------------
# setup
# ----------------------------------------------------------------------------

def prompt(text, default=""):
    answer = input("%s%s: " % (text, " [%s]" % default if default else "")).strip()
    return answer or default


def cmd_setup(args):
    path = os.path.abspath(args.config)
    base = os.path.dirname(path)
    if os.path.exists(path) and not args.force:
        print("Config %s already exists - testing it (use --force to recreate)." % path)
    else:
        print("Creating %s" % path)
        servers = ""
        while not servers:
            servers = prompt("vCenter FQDN(s), comma separated")
        username = prompt("Read-only SSO user", "svc-memtier@vsphere.local")
        password = getpass.getpass("Password (stored in the config file, empty = use MEMTIER_PASSWORD): ")
        verify = prompt("Verify TLS certificates (true/false)", "true")
        interval = prompt("Collection interval in minutes (60, 30 or 15)", "60")
        if as_int(interval) not in INTERVAL_CHOICES:
            print("  not one of 60/30/15 - using 60")
            interval = "60"
        ensure_dir(base)
        text = DEFAULT_CONFIG.format(servers=servers, username=username, password=password.replace("\n", ""),
                                     verify_tls=verify, interval_minutes=int(interval), data_dir=os.path.join(base, "data"),
                                     log_dir=os.path.join(base, "logs"), report_dir=os.path.join(base, "reports"))
        old_umask = os.umask(0o077)
        try:
            with io.open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        finally:
            os.umask(old_umask)
        os.chmod(path, 0o600)

    cfg = Config(path)
    ok = True
    for server in cfg.servers:
        client = ViJsonClient(server, cfg.username, cfg.password, cfg.verify_tls, cfg.ca_file, cfg.api_release, cfg.timeout)
        try:
            client.login()
            about = client.content.get("about", {})
            print("OK   %s - %s (API %s)" % (server, about.get("fullName", "?"), client.release))
        except Exception as exc:
            ok = False
            print("FAIL %s - %s" % (server, exc))
        finally:
            client.logout()

    python = sys.executable or "/usr/bin/python3"
    script = os.path.abspath(__file__)
    # The schedule has to match [collector] interval_minutes, otherwise consecutive windows
    # overlap (double counting) or leave gaps the hosts have already discarded.
    minute = "5" if cfg.interval_minutes >= 60 else "5-59/%d" % cfg.interval_minutes
    cron = ("# VMware memory tiering collector - installed by memtier.py setup\n"
            "SHELL=/bin/bash\n"
            '%(min)s * * * * root "%(py)s" "%(script)s" collect --config "%(cfg)s" --quiet\n') % {
        "min": minute, "py": python, "script": script, "cfg": path}
    print("\nCron entry (collect every %d min from :05, the report is rebuilt after every run):\n\n%s"
          % (cfg.interval_minutes, cron))
    if args.install_cron:
        if not ok:
            print("Not installing the cron job because the connection test failed.")
            return 1
        target = "/etc/cron.d/memtier"
        with io.open(target, "w", encoding="utf-8") as handle:
            handle.write(cron)
        os.chmod(target, 0o644)
        print("Installed %s" % target)
    return 0 if ok else 1


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    def common_options(target, suppress):
        default = (lambda value: argparse.SUPPRESS) if suppress else (lambda value: value)
        target.add_argument("--config", default=default(os.path.join(here, "memtier.ini")),
                            help="config file (default: memtier.ini next to the script)")
        target.add_argument("--verbose", action="store_true", default=default(False), help="debug logging")
        target.add_argument("--quiet", action="store_true", default=default(False), help="log to the log file only (for cron)")

    parser = argparse.ArgumentParser(description="VMware memory tiering collector and report (v%s)" % VERSION)
    common_options(parser, False)
    common = argparse.ArgumentParser(add_help=False)
    common_options(common, True)  # also accepted after the command, e.g. "collect --config x.ini"
    sub = parser.add_subparsers(dest="command")
    col = sub.add_parser("collect", parents=[common],
                         help="collect the last window of real-time memory statistics and rebuild the report")
    col.add_argument("--no-report", action="store_true", help="collect only, do not rebuild the report")
    rep = sub.add_parser("report", parents=[common], help="build the HTML trend report")
    rep.add_argument("--days", type=int, help="days of history to include (default from config)")
    rep.add_argument("--output", help="write to this file instead of the report directory")
    rep.add_argument("--stretched-cluster", action="store_true",
                     help="all clusters are stretched: capacity after a failure is one site (50%%) instead of N+1")
    rep.add_argument("--stretched-clusters", "--stretched-cluster-name", metavar="NAMES", dest="stretched_clusters",
                     help="comma-separated names of the stretched clusters")
    rep.add_argument("--threshold-pct", type=float, help="feasibility guidance: active memory, %% of DRAM (default from config)")
    rep.add_argument("--candidate-pct", type=float,
                     help="decision metric: a cluster counts as a tiering candidate at or below this %% of active over consumed memory")
    rep.add_argument("--tier-ratio", type=float, help="NVMe tier size relative to DRAM used for sizing (default 1.0 = 1:1)")
    rep.add_argument("--ram-bound-pct", type=float, help="a host is memory bound at or above this %% of DRAM consumed")
    rep.add_argument("--cpu-idle-pct", type=float, help="...and CPU idle at or below this %% CPU usage")
    rep.add_argument("--language", choices=sorted(LANGUAGES), help="report language (readers can switch it in the report)")
    rep.add_argument("--cold-pct", type=float, help="per-VM cold threshold, %% of configured memory (default from config)")
    rep.add_argument("--hot-pct", type=float, help="per-VM hot threshold, %% of configured memory (default from config)")
    rep.add_argument("--title", help="report title")
    rep.add_argument("--support-contact", help="support contact shown in the header")
    rep.add_argument("--template", help="path to memtier-report.template.html")
    st = sub.add_parser("setup", parents=[common], help="create the config, test the connection, print/install the cron job")
    st.add_argument("--force", action="store_true", help="overwrite an existing config")
    st.add_argument("--install-cron", action="store_true", help="write /etc/cron.d/memtier")
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    if args.command == "setup":
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
        try:
            return cmd_setup(args)
        except (MemTierError, KeyboardInterrupt) as exc:
            print("setup aborted: %s" % exc)
            return 1

    try:
        cfg = Config(args.config)
    except (MemTierError, configparser.Error) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    setup_logging(cfg.log_dir, args.command, args.verbose)
    if args.quiet and cfg.log_dir:
        for handler in list(LOG.handlers):
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.handlers.RotatingFileHandler):
                LOG.removeHandler(handler)
    try:
        if args.command == "collect":
            return cmd_collect(cfg, args)
        return cmd_report(cfg, args)
    except MemTierError as exc:
        LOG.error("%s", exc)
        return 1
    except Exception:
        LOG.exception("unexpected error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
