// server.js — HTTP API + SSE for the WhatsApp hub.
//
// Endpoints
//   GET  /health
//   GET  /sessions
//   POST /sessions                { key, influencer_id, label }  -> starts a session
//   GET  /sessions/:key           -> { status, phone, hasQR }
//   GET  /sessions/:key/qr        -> { qr }  (latest QR string; null if connected)
//   GET  /sessions/:key/qr/stream -> SSE: pushes {type:'qr',qr} then {type:'status',...}
//   POST /sessions/:key/send      { to, text }
//   POST /sessions/:key/group     { subject, participant }
//   POST /sessions/:key/newsletter { name, description }
//   POST /sessions/:key/stop
//
// Auth: if WA_HUB_TOKEN is set, all routes require `Authorization: Bearer <token>`.

import express from 'express'
import QRCode from 'qrcode'
import { hub } from './wa_manager.js'

const app = express()
app.use(express.json())

const TOKEN = process.env.WA_HUB_TOKEN || ''
const PORT = parseInt(process.env.WA_HUB_PORT || '8088', 10)

function auth(req, res, next) {
  if (!TOKEN) return next()
  const h = req.headers['authorization'] || ''
  if (h === `Bearer ${TOKEN}`) return next()
  return res.status(401).json({ ok: false, error: 'unauthorized' })
}
app.use(auth)

app.get('/health', (_req, res) => res.json({ ok: true, sessions: hub.list().length }))

app.get('/sessions', (_req, res) => res.json({ sessions: hub.list() }))

app.post('/sessions', async (req, res) => {
  const { key, influencer_id, label } = req.body || {}
  if (!key) return res.status(400).json({ ok: false, error: 'key required' })
  try {
    const sess = await hub.start(key, { influencer_id, label })
    res.json({ ok: true, key, status: sess.status })
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) })
  }
})

app.get('/sessions/:key', (req, res) => {
  const s = hub.get(req.params.key)
  if (!s) return res.status(404).json({ ok: false, error: 'no such session' })
  res.json({ ok: true, status: s.status, phone: s.phone, hasQR: !!s.qr })
})

app.get('/sessions/:key/qr', async (req, res) => {
  const s = hub.get(req.params.key)
  if (!s) return res.status(404).json({ ok: false, error: 'no such session' })
  const qr = s.qr
  if (!qr) return res.json({ ok: true, qr: null, status: s.status })
  const dataUrl = await QRCode.toDataURL(qr, { width: 320 })
  res.json({ ok: true, qr: dataUrl, status: s.status })
})

// Server-Sent Events: live QR + status for the dashboard.
app.get('/sessions/:key/qr/stream', async (req, res) => {
  const s = hub.get(req.params.key)
  if (!s) return res.status(404).end()
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
  })
  const send = (obj) => res.write(`data: ${JSON.stringify(obj)}\n\n`)
  if (s.qr) send({ type: 'qr', qr: await QRCode.toDataURL(s.qr, { width: 320 }) })
  send({ type: 'status', status: s.status, phone: s.phone })
  const unsub = s.addListener(async (ev) => {
    if (ev.type === 'qr') {
      send({ type: 'qr', qr: await QRCode.toDataURL(ev.qr, { width: 320 }) })
    } else {
      send(ev)
    }
  })
  req.on('close', unsub)
})

app.post('/sessions/:key/resolve-invite', async (req, res) => {
  const { invite } = req.body || {}
  if (!invite) return res.status(400).json({ ok: false, error: 'invite required' })
  try {
    const r = await hub.resolveInviteCode(req.params.key, invite)
    res.json(r)
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) })
  }
})

app.get('/sessions/:key/chats', (req, res) => {
  try {
    const chats = hub.listChats(req.params.key)
    res.json({ ok: true, chats })
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) })
  }
})

app.post('/sessions/:key/send', async (req, res) => {
  const { to, text } = req.body || {}
  if (!to || !text) return res.status(400).json({ ok: false, error: 'to+text required' })
  try {
    const r = await hub.sendText(req.params.key, to, text)
    res.json({ ok: true, ...r })
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) })
  }
})

app.post('/sessions/:key/group', async (req, res) => {
  const { subject, participant } = req.body || {}
  if (!subject || !participant) return res.status(400).json({ ok: false, error: 'subject+participant required' })
  try {
    const r = await hub.createGroup(req.params.key, subject, participant)
    res.json({ ok: true, ...r })
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) })
  }
})

app.post('/sessions/:key/newsletter', async (req, res) => {
  const { name, description } = req.body || {}
  if (!name) return res.status(400).json({ ok: false, error: 'name required' })
  try {
    const r = await hub.createNewsletter(req.params.key, name, description || '')
    res.json({ ok: true, ...r })
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) })
  }
})

app.post('/sessions/:key/stop', async (req, res) => {
  await hub.stop(req.params.key)
  res.json({ ok: true })
})

app.listen(PORT, '0.0.0.0', () => {
  console.log(`[wa-hub] listening on 0.0.0.0:${PORT}`)
  // Auto-resume any previously paired sessions.
  for (const s of hub.list()) {
    hub.start(s.key, s.meta).catch((e) => console.error('resume failed', s.key, e))
  }
})
