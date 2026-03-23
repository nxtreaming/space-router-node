# E2E smoke tests for the SpaceRouter GUI binary.
# Runs on Windows CI runners after PyInstaller build.
#
# Usage: .\tests\e2e\gui_smoke_test.ps1 -Binary "dist\spacerouter-gui-windows-x64.exe"
#
# The binary is started with --smoke-test which:
#   1. Opens the webview window
#   2. Starts a health-check HTTP server on port 19876
#   3. Runs internal GUI checks (DOM, JS bridge, API)
#   4. Exits with code 0 on success, 1 on failure

param(
    [Parameter(Mandatory = $true)]
    [string]$Binary
)

$ErrorActionPreference = "Continue"
$Pass = 0
$Fail = 0
$GuiProcess = $null
$HealthPort = 19876
$HealthUrl = "http://127.0.0.1:${HealthPort}/"

function Log($msg) { Write-Host "  [INFO]  $msg" }
function Pass($msg) { Write-Host "  [PASS]  $msg"; $script:Pass++ }
function Fail($msg) { Write-Host "  [FAIL]  $msg"; $script:Fail++ }

function Stop-GuiProcess {
    if ($null -ne $script:GuiProcess -and -not $script:GuiProcess.HasExited) {
        Stop-Process -Id $script:GuiProcess.Id -Force -ErrorAction SilentlyContinue
        $script:GuiProcess.WaitForExit(5000) | Out-Null
    }
}

# ---------- Test 1: GUI binary launches and health endpoint responds ----------
function Test-GuiLaunches {
    Log "Starting GUI binary with --smoke-test flag..."

    $script:GuiProcess = Start-Process -FilePath $Binary -ArgumentList "--smoke-test" -PassThru -NoNewWindow
    Log "Started GUI binary with PID $($script:GuiProcess.Id)"

    # Poll the health endpoint for up to 30 seconds
    Log "Waiting for health endpoint on port $HealthPort..."
    for ($i = 0; $i -lt 60; $i++) {
        if ($script:GuiProcess.HasExited) {
            $exitCode = $script:GuiProcess.ExitCode
            if ($exitCode -eq 0) {
                Pass "GUI smoke test completed (process exited before health poll)"
                $script:GuiProcess = $null
                return
            }
            else {
                Fail "GUI binary exited prematurely with code $exitCode"
                $script:GuiProcess = $null
                return
            }
        }

        try {
            $null = Invoke-WebRequest -Uri $HealthUrl -Method GET -TimeoutSec 1 -ErrorAction SilentlyContinue
            Pass "Health endpoint is responding on port $HealthPort"
            return
        }
        catch {
            # Connection refused — server not ready yet
        }
        Start-Sleep -Milliseconds 500
    }

    Fail "Health endpoint did not respond within 30 seconds"
}

# ---------- Test 2: Health endpoint returns valid JSON with expected keys ----------
function Test-HealthResponse {
    # Process may have already exited from the smoke test
    if ($null -eq $script:GuiProcess -or $script:GuiProcess.HasExited) {
        Log "Process already exited, skipping health response check"
        Pass "Health response check skipped (process self-terminated)"
        return
    }

    Log "Checking health endpoint response..."
    try {
        $response = Invoke-WebRequest -Uri $HealthUrl -Method GET -TimeoutSec 5 -ErrorAction Stop
        $body = $response.Content

        if ($body -match '"running"') {
            Pass "Health endpoint returns valid status JSON"
        }
        else {
            Fail "Health response missing 'running' key: $body"
        }
    }
    catch {
        Fail "Failed to get health response: $_"
    }
}

# ---------- Test 3: Smoke test exits cleanly ----------
function Test-CleanExit {
    if ($null -eq $script:GuiProcess) {
        Log "Process already exited"
        Pass "Clean exit (already terminated)"
        return
    }

    Log "Waiting for GUI smoke test to complete (up to 60 seconds)..."

    $exited = $script:GuiProcess.WaitForExit(60000)

    if ($exited) {
        $exitCode = $script:GuiProcess.ExitCode
        $script:GuiProcess = $null
        if ($exitCode -eq 0) {
            Pass "GUI smoke test exited cleanly (code 0)"
        }
        else {
            Fail "GUI smoke test exited with code $exitCode"
        }
    }
    else {
        Fail "GUI binary did not exit within 60 seconds"
        Stop-GuiProcess
        $script:GuiProcess = $null
    }
}

# ---------- Run all tests ----------
Write-Host ""
Write-Host "=== SpaceRouter GUI - E2E Smoke Tests ==="
Write-Host "Binary: $Binary"
Write-Host ""

try {
    Test-GuiLaunches
    Test-HealthResponse
    Test-CleanExit
}
finally {
    Stop-GuiProcess
}

Write-Host ""
Write-Host "=== Results: $Pass passed, $Fail failed ==="
Write-Host ""

if ($Fail -gt 0) {
    exit 1
}
