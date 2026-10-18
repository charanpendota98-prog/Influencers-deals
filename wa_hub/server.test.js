import assert from 'node:assert/strict'
import { once } from 'node:events'
import { spawn } from 'node:child_process'
import net from 'node:net'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'

const HERE = path.dirname(fileURLToPath(import.meta.url))

async function availablePort() {
  const probe = net.createServer()
  probe.listen(0, '127.0.0.1')
  await once(probe, 'listening')
  const { port } = probe.address()
  await new Promise((resolve, reject) => probe.close((error) => error ? reject(error) : resolve()))
  return port
}

async function waitForReady(child) {
  let output = ''
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`WhatsApp Hub did not start: ${output}`)), 8000)
    const onData = (chunk) => {
      output += chunk.toString()
      if (output.includes('listening on')) {
        clearTimeout(timeout)
        resolve()
      }
    }
    child.stdout.on('data', onData)
    child.stderr.on('data', onData)
    child.once('error', (error) => {
      clearTimeout(timeout)
      reject(error)
    })
    child.once('exit', (code) => {
      clearTimeout(timeout)
      reject(new Error(`WhatsApp Hub exited early (${code}): ${output}`))
    })
  })
}

async function stopChild(child) {
  if (child.exitCode !== null || child.signalCode !== null) return
  child.kill('SIGTERM')
  await Promise.race([once(child, 'exit'), new Promise((resolve) => setTimeout(resolve, 3000))])
  if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL')
}

test('HTTP API enforces bearer auth, bounds input, and marks responses non-cacheable', async (t) => {
  const port = await availablePort()
  const token = '1234567890abcdef1234567890abcdef'
  const child = spawn(process.execPath, ['server.js'], {
    cwd: HERE,
    env: {
      ...process.env,
      NODE_ENV: 'test',
      WA_HUB_HOST: '127.0.0.1',
      WA_HUB_PORT: String(port),
      WA_HUB_TOKEN: token,
      WA_LOG_LEVEL: 'silent',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  t.after(() => stopChild(child))
  await waitForReady(child)

  const base = `http://127.0.0.1:${port}`
  const unauthorized = await fetch(`${base}/health`)
  assert.equal(unauthorized.status, 401)
  assert.deepEqual(await unauthorized.json(), { ok: false, error: 'unauthorized' })

  const headers = { Authorization: `Bearer ${token}` }
  const health = await fetch(`${base}/health`, { headers })
  assert.equal(health.status, 200)
  assert.match(health.headers.get('cache-control') || '', /no-store/)
  assert.deepEqual(await health.json(), { ok: true, sessions: 0 })

  const invalidKey = await fetch(`${base}/sessions`, {
    method: 'POST',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify({ key: { unexpected: true } }),
  })
  assert.equal(invalidKey.status, 400)
  assert.match((await invalidKey.json()).error, /key must be/)

  const oversized = await fetch(`${base}/sessions`, {
    method: 'POST',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify({ key: 'x'.repeat(70 * 1024) }),
  })
  assert.equal(oversized.status, 413)
  assert.deepEqual(await oversized.json(), { ok: false, error: 'request body too large' })
})
