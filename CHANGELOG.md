# Changelog

Container and Home Assistant app releases use `YY.MM.NN` (tag `vYY.MM.NN`), where
`NN` counts releases within the month. The PowerShell and Python scripts are
attached to every release.

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
