# Runs ONE bot cycle (process the latest closed candle, then exit).
# Designed to be called on a schedule (e.g. every hour by Task Scheduler).
# State lives in SQLite, so each run resumes exactly where the last left off;
# the dedupe means extra runs with no new closed bar are harmless no-ops.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot          # project root (parent of scripts/)
Set-Location $root
$env:PYTHONIOENCODING = "utf-8"
New-Item -ItemType Directory -Force -Path "logs" | Out-Null

# rotate the log if it has grown past ~5 MB (keep one previous archive)
$log = "logs\paper.log"
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) {
    Move-Item $log "logs\paper.prev.log" -Force
}

$py = "C:\Users\DELL\AppData\Local\Programs\Python\Python313\python.exe"
$ts = (Get-Date).ToString("s")
"[$ts] tick start" | Out-File -FilePath $log -Append -Encoding utf8
& $py -m tradebot.main --once *>> $log
"[$ts] tick exit=$LASTEXITCODE" | Out-File -FilePath $log -Append -Encoding utf8
