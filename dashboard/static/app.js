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
