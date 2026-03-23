#!/usr/bin/env bash
# E2E smoke tests for the SpaceRouter GUI binary.
# Runs on macOS CI runners after PyInstaller build.
#
# Usage: bash tests/e2e/gui_smoke_test.sh <path-to-binary>
#
# The binary is started with --smoke-test which:
#   1. Opens the webview window
#   2. Starts a health-check HTTP server on port 19876
#   3. Runs internal GUI checks (DOM, JS bridge, API)
#   4. Exits with code 0 on success, 1 on failure

set -euo pipefail

BINARY="$1"
PASS=0
FAIL=0
GUI_PID=""
HEALTH_PORT=19876
HEALTH_URL="http://127.0.0.1:${HEALTH_PORT}/"

log()  { echo "  [INFO]  $*"; }
pass() { echo "  [PASS]  $*"; PASS=$((PASS + 1)); }
fail() { echo "  [FAIL]  $*"; FAIL=$((FAIL + 1)); }

cleanup() {
    if [ -n "$GUI_PID" ]; then
        kill "$GUI_PID" 2>/dev/null || true
        wait "$GUI_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ---------- Test 1: GUI binary launches and health endpoint responds ----------
test_gui_launches() {
    log "Starting GUI binary with --smoke-test flag..."

    "$BINARY" --smoke-test &
    GUI_PID=$!
    log "Started GUI binary with PID $GUI_PID"

    # Poll the health endpoint for up to 30 seconds
    log "Waiting for health endpoint on port $HEALTH_PORT..."
    for i in $(seq 1 60); do
        if ! kill -0 "$GUI_PID" 2>/dev/null; then
            # Process exited already — check if it was the smoke test completing
            wait "$GUI_PID" 2>/dev/null
            EXIT_CODE=$?
            if [ "$EXIT_CODE" -eq 0 ]; then
                pass "GUI smoke test completed (process exited before health poll)"
                GUI_PID=""
                return 0
            else
                fail "GUI binary exited prematurely with code $EXIT_CODE"
                GUI_PID=""
                return 1
            fi
        fi

        if curl -s -o /dev/null -w '' "$HEALTH_URL" 2>/dev/null; then
            pass "Health endpoint is responding on port $HEALTH_PORT"
            return 0
        fi
        sleep 0.5
    done

    fail "Health endpoint did not respond within 30 seconds"
    return 1
}

# ---------- Test 2: Health endpoint returns valid JSON with expected keys ----------
test_health_response() {
    # Process may have already exited from the smoke test
    if [ -z "$GUI_PID" ] || ! kill -0 "$GUI_PID" 2>/dev/null; then
        log "Process already exited, skipping health response check"
        pass "Health response check skipped (process self-terminated)"
        return 0
    fi

    log "Checking health endpoint response..."
    RESPONSE=$(curl -s "$HEALTH_URL" 2>/dev/null || true)

    if [ -z "$RESPONSE" ]; then
        fail "Empty response from health endpoint"
        return 1
    fi

    # Check that response contains "running" key (basic JSON validation)
    if echo "$RESPONSE" | grep -q '"running"'; then
        pass "Health endpoint returns valid status JSON"
    else
        fail "Health response missing 'running' key: $RESPONSE"
    fi
}

# ---------- Test 3: Smoke test exits cleanly ----------
test_clean_exit() {
    if [ -z "$GUI_PID" ]; then
        log "Process already exited"
        pass "Clean exit (already terminated)"
        return 0
    fi

    log "Waiting for GUI smoke test to complete (up to 60 seconds)..."

    for i in $(seq 1 60); do
        if ! kill -0 "$GUI_PID" 2>/dev/null; then
            wait "$GUI_PID" 2>/dev/null
            EXIT_CODE=$?
            GUI_PID=""
            if [ "$EXIT_CODE" -eq 0 ]; then
                pass "GUI smoke test exited cleanly (code 0)"
            else
                fail "GUI smoke test exited with code $EXIT_CODE"
            fi
            return 0
        fi
        sleep 1
    done

    fail "GUI binary did not exit within 60 seconds"
    kill -9 "$GUI_PID" 2>/dev/null || true
    wait "$GUI_PID" 2>/dev/null || true
    GUI_PID=""
}

# ---------- Run all tests ----------
echo ""
echo "=== SpaceRouter GUI — E2E Smoke Tests ==="
echo "Binary: $BINARY"
echo ""

test_gui_launches
test_health_response
test_clean_exit

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
echo ""

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
