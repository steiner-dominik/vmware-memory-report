# Changelog

Container and Home Assistant app releases use `YY.MM.NN` (tag `vYY.MM.NN`), where
`NN` counts releases within the month. The PowerShell and Python scripts are
attached to every release.

## 26.09.09

Verdicts that agree with themselves.

- **Usable extra memory keeps the hot set within the limit.** Extra memory is filled by more of
  the same workload, so the hot set grows with it. The retrofit figure is now also capped at what
  keeps active P95 at or below 50% of DRAM, next to the CPU cap. On the README example the
  promise drops from 336 GB, which would have pushed the hot set to 202 GB of 384 GB DRAM, to
  303 GB.
- **Standalone hosts are sized.** A host outside any cluster is its own group of one. Before, a
  fleet of standalone hosts showed *No workload* and "too much of the memory is hot" for new
  servers next to a *Strong candidate* verdict.
- **The New servers badge follows the clusters that have a saving.** One large hot cluster no
  longer turns a real saving on the other clusters into *Little to gain*, and the
  *Active P95 ÷ assigned* figure beside it now covers the same clusters as the headline.
- **A saving of nothing is not a *Full saving*.** Heavily overcommitted hosts that already have no
  more DRAM than assigned ÷ (1 + ratio) are *Little to gain*, with the measured best case named.
- The "same figure" note no longer credits the hot set when DIMM rounding is what makes the
  conservative and measured sizing meet; *No workload* and *No data* get their own text.
- Memory used is measured against the DRAM and NVMe tier each host had in each interval, so a
  host that got its tier mid-range is not read as tiered all along.
- The snapshot reports active ÷ consumed as average over average, the same metric as the trend
  report (it divided active P95 by average consumed, which read systematically hotter).

### Fixes

- Performance queries end at the run's timestamp. A query that ran late (large inventories, or a
  second vCenter after a slow first one) reached into the next run's window, and those samples
  were counted twice.
- The container could corrupt the report when a rebuild at startup and a collection wrote it at
  the same time: both used the same temporary file.
- Home Assistant: `sensor.*_cold_in_dram` and `sensor.*_active_of_consumed` showed *unknown*
  instead of 0.
- PowerShell: the collector and the installer pass `RamBoundPct` and `CpuIdlePct` (and the
  installer `TierRatio`, `ColdPct`, `HotPct`) to the report; before, every scheduled run rebuilt it
  with the defaults. `Days` is limited to 400 as in the Python edition and the app.
- PowerShell: the per-tier counter recognises the DRAM tier by the name the host reports, as the
  Python edition does, and CSV numbers round half up in both editions.
- Python: an invalid or out-of-range number in `memtier.ini` (e.g. `threshold_pct = 0`, which
  divided by zero in the sizing) warns and falls back to the default instead of breaking the
  report; command-line thresholds are checked against the same ranges.
- `env.example` lists `MEMTIER_RAM_BOUND_PCT` and `MEMTIER_CPU_IDLE_PCT`.
- The Retrofit column of *Clusters: the three questions* sorts.
- A single-host group reads "1 host", not "1 hosts".
- New screenshots of the mock reports; the failover screenshot showed a section removed in 26.09.08.

## 26.09.08

Two buying decisions, three views.

- **Three views: Summary, Simple, Expert.** *Summary* is the page for the customer: the
  verdict, the two buying decisions and where the memory goes. *Simple* (the default) is the
  decision board. *Expert* replaces "Everything" and adds a data-quality panel.
- **One verdict per buying decision.** *New servers* (less DRAM plus an NVMe tier) and
  *Existing hosts* (add an NVMe tier) each get their own verdict, headline number and the
  two figures that decide it. Before, one "strong candidate" had to answer both.
- **Half-DRAM test: active P95 ÷ assigned.** At or below 50% ÷ (1 + tier ratio) - 25% at
  1:1 - DRAM can shrink to assigned ÷ (1 + ratio) and still hold the hot set (*Full
  saving*); above it the hot set sets the DRAM (*Partial saving*).
