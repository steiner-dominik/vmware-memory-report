# vmware-memory-report

Analyzes long-term active vs. consumed memory of VMware hosts, clusters and VMs to help plan **NVMe memory tiering** (vSphere 8.0 U3 / VCF 9) and its DRAM:NVMe ratio.

It answers two questions over weeks instead of one snapshot:

* **Which clusters are worth tiering?** The **Memory tiering candidates** section at the top of the report ranks every cluster by the gap between *active*, *consumed* and *configured* memory. Configured memory is what the VMs were given, consumed is what the hosts actually back with memory, active is what has to stay in DRAM. The distance between active and consumed is memory that is resident but cold — exactly what an NVMe tier absorbs. The bigger that gap, the better the candidate.
* **Does it fit?** Does the active memory of each host, and of each cluster after a failure, fit into DRAM? Broadcom's guidance for memory tiering with the default 1:1 DRAM:NVMe ratio is to keep active memory at or below **50% of DRAM**.

Memory sizes switch between **GB and TB** (*Auto* picks TB from 2 TB of cluster DRAM upwards).

![Trend report (mock data)](docs/images/trend-report.png)

<sub>All screenshots and example reports use mock data (`example.com`). Open [`examples/mock-trend-report.html`](examples/mock-trend-report.html) or [`examples/mock-snapshot-report.html`](examples/mock-snapshot-report.html) locally to try the interactive reports.</sub>

## Contents

```
powershell/                     Windows (or any OS with PowerShell 7 + PowerCLI)
  Get-MemTierSnapshot.ps1         one-off snapshot report (last hour)
  Invoke-MemTierCollector.ps1     hourly collector
  New-MemTierReport.ps1           trend report builder
  Install-MemTierTasks.ps1        one-time setup of two Windows scheduled tasks
  MemTier.Common.ps1              shared helpers
python/
  memtier.py                      collector + trend report + setup, Python 3.6+, standard library only
                                  (runs directly on the vCenter Server Appliance)
template/
  memtier-report.template.html    trend report layout (self-contained, no CDN)
  memtier-snapshot.template.html  snapshot report layout
examples/
  generate-mock-data.py           mock collector data, no vCenter needed
  New-MockSnapshotReport.ps1      snapshot report against a mocked PowerCLI inventory
  mock-trend-report.html          example trend report (mock data)
  mock-snapshot-report.html       example snapshot report (mock data)
```

Both editions write **the same CSV files** and embed **the same data** in the report (verified to be identical). You can collect with one and build the report with the other.

## Try it with mock data

No vCenter needed:

```bash
python3 examples/generate-mock-data.py examples/mock-data
python3 python/memtier.py report --config examples/mock.ini --output mock-report.html
```

or with PowerShell:

```powershell
pwsh ./powershell/New-MemTierReport.ps1 -DataDir ./examples/mock-data -StretchedClusterName Metro-Stretched -OutputPath ./mock-report.html
```

The mock environment has a stretched metro cluster. It fits a single host failure but not a site failure, which demonstrates the stretched-cluster mode.

## Stretched clusters (site failover)

In a stretched cluster (vSAN stretched cluster or metro storage cluster, very common in Europe), a whole site can fail. The surviving site must then carry every VM, so **at most 50% of the cluster's memory may be used**. The usual N+1 check (cluster without its largest host) is far too optimistic for these clusters.

The reports calculate both failure models:

| | N+1 (default) | Stretched |
|---|---|---|
| Capacity after a failure | cluster minus its largest host | one site = 50% of the cluster |
| Tiering check | active P95 as % of surviving DRAM ≤ tiering guidance (50%) | same, against 50% of cluster DRAM |
| Capacity check | consumed P95 as % of surviving memory (DRAM + NVMe tier) ≤ 100% | consumed ≤ 50% of cluster memory |

Enable it:

| | All clusters stretched | Only some clusters |
|---|---|---|
| `New-MemTierReport.ps1`, `Get-MemTierSnapshot.ps1` | `-StretchedCluster` | `-StretchedClusterName Metro-A,Metro-B` |
| `memtier.py report` | `--stretched-cluster` or `stretched_cluster = true` | `--stretched-clusters "Metro-A,Metro-B"` or `stretched_clusters = Metro-A, Metro-B` |

The **Cluster failover** selector in the report switches between *N+1*, *Stretched* and *As configured* without rebuilding.

![Cluster failover headroom (mock data)](docs/images/cluster-failover.png)

The model assumes two symmetric sites; the witness host is not counted.

## Quick look: snapshot report

