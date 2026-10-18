param([int]$PauseSeconds = 35)

$ErrorActionPreference = 'Stop'
if ($PauseSeconds -lt 31 -or $PauseSeconds -gt 120) {
    throw 'Use a pause between 31 and 120 seconds for the default thirty-second alert threshold.'
}
$projectPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Push-Location $projectPath
try {
    $baseline = Invoke-RestMethod 'http://127.0.0.1:8003/backlog' -TimeoutSec 10
    if ($baseline.metrics.outbox_pending -ne 0 -or $baseline.metrics.processing_outstanding -ne 0) {
        throw 'Start with a drained pipeline; inspect /backlog before running this experiment.'
    }
    $headers = @{ 'X-Customer-Id' = 'backlog-drill'; 'Idempotency-Key' = [guid]::NewGuid().ToString() }
    $hold = $null
    try {
        docker compose stop consumer | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not stop consumer' }
        $body = @{ event_id = 'demo-general'; quantity = 1 } | ConvertTo-Json
        $hold = Invoke-RestMethod 'http://127.0.0.1:8002/reservations' -Method Post -Headers $headers -ContentType 'application/json' -Body $body -TimeoutSec 10
        $cancelUrl = 'http://127.0.0.1:8002/reservations/' + $hold.id + '/cancel'
        $cancelled = Invoke-RestMethod $cancelUrl -Method Post -Headers $headers -TimeoutSec 10
        if ($cancelled.status -ne 'CANCELLED') { throw 'The experiment hold was not cancelled' }
        $publishDeadline = (Get-Date).AddSeconds(20)
        do {
            $paused = Invoke-RestMethod 'http://127.0.0.1:8003/backlog' -TimeoutSec 10
            if ($paused.metrics.outbox_pending -eq 0 -and $paused.metrics.processing_outstanding -ge 2) { break }
            if ((Get-Date) -ge $publishDeadline) { throw 'Events did not reach the published backlog' }
            Start-Sleep -Seconds 1
        } while ($true)
        Start-Sleep -Seconds $PauseSeconds
        $paused = Invoke-RestMethod 'http://127.0.0.1:8003/backlog' -TimeoutSec 10
        if ($paused.alerts -notcontains 'PROCESSING_BACKLOG_AGING') { throw 'Expected backlog age flag was absent' }
    }
    finally {
        # Always restore the worker, including when an assertion or HTTP call fails.
        if ($null -ne $hold) {
            try {
                Invoke-RestMethod ('http://127.0.0.1:8002/reservations/' + $hold.id + '/cancel') -Method Post -Headers $headers -TimeoutSec 10 | Out-Null
            }
            catch { Write-Warning 'Could not verify cancellation; inspect the experiment reservation.' }
        }
        docker compose start consumer | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not restart consumer; run docker compose start consumer' }
    }
    $recoveryDeadline = (Get-Date).AddSeconds(30)
    do {
        $recovered = Invoke-RestMethod 'http://127.0.0.1:8003/backlog' -TimeoutSec 10
        if ($recovered.metrics.outbox_pending -eq 0 -and $recovered.metrics.processing_outstanding -eq 0) { break }
        if ((Get-Date) -ge $recoveryDeadline) { throw 'Backlog did not drain after restarting consumer' }
        Start-Sleep -Seconds 1
    } while ($true)
    $report = @{
        recorded_at_utc = [DateTime]::UtcNow.ToString('o')
        pause_seconds = $PauseSeconds
        reservation_id = $hold.id
        baseline = $baseline
        paused = $paused
        recovered = $recovered
    } | ConvertTo-Json -Depth 8
    $reportDirectory = Join-Path $projectPath 'test-results'
    New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $reportDirectory 'backlog-drill.json'), $report)
    $report
}
finally {
    Pop-Location
}
