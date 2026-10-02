// wa_manager.js — multi-session WhatsApp Web hub built on baileys.
//
// One session per influencer phone number. Each session keeps its own auth
// state on disk so it survives restarts without re-scanning the QR. The Python
// side talks to this service over a tiny REST/SSE API (see server.js).
//
// WhatsApp Channels (newsletters): the pinned Baileys build has a low-level
// text-send path for @newsletter JIDs. Channel-link resolution and a visible
// test post are required before a destination is activated. This is an
// unofficial WhatsApp-Web integration; never promise delivery or account safety.

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import pino from 'pino'
import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  makeCacheableSignalKeyStore,
} from 'baileys'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const AUTH_ROOT = path.join(__dirname, 'auth')
const SESSIONS_FILE = path.join(__dirname, 'sessions.json')

const logger = pino({ level: process.env.WA_LOG_LEVEL || 'info' })

class Session {
  constructor(key, meta = {}, authRoot = AUTH_ROOT) {
    this.key = key
    this.meta = meta
    this.sock = null
    this.startPromise = null
    this.stopping = false
    this.qr = null
    this.status = 'offline' // offline | qr | connecting | connected | error
    this.phone = ''
    this.listeners = new Set() // SSE subscribers
    this.authDir = path.join(authRoot, sanitize(key))
  }

  emit(event) {
    for (const fn of this.listeners) {
      try { fn(event) } catch (_) { /* ignore broken sink */ }
    }
  }

  addListener(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn) }
}

function sanitize(key) {
  return key.replace(/[^a-zA-Z0-9_-]/g, '_')
}

export class Hub {
  constructor({
    authRoot = AUTH_ROOT,
    sessionsFile = SESSIONS_FILE,
    useAuthState = useMultiFileAuthState,
    makeSocket = makeWASocket,
    cacheSignalKeyStore = makeCacheableSignalKeyStore,
    schedule = setTimeout,
    sessionLogger = logger,
  } = {}) {
    this.authRoot = authRoot
    this.sessionsFile = sessionsFile
    this.useAuthState = useAuthState
    this.makeSocket = makeSocket
    this.cacheSignalKeyStore = cacheSignalKeyStore
    this.schedule = schedule
    this.logger = sessionLogger
    this.sessions = new Map()
    fs.mkdirSync(this.authRoot, { recursive: true, mode: 0o700 })
    this._load()
  }

  _load() {
    try {
      const saved = JSON.parse(fs.readFileSync(this.sessionsFile, 'utf8'))
      for (const s of saved) {
        if (!s?.key) continue
        // The session identity persists; connection status is re-read from the
        // live socket after its saved Baileys credentials are resumed.
        const sess = new Session(s.key, s.meta || {}, this.authRoot)
        this.sessions.set(s.key, sess)
      }
    } catch (_) { /* no saved sessions yet */ }
  }

  _save() {
    const data = [...this.sessions.values()].map(s => ({ key: s.key, meta: s.meta }))
    fs.writeFileSync(this.sessionsFile, JSON.stringify(data, null, 2), { mode: 0o600 })
  }

  list() {
    return [...this.sessions.values()].map(s => ({
      key: s.key,
      influencer_id: s.meta?.influencer_id,
      label: s.meta?.label,
      status: s.status,
      phone: s.phone,
    }))
  }

  get(key) { return this.sessions.get(key) }

  async start(key, meta = {}) {
    const sess = this.sessions.get(key) || new Session(key, meta, this.authRoot)
    sess.meta = { ...sess.meta, ...meta }
    this.sessions.set(key, sess)
    if (sess.sock) return sess
    if (sess.startPromise) return sess.startPromise

    sess.stopping = false
    sess.status = 'connecting'
    this._save()
    const startPromise = this._openSocket(sess)
    sess.startPromise = startPromise
    try {
      return await startPromise
    } catch (error) {
      if (this.sessions.get(key) === sess && !sess.sock) sess.status = 'offline'
      throw error
    } finally {
      if (sess.startPromise === startPromise) sess.startPromise = null
    }
  }

