/**
 * SpaceRouter Desktop App — Frontend
 *
 * Communicates with the Python backend via window.pywebview.api.*
 */

const EVM_RE = /^(0x)?[0-9a-fA-F]{40}$/;

let statusPollId = null;

// ── Helpers ──

function $(selector) {
  return document.querySelector(selector);
}

function show(id) {
  document.getElementById(id).style.display = "flex";
}

function hide(id) {
  document.getElementById(id).style.display = "none";
}

function truncateAddress(addr) {
  if (!addr || addr.length < 12) return addr || "-";
  return addr.slice(0, 6) + "..." + addr.slice(-4);
}

// ── Onboarding Screen ──

function initOnboarding() {
  const input = $("#wallet-input");
  const error = $("#wallet-error");
  const btn = $("#btn-start");

  input.addEventListener("input", function () {
    const val = input.value.trim();
    if (!val) {
      error.textContent = "";
      input.classList.remove("invalid");
      btn.disabled = true;
      return;
    }
    if (!EVM_RE.test(val)) {
      error.textContent = "Invalid address — expected 0x followed by 40 hex characters";
      input.classList.add("invalid");
      btn.disabled = true;
    } else {
      error.textContent = "";
      input.classList.remove("invalid");
      btn.disabled = false;
    }
  });

  btn.addEventListener("click", async function () {
    const addr = input.value.trim();
    if (!EVM_RE.test(addr)) return;

    btn.disabled = true;
    btn.textContent = "Starting...";

    try {
      const result = await window.pywebview.api.save_wallet_and_start(addr);
      if (result.ok) {
        hide("screen-onboarding");
        showStatus();
      } else {
        error.textContent = result.error || "Unknown error";
        btn.disabled = false;
        btn.textContent = "Start SpaceRouter";
      }
    } catch (e) {
      error.textContent = "Failed to connect to backend";
      btn.disabled = false;
      btn.textContent = "Start SpaceRouter";
    }
  });
}

// ── Status Dashboard ──

function showStatus() {
  show("screen-status");
  updateStatus();
  // Poll every 3 seconds
  if (statusPollId) clearInterval(statusPollId);
  statusPollId = setInterval(updateStatus, 3000);
}

async function updateStatus() {
  try {
    const status = await window.pywebview.api.get_status();

    const dot = $("#status-dot");
    const text = $("#status-text");
    const walletEl = $("#wallet-address");
    const errorBanner = $("#error-banner");
    const errorText = $("#error-text");

    // Wallet address
    walletEl.textContent = status.wallet || "-";

    // Status indicator
    if (status.running) {
      dot.className = "dot dot-running";
      text.textContent = "SpaceRouter is running";
    } else if (status.error) {
      dot.className = "dot dot-stopped";
      text.textContent = "SpaceRouter is stopped";
    } else {
      dot.className = "dot dot-starting";
      text.textContent = "Starting...";
    }

    // Error display
    if (status.error) {
      errorText.textContent = status.error;
      errorBanner.style.display = "block";
    } else {
      errorBanner.style.display = "none";
    }
  } catch (e) {
    // Backend not ready yet — ignore
  }
}

// ── Initialisation ──

async function init() {
  try {
    const needsOnboarding = await window.pywebview.api.needs_onboarding();

    if (needsOnboarding) {
      show("screen-onboarding");
      initOnboarding();
    } else {
      // Already configured — start node and show status
      await window.pywebview.api.start_node();
      showStatus();
    }
  } catch (e) {
    // pywebview.api not ready — retry
    setTimeout(init, 200);
  }
}

// Wait for pywebview to be ready
window.addEventListener("pywebviewready", init);