`Get-MemTierSnapshot.ps1` gives a picture of the current state. Run it interactively, no scheduling needed:

```powershell
.\Get-MemTierSnapshot.ps1 -VCenterServer vcenter01.example.com
.\Get-MemTierSnapshot.ps1 -VCenterServer vcenter01.example.com,vcenter02.example.com -Credential (Get-Credential) -ExportDir C:\Reports -StretchedClusterName Metro-A
```

It writes `VMware_Memory_Tiering_Snapshot_<date>.html`, `..._VMs.csv` and `..._Hosts.csv`, and prints a host and cluster summary to the console.

* **Last hour, not one sample:** values are avg / P95 / max over the last 60 minutes of 20-second samples (`-WindowMinutes`), not a single QuickStats reading.
* **Hosts and clusters:** each host's active memory is compared with its physical DRAM, and each cluster is checked after a failure (N+1 or site failover). Hosts, clusters and VMs all carry *active / consumed*, the ratio that decides whether tiering has anything to move.
* **All VMs:** every VM is listed, including powered-off ones. Templates are excluded and counted separately.
* **Excel-ready CSVs:** UTF-8 with BOM and the locale's list separator; `;` whenever that separator is also the decimal separator.
* **`-PassThru`:** returns the VM objects for further processing.

![Snapshot report (mock data)](docs/images/snapshot-report.png)

## How the long-term collection works

```
every hour ──► collector ──► data/host-2026-09.csv   one row per host  per run
                         ──► data/vm-2026-09.csv     one row per VM    per run
                         ──► data/run-2026-09.csv    one row per vCenter per run (collector health)
daily     ──► report    ──► reports/MemTier_Report_2026-09-15_0630.html  (+ MemTier_Report_latest.html)
```

* **Real-time samples:** each run reads the last 60 minutes of 20-second samples. ESXi hosts keep these for about an hour, independent of the vCenter statistics level. At the default level 1, vCenter's historical rollups do **not** contain active memory, so this is the only way to get it without changing vCenter settings.
* **Hourly values:** samples are reduced to average, P95 and maximum per hour for active and consumed memory, plus maximum balloon and swap values. About 180 samples per hour means short peaks are not lost.
* **Counters:** `mem.active`, `mem.consumed`, `mem.vmmemctl` (balloon), `mem.swapped` (VM) / `mem.swapused` (host).
* **Tier sizes:** DRAM and NVMe tier sizes come from `hardware.memoryTierInfo` (vSphere 8.0 U3 or later). On older hosts, `hardware.memorySize` is used as DRAM.
* **Read-only:** a read-only vCenter role is sufficient.

## Option A – PowerShell (Windows jump host / management server)

Requirements: PowerCLI 13.x or later (VCF.PowerCLI), Windows PowerShell 5.1 or PowerShell 7, and a read-only vCenter account (e.g. `svc-memtier@vsphere.local`).

1. Copy the `powershell` and `template` folders to the server, keeping them side by side (e.g. `D:\MemTier\scripts\powershell` and `D:\MemTier\scripts\template`).
2. Open an **elevated** PowerShell **as the Windows account that will run the tasks**. Only that account can decrypt the saved credential (DPAPI):
   ```powershell
   runas /user:DOMAIN\svc-memtier powershell.exe
   ```
3. Run the installer:
   ```powershell
   .\Install-MemTierTasks.ps1 -VCenterServer vcenter01.example.com,vcenter02.example.com -BaseDir D:\MemTier -CompressOldMonths
   ```
   The installer does the following:
   * sets the PowerCLI user configuration (CEIP off, certificate handling), so unattended runs never prompt
   * saves the vCenter credential to `D:\MemTier\vc-cred.xml` (DPAPI-encrypted, never commit it)
   * tests the connection to each vCenter
   * registers **MemTier Collector** (hourly at :05) and **MemTier Report** (daily at 06:30)

   Registering the tasks asks once for the Windows password of the account; Task Scheduler stores it. For stretched clusters, add `-StretchedCluster` or `-StretchedClusterName Metro-A,Metro-B`; they are passed on to the report task.
4. Test right away:
   ```powershell
   Start-ScheduledTask -TaskName 'MemTier Collector'
   Get-Content D:\MemTier\logs\memtier-collect-*.log -Tail 20
   ```

Manual/interactive use (re-uses an existing `Connect-VIServer` session if no credential is given):

```powershell
.\Invoke-MemTierCollector.ps1 -VCenterServer vcenter01.example.com -DataDir D:\MemTier\data
.\New-MemTierReport.ps1 -DataDir D:\MemTier\data -ReportDir D:\MemTier\reports -Days 30 -StretchedClusterName Metro-A
```