  async _openSocket(sess) {
    const { state, saveCreds } = await this.useAuthState(sess.authDir)
    if (this.sessions.get(sess.key) !== sess || sess.stopping) return sess

    const sock = this.makeSocket({
      auth: {
        creds: state.creds,
        keys: this.cacheSignalKeyStore(state.keys, this.logger),
      },
      logger: this.logger,
      printQRInTerminal: false,
      connectTimeoutMs: 60_000,
    })
    sess.sock = sock

    sock.ev.on('connection.update', (u) => {
      // Ignore late events from a socket replaced during reconnect or logout.
      if (this.sessions.get(sess.key) !== sess || sess.sock !== sock) return
      const { connection, qr, lastDisconnect } = u
      if (qr) {
        sess.qr = qr
        sess.status = 'qr'
        sess.emit({ type: 'qr', qr })
        this.logger.info({ key: sess.key }, 'QR available')
      }
      if (connection === 'connecting') sess.status = 'connecting'
      if (connection === 'open') {
        sess.status = 'connected'
        sess.phone = sock.user?.id?.split('@')[0] || ''
        sess.qr = null
        sess.emit({ type: 'status', status: 'connected', phone: sess.phone })
        this.logger.info({ key: sess.key, phone: sess.phone }, 'session connected')
      }
      if (connection === 'close') {
        const reason = lastDisconnect?.error?.output?.statusCode
        const loggedOut = reason === DisconnectReason.loggedOut
        sess.sock = null
        sess.qr = null
        sess.status = 'offline'
        if (loggedOut) {
          sess.phone = ''
          try {
            fs.rmSync(sess.authDir, { recursive: true, force: true })
          } catch (error) {
            this.logger.warn({ key: sess.key, error }, 'could not clear logged-out auth state')
          }
        }
        const shouldReconnect = !sess.stopping && !loggedOut
        sess.emit({ type: 'status', status: 'offline' })
        this.logger.warn({ key: sess.key, reason }, 'session closed')
        if (shouldReconnect) {
          // Clear the closed socket before retrying so start() can really create
          // a replacement socket instead of returning the stale one.
          this.schedule(() => {
            if (this.sessions.get(sess.key) !== sess || sess.stopping || sess.sock || sess.startPromise) return
            return this.start(sess.key, sess.meta).catch((error) => {
              this.logger.warn({ key: sess.key, error }, 'session reconnect failed')
            })
          }, 3000)
        }
      }
    })
    sock.ev.on('creds.update', saveCreds)

    return sess
  }

  // Latest QR string (used for polling fallback).
  getQR(key) { return this.sessions.get(key)?.qr || null }

  async sendText(key, to, text) {
    const s = this.sessions.get(key)
    if (!s?.sock) throw new Error('session not connected: ' + key)

    // Random human-like typing simulation & delay (between 1200ms and 3500ms)
    // This protects individual influencer WhatsApp accounts from anti-spam algorithms!
    try {
      await s.sock.sendPresenceUpdate('composing', to)
      const typingDelay = Math.floor(Math.random() * 2000) + 1200
      await new Promise(resolve => setTimeout(resolve, typingDelay))
      await s.sock.sendPresenceUpdate('paused', to)
    } catch (_) { /* presence update is best effort */ }

    const res = await s.sock.sendMessage(to, { text })
    return { ok: true, id: res?.key?.id }
  }

  async createGroup(key, subject, participant) {
    const s = this.sessions.get(key)
    if (!s?.sock) throw new Error('session not connected: ' + key)
    const res = await s.sock.groupCreate(subject, [participant])
    const jid = res.id
    // Make the influencer an admin so they can manage the group too.
    try {
      await s.sock.groupParticipantsUpdate(jid, [participant], 'promote')
    } catch (_) { /* best effort */ }
    return { jid, subject }
  }

  async createNewsletter(key, name, description = '') {
    const s = this.sessions.get(key)
    if (!s?.sock) throw new Error('session not connected: ' + key)
    if (typeof s.sock.newsletterCreate !== 'function') {
      return { ok: false, note: 'newsletterCreate unavailable in this baileys build' }
    }
    const res = await s.sock.newsletterCreate({ name, description })
    const jid = res?.jid || res?.id
    try { await s.sock.newsletterFollow(jid) } catch (_) { /* best effort */ }
    return { ok: true, jid, name }
  }

