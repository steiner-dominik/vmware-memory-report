<#
.SYNOPSIS
    VMware NVMe Memory Tiering: one-off snapshot report.
.DESCRIPTION
    Connects to one or more vCenters, reads the last hour of 20-second real-time
    memory statistics for every host and powered-on VM and writes:

        VMware_Memory_Tiering_Snapshot_<date>.html      interactive, self-contained dashboard
        VMware_Memory_Tiering_Snapshot_<date>_VMs.csv   all VMs, sorted by cluster and VM name
        VMware_Memory_Tiering_Snapshot_<date>_Hosts.csv all hosts

    Values are avg / P95 / max over the last hour instead of a
    single QuickStats sample, host active memory is compared with physical DRAM
    (the actual tiering criterion), and clusters are checked for a failure: one host down (N+1)
    or, for stretched clusters, one site down (-StretchedCluster).

    For a long-term view, schedule Invoke-MemTierCollector.ps1 and New-MemTierReport.ps1.
.PARAMETER VCenterServer
    One or more vCenter FQDNs.
.PARAMETER Credential
    vCenter credential. Without it (and without -CredentialFile) an existing session,
    Windows pass-through or the PowerCLI credential prompt is used.
.PARAMETER ExportDir
    Output folder (default: current folder). Created if missing.
.PARAMETER WindowMinutes
    Minutes of real-time data to evaluate (5-60, hosts keep about one hour).
.PARAMETER StretchedCluster
    All clusters are stretched across two sites: after a site failure the surviving site must hold
    everything, so capacity is 50% of the cluster instead of N+1. Can also be switched in the report.
.PARAMETER StretchedClusterName
    Names of the stretched clusters, if only some clusters are stretched.
.PARAMETER CsvDelimiter
    Default: the list separator of the current culture (";" on German systems, or ";" whenever the list
    separator equals the decimal separator), so Excel opens the file directly.
.PARAMETER PassThru
    Also return the VM objects to the pipeline.
.EXAMPLE
    .\Get-MemTierSnapshot.ps1 -VCenterServer vcenter01.example.com
.EXAMPLE
    .\Get-MemTierSnapshot.ps1 -VCenterServer vcenter01.example.com,vcenter02.example.com -Credential (Get-Credential) -ExportDir C:\Reports
.NOTES
    Exit codes: 0 = ok, 1 = no vCenter could be read, 2 = partial (a vCenter failed or entities without data).
    Requires VMware PowerCLI (VCF.PowerCLI) 13.x or later; Windows PowerShell 5.1 or PowerShell 7+.
#>
[CmdletBinding()]
param (
    [Parameter(Mandatory = $true)][string[]]$VCenterServer,
    [System.Management.Automation.PSCredential]$Credential,
    [string]$CredentialFile,
    [string]$ExportDir,
    [ValidateRange(5, 60)][int]$WindowMinutes = 60,
    [string]$ExcludeVmPattern = '^vCLS-',
    [ValidateRange(1, 1000)][int]$BatchSize = 50,
    [double]$ThresholdPct = 50,
    [double]$ColdPct = 40,
    [double]$HotPct = 75,
    [switch]$StretchedCluster,
    [Alias('StretchedClusters')]
    [string[]]$StretchedClusterName = @(),
    [string]$Title = 'VMware Memory Tiering Snapshot',
    [string]$SupportContact = 'https://github.com/steiner-dominik/vmware-memory-report/issues',
    [string]$TemplatePath,
    [string]$CsvDelimiter,
    [switch]$PassThru
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'MemTier.Common.ps1')

$VCenterServer = @($VCenterServer | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$StretchedClusterName = @($StretchedClusterName | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $ExportDir) { $ExportDir = (Get-Location -PSProvider FileSystem).ProviderPath }
$ExportDir = [System.IO.Path]::GetFullPath($ExportDir)
if (-not $CsvDelimiter) {
    # Excel splits on the culture list separator; some cultures (e.g. en-AT) use "," for both lists and decimals
    $culture = Get-Culture
    $CsvDelimiter = $culture.TextInfo.ListSeparator
    if (-not $CsvDelimiter -or $CsvDelimiter -eq $culture.NumberFormat.NumberDecimalSeparator) { $CsvDelimiter = ';' }
}
if (-not $TemplatePath) {
    $candidates = @((Join-Path $PSScriptRoot 'memtier-snapshot.template.html'),
        (Join-Path (Split-Path $PSScriptRoot -Parent) (Join-Path 'template' 'memtier-snapshot.template.html')))
    $TemplatePath = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $TemplatePath) { throw "snapshot template not found (looked in: $($candidates -join ', '))" }
}

