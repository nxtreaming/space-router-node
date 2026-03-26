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

// ── Environment Selector ──

async function populateEnvSelector() {
  const select = $("#env-select");
  try {
    const envs = await window.pywebview.api.get_environments();
    select.innerHTML = "";
    for (const env of envs) {
      const opt = document.createElement("option");
      opt.value = env.key;
      opt.textContent = env.label;
      if (env.active) opt.selected = true;
      select.appendChild(opt);
    }
    select.addEventListener("change", async function () {
      await window.pywebview.api.set_environment(select.value);
    });
  } catch (e) {
    // Fallback if API not ready
  }
}

function envLabel(envKey) {
  const labels = {
    production: "Production",
    test: "Test (CC Testnet)",
    staging: "Staging",
    local: "Local",
  };
  return labels[envKey] || envKey;
}

// ── Onboarding Screen ──

function validateInputs() {
  const stakingInput = $("#staking-input");
  const stakingError = $("#staking-error");
  const collectionInput = $("#collection-input");
  const collectionError = $("#collection-error");
  const btn = $("#btn-start");

  const stakingVal = stakingInput.value.trim();
  const collectionVal = collectionInput.value.trim();

  let stakingValid = false;
  let collectionValid = true; // optional — valid when empty

  // Validate staking (required)
  if (!stakingVal) {
    stakingError.textContent = "";
    stakingInput.classList.remove("invalid");
  } else if (!EVM_RE.test(stakingVal)) {
    stakingError.textContent = "Invalid address — expected 0x followed by 40 hex characters";
    stakingInput.classList.add("invalid");
  } else {
    stakingError.textContent = "";
    stakingInput.classList.remove("invalid");
    stakingValid = true;
  }

  // Validate collection (optional)
  if (!collectionVal) {
    collectionError.textContent = "";
    collectionInput.classList.remove("invalid");
  } else if (!EVM_RE.test(collectionVal)) {
    collectionError.textContent = "Invalid address — expected 0x followed by 40 hex characters";
    collectionInput.classList.add("invalid");
    collectionValid = false;
  } else {
    collectionError.textContent = "";
    collectionInput.classList.remove("invalid");
  }

  btn.disabled = !(stakingValid && collectionValid);
}

function initOnboarding() {
  const stakingInput = $("#staking-input");
  const collectionInput = $("#collection-input");
  const stakingError = $("#staking-error");
  const btn = $("#btn-start");

  populateEnvSelector();

  stakingInput.addEventListener("input", validateInputs);
  collectionInput.addEventListener("input", validateInputs);

  btn.addEventListener("click", async function () {
    const stakingAddr = stakingInput.value.trim();
    const collectionAddr = collectionInput.value.trim();
    if (!EVM_RE.test(stakingAddr)) return;

    btn.disabled = true;
    btn.textContent = "Starting...";

    try {
      const result = await window.pywebview.api.save_wallet_and_start(
        stakingAddr, collectionAddr
      );
      if (result.ok) {
        hide("screen-onboarding");
        showStatus();
      } else {
        stakingError.textContent = result.error || "Unknown error";
        btn.disabled = false;
        btn.textContent = "Start SpaceRouter";
      }
    } catch (e) {
      stakingError.textContent = "Failed to connect to backend";
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
    const stakingEl = $("#staking-address");
    const collectionEl = $("#collection-address");
    const envBadge = $("#env-badge");
    const errorBanner = $("#error-banner");
    const errorText = $("#error-text");

    // Wallet addresses
    stakingEl.textContent = status.staking_address || status.wallet || "-";
    collectionEl.textContent = status.collection_address || "-";

    // Environment badge
    if (status.environment && status.environment !== "production") {
      envBadge.textContent = envLabel(status.environment);
      envBadge.style.display = "block";
    } else {
      envBadge.style.display = "none";
    }

    // Status indicator — use phase for granular state
    const phase = status.phase || "stopped";
    if (phase === "running") {
      dot.className = "dot dot-running";
      text.textContent = "SpaceRouter is running";
    } else if (phase === "registering") {
      dot.className = "dot dot-starting";
      text.textContent = "Registering with network...";
    } else if (phase === "starting") {
      dot.className = "dot dot-starting";
      text.textContent = "Starting...";
    } else if (status.error) {
      dot.className = "dot dot-stopped";
      text.textContent = "SpaceRouter is stopped";
    } else {
      dot.className = "dot dot-stopped";
      text.textContent = "Stopped";
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