  async resolveNewsletterInvite(key, inviteUrlOrCode) {
    const s = this.sessions.get(key)
    if (!s?.sock) throw new Error('session not connected: ' + key)

    const input = String(inviteUrlOrCode || '').trim()
    if (!input) throw new Error('channel link required')

    let code = ''
    if (/^\d+@newsletter$/.test(input)) {
      const metadata = await s.sock.newsletterMetadata('jid', input)
      if (!metadata?.id || !metadata.id.endsWith('@newsletter')) {
        throw new Error('could not verify WhatsApp Channel JID')
      }
      return {
        ok: true,
        jid: metadata.id,
        name: metadata.name || '',
        invite: metadata.invite || '',
      }
    }

    if (/^[0-9A-Za-z_-]{8,}$/.test(input)) {
      code = input
    } else {
      let candidate = input
      if (!/^https?:\/\//i.test(candidate)) candidate = `https://${candidate}`
      let parsed
      try { parsed = new URL(candidate) } catch (_) {
        throw new Error('invalid WhatsApp Channel link')
      }
      const host = parsed.hostname.toLowerCase()
      if (parsed.protocol !== 'https:' || !['whatsapp.com', 'www.whatsapp.com'].includes(host)) {
        throw new Error('use an HTTPS whatsapp.com/channel link')
      }
      const match = parsed.pathname.match(/^\/channel\/([0-9A-Za-z_-]+)\/?$/i)
      if (!match) throw new Error('invalid WhatsApp Channel link')
      code = match[1]
    }

    const metadata = await s.sock.newsletterMetadata('invite', code)
    if (!metadata?.id || !metadata.id.endsWith('@newsletter')) {
      throw new Error('WhatsApp did not return a Channel destination')
    }
    return {
      ok: true,
      jid: metadata.id,
      name: metadata.name || '',
      invite: metadata.invite || input,
    }
  }

  async resolveInviteCode(key, inviteUrlOrCode) {
    const s = this.sessions.get(key)
    if (!s?.sock) throw new Error('session not connected: ' + key)
    // Extract code from full link e.g. https://chat.whatsapp.com/ABC123xyz
    let code = inviteUrlOrCode.trim()
    const m = code.match(/chat\.whatsapp\.com\/([0-9A-Za-z_-]+)/i)
    if (m) code = m[1]
    code = code.replace(/[^0-9A-Za-z_-]/g, '')

    try {
      const info = await s.sock.groupGetInviteInfo(code)
      return { ok: true, jid: info.id, subject: info.subject, size: info.size }
    } catch (e) {
      return { ok: false, error: String(e), code }
    }
  }

  listChats(key) {
    const s = this.sessions.get(key)
    if (!s?.sock) throw new Error('session not connected: ' + key)
    const chats = []
    if (s.sock.chats) {
      for (const c of Object.values(s.sock.chats)) {
        chats.push({
          jid: c.id,
          name: c.name || c.subject || c.id,
          isGroup: c.id.endsWith('@g.us'),
          isChannel: c.id.endsWith('@newsletter'),
        })
      }
    }
    return chats
  }

  async stop(key) {
    const s = this.sessions.get(key)
    if (!s) return
    s.stopping = true
    if (s.startPromise) {
      try { await s.startPromise } catch (_) { /* best-effort shutdown */ }
    }

    const sock = s.sock
    s.sock = null
    s.qr = null
    s.phone = ''
    s.status = 'offline'
    if (sock) {
      try { await sock.logout() } catch (_) { /* best effort */ }
      try { sock.end() } catch (_) { /* best effort */ }
    }
    if (this.sessions.get(key) === s) this.sessions.delete(key)
    this._save()
    try {
      await fs.promises.rm(s.authDir, { recursive: true, force: true })
    } catch (error) {
      this.logger.warn({ key, error }, 'could not remove stopped session auth state')
    }
  }
}

export const hub = new Hub()