function Get-Pct($Part, $Whole) {
    if ($null -eq $Part -or -not $Whole) { return $null }
    Get-Round1 ([double]$Part * 100.0 / [double]$Whole)
}

function Get-Verdict($P95Pct, $MaxPct) {
    if ($null -eq $P95Pct) { return 'No data' }
    if ($P95Pct -gt $ThresholdPct) { return 'Exceeds' }
    if ($null -ne $MaxPct -and $MaxPct -gt $ThresholdPct) { return 'Fits, peaks above' }
    'Fits'
}

function Format-Pct($Value) { if ($null -eq $Value) { '-' } else { '{0:N1}' -f $Value } }

$powerNames = @{ poweredOn = 'Powered On'; poweredOff = 'Powered Off'; suspended = 'Suspended' }

# ---------------------------------------------------------------------------
# 1. Collect
# ---------------------------------------------------------------------------
Write-Host "`n[+] VMware Memory Tiering Snapshot" -ForegroundColor Cyan
Import-MemTierPowerCLI
if (-not $Credential -and $CredentialFile) { $Credential = Import-Clixml -LiteralPath $CredentialFile }

$nowUtc = Get-MemTierUtcNow
$startUtc = $nowUtc.AddMinutes(-$WindowMinutes)
$hostObjects = New-Object 'System.Collections.Generic.List[object]'
$vmObjects = New-Object 'System.Collections.Generic.List[object]'
$runs = New-Object 'System.Collections.Generic.List[object]'

