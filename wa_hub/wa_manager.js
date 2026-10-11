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
    this.status = 'offline' // offline | qr | pairing | connecting | connected | error
    this.phone = ''
    this.listeners = new Set() // SSE subscribers
    this.authDir = path.join(authRoot, sanitize(key))
    // Phone-link (OTP) pairing: the operator types the number, WhatsApp shows
    // an 8-character code on that phone, and the same registered device results.
    // The code is a one-time login credential, so it is never logged.
    this.pairingPhone = ''
    this.pairingCode = ''
    this.pairingError = ''
    this.pairingIssued = false
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

// A WhatsApp phone-linking code is only offered for a full international
// number (country code included). Anything shorter cannot be routed by
// WhatsApp, so it is rejected before a socket is even created.
const PAIRING_PHONE_MIN_DIGITS = 10
const PAIRING_PHONE_MAX_DIGITS = 15

export function normalizePairingPhone(raw) {
  const digits = String(raw ?? '').replace(/[^0-9]/g, '')
  if (digits.length < PAIRING_PHONE_MIN_DIGITS || digits.length > PAIRING_PHONE_MAX_DIGITS) return ''
  return digits
}

// WhatsApp prints the code in two groups of four (e.g. "ABCD-EFGH").
export function formatPairingCode(code) {
  const clean = String(code ?? '').toUpperCase().replace(/[^0-9A-Z]/g, '')
  if (clean.length !== 8) return clean
  return `${clean.slice(0, 4)}-${clean.slice(4)}`
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
      // Only whether a code is waiting; the one-time code itself stays on the
      // session and is returned solely by the explicit pairing endpoints.
      pairing: !!s.pairingCode,
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
      // A pairing code can only be requested while the device is still
      // unregistered and the socket is open (Baileys sends the code over the
      // live websocket), so it is issued on the first connection update of a
      // requested number and cached on the session.
      if (sess.pairingPhone && !sess.pairingCode && !sess.pairingIssued) {
        void this._issuePairingCode(sess, sock)
      }
      if (qr) {
        sess.qr = qr
        if (!sess.pairingPhone) sess.status = 'qr'
        sess.emit({ type: 'qr', qr })
        this.logger.info({ key: sess.key }, 'QR available')
      }
      if (connection === 'connecting') {
        if (sess.status !== 'pairing') sess.status = 'connecting'
      }
      if (connection === 'open') {
        sess.status = 'connected'
        sess.phone = sock.user?.id?.split('@')[0] || ''
        sess.qr = null
        // The number is linked now: the one-time code is finished.
        sess.pairingPhone = ''
        sess.pairingCode = ''
        sess.pairingError = ''
        sess.pairingIssued = false
        sess.emit({ type: 'status', status: 'connected', phone: sess.phone })
        this.logger.info({ key: sess.key, phone: sess.phone }, 'session connected')
      }
      if (connection === 'close') {
        const reason = lastDisconnect?.error?.output?.statusCode
        const loggedOut = reason === DisconnectReason.loggedOut
        sess.sock = null
        sess.qr = null
        sess.status = 'offline'
        // A code belongs to one socket attempt; drop it so a reconnect can
        // issue a fresh one instead of showing an expired code.
        sess.pairingCode = ''
        sess.pairingIssued = false
        if (loggedOut) {
          sess.phone = ''
          sess.pairingPhone = ''
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

  /**
   * Link a phone number with an 8-character pairing code instead of a QR scan.
   *
   * The operator enters the influencer's full international number; WhatsApp
   * shows the code on that phone (Linked devices -> "Link with phone number").
   * The result is the same registered device as a QR scan, so every later
   * send/read path is unchanged. One socket attempt can only request one code,
   * and the code dies with its socket, so it is cached on the session and
   * re-issued by the next connect attempt.
   */
  async requestPairingCode(key, phone, meta = {}) {
    const digits = normalizePairingPhone(phone)
    if (!digits) {
      throw new Error('a full international WhatsApp number is required (10-15 digits, country code included)')
    }

    const sess = this.sessions.get(key) || new Session(key, meta, this.authRoot)
    sess.meta = { ...sess.meta, ...meta }
    this.sessions.set(key, sess)
    this._save()

    if (sess.status === 'connected') {
      throw new Error('this WhatsApp number is already linked; disconnect it before pairing again')
    }

    sess.pairingPhone = digits
    sess.pairingError = ''
    if (sess.pairingCode) {
      return this._pairingPayload(sess, true)
    }

    if (!sess.sock) {
      // Creating the socket makes Baileys emit its connection updates, which is
      // where the code gets requested (the code needs a live connection).
      await this.start(key, sess.meta)
    }
    if (sess.sock && sess.qr && !sess.pairingIssued && !sess.pairingCode) {
      // The socket was already past its first update (for example a QR that is
      // live right now), so nothing new would trigger the request on its own.
      void this._issuePairingCode(sess, sess.sock)
    }

    const payload = await this._waitForPairingCode(sess, 25_000)
    if (!payload.ok || !payload.code) {
      const detail = sess.pairingError
        || (sess.status === 'connected'
          ? 'this WhatsApp number is already linked'
          : 'WhatsApp did not answer with a pairing code in time')
      const error = new Error(detail)
      error.status = 504
      throw error
    }
    return payload
  }

  /** Read-only view of the pairing attempt (never starts or mutates a socket). */
  pairingState(key) {
    const sess = this.sessions.get(key)
    if (!sess) return { ok: true, status: 'offline', phone: '', code: '', error: '', hasCode: false }
    return {
      ok: true,
      status: sess.status,
      phone: sess.pairingPhone,
      code: formatPairingCode(sess.pairingCode),
      hasCode: !!sess.pairingCode,
      error: sess.pairingError,
    }
  }

  _pairingPayload(sess, cached = false) {
    return {
      ok: true,
      status: sess.status,
      phone: sess.pairingPhone,
      code: formatPairingCode(sess.pairingCode),
      cached,
    }
  }

  async _issuePairingCode(sess, sock) {
    if (sess.pairingIssued || !sess.pairingPhone) return
    sess.pairingIssued = true
    try {
      if (typeof sock.requestPairingCode !== 'function') {
        throw new Error('this WhatsApp build cannot request a pairing code; scan the QR instead')
      }
      const code = await sock.requestPairingCode(sess.pairingPhone)
      if (this.sessions.get(sess.key) !== sess || sess.sock !== sock) return
      sess.pairingCode = String(code || '').trim().toUpperCase()
      if (!sess.pairingCode) throw new Error('WhatsApp returned an empty pairing code')
      sess.status = 'pairing'
      sess.pairingError = ''
      // The code itself is a one-time credential and is never logged.
      this.logger.info({ key: sess.key }, 'phone-link pairing code issued')
      sess.emit({ type: 'pairing', phone: sess.pairingPhone })
    } catch (error) {
      sess.pairingIssued = false
      sess.pairingCode = ''
      sess.pairingError = String(error?.message || error)
      this.logger.warn({ key: sess.key, error: sess.pairingError }, 'pairing code request failed')
      sess.emit({ type: 'pairing_error', error: sess.pairingError })
    }
  }

  _waitForPairingCode(sess, timeoutMs) {
    if (sess.pairingCode || sess.status === 'connected') {
      return Promise.resolve(this._pairingPayload(sess))
    }
    if (sess.pairingError && !sess.pairingIssued) {
      return Promise.resolve({
        ok: false, status: sess.status, phone: sess.pairingPhone, error: sess.pairingError,
      })
    }
    return new Promise((resolve) => {
      let settled = false
      const finish = (payload) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        unsubscribe()
        resolve(payload)
      }
      const unsubscribe = sess.addListener((event) => {
        if (event.type === 'pairing_error') {
          finish({ ok: false, status: sess.status, phone: sess.pairingPhone, error: event.error })
        }
        if (event.type === 'pairing') finish(this._pairingPayload(sess))
        if (event.type === 'status' && event.status === 'connected') finish(this._pairingPayload(sess))
      })
      const timer = setTimeout(() => {
        finish({
          ok: false, status: sess.status, phone: sess.pairingPhone,
          error: sess.pairingError || 'WhatsApp did not answer with a pairing code in time',
        })
      }, timeoutMs)
      // A pending pairing wait must never hold the process (or a test run) open.
      if (typeof timer.unref === 'function') timer.unref()
    })
  }

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

  async sendPoll(key, to, question, options, allowMultiple = false) {
    const s = this.sessions.get(key)
    if (!s?.sock) throw new Error('session not connected: ' + key)
    if (typeof to !== 'string' || !to.endsWith('@g.us')) {
      throw new Error('WhatsApp polls are supported only in groups, not Channels or direct chats')
    }

    const cleanQuestion = String(question || '').trim()
    const cleanOptions = Array.isArray(options)
      ? options.map(option => String(option || '').trim()).filter(Boolean)
      : []
    if (!cleanQuestion || cleanQuestion.length > 255) throw new Error('invalid poll question')
    if (cleanOptions.length < 2 || cleanOptions.length > 10) throw new Error('polls require 2 to 10 options')
    if (cleanOptions.some(option => option.length > 100)) throw new Error('poll options must be 100 characters or fewer')
    const optionKeys = cleanOptions.map(option => option.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim())
    if (new Set(optionKeys).size !== optionKeys.length) throw new Error('poll options must be unique')

    const res = await s.sock.sendMessage(to, {
      poll: {
        name: cleanQuestion,
        values: cleanOptions,
        selectableCount: allowMultiple === true ? cleanOptions.length : 1,
      },
    })
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
    s.pairingPhone = ''
    s.pairingCode = ''
    s.pairingError = ''
    s.pairingIssued = false
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
