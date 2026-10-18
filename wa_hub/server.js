// server.js — HTTP API + SSE for the WhatsApp hub.
//
// Endpoints
//   GET  /health
//   GET  /sessions
//   POST /sessions                { key, influencer_id, label }  -> starts a session
//   GET  /sessions/:key           -> { status, phone, hasQR }
//   GET  /sessions/:key/qr        -> { qr }  (latest QR string; null if connected)
//   GET  /sessions/:key/qr/stream -> SSE: pushes {type:'qr',qr} then {type:'status',...}
//   POST /sessions/:key/pairing-code { phone } -> { code } (link by phone number, no QR)
//   GET  /sessions/:key/pairing   -> { status, phone, hasCode, code } (read-only)
//   POST /sessions/:key/send      { to, text }
//   POST /sessions/:key/poll      { to, question, options, allow_multiple } (groups only)
//   POST /sessions/:key/group     { subject, participant }
//   GET  /sessions/:key/chats           — every chat the account knows
//   GET  /sessions/:key/admin-chats     — groups/channels where this number is admin
//   POST /sessions/:key/newsletter { name, description }
//   POST /sessions/:key/resolve-newsletter { invite }
//   POST /sessions/:key/stop
//
// Auth: all routes require `Authorization: Bearer <token>` when configured;
// production requires a strong token and loopback-only binding.

import express from 'express'
import QRCode from 'qrcode'
import { hub } from './wa_manager.js'
import { bearerTokenMatches, isValidSessionKey, validateServerConfig } from './server_security.js'

let serverConfig
try {
  serverConfig = validateServerConfig(process.env)
} catch (error) {
  console.error(`[wa-hub] ${error.message}`)
  process.exit(1)
}

const { TOKEN, PORT, HOST } = {
  TOKEN: serverConfig.token,
  PORT: serverConfig.port,
  HOST: serverConfig.host,
}

const app = express()
app.disable('x-powered-by')
app.use(express.json({ limit: '64kb', strict: true }))
app.use((_req, res, next) => {
  // QR codes and WhatsApp phone-link codes are short-lived login credentials.
  res.set('Cache-Control', 'no-store, private')
  res.set('X-Content-Type-Options', 'nosniff')
  next()
})

function auth(req, res, next) {
  // An empty token is permitted only on a loopback binding, enforced before listen.
  if (!TOKEN || bearerTokenMatches(req.headers.authorization || '', TOKEN)) return next()
  return res.status(401).json({ ok: false, error: 'unauthorized' })
}
app.use(auth)
app.param('key', (req, res, next, key) => {
  if (!isValidSessionKey(key)) {
    return res.status(400).json({ ok: false, error: 'invalid session key' })
  }
  return next()
})

app.get('/health', (_req, res) => res.json({ ok: true, sessions: hub.list().length }))

app.get('/sessions', (_req, res) => res.json({ sessions: hub.list() }))

app.post('/sessions', async (req, res) => {
  const { key, influencer_id, label } = req.body || {}
  if (!isValidSessionKey(key)) {
    return res.status(400).json({ ok: false, error: 'key must be a non-empty string of at most 128 characters' })
  }
  if (influencer_id !== undefined && (!Number.isSafeInteger(Number(influencer_id)) || Number(influencer_id) < 1)) {
    return res.status(400).json({ ok: false, error: 'influencer_id must be a positive integer' })
  }
  if (label !== undefined && (typeof label !== 'string' || label.length > 120)) {
    return res.status(400).json({ ok: false, error: 'label must be a string of at most 120 characters' })
  }
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
  if (!qr) return res.json({ ok: true, qr: null, status: s.status, phone: s.phone })
  const dataUrl = await QRCode.toDataURL(qr, { width: 320 })
  res.json({ ok: true, qr: dataUrl, status: s.status, phone: s.phone })
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

// Phone-number linking: same result as scanning the QR, but the operator reads
// an 8-character code on the influencer's phone (Linked devices -> Link with
// phone number). Read-only GET so the dashboard can re-display a live code.
app.post('/sessions/:key/pairing-code', async (req, res) => {
  const { phone, influencer_id, label } = req.body || {}
  if (!phone) return res.status(400).json({ ok: false, error: 'phone required' })
  try {
    const result = await hub.requestPairingCode(
      req.params.key, phone, { influencer_id, label },
    )
    res.json(result)
  } catch (e) {
    const status = Number.isInteger(e?.status) ? e.status : 422
    res.status(status).json({ ok: false, error: String(e?.message || e), code: '' })
  }
})

app.get('/sessions/:key/pairing', (req, res) => {
  res.json(hub.pairingState(req.params.key))
})

app.post('/sessions/:key/resolve-invite', async (req, res) => {
  const { invite } = req.body || {}
  if (!invite) return res.status(400).json({ ok: false, error: 'invite required' })
  try {
    const r = await hub.resolveInviteCode(req.params.key, invite)
    if (!r?.ok || !r?.jid) return res.status(422).json(r)
    res.json(r)
  } catch (e) {
    res.status(422).json({ ok: false, error: String(e) })
  }
})

app.post('/sessions/:key/resolve-newsletter', async (req, res) => {
  const { invite } = req.body || {}
  if (!invite) return res.status(400).json({ ok: false, error: 'invite required' })
  try {
    const r = await hub.resolveNewsletterInvite(req.params.key, invite)
    if (!r?.ok || !r?.jid) return res.status(422).json(r)
    res.json(r)
  } catch (e) {
    res.status(422).json({ ok: false, error: String(e) })
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

app.get('/sessions/:key/admin-chats', async (req, res) => {
  try {
    const chats = await hub.listAdminChats(req.params.key)
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

app.post('/sessions/:key/poll', async (req, res) => {
  const { to, question, options, allow_multiple } = req.body || {}
  if (!to || !question || !Array.isArray(options)) {
    return res.status(400).json({ ok: false, error: 'to, question, and options are required' })
  }
  if (typeof to !== 'string' || !to.endsWith('@g.us')) {
    return res.status(422).json({ ok: false, error: 'WhatsApp polls are supported only in groups' })
  }
  try {
    const r = await hub.sendPoll(req.params.key, to, question, options, allow_multiple)
    if (!r?.ok) return res.status(422).json(r || { ok: false, error: 'WhatsApp rejected the poll' })
    res.json(r)
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

// Avoid Express's default HTML error response for malformed or oversized JSON.
app.use((error, _req, res, next) => {
  if (res.headersSent) return next(error)
  const status = error?.type === 'entity.too.large' ? 413 : 400
  if (status === 400) console.warn('[wa-hub] rejected malformed request body')
  return res.status(status).json({
    ok: false,
    error: status === 413 ? 'request body too large' : 'invalid request body',
  })
})

app.listen(PORT, HOST, () => {
  console.log(`[wa-hub] listening on ${HOST}:${PORT}`)
  // Auto-resume any previously paired sessions.
  for (const s of hub.list()) {
    const saved = hub.get(s.key)
    hub.start(s.key, saved?.meta || {}).catch((e) => console.error('resume failed', s.key, e))
  }
})
