# Changelog

Container and Home Assistant app releases use `YY.MM.NN` (tag `vYY.MM.NN`), where
`NN` counts releases within the month. The PowerShell and Python scripts are
attached to every release.

## 26.09.06

Host CPU, and the case where a tier replaces a purchase.

- **CPU usage and core counts are collected per host.** A host whose memory is full
  while its CPUs idle gains capacity from an NVMe tier instead of from another socket -
  the strongest "buy a tier, not a host" signal there is. New figure "Tier instead of a
  new host", a badge in the host table, and CPU P95 and core columns. Thresholds are
  `ram_bound_pct` (default 70% of DRAM consumed) and `cpu_idle_pct` (default 50% CPU P95).
- A host that is memory bound *and* CPU bound is deliberately not flagged: on the mock
  fleet the Branch hosts (memory 95-100%, CPU 23%) are flagged and the Compute hosts
  (memory 73%, CPU 99%) are not, because those need a host rather than a tier.
- **The CPU counter is optional.** A vCenter that does not publish `cpu.usage.average`
  still produces the full memory report, with the CPU columns left empty and a log line
  saying so.

### Memory tier usage: the answer is no, at least on 8.0 U3

The discovery added in 26.09.04 has run against a vCenter with tiering enabled. It
enumerated 734 performance counters and **none** of them has "tier" in its name, so
per-tier usage is not available through the performance API. `hardware.memoryTierInfo`
still gives the tier *sizes*, which is what the report uses. Reading current usage means
esxcli, which is per-host and reachable from both editions - the vim25 `EsxCLI` managed
objects are not PowerCLI-only. Not implemented yet, pending the exact namespace.

### Compatibility

- Monthly host CSV files gain `CpuCores`, `CpuThreads`, `CpuMhz`, `CpuAvgPct`, `CpuP95Pct`
  and `CpuMaxPct`, and are widened in place on the first run after the update.
- The report payload is schema 4. Both builders still emit identical payloads for the
  mock fleet, CPU columns included.

## 26.09.05

Fixes the blank report shipped in 26.09.04.

- **The report rendered as empty cards.** Removing the failover section in 26.09.04 left
  two references to elements that went with it (`lgThr2`, `lgMetric2`). In a browser those
  return `null`, and setting a property on `null` throws - before the first render, so the
  whole page stayed empty. Both references are gone.
- **The headless render harness was what let this through.** Its DOM shim invented an
  element for any id asked of it, so the removed elements still "existed" in the test. It
  now returns `null` for anything the markup does not declare, exactly as a browser does,
  and reproduces the crash. A separate test fails the build if any `$("id")` in the script
  has no matching `id=` in the markup.
- **Tier counter discovery looks at every counter group**, not only `mem.*`. A narrow filter
  would report "none available" when the counters were simply filed elsewhere. When nothing
  matches, the log now also says how many counters the vCenter offered, so an empty result
  can be told apart from a failed lookup.

## 26.09.04

Everything in the report now has to earn its place by answering a tiering question.

- **The "cluster failover headroom" section is gone.** It compared consumed memory with
  the capacity surviving a failure, which is ordinary HA admission control - vCenter
  answers that better, and it has nothing to do with tiering.
- **What replaced it is the question that *is* tiering-specific**: after a host or a site
  is lost, the same hot working set lands on fewer hosts, so it has to fit less DRAM. A
  cluster sized right at the 50% limit crosses it the moment a host dies, and the failure
  mode is the tier serving hot pages while the cluster is already degraded. Now a badge
  and a figure, computed per cluster from its own failover model.
- **Stretched clusters became a sizing input** rather than a section. The new "failure to
  survive" switch decides whether the sizing reserves capacity for a failure; it defaults
  to none, because folding a reserve in by default quietly turns every saving into zero.
- **The heatmap moved to active over consumed memory**, the metric the decision is made
  on, with bins straddling the candidate threshold. Against DRAM it mostly showed when
  the hosts were busy, which is a different question.
- **DRAM is sized in DIMMs.** The sizing rounds up to a population that can actually be
  ordered - 16, 32, 48, 64, 96, 128 and 256 GB modules, up to 48 per host - and names
  one, e.g. `20 x 32 GB`. On the mock fleet that turns 4.07 TB of theoretical saving into
  3.50 TB of buildable saving. For equal totals it prefers more, smaller modules, because
  populating every channel is what gives the bandwidth.
- **The candidate bars show a "DRAM after tiering" line**, so the decision is one glance:
  the active bar has to sit well under it.
- **Memory tier counters are discovered and reported.** How much a host currently keeps on
  its NVMe tier is not in the inventory - `memoryTierInfo` only gives the tier sizes - and
  the per-tier performance counters have moved between vSphere releases. The collector now
  lists whatever `mem.*tier*` counters its vCenter publishes, records them in the run row
  and exposes them on `sensor.<prefix>_status`, so the next release can collect the right
  ones instead of guessing.

### Compatibility

- Monthly CSV files gain a `TierCounters` column. A file whose header is a prefix of the
  current one is now **widened in place** instead of refused, so an upgrade no longer
  strands the running month. A header that is not a prefix is still refused. Both editions
  do this identically.

## 26.09.03

The report was showing everything it knows at once. It now opens on the decision and
keeps the rest one click away.

