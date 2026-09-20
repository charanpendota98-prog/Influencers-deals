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
        box.innerHTML = `<img src="${data.qr}" alt="WhatsApp QR" />`;
      } else if (data && data.status === 'connected') {
        box.innerHTML = `<p class="ok">✅ Connected as ${data.phone || ''}</p>`;
        return; // stop polling
      }
    } catch (e) {
      box.innerHTML = `<p class="hint">Waiting for wa_hub…</p>`;
    }
    setTimeout(tick, 2500);
  }
  tick();
})();
