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

// Removals (delete a channel, a creator, a deal source) need one password
// confirmation per unlock window: the first delete asks, later deletes in the
// window do not. Adding and saving are never intercepted. Passwords travel
// only in the same-origin request body and are never stored in the page.
(function () {
  const dialog = document.getElementById('reauth-dialog');
  if (!dialog) return;

  const passwordInput = document.getElementById('reauth-password');
  const errorBox = document.getElementById('reauth-error');
  const confirmButton = document.getElementById('reauth-confirm');
  const cancelButton = document.getElementById('reauth-cancel');
  const lockBadge = document.querySelector('.nav-lock');
  const SNAPSHOT_KEY = 'pending_setup_form';
  let pendingForm = null;
  let pendingSubmitter = null;
  let isVerifying = false;
  let unlocked = false;
  let windowMinutes = Number(dialog.dataset.unlockMinutes || 30);

  // ---------- setup unlock state ----------
  function paintBadge(remainingSeconds) {
    if (!lockBadge) return;
    if (remainingSeconds > 0) {
      const minutes = Math.max(1, Math.round(remainingSeconds / 60));
      lockBadge.classList.add('is-unlocked');
      lockBadge.classList.remove('is-locked');
      lockBadge.textContent = `🔓 Unlocked · ${minutes}m left · Lock now`;
      lockBadge.title = 'Removals are unlocked. Click to lock now.';
    } else if (lockBadge.classList.contains('is-unlocked')) {
      // The server owns the truth; reload so the nav renders the locked state.
      window.location.reload();
    }
  }

  async function refreshStatus() {
    try {
      const response = await fetch('/reauth/status', {
        cache: 'no-store',
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' },
      });
      const status = await response.json().catch(() => ({}));
      if (!response.ok || !status.ok) return;
      unlocked = Boolean(status.unlocked);
      windowMinutes = Math.round((status.window_seconds || 1800) / 60);
      paintBadge(status.remaining_seconds || 0);
    } catch (_error) {
      // Offline or hub restarting: keep the last known state.
    }
  }

  // ---------- keep typed form data across a lock ----------
  function snapshotForm(form) {
    if (form.querySelector('input[type="file"]')) return;
    try {
      const values = [];
      form.querySelectorAll('input, select, textarea').forEach((field) => {
        if (!field.name || field.type === 'file') return;
        if (field.type === 'checkbox' || field.type === 'radio') values.push([field.name, field.checked ? 'on' : 'off']);
        else values.push([field.name, field.value]);
      });
      window.sessionStorage.setItem(SNAPSHOT_KEY, JSON.stringify({ action: form.action, values }));
    } catch (_error) { /* private mode: skip the convenience restore */ }
  }

  function restoreSnapshot() {
    let snapshot = null;
    try {
      const raw = window.sessionStorage.getItem(SNAPSHOT_KEY);
      window.sessionStorage.removeItem(SNAPSHOT_KEY);
      snapshot = raw ? JSON.parse(raw) : null;
    } catch (_error) { snapshot = null; }
    if (!snapshot || !snapshot.action) return false;

    const form = Array.from(document.forms).find(
      (candidate) => candidate.action === snapshot.action
        || candidate.getAttribute('action') === snapshot.action
    );
    if (!form) return false;
    snapshot.values.forEach(([name, value]) => {
      const fields = form.querySelectorAll(`[name="${CSS.escape(name)}"]`);
      fields.forEach((field) => {
        if (field.type === 'checkbox' || field.type === 'radio') field.checked = value === 'on';
        else if (field.type !== 'file') field.value = value === 'off' ? '' : value;
      });
    });
    form.classList.add('restored-form');
    form.scrollIntoView({ block: 'center', behavior: 'smooth' });
    return true;
  }

  // ---------- submit interception ----------
  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;

    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }

    if (!form.matches('[data-require-reauth]')) return;
    if (form.dataset.reauthPassed === 'true') {
      delete form.dataset.reauthPassed;
      return;
    }
    if (unlocked) {
      // Server-side window is still open, so submit straight away. Keep a
      // snapshot in case the window expires in flight.
      snapshotForm(form);
      return;
    }

    event.preventDefault();
    pendingForm = form;
    pendingSubmitter = event.submitter || null;
    snapshotForm(form);
    errorBox.textContent = '';
    passwordInput.value = '';
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else {
      // Safe compatibility fallback for older mobile browsers.
      const password = window.prompt('Enter the dashboard password to confirm this removal');
      if (password) verifyPassword(password);
    }
    if (dialog.open) window.setTimeout(() => passwordInput.focus(), 0);
  }, true);

  async function verifyPassword(password) {
    if (isVerifying) return;
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

      unlocked = true;
      passwordInput.value = '';
      errorBox.textContent = '';
      if (dialog.open) dialog.close();
      paintBadge(result.remaining_seconds || windowMinutes * 60);

      const form = pendingForm;
      const submitter = pendingSubmitter;
      pendingForm = null;
      pendingSubmitter = null;
      if (form) {
        form.dataset.reauthPassed = 'true';
        if (submitter && submitter.form === form) form.requestSubmit(submitter);
        else form.requestSubmit();
      } else if (window.location.search.includes('reauth=1')) {
        restoreSnapshot();
        const url = new URL(window.location.href);
        url.searchParams.delete('reauth');
        window.history.replaceState({}, '', url);
      }
    } catch (_error) {
      errorBox.textContent = 'Could not confirm right now. Check your connection and try again.';
    } finally {
      isVerifying = false;
      confirmButton.disabled = false;
    }
  }

  function openStandaloneUnlock(message) {
    pendingForm = null;
    pendingSubmitter = null;
    errorBox.textContent = message || '';
    passwordInput.value = '';
    if (typeof dialog.showModal === 'function') dialog.showModal();
    if (dialog.open) window.setTimeout(() => passwordInput.focus(), 0);
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

  document.querySelectorAll('[data-unlock-trigger]').forEach((button) => {
    button.addEventListener('click', () => openStandaloneUnlock(''));
  });

  refreshStatus();
  window.setInterval(refreshStatus, 60000);

  if (window.location.search.includes('reauth=1')) {
    const restored = restoreSnapshot();
    openStandaloneUnlock(
      restored
        ? 'The removal was locked, so nothing was deleted. Your details were restored — confirm the password, then press the button again.'
        : 'The removal was locked, so nothing was deleted. Confirm the password once to unlock removals.'
    );
  }
})();

