// Read-only status/QR polling keeps the displayed connection state current
// across page loads and later disconnects. Session creation remains an explicit
// operator action on the pairing form.
(function () {
  const box = document.getElementById('qrbox');
  if (!box) return;
  const key = box.dataset.key;
  if (!key) return;

  function showMessage(message, color = '#86efac') {
    const text = document.createElement('p');
    text.className = 'hint';
    text.style.color = color;
    text.textContent = message;
    box.replaceChildren(text);
  }

  function showConnected(phone) {
    const badge = document.createElement('div');
    badge.style.cssText = 'background:#064e3b; color:#86efac; padding:12px 18px; border-radius:8px; font-weight:bold; font-size:15px; border:1px solid #10b981;';
    badge.textContent = `✅ WhatsApp Linked Successfully! (${phone || 'Connected'})`;
    box.replaceChildren(badge);
  }

  function showQR(dataUrl) {
    const frame = document.createElement('div');
    frame.style.cssText = 'background:#fff; padding:12px; border-radius:12px; display:inline-block; box-shadow:0 10px 25px rgba(0,0,0,0.5);';
    const image = document.createElement('img');
    image.src = dataUrl;
    image.alt = 'Scan WhatsApp QR';
    image.style.cssText = 'width:240px; height:240px; display:block;';
    frame.appendChild(image);

    const hint = document.createElement('p');
    hint.style.cssText = 'color:#86efac; font-weight:bold; font-size:13px; margin-top:10px;';
    hint.textContent = '📲 Influencer Phone lo WhatsApp > Linked Devices తో ఈ QR స్కాన్ చేయండి';
    box.replaceChildren(frame, hint);
  }

  async function tick() {
    let nextPollMs = 2500;
    try {
      const statusResponse = await fetch(`/api/wa/${encodeURIComponent(key)}/status`, {
        cache: 'no-store',
        credentials: 'same-origin',
      });
      const status = await statusResponse.json();
      if (status && status.ok && status.status === 'connected') {
        showConnected(status.phone);
        nextPollMs = 5000;
      } else if (status && status.ok && status.hasQR) {
        const qrResponse = await fetch(`/api/wa/${encodeURIComponent(key)}/qr`, {
          cache: 'no-store',
          credentials: 'same-origin',
        });
        const qr = await qrResponse.json();
        if (qr && qr.qr) showQR(qr.qr);
        else showMessage('WhatsApp QR is refreshing. Please wait…');
      } else if (status && status.ok && status.status === 'connecting') {
        showMessage('Connecting to WhatsApp…');
      } else if (status && status.ok && status.status === 'offline') {
        showMessage('WhatsApp is offline. The hub may retry; use Generate Live QR Code to pair again.', '#fbbf24');
        nextPollMs = 5000;
      }
    } catch (e) {
      // Keep the current view while the hub is unavailable; polling is GET-only.
      nextPollMs = 5000;
    }
    // Keep checking after a successful connection so a later disconnect is
    // reflected instead of leaving a stale green badge on the page.
    window.setTimeout(tick, nextPollMs);
  }

  tick();
})();

