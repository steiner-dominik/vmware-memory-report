<#
.SYNOPSIS
    Runs Get-MemTierSnapshot.ps1 against a mocked PowerCLI inventory - no vCenter needed.
.DESCRIPTION
    Replaces Connect-VIServer, Disconnect-VIServer and Get-View with in-memory fakes that model
    the same fictitious environment as generate-mock-data.py (example.com names) and returns
    deterministic real-time performance samples. Used to build examples/mock-snapshot-report.html
    and as a smoke test of the snapshot script.

    Requires the PowerCLI modules to be installed (only for the VMware.Vim data types).
.EXAMPLE
    ./examples/New-MockSnapshotReport.ps1 -OutputPath ./examples/mock-snapshot-report.html
#>
[CmdletBinding()]
param (
    [string]$OutputPath = (Join-Path $PSScriptRoot 'mock-snapshot-report.html'),
    [int]$Seed = 7
)

$ErrorActionPreference = 'Stop'
Import-Module VMware.Vim -ErrorAction Stop

function New-MoRef([string]$Type, [string]$Value) {
    $m = New-Object VMware.Vim.ManagedObjectReference
    $m.Type = $Type; $m.Value = $Value
    $m
}

$rnd = New-Object System.Random $Seed
$model = @(
    # vCenter, cluster, hosts, DRAM GB, NVMe GB, active load, consumed load, host prefix
    @('vcenter01.example.com', 'Metro-Stretched', 8, 1024, 0, 0.16, 0.58, 'esx-metro'),
    @('vcenter01.example.com', 'Compute', 4, 768, 0, 0.34, 0.62, 'esx-comp'),
    # Tiering already on: consumed is above DRAM, so part of it is served by the NVMe tier.
    @('vcenter01.example.com', 'VDI', 4, 512, 512, 0.22, 1.45, 'esx-vdi'),
    @('vcenter02.example.com', 'Branch', 2, 384, 0, 0.12, 0.40, 'esx-branch')
)
$global:MockInventory = @{}
$global:MockLoad = @{}
$n = 0; $vmNo = 0
foreach ($m in $model) {
    $vc = $m[0]
    if (-not $global:MockInventory.ContainsKey($vc)) { $global:MockInventory[$vc] = @{ Clusters = @(); Hosts = @(); Vms = @() } }
    $inv = $global:MockInventory[$vc]
    $clusterRef = New-MoRef ClusterComputeResource ("domain-c{0}" -f ($inv.Clusters.Count + 10))
    $inv.Clusters += [pscustomobject]@{ MoRef = $clusterRef; Name = $m[1] }
    for ($i = 1; $i -le $m[2]; $i++) {
        $n++
        $bytes = [long]$m[3] * 1GB
        $tiers = @([pscustomobject]@{ Type = 'DRAM'; Size = $bytes })
        if ($m[4]) { $tiers += [pscustomobject]@{ Type = 'NVMe'; Size = [long]$m[4] * 1GB } }
        $hostRef = New-MoRef HostSystem "host-$n"
        $inv.Hosts += [pscustomobject]@{
            MoRef = $hostRef; Name = ('{0}-{1:00}.example.com' -f $m[7], $i); Parent = $clusterRef
            Hardware = [pscustomobject]@{ MemorySize = $bytes; MemoryTieringType = $(if ($m[4]) { 'softwareTiering' } else { 'noTiering' }); MemoryTierInfo = $tiers }
            Runtime = [pscustomobject]@{ ConnectionState = 'connected'; InMaintenanceMode = $false }
        }
        $global:MockLoad["host-$n"] = @{ Kb = [long]$m[3] * 1048576; Active = $m[5] * (0.85 + 0.4 * $rnd.NextDouble()); Consumed = $m[6] }
        # Assigned memory is usually above consumed; a tier lets consumed exceed DRAM.
        $budget = [math]::Min([long]($m[3] + $m[4]) * 1024, [long]$m[3] * 1024 * $m[6]) * 1.1
        while ($budget -gt 0) {
            $vmNo++
            $size = if ($m[1] -eq 'VDI') { @(4096, 8192)[$rnd.Next(2)] } else { @(4096, 8192, 8192, 16384, 16384, 32768, 65536)[$rnd.Next(7)] }
            $power = if ($vmNo % 9 -eq 0) { 'poweredOff' } else { 'poweredOn' }
            $prefix = @{ 'Metro-Stretched' = 'app'; 'Compute' = 'sql'; 'VDI' = 'vdi'; 'Branch' = 'fs' }[$m[1]]
            $inv.Vms += [pscustomobject]@{
                MoRef = (New-MoRef VirtualMachine "vm-$vmNo"); Name = ('{0}-{1:000}' -f $prefix, $vmNo)
                Runtime = [pscustomobject]@{ PowerState = $power; Host = $hostRef; ConnectionState = 'connected' }
                Config = [pscustomobject]@{ Template = $false; Hardware = [pscustomobject]@{ MemoryMB = $size }
                    MemoryAllocation = [pscustomobject]@{ Reservation = 0 }; LatencySensitivity = [pscustomobject]@{ Level = 'normal' } }
            }
            $global:MockLoad["vm-$vmNo"] = @{ Kb = [long]$size * 1024; Active = [math]::Pow($rnd.NextDouble(), 2.2); Consumed = 0.6; Balloon = ($vmNo % 53 -eq 0) }
            $budget -= $size
        }
    }
}

