<#
.SYNOPSIS
    VMware NVMe Memory Tiering: shared helpers for the collector and the report builder.
.DESCRIPTION
    Dot-sourced by Invoke-MemTierCollector.ps1 and New-MemTierReport.ps1.
    Compatible with Windows PowerShell 5.1 and PowerShell 7+.
    The CSV schema and all calculations are identical to python/memtier.py.
#>

$script:MemTierVersion = '1.1.0'
$script:Inv = [System.Globalization.CultureInfo]::InvariantCulture
$script:IsoFormat = "yyyy-MM-dd'T'HH:mm:ss'Z'"
$script:DataPlaceholder = '/*__MEMTIER_DATA__*/null'

$script:HostFields = @(
    'Timestamp', 'WindowStart', 'VCenter', 'Cluster', 'VMHost', 'HostId', 'ConnectionState', 'MaintenanceMode',
    'TieringType', 'PhysicalMB', 'DramMB', 'NvmeTierMB', 'VMsOn', 'AssignedMB', 'Samples',
    'ActiveAvgMB', 'ActiveP95MB', 'ActiveMaxMB', 'ConsumedAvgMB', 'ConsumedMaxMB', 'BalloonMaxMB', 'SwapUsedMaxMB'
)
$script:VmFields = @(
    'Timestamp', 'WindowStart', 'VCenter', 'Cluster', 'VMHost', 'VM', 'VMId', 'AssignedMB', 'ReservationMB',
    'LatencySensitivity', 'Samples', 'ActiveAvgMB', 'ActiveP95MB', 'ActiveMaxMB', 'ConsumedAvgMB', 'ConsumedMaxMB',
    'BalloonMaxMB', 'SwappedMaxMB'
)
$script:RunFields = @(
    'Timestamp', 'VCenter', 'Status', 'Hosts', 'HostsConnected', 'VMsTotal', 'VMsOn', 'VMsOff', 'VMsSuspended',
    'Templates', 'VMsExcluded', 'VMsWithoutStats', 'HostsWithoutStats', 'DurationSec', 'Message', 'TierCounters'
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
function Write-MemTierLog {
    param([string]$Message, [ValidateSet('INFO', 'WARN', 'ERROR', 'DEBUG')][string]$Level = 'INFO')
    if ($Level -eq 'DEBUG' -and $VerbosePreference -eq 'SilentlyContinue') { return }
    $line = '{0} {1,-5} {2}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss', $script:Inv), $Level, $Message
    $color = switch ($Level) { 'WARN' { 'Yellow' } 'ERROR' { 'Red' } 'DEBUG' { 'DarkGray' } default { 'Gray' } }
    Write-Host $line -ForegroundColor $color
}

function Start-MemTierTranscript {
    param([string]$LogDir, [string]$Name, [int]$KeepDays = 30)
    if (-not $LogDir) { return $false }
    if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    Get-ChildItem -LiteralPath $LogDir -Filter "memtier-$Name-*.log" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    $path = Join-Path $LogDir ('memtier-{0}-{1}.log' -f $Name, (Get-Date).ToString('yyyy-MM-dd', $script:Inv))
    try { Start-Transcript -Path $path -Append | Out-Null; return $true } catch { return $false }
}

# ---------------------------------------------------------------------------
# Time and math (must match memtier.py exactly)
# ---------------------------------------------------------------------------
function ConvertTo-MemTierIso([datetime]$Utc) { $Utc.ToString($script:IsoFormat, $script:Inv) }

# [string[]] matters: without the cast PowerShell binds the single-format ParseExact overload
# and flattens the array into one unusable format string.
$script:IsoFormats = [string[]]@(
    "yyyy-MM-dd'T'HH:mm:ss'Z'", "yyyy-MM-dd'T'HH:mm:ss.FFFFFFF'Z'",
    "yyyy-MM-dd'T'HH:mm:sszzz", "yyyy-MM-dd'T'HH:mm:ss.FFFFFFFzzz",
    "yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm:ss.FFFFFFF"
)
function ConvertFrom-MemTierIso([string]$Text) {
    # The collectors write plain "...Z", but a CSV touched by another tool may carry milliseconds
    # or a numeric offset. Rejecting those silently dropped every such row from the report.
    $styles = [System.Globalization.DateTimeStyles]::AssumeUniversal -bor [System.Globalization.DateTimeStyles]::AdjustToUniversal
    [datetime]::ParseExact($Text.Trim(), $script:IsoFormats, $script:Inv, $styles)
}

