param([Parameter(Mandatory)][string]$ParamsJson)
# Submits a Spansh trade-route job and waits for the result. Uses Windows' HTTPS stack
# (Python's OpenSSL can't validate Let's Encrypt's 2026 chain on this machine).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ua = 'EliteTradeCopilot/1.0 (personal tool)'
$p = $ParamsJson | ConvertFrom-Json
$body = @{}
$p.PSObject.Properties | ForEach-Object { $body[$_.Name] = $_.Value }
try {
    $job = Invoke-RestMethod -Method Post -Uri 'https://spansh.co.uk/api/trade/route' -Body $body -UserAgent $ua -TimeoutSec 30
    if ($job.status -eq 'error') { throw "Spansh: $($job.error)" }
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 3
        $r = Invoke-RestMethod -Uri "https://spansh.co.uk/api/results/$($job.job)" -UserAgent $ua -TimeoutSec 30
        if ($r.status -ne 'queued') { break }
    }
    if ($r.status -ne 'ok') { throw "Spansh devolveu status '$($r.status)' $($r.error)" }
    @{ ok = $true; result = $r.result } | ConvertTo-Json -Depth 12 -Compress
} catch {
    $msg = $_.Exception.Message
    if ($_.ErrorDetails.Message) { $msg = $_.ErrorDetails.Message }
    @{ ok = $false; error = $msg } | ConvertTo-Json -Compress
}
