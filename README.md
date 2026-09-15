# vmware-memory-report

**Find out whether NVMe memory tiering can cut your VMware memory bill, based on weeks of real data instead of a guess.**

![Trend report (mock data)](docs/images/trend-report.png)

<sub>All screenshots and example reports use mock data. Open [`examples/mock-trend-report.html`](examples/mock-trend-report.html) or [`examples/mock-snapshot-report.html`](examples/mock-snapshot-report.html) to click through a report.</sub>

> **Disclaimer:** This is an independent community project. It is not affiliated with, endorsed by or supported by VMware or Broadcom. Use at your own risk.

## 💸 Memory is unacceptably expensive?

* DRAM is often the biggest line item in a new ESXi host, and prices keep rising.
* Most VMs keep a lot of memory they rarely touch.
* You pay DRAM prices for memory that just sits there.

## 🚀 Memory tiering can help

* **NVMe memory tiering** (vSphere 8.0 U3 / VCF 9) moves cold memory pages to fast NVMe drives.
* Hot pages stay in DRAM. With the default 1:1 ratio, a host can offer up to twice its DRAM.
* The catch: it only works if your **active** memory fits into DRAM. Broadcom's guidance is to keep active memory at or below **50% of DRAM**.

## 🔍 But will it work for *your* clusters?

A single look at vCenter won't tell you. Active memory swings with backups, batch jobs and month-end. This project measures it **every hour, for weeks**, and answers:

* **Which clusters are good candidates?** Clusters with a big gap between *active* and *consumed* memory have a lot of cold memory that NVMe can take over.
* **Does it fit?** Does each host's active memory stay under the guidance?
* **Does it still fit after a failure?** Checked for N+1 and for stretched clusters (a whole site down).
* **Which VMs are oversized?** Worst-day active vs. configured memory per VM.

## ✨ What you get

* 📊 **One HTML report**, self-contained, works offline, light and dark mode.
* 🔄 **Always up to date:** the report is rebuilt after every hourly run and overwrites the previous one. No history is lost, the raw data stays in CSV files.
* 🎯 **Real numbers:** 20-second samples reduced to hourly average, P95 and max. No single-sample guesswork.
* 🏢 **Stretched cluster aware:** site failover (50% capacity) next to classic N+1.
* 🔒 **Read-only:** a read-only vCenter account is all it needs. Nothing is changed in vCenter.
* 🧰 **Two editions, same result:** PowerShell (PowerCLI) or Python (no extra packages). Both write the same CSV files and build the same report.
* ⚡ **Quick snapshot:** want a first impression right now? The snapshot script looks at the last hour, no scheduling needed.

## ✅ Supported environments

| | Supported | Tested |
|---|---|---|
| **vCenter** | 8.0 U1 or later (Python edition needs the VI/JSON API from 8.0 U1) | 8.0 U3, 9.1 |
| **ESXi / memory tiering** | NVMe tier sizes are read on 8.0 U3 or later; older hosts are reported with DRAM only | 8.0 U3, 9.1 |
| **PowerShell** | Windows PowerShell 5.1 or PowerShell 7.x, with PowerCLI 13.x or later (VCF.PowerCLI) | PowerShell 7.x |
| **Python** | 3.6 or later, standard library only | Python shipped with vCenter 8.0 U3 / 9.1, Python 3.14 |
| **Operating system** | Windows (scheduled task installer), Linux (cron setup), macOS for manual runs | Linux (Photon OS), macOS |

Not tested yet: Windows PowerShell 5.1, the Windows scheduled task installer, and hosts that already have memory tiering enabled (mock data only).

## 🖥️ Where should it run?

* ✅ **Recommended:** a Windows jump host, a management server or a small Linux VM that can reach vCenter on port 443.
* ⛔ **Not recommended:** the vCenter Server Appliance itself. It would technically work (Python 3 is on the appliance), but custom scripts and cron jobs on the appliance are not supported by Broadcom, and the data is gone after the next major upgrade.

## ⏱️ Try it in one minute (no vCenter needed)

```bash
python3 examples/generate-mock-data.py examples/mock-data
python3 python/memtier.py report --config examples/mock.ini --output mock-report.html
```

Then open `mock-report.html` in your browser.

## 🪟 Setup on Windows (PowerShell)