// Setup mutations require a fresh password confirmation. Passwords are sent
// only in the same-origin HTTPS request body and are never stored in the page.
(function () {
  const dialog = document.getElementById('reauth-dialog');
  if (!dialog) return;

  const passwordInput = document.getElementById('reauth-password');
  const errorBox = document.getElementById('reauth-error');
  const confirmButton = document.getElementById('reauth-confirm');
  const cancelButton = document.getElementById('reauth-cancel');
  let pendingForm = null;
  let pendingSubmitter = null;
  let isVerifying = false;

  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches('[data-require-reauth]')) return;
    if (form.dataset.reauthPassed === 'true') {
      delete form.dataset.reauthPassed;
      return;
    }

    event.preventDefault();
    pendingForm = form;
    pendingSubmitter = event.submitter || null;
    errorBox.textContent = '';
    passwordInput.value = '';
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else {
      // Safe compatibility fallback for older mobile browsers.
      const password = window.prompt('Confirm the dashboard password to save this change');
      if (password) verifyPassword(password);
    }
    if (dialog.open) window.setTimeout(() => passwordInput.focus(), 0);
  }, true);

  async function verifyPassword(password) {
    if (isVerifying || !pendingForm) return;
    isVerifying = true;
    confirmButton.disabled = true;
    errorBox.textContent = 'Checking password…';
    try {
      const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
      const body = new FormData();
      body.append('password', password);
      const response = await fetch('/reauth', {
        method: 'POST',
        body,
        headers: { 'X-CSRF-Token': csrf, 'Accept': 'application/json' },
        credentials: 'same-origin',
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || !result.ok) {
        errorBox.textContent = response.status === 429
          ? 'Too many attempts. Please wait before trying again.'
          : 'Password did not match. Try again.';
        passwordInput.value = '';
        passwordInput.focus();
        return;
      }

      const form = pendingForm;
      const submitter = pendingSubmitter;
      pendingForm = null;
      pendingSubmitter = null;
      passwordInput.value = '';
      if (dialog.open) dialog.close();
      form.dataset.reauthPassed = 'true';
      if (submitter && submitter.form === form) form.requestSubmit(submitter);
      else form.requestSubmit();
    } catch (_error) {
      errorBox.textContent = 'Could not confirm right now. Check your connection and try again.';
    } finally {
      isVerifying = false;
      confirmButton.disabled = false;
    }
  }

  confirmButton.addEventListener('click', () => verifyPassword(passwordInput.value));
  passwordInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      verifyPassword(passwordInput.value);
    }
  });
  cancelButton.addEventListener('click', () => {
    pendingForm = null;
    pendingSubmitter = null;
    passwordInput.value = '';
    if (dialog.open) dialog.close();
  });
  dialog.addEventListener('cancel', () => {
    pendingForm = null;
    pendingSubmitter = null;
    passwordInput.value = '';
  });
})();

// Runs bounded, read-only source-to-service checks from the Setup Center.
(function () {
  const button = document.getElementById('run-live-setup-checks');
  const resultsBox = document.getElementById('live-setup-results');
  if (!button || !resultsBox) return;

  const labels = {
    telegram: 'Telegram account',
    sources: 'Joined source matching',
    whatsapp_hub: 'WhatsApp hub',
  };
  const goodStates = new Set(['connected', 'matched', 'reachable']);
  const errorStates = new Set(['error', 'offline', 'timeout', 'not_configured', 'not_authorized', 'no_matches', 'no_sources']);

  function showResult(title, result) {
    const row = document.createElement('div');
    const state = String(result?.state || 'error');
    row.className = `live-check-result ${goodStates.has(state) ? 'is-ok' : (errorStates.has(state) ? 'is-error' : 'is-warn')}`;
    const heading = document.createElement('strong');
    heading.textContent = `${title} · ${state.replaceAll('_', ' ')}`;
    const detail = document.createElement('span');
    detail.textContent = String(result?.message || 'No diagnostic detail returned.');
    row.append(heading, detail);
    resultsBox.appendChild(row);
  }

  button.addEventListener('click', async () => {
    button.disabled = true;
    button.textContent = 'Checking…';
    resultsBox.replaceChildren();
    resultsBox.setAttribute('aria-busy', 'true');
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
    try {
      const response = await fetch('/api/setup/live-checks', {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrf, 'Accept': 'application/json' },
        credentials: 'same-origin',
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.checks) {
        throw new Error(response.status === 401 ? 'Sign in again to run diagnostics.' : 'Live checks could not be completed.');
      }
      Object.entries(labels).forEach(([key, label]) => showResult(label, payload.checks[key] || {}));
    } catch (error) {
      showResult('Live checks', { state: 'error', message: error.message || 'Check your connection and retry.' });
    } finally {
      resultsBox.setAttribute('aria-busy', 'false');
      button.disabled = false;
      button.textContent = 'Run live checks';
    }
  });
})();