- **The DRAM saving is conservative.** It is sized for `max(assigned P95, consumed peak)`,
  so it still holds once consumed memory has grown into what the VMs are assigned - which it
  keeps doing for weeks, because ESXi does not reclaim touched memory without pressure. The
  measured figure (consumed only, the previous headline) is shown next to it.
- **Hosts that already run a tier are left out of the new-server saving.** They have made
  their DRAM saving; counting them again promised it twice.
- **Extra memory is gated by CPU.** The retrofit case uses sustained CPU - the P95 of the
  per-interval averages - instead of the P95 of the per-interval peaks, which one busy day
  could push over the line. "Usable extra memory" counts only hosts whose memory is full
  while their CPU idles, capped at what that CPU can run up to 80% sustained. The old "Extra
  memory" figure is kept as "Extra on paper" in the Expert view.
- **New sections:** *Where the memory goes* (assigned, consumed, active against DRAM, with
  days collected and whether consumed is still rising), *Clusters: the three questions*,
  *Which runs out first: memory or CPU?* (every host by memory used and sustained CPU) and
  *Data quality*.
- **Clusters are ranked by cold memory**, not by ratio. A cluster with no powered-on VM is
  listed last as *No workload* instead of first as *Strong candidate*.
- The host table is grouped by the question each column answers.

### Fixes

- The report header showed "Support:" with nothing after it.
- The host table showed the raw vSphere value (`noTiering`, `softwareTiering`) as the
  tiering type.
- VMs reporting more active than consumed memory (vSphere Pods do) are marked *Unclear* and
  left out of the Cold / Warm / Hot counts instead of filling the "Hot" list.

No collector or CSV change: the report payload stays schema 4, and reports from older data
render with the new views.

## 26.09.07

Memory tier usage is readable after all - from vSphere 9.

- **`mem.tier.consumed.latest` is collected where it exists.** vCenter 9.1 publishes it
  (level 2, so it arrives with the default statistics settings), keyed per instance by the
  tier's own name - `DRAM`, `NVMe` - in MB. vCenter 8.0 U3 does not publish it, or any
  other counter with "tier" in its name, which is why the discovery in 26.09.04 came back
  empty against an 8.0 U3 host that *does* run a tier.
- **Where it exists, the DRAM/NVMe split is measured instead of derived.** "Cold in DRAM"
  becomes `tier DRAM consumed - active` rather than `min(consumed, DRAM) - active`, and
  "on NVMe" is read rather than inferred. Older vCenters keep the derived estimate, which
  is close: on a 9.1 host the measured DRAM figure matched `mem.consumed.average` to within
  2 MB of 142 GB.
- **The NVMe tier switch starts from the hardware.** Hosts that already run a tier know
  their own ratio, so the switch defaults to it (snapped to the nearest offered value)
  rather than to the configured one. Only hosts that actually have a tier are counted,
  since averaging tiered and untiered hosts produces a ratio nobody is running.
- New host CSV columns `TierDramMB` and `TierNvmeMB`, empty before vSphere 9.

### Verified against real vCenters

This release was tested against vCenter 8.0.3 (11 hosts, 521 VMs) and vCenter 9.1.1
(6 hosts, 73 VMs), with both editions:

| | 8.0.3 | 9.1.1 |
|---|---|---|
| counters offered | 749 | 875 |
| counters matching "tier" | none | `mem.tier.consumed.latest`, `mem.tier.size.latest` |
| `cpu.usage.average` | yes | yes |
| `TierDramMB` / `TierNvmeMB` | empty, derived instead | measured |

`mem.tier.size.latest` is a level 4 counter, so it is not collected by default and returns
nothing; tier sizes keep coming from `hardware.memoryTierInfo`, which every 8.0 U3 and later
host reports.

### Not reachable remotely

`vsish -e get /memory/tiers/N/info` and `memstats -r vmtier-stats` give per-tier free space
and per-VM tier residency, but both are ESXi shell tools rather than esxcli namespaces, so
neither is reachable through vCenter. Collecting them would mean SSH to every host, which
this project deliberately does not do. `esxcli system tierdevice list` *is* reachable through
the vim25 EsxCLI objects but only names the backing device, not its usage.

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