$script:EpochBase = New-Object DateTime 1970, 1, 1, 0, 0, 0, ([DateTimeKind]::Utc)
function Get-MemTierEpoch([datetime]$Utc) { [long][math]::Floor(($Utc - $script:EpochBase).TotalSeconds) }

function Get-MemTierUtcNow {
    $now = [datetime]::UtcNow
    $now.AddTicks( - ($now.Ticks % [TimeSpan]::TicksPerSecond))
}

function Get-RoundHalfUp([double]$Value) { [long][math]::Floor($Value + 0.5) }
function Get-Round1([double]$Value) { [math]::Floor($Value * 10 + 0.5) / 10 }

function Get-MemTierP95 {
    # Nearest-rank 95th percentile. Sorts the list in place.
    param([System.Collections.Generic.List[double]]$Values)
    if ($null -eq $Values -or $Values.Count -eq 0) { return $null }
    $Values.Sort()
    $Values[[int][math]::Ceiling(0.95 * $Values.Count) - 1]
}

function Get-MemTierSummary {
    # Reduce real-time KB samples to avg/p95/max in MB. Negative samples mean "no data".
    param($Samples)
    $valid = New-Object 'System.Collections.Generic.List[double]'
    $sum = 0.0
    if ($null -ne $Samples) {
        foreach ($s in $Samples) {
            if ($null -ne $s -and $s -ge 0) { $valid.Add([double]$s); $sum += [double]$s }
        }
    }
    if ($valid.Count -eq 0) { return @{ Avg = $null; P95 = $null; Max = $null; Samples = 0 } }
    $count = $valid.Count
    $avg = Get-RoundHalfUp ($sum / $count / 1024.0)
    $p95 = Get-RoundHalfUp ((Get-MemTierP95 $valid) / 1024.0)   # list is sorted now
    $max = Get-RoundHalfUp ($valid[$count - 1] / 1024.0)
    @{ Avg = $avg; P95 = $p95; Max = $max; Samples = $count }
}

function ConvertTo-MemTierInt([string]$Text) {
    if ($null -eq $Text) { return $null }
    $t = $Text.Trim()
    if ($t.Length -eq 0) { return $null }
    $l = 0L
    if ([long]::TryParse($t, [System.Globalization.NumberStyles]::Integer, $script:Inv, [ref]$l)) { return $l }
    $d = 0.0
    if ([double]::TryParse($t, [System.Globalization.NumberStyles]::Float, $script:Inv, [ref]$d)) { return [long][math]::Round($d) }
    return $null
}

# ---------------------------------------------------------------------------
# CSV storage (UTF-8 without BOM, minimal quoting, CRLF - same as memtier.py)
# ---------------------------------------------------------------------------
function ConvertTo-MemTierCsvField($Value) {
    if ($null -eq $Value) { return '' }
    if ($Value -is [bool]) { $s = $Value.ToString().ToLowerInvariant() }
    elseif ($Value -is [IFormattable]) { $s = $Value.ToString($null, $script:Inv) }
    else { $s = [string]$Value }
    if ($s.IndexOfAny([char[]]@(',', '"', "`r", "`n")) -ge 0) { return '"' + $s.Replace('"', '""') + '"' }
    return $s
}

function Expand-MemTierCsv {
    <# Rewrites a monthly file with additional trailing columns, empty for the existing rows. #>
    param([string]$Path, [int]$OldCount, [string[]]$Fields)
    Write-MemTierLog ("adding {0} new column(s) to {1}" -f ($Fields.Count - $OldCount), $Path)
    $pad = ',' * ($Fields.Count - $OldCount)
    $tmp = '{0}.{1}.tmp' -f $Path, $PID
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $reader = New-Object System.IO.StreamReader($Path, [System.Text.Encoding]::UTF8, $true)
    try {
        $writer = New-Object System.IO.StreamWriter($tmp, $false, $encoding)
        try {
            $writer.NewLine = "`r`n"
            [void]$reader.ReadLine()
            $writer.WriteLine($Fields -join ',')
            while ($null -ne ($line = $reader.ReadLine())) {
                if ($line.Length -gt 0) { $writer.WriteLine($line + $pad) }
            }
        }
        finally { $writer.Dispose() }
    }
    finally { $reader.Dispose() }
    [System.IO.File]::Replace($tmp, $Path, [NullString]::Value)
}

