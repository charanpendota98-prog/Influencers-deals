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

// Phone-linking (OTP) pairing: show the live 8-character code WhatsApp puts on
// the influencer's phone, without ever starting a session from the browser.
// GET-only polling; the dashboard proxies the loopback endpoints.
(function () {
  const box = document.getElementById('pairbox');
  if (!box) return;
  const key = box.dataset.key;
  if (!key) return;

  function paint(code, phone, status) {
    box.replaceChildren();
    if (code) {
      const card = document.createElement('div');
      card.style.cssText = 'background:#052e16; border:1px solid #22c55e; padding:10px 12px; border-radius:8px; text-align:center;';
      const hint = document.createElement('div');
      hint.className = 'hint';
      hint.style.cssText = 'font-size:11px; margin:0;';
      hint.textContent = `WhatsApp → Linked devices → Link with phone number (${phone || 'this phone'}):`;
      const value = document.createElement('div');
      value.style.cssText = 'font-size:26px; font-weight:900; letter-spacing:4px; color:#4ade80; font-family:monospace;';
      value.textContent = code;
      const note = document.createElement('div');
      note.className = 'hint';
      note.style.cssText = 'font-size:11px; margin-top:4px;';
      note.textContent = 'Type this code on the influencer phone. It is valid while this page shows it.';
      card.append(hint, value, note);
      box.appendChild(card);
      return;
    }
    if (status === 'connected') {
      const done = document.createElement('div');
      done.style.cssText = 'background:#064e3b; color:#86efac; padding:8px 12px; border-radius:6px; font-weight:bold; font-size:13px;';
      done.textContent = '✅ Phone linked — the code is no longer needed.';
      box.appendChild(done);
      return;
    }
    if (status === 'pairing') {
      const waiting = document.createElement('p');
      waiting.className = 'hint';
      waiting.style.cssText = 'font-size:11px; margin:0;';
      waiting.textContent = 'Pairing code requested — WhatsApp is answering. Keep this page open.';
      box.appendChild(waiting);
    }
  }

  let stopped = false;
  async function tick() {
    if (stopped) return;
    let nextPollMs = 3000;
    try {
      const response = await fetch(`/api/wa/${encodeURIComponent(key)}/pairing`, {
        cache: 'no-store',
        credentials: 'same-origin',
      });
      const state = await response.json();
      if (state && state.ok) {
        if (state.hasCode && state.code) {
          paint(state.code, state.phone, state.status);
        } else if (state.status === 'connected') {
          paint(null, state.phone, 'connected');
          stopped = true;
          return;
        } else if (state.status === 'pairing') {
          paint(null, state.phone, 'pairing');
        }
      }
      if (state && state.error) {
        const error = document.createElement('p');
        error.className = 'hint';
        error.style.cssText = 'color:#fbbf24; font-size:11px; margin:0;';
        error.textContent = `⚠ ${state.error}`;
        box.replaceChildren(error);
      }
    } catch (_error) {
      // Hub restarting or unavailable: keep whatever is on screen.
      nextPollMs = 5000;
    }
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
    const shortLinks = preview.short_links || {};
    let proof = '';
    if (preview.tag_proof) {
      if (shortLinks.active_for_this_channel) {
        proof = `<p class="routing-note${preview.tag_usable ? '' : ' is-warn'}">Canonical Amazon target: <code class="routing-result">${escapeHtml(preview.tag_proof)}</code> ${preview.tag_usable ? '✅ with the tag you gave' : '⚠ check this tag'}<br><strong>Compact branded links are ON:</strong> broadcast/WhatsApp posts use <code class="routing-result">${escapeHtml(shortLinks.base_url)}/a/&lt;code&gt;?tag=${escapeHtml(preview.tag)}</code>. Save the channel or use Test a Deal to mint the exact clickable code; it redirects only to the target above.</p>`;
      } else if (shortLinks.native_for_approval) {
        proof = `<p class="routing-note">Approval posts stay native by design: <code class="routing-result">${escapeHtml(preview.tag_proof)}</code> ✅ with the tag you gave.</p>`;
      } else {
        proof = `<p class="routing-note${preview.tag_usable ? '' : ' is-warn'}">Amazon deals will post this canonical link: <code class="routing-result">${escapeHtml(preview.tag_proof)}</code> ${preview.tag_usable ? `✅ with the tag you gave. ${escapeHtml(shortLinks.reason || '')}` : '⚠ this tag will be replaced by the configured fallback — check it'}</p>`;
      }
    }
    return `
      <p class="routing-summary"><strong>${escapeHtml(preview.summary)}</strong></p>
      ${proof}
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

/* --- Setup command center: live readiness (no reload, read-only) ----------
   The panel is server-rendered first, so this is pure enhancement: it fetches
   the same checklist JSON with `live=1`, which lets the dashboard ask the
   WhatsApp hub for the real session state, and patches the cells that change
   most often (phone link, WhatsApp live, worker, progress). */
(() => {
  const panel = document.getElementById('command-center');
  if (!panel) return;
  const src = panel.dataset.liveSrc;
  if (!src) return;
  let timer = null;
  let failures = 0;

  const setText = (selector, value) => {
    const node = panel.querySelector(selector);
    if (node && value !== undefined && value !== null) node.textContent = value;
  };

  function paint(payload) {
    const summary = payload.summary || {};
    const globals = payload.globals || {};
    const bar = panel.querySelector('.command-bar > span');
    if (bar) bar.style.width = `${summary.percent ?? 0}%`;
    setText('.command-headline strong', `${summary.percent ?? 0}%`);
    setText('[data-live-worker]',
      `${globals.worker && globals.worker.done ? '🟢' : '🔴'} worker ${globals.worker && globals.worker.done ? 'running' : 'not running'}`);
    const chips = panel.querySelectorAll('.command-chip');
    if (chips[0] && summary.ready !== undefined) chips[0].textContent = `👥 ${summary.ready} ready to post`;
    if (chips[1] && summary.blocked !== undefined) {
      chips[1].textContent = `⚠️ ${summary.blocked} still blocked`;
      chips[1].className = `command-chip ${summary.blocked ? 'is-warn' : 'is-ok'}`;
    }
    if (chips[2] && summary.mirroring !== undefined) chips[2].textContent = `🔁 ${summary.mirroring} mirroring to WhatsApp`;
    (payload.creators || []).forEach((creator) => {
      const steps = {};
      (creator.steps || []).forEach((step) => { steps[step.id] = step; });
      const phone = panel.querySelector(`[data-live-cell="phone-${creator.id}"]`);
      if (phone && steps.phone) {
        phone.textContent = steps.phone.done
          ? '✅ linked'
          : (creator.wa_status && creator.wa_status !== 'not_paired' ? `⛔ ${creator.wa_status}` : '➡️ not linked');
        phone.className = `command-step is-${steps.phone.state}`;
        phone.title = steps.phone.detail || '';
      }
      const live = panel.querySelector(`[data-live-cell="live-${creator.id}"]`);
      if (live && steps.activate) {
        live.textContent = steps.activate.done ? '🟢 live' : '· pending test';
        live.className = `command-step is-${steps.activate.state}`;
        live.title = steps.activate.detail || '';
      }
    });
    const stamp = panel.querySelector('[data-live-updated]');
    if (stamp) {
      stamp.textContent = payload.live_error
        ? `hub unreachable · saved state`
        : `checked ${String(payload.generated_at || '').replace('T', ' ').replace('+00:00', 'Z')}`;
    }
  }

  async function refresh() {
    try {
      const response = await fetch(src, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' });
      if (response.status === 401) return;
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) throw new Error('readiness refresh failed');
      failures = 0;
      paint(payload);
    } catch (error) {
      failures += 1;
    }
    schedule();
  }

  function schedule() {
    clearTimeout(timer);
    const delay = failures ? Math.min(120000, 20000 * failures) : 20000;
    timer = setTimeout(() => {
      if (document.visibilityState === 'hidden') { schedule(); return; }
      refresh();
    }, delay);
  }

  schedule();
})();

/* --- Easy Setup stepper: keep the "what is done / next" track live --------
   Read-only poll of the same checklist endpoint for the creator on screen. */
(() => {
  const panel = document.querySelector('[data-live-src][data-creator]');
  if (!panel || panel.id !== 'next-steps') return;
  const creator = panel.dataset.creator;
  const src = `${panel.dataset.liveSrc}&inf_id=${encodeURIComponent(creator)}`;
  let failures = 0;

  function paint(payload) {
    const creator = (payload.creators || [])[0];
    if (!creator) return;
    (creator.steps || []).forEach((step) => {
      const dot = panel.querySelector(`[data-live-step="${step.id}"]`);
      if (!dot) return;
      const mark = step.done ? '✅' : (step.state === 'blocked' ? '⛔' : (step.state === 'next' ? '➡️' : '·'));
      dot.className = `step-dot is-${step.state}`;
      dot.title = `${step.label} — ${step.detail}`;
      dot.textContent = `${mark} ${step.label}`;
    });
    const next = panel.querySelector('.next-action');
    if (next && creator.next_step) {
      const heading = next.querySelector('strong');
      const copy = next.querySelector('p');
      if (heading) heading.textContent = `🎯 Next: ${creator.next_step.label}`;
      if (copy) copy.textContent = creator.next_step.action || creator.next_step.detail;
    }
  }

  async function tick() {
    try {
      const response = await fetch(src, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' });
      if (response.status === 401) return;
      const payload = await response.json().catch(() => ({}));
      if (response.ok && payload.ok) { failures = 0; paint(payload); }
      else failures += 1;
    } catch (error) {
      failures += 1;
    }
    window.setTimeout(tick, failures ? Math.min(60000, 10000 * failures) : 10000);
  }
  tick();
})();

/* --- Self-check panel: re-run the read-only checks, including the hub ping -- */
(() => {
  const panel = document.getElementById('self-check');
  if (!panel) return;
  const src = panel.dataset.liveSrc;
  if (!src) return;
  const marquee = { ok: '✅', warn: '⚠️', fail: '⛔', skip: '·' };
  const verdictText = { ready: '✅ READY', attention: '⚠️ ATTENTION', fail: '⛔ PROBLEMS' };
  let failures = 0;

  function paint(payload) {
    const verdict = panel.querySelector('[data-live-verdict]');
    if (verdict) {
      verdict.textContent = verdictText[payload.verdict] || '⚠️ ATTENTION';
      verdict.className = `selfcheck-verdict is-${payload.verdict}`;
    }
    (payload.checks || []).forEach((check) => {
      const item = panel.querySelector(`.selfcheck-item[data-check="${check.id}"]`);
      if (!item) return;
      item.className = `selfcheck-item is-${check.state}`;
      item.title = `${check.detail}${check.fix ? ` — fix: ${check.fix}` : ''}`;
      const mark = item.querySelector('.selfcheck-mark');
      if (mark) mark.textContent = marquee[check.state] || '·';
      const small = item.querySelector('.selfcheck-body small');
      if (small) small.textContent = check.detail;
    });
    const fixes = panel.querySelector('[data-live-fixes]');
    if (fixes && Array.isArray(payload.next)) {
      fixes.replaceChildren(...payload.next.map((check) => {
        const li = document.createElement('li');
        li.className = `is-${check.state}`;
        const strong = document.createElement('strong');
        strong.textContent = `${check.label}: `;
        li.append(strong, document.createTextNode(check.detail));
        if (check.fix) {
          const span = document.createElement('span');
          span.className = 'routing-note';
          span.textContent = check.fix;
          li.appendChild(span);
        }
        return li;
      }));
    }
  }

  async function tick() {
    try {
      const response = await fetch(src, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' });
      if (response.status === 401) return;
      const payload = await response.json().catch(() => ({}));
      if (response.ok && payload.checks) { failures = 0; paint(payload); }
      else failures += 1;
    } catch (error) {
      failures += 1;
    }
    window.setTimeout(tick, failures ? Math.min(180000, 30000 * failures) : 30000);
  }
  window.setTimeout(tick, 3000);
})();

/* --- Ops digest: keep the 24h numbers current without a reload ------------- */
(() => {
  const card = document.getElementById('ops-digest');
  if (!card) return;
  const src = card.dataset.liveSrc;
  if (!src) return;
  let failures = 0;
  const set = (key, value) => {
    const node = card.querySelector(`[data-live-ops="${key}"]`);
    if (node && value !== undefined && value !== null) node.textContent = value;
  };
  const note = (key, value) => {
    const node = card.querySelector(`[data-live-ops-note="${key}"]`);
    if (node && value !== undefined && value !== null) node.textContent = value;
  };
  async function tick() {
    try {
      const response = await fetch(src, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' });
      if (response.status === 401) return;
      const data = await response.json().catch(() => ({}));
      if (response.ok && data.ok) {
        failures = 0;
        set('posted', data.posts.posted);
        set('failed', data.posts.failed);
        set('retry', data.retry_queue);
        set('worker', `${['running', 'idle', 'polling', 'healthy'].includes(data.worker.state) ? '🟢' : '🔴'} ${data.worker.state}`);
        set('ready', `${data.creators.ready} / ${data.creators.total}`);
        set('percent', `${data.creators.setup_percent}%`);
        const bad = card.querySelector('[data-live-ops="failed"]');
        if (bad) bad.className = data.posts.failed ? 'is-bad' : '';
        const rate = data.delivery_rate_pct === null || data.delivery_rate_pct === undefined ? '\u2014' : data.delivery_rate_pct;
        note('posted', `delivery ${rate}% \u00b7 ${data.posts.mirrored} mirrored`);
        note('failed', `${data.posts.skipped} skipped \u00b7 ${data.posts.attempts} attempts`);
        note('worker', `sources ${data.sources.active}/${data.sources.total} active`);
        note('ready', `${data.creators.blocked} blocked \u00b7 ${data.creators.mirroring} mirroring`);
        const failures = (data.posts.top_failures || []).map(f => `${f.count}\u00d7 ${f.reason}`).join(' \u00b7 ');
        note('failures', failures);
        const failuresRow = card.querySelector('[data-live-ops="failures"]');
        if (failuresRow) failuresRow.style.display = failures ? '' : 'none';
      } else failures += 1;
    } catch (error) {
      failures += 1;
    }
    window.setTimeout(tick, failures ? Math.min(120000, 30000 * failures) : 30000);
  }
  tick();
})();