$counterNames = @('cpu.usage.average', 'mem.active.average', 'mem.consumed.average', 'mem.vmmemctl.average', 'mem.swapped.average', 'mem.swapused.average')
$global:MockPerf = [pscustomobject]@{
    PerfCounter = for ($i = 0; $i -lt $counterNames.Count; $i++) {
        $parts = $counterNames[$i].Split('.')
        [pscustomobject]@{ Key = $i + 1; GroupInfo = [pscustomobject]@{ Key = $parts[0] }; NameInfo = [pscustomobject]@{ Key = $parts[1] }; RollupType = [VMware.Vim.PerfSummaryType]::average }
    }
}
$global:MockPerf | Add-Member ScriptMethod QueryPerf {
    param($Specs)
    foreach ($s in $Specs) {
        $load = $global:MockLoad[$s.Entity.Value]
        $series = foreach ($metric in $s.MetricId) {
            $values = New-Object 'long[]' 180
            for ($k = 0; $k -lt 180; $k++) {
                $f = switch ($metric.CounterId) {
                    2 { $load.Active * (0.85 + 0.3 * [math]::Sin($k / 17.0)) }
                    3 { $load.Consumed }
                    4 { if ($load.Balloon) { 0.03 } else { 0.0 } }
                    default { 0.0 }
                }
                $values[$k] = [long]($load.Kb * $f)
            }
            [pscustomobject]@{ Id = [pscustomobject]@{ CounterId = $metric.CounterId; Instance = '' }; Value = $values }
        }
        [pscustomobject]@{ Entity = $s.Entity; Value = @($series) }
    }
}

function global:Connect-VIServer {
    [CmdletBinding()] param($Server, $Credential, [switch]$NotDefault)
    [pscustomobject]@{ Name = $Server; IsConnected = $true }
}
function global:Disconnect-VIServer { [CmdletBinding(SupportsShouldProcess = $true)] param($Server, [switch]$Force) }
function global:Get-View {
    [CmdletBinding()] param([Parameter(Position = 0)]$VIObject, $Server, $ViewType, [string[]]$Property, $Id)
    if ($VIObject -eq 'ServiceInstance') { return [pscustomobject]@{ Content = [pscustomobject]@{ PerfManager = (New-MoRef PerformanceManager PerfMgr) } } }
    if ($Id) { return $global:MockPerf }
    $inv = $global:MockInventory[$Server.Name]
    switch ($ViewType) {
        'ClusterComputeResource' { $inv.Clusters }
        'HostSystem' { $inv.Hosts }
        'VirtualMachine' { $inv.Vms }
    }
}

$work = Join-Path ([System.IO.Path]::GetTempPath()) ("memtier-mock-" + [guid]::NewGuid())
try {
    & (Join-Path (Join-Path (Split-Path $PSScriptRoot -Parent) 'powershell') 'Get-MemTierSnapshot.ps1') -VCenterServer 'vcenter01.example.com', 'vcenter02.example.com' `
        -ExportDir $work -StretchedClusterName 'Metro-Stretched' -Title 'VMware Memory Tiering Snapshot (mock data)'
    $html = Get-ChildItem -LiteralPath $work -Filter '*.html' | Select-Object -First 1
    Copy-Item -LiteralPath $html.FullName -Destination $OutputPath -Force
    Write-Host "mock snapshot report: $OutputPath" -ForegroundColor Green
}
finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