function Add-MemTierCsv {
    param([string]$Path, [string[]]$Fields, [System.Collections.IEnumerable]$Rows)
    $list = @($Rows)
    if ($list.Count -eq 0) { return }
    $exists = (Test-Path -LiteralPath $Path) -and ((Get-Item -LiteralPath $Path).Length -gt 0)
    if ($exists) {
        $reader = New-Object System.IO.StreamReader($Path, [System.Text.Encoding]::UTF8, $true)
        try { $header = $reader.ReadLine() } finally { $reader.Dispose() }
        if ($header -ne ($Fields -join ',')) {
            # Releases add columns. A file whose header is a prefix of the current one is widened
            # in place rather than refused, so an upgrade does not strand the running month.
            $old = @($header -split ',')
            $isPrefix = $old.Count -lt $Fields.Count -and (($Fields[0..($old.Count - 1)] -join ',') -eq $header)
            if ($isPrefix) { Expand-MemTierCsv -Path $Path -OldCount $old.Count -Fields $Fields }
            else { throw "$Path has an unexpected header - move it away and rerun" }
        }
    }
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $writer = New-Object System.IO.StreamWriter($Path, $true, $encoding)
    try {
        $writer.NewLine = "`r`n"
        if (-not $exists) { $writer.WriteLine($Fields -join ',') }
        foreach ($row in $list) {
            $values = foreach ($f in $Fields) { ConvertTo-MemTierCsvField $row[$f] }
            $writer.WriteLine(($values -join ','))
        }
    }
    finally { $writer.Dispose() }
}

function Get-MemTierDataFiles {
    # Returns a sorted list of @{ Month; Path } for host|vm|run, preferring .csv over .csv.gz
    param([string]$DataDir, [string]$Kind)
    $byMonth = @{}
    if (-not (Test-Path -LiteralPath $DataDir)) { return @() }
    foreach ($f in Get-ChildItem -LiteralPath $DataDir -File) {
        if ($f.Name -match "^$Kind-(\d{4}-\d{2})\.csv(\.gz)?$") {
            $month = $Matches[1]
            if (-not $byMonth.ContainsKey($month) -or -not $f.Name.EndsWith('.gz')) { $byMonth[$month] = $f.FullName }
        }
    }
    $months = [string[]]@($byMonth.Keys)
    [Array]::Sort([Array]$months, [System.Collections.IComparer][StringComparer]::Ordinal)
    foreach ($m in $months) { [pscustomobject]@{ Month = $m; Path = $byMonth[$m] } }
}

function Invoke-MemTierFileMaintenance {
    param([string]$DataDir, [datetime]$NowUtc, [bool]$Compress, [int]$RetentionMonths)
    $current = $NowUtc.ToString('yyyy-MM', $script:Inv)
    $cutoff = $null
    if ($RetentionMonths -gt 0) { $cutoff = $NowUtc.AddMonths(-$RetentionMonths).ToString('yyyy-MM', $script:Inv) }
    foreach ($f in Get-ChildItem -LiteralPath $DataDir -File) {
        if ($f.Name -notmatch '^(host|vm|run)-(\d{4}-\d{2})\.csv(\.gz)?$') { continue }
        $month = $Matches[2]
        if ($cutoff -and [string]::CompareOrdinal($month, $cutoff) -lt 0) {
            Write-MemTierLog "retention: deleting $($f.FullName)"
            Remove-Item -LiteralPath $f.FullName -Force
        }
        elseif ($Compress -and [string]::CompareOrdinal($month, $current) -lt 0 -and -not $f.Name.EndsWith('.gz')) {
            Write-MemTierLog "compressing $($f.FullName)"
            $src = [System.IO.File]::OpenRead($f.FullName)
            try {
                $dst = [System.IO.File]::Create($f.FullName + '.gz')
                try {
                    $gz = New-Object System.IO.Compression.GZipStream($dst, [System.IO.Compression.CompressionMode]::Compress)
                    try { $src.CopyTo($gz) } finally { $gz.Dispose() }
                }
                finally { $dst.Dispose() }
            }
            finally { $src.Dispose() }
            Remove-Item -LiteralPath $f.FullName -Force
        }
    }
}