| Collector parameter | Default | |
|---|---|---|
| `-WindowMinutes` | 60 | must match the schedule interval, max. 60 |
| `-ExcludeVmPattern` | `^vCLS-` | regex of VM names to skip |
| `-BatchSize` | 50 | entities per performance query; failing batches are split automatically |
| `-RetentionMonths` | 13 | delete older CSV files (0 = keep) |
| `-CompressOldMonths` | off | gzip completed months (both builders read `.csv.gz`) |

| Report parameter | Default | |
|---|---|---|
| `-Days` | 30 | history in the report; above ~62 days host series are bucketed (2 h, 6 h …) |
| `-StretchedCluster` / `-StretchedClusterName` | off | site-failover capacity (50%) instead of N+1, see above (alias: `-StretchedClusters`) |
| `-ThresholdPct` | 50 | tiering guidance, % of DRAM |
| `-ColdPct` / `-HotPct` | 40 / 75 | per-VM hints, worst-day P95 of active vs. configured memory |
| `-Title`, `-SupportContact` | | report header |

`memtier.py report` takes the same settings as `--days`, `--stretched-cluster`, `--stretched-clusters` (alias `--stretched-cluster-name`), `--threshold-pct`, `--cold-pct`, `--hot-pct`, `--title`, `--support-contact` and `--template`; each overrides the `.ini` file.

Exit codes: `0` ok, `1` failed, `2` partial (some hosts/VMs returned no statistics). Task Scheduler shows these as the "Last Run Result".

## Option B – Python directly on the vCenter Server Appliance

`memtier.py` uses the **VI/JSON API** (vCenter 8.0 U1 or later). Every vCenter that supports memory tiering has it. No packages need to be installed.

> **Note:** Running custom scripts and cron jobs on the appliance is not something Broadcom supports. The footprint here is small: read-only API calls, one cron file and one folder. Remove it if support asks, and keep in mind that **a major vCenter upgrade deploys a new appliance, so copy the `data` folder off before upgrading**. If you prefer not to touch the appliance, the same script runs unchanged on any Linux VM with Python 3.6+.

1. Enable SSH in the VAMI (`https://vcenter:5480` → Access) and log in as root.
   If the login lands in the appliance shell (`Command>`), type `shell`. For scp/WinSCP the root shell must be bash (`chsh -s /bin/bash root`; revert with `chsh -s /bin/appliancesh root`).
2. Copy the files and check free space. Data needs roughly 30 MB per month per 300 VMs uncompressed; compressed months are 4–10× smaller:
   ```bash
   mkdir -p /root/memtier && cd /root/memtier
   # copy python/memtier.py and template/memtier-report.template.html into this folder
   df -h /root && python3 --version
   ```
3. Create the config, test the login, and install the cron jobs:
   ```bash
   python3 /root/memtier/memtier.py setup --config /root/memtier/memtier.ini --install-cron
   ```
   This asks for the vCenter FQDN (defaults to the appliance itself), a read-only SSO user and its password. The password is stored in `memtier.ini`, which is created with mode 600. Leave the password empty to use the `MEMTIER_PASSWORD` environment variable instead.
   Cron entries in `/etc/cron.d/memtier`: collect hourly at :05, report daily at 06:30.
4. Test right away:
   ```bash
   python3 /root/memtier/memtier.py collect --config /root/memtier/memtier.ini
   python3 /root/memtier/memtier.py report --config /root/memtier/memtier.ini
   ```
5. Fetch the report with scp from `/root/memtier/reports/MemTier_Report_latest.html`.

Appliance notes:
* **TLS:** on the appliance itself, `verify_tls = true` works out of the box (the VMCA root is in the appliance trust store). When running `memtier.py` elsewhere, set `ca_file` to the VMCA root certificate from the vCenter landing page ("Download trusted root CA certificates"), or use `verify_tls = false` in a lab.
* **Stretched clusters:** set `stretched_cluster = true` or `stretched_clusters = Metro-A, Metro-B` in the `[report]` section.
* **First run:** the very first real-time query after vCenter has been idle can take ~30 s; later runs take a few seconds.
* **Account lockout:** root logins are locked after 3 failed attempts (`pam_faillock`). Avoid scripted SSH logins in loops.
* **Root password expiry:** if the appliance root password expires, cron jobs may stop running. Check with `chage -l root`. The "Collector runs" section of the report makes gaps visible.
* **Logs:** `logs/memtier-collect.log` and `logs/memtier-report.log`, rotated at 5 MB × 5 files.
* **Other settings:** window, exclusions, batch size, retention, compression and thresholds are all in `memtier.ini`, with the same meaning as the PowerShell parameters. `compress_old_months` is on by default here.