foreach ($server in $VCenterServer) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $run = [ordered]@{ vc = $server; status = 'failed'; message = ''; hosts = $null; hostsConnected = $null; vmsTotal = $null; vmsOn = $null; templates = $null; excluded = $null; durationSec = $null }
    $connection = $null
    # rows of one vCenter are only kept when its collection completes
    $hostRows = New-Object 'System.Collections.Generic.List[object]'
    $vmRows = New-Object 'System.Collections.Generic.List[object]'
    try {
        Write-Host "[*] ${server}: connecting" -ForegroundColor Yellow
        $connection = Connect-MemTierVCenter -Server $server -Credential $Credential
        Write-Host "[*] ${server}: reading inventory" -ForegroundColor Yellow
        $inventory = Get-MemTierInventory -VI $connection.VI -Server $server -ExcludeVmPattern $ExcludeVmPattern
        Write-Host ("[*] {0}: reading {1} min of real-time statistics for {2} hosts and {3} VMs" -f $server, $WindowMinutes,
            $inventory.HostMeta.Count, @($inventory.Vms | Where-Object { $_.Collect }).Count) -ForegroundColor Yellow
        $stats = Get-MemTierStatistics -VI $connection.VI -Inventory $inventory -StartUtc $startUtc -BatchSize $BatchSize

        foreach ($mid in $inventory.HostMeta.Keys) {
            $h = $inventory.HostMeta[$mid]
            $st = if ($stats.Hosts.ContainsKey($mid)) { $stats.Hosts[$mid] } else { Get-MemTierEmptyStats }
            $p95Pct = Get-Pct $st.Active.P95 $h.Dram
            $maxPct = Get-Pct $st.Active.Max $h.Dram
            $hostRows.Add([pscustomobject][ordered]@{
                    vc = $server; cluster = $h.Cluster; name = $h.Name; connectionState = $h.State; maintenanceMode = $h.Maint
                    tieringType = $h.Tiering; dramMB = $h.Dram; nvmeMB = $h.Nvme; vmsOn = $h.VmsOn; assignedMB = $h.Assigned
                    assignedPctOfDram = Get-Pct $h.Assigned $h.Dram; samples = $st.Active.Samples
                    activeAvgMB = $st.Active.Avg; activeP95MB = $st.Active.P95; activeMaxMB = $st.Active.Max; consumedAvgMB = $st.Consumed.Avg
                    balloonMaxMB = $st.Balloon.Max; swapUsedMaxMB = $st.Swap.Max
                    activeAvgPct = Get-Pct $st.Active.Avg $h.Dram; activeP95Pct = $p95Pct; activeMaxPct = $maxPct
                    verdict = Get-Verdict $p95Pct $maxPct
                })
        }

        $noData = 0
        foreach ($v in $inventory.Vms) {
            $st = $stats.Vms[$v.Id]
            if (-not $st) { $st = Get-MemTierEmptyStats }
            $power = if ($powerNames.ContainsKey($v.Power)) { $powerNames[$v.Power] } else { $v.Power }
            $p95Pct = Get-Pct $st.Active.P95 $v.Assigned
            $status = if ($v.Power -ne 'poweredOn') { $power }
            elseif ($v.Excluded) { 'Excluded' }
            elseif ($st.Active.Samples -eq 0 -or $null -eq $p95Pct) { $noData++; 'No data' }
            elseif ($p95Pct -lt $ColdPct) { 'Cold' }
            elseif ($p95Pct -lt $HotPct) { 'Warm' }
            else { 'Hot' }
            $vmRows.Add([pscustomobject][ordered]@{
                    vc = $server; cluster = $v.Cluster; host = $v.Host; name = $v.Name; powerState = $power
                    assignedMB = $v.Assigned; reservationMB = $v.Reservation; latencySensitivity = $v.Latency; samples = $st.Active.Samples
                    activeAvgMB = $st.Active.Avg; activeP95MB = $st.Active.P95; activeMaxMB = $st.Active.Max; consumedAvgMB = $st.Consumed.Avg
                    balloonMaxMB = $st.Balloon.Max; swappedMaxMB = $st.Swap.Max
                    activeAvgPct = Get-Pct $st.Active.Avg $v.Assigned; activeP95Pct = $p95Pct; activeMaxPct = Get-Pct $st.Active.Max $v.Assigned
                    status = $status; pressure = [bool](($st.Balloon.Max -gt 0) -or ($st.Swap.Max -gt 0))
                })
        }

        $hostObjects.AddRange($hostRows)
        $vmObjects.AddRange($vmRows)
        $partial = ($stats.HostsFailed -gt 0) -or ($stats.VmsFailed -gt 0)
        # like the collector: only failed performance queries make a run partial; VMs without data
        # (e.g. on a disconnected host, or just powered on) are reported but expected
        $run.status = if ($partial) { 'partial' } else { 'ok' }
        $notes = @()
        if ($partial) { $notes += 'performance query failed for {0} hosts, {1} VMs' -f $stats.HostsFailed, $stats.VmsFailed }
        if ($noData) { $notes += '{0} powered-on VMs without statistics (e.g. on a disconnected host)' -f $noData }
        $run.message = $notes -join '; '
        $run.hosts = $inventory.HostMeta.Count; $run.hostsConnected = $stats.LiveHosts
        $run.vmsTotal = $inventory.Counts.Total; $run.vmsOn = $inventory.Counts.On
        $run.templates = $inventory.Counts.Templates; $run.excluded = $inventory.Counts.Excluded
    }
    catch {
        $run.message = $_.Exception.Message
        Write-Host "[!] ${server}: $($_.Exception.Message)" -ForegroundColor Red
    }
    finally {
        Disconnect-MemTierVCenter -Connection $connection -Server $server
        $run.durationSec = [long][math]::Round($sw.Elapsed.TotalSeconds)
        $runs.Add([pscustomobject]$run)
    }
}