$script:VisualBasicLoaded = $false
function Open-MemTierCsv {
    <#
        Opens a (optionally gzipped) CSV file for fast row-by-row reading.
        Returns @{ Parser; Index (column name -> position); Count } or $null for an empty file.
        TextFieldParser handles quoting correctly without the memory cost of Import-Csv.
        Close with Close-MemTierCsv.
    #>
    param([string]$Path, [string[]]$RequiredColumns)
    if (-not $script:VisualBasicLoaded) { Add-Type -AssemblyName Microsoft.VisualBasic; $script:VisualBasicLoaded = $true }
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        if ($Path.EndsWith('.gz')) {
            $stream = New-Object System.IO.Compression.GZipStream($stream, [System.IO.Compression.CompressionMode]::Decompress)
        }
        $parser = New-Object Microsoft.VisualBasic.FileIO.TextFieldParser($stream, [System.Text.Encoding]::UTF8, $true)
        $parser.TextFieldType = [Microsoft.VisualBasic.FileIO.FieldType]::Delimited
        $parser.SetDelimiters(',')
        $parser.HasFieldsEnclosedInQuotes = $true
        $parser.TrimWhiteSpace = $false
        if ($parser.EndOfData) { $parser.Dispose(); $stream.Dispose(); return $null }
        $header = $parser.ReadFields()
        $index = @{}
        for ($i = 0; $i -lt $header.Count; $i++) { $index[$header[$i]] = $i }
        foreach ($c in $RequiredColumns) {
            if (-not $index.ContainsKey($c)) { throw "$Path is missing column $c" }
        }
        return @{ Parser = $parser; Stream = $stream; Index = $index; Count = $header.Count }
    }
    catch {
        $stream.Dispose()
        throw
    }
}

function Close-MemTierCsv($Csv) {
    if ($Csv) { $Csv.Parser.Dispose(); $Csv.Stream.Dispose() }
}

# ---------------------------------------------------------------------------
# JSON writing (culture-invariant, safe to embed inside <script>)
# ---------------------------------------------------------------------------
function ConvertTo-MemTierJsonString([string]$Text) {
    if ($null -eq $Text) { return 'null' }
    $sb = New-Object System.Text.StringBuilder ($Text.Length + 2)
    [void]$sb.Append('"')
    foreach ($ch in $Text.ToCharArray()) {
        $code = [int]$ch
        switch ($code) {
            34 { [void]$sb.Append('\"') }
            92 { [void]$sb.Append('\\') }
            10 { [void]$sb.Append('\n') }
            13 { [void]$sb.Append('\r') }
            9 { [void]$sb.Append('\t') }
            default {
                if ($code -lt 32 -or $code -eq 60 -or $code -eq 62 -or $code -eq 38 -or $code -eq 0x2028 -or $code -eq 0x2029) {
                    [void]$sb.AppendFormat('\u{0:x4}', $code)
                }
                else { [void]$sb.Append($ch) }
            }
        }
    }
    [void]$sb.Append('"')
    $sb.ToString()
}

function ConvertTo-MemTierJsonNumber($Value) {
    if ($null -eq $Value) { return 'null' }
    if ($Value -is [double] -or $Value -is [single] -or $Value -is [decimal]) {
        $d = [double]$Value
        if ([double]::IsNaN($d) -or [double]::IsInfinity($d)) { return 'null' }
        return $d.ToString('R', $script:Inv)
    }
    return ([long]$Value).ToString($script:Inv)
}

