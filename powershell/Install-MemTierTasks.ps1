<#
.SYNOPSIS
    VMware NVMe Memory Tiering: one-time setup of the Windows scheduled tasks.
.DESCRIPTION
    Run this script AS THE ACCOUNT THAT WILL RUN THE TASKS (for example with
    "runas /user:DOMAIN\svc-memtier powershell.exe"), from an elevated prompt:

      1. sets the PowerCLI user configuration so unattended runs never prompt
      2. saves the vCenter credential with Export-Clixml (DPAPI - only this
         Windows account on this machine can decrypt it)
      3. tests the connection to every vCenter
      4. registers the scheduled task "MemTier Collector" (every hour at :05);
         every run collects and then rebuilds <BaseDir>\reports\MemTier_Report.html

    The task runs "whether the user is logged on or not", which needs the
    Windows password of the account once (it is stored by Task Scheduler, not
    by this script) and the "Log on as a batch job" right.
.PARAMETER BaseDir
    Folder for data, reports, logs and the credential file.
.PARAMETER StretchedCluster
    Passed to the report: all clusters are stretched (site failover = 50% capacity).
.PARAMETER StretchedClusterName
    Passed to the report: names of the stretched clusters.
.PARAMETER InvalidCertificateAction
    Fail (default, recommended with trusted vCenter certificates) or Ignore.
.EXAMPLE
    .\Install-MemTierTasks.ps1 -VCenterServer vcenter01.example.com -BaseDir D:\MemTier
#>
[CmdletBinding()]
param (
    [Parameter(Mandatory = $true)][string[]]$VCenterServer,
    [Parameter(Mandatory = $true)][string]$BaseDir,
    [ValidateSet('Fail', 'Warn', 'Ignore')][string]$InvalidCertificateAction = 'Fail',
    [int]$Days = 30,
    [switch]$CompressOldMonths,
    [switch]$StretchedCluster,
    [Alias('StretchedClusters')]
    [string[]]$StretchedClusterName = @(),
    [switch]$SkipConnectionTest
)

$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -eq 'Core' -and -not $IsWindows) { throw 'This installer is for Windows. On Linux use python/memtier.py setup (cron).' }

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Registering tasks that run without a logged-on user needs an elevated PowerShell.'
}

$BaseDir = [System.IO.Path]::GetFullPath($BaseDir)
foreach ($d in 'data', 'reports', 'logs') { New-Item -ItemType Directory -Path (Join-Path $BaseDir $d) -Force | Out-Null }
$credFile = Join-Path $BaseDir 'vc-cred.xml'
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name

# 1. PowerCLI configuration for this user
if (-not (Get-Command Connect-VIServer -ErrorAction SilentlyContinue)) { Import-Module VMware.VimAutomation.Core }
Set-PowerCLIConfiguration -Scope User -ParticipateInCEIP $false -InvalidCertificateAction $InvalidCertificateAction -DisplayDeprecationWarnings $false -Confirm:$false | Out-Null
Write-Host "[+] PowerCLI configured for $user (CEIP off, certificates: $InvalidCertificateAction)" -ForegroundColor Green

# 2. vCenter credential
if (Test-Path -LiteralPath $credFile) {
    Write-Host "[*] Using existing credential file $credFile" -ForegroundColor Yellow
    $cred = Import-Clixml -LiteralPath $credFile
}
else {
    $cred = Get-Credential -Message 'vCenter read-only account (e.g. svc-memtier@vsphere.local)'
    $cred | Export-Clixml -LiteralPath $credFile
    Write-Host "[+] Credential saved to $credFile (DPAPI, $user only)" -ForegroundColor Green
}

# 3. connection test
if (-not $SkipConnectionTest) {
    foreach ($server in $VCenterServer) {
        $vi = Connect-VIServer -Server $server -Credential $cred -NotDefault -ErrorAction Stop
        Write-Host "[+] Connected to $server ($($vi.Version) build $($vi.Build))" -ForegroundColor Green
        Disconnect-VIServer -Server $vi -Confirm:$false
    }
}

# 4. scheduled tasks
$exe = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
if (-not $exe) { $exe = Join-Path $PSHOME 'powershell.exe' }
$here = $PSScriptRoot
# -File passes plain strings: the collector splits comma-separated server lists itself
$servers = $VCenterServer -join ','

$collectArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$here\Invoke-MemTierCollector.ps1`" " +
    "-VCenterServer $servers -CredentialFile `"$credFile`" -DataDir `"$BaseDir\data`" -LogDir `"$BaseDir\logs`"" +
    " -ReportDir `"$BaseDir\reports`" -Days $Days" +
    $(if ($CompressOldMonths) { ' -CompressOldMonths' } else { '' }) +
    $(if ($StretchedCluster) { ' -StretchedCluster' } else { '' }) +
    $(if ($StretchedClusterName.Count) { " -StretchedClusterName `"$($StretchedClusterName -join ',')`"" } else { '' })

$winCred = Get-Credential -UserName $user -Message "Windows password of $user (stored by Task Scheduler to run the task unattended)"
$password = $winCred.GetNetworkCredential().Password

# Parallel + 2 h: a long report must not make Task Scheduler skip the next hourly collection.
# Overlapping collections are still prevented by the collector's own lock.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances Parallel -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$start = (Get-Date).Date.AddHours((Get-Date).Hour + 1).AddMinutes(5)
$collectTrigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1)
$collectAction = New-ScheduledTaskAction -Execute $exe -Argument $collectArgs -WorkingDirectory $here
Register-ScheduledTask -TaskName 'MemTier Collector' -Description 'Hourly VMware memory tiering statistics and report (real-time active/consumed memory)' `
    -Action $collectAction -Trigger $collectTrigger -Settings $settings -User $user -Password $password -RunLevel Limited -Force | Out-Null
$password = $null

# Earlier versions registered a separate daily report task; the collector now rebuilds the report itself
if (Get-ScheduledTask -TaskName 'MemTier Report' -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName 'MemTier Report' -Confirm:$false
    Write-Host "[+] Removed the old 'MemTier Report' task" -ForegroundColor Green
}

Write-Host "[+] Registered 'MemTier Collector' (hourly from $($start.ToString('HH:mm')), rebuilds $BaseDir\reports\MemTier_Report.html)" -ForegroundColor Green
Write-Host "    Collector: $exe $collectArgs"
Write-Host "    Test now:  Start-ScheduledTask -TaskName 'MemTier Collector'; then check $BaseDir\logs" -ForegroundColor Cyan