## Reading the trend report

A single HTML file that works offline, in light and dark mode. Filters at the top (time range, vCenter, cluster, host metric, cluster failover model) apply to everything on the page.

* **Memory tiering candidates:** one row per cluster, sorted best first. Three bars against the cluster's own DRAM — active P95, consumed P95 and configured — plus *active / consumed*, *active / configured* and the amount of cold memory currently held in DRAM. Verdicts:
  * *Strong candidate* / *Good candidate*: active P95 stays at or below the guidance and at least 50% / 30% of consumed memory is cold.
  * *Limited benefit* (≥ 15% cold) and *Little benefit*: the tier would have little to move.
  * *Not suitable*: active memory alone is already above the guidance, so a tier would push hot pages onto NVMe. Add DRAM or rebalance first.
* **Host active vs. consumed memory:** one small chart per host, sorted worst first. The blue line is the hourly P95 of active memory (switchable to average or max), the shaded area is the hourly maximum, the orange line is consumed memory, and the dashed line is the 50% guidance. All panels share one scale, so hosts compare directly — including hosts that already have a tier and consume more than their DRAM. Verdicts:
  * *Fits*: P95 over the time range is at or below the threshold.
  * *Fits, peaks above*: P95 is below, but the maximum crosses the threshold.
  * *Exceeds*: P95 is above the threshold.
* **Cluster failover headroom:** active memory against the DRAM that survives a failure (N+1 or one site), plus a table with the capacity check and the tiering ratios. Consumed memory must fit into the surviving memory.
* **Weekday × hour heatmap:** shows batch windows, business hours and month-end peaks that a snapshot never shows.
* **Hosts table:** DRAM/NVMe tier, configured memory vs. DRAM, active avg/P95/max, consumed P95, *active / consumed*, *active / configured*, cold memory in DRAM, balloon and swap.
* **Virtual machines:** worst-day P95 of active vs. configured memory, consumed avg, *active / consumed*, daily trend, balloon/swap flags, reservation and latency sensitivity. You can search, sort, filter and export to CSV. The *Cold/Warm/Hot* status is a right-sizing hint; tiering itself is decided per host and cluster. Per-VM sizes stay in GB regardless of the units switch.
* **Collector runs:** coverage of hourly slots, failed or partial runs, and the collector's error message.

## Design notes

| Common pitfall | How this project handles it |
|---|---|
| Single `QuickStats` sample used for a tiering decision | hourly avg/P95/max from 20 s real-time samples, kept for months |
| Unweighted average of per-VM percentages | weighted sums (MB / MB), per host and cluster |
| "Active fits DRAM" treated as the whole tiering question | active *and* consumed *and* configured, so a cluster with no cold memory is not proposed for a tier |
| No host "active vs. DRAM" metric, no failover view | both, with N+1 and stretched (site) failover, plus DRAM/NVMe tier sizes from `memoryTierInfo` |
| Silent connection failure → "success: 0 VMs" | errors are logged, the run is recorded as `failed`, exit code 1 |
| Script disconnects the user's session or mixes vCenters | `-NotDefault` connections owned by the script; every query uses `-Server`; keys are `vCenter\|MoRef` |
| `.Count` / `ConvertTo-Json` break with one item (PS 5.1) | no reliance on either; JSON is written explicitly and culture-invariant |
| CSV in ASCII / wrong delimiter | UTF-8 data files; report export with BOM and locale-aware delimiter |
| Unescaped HTML and `%2f` names | names unescaped; all text inserted via `textContent`; data embedded script-safe |
| CDN dependencies | none: plain HTML/CSS/JS, works offline |

## Testing

* **Live**, against vCenter 9.1 and vCenter 8.0 U3 labs:
  * both collectors, from a workstation and (Python) on the appliances themselves: setup, collect, report, cron
  * the snapshot script, with re-used and with its own sessions
  * PowerShell and Python report builders produce identical output from the same live data

  Values from both collectors match, except for VMs that DRS migrated between runs (a vMotion resets real-time history).
* **Mock:** both report builders on generated data (identical output), and the collectors against a mocked VI/JSON server and mocked PowerCLI.
* **Not tested:** Windows PowerShell 5.1 and the Task Scheduler installer, and hosts with NVMe memory tiering already enabled (only mock data).

## Support

dominik.steiner@nts.eu