function ConvertTo-MemTierJson {
    <# Minimal culture-invariant JSON writer for hashtables, ordered dictionaries, PSCustomObjects, arrays and scalars. #>
    param($Value)
    if ($null -eq $Value) { return 'null' }
    if ($Value -is [bool]) { if ($Value) { return 'true' } else { return 'false' } }
    if ($Value -is [string] -or $Value -is [char] -or $Value -is [enum]) { return ConvertTo-MemTierJsonString ([string]$Value) }
    if ($Value -is [datetime]) { return ConvertTo-MemTierJsonString (ConvertTo-MemTierIso $Value.ToUniversalTime()) }
    if ($Value -is [System.Collections.IDictionary]) {
        $parts = foreach ($k in $Value.Keys) { (ConvertTo-MemTierJsonString ([string]$k)) + ':' + (ConvertTo-MemTierJson $Value[$k]) }
        return '{' + (@($parts) -join ',') + '}'
    }
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        $parts = foreach ($p in $Value.PSObject.Properties) { (ConvertTo-MemTierJsonString $p.Name) + ':' + (ConvertTo-MemTierJson $p.Value) }
        return '{' + (@($parts) -join ',') + '}'
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        $parts = foreach ($item in $Value) { ConvertTo-MemTierJson $item }
        return '[' + (@($parts) -join ',') + ']'
    }
    return ConvertTo-MemTierJsonNumber $Value
}

# ---------------------------------------------------------------------------
# vCenter access (shared by Invoke-MemTierCollector.ps1 and Get-MemTierSnapshot.ps1)
# Requires PowerCLI to be loaded - see Import-MemTierPowerCLI.
# ---------------------------------------------------------------------------
$script:HostCounters = @('mem.active.average', 'mem.consumed.average', 'mem.vmmemctl.average', 'mem.swapused.average')
$script:VmCounters = @('mem.active.average', 'mem.consumed.average', 'mem.vmmemctl.average', 'mem.swapped.average')

function Import-MemTierPowerCLI {
    if (-not (Get-Command Connect-VIServer -ErrorAction SilentlyContinue)) {
        Import-Module VMware.VimAutomation.Core -ErrorAction Stop
    }
}

function Get-MemTierOrdinalSorted {
    # Ordinal sort of a list by a string key (culture-independent, same order as Python)
    param([System.Collections.Generic.List[object]]$List, [scriptblock]$Key)
    $keys = New-Object 'System.Collections.Generic.List[string]'
    foreach ($item in $List) { $keys.Add([string](& $Key $item)) }
    $k = $keys.ToArray(); $v = $List.ToArray()
    # explicit [Array]/IComparer: otherwise PowerShell may sort converted copies and leave $k/$v untouched
    [Array]::Sort([Array]$k, [Array]$v, [System.Collections.IComparer][StringComparer]::Ordinal)
    $v
}

function Connect-MemTierVCenter {
    <#
        Re-uses an existing connected session only when no credential is given.
        New connections use -NotDefault, so the caller's default server is never changed.
        Returns @{ VI; Owns } - pass it to Disconnect-MemTierVCenter.
    #>
    param([string]$Server, $Credential)
    $existing = @($global:DefaultVIServers | Where-Object { $_ -and $_.Name -eq $Server -and $_.IsConnected })
    if ($existing.Count -gt 0 -and -not $Credential) { return @{ VI = $existing[0]; Owns = $false } }
    $connect = @{ Server = $Server; NotDefault = $true; ErrorAction = 'Stop' }
    if ($Credential) { $connect.Credential = $Credential }
    @{ VI = (Connect-VIServer @connect); Owns = $true }
}

function Disconnect-MemTierVCenter {
    param($Connection, [string]$Server)
    if ($Connection -and $Connection.Owns -and $Connection.VI) {
        # a failing disconnect must never lose collected data
        try { Disconnect-VIServer -Server $Connection.VI -Confirm:$false -Force -ErrorAction Stop }
        catch { Write-MemTierLog "${Server}: disconnect failed: $($_.Exception.Message)" 'WARN' }
    }
}

