# Changelog

Container and Home Assistant app releases use `YY.MM.NN` (tag `vYY.MM.NN`), where
`NN` counts releases within the month. The PowerShell and Python scripts are
attached to every release.

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
