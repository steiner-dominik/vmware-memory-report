<#
.SYNOPSIS
    VMware NVMe Memory Tiering: builds the HTML trend report (PowerShell edition).
.DESCRIPTION
    Reads the monthly CSV files written by Invoke-MemTierCollector.ps1 (or by
    python/memtier.py), aggregates them and writes one self-contained HTML file
    (no CDN, works offline) based on template\memtier-report.template.html.

    The report shows per host and per cluster (N+1) how much of DRAM is actively
    used over time, a weekday x hour heatmap, and per-VM worst-day P95 values.
.PARAMETER Days
    Days of history to include (default 30).
.PARAMETER StretchedCluster
    All clusters are stretched across two sites: capacity after a failure is one site (50% of the
    cluster) instead of the cluster without its largest host (N+1). The mode can also be switched in the report.
.PARAMETER StretchedClusterName
    Names of the stretched clusters, if only some clusters are stretched.
.PARAMETER ThresholdPct
    Tiering guidance: host active memory should stay at or below this % of DRAM.
    Broadcom's guidance for the default 1:1 DRAM:NVMe ratio is 50%.
.EXAMPLE
    .\New-MemTierReport.ps1 -DataDir D:\MemTier\data -ReportDir D:\MemTier\reports -Days 30
#>
[CmdletBinding()]
param (
    [string]$DataDir,
    [string]$ReportDir,
    [string]$LogDir,
    [ValidateRange(1, 800)][int]$Days = 30,
    [double]$ThresholdPct = 50,
    [switch]$StretchedCluster,
    [Alias('StretchedClusters')]
    [string[]]$StretchedClusterName = @(),
    [double]$ColdPct = 40,
    [double]$HotPct = 75,
    [string]$Title = 'VMware Memory Tiering Report',
    [string]$SupportContact = 'dominik.steiner@nts.eu',
    [string]$TemplatePath,
    [string]$OutputPath,
    [int]$KeepReports = 30
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'MemTier.Common.ps1')