function Get-MemTierInventory {
    <#
        Reads clusters, hosts (incl. DRAM/NVMe tier sizes) and VMs with the PropertyCollector.
        Returns @{
          HostMeta = @{ moref -> @{ MoRef; Name; Cluster; State; Maint; Phys; Dram; Nvme; Tiering; VmsOn; Assigned } }
          Vms      = List of @{ Id; MoRef; Name; Power; Assigned; Reservation; Latency; Host; Cluster; Excluded; Collect }
                     (all VMs except templates; Collect = powered on, connected, not excluded)
          Counts   = @{ Total; On; Off; Suspended; Templates; Excluded; Unreachable }
        }
    #>
    param($VI, [string]$Server, [string]$ExcludeVmPattern)

    $clusterNames = @{}
    foreach ($c in @(Get-View -Server $VI -ViewType ClusterComputeResource -Property Name)) {
        if ($c) { $clusterNames[$c.MoRef.Value] = $c.Name }
    }
    $hostViews = @(Get-View -Server $VI -ViewType HostSystem -Property Name, Parent, Hardware.MemorySize, Runtime.ConnectionState, Runtime.InMaintenanceMode | Where-Object { $_ })
    $tierViews = @{}
    try {
        foreach ($t in @(Get-View -Server $VI -ViewType HostSystem -Property Hardware.MemoryTieringType, Hardware.MemoryTierInfo -ErrorAction Stop)) {
            if ($t) { $tierViews[$t.MoRef.Value] = $t }
        }
    }
    catch {
        Write-MemTierLog "memory tier properties not available on $Server ($($_.Exception.Message)) - using Hardware.MemorySize as DRAM"
    }
    $vmViews = @(Get-View -Server $VI -ViewType VirtualMachine -Property Name, Runtime.PowerState, Runtime.Host, Runtime.ConnectionState, Config.Template, Config.Hardware.MemoryMB, Config.MemoryAllocation.Reservation, Config.LatencySensitivity.Level | Where-Object { $_ })

    $hostMeta = @{}
    foreach ($h in $hostViews) {
        $mid = $h.MoRef.Value
        $cluster = if ($h.Parent -and $clusterNames.ContainsKey($h.Parent.Value)) { $clusterNames[$h.Parent.Value] } else { '(standalone)' }
        $physMB = Get-RoundHalfUp ([double]$h.Hardware.MemorySize / 1048576.0)
        $dramMB = $physMB; $nvmeMB = 0L; $tiering = ''
        if ($tierViews.ContainsKey($mid)) {
            $tv = $tierViews[$mid]
            if ($tv.Hardware.MemoryTieringType) { $tiering = [string]$tv.Hardware.MemoryTieringType }
            $tiers = @($tv.Hardware.MemoryTierInfo | Where-Object { $_ })
            if ($tiers.Count -gt 0) {
                $dram = 0.0; $nvme = 0.0
                foreach ($tier in $tiers) {
                    if ([string]$tier.Type -eq 'DRAM') { $dram += [double]$tier.Size } else { $nvme += [double]$tier.Size }
                }
                if ($dram -gt 0) { $dramMB = Get-RoundHalfUp ($dram / 1048576.0) }
                $nvmeMB = Get-RoundHalfUp ($nvme / 1048576.0)
            }
        }
        $hostMeta[$mid] = @{
            MoRef = $h.MoRef; Name = $h.Name; Cluster = $cluster; State = [string]$h.Runtime.ConnectionState
            Maint = [bool]$h.Runtime.InMaintenanceMode; Phys = $physMB; Dram = $dramMB; Nvme = $nvmeMB; Tiering = $tiering
            VmsOn = 0L; Assigned = 0L
        }
    }

    $exclude = if ($ExcludeVmPattern) { New-Object System.Text.RegularExpressions.Regex($ExcludeVmPattern) } else { $null }
    $counts = @{ Total = 0; On = 0; Off = 0; Suspended = 0; Templates = 0; Excluded = 0; Unreachable = 0 }
    $vms = New-Object 'System.Collections.Generic.List[object]'
    foreach ($vm in $vmViews) {
        if ($vm.Config -and $vm.Config.Template) { $counts.Templates++; continue }
        $counts.Total++
        $name = [uri]::UnescapeDataString([string]$vm.Name)   # the API escapes / \ % in names
        $power = [string]$vm.Runtime.PowerState
        switch ($power) {
            'poweredOn' { $counts.On++ }
            'suspended' { $counts.Suspended++ }
            default { $counts.Off++ }
        }
        $hostId = if ($vm.Runtime.Host) { $vm.Runtime.Host.Value } else { $null }
        $hm = if ($hostId -and $hostMeta.ContainsKey($hostId)) { $hostMeta[$hostId] } else { $null }
        $assigned = if ($vm.Config -and $vm.Config.Hardware) { [long]$vm.Config.Hardware.MemoryMB } else { 0L }
        $reservation = $null
        $latency = ''
        if ($vm.Config) {
            if ($vm.Config.MemoryAllocation) { $reservation = $vm.Config.MemoryAllocation.Reservation }
            if ($vm.Config.LatencySensitivity) { $latency = [string]$vm.Config.LatencySensitivity.Level }
        }
        $conn = [string]$vm.Runtime.ConnectionState
        $excluded = [bool]($exclude -and $exclude.IsMatch($name))
        $collect = $power -eq 'poweredOn' -and (-not $conn -or $conn -eq 'connected') -and -not $excluded
        if ($power -eq 'poweredOn' -and (-not $conn -or $conn -eq 'connected') -and $excluded) { $counts.Excluded++ }
        if ($power -eq 'poweredOn' -and $conn -and $conn -ne 'connected') { $counts.Unreachable++ }   # e.g. VM on a disconnected host
        if ($collect -and $hm) { $hm.VmsOn++; $hm.Assigned += $assigned }
        $vms.Add(@{
            Id = $vm.MoRef.Value; MoRef = $vm.MoRef; Name = $name; Power = $power; Connection = $conn
            Assigned = $assigned; Reservation = $reservation; Latency = $latency; Excluded = $excluded; Collect = $collect
            Host = if ($hm) { $hm.Name } elseif ($hostId) { '(unknown)' } else { '(none)' }
            Cluster = if ($hm) { $hm.Cluster } elseif ($hostId) { '(unknown)' } else { '(none)' }
        })
    }
    @{ HostMeta = $hostMeta; Vms = $vms; Counts = $counts }
}