# ---------------------------------------------------------------------------
# 2. Cluster failover capacity (hosts with data only)
#    N+1       : the cluster without its largest host must carry the load
#    stretched : one site (50% of the cluster) must carry the load after a site failure
#    Tiering   : active P95 as % of surviving DRAM vs. the tiering guidance
#    Capacity  : consumed as % of surviving memory (DRAM + NVMe tier) must stay <= 100%
# ---------------------------------------------------------------------------
function Get-FailoverCapacity([bool]$Stretched, [double]$Total, [double]$Largest, [int]$Hosts) {
    if ($Stretched) { return $Total * 0.5 }
    if ($Hosts -gt 1) { return $Total - $Largest }
    $Total
}
function Get-CapacityVerdict($ConsumedPct) {
    if ($null -eq $ConsumedPct) { return 'No data' }
    if ($ConsumedPct -gt 100) { return 'Does not fit' }
    if ($ConsumedPct -gt 90) { return 'Tight' }
    'Fits'
}
$clusterObjects = New-Object 'System.Collections.Generic.List[object]'
$groups = @{}
foreach ($h in $hostObjects) {
    if ($h.cluster -eq '(standalone)' -or $null -eq $h.activeP95MB -or -not $h.dramMB) { continue }
    $key = $h.vc + '|' + $h.cluster
    if (-not $groups.ContainsKey($key)) { $groups[$key] = New-Object 'System.Collections.Generic.List[object]' }
    $groups[$key].Add($h)
}
foreach ($key in $groups.Keys) {
    $members = $groups[$key]
    $stretched = $StretchedCluster.IsPresent -or ($StretchedClusterName -contains $members[0].cluster)
    $dram = 0.0; $largest = 0.0; $mem = 0.0; $largestMem = 0.0; $p95 = 0.0; $max = 0.0; $consumed = 0.0; $consumedN = 0
    foreach ($h in $members) {
        $dram += $h.dramMB; $largest = [math]::Max($largest, [double]$h.dramMB)
        $mem += $h.dramMB + $h.nvmeMB; $largestMem = [math]::Max($largestMem, [double]($h.dramMB + $h.nvmeMB))
        $p95 += $h.activeP95MB; $max += $h.activeMaxMB
        if ($null -ne $h.consumedAvgMB) { $consumed += $h.consumedAvgMB; $consumedN++ }
    }
    $capacity = Get-FailoverCapacity $stretched $dram $largest $members.Count
    $capacityMem = Get-FailoverCapacity $stretched $mem $largestMem $members.Count
    $p95Pct = Get-Pct $p95 $capacity
    $maxPct = Get-Pct $max $capacity
    # One host without consumed statistics would understate the cluster and fake a "Fits" verdict.
    $complete = $consumedN -eq $members.Count
    $consumedPct = if ($complete) { Get-Pct $consumed $capacityMem } else { $null }
    $activeOverConsumed = if ($complete -and $consumed) { Get-Pct $p95 $consumed } else { $null }
    $clusterObjects.Add([pscustomobject][ordered]@{
            vc = $members[0].vc; cluster = $members[0].cluster; model = $(if ($stretched) { 'Stretched (50%)' } else { 'N+1' })
            hosts = $members.Count; dramMB = [long]$dram; capacityMB = [long]$capacity; capacityMemMB = [long]$capacityMem
            activeP95MB = [long]$p95; p95Pct = $p95Pct; maxPct = $maxPct; verdict = Get-Verdict $p95Pct $maxPct
            consumedMB = $(if ($complete) { [long]$consumed } else { $null }); consumedPct = $consumedPct
            activeOverConsumedPct = $activeOverConsumed; capacityVerdict = Get-CapacityVerdict $consumedPct
        })
}

# ---------------------------------------------------------------------------
# 3. Output: HTML, CSV, console
# ---------------------------------------------------------------------------
$okRuns = @($runs | Where-Object { $_.status -ne 'failed' })
$exitCode = if ($okRuns.Count -eq 0) { 1 } elseif (@($runs | Where-Object { $_.status -ne 'ok' }).Count -gt 0) { 2 } else { 0 }

if (-not (Test-Path -LiteralPath $ExportDir)) { New-Item -ItemType Directory -Path $ExportDir -Force | Out-Null }
$stamp = (Get-Date).ToString('yyyy-MM-dd_HHmm', $script:Inv)
$base = Join-Path $ExportDir "VMware_Memory_Tiering_Snapshot_$stamp"
$utf8 = New-Object System.Text.UTF8Encoding($false)

$data = [ordered]@{
    schema   = 1
    kind     = 'snapshot'
    meta     = [ordered]@{
        title = $Title; support = $SupportContact; generatedUtc = (ConvertTo-MemTierIso $nowUtc); windowStartUtc = (ConvertTo-MemTierIso $startUtc)
        windowMinutes = $WindowMinutes; vcenters = @($VCenterServer); thresholdPct = $ThresholdPct; coldPct = $ColdPct; hotPct = $HotPct
        builder = "Get-MemTierSnapshot.ps1 $($script:MemTierVersion) (PowerShell $($PSVersionTable.PSVersion))"
        failover = [ordered]@{ stretched = $StretchedCluster.IsPresent; stretchedClusters = @($StretchedClusterName) }
    }
    runs     = $runs
    hosts    = $hostObjects
    vms      = $vmObjects
}
$template = [System.IO.File]::ReadAllText($TemplatePath, [System.Text.Encoding]::UTF8)
$pos = $template.IndexOf($script:DataPlaceholder, [StringComparison]::Ordinal)
if ($pos -lt 0) { throw "template does not contain the data placeholder $($script:DataPlaceholder)" }
$html = $template.Substring(0, $pos) + (ConvertTo-MemTierJson $data) + $template.Substring($pos + $script:DataPlaceholder.Length)
[System.IO.File]::WriteAllText("$base.html", $html, $utf8)

