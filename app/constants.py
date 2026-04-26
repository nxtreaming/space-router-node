"""Hardcoded provider tunables.

A small set of knobs were previously exposed through Pydantic settings
(``SR_BUFFER_SIZE``, ``SR_MAX_CONNECTIONS``, ...). The v1.5 settings
audit determined that none of these are user-tunable in practice — no
operator has ever set them, and exposing them as config widens the
surface area of the schema for no benefit. They live here as plain
module-level constants. Tests that need to alter them use
``monkeypatch.setattr(app.constants, "X", ...)``.

The Pydantic ``app.config.Settings`` fields with the same names are
intentionally kept in place for now — the broader ``SR_*`` env-var
sweep is a separate track. Once that lands the duplicate definitions
in ``app.config`` go away.
"""

from __future__ import annotations

# ── Network binding / sizing ─────────────────────────────────────────

# Address the proxy listens on. Restrict to a specific interface only
# in unusual deployments; default is "all interfaces" which matches
# what the daemon has shipped with since v0.1.
BIND_ADDRESS: str = "0.0.0.0"

# Per-process cap on concurrent proxy connections. DoS protection (#46).
MAX_CONNECTIONS: int = 256

# Read/write buffer for the proxy stream relay.
BUFFER_SIZE: int = 65536

# How long we wait on individual upstream operations (DNS, connect,
# initial response read).
REQUEST_TIMEOUT: float = 30.0

# Idle-tunnel timeout for established CONNECT relays.
RELAY_TIMEOUT: float = 300.0

# ── Registration / NAT ──────────────────────────────────────────────

# Number of times the daemon retries registration with the
# Coordination API before giving up.
REGISTER_MAX_RETRIES: int = 5

# Lease duration (seconds) we ask the home router to keep the UPnP /
# NAT-PMP mapping alive. 0 = permanent (some routers ignore that).
UPNP_LEASE_DURATION: int = 3600
