<#
.SYNOPSIS
    VMware NVMe Memory Tiering: hourly statistics collector (PowerShell edition).
.DESCRIPTION
    Pulls the last hour of 20-second real-time memory samples (active, consumed,
    balloon, swap) for every connected ESXi host and powered-on VM, reduces them
    to avg / P95 / max per entity and appends the results to monthly CSV files:

        <DataDir>\host-yyyy-MM.csv   one row per host per run
        <DataDir>\vm-yyyy-MM.csv     one row per powered-on VM per run
        <DataDir>\run-yyyy-MM.csv    one row per vCenter per run (collector health)

    Real-time statistics are kept on the hosts for about one hour and do not
    depend on the vCenter statistics level, so running this script every hour
    gives complete coverage.

    After every run the trend report is rebuilt with New-MemTierReport.ps1 and
    <ReportDir>\MemTier_Report.html is overwritten, so it is always up to date.
    Use -NoReport to collect only.

    The CSV format is shared with python/memtier.py.
.PARAMETER VCenterServer
    One or more vCenter FQDNs.
.PARAMETER CredentialFile
    Credential saved with: Get-Credential | Export-Clixml <file>
    (only readable by the same Windows user on the same machine).
    Without it, an existing PowerCLI session or Windows pass-through (SSPI) is used.
.PARAMETER IntervalMinutes
    Collection interval the scheduled task uses: 60, 30 or 15. Every run reads all 20-second
    samples of its window, so a shorter interval does not find peaks an hourly run misses - it
    gives a finer time resolution and loses less data when a single run fails (hosts keep only
    about one hour of real-time samples).
.PARAMETER WindowMinutes
    Minutes of real-time data to read (max. 60). Defaults to IntervalMinutes; set it only to
    cover a gap deliberately.
.PARAMETER ReportDir
    Folder for MemTier_Report.html. Days, ThresholdPct, ColdPct, HotPct, Title, SupportContact,
    StretchedCluster and StretchedClusterName are passed on to New-MemTierReport.ps1.
.PARAMETER NoReport
    Collect only, do not rebuild the report.
.EXAMPLE
    .\Invoke-MemTierCollector.ps1 -VCenterServer vcenter01.example.com -CredentialFile D:\MemTier\vc-cred.xml -DataDir D:\MemTier\data
.NOTES
    Exit codes: 0 = ok, 1 = failed, 2 = partial (some entities without data).
    Requires VMware PowerCLI (VCF.PowerCLI) 13.x or later; Windows PowerShell 5.1 or PowerShell 7+.
#>
[CmdletBinding()]
param (
    [Parameter(Mandatory = $true)][string[]]$VCenterServer,
    [string]$CredentialFile,
    [System.Management.Automation.PSCredential]$Credential,
    [string]$DataDir,
    [string]$LogDir,
    [ValidateSet(15, 30, 60)][int]$IntervalMinutes = 60,
    [ValidateRange(5, 60)][int]$WindowMinutes,
    [string]$ExcludeVmPattern = '^vCLS-',
    [ValidateRange(1, 1000)][int]$BatchSize = 50,
    [int]$RetentionMonths = 13,
    [switch]$CompressOldMonths,
    [string]$ReportDir,
    [ValidateRange(1, 800)][int]$Days = 30,
    [ValidateRange(1, 100)][double]$CandidatePct = 40,
    [ValidateRange(1, 100)][double]$ThresholdPct = 50,
    [ValidateRange(0.1, 8)][double]$TierRatio = 1.0,
    [ValidateSet('en', 'de')][string]$Language = 'en',
    [double]$ColdPct = 40,
    [double]$HotPct = 75,
    [string]$Title,
    [string]$SupportContact,
    [switch]$StretchedCluster,
    [Alias('StretchedClusters')]
    [string[]]$StretchedClusterName = @(),
    [switch]$NoReport
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'MemTier.Common.ps1')

# The window has to match the schedule, otherwise consecutive runs overlap (double counting)
# or leave a gap the hosts have already discarded.
if (-not $PSBoundParameters.ContainsKey('WindowMinutes')) { $WindowMinutes = $IntervalMinutes }

# "powershell.exe -File" (Task Scheduler) passes "vc01,vc02" as one string
$VCenterServer = @($VCenterServer | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })

# Resolve paths relative to the script, never to the current directory (Task Scheduler starts in System32)
if (-not $DataDir) { $DataDir = Join-Path $PSScriptRoot 'data' }
if (-not $LogDir) { $LogDir = Join-Path $PSScriptRoot 'logs' }
$DataDir = [System.IO.Path]::GetFullPath($DataDir)
if (-not (Test-Path -LiteralPath $DataDir)) { New-Item -ItemType Directory -Path $DataDir -Force | Out-Null }
$transcript = Start-MemTierTranscript -LogDir $LogDir -Name 'collect'

function Invoke-VCenterCollection {
    param([string]$Server, [datetime]$NowUtc, $Cred)

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $run = @{ Timestamp = (ConvertTo-MemTierIso $NowUtc); VCenter = $Server; Status = 'failed' }
    $hostRows = New-Object 'System.Collections.Generic.List[object]'
    $vmRows = New-Object 'System.Collections.Generic.List[object]'
    $connection = $null
    try {
        $connection = Connect-MemTierVCenter -Server $Server -Credential $Cred
        $startUtc = $NowUtc.AddMinutes(-$WindowMinutes)
        $inventory = Get-MemTierInventory -VI $connection.VI -Server $Server -ExcludeVmPattern $ExcludeVmPattern
        $stats = Get-MemTierStatistics -VI $connection.VI -Inventory $inventory -StartUtc $startUtc -BatchSize $BatchSize
        $hostMeta = $inventory.HostMeta
        $counts = $inventory.Counts

        $nowIso = ConvertTo-MemTierIso $NowUtc
        $startIso = ConvertTo-MemTierIso $startUtc
        $hostsWithout = 0
        $hostList = New-Object 'System.Collections.Generic.List[object]'
        foreach ($k in $hostMeta.Keys) { $hostList.Add($k) }
        foreach ($mid in (Get-MemTierOrdinalSorted $hostList { param($x) $hostMeta[$x].Cluster + [char]1 + $hostMeta[$x].Name })) {
            $h = $hostMeta[$mid]
            $st = if ($stats.Hosts.ContainsKey($mid)) { $stats.Hosts[$mid] } else { Get-MemTierEmptyStats }
            if ($h.State -eq 'connected' -and $st.Active.Samples -eq 0) { $hostsWithout++ }
            $hostRows.Add(@{
                Timestamp = $nowIso; WindowStart = $startIso; VCenter = $Server; Cluster = $h.Cluster; VMHost = $h.Name; HostId = $mid
                ConnectionState = $h.State; MaintenanceMode = $h.Maint.ToString().ToLowerInvariant(); TieringType = $h.Tiering
                PhysicalMB = $h.Phys; DramMB = $h.Dram; NvmeTierMB = $h.Nvme; VMsOn = $h.VmsOn; AssignedMB = $h.Assigned
                Samples = $st.Active.Samples; ActiveAvgMB = $st.Active.Avg; ActiveP95MB = $st.Active.P95; ActiveMaxMB = $st.Active.Max
                ConsumedAvgMB = $st.Consumed.Avg; ConsumedMaxMB = $st.Consumed.Max; BalloonMaxMB = $st.Balloon.Max; SwapUsedMaxMB = $st.Swap.Max
            })
        }

        $vmsWithout = $counts.Unreachable
        $vmList = New-Object 'System.Collections.Generic.List[object]'
        foreach ($v in $inventory.Vms) { if ($v.Collect) { $vmList.Add($v) } }
        foreach ($v in (Get-MemTierOrdinalSorted $vmList { param($x) $x.Name.ToLowerInvariant() })) {
            $st = $stats.Vms[$v.Id]
            if (-not $st -or $st.Active.Samples -eq 0) { $vmsWithout++; continue }
            $vmRows.Add(@{
                Timestamp = $nowIso; WindowStart = $startIso; VCenter = $Server; Cluster = $v.Cluster; VMHost = $v.Host
                VM = $v.Name; VMId = $v.Id; AssignedMB = $v.Assigned; ReservationMB = $v.Reservation; LatencySensitivity = $v.Latency
                Samples = $st.Active.Samples; ActiveAvgMB = $st.Active.Avg; ActiveP95MB = $st.Active.P95; ActiveMaxMB = $st.Active.Max
                ConsumedAvgMB = $st.Consumed.Avg; ConsumedMaxMB = $st.Consumed.Max; BalloonMaxMB = $st.Balloon.Max; SwappedMaxMB = $st.Swap.Max
            })
        }

        $partial = ($stats.HostsFailed -gt 0) -or ($stats.VmsFailed -gt 0) -or ($hostsWithout -gt 0)
        $run.Status = if ($partial) { 'partial' } else { 'ok' }
        $run.Hosts = $hostMeta.Count; $run.HostsConnected = $stats.LiveHosts
        $run.VMsTotal = $counts.Total; $run.VMsOn = $counts.On; $run.VMsOff = $counts.Off; $run.VMsSuspended = $counts.Suspended
        $run.Templates = $counts.Templates; $run.VMsExcluded = $counts.Excluded; $run.VMsWithoutStats = $vmsWithout
        $run.HostsWithoutStats = $hostsWithout
        $run.Message = if ($partial) { 'perf query failed for {0} hosts, {1} VMs' -f $stats.HostsFailed, $stats.VmsFailed } else { '' }
        Write-MemTierLog ('{0}: {1} hosts ({2} connected), {3} VMs ({4} powered on, {5} with stats), {6} templates' -f
            $Server, $hostMeta.Count, $stats.LiveHosts, $counts.Total, $counts.On, $vmRows.Count, $counts.Templates)
    }
    catch {
        Write-MemTierLog "${Server}: collection failed: $($_.Exception.Message)" 'ERROR'
        $msg = $_.Exception.Message
        $run.Message = if ($msg.Length -gt 500) { $msg.Substring(0, 500) } else { $msg }
        $hostRows.Clear(); $vmRows.Clear()
    }
    finally {
        Disconnect-MemTierVCenter -Connection $connection -Server $Server
        $run.DurationSec = [long][math]::Round($sw.Elapsed.TotalSeconds)
    }
    @{ HostRows = $hostRows; VmRows = $vmRows; Run = $run }
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
$exitCode = 0
$mutex = New-Object System.Threading.Mutex($false, 'Global\MemTier-Collector')
$hasLock = $false
try {
    try { $hasLock = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $hasLock = $true }
    if (-not $hasLock) { throw 'another collector run is still active - exiting' }

    Import-MemTierPowerCLI
    $cred = $Credential
    if (-not $cred -and $CredentialFile) { $cred = Import-Clixml -LiteralPath $CredentialFile }

    $nowUtc = Get-MemTierUtcNow
    $month = $nowUtc.ToString('yyyy-MM', $script:Inv)
    foreach ($server in $VCenterServer) {
        $result = Invoke-VCenterCollection -Server $server -NowUtc $nowUtc -Cred $cred
        Add-MemTierCsv (Join-Path $DataDir "host-$month.csv") $script:HostFields $result.HostRows
        Add-MemTierCsv (Join-Path $DataDir "vm-$month.csv") $script:VmFields $result.VmRows
        Add-MemTierCsv (Join-Path $DataDir "run-$month.csv") $script:RunFields @($result.Run)
        if ($result.Run.Status -eq 'failed') { $exitCode = 1 }
        elseif ($result.Run.Status -eq 'partial' -and $exitCode -eq 0) { $exitCode = 2 }
    }
    Invoke-MemTierFileMaintenance -DataDir $DataDir -NowUtc $nowUtc -Compress $CompressOldMonths.IsPresent -RetentionMonths $RetentionMonths
}
catch {
    Write-MemTierLog $_.Exception.Message 'ERROR'
    $exitCode = 1
}
finally {
    if ($hasLock) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
    if ($transcript) { Stop-Transcript | Out-Null }
}

# Rebuild the report after the lock is released: a slow report must not block the next collection
if (-not $NoReport) {
    $reportArgs = @{ DataDir = $DataDir; LogDir = $LogDir }
    foreach ($name in 'ReportDir', 'Days', 'CandidatePct', 'ThresholdPct', 'TierRatio', 'Language', 'ColdPct', 'HotPct',
        'Title', 'SupportContact', 'StretchedCluster', 'StretchedClusterName') {
        if ($PSBoundParameters.ContainsKey($name)) { $reportArgs[$name] = $PSBoundParameters[$name] }
    }
    $reportArgs['IntervalMinutes'] = $IntervalMinutes   # the report judges run coverage against it
    & (Join-Path $PSScriptRoot 'New-MemTierReport.ps1') @reportArgs
    if ($LASTEXITCODE -ne 0) { $exitCode = 1 }
}
exit $exitCode
