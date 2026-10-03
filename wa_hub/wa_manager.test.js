import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { DisconnectReason } from 'baileys'

import { Hub } from './wa_manager.js'

class FakeSocket {
  constructor() {
    this.ev = new EventEmitter()
    this.user = { id: '919876543210@s.whatsapp.net' }
    this.logoutCalls = 0
    this.endCalls = 0
    this.sendCalls = []
  }

  async sendMessage(to, payload) {
    this.sendCalls.push({ to, payload })
    return { key: { id: `message-${this.sendCalls.length}` } }
  }

  emitUpdate(update) {
    this.ev.emit('connection.update', update)
  }

  async logout() {
    this.logoutCalls += 1
  }

  end() {
    this.endCalls += 1
  }
}

function makeFixture(t, existingRoot = null) {
  const root = existingRoot || fs.mkdtempSync(path.join(os.tmpdir(), 'wa-hub-test-'))
  if (!existingRoot) t.after(() => fs.rmSync(root, { recursive: true, force: true }))

  const authRoot = path.join(root, 'auth')
  const sessionsFile = path.join(root, 'sessions.json')
  const sockets = []
  const timers = []
  const authLoads = []
  const hub = new Hub({
    authRoot,
    sessionsFile,
    useAuthState: async (authDir) => {
      authLoads.push(authDir)
      fs.mkdirSync(authDir, { recursive: true, mode: 0o700 })
      return { state: { creds: {}, keys: {} }, saveCreds: async () => {} }
    },
    makeSocket: () => {
      const socket = new FakeSocket()
      sockets.push(socket)
      return socket
    },
    cacheSignalKeyStore: (keys) => keys,
    sessionLogger: { info() {}, warn() {} },
    schedule: (callback, delay) => {
      const timer = { callback, delay }
      timers.push(timer)
      return timer
    },
  })

  return { root, authRoot, sessionsFile, hub, sockets, timers, authLoads }
}

test('transient disconnect clears stale socket and re-establishes live connected state', async (t) => {
  const fixture = makeFixture(t)
  const { hub, sockets, timers, sessionsFile } = fixture
  const key = 'inf-7-wa'
  const meta = { influencer_id: 7, label: 'wa' }
  const session = await hub.start(key, meta)

  assert.equal(session.status, 'connecting')
  assert.equal(sockets.length, 1)
  sockets[0].emitUpdate({ connection: 'qr', qr: 'one-time-qr' })
  assert.equal(session.status, 'qr')
  assert.equal(session.qr, 'one-time-qr')

  sockets[0].emitUpdate({ connection: 'open' })
  assert.equal(session.status, 'connected')
  assert.equal(session.phone, '919876543210')
  assert.equal(session.qr, null)

  sockets[0].emitUpdate({
    connection: 'close',
    lastDisconnect: { error: { output: { statusCode: DisconnectReason.connectionLost } } },
  })
  assert.equal(session.status, 'offline')
  assert.equal(session.sock, null)
  assert.equal(session.qr, null)
  assert.equal(session.phone, '919876543210')
  assert.equal(timers.length, 1)
  assert.equal(timers[0].delay, 3000)

  await timers[0].callback()
  assert.equal(sockets.length, 2)
  assert.equal(session.sock, sockets[1])
  assert.equal(session.status, 'connecting')

  // Late updates from the closed socket must not override the replacement.
  sockets[0].emitUpdate({ connection: 'open' })
  assert.equal(session.status, 'connecting')
  sockets[1].emitUpdate({ connection: 'open' })
  assert.equal(session.status, 'connected')
  assert.equal(session.phone, '919876543210')

  assert.deepEqual(JSON.parse(fs.readFileSync(sessionsFile, 'utf8')), [{ key, meta }])
})

test('saved session identity resumes after Hub restart and status is re-read from the live socket', async (t) => {
  const first = makeFixture(t)
  const key = 'inf-12-wa'
  const meta = { influencer_id: 12, label: 'wa' }
  const original = await first.hub.start(key, meta)
  first.sockets[0].emitUpdate({ connection: 'open' })
  assert.equal(original.status, 'connected')

  const restarted = makeFixture(t, first.root)
  const restored = restarted.hub.get(key)
  assert.ok(restored)
  assert.deepEqual(restored.meta, meta)
  // Connection status is live, not a durable claim that a dead process is online.
  assert.equal(restored.status, 'offline')
  assert.equal(restored.phone, '')

  await restarted.hub.start(key)
  assert.deepEqual(restarted.authLoads, [path.join(restarted.authRoot, key)])
  assert.equal(restored.status, 'connecting')
  restarted.sockets[0].emitUpdate({ connection: 'open' })
  assert.equal(restored.status, 'connected')
  assert.equal(restored.phone, '919876543210')
})

test('native WhatsApp polls validate options and are restricted to groups', async (t) => {
  const fixture = makeFixture(t)
  const { hub, sockets } = fixture
  await hub.start('inf-polls-wa', { influencer_id: 7, label: 'wa' })

  const first = await hub.sendPoll(
    'inf-polls-wa', '120363001@g.us', 'Which category should we feature?',
    ['Fashion', 'Electronics'], false,
  )
  assert.deepEqual(first, { ok: true, id: 'message-1' })
  assert.deepEqual(sockets[0].sendCalls[0], {
    to: '120363001@g.us',
    payload: {
      poll: {
        name: 'Which category should we feature?',
        values: ['Fashion', 'Electronics'],
        selectableCount: 1,
      },
    },
  })

  await hub.sendPoll(
    'inf-polls-wa', '120363001@g.us', 'Pick every topic you like',
    ['Fashion', 'Phones', 'Home'], true,
  )
  assert.equal(sockets[0].sendCalls[1].payload.poll.selectableCount, 3)

  await assert.rejects(
    hub.sendPoll('inf-polls-wa', '123@newsletter', 'Question?', ['A', 'B']),
    /only in groups/,
  )
  await assert.rejects(
    hub.sendPoll('inf-polls-wa', '120363001@g.us', 'Question?', ['Yes', 'yes!']),
    /must be unique/,
  )
  assert.equal(sockets[0].sendCalls.length, 2)
})

test('logged-out sessions clear credentials and can be explicitly paired again', async (t) => {
  const fixture = makeFixture(t)
  const { hub, sockets, timers } = fixture
  const session = await hub.start('inf-3-wa', { influencer_id: 3, label: 'wa' })
  const authDir = session.authDir
  assert.equal(fs.existsSync(authDir), true)

  sockets[0].emitUpdate({
    connection: 'close',
    lastDisconnect: { error: { output: { statusCode: DisconnectReason.loggedOut } } },
  })
  assert.equal(session.status, 'offline')
  assert.equal(session.sock, null)
  assert.equal(session.phone, '')
  assert.equal(fs.existsSync(authDir), false)
  assert.equal(timers.length, 0)

  await hub.start('inf-3-wa')
  assert.equal(sockets.length, 2)
  assert.equal(session.status, 'connecting')
})