function Get-MemTierCounterIndex {
    param($PerfManager)
    $index = @{}
    foreach ($c in $PerfManager.PerfCounter) {
        $index['{0}.{1}.{2}' -f $c.GroupInfo.Key, $c.NameInfo.Key, $c.RollupType] = [int]$c.Key
    }
    $index
}

function Get-MemTierCounterIds {
    param($PerfManager, [string[]]$Names, $Index)
    if (-not $Index) { $Index = Get-MemTierCounterIndex $PerfManager }
    $ids = [ordered]@{}
    foreach ($n in $Names) {
        if (-not $Index.Contains($n)) { throw "performance counter $n not found" }
        $ids[$n] = $Index[$n]
    }
    $ids
}

function Get-MemTierTierCounters {
    <#
        Memory-tiering counters this vCenter offers.

        How much memory a host currently keeps on its NVMe tier is not part of the inventory:
        Hardware.MemoryTierInfo only gives the tier sizes. vSphere 8.0 U3 and later publish
        per-tier performance counters, but their names have moved between releases, so they are
        discovered rather than assumed.
    #>
    param($Index)
    @($Index.Keys | Where-Object { $_ -like 'mem.*' -and $_ -match 'tier' } | Sort-Object)
}

function Invoke-MemTierPerfQuery {
    <# Queries real-time stats for a batch; splits the batch on failure and skips entities that fail alone. #>
    param($PerfManager, [object[]]$Entities, $CounterIds, [datetime]$StartUtc, [hashtable]$Results, [System.Collections.Generic.List[string]]$Failed)
    if ($Entities.Count -eq 0) { return }
    $metricIds = New-Object 'System.Collections.Generic.List[VMware.Vim.PerfMetricId]'
    foreach ($id in $CounterIds.Values) {
        $m = New-Object VMware.Vim.PerfMetricId
        $m.CounterId = $id
        $m.Instance = ''
        $metricIds.Add($m)
    }
    $specs = New-Object 'System.Collections.Generic.List[VMware.Vim.PerfQuerySpec]'
    foreach ($e in $Entities) {
        $s = New-Object VMware.Vim.PerfQuerySpec
        $s.Entity = $e
        $s.StartTime = $StartUtc
        $s.IntervalId = 20
        $s.Format = 'normal'
        $s.MetricId = $metricIds.ToArray()
        $specs.Add($s)
    }
    try {
        $answer = $PerfManager.QueryPerf($specs.ToArray())
    }
    catch {
        if ($Entities.Count -eq 1) {
            Write-MemTierLog ("no performance data for {0} {1}: {2}" -f $Entities[0].Type, $Entities[0].Value, $_.Exception.Message) 'WARN'
            $Failed.Add($Entities[0].Value)
            return
        }
        Write-MemTierLog ("perf batch of {0} failed ({1}) - splitting" -f $Entities.Count, $_.Exception.Message) 'DEBUG'
        $half = [int][math]::Floor($Entities.Count / 2)
        Invoke-MemTierPerfQuery $PerfManager $Entities[0..($half - 1)] $CounterIds $StartUtc $Results $Failed
        Invoke-MemTierPerfQuery $PerfManager $Entities[$half..($Entities.Count - 1)] $CounterIds $StartUtc $Results $Failed
        return
    }
    $byId = @{}
    foreach ($k in $CounterIds.Keys) { $byId[[int]$CounterIds[$k]] = $k }
    foreach ($em in @($answer)) {
        if ($null -eq $em) { continue }
        $eid = $em.Entity.Value
        if (-not $Results.ContainsKey($eid)) { $Results[$eid] = @{} }
        foreach ($series in @($em.Value)) {
            if ($null -eq $series -or $series.Id.Instance) { continue }
            $name = $byId[[int]$series.Id.CounterId]
            if (-not $name) { continue }
            if (-not $Results[$eid].ContainsKey($name)) { $Results[$eid][$name] = New-Object 'System.Collections.Generic.List[long]' }
            if ($series.Value) { $Results[$eid][$name].AddRange([long[]]$series.Value) }
        }
    }
}

