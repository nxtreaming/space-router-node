/**
 * SpaceRouter Desktop App — Frontend
 *
 * Communicates with the Python backend via window.pywebview.api.*
 */

const EVM_RE = /^(0x)?[0-9a-fA-F]{40}$/;
const HEX_KEY_RE = /^(0x)?[0-9a-fA-F]{64}$/;

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

function showBlock(id) {
  document.getElementById(id).style.display = "block";
}

function truncateAddress(addr) {
  if (!addr || addr.length < 12) return addr || "-";
  return addr.slice(0, 6) + "..." + addr.slice(-4);
}

// ── Onboarding Screen ──

function initOnboarding() {
  const radioGenerate = $("#radio-generate");
  const radioImport = $("#radio-import");
  const importSection = $("#import-key-section");
  const identityKeyInput = $("#identity-key-input");
  const identityKeyError = $("#identity-key-error");
  const stakingInput = $("#staking-input");
  const stakingError = $("#staking-error");
  const collectionInput = $("#collection-input");
  const collectionError = $("#collection-error");
  const btn = $("#btn-start");
  const advancedToggle = $("#advanced-toggle");
  const advancedSection = $("#advanced-section");
  const advancedArrow = $("#advanced-arrow");

  // ── Identity key mode toggle ──
  function updateKeyMode() {
    if (radioImport.checked) {
      importSection.style.display = "block";
    } else {
      importSection.style.display = "none";
      identityKeyError.textContent = "";
      identityKeyInput.classList.remove("invalid");
    }
    validateForm();
  }

  radioGenerate.addEventListener("change", updateKeyMode);
  radioImport.addEventListener("change", updateKeyMode);

  // ── Import key validation ──
  identityKeyInput.addEventListener("input", function () {
    const val = identityKeyInput.value.trim();
    if (!val) {
      identityKeyError.textContent = "";
      identityKeyInput.classList.remove("invalid");
    } else if (!HEX_KEY_RE.test(val)) {
      identityKeyError.textContent = "Expected 64 hex characters (with or without 0x prefix)";
      identityKeyInput.classList.add("invalid");
    } else {
      identityKeyError.textContent = "";
      identityKeyInput.classList.remove("invalid");
    }
    validateForm();
  });

  // ── Advanced toggle ──
  advancedToggle.addEventListener("click", function () {
    const open = advancedSection.style.display !== "none";
    advancedSection.style.display = open ? "none" : "block";
    advancedArrow.textContent = open ? "▸" : "▾";
  });

  // ── Optional address validation ──
  function validateAddress(input, errorEl) {
    const val = input.value.trim();
    if (!val) {
      errorEl.textContent = "";
      input.classList.remove("invalid");
      return true;
    }
    if (!EVM_RE.test(val)) {
      errorEl.textContent = "Invalid address — expected 0x followed by 40 hex characters";
      input.classList.add("invalid");
      return false;
    }
    errorEl.textContent = "";
    input.classList.remove("invalid");
    return true;
  }

  stakingInput.addEventListener("input", function () {
    validateAddress(stakingInput, stakingError);
    validateForm();
  });
  collectionInput.addEventListener("input", function () {
    validateAddress(collectionInput, collectionError);
    validateForm();
  });

  // ── Form-level enable/disable ──
  function validateForm() {
    const importValid = radioGenerate.checked ||
      (radioImport.checked && HEX_KEY_RE.test(identityKeyInput.value.trim()));
    const stakingValid = validateAddress(stakingInput, stakingError);
    const collectionValid = validateAddress(collectionInput, collectionError);
    btn.disabled = !(importValid && stakingValid && collectionValid);
  }

  // Enable button immediately for generate mode
  validateForm();

  // ── Submit ──
  btn.addEventListener("click", async function () {
    btn.disabled = true;
    btn.textContent = "Starting...";

    const passphrase = $("#passphrase-input").value;
    const staking = stakingInput.value.trim();
    const collection = collectionInput.value.trim();
    const identityKeyHex = radioImport.checked ? identityKeyInput.value.trim() : "";

    try {
      const result = await window.pywebview.api.save_onboarding_and_start(
        passphrase, staking, collection, identityKeyHex,
      );
      if (result.ok) {
        hide("screen-onboarding");
        showStatus();
      } else {
        // Show error inline
        const errEl = document.createElement("p");
        errEl.className = "error";
        errEl.style.marginTop = "12px";
        errEl.textContent = result.error || "Unknown error";
        btn.parentNode.insertBefore(errEl, btn.nextSibling);
        btn.disabled = false;
        btn.textContent = "Start SpaceRouter";
      }
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "Start SpaceRouter";
    }
  });
}

// ── Status Dashboard ──

function showStatus() {
  show("screen-status");
  updateStatus();
  if (statusPollId) clearInterval(statusPollId);
  statusPollId = setInterval(updateStatus, 3000);
}

async function updateStatus() {
  try {
    const status = await window.pywebview.api.get_status();

    const dot = $("#status-dot");
    const text = $("#status-text");
    const stakingEl = $("#staking-address");
    const stakingDisplay = $("#staking-display");
    const errorBanner = $("#error-banner");
    const errorText = $("#error-text");

    // Staking address
    if (status.staking) {
      stakingEl.textContent = status.staking;
      stakingDisplay.style.display = "flex";
    }

    // Check for passphrase-required error
    if (status.error && status.error.includes("KeystorePassphraseRequired")) {
      showUnlockDialog();
      return;
    }

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
    if (status.error && !status.error.includes("KeystorePassphraseRequired")) {
      errorText.textContent = status.error;
      errorBanner.style.display = "block";
    } else {
      errorBanner.style.display = "none";
    }
  } catch (e) {
    // Backend not ready yet — ignore
  }
}

// ── Passphrase Unlock Dialog ──

function showUnlockDialog() {
  show("dialog-overlay");
  if (statusPollId) {
    clearInterval(statusPollId);
    statusPollId = null;
  }

  const btn = $("#btn-unlock");
  const input = $("#unlock-passphrase");
  const errEl = $("#unlock-error");

  // Prevent duplicate listeners
  const newBtn = btn.cloneNode(true);
  btn.parentNode.replaceChild(newBtn, btn);

  newBtn.addEventListener("click", async function () {
    const passphrase = input.value;
    if (!passphrase) {
      errEl.textContent = "Passphrase is required";
      return;
    }
    newBtn.disabled = true;
    newBtn.textContent = "Unlocking...";
    errEl.textContent = "";

    try {
      const result = await window.pywebview.api.unlock_and_start(passphrase);
      if (result.ok) {
        hide("dialog-overlay");
        input.value = "";
        showStatus();
      } else {
        errEl.textContent = result.error || "Incorrect passphrase";
        newBtn.disabled = false;
        newBtn.textContent = "Unlock";
      }
    } catch (e) {
      errEl.textContent = "Failed to connect to backend";
      newBtn.disabled = false;
      newBtn.textContent = "Unlock";
    }
  });
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
