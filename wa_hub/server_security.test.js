import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  bearerTokenMatches,
  isValidSessionKey,
  validateServerConfig,
} from './server_security.js'

const strongToken = '0123456789abcdef0123456789abcdef'

test('loopback development defaults remain easy to run without a token', () => {
  assert.deepEqual(validateServerConfig({}), {
    host: '127.0.0.1', port: 8088, token: '', production: false,
  })
  assert.deepEqual(validateServerConfig({ WA_HUB_HOST: '::1', WA_HUB_PORT: '9000' }), {
    host: '::1', port: 9000, token: '', production: false,
  })
})

test('non-loopback binding requires a strong token in every environment', () => {
  assert.throws(
    () => validateServerConfig({ WA_HUB_HOST: '0.0.0.0' }),
    /WA_HUB_TOKEN.*32 bytes/,
  )
  assert.throws(
    () => validateServerConfig({ WA_HUB_HOST: '0.0.0.0', WA_HUB_TOKEN: 'short' }),
    /WA_HUB_TOKEN.*32 bytes/,
  )
  assert.equal(validateServerConfig({
    WA_HUB_HOST: '0.0.0.0', WA_HUB_TOKEN: strongToken,
  }).host, '0.0.0.0')
})

test('production requires loopback binding and a strong token', () => {
  assert.throws(
    () => validateServerConfig({ NODE_ENV: 'production', WA_HUB_TOKEN: strongToken, WA_HUB_HOST: '0.0.0.0' }),
    /production WhatsApp Hub must bind/,
  )
  assert.throws(
    () => validateServerConfig({ NODE_ENV: 'production' }),
    /WA_HUB_TOKEN.*32 bytes/,
  )
  assert.equal(validateServerConfig({
    NODE_ENV: 'production', WA_HUB_TOKEN: strongToken,
  }).production, true)
})

test('invalid ports fail before the server binds', () => {
  for (const port of ['0', '-1', '65536', '8080x', '1.5']) {
    assert.throws(() => validateServerConfig({ WA_HUB_PORT: port }), /WA_HUB_PORT/)
  }
})

test('bearer token comparison requires an exact, non-empty credential', () => {
  assert.equal(bearerTokenMatches(`Bearer ${strongToken}`, strongToken), true)
  assert.equal(bearerTokenMatches(`bearer ${strongToken}`, strongToken), false)
  assert.equal(bearerTokenMatches(`Bearer ${strongToken}x`, strongToken), false)
  assert.equal(bearerTokenMatches('', strongToken), false)
  assert.equal(bearerTokenMatches(`Bearer ${strongToken}`, ''), false)
})

test('session keys reject empty, oversized, and control-character inputs', () => {
  assert.equal(isValidSessionKey('inf-42-wa'), true)
  assert.equal(isValidSessionKey('legacy:session name'), true)
  assert.equal(isValidSessionKey(''), false)
  assert.equal(isValidSessionKey('x'.repeat(129)), false)
  assert.equal(isValidSessionKey('bad\nkey'), false)
  assert.equal(isValidSessionKey({ key: 'not a string' }), false)
})
