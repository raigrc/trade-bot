# Runs ONE bot cycle (process the latest closed candle, then exit).
# Designed to be called on a schedule (e.g. every hour by Task Scheduler).
# State lives in SQLite, so each run resumes exactly where the last left off;
# the dedupe means extra runs with no new closed bar are harmless no-ops.
#
# IMPORTANT — must stay robust under WINDOWS POWERSHELL 5.1 (the scheduler's
# interpreter), not just PowerShell 7. Two 5.1-specific hazards are handled here:
#
#   1. Python logs INFO to *stderr*. Under PS 5.1 with $ErrorActionPreference =
#      "Stop", native-command stderr captured via `*>>` is promoted to a
#      TERMINATING NativeCommandError, which aborts the script BEFORE the
#      `tick exit` line is written — so the cycle never completes and SQLite
#      state freezes. We therefore relax error handling around the native call
#      and capture stderr explicitly instead of letting `*>>` redirect it.
#
#   2. `*>>` (and `Out-File -Encoding utf8`) under PS 5.1 write UTF-16 / a
#      UTF-8 BOM, corrupting the log (grep sees binary). We write BOM-free
#      UTF-8 ourselves via .NET so the log is clean on both 5.1 and 7.

# Strict only for the setup cmdlets below; explicitly relaxed before the
# python call so its stderr can never terminate the script.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot          # project root (parent of scripts/)
Set-Location $root
$env:PYTHONIOENCODING = "utf-8"
New-Item -ItemType Directory -Force -Path "logs" | Out-Null

$log       = Join-Path $root "logs\paper.log"
$prev      = Join-Path $root "logs\paper.prev.log"
$heartbeat = Join-Path $root "logs\last_success.txt"

# rotate the log if it has grown past ~5 MB (keep one previous archive)
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) {
    Move-Item $log $prev -Force
}

# BOM-free UTF-8 appender — sidesteps PS 5.1's UTF-16/BOM Out-File behavior.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
function Write-Log([string]$text) {
    [System.IO.File]::AppendAllText($log, ($text + "`r`n"), $utf8NoBom)
}

$py = "C:\Users\DELL\AppData\Local\Programs\Python\Python313\python.exe"
$ts = (Get-Date).ToString("s")
Write-Log "[$ts] tick start"

# Run the cycle. Relax error handling so python's stderr (its normal INFO
# logging) cannot raise a terminating NativeCommandError under PS 5.1. Merge
# stderr into stdout (2>&1) and capture as text so we both log it and keep the
# native process's real exit code in $LASTEXITCODE.
$ErrorActionPreference = "Continue"
$output = & $py -m tradebot.main --once 2>&1 | ForEach-Object { $_.ToString() }
$code   = $LASTEXITCODE   # read IMMEDIATELY — the python process's code, not a cmdlet's

if ($output) {
    Write-Log ($output -join "`r`n")
}

Write-Log "[$ts] tick exit=$code"

if ($code -eq 0) {
    # Heartbeat for staleness detection: operators / admin can check this file's
    # age to notice a future stall. Cheap, append-free, last-success only.
    [System.IO.File]::WriteAllText($heartbeat, ((Get-Date).ToString("o") + "`r`n"), $utf8NoBom)
} else {
    # Grep-able failure marker. The bot's own Telegram alerting (tradebot/notify.py)
    # still fires from inside python; this is just a local breadcrumb for the operator.
    Write-Log "[$ts] TICK-FAIL exit=$code -- see output above"
}

exit $code