# CSV for Excel: UTF-8 with BOM, culture list separator; numbers formatted with the current culture to match
$csvEncoding = if ($PSVersionTable.PSVersion.Major -ge 6) { 'utf8BOM' } else { 'UTF8' }
$vmCsv = $vmObjects | Sort-Object -Property cluster, name | Select-Object `
@{ n = 'vCenter'; e = { $_.vc } }, @{ n = 'Cluster'; e = { $_.cluster } }, @{ n = 'ESX Host'; e = { $_.host } }, @{ n = 'VM Name'; e = { $_.name } },
@{ n = 'Power State'; e = { $_.powerState } }, @{ n = 'Memory Assigned [MB]'; e = { $_.assignedMB } }, @{ n = 'Memory Reservation [MB]'; e = { $_.reservationMB } },
@{ n = 'Active Avg [MB]'; e = { $_.activeAvgMB } }, @{ n = 'Active P95 [MB]'; e = { $_.activeP95MB } }, @{ n = 'Active Max [MB]'; e = { $_.activeMaxMB } },
@{ n = 'Consumed Avg [MB]'; e = { $_.consumedAvgMB } }, @{ n = 'Ballooned Max [MB]'; e = { $_.balloonMaxMB } }, @{ n = 'Swapped Max [MB]'; e = { $_.swappedMaxMB } },
@{ n = 'Active Avg vs Assigned [%]'; e = { $_.activeAvgPct } }, @{ n = 'Active P95 vs Assigned [%]'; e = { $_.activeP95Pct } },
@{ n = 'Active Max vs Assigned [%]'; e = { $_.activeMaxPct } }, @{ n = 'Latency Sensitivity'; e = { $_.latencySensitivity } },
@{ n = 'Samples'; e = { $_.samples } }, @{ n = 'Status'; e = { $_.status } }, @{ n = 'Memory Pressure'; e = { if ($_.pressure) { 'yes' } else { 'no' } } }
if ($vmCsv) { $vmCsv | Export-Csv -LiteralPath "$($base)_VMs.csv" -NoTypeInformation -Delimiter $CsvDelimiter -Encoding $csvEncoding }

$hostCsv = $hostObjects | Sort-Object -Property cluster, name | Select-Object `
@{ n = 'vCenter'; e = { $_.vc } }, @{ n = 'Cluster'; e = { $_.cluster } }, @{ n = 'ESX Host'; e = { $_.name } }, @{ n = 'Connection State'; e = { $_.connectionState } },
@{ n = 'Maintenance Mode'; e = { $_.maintenanceMode } }, @{ n = 'Tiering Type'; e = { $_.tieringType } }, @{ n = 'DRAM [MB]'; e = { $_.dramMB } },
@{ n = 'NVMe Tier [MB]'; e = { $_.nvmeMB } }, @{ n = 'Powered-on VMs'; e = { $_.vmsOn } }, @{ n = 'Assigned [MB]'; e = { $_.assignedMB } },
@{ n = 'Assigned vs DRAM [%]'; e = { $_.assignedPctOfDram } }, @{ n = 'Active Avg [MB]'; e = { $_.activeAvgMB } }, @{ n = 'Active P95 [MB]'; e = { $_.activeP95MB } },
@{ n = 'Active Max [MB]'; e = { $_.activeMaxMB } }, @{ n = 'Consumed Avg [MB]'; e = { $_.consumedAvgMB } }, @{ n = 'Balloon Max [MB]'; e = { $_.balloonMaxMB } },
@{ n = 'Swap Used Max [MB]'; e = { $_.swapUsedMaxMB } }, @{ n = 'Active P95 vs DRAM [%]'; e = { $_.activeP95Pct } }, @{ n = 'Active Max vs DRAM [%]'; e = { $_.activeMaxPct } },
@{ n = 'Verdict'; e = { $_.verdict } }
if ($hostCsv) { $hostCsv | Export-Csv -LiteralPath "$($base)_Hosts.csv" -NoTypeInformation -Delimiter $CsvDelimiter -Encoding $csvEncoding }

