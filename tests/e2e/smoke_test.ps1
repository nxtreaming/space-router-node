# E2E smoke tests for the compiled Space Router Home Node binary.
# Runs on Windows CI runners after PyInstaller build.
#
# Usage: .\tests\e2e\smoke_test.ps1 -Binary "dist\space-router-node-windows-x64.exe"
#
# Required environment variables:
#   EXPECTED_VERSION  - version string the binary should report
#   SR_NODE_PORT      - port for the node to bind (use a high port to avoid conflicts)
#   SR_PUBLIC_IP      - public IP override
#   SR_UPNP_ENABLED   - set to "false" for CI
#   SR_WALLET_ADDRESS - EVM wallet address (e.g. 0x0000...0001 for CI)

param(
    [Parameter(Mandatory = $true)]
    [string]$Binary
)

$ErrorActionPreference = "Continue"
$Pass = 0
$Fail = 0
$MockApiProcess = $null
$MockApiPort = 19099
$BasePort = [int]$env:SR_NODE_PORT
if ($BasePort -eq 0) { $BasePort = 19090 }
$PortBindingPort = $BasePort
$ShutdownPort = $BasePort + 1

# The daemon enforces a single-instance lock at ~/.spacerouter/daemon.lock.
# That's the right behaviour for real operators (we don't want two daemons
# fighting over the same receipts.db), but the smoke test runs two daemon
# instances back-to-back on the same host — Test-PortBinding then
# Test-CleanShutdown — so we explicitly tear the lock down between cases.
# The path is derived from app/paths.py::config_dir() and ignores
# SR_RECEIPT_STORE_PATH (settings_from_provider_settings rebuilds it from
# config_dir), so per-test path overrides do NOT decouple the lock.
$script:DaemonLockPath = Join-Path $env:USERPROFILE ".spacerouter\daemon.lock"

function Log($msg) { Write-Host "  [INFO]  $msg" }
function Pass($msg) { Write-Host "  [PASS]  $msg"; $script:Pass++ }
function Fail($msg) { Write-Host "  [FAIL]  $msg"; $script:Fail++ }

# Stop a daemon process, wait for it to fully exit, then remove the
# daemon.lock file so the next sub-test can acquire it. The msvcrt lock
# releases when the handle is closed at process death, but the file
# itself sticks around with the prior PID inside it — and on Windows
# the OS sometimes lags a beat between TerminateProcess and the kernel
# closing the handle, which is exactly the window where the stale-PID
# branch in _acquire_daemon_lock can mis-fire under CI load. Explicit
# delete after Wait removes the ambiguity.
function Stop-DaemonAndClearLock {
    param([System.Diagnostics.Process]$Proc, [int]$WaitMs = 10000)
    if ($null -eq $Proc) { return }
    try {
        if (-not $Proc.HasExited) {
            # IMPORTANT: PyInstaller produces a bootloader .exe that
            # spawns the actual Python child process. Stop-Process
            # kills only the named PID — the orphan child keeps the
            # daemon.lock and continues handling traffic. We need
            # taskkill /T (tree) /F (force) to kill the whole tree.
            #
            # Without /T the next sub-test sees the orphan, refuses to
            # acquire the lock, and the test fails with "Another daemon
            # already running" — even though Test-PortBinding logged
            # PASS. Caught in the test.93 CI run after PR #84's
            # PID-reuse fix wasn't enough.
            $taskkillExe = Join-Path $env:SystemRoot "System32\taskkill.exe"
            if (Test-Path $taskkillExe) {
                & $taskkillExe /PID $Proc.Id /T /F 2>$null | Out-Null
            } else {
                Stop-Process -Id $Proc.Id -Force -ErrorAction SilentlyContinue
            }
        }
        $Proc.WaitForExit($WaitMs) | Out-Null
        # WaitForExit returns when the named PID exits. Children may
        # take a tick longer to clean up — short fixed sleep makes the
        # next acquire reliable.
        Start-Sleep -Milliseconds 500
    }
    catch { }
    Remove-Item -Force -ErrorAction SilentlyContinue $script:DaemonLockPath
}

# Reliable TCP port check using TcpClient (Test-NetConnection is unreliable on CI runners)
function Test-TcpPort {
    param([int]$Port, [int]$TimeoutMs = 2000)
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $result = $tcp.BeginConnect("127.0.0.1", $Port, $null, $null)
        $success = $result.AsyncWaitHandle.WaitOne($TimeoutMs)
        if ($success) {
            $tcp.EndConnect($result)
        }
        $tcp.Close()
        return $success
    }
    catch {
        return $false
    }
}

