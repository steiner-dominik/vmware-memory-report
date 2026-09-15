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
      4. registers two scheduled tasks:
           "MemTier Collector"  every hour at :05
           "MemTier Report"     daily at -ReportTime

    The tasks run "whether the user is logged on or not", which needs the
    Windows password of the account once (it is stored by Task Scheduler, not
    by this script) and the "Log on as a batch job" right.
.PARAMETER BaseDir
    Folder for data, reports, logs and the credential file.
.PARAMETER StretchedCluster
    Passed to the report task: all clusters are stretched (site failover = 50% capacity).
.PARAMETER StretchedClusterName
    Passed to the report task: names of the stretched clusters.
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
    [string]$ReportTime = '06:30',
    [int]$Days = 30,
    [switch]$CompressOldMonths,
    [switch]$StretchedCluster,
    [string[]]$StretchedClusterName = @(),
    [switch]$SkipConnectionTest
)

$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -eq 'Core' -and -not $IsWindows) { throw 'This installer is for Windows. On Linux/vCenter use python/memtier.py setup (cron).' }

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
    $(if ($CompressOldMonths) { ' -CompressOldMonths' } else { '' })
$reportArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$here\New-MemTierReport.ps1`" " +
    "-DataDir `"$BaseDir\data`" -ReportDir `"$BaseDir\reports`" -LogDir `"$BaseDir\logs`" -Days $Days" +
    $(if ($StretchedCluster) { ' -StretchedCluster' } else { '' }) +
    $(if ($StretchedClusterName.Count) { " -StretchedClusterName `"$($StretchedClusterName -join ',')`"" } else { '' })

$winCred = Get-Credential -UserName $user -Message "Windows password of $user (stored by Task Scheduler to run the tasks unattended)"
$password = $winCred.GetNetworkCredential().Password

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 55)
$start = (Get-Date).Date.AddHours((Get-Date).Hour + 1).AddMinutes(5)
$collectTrigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1)
$collectAction = New-ScheduledTaskAction -Execute $exe -Argument $collectArgs -WorkingDirectory $here
Register-ScheduledTask -TaskName 'MemTier Collector' -Description 'Hourly VMware memory tiering statistics (real-time active/consumed memory)' `
    -Action $collectAction -Trigger $collectTrigger -Settings $settings -User $user -Password $password -RunLevel Limited -Force | Out-Null

$reportSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$reportTrigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($ReportTime, 'HH:mm', $null))
$reportAction = New-ScheduledTaskAction -Execute $exe -Argument $reportArgs -WorkingDirectory $here
Register-ScheduledTask -TaskName 'MemTier Report' -Description 'Daily VMware memory tiering trend report' `
    -Action $reportAction -Trigger $reportTrigger -Settings $reportSettings -User $user -Password $password -RunLevel Limited -Force | Out-Null
$password = $null

Write-Host "[+] Registered 'MemTier Collector' (hourly from $($start.ToString('HH:mm'))) and 'MemTier Report' (daily $ReportTime)" -ForegroundColor Green
Write-Host "    Collector: $exe $collectArgs"
Write-Host "    Report:    $exe $reportArgs"
Write-Host "    Test now:  Start-ScheduledTask -TaskName 'MemTier Collector'; then check $BaseDir\logs" -ForegroundColor Cyan
