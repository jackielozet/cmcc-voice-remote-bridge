# audio_channel_probe.ps1 -- find out which HID report channel carries the voice stream
# Pure ASCII on purpose: Windows PowerShell 5.1 parses no-BOM files as GBK.
#
# Usage (close CMCC_RC_Keys first -- it holds the COM port):
#   powershell -ExecutionPolicy Bypass -File .\audio_channel_probe.ps1 -Port COM3 -Secs 25
#
# It turns on the firmware RAW mode, records every notification, then prints a
# per-report-id histogram plus the first few raw lines of each channel.

param(
    [string]$Port = "COM3",
    [int]$Secs = 25
)

$ErrorActionPreference = "Continue"

try {
    $sp = New-Object System.IO.Ports.SerialPort($Port, 115200, [System.IO.Ports.Parity]::None, 8, [System.IO.Ports.StopBits]::One)
    $sp.ReadTimeout = 200
    $sp.NewLine = "`n"
    $sp.Open()
} catch {
    Write-Host "FAIL: cannot open $Port"
    Write-Host "  $($_.Exception.Message)"
    Write-Host "  Close CMCC_RC_Keys first (only one program can hold the port)."
    exit 1
}

Write-Host "OPEN $Port @ 115200"

function Send-Cmd([string]$c) {
    $sp.Write($c + "`r`n")
    Start-Sleep -Milliseconds 250
}

Start-Sleep -Milliseconds 300
$sp.DiscardInBuffer()

Send-Cmd "RAW 1"
Start-Sleep -Milliseconds 400

Write-Host ""
Write-Host "=== RECORDING $Secs s ==="
Write-Host "=== press and HOLD the voice key now, say a few words, release ==="
Write-Host ""

$sb = New-Object System.Text.StringBuilder
$pending = ""
$stats = @{}
$first = @{}
$deadline = (Get-Date).AddSeconds($Secs)

while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 60
    $chunk = $sp.ReadExisting()
    if ([string]::IsNullOrEmpty($chunk)) { continue }
    [void]$sb.Append($chunk)
    $pending = $pending + $chunk
    while ($true) {
        $idx = $pending.IndexOf("`n")
        if ($idx -lt 0) { break }
        $line = $pending.Substring(0, $idx).Trim()
        $pending = $pending.Substring($idx + 1)
        if ($line.Length -eq 0) { continue }
        if ($line -match 'id=0x([0-9A-Fa-f]{2})') {
            $id = $Matches[1].ToUpper()
            if ($stats.ContainsKey($id)) {
                $stats[$id] = $stats[$id] + 1
            } else {
                $stats[$id] = 1
                $first[$id] = New-Object System.Collections.ArrayList
            }
            if ($first[$id].Count -lt 4) { [void]$first[$id].Add($line) }
        }
    }
}

Send-Cmd "RAW 0"
Start-Sleep -Milliseconds 300
Send-Cmd "STAT"
Start-Sleep -Milliseconds 600
$tail = $sp.ReadExisting()
if (-not [string]::IsNullOrEmpty($tail)) { [void]$sb.Append($tail) }

$sp.Close()

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $dir ("probe_" + $stamp + ".log")
[System.IO.File]::WriteAllText($log, $sb.ToString(), (New-Object System.Text.UTF8Encoding($false)))

Write-Host ""
Write-Host "=== CHANNEL HISTOGRAM (frames received during the window) ==="
$keys = @($stats.Keys)
if ($keys.Count -eq 0) {
    Write-Host "  (nothing received at all -- check that the remote was connected)"
} else {
    foreach ($k in ($keys | Sort-Object)) {
        Write-Host ("  id=0x{0}  count={1}" -f $k, $stats[$k])
    }
    Write-Host ""
    Write-Host "=== FIRST LINES OF EACH CHANNEL ==="
    foreach ($k in ($keys | Sort-Object)) {
        Write-Host ("--- 0x{0} ---" -f $k)
        foreach ($l in $first[$k]) { Write-Host ("  " + $l) }
    }
}

Write-Host ""
Write-Host "=== STAT line (tail of log) ==="
$statLine = ($sb.ToString() -split "`n") | Where-Object { $_ -match 'STAT ' } | Select-Object -Last 1
if ($statLine) { Write-Host ("  " + $statLine.Trim()) }

Write-Host ""
Write-Host "LOG SAVED: $log"
Write-Host ""
Write-Host "How to read it:"
Write-Host "  id=0xFC hundreds of frames  -> voice still on 0xFC, problem is downstream"
Write-Host "  id=0xFB hundreds of frames  -> voice moved to 0xFB, firmware must handle it"
Write-Host "  only 0x01 / 0x02 tiny counts -> remote is not streaming, needs a start handshake"
