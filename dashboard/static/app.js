// Polls the wa_hub QR endpoint and renders the live QR for the influencer's
// WhatsApp session. Falls back silently if the hub isn't running yet.
(function () {
  const box = document.getElementById('qrbox');
  if (!box) return;
  const key = box.dataset.key;
  if (!key) return;

  async function tick() {
    try {
      const res = await fetch(`/api/wa/${key}/qr`);
      const data = await res.json();
      if (data && data.qr) {
        box.innerHTML = `
          <div style="background:#fff; padding:12px; border-radius:12px; display:inline-block; box-shadow:0 10px 25px rgba(0,0,0,0.5);">
            <img src="${data.qr}" alt="Scan WhatsApp QR" style="width:240px; height:240px; display:block;" />
          </div>
          <p style="color:#86efac; font-weight:bold; font-size:13px; margin-top:10px;">📲 Influencer Phone lo WhatsApp > Linked Devices తో ఈ QR స్కాన్ చేయండి</p>
        `;
      } else if (data && data.status === 'connected') {
        box.innerHTML = `
          <div style="background:#064e3b; color:#86efac; padding:12px 18px; border-radius:8px; font-weight:bold; font-size:15px; border:1px solid #10b981;">
            ✅ WhatsApp Linked Successfully! (${data.phone || 'Connected'})
          </div>
        `;
        return; // stop polling once connected
      }
    } catch (e) {
      // Hub waiting or quiet
    }
    setTimeout(tick, 2000);
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