if (-not $DataDir) { $DataDir = Join-Path $PSScriptRoot 'data' }
if (-not $ReportDir) { $ReportDir = Join-Path $PSScriptRoot 'reports' }
if (-not $LogDir) { $LogDir = Join-Path $PSScriptRoot 'logs' }
if (-not $TemplatePath) {
    $candidates = @((Join-Path $PSScriptRoot 'memtier-report.template.html'),
        (Join-Path (Split-Path $PSScriptRoot -Parent) (Join-Path 'template' 'memtier-report.template.html')))
    $TemplatePath = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $TemplatePath) { throw "report template not found (looked in: $($candidates -join ', '))" }
}
# "powershell.exe -File" passes "A,B" as one string
$StretchedClusterName = @($StretchedClusterName | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$transcript = Start-MemTierTranscript -LogDir $LogDir -Name 'report'

function Get-CsvPaths {
    # Data files of one kind whose month is not older than the cutoff month, oldest first
    param([string]$Kind, [datetime]$CutoffUtc)
    $cutoffMonth = $CutoffUtc.ToString('yyyy-MM', $script:Inv)
    foreach ($file in @(Get-MemTierDataFiles -DataDir $DataDir -Kind $Kind)) {
        if ([string]::CompareOrdinal($file.Month, $cutoffMonth) -ge 0) { $file.Path }
    }
}

# Timestamps repeat for every row of a run - parse each distinct value once
$script:TsCache = @{}
function Get-RowEpoch([string]$Text) {
    $ts = $script:TsCache[$Text]
    if ($null -eq $ts) {
        try { $ts = Get-MemTierEpoch (ConvertFrom-MemTierIso $Text) } catch { $ts = [long]::MinValue }
        $script:TsCache[$Text] = $ts
    }
    $ts
}

function Get-OrdinalOrder {
    param([string[]]$Keys, [string[]]$Items)
    $k = [string[]]$Keys.Clone(); $v = [string[]]$Items.Clone()
    [Array]::Sort([Array]$k, [Array]$v, [System.Collections.IComparer][StringComparer]::Ordinal)
    $v
}

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$exitCode = 0
try {
    $nowUtc = Get-MemTierUtcNow
    $cutoffUtc = $nowUtc.AddDays(-$Days)
    $cutoffTs = Get-MemTierEpoch $cutoffUtc
    $nowTs = Get-MemTierEpoch $nowUtc
    $bucketHours = [math]::Max(1, [int][math]::Ceiling($Days * 24 / 1500.0))
    $bucket = [long]($bucketHours * 3600)

    # Hot loops below avoid function calls per field on purpose: PowerShell call overhead
    # dominates on large monthly files. [long]::TryParse leaves 0 in the out variable on failure.

    # ---------------- hosts ----------------
    $hosts = @{}
    foreach ($path in @(Get-CsvPaths 'host' $cutoffUtc)) {
        Write-MemTierLog "reading $path" 'DEBUG'
        $csv = Open-MemTierCsv $path $script:HostFields
        if (-not $csv) { continue }
        try {
            $p = $csv.Parser; $ix = $csv.Index; $cols = $csv.Count
            $iTs = $ix.Timestamp; $iVc = $ix.VCenter; $iHostId = $ix.HostId; $iName = $ix.VMHost; $iCluster = $ix.Cluster
            $iTier = $ix.TieringType; $iPhys = $ix.PhysicalMB; $iDram = $ix.DramMB; $iNvme = $ix.NvmeTierMB; $iSamples = $ix.Samples
            $iAvg = $ix.ActiveAvgMB; $iP95 = $ix.ActiveP95MB; $iMax = $ix.ActiveMaxMB; $iCons = $ix.ConsumedAvgMB; $iVms = $ix.VMsOn
            $iConsMax = $ix.ConsumedMaxMB
            $iAssigned = $ix.AssignedMB; $iBalloon = $ix.BalloonMaxMB; $iSwap = $ix.SwapUsedMaxMB
            while (-not $p.EndOfData) {
                $f = $p.ReadFields()
                if ($null -eq $f -or $f.Count -lt $cols) { continue }
                $ts = Get-RowEpoch $f[$iTs]
                if ($ts -lt $cutoffTs) { continue }
                $key = $f[$iVc] + '|' + $f[$iHostId]
                $h = $hosts[$key]
                if ($null -eq $h) {
                    $h = @{ key = $key; vc = $f[$iVc]; buckets = @{}; lastTs = [long]::MinValue }
                    $hosts[$key] = $h
                }
                $phys = 0L; [void][long]::TryParse($f[$iPhys], [ref]$phys)
                $dram = 0L; [void][long]::TryParse($f[$iDram], [ref]$dram)
                if (-not $dram) { $dram = $phys }
                if ($ts -ge $h.lastTs) {
                    # latest metadata wins
                    $h.lastTs = $ts; $h.name = $f[$iName]; $h.cluster = $f[$iCluster]; $h.tiering = $f[$iTier]
                    $nvme = 0L; [void][long]::TryParse($f[$iNvme], [ref]$nvme)
                    $h.dramMB = $dram; $h.nvmeMB = $nvme; $h.physMB = $phys
                }
                $avg = 0L
                if (-not [long]::TryParse($f[$iAvg], [ref]$avg)) { continue }
                $samples = 0L; [void][long]::TryParse($f[$iSamples], [ref]$samples)
                if ($samples -lt 1) { $samples = 1L }
                $b = [long]([math]::Floor($ts / $bucket) * $bucket)
                $acc = $h.buckets[$b]
                if ($null -eq $acc) {
                    $acc = @{ w = 0L; avg = 0.0; cons = 0.0; consW = 0L; consMax = 0L; consMaxN = 0L; vms = 0L; assigned = 0L; p95 = 0L; max = 0L; balloon = 0L; swap = 0L; dram = 0L; dramTs = [long]::MinValue }
                    $h.buckets[$b] = $acc
                }
                $acc.w += $samples
                $acc.avg += [double]$avg * $samples
                $x = 0L
                if ([long]::TryParse($f[$iCons], [ref]$x)) { $acc.cons += [double]$x * $samples; $acc.consW += $samples }
                $x = 0L
                if ([long]::TryParse($f[$iConsMax], [ref]$x)) { if ($x -gt $acc.consMax) { $acc.consMax = $x }; $acc.consMaxN++ }
                $x = 0L; [void][long]::TryParse($f[$iVms], [ref]$x); if ($x -gt $acc.vms) { $acc.vms = $x }
                $x = 0L; [void][long]::TryParse($f[$iAssigned], [ref]$x); if ($x -gt $acc.assigned) { $acc.assigned = $x }
                $x = 0L; [void][long]::TryParse($f[$iP95], [ref]$x); if ($x -gt $acc.p95) { $acc.p95 = $x }
                $x = 0L; [void][long]::TryParse($f[$iMax], [ref]$x); if ($x -gt $acc.max) { $acc.max = $x }
                $x = 0L; [void][long]::TryParse($f[$iBalloon], [ref]$x); if ($x -gt $acc.balloon) { $acc.balloon = $x }
                $x = 0L; [void][long]::TryParse($f[$iSwap], [ref]$x); if ($x -gt $acc.swap) { $acc.swap = $x }
                if ($ts -ge $acc.dramTs) { $acc.dramTs = $ts; $acc.dram = $dram }
            }
        }
        finally { Close-MemTierCsv $csv }
    }

    # ---------------- VMs ----------------
    $day0 = [long]([math]::Floor($cutoffTs / 86400) * 86400)
    $nDays = [int]([math]::Floor($nowTs / 86400) - [math]::Floor($cutoffTs / 86400) + 1)
    $vms = @{}
    foreach ($path in @(Get-CsvPaths 'vm' $cutoffUtc)) {
        Write-MemTierLog "reading $path" 'DEBUG'
        $csv = Open-MemTierCsv $path $script:VmFields
        if (-not $csv) { continue }
        try {
            $p = $csv.Parser; $ix = $csv.Index; $cols = $csv.Count
            $iTs = $ix.Timestamp; $iVc = $ix.VCenter; $iId = $ix.VMId; $iName = $ix.VM; $iCluster = $ix.Cluster; $iHost = $ix.VMHost
            $iAssigned = $ix.AssignedMB; $iRes = $ix.ReservationMB; $iLat = $ix.LatencySensitivity; $iSamples = $ix.Samples
            $iAvg = $ix.ActiveAvgMB; $iP95 = $ix.ActiveP95MB; $iMax = $ix.ActiveMaxMB; $iBalloon = $ix.BalloonMaxMB; $iSwap = $ix.SwappedMaxMB
            $iCons = $ix.ConsumedAvgMB; $iConsMax = $ix.ConsumedMaxMB
            while (-not $p.EndOfData) {
                $f = $p.ReadFields()
                if ($null -eq $f -or $f.Count -lt $cols) { continue }
                $ts = Get-RowEpoch $f[$iTs]
                if ($ts -lt $cutoffTs) { continue }
                $assigned = 0L; [void][long]::TryParse($f[$iAssigned], [ref]$assigned)
                $avg = 0L
                if (-not $assigned -or -not [long]::TryParse($f[$iAvg], [ref]$avg)) { continue }
                $key = $f[$iVc] + '|' + $f[$iId]
                $v = $vms[$key]
                if ($null -eq $v) { $v = @{ days = @{}; lastTs = [long]::MinValue }; $vms[$key] = $v }
                if ($ts -ge $v.lastTs) {
                    $v.lastTs = $ts; $v.vc = $f[$iVc]; $v.name = $f[$iName]; $v.cluster = $f[$iCluster]; $v.host = $f[$iHost]
                    $v.assignedMB = $assigned; $v.latency = $f[$iLat]
                    $x = 0L
                    $v.reservationMB = if ([long]::TryParse($f[$iRes], [ref]$x)) { $x } else { $null }
                }
                $di = [int](([math]::Floor($ts / 86400) * 86400 - $day0) / 86400)
                if ($di -lt 0 -or $di -ge $nDays) { continue }
                $samples = 0L; [void][long]::TryParse($f[$iSamples], [ref]$samples)
                if ($samples -lt 1) { $samples = 1L }
                $d = $v.days[$di]
                if ($null -eq $d) {
                    $d = @{ hours = 0L; num = 0.0; den = 0.0; p95 = (New-Object 'System.Collections.Generic.List[double]'); max = 0.0; balloon = 0L; swap = 0L
                        cnum = 0.0; cden = 0.0; cmax = $null }
                    $v.days[$di] = $d
                }
                $d.hours++
                $d.num += [double]$avg * $samples
                $d.den += [double]$assigned * $samples
                $x = 0L; [void][long]::TryParse($f[$iP95], [ref]$x); $d.p95.Add([double]$x * 100.0 / $assigned)
                $x = 0L; [void][long]::TryParse($f[$iMax], [ref]$x); $pct = [double]$x * 100.0 / $assigned; if ($pct -gt $d.max) { $d.max = $pct }
                $x = 0L; [void][long]::TryParse($f[$iBalloon], [ref]$x); if ($x -gt $d.balloon) { $d.balloon = $x }
                $x = 0L; [void][long]::TryParse($f[$iSwap], [ref]$x); if ($x -gt $d.swap) { $d.swap = $x }
                $x = 0L
                if ([long]::TryParse($f[$iCons], [ref]$x)) { $d.cnum += [double]$x * $samples; $d.cden += [double]$assigned * $samples }
                $x = 0L
                if ([long]::TryParse($f[$iConsMax], [ref]$x)) { $cpct = [double]$x * 100.0 / $assigned; if ($null -eq $d.cmax -or $cpct -gt $d.cmax) { $d.cmax = $cpct } }
            }
        }
        finally { Close-MemTierCsv $csv }
    }

    # ---------------- runs ----------------
    $runs = New-Object 'System.Collections.Generic.List[object]'
    foreach ($path in @(Get-CsvPaths 'run' $cutoffUtc)) {
        $csv = Open-MemTierCsv $path $script:RunFields
        if (-not $csv) { continue }
        try {
            $p = $csv.Parser; $ix = $csv.Index; $cols = $csv.Count
            while (-not $p.EndOfData) {
                $f = $p.ReadFields()
                if ($null -eq $f -or $f.Count -lt $cols) { continue }
                $ts = Get-RowEpoch $f[$ix.Timestamp]
                if ($ts -lt $cutoffTs) { continue }
                $row = @{ _ts = $ts }
                foreach ($name in $script:RunFields) { $row[$name] = $f[$ix[$name]] }
                $runs.Add($row)
            }
        }
        finally { Close-MemTierCsv $csv }
    }

    # ---------------- JSON ----------------
    $J = { param($s) ConvertTo-MemTierJsonString $s }
    $N = { param($n) ConvertTo-MemTierJsonNumber $n }
    $sb = New-Object System.Text.StringBuilder (1024 * 1024)
    $vcSet = New-Object 'System.Collections.Generic.SortedSet[string]' ([StringComparer]::Ordinal)

    # hosts, ordered by (vCenter, cluster, name)
    $hostKeys = [string[]]@($hosts.Keys)
    $hostOrder = if ($hostKeys.Count) { Get-OrdinalOrder ($hostKeys | ForEach-Object { $h = $hosts[$_]; $h.vc + [char]1 + $h.cluster + [char]1 + $h.name }) $hostKeys } else { @() }
    $hostJson = New-Object 'System.Collections.Generic.List[string]'
    foreach ($key in $hostOrder) {
        $h = $hosts[$key]
        [void]$vcSet.Add($h.vc)
        $bk = [long[]]@($h.buckets.Keys)
        [Array]::Sort([Array]$bk)
        $points = foreach ($b in $bk) {
            $a = $h.buckets[$b]
            $consAvg = if ($a.consW) { Get-RoundHalfUp ($a.cons / $a.consW) } else { $null }
            $consMax = if ($a.consMaxN) { $a.consMax } else { $null }
            '[' + ((& $N $b), (& $N $a.vms), (& $N $a.assigned), (& $N (Get-RoundHalfUp ($a.avg / $a.w))), (& $N $a.p95), (& $N $a.max),
                (& $N $consAvg), (& $N $a.balloon), (& $N $a.swap), (& $N $a.dram), (& $N $consMax) -join ',') + ']'
        }
        $hostJson.Add(('{{"key":{0},"vc":{1},"name":{2},"cluster":{3},"tiering":{4},"dramMB":{5},"nvmeMB":{6},"physMB":{7},"s":[{8}]}}' -f
                (& $J $h.key), (& $J $h.vc), (& $J $h.name), (& $J $h.cluster), (& $J $h.tiering),
                (& $N $h.dramMB), (& $N $h.nvmeMB), (& $N $h.physMB), (@($points) -join ',')))
    }

    # VMs, ordered by (vCenter, lower-case name, key)
    $vmKeys = [string[]]@($vms.Keys)
    $vmOrder = if ($vmKeys.Count) { Get-OrdinalOrder ($vmKeys | ForEach-Object { $v = $vms[$_]; $v.vc + [char]1 + $v.name.ToLowerInvariant() + [char]1 + $_ }) $vmKeys } else { @() }
    $vmJson = New-Object 'System.Collections.Generic.List[string]'
    foreach ($key in $vmOrder) {
        $v = $vms[$key]
        [void]$vcSet.Add($v.vc)
        $daily = New-Object 'System.Collections.Generic.List[string]'
        for ($i = 0; $i -lt $nDays; $i++) {
            $d = $v.days[$i]
            if ($null -eq $d) { $daily.Add('null'); continue }
            $cAvg = if ($d.cden) { Get-Round1 ($d.cnum * 100.0 / $d.cden) } else { $null }
            $cMax = if ($null -ne $d.cmax) { Get-Round1 $d.cmax } else { $null }
            $daily.Add('[' + ((& $N $d.hours), (& $N (Get-Round1 ($d.num * 100.0 / $d.den))), (& $N (Get-Round1 (Get-MemTierP95 $d.p95))),
                    (& $N (Get-Round1 $d.max)), (& $N $d.balloon), (& $N $d.swap), (& $N $cAvg), (& $N $cMax) -join ',') + ']')
        }
        $vmJson.Add(('{{"id":{0},"vc":{1},"name":{2},"cluster":{3},"host":{4},"assignedMB":{5},"reservationMB":{6},"latency":{7},"lastTs":{8},"day0":{9},"d":[{10}]}}' -f
                (& $J $key), (& $J $v.vc), (& $J $v.name), (& $J $v.cluster), (& $J $v.host), (& $N $v.assignedMB),
                (& $N $v.reservationMB), (& $J $v.latency), (& $N $v.lastTs), (& $N $day0), ($daily -join ',')))
    }

    $runJson = New-Object 'System.Collections.Generic.List[string]'
    foreach ($r in $runs) {
        [void]$vcSet.Add($r['VCenter'])
        $runJson.Add('[' + ((& $N $r['_ts']), (& $J $r['VCenter']), (& $J ([string]$r['Status'])), (& $N (ConvertTo-MemTierInt $r['Hosts'])),
                (& $N (ConvertTo-MemTierInt $r['HostsConnected'])), (& $N (ConvertTo-MemTierInt $r['VMsTotal'])), (& $N (ConvertTo-MemTierInt $r['VMsOn'])),
                (& $N (ConvertTo-MemTierInt $r['Templates'])), (& $N (ConvertTo-MemTierInt $r['DurationSec'])),
                (& $J ([string]$r['Message'])) -join ',') + ']')
    }

    $meta = '{{"title":{0},"support":{1},"generatedUtc":{2},"fromUtc":{3},"toUtc":{4},"days":{5},"thresholdPct":{6},"coldPct":{7},"hotPct":{8},"bucketHours":{9},"vcenters":[{10}],"builder":{11},"failover":{{"stretched":{12},"stretchedClusters":[{13}]}}}}' -f
        (& $J $Title), (& $J $SupportContact), (& $J (ConvertTo-MemTierIso $nowUtc)), (& $J (ConvertTo-MemTierIso $cutoffUtc)),
        (& $J (ConvertTo-MemTierIso $nowUtc)), (& $N $Days), (& $N $ThresholdPct), (& $N $ColdPct), (& $N $HotPct), (& $N $bucketHours),
        ((@($vcSet) | ForEach-Object { & $J $_ }) -join ','), (& $J "New-MemTierReport.ps1 $($script:MemTierVersion) (PowerShell $($PSVersionTable.PSVersion))"),
        $(if ($StretchedCluster) { 'true' } else { 'false' }), ((@($StretchedClusterName) | ForEach-Object { & $J $_ }) -join ',')

    [void]$sb.Append('{"schema":2,"meta":').Append($meta)
    [void]$sb.Append(',"runs":[').Append(($runJson -join ',')).Append(']')
    [void]$sb.Append(',"hosts":[').Append(($hostJson -join ',')).Append(']')
    [void]$sb.Append(',"vms":[').Append(($vmJson -join ',')).Append(']}')

    $template = [System.IO.File]::ReadAllText($TemplatePath, [System.Text.Encoding]::UTF8)
    $pos = $template.IndexOf($script:DataPlaceholder, [StringComparison]::Ordinal)
    if ($pos -lt 0) { throw "template does not contain the data placeholder $($script:DataPlaceholder)" }
    $html = $template.Substring(0, $pos) + $sb.ToString() + $template.Substring($pos + $script:DataPlaceholder.Length)

    if (-not $OutputPath -and -not (Test-Path -LiteralPath $ReportDir)) { New-Item -ItemType Directory -Path $ReportDir -Force | Out-Null }
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $dated = Join-Path $ReportDir ('MemTier_Report_{0}.html' -f (Get-Date).ToString('yyyy-MM-dd_HHmm', $script:Inv))
    $target = if ($OutputPath) { [System.IO.Path]::GetFullPath($OutputPath) } else { [System.IO.Path]::GetFullPath($dated) }
    $targetDir = [System.IO.Path]::GetDirectoryName($target)
    if ($targetDir -and -not (Test-Path -LiteralPath $targetDir)) { New-Item -ItemType Directory -Path $targetDir -Force | Out-Null }
    [System.IO.File]::WriteAllText($target, $html, $utf8)
    if (-not $OutputPath) {
        Copy-Item -LiteralPath $target -Destination (Join-Path $ReportDir 'MemTier_Report_latest.html') -Force
        if ($KeepReports -gt 0) {
            $old = @(Get-ChildItem -LiteralPath $ReportDir -Filter 'MemTier_Report_2*.html' | Sort-Object Name)
            if ($old.Count -gt $KeepReports) { $old[0..($old.Count - $KeepReports - 1)] | Remove-Item -Force }
        }
    }
    Write-MemTierLog ('report written: {0} ({1} hosts, {2} VMs, {3} runs, {4:N1} MB, {5:N1} s)' -f $target, $hostJson.Count, $vmJson.Count,
        $runJson.Count, ($utf8.GetByteCount($html) / 1MB), $sw.Elapsed.TotalSeconds)
}
catch {
    Write-MemTierLog $_.Exception.Message 'ERROR'
    Write-MemTierLog $_.ScriptStackTrace 'DEBUG'
    $exitCode = 1
}
finally {
    if ($transcript) { Stop-Transcript | Out-Null }
}
exit $exitCode