// Easy Setup: the routing table follows the switches as they are toggled, so
// the operator sees the outcome before saving anything.
(function () {
  const form = document.getElementById('easy-setup-form');
  const target = document.getElementById('routing-preview');
  if (!form || !target) return;

  let inflight = null;

  function buildQuery() {
    const data = new FormData(form);
    const params = new URLSearchParams();
    ['amazon_tag', 'hypd_store_id', 'inf_id'].forEach((name) => {
      const value = String(data.get(name) || '').trim();
      if (value) params.set(name, value);
    });
    ['allow_amazon', 'allow_earnkaro', 'allow_hypd', 'only_amazon'].forEach((name) => {
      params.set(name, form.querySelector(`[name="${name}"]`)?.checked ? '1' : '0');
    });
    return params.toString();
  }

  async function refresh() {
    if (inflight) return;
    inflight = true;
    try {
      const response = await fetch(`/api/routing-preview?${buildQuery()}`, {
        cache: 'no-store',
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' },
      });
      const payload = await response.json().catch(() => ({}));
      if (response.ok && payload.ok && payload.preview) {
        target.innerHTML = renderPreview(payload.preview);
      }
    } catch (_error) {
      // Keep the server-rendered table when the hub is unreachable.
    } finally {
      inflight = false;
    }
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (character) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]
    ));
  }

  function renderPreview(preview) {
    const rows = preview.rows.map((row) => `
      <tr class="routing-row is-${escapeHtml(row.state)}">
        <td><strong>${escapeHtml(row.label)}</strong><code>${escapeHtml(row.sample)}</code></td>
        <td>${row.result ? `<code class="routing-result">${escapeHtml(row.result)}</code>` : ''}
          <span class="routing-note">${escapeHtml(row.note)}</span></td>
      </tr>`).join('');
    return `
      <p class="routing-summary"><strong>${escapeHtml(preview.summary)}</strong></p>
      ${preview.strict ? '<p class="routing-note">🔥 Strict mode is on: only Amazon deals are posted, with this creator\'s tag.</p>' : ''}
      <table class="routing-table">
        <thead><tr><th>Link found in a source deal</th><th>What your channel posts</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      ${preview.earnkaro_on && !preview.earnkaro_ready
        ? '<p class="routing-note is-warn">💰 EarnKaro is on but its API key is missing. Add it in Vault &amp; Sources to convert Flipkart/Myntra/Ajio links.</p>'
        : ''}`;
  }

  form.querySelectorAll('[data-preview-input]').forEach((field) => {
    field.addEventListener('change', refresh);
    field.addEventListener('input', refresh);
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
