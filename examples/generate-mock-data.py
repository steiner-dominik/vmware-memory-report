#!/usr/bin/env python3
"""
Generate mock collector data (same CSV format as the collectors) for demos and tests.

    python3 examples/generate-mock-data.py examples/mock-data --days 45
    python3 python/memtier.py report --config examples/mock.ini

No vCenter needed. All names are fictitious (example.com).

Environment modelled:
  vcenter01.example.com
    Metro-Stretched  8 hosts, 2 sites, 1024 GB  low active, consumed ~58% -> fits N+1, not a site failure
    Compute          4 hosts,  768 GB          busy, nightly batch peaks above the 50% active guidance
    VDI              4 hosts,  512 GB + 512 GB NVMe tier (tiering on, per-tier counters as on vSphere 9)
  vcenter02.example.com
    Branch           2 hosts,  384 GB          memory full, CPU idle -> tier instead of a new host
    (standalone)     1 witness host, 64 GB
"""

import argparse
import datetime as dt
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "python"))
import memtier as mt  # noqa: E402  (CSV schema and helpers shared with the collectors)

CLUSTERS = [
    # vcenter, cluster, hosts, DRAM GB, NVMe GB, active load, consumed load, host prefix, cores, CPU load
    ("vcenter01.example.com", "Metro-Stretched", 8, 1024, 0, 0.16, 0.58, "esx-metro", 64, 0.35),
    ("vcenter01.example.com", "Compute", 4, 768, 0, 0.34, 0.62, "esx-comp", 48, 0.62),
    # Tiering already on: consumed is above DRAM, so part of it is served by the NVMe tier.
    ("vcenter01.example.com", "VDI", 4, 512, 512, 0.22, 1.45, "esx-vdi", 32, 0.30),
    # Memory full, CPU idle: the case where a tier buys capacity instead of another host.
    ("vcenter02.example.com", "Branch", 2, 384, 0, 0.12, 0.82, "esx-branch", 24, 0.14),
    ("vcenter02.example.com", "(standalone)", 1, 64, 0, 0.05, 0.20, "esx-witness", 8, 0.05),
]
VM_PREFIXES = {"Metro-Stretched": ["app", "db", "web", "erp", "mq"], "Compute": ["batch", "sql", "etl", "cache"],
               "VDI": ["vdi-pool-a", "vdi-pool-b"], "Branch": ["fs", "print", "dc"], "(standalone)": ["witness"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    rnd = random.Random(args.seed)
    os.makedirs(args.out_dir, exist_ok=True)
    for name in os.listdir(args.out_dir):
        if name.endswith((".csv", ".csv.gz")):
            os.remove(os.path.join(args.out_dir, name))

    end = mt.utcnow().replace(minute=5, second=0) - dt.timedelta(hours=1)
    start = end - dt.timedelta(days=args.days)

    hosts, vms = [], []
    for vc, cluster, count, dram_gb, nvme_gb, active, consumed, prefix, cores, cpu in CLUSTERS:
        for i in range(count):
            hosts.append({"vc": vc, "cluster": cluster, "name": "%s-%02d.example.com" % (prefix, i + 1),
                          "id": "host-%d" % (1000 + len(hosts)), "dram": dram_gb * 1024, "nvme": nvme_gb * 1024,
                          "tier": "softwareTiering" if nvme_gb else "noTiering", "cores": cores,
                          "cpu": cpu * rnd.uniform(0.9, 1.1),
                          "active": active * rnd.uniform(0.85, 1.25), "consumed": consumed * rnd.uniform(0.95, 1.05)})
    for h in hosts:
        prefixes = VM_PREFIXES[h["cluster"]]
        # assigned memory is usually above consumed; a tier lets consumed exceed DRAM
        budget = min(h["dram"] + h["nvme"], h["dram"] * h["consumed"]) * 1.1
        while budget > 0:
            size = rnd.choice([4096, 8192, 8192, 16384, 16384, 32768, 65536])
            if h["cluster"] == "VDI":
                size = rnd.choice([4096, 8192])
            if h["cluster"] == "(standalone)":
                size = 16384
            prefix = rnd.choice(prefixes)
            vms.append({"vc": h["vc"], "host": h, "name": "%s-%03d" % (prefix, len(vms) + 1), "id": "vm-%d" % (5000 + len(vms)),
                        "assigned": size, "base": rnd.betavariate(1.2, 4.0), "batch": prefix in ("batch", "etl"),
                        "vdi": prefix.startswith("vdi"), "balloon": rnd.random() < 0.01})
            budget -= size
            if h["cluster"] == "(standalone)":
                break

    buf = {}
    ts = start
    outage = (start + dt.timedelta(days=args.days * 0.6)).replace(hour=2)
    while ts <= end:
        if outage <= ts < outage + dt.timedelta(hours=5):  # collector outage, visible in "Collector runs"
            ts += dt.timedelta(hours=1)
            continue
        local_hour = (ts.hour + 2) % 24
        weekday = ts.weekday() < 5
        business = 1.35 if weekday and 8 <= local_hour <= 18 else 0.85
        month = mt.month_key(ts)
        per_host = dict((h["id"], [0, 0]) for h in hosts)
        vm_rows = []
        for v in vms:
            h = v["host"]
            f = v["base"] * business
            if v["batch"]:
                f = 0.7 if 1 <= local_hour <= 4 else 0.08
            if v["vdi"]:
                f = (0.45 if weekday and 7 <= local_hour <= 17 else 0.05) * (0.6 + v["base"])
            f = min(0.97, f * rnd.uniform(0.9, 1.1))
            avg = int(v["assigned"] * f)
            per_host[h["id"]][0] += 1
            per_host[h["id"]][1] += v["assigned"]
            vm_rows.append({
                "Timestamp": mt.iso(ts), "WindowStart": mt.iso(ts - dt.timedelta(hours=1)), "VCenter": v["vc"], "Cluster": h["cluster"],
                "VMHost": h["name"], "VM": v["name"], "VMId": v["id"], "AssignedMB": v["assigned"], "ReservationMB": 0,
                "LatencySensitivity": "normal", "Samples": 180, "ActiveAvgMB": avg, "ActiveP95MB": int(min(v["assigned"], avg * 1.18)),
                "ActiveMaxMB": int(min(v["assigned"], avg * 1.4)), "ConsumedAvgMB": int(v["assigned"] * min(0.98, 0.55 + f)),
                "ConsumedMaxMB": int(v["assigned"] * min(1.0, 0.6 + f)),
                "BalloonMaxMB": 256 if v["balloon"] and local_hour == 10 else 0, "SwappedMaxMB": 0})
        drift = 1 + (ts - start).days * 0.004  # slow growth over the period
        host_rows = []
        for h in hosts:
            a = h["dram"] * h["active"] * business * drift * rnd.uniform(0.93, 1.07)
            if h["cluster"] == "Compute" and 1 <= local_hour <= 4:
                a *= 1.45  # nightly batch window
            a = min(a, h["dram"] * 0.95)
            # Consumed can exceed DRAM only where a tier backs it, never the total memory.
            consumed_mb = int(min(h["dram"] + h["nvme"], h["dram"] * h["consumed"] * drift))
            cpu_pct = min(98.0, h["cpu"] * business * 100.0 * rnd.uniform(0.9, 1.1))
            host_rows.append({
                "Timestamp": mt.iso(ts), "WindowStart": mt.iso(ts - dt.timedelta(hours=1)), "VCenter": h["vc"], "Cluster": h["cluster"],
                "VMHost": h["name"], "HostId": h["id"], "ConnectionState": "connected", "MaintenanceMode": "false",
                "TieringType": h["tier"], "PhysicalMB": h["dram"] + h["nvme"], "DramMB": h["dram"], "NvmeTierMB": h["nvme"],
                "VMsOn": per_host[h["id"]][0], "AssignedMB": per_host[h["id"]][1], "Samples": 180,
                "ActiveAvgMB": int(a), "ActiveP95MB": int(min(h["dram"], a * 1.1)), "ActiveMaxMB": int(min(h["dram"], a * 1.25)),
                "ConsumedAvgMB": consumed_mb, "ConsumedMaxMB": int(min(h["dram"] + h["nvme"], consumed_mb * 1.03)),
                "BalloonMaxMB": 0, "SwapUsedMaxMB": 0,
                "CpuCores": h["cores"], "CpuThreads": h["cores"] * 2, "CpuMhz": 2600,
                "CpuAvgPct": mt.round1(cpu_pct), "CpuP95Pct": mt.round1(min(99.0, cpu_pct * 1.2)),
                "CpuMaxPct": mt.round1(min(100.0, cpu_pct * 1.45)),
                # Only the tiered cluster reports these: they exist from vSphere 9 onwards, and
                # everywhere else the report derives the split instead.
                "TierDramMB": min(consumed_mb, h["dram"]) if h["nvme"] else None,
                "TierNvmeMB": max(0, consumed_mb - h["dram"]) if h["nvme"] else None})
        runs = []
        for vc in sorted(set(h["vc"] for h in hosts)):
            vc_vms = [v for v in vms if v["vc"] == vc]
            runs.append({"Timestamp": mt.iso(ts), "VCenter": vc, "Status": "ok", "Hosts": sum(1 for h in hosts if h["vc"] == vc),
                         "HostsConnected": sum(1 for h in hosts if h["vc"] == vc), "VMsTotal": len(vc_vms) + len(vc_vms) // 6,
                         "VMsOn": len(vc_vms), "VMsOff": len(vc_vms) // 6, "VMsSuspended": 0, "Templates": 12, "VMsExcluded": 0,
                         "VMsWithoutStats": 0, "HostsWithoutStats": 0, "DurationSec": int(rnd.uniform(3, 9)), "Message": ""})
        for kind, rows in (("host", host_rows), ("vm", vm_rows), ("run", runs)):
            buf.setdefault((kind, month), []).extend(rows)
        ts += dt.timedelta(hours=1)

    for (kind, month), rows in sorted(buf.items()):
        fields = {"host": mt.HOST_FIELDS, "vm": mt.VM_FIELDS, "run": mt.RUN_FIELDS}[kind]
        mt.append_csv(os.path.join(args.out_dir, "%s-%s.csv" % (kind, month)), fields, rows)
    print("mock data: %d hosts, %d VMs, %d days -> %s" % (len(hosts), len(vms), args.days, args.out_dir))


if __name__ == "__main__":
    main()