1. Copy the `powershell` and `template` folders side by side, e.g. to `D:\MemTier\scripts\`.
2. Open an **elevated** PowerShell **as the account that will run the task**, for example:
   ```powershell
   runas /user:DOMAIN\svc-memtier powershell.exe
   ```
3. Run the installer:
   ```powershell
   .\Install-MemTierTasks.ps1 -VCenterServer vcenter01.example.com -BaseDir D:\MemTier -CompressOldMonths
   ```
   * Saves the vCenter credential encrypted for this account only
   * Tests the connection
   * Registers the hourly task **MemTier Collector**
4. Start a first run and open the report:
   ```powershell
   Start-ScheduledTask -TaskName 'MemTier Collector'
   ```
   📄 `D:\MemTier\reports\MemTier_Report.html` (updated every hour)

Stretched clusters? Add `-StretchedCluster` (all clusters) or `-StretchedClusterName Metro-A,Metro-B`.

## 🐧 Setup on Linux (Python)

1. Copy `python/memtier.py` and `template/memtier-report.template.html` into one folder, e.g. `/opt/memtier`.
2. Create the config, test the login and install the hourly cron job (as root):
   ```bash
   python3 /opt/memtier/memtier.py setup --config /opt/memtier/memtier.ini --install-cron
   ```
   * Asks for the vCenter FQDN, a read-only user and its password
   * The config file is created with mode 600; leave the password empty to use the `MEMTIER_PASSWORD` environment variable instead
3. Start a first run:
   ```bash
   python3 /opt/memtier/memtier.py collect --config /opt/memtier/memtier.ini
   ```
   📄 `/opt/memtier/reports/MemTier_Report.html` (updated every hour)

All settings (stretched clusters, thresholds, retention, TLS) live in `memtier.ini`. For TLS verification, set `ca_file` to the vCenter root certificate ("Download trusted root CA certificates" on the vCenter start page).

## ⚡ Quick snapshot (last hour only)

```powershell
.\Get-MemTierSnapshot.ps1 -VCenterServer vcenter01.example.com
```

* Writes an HTML report plus Excel-ready CSV files for VMs and hosts
* Good for a first impression; for a decision, let the collector run for a few weeks

![Snapshot report (mock data)](docs/images/snapshot-report.png)

## 📖 Reading the report

* **Memory tiering candidates:** every cluster ranked by how much cold memory it holds.
  * *Strong / Good candidate:* active memory fits and a lot of consumed memory is cold.
  * *Limited / Little benefit:* not much for NVMe to take over.
  * *Not suitable:* active memory is already above the guidance. Add DRAM or rebalance first.
* **Host charts:** active and consumed memory per host over time, with the 50% guidance line.
  * *Fits* · *Fits, peaks above* · *Exceeds*
* **Cluster failover headroom:** does active memory still fit after a host or site failure?
* **Weekday × hour heatmap:** batch windows and business hours at a glance.
* **Hosts and VMs tables:** searchable, sortable, exportable to CSV.
* **Collector runs:** gaps and failed runs are visible, nothing fails silently.

![Cluster failover headroom (mock data)](docs/images/cluster-failover.png)

## 🧩 Good to know

* **How data is collected:** ESXi keeps 20-second samples for about an hour. The collector reads them every hour, so nothing is missed, and no vCenter statistics level changes are needed.
* **Where data goes:** monthly CSV files in `data/` (`host-`, `vm-` and `run-yyyy-MM.csv`). Old months can be compressed and are deleted after 13 months by default.
* **Stretched clusters:** after a site failure only half of the cluster is left, so the report checks against 50% of the cluster instead of "cluster minus one host". The report lets you switch between the models.
* **Manual runs:** `Invoke-MemTierCollector.ps1` and `memtier.py collect` rebuild the report after collecting. Use `-NoReport` / `--no-report` to skip that, or `New-MemTierReport.ps1` / `memtier.py report` to only rebuild it.
* **Exit codes:** `0` ok, `1` failed, `2` partial (some hosts or VMs returned no data).

---

<sub>Community project by [Dominik Steiner](https://dominik.st/einer) · Not affiliated with VMware or Broadcom · VMware, vSphere, vCenter and VCF are trademarks of Broadcom.</sub>
