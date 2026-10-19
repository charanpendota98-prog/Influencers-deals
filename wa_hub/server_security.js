import { timingSafeEqual } from 'node:crypto'

const LOOPBACK_HOSTS = new Set(['127.0.0.1', '::1'])
const MIN_TOKEN_BYTES = 32
const MAX_SESSION_KEY_LENGTH = 128

export function validateServerConfig(env = process.env) {
  const host = String(env.WA_HUB_HOST || '127.0.0.1').trim()
  const rawPort = env.WA_HUB_PORT || '8088'
  const port = Number(rawPort)
  const token = String(env.WA_HUB_TOKEN || '')
  const production = String(env.NODE_ENV || '').toLowerCase() === 'production'
  const loopbackOnly = LOOPBACK_HOSTS.has(host)

  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('WA_HUB_PORT must be an integer between 1 and 65535')
  }
  if (production && !loopbackOnly) {
    throw new Error('production WhatsApp Hub must bind to 127.0.0.1 or ::1')
  }
  if ((production || !loopbackOnly) && Buffer.byteLength(token, 'utf8') < MIN_TOKEN_BYTES) {
    throw new Error('a private WA_HUB_TOKEN of at least 32 bytes is required for this binding')
  }

  return { host, port, token, production }
}

export function bearerTokenMatches(authorization, token) {
  if (typeof authorization !== 'string' || typeof token !== 'string' || !token) return false
  const received = Buffer.from(authorization, 'utf8')
  const expected = Buffer.from(`Bearer ${token}`, 'utf8')
  return received.length === expected.length && timingSafeEqual(received, expected)
}

export function isValidSessionKey(value) {
  return typeof value === 'string'
    && value.length > 0
    && value.length <= MAX_SESSION_KEY_LENGTH
    && !/[\u0000-\u001f\u007f]/u.test(value)
}
