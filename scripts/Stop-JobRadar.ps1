[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Medium")]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,

    [ValidateRange(1, 120)]
    [int]$GracefulTimeoutSeconds = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-DescendantProcessIds {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RootProcessId
    )

    $processes = @(
        Get-CimInstance Win32_Process |
            Select-Object ProcessId, ParentProcessId
    )
    $pendingIds = [System.Collections.Generic.Queue[int]]::new()
    $seenIds = [System.Collections.Generic.HashSet[int]]::new()
    $resultIds = [System.Collections.Generic.List[int]]::new()
    $pendingIds.Enqueue($RootProcessId)

    while ($pendingIds.Count -gt 0) {
        $parentProcessId = $pendingIds.Dequeue()

        foreach ($process in $processes) {
            if (
                [int]$process.ParentProcessId -ne $parentProcessId
            ) {
                continue
            }

            $childProcessId = [int]$process.ProcessId

            if ($seenIds.Add($childProcessId)) {
                $resultIds.Add($childProcessId)
                $pendingIds.Enqueue($childProcessId)
            }
        }
    }

    return $resultIds.ToArray()
}

function Test-ProcessExists {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId
    )

    return $null -ne (
        Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    )
}

$baseUri = "http://127.0.0.1:$Port"

try {
    $runtime = Invoke-RestMethod `
        -Uri "$baseUri/api/runtime" `
        -Method Get `
        -TimeoutSec 3
}
catch {
    throw (
        "No responsive Job Radar dashboard was found on port " +
        "$Port. Nothing was stopped."
    )
}

if (
    $runtime.application -ne "job-radar" -or
    $null -eq $runtime.process_id
) {
    throw (
        "The service on port $Port did not identify itself as " +
        "Job Radar. Nothing was stopped."
    )
}

$dashboardProcessId = [int]$runtime.process_id
$listenerProcessIds = @(
    Get-NetTCPConnection `
        -State Listen `
        -LocalPort $Port `
        -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
)

if ($listenerProcessIds -notcontains $dashboardProcessId) {
    throw (
        "Job Radar reported process $dashboardProcessId, but that " +
        "process does not own port $Port. Nothing was stopped."
    )
}

$targetDescription = (
    "Job Radar on port $Port (process $dashboardProcessId)"
)

if (
    -not $PSCmdlet.ShouldProcess(
        $targetDescription,
        "Stop the dashboard and its managed child processes"
    )
) {
    return
}

$trackedProcessIds = (
    [System.Collections.Generic.HashSet[int]]::new()
)

foreach (
    $processId in Get-DescendantProcessIds `
        -RootProcessId $dashboardProcessId
) {
    $null = $trackedProcessIds.Add($processId)
}

$shutdownRequested = $false

try {
    $null = Invoke-RestMethod `
        -Uri "$baseUri/api/shutdown" `
        -Method Post `
        -TimeoutSec 5
    $shutdownRequested = $true
}
catch {
    Write-Warning (
        "The graceful shutdown request failed. The verified " +
        "Job Radar process tree will be stopped after the " +
        "grace period."
    )
}

$deadline = (
    Get-Date
).AddSeconds($GracefulTimeoutSeconds)

do {
    if (Test-ProcessExists -ProcessId $dashboardProcessId) {
        foreach (
            $processId in Get-DescendantProcessIds `
                -RootProcessId $dashboardProcessId
        ) {
            $null = $trackedProcessIds.Add($processId)
        }
    }

    $remainingChildIds = @(
        $trackedProcessIds |
            Where-Object {
                Test-ProcessExists -ProcessId $_
            }
    )
    $dashboardIsRunning = (
        Test-ProcessExists -ProcessId $dashboardProcessId
    )

    if (
        -not $dashboardIsRunning -and
        $remainingChildIds.Count -eq 0
    ) {
        Write-Output (
            "Job Radar stopped successfully. Dashboard and " +
            "managed child processes are no longer running."
        )
        return
    }

    Start-Sleep -Milliseconds 250
} while ((Get-Date) -lt $deadline)

Write-Warning (
    "Graceful shutdown did not finish within " +
    "$GracefulTimeoutSeconds seconds. Stopping only the " +
    "verified Job Radar process tree."
)

if (Test-ProcessExists -ProcessId $dashboardProcessId) {
    & taskkill.exe `
        /PID $dashboardProcessId `
        /T `
        /F |
        Out-Null
}

foreach ($processId in $trackedProcessIds) {
    if (Test-ProcessExists -ProcessId $processId) {
        Stop-Process -Id $processId -Force
    }
}

Start-Sleep -Milliseconds 500

$remainingProcessIds = @(
    $dashboardProcessId
    $trackedProcessIds
) | Where-Object {
    Test-ProcessExists -ProcessId $_
}

if ($remainingProcessIds.Count -gt 0) {
    throw (
        "Job Radar shutdown is incomplete. Remaining process IDs: " +
        ($remainingProcessIds -join ", ")
    )
}

$shutdownMethod = if ($shutdownRequested) {
    "after a graceful shutdown request and verified fallback cleanup"
}
else {
    "using verified fallback cleanup"
}

Write-Output "Job Radar stopped $shutdownMethod."