# Console summary
$on = @($vmObjects | Where-Object { $_.powerState -eq 'Powered On' })
Write-Host ("`n[+] {0} hosts, {1} VMs ({2} powered on): Cold {3} • Warm {4} • Hot {5} • memory pressure {6}" -f $hostObjects.Count, $vmObjects.Count, $on.Count,
    @($vmObjects | Where-Object { $_.status -eq 'Cold' }).Count, @($vmObjects | Where-Object { $_.status -eq 'Warm' }).Count,
    @($vmObjects | Where-Object { $_.status -eq 'Hot' }).Count, @($vmObjects | Where-Object { $_.pressure }).Count) -ForegroundColor Cyan
if ($hostObjects.Count -gt 0) {
    Write-Host "`n[+] Hosts: active memory P95 / max of the last $WindowMinutes min and assigned memory, as % of DRAM - guidance: P95 <= $ThresholdPct%" -ForegroundColor Cyan
    $hostObjects | Sort-Object -Property @{ e = { if ($null -eq $_.activeP95Pct) { -1 } else { $_.activeP95Pct } } } -Descending |
        Format-Table -AutoSize -Property @{ n = 'Cluster'; e = { $_.cluster } }, @{ n = 'Host'; e = { $_.name } },
        @{ n = 'DRAM GB'; e = { '{0:N0}' -f ($_.dramMB / 1024) }; a = 'Right' }, @{ n = 'VMs'; e = { $_.vmsOn }; a = 'Right' },
        @{ n = 'Asg %'; e = { Format-Pct $_.assignedPctOfDram }; a = 'Right' }, @{ n = 'P95 %'; e = { Format-Pct $_.activeP95Pct }; a = 'Right' },
        @{ n = 'Max %'; e = { Format-Pct $_.activeMaxPct }; a = 'Right' }, @{ n = 'Verdict'; e = { $_.verdict } } | Out-Host
}
if ($clusterObjects.Count -gt 0) {
    Write-Host "[+] Clusters after a failure (N+1: one host down, stretched: one site down = 50%) - active vs. surviving DRAM, consumed vs. surviving memory" -ForegroundColor Cyan
    $clusterObjects | Sort-Object -Property p95Pct -Descending |
        Format-Table -AutoSize -Property @{ n = 'Cluster'; e = { $_.cluster } }, @{ n = 'Model'; e = { $_.model } }, @{ n = 'Hosts'; e = { $_.hosts } },
        @{ n = 'Surv. DRAM GB'; e = { '{0:N0}' -f ($_.capacityMB / 1024) }; a = 'Right' }, @{ n = 'P95 %'; e = { Format-Pct $_.p95Pct }; a = 'Right' },
        @{ n = 'Tiering'; e = { $_.verdict } }, @{ n = 'Act/Cons %'; e = { Format-Pct $_.activeOverConsumedPct }; a = 'Right' },
        @{ n = 'Consumed %'; e = { Format-Pct $_.consumedPct }; a = 'Right' },
        @{ n = 'Capacity'; e = { $_.capacityVerdict } } | Out-Host
}
foreach ($r in $runs) {
    $color = switch ($r.status) { 'ok' { 'Green' } 'partial' { 'Yellow' } default { 'Red' } }
    Write-Host ("    {0}: {1}{2}" -f $r.vc, $r.status, $(if ($r.message) { " - $($r.message)" } else { '' })) -ForegroundColor $color
}
Write-Host "    --> HTML Dashboard : $base.html" -ForegroundColor Green
if ($vmCsv) { Write-Host "    --> VM CSV         : $($base)_VMs.csv" -ForegroundColor Green }
if ($hostCsv) { Write-Host "    --> Host CSV       : $($base)_Hosts.csv`n" -ForegroundColor Green }

if ($PassThru) { $vmObjects }
if ($exitCode -eq 1) { Write-Host '[!] No vCenter could be read - the report only contains the collection status.' -ForegroundColor Red }
exit $exitCode