- **Simple view is the default.** A one-line verdict ("Strong candidate - 90% of the
  memory your hosts back is cold"), four figures and two tables. The host and cluster
  charts, failover headroom, the heatmap and the per-host and per-VM tables move behind
  an **Everything** switch, remembered per browser. Simple view also skips building
  them, which is the expensive part on a large fleet.
- **Simulate the NVMe tier size**: 50%, 100% (the 1:1 default), 200% or 400% of DRAM,
  the way it is configured on the host. It moves the sizing numbers only - whether a
  cluster is a candidate is active over consumed memory, which does not depend on the
  ratio. `tier_ratio` still sets where the switch starts.
- The wording around the ratio changed from "1:1.5" to "150% of DRAM" throughout, to
  match how the setting is expressed on the host.

### Fixes

- The verdict banner used `classList.add()` with a possibly empty token, which throws
  in the browser. Caught before release by the new headless render test.

## 26.09.02

The report now leads with the metric the tiering decision is actually made on, and
turns it into the two numbers you buy hardware with.

- **Active vs. consumed memory is the headline.** Of the memory the hosts really
  back the VMs with, how much is hot? Everything else is cold and is exactly what an
  NVMe tier moves out of DRAM. At or below `candidate_pct` (default 40%) a cluster
  is a tiering candidate. Clusters, hosts and the KPI row are ordered by it.
- **Active as a share of DRAM was demoted to what it is:** a feasibility check.
  A host can sit at 12% of DRAM simply because it is empty, which says nothing about
  how much cold memory there is to move. It is still shown, per host and per cluster,
  as "hot set fits DRAM" / "DRAM-bound today", and it still limits tiering on hardware
  you already own.
- **New sizing section** for the two cases where tiering pays off:
  - buying new hardware: `DRAM needed` = max(active P95 / hot-set limit, consumed peak /
    (1 + tier ratio)), and `DRAM saved` against what the cluster has today;
  - extending hosts you own: `Capacity with a tier` and the `Extra memory` it frees up,
    reported as zero for clusters whose hot set already needs the DRAM.
- **Hosts that already have a tier are no longer double counted.** Cold memory is
  capped at DRAM (`min(consumed, DRAM) - active`); what sits above DRAM is shown
  separately as "on NVMe". Reporting all of `consumed - active` promised the same
  saving twice on exactly those hosts. The same fix landed in the snapshot report.
- **How these numbers are calculated**: a diagram in the report (and in the README)
  that walks through one host, both buying cases and the already-tiered case.
- **The report is translatable**, like the Home Assistant panel: English and German,
  switchable in the report, remembered per browser, with matching number and date
  formats. `language` sets the default.
- **Collection every 60, 30 or 15 minutes** (`interval_minutes`). A shorter interval
  does not find peaks an hourly run misses - every run already reduces all 20-second
  samples of its window to average, P95 and max - but it gives a finer time resolution
  and a failed run costs 15 minutes of history instead of an hour. The cron entry, the
  Windows task and the container schedule follow the setting.
- Two more Home Assistant entities: `sensor.<prefix>_active_of_consumed` and
  `sensor.<prefix>_cold_in_dram`.

### Fixes

- Per-VM day weights are now the minutes actually covered, derived from the 20-second
  sample count. They used to count rows, which would have quadrupled a VM's weight at a
  15-minute interval.
- Consumed memory per host was averaged over the DRAM of *all* intervals, including
  those that reported no consumed value, which understated it.
- The "Configured" bar in the candidates chart showed the last interval's value while
  the legend said P95; it is a P95 now, like the other two bars.
- The fleet "cold memory in DRAM" figure was divided by the number of host-intervals
  instead of the number of intervals, so it showed a per-host average under a fleet label.
- Run coverage ("x% of expected slots") is calculated against the configured interval
  instead of assuming hourly runs.
- The default support contact is the project's issue tracker, not a personal address.
- `compose.yaml` referred to `env.example`; in the repository the file is `.env.example`.
- The NVMe tier size is carried per interval, so a host that gets a tier mid-range is
  not treated as tiered for the whole period.

## 26.09.01

First container release: the Python collector packaged as a Docker image and a
Home Assistant app, for an easy install in a homelab. Collection and the report
are exactly those of `memtier.py`; the scripts keep working on their own.

- **Hourly collection without cron.** The first run starts as soon as the vCenter
  connection is configured, then every hour. Each run covers the time since the
  previous one, so a restart or a manual run neither double counts nor skips
  samples.
- **Web interface**: the trend report, the status of the last collection per
  vCenter, the run history, CSV downloads and the running configuration (secrets
  shown only as set/not set). English and German.
- **Home Assistant app** with ingress, the Supervisor watchdog and health entities
  written to the Core API: collector status, last collection, hosts, powered-on
  VMs and the busiest host's active memory as % of DRAM.
- **Standalone Docker** configured through `MEMTIER_*` environment variables, with
  a `compose.yaml` that runs as a non-root user on a read-only root filesystem and
  optional basic authentication for the web interface.
- The report is rebuilt at startup, so changed thresholds or stretched clusters
  apply immediately.
- Multi-arch image for `amd64`, `arm64` and `armv7`, with SBOM and build
  provenance.
