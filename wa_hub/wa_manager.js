// wa_manager.js — multi-session WhatsApp Web hub built on baileys.
//
// One session per influencer phone number. Each session keeps its own auth
// state on disk so it survives restarts without re-scanning the QR. The Python
// side talks to this service over a tiny REST/SSE API (see server.js).
//
// Design note on "official WhatsApp Channels": baileys can CREATE a newsletter
// (channel) on the connected number and the influencer can follow it, but
// reliable automated posting to a newsletter is NOT exposed by this baileys
// build. We create the channel and fall back to the group feed for automated
// deal posting. This is documented in the README.

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
fs.mkdirSync(AUTH_ROOT, { recursive: true })

const logger = pino({ level: process.env.WA_LOG_LEVEL || 'info' })

class Session {
  constructor(key, meta = {}) {
    this.key = key
    this.meta = meta
    this.sock = null
    this.qr = null
    this.status = 'offline' // offline | qr | connecting | connected | error
    this.phone = ''
    this.listeners = new Set() // SSE subscribers
    this.authDir = path.join(AUTH_ROOT, sanitize(key))
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
  constructor() {
    this.sessions = new Map()
    this._load()
  }

  _load() {
    try {
      const saved = JSON.parse(fs.readFileSync(SESSIONS_FILE, 'utf8'))
      for (const s of saved) {
        // Re-create the object shell; the socket is connected lazily on start().
        const sess = new Session(s.key, s.meta || {})
        this.sessions.set(s.key, sess)
      }
    } catch (_) { /* no saved sessions yet */ }
  }

  _save() {
    const data = [...this.sessions.values()].map(s => ({ key: s.key, meta: s.meta }))
    fs.writeFileSync(SESSIONS_FILE, JSON.stringify(data, null, 2))
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
    if (this.sessions.has(key) && this.sessions.get(key).sock) {
      return this.sessions.get(key)
    }
    const sess = this.sessions.get(key) || new Session(key, meta)
    sess.meta = { ...sess.meta, ...meta }
    this.sessions.set(key, sess)
    this._save()

    const { state, saveCreds } = await useMultiFileAuthState(sess.authDir)
    sess.status = 'connecting'
    const sock = makeWASocket({
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, logger),
      },
      logger,
      printQRInTerminal: false,
      connectTimeoutMs: 60_000,
    })
    sess.sock = sock

    sock.ev.on('connection.update', (u) => {
      const { connection, qr, lastDisconnect } = u
      if (qr) {
        sess.qr = qr
        sess.status = 'qr'
        sess.emit({ type: 'qr', qr })
        logger.info({ key }, 'QR available')
      }
      if (connection === 'connecting') sess.status = 'connecting'
      if (connection === 'open') {
        sess.status = 'connected'
        sess.phone = sock.user?.id?.split('@')[0] || ''
        sess.qr = null
        sess.emit({ type: 'status', status: 'connected', phone: sess.phone })
        logger.info({ key, phone: sess.phone }, 'session connected')
      }
      if (connection === 'close') {
        const reason = lastDisconnect?.error?.output?.statusCode
        sess.status = 'offline'
        const shouldReconnect = reason !== DisconnectReason.loggedOut
        sess.emit({ type: 'status', status: 'offline' })
        logger.warn({ key, reason }, 'session closed')
        if (shouldReconnect) {
          // baileys will auto-reconnect using saved creds; give it a tick.
          setTimeout(() => this.start(key, sess.meta).catch(() => {}), 3000)
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
      // If unable to query metadata, accept the raw code/jid
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
    if (s?.sock) { try { await s.sock.logout() } catch (_) {} try { s.sock.end() } catch (_) {} }
    this.sessions.delete(key)
    this._save()
  }
}

export const hub = new Hub()