function Get-MemTierStatistics {
    <#
        Pulls real-time samples since StartUtc for all connected hosts and all collectable VMs
        and reduces them with Get-MemTierSummary.
        Returns @{
          Hosts  = @{ moref -> @{ Active; Consumed; Balloon; Swap } }   (summaries: Avg/P95/Max/Samples in MB)
          Vms    = @{ moref -> @{ Active; Consumed; Balloon; Swap } }
          HostsFailed; VmsFailed; LiveHosts
        }
    #>
    param($VI, $Inventory, [datetime]$StartUtc, [int]$BatchSize = 50)
    $si = Get-View -Server $VI ServiceInstance
    $perfManager = Get-View -Server $VI -Id $si.Content.PerfManager
    $counterIndex = Get-MemTierCounterIndex $perfManager
    $hostCounterIds = Get-MemTierCounterIds $perfManager $script:HostCounters $counterIndex
    $vmCounterIds = Get-MemTierCounterIds $perfManager $script:VmCounters $counterIndex

    $liveHosts = @($Inventory.HostMeta.Values | Where-Object { $_.State -eq 'connected' } | ForEach-Object { $_.MoRef })
    $vmEntities = @($Inventory.Vms | Where-Object { $_.Collect } | ForEach-Object { $_.MoRef })

    $out = @{ Hosts = @{}; Vms = @{}; LiveHosts = $liveHosts.Count
        TierCounters = Get-MemTierTierCounters $counterIndex }
    foreach ($pass in @(
            @{ Kind = 'Hosts'; Entities = $liveHosts; Ids = $hostCounterIds; Swap = 'mem.swapused.average' },
            @{ Kind = 'Vms'; Entities = $vmEntities; Ids = $vmCounterIds; Swap = 'mem.swapped.average' })) {
        $raw = @{}
        $failed = New-Object 'System.Collections.Generic.List[string]'
        $entities = $pass.Entities
        for ($i = 0; $i -lt $entities.Count; $i += $BatchSize) {
            $end = [math]::Min($i + $BatchSize, $entities.Count) - 1
            Invoke-MemTierPerfQuery $perfManager @($entities[$i..$end]) $pass.Ids $StartUtc $raw $failed
        }
        foreach ($eid in $raw.Keys) {
            $series = $raw[$eid]
            $out[$pass.Kind][$eid] = @{
                Active = Get-MemTierSummary $series['mem.active.average']
                Consumed = Get-MemTierSummary $series['mem.consumed.average']
                Balloon = Get-MemTierSummary $series['mem.vmmemctl.average']
                Swap = Get-MemTierSummary $series[$pass.Swap]
            }
        }
        $out[$pass.Kind + 'Failed'] = $failed.Count
    }
    $out
}

function Get-MemTierEmptyStats {
    $empty = @{ Avg = $null; P95 = $null; Max = $null; Samples = 0 }
    @{ Active = $empty; Consumed = $empty; Balloon = $empty; Swap = $empty }
}
