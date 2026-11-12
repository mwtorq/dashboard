# Stop every local cost-dashboard Python process, pull latest main, free the
# listen port, then start again.
# Default: cursor_dashboard.py on 127.0.0.1:8787
#
# Usage:
#   .\restart_dashboard.ps1
#   .\restart_dashboard.ps1 -Copilot
#   .\restart_dashboard.ps1 -Port 8790
#   .\restart_dashboard.ps1 -- --db "C:\path\to\state.vscdb"

[CmdletBinding()]
param(
    [switch]$Copilot,
    [int]$Port = 8787,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Passthrough
)

$ErrorActionPreference = 'Stop'
$ScriptDir = $PSScriptRoot
$ScriptName = if ($Copilot) { 'github_copilot_dashboard.py' } else { 'cursor_dashboard.py' }
$ScriptPath = Join-Path $ScriptDir $ScriptName

if (-not (Test-Path -LiteralPath $ScriptPath)) {
    Write-Error "Dashboard script not found: $ScriptPath"
}

$DashboardPattern = '(?i)(?:cursor_dashboard|github_copilot_dashboard)\.py'

function Get-DashboardProcesses {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'py.exe'" |
        Where-Object { $_.CommandLine -and ($_.CommandLine -match $DashboardPattern) }
}

function Stop-DashboardInstances {
    $procs = @(Get-DashboardProcesses)
    if ($procs.Count -eq 0) {
        Write-Host 'No running dashboard processes found.'
        return
    }

    Write-Host "Stopping $($procs.Count) dashboard process(es)..."
    foreach ($proc in $procs) {
        Write-Host ("  PID {0} ({1})" -f $proc.ProcessId, $proc.Name)
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }

    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
        if (@(Get-DashboardProcesses).Count -eq 0) { break }
    }

    $remaining = @(Get-DashboardProcesses)
    if ($remaining.Count -gt 0) {
        $ids = ($remaining | ForEach-Object { $_.ProcessId }) -join ', '
        Write-Error "Failed to stop dashboard process(es): $ids"
    }
}

function Clear-ListenPort {
    param([int]$ListenPort)

    $listeners = @(
        Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue
    )
    if ($listeners.Count -eq 0) { return }

    $ownerIds = $listeners |
        Select-Object -ExpandProperty OwningProcess -Unique |
        Where-Object { $_ -and $_ -gt 0 }

    foreach ($ownerPid in $ownerIds) {
        $p = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
        if (-not $p) { continue }
        Write-Host ("Freeing port {0}: stopping PID {1} ({2})" -f $ListenPort, $ownerPid, $p.ProcessName)
        Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
    }

    $deadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 200
        $still = @(Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue)
        if ($still.Count -eq 0) { return }
    }

    Write-Error "Port $ListenPort is still in use after stop attempts."
}

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$GitArgs
    )
    # git writes progress to stderr; with $ErrorActionPreference Stop that becomes
    # a terminating RemoteException even when the command succeeds.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & git @GitArgs 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) {
                Write-Host $_.Exception.Message
            } else {
                Write-Host $_
            }
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Error ("git {0} failed (exit {1})." -f ($GitArgs -join ' '), $LASTEXITCODE)
        }
    }
    finally {
        $ErrorActionPreference = $prev
    }
}

function Update-FromMain {
    Push-Location -LiteralPath $ScriptDir
    try {
        if (-not (Test-Path -LiteralPath (Join-Path $ScriptDir '.git'))) {
            Write-Error "Not a git repo: $ScriptDir"
        }

        Write-Host 'Updating from origin/main...'
        Invoke-Git -GitArgs @('fetch', 'origin', 'main')

        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $branch = (& git branch --show-current 2>$null | Out-String).Trim()
        }
        finally {
            $ErrorActionPreference = $prev
        }
        if ($branch -ne 'main') {
            Write-Host "  Checking out main (was on '$branch')..."
            Invoke-Git -GitArgs @('checkout', 'main')
        }

        Invoke-Git -GitArgs @('pull', '--ff-only', 'origin', 'main')

        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $head = (& git rev-parse --short HEAD 2>$null | Out-String).Trim()
            $subject = (& git log -1 --pretty=format:%s 2>$null | Out-String).Trim()
        }
        finally {
            $ErrorActionPreference = $prev
        }
        Write-Host "  Now at $head - $subject" -ForegroundColor Green
    }
    finally {
        Pop-Location
    }
}

Write-Host ''
Write-Host '=== Restart cost dashboard ===' -ForegroundColor Cyan
Write-Host "Script : $ScriptName"
Write-Host "Port   : $Port"
Write-Host ''

Stop-DashboardInstances
Clear-ListenPort -ListenPort $Port
Update-FromMain

$env:PYTHONIOENCODING = 'utf-8'
# Use the filename + WorkingDirectory so paths with spaces (e.g. OneDrive) stay intact.
$pyArgs = @('-3', $ScriptName, '--port', "$Port", '--no-open')
if ($Passthrough) { $pyArgs += $Passthrough }

Write-Host "Starting: py $($pyArgs -join ' ')  (cwd: $ScriptDir)"
Write-Host "URL     : http://127.0.0.1:$Port/"
Write-Host ''

# Detached so this script can exit while the dashboard keeps serving.
$started = Start-Process -FilePath 'py' -ArgumentList $pyArgs -WorkingDirectory $ScriptDir -WindowStyle Hidden -PassThru

$ready = $false
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    if ($started.HasExited) {
        Write-Error "Dashboard launcher exited early (exit code $($started.ExitCode))."
    }
    $listening = @(
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    )
    if ($listening.Count -gt 0) {
        $ready = $true
        break
    }
}

if (-not $ready) {
    Write-Warning "Dashboard process started, but port $Port is not listening yet. Check Task Manager / py output."
    exit 1
}

Write-Host "Dashboard is listening on http://127.0.0.1:$Port/" -ForegroundColor Green