# Poll until port is listening or timeout (returns $true/$false)
function Wait-ForPort {
    param([int]$Port, [int]$MaxSeconds = 30)
    for ($i = 0; $i -lt $MaxSeconds; $i++) {
        if (Test-TcpPort -Port $Port) {
            return $true
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

# Start a mock coordination API
function Start-MockApi {
    $mockScript = @"
import http.server, json

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        if '/request-probe' in self.path:
            self.wfile.write(json.dumps({'ok': True}).encode())
        else:
            self.wfile.write(json.dumps({
                'status': 'registered',
                'node_id': 'test-node-001',
                'identity_address': '0x0000000000000000000000000000000000000001',
                'staking_address': '0x0000000000000000000000000000000000000001',
                'collection_address': '0x0000000000000000000000000000000000000001',
                'endpoint_url': 'https://127.0.0.1:19090',
                'wallet_address': '0x0000000000000000000000000000000000000001',
                'node_address': '0x0000000000000000000000000000000000000000',
            }).encode())
    def do_PATCH(self):
        length = int(self.headers.get('Content-Length', 0))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{}')
    def log_message(self, *args):
        pass

server = http.server.HTTPServer(('127.0.0.1', $MockApiPort), Handler)
server.serve_forever()
"@

    $tempFile = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.py'
    $mockScript | Out-File -FilePath $tempFile -Encoding utf8
    $script:MockApiProcess = Start-Process -FilePath python -ArgumentList $tempFile -PassThru -NoNewWindow
    $env:SR_COORDINATION_API_URL = "http://127.0.0.1:$MockApiPort"
    Log "Started mock coordination API on port $MockApiPort (PID $($script:MockApiProcess.Id))"

    # Wait for the mock API to be ready
    if (Wait-ForPort -Port $MockApiPort -MaxSeconds 10) {
        Log "Mock API is ready"
    }
    else {
        Log "WARNING: Mock API may not be ready after 10 seconds"
    }
}

function Stop-MockApi {
    if ($null -ne $script:MockApiProcess -and -not $script:MockApiProcess.HasExited) {
        Stop-Process -Id $script:MockApiProcess.Id -Force -ErrorAction SilentlyContinue
        $script:MockApiProcess.WaitForExit(5000) | Out-Null
    }
}

# ---------- Test 1: --version flag ----------
function Test-VersionFlag {
    Log "Testing --version flag..."
    try {
        $output = & $Binary --version 2>&1 | Out-String
        if ($output -match [regex]::Escape($env:EXPECTED_VERSION)) {
            Pass "--version reports '$($env:EXPECTED_VERSION)'"
        }
        else {
            Fail "--version output was '$($output.Trim())', expected to contain '$($env:EXPECTED_VERSION)'"
        }
    }
    catch {
        Fail "--version threw an error: $_"
    }
}

# ---------- Test 2: Binary starts and binds to port ----------
function Test-PortBinding {
    # Use a dedicated port so TIME_WAIT state doesn't affect other tests
    $env:SR_NODE_PORT = "$PortBindingPort"
    Log "Testing port binding on port $($env:SR_NODE_PORT)..."

    $proc = Start-Process -FilePath $Binary -PassThru -NoNewWindow
    Log "Started binary with PID $($proc.Id)"

    # Poll until the port is listening (up to 30 seconds)
    $listening = Wait-ForPort -Port ([int]$env:SR_NODE_PORT) -MaxSeconds 30

    if ($proc.HasExited) {
        Fail "Binary exited prematurely with code $($proc.ExitCode)"
        # Even on premature exit, drop the lock file so the next test
        # isn't blocked by a stale-PID daemon.lock entry.
        Remove-Item -Force -ErrorAction SilentlyContinue $script:DaemonLockPath
        return
    }

    # Clean up: stop the daemon, wait for full exit, drop the lock file
    # so Test-CleanShutdown's daemon can acquire it.
    Stop-DaemonAndClearLock -Proc $proc -WaitMs 5000

    if ($listening) {
        Pass "Binary is listening on port $($env:SR_NODE_PORT)"
    }
    else {
        Fail "Binary did not bind to port $($env:SR_NODE_PORT) within 30 seconds"
    }
}

# ---------- Test 3: Clean shutdown ----------
function Test-CleanShutdown {
    # Use a different port than Test-PortBinding to avoid TIME_WAIT conflicts
    $env:SR_NODE_PORT = "$ShutdownPort"
    Log "Testing clean shutdown on port $($env:SR_NODE_PORT)..."

    $proc = Start-Process -FilePath $Binary -PassThru -NoNewWindow
    Log "Started binary with PID $($proc.Id)"

    # Wait until the port is bound before sending stop signal
    $ready = Wait-ForPort -Port ([int]$env:SR_NODE_PORT) -MaxSeconds 30

    if ($proc.HasExited) {
        Fail "Binary exited before shutdown signal could be sent"
        return
    }

    if (-not $ready) {
        Log "WARNING: Port not detected as listening, proceeding with shutdown test anyway"
    }

    # On Windows, console apps don't respond to WM_CLOSE. Use taskkill /T /F
    # to kill the entire process tree — without /T the PyInstaller
    # bootloader exits but the actual Python child orphan keeps running
    # and holds the daemon.lock. Same fix shape as Stop-DaemonAndClearLock.
    $taskkillExe = Join-Path $env:SystemRoot "System32\taskkill.exe"
    if (Test-Path $taskkillExe) {
        & $taskkillExe /PID $proc.Id /T /F 2>$null | Out-Null
    } else {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
    Log "Sent stop signal to PID $($proc.Id) (tree)"

    # Wait up to 10 seconds for exit of the named PID
    $exited = $proc.WaitForExit(10000)
    # Children may take a moment longer
    Start-Sleep -Milliseconds 500

    # Always tidy the lock file — if the test fails, the next CI run on
    # a recycled runner image shouldn't inherit a stuck lock.
    Remove-Item -Force -ErrorAction SilentlyContinue $script:DaemonLockPath

    if ($exited) {
        Pass "Process stopped successfully (exit code $($proc.ExitCode))"
    }
    else {
        Fail "Binary did not exit within 10 seconds after stop signal"
    }
}

# ---------- Run all tests ----------
Write-Host ""
Write-Host "=== Space Router Home Node - E2E Smoke Tests ==="
Write-Host "Binary:  $Binary"
Write-Host "Version: $($env:EXPECTED_VERSION)"
Write-Host "Ports:   $PortBindingPort (binding), $ShutdownPort (shutdown)"
Write-Host ""

try {
    Start-MockApi

    Test-VersionFlag
    Test-PortBinding
    Test-CleanShutdown
}
finally {
    Stop-MockApi
}

Write-Host ""
Write-Host "=== Results: $Pass passed, $Fail failed ==="
Write-Host ""

if ($Fail -gt 0) {
    exit 1
}
