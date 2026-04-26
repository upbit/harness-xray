'use strict'

const { createHash, randomUUID } = require('node:crypto')
const fs = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')

if (!globalThis.__anthropicDumpPreloadInstalled) {
  globalThis.__anthropicDumpPreloadInstalled = true
  install()
}

function install() {
  const originalFetch = globalThis.fetch
  if (typeof originalFetch !== 'function') {
    debug('globalThis.fetch is unavailable; preload skipped')
    return
  }

  const baseDir = getDumpBaseDir()
  const dumpId = getDumpId()
  const filePath = resolveDumpFile(baseDir, dumpId)
  const dumpState = new Map()

  globalThis.fetch = async function dumpWrappedFetch(input, init) {
    const requestUrl = getRequestUrl(input)
    const timestamp = new Date().toISOString()
    const stateKey = process.env.CLAUDE_DUMP_STATE_KEY || dumpId
    const state = getOrCreateState(dumpState, stateKey)

    try {
      if (shouldInspectRequest(requestUrl, init)) {
        const requestBody = getStringBody(init)
        if (requestBody) {
          setImmediate(() => {
            dumpRequest({
              body: requestBody,
              timestamp,
              state,
              filePath,
              requestUrl,
              method: init && init.method ? init.method : 'POST',
            })
          })
        }
      }
    } catch (error) {
      debug('request dump scheduling failed', error)
    }

    const response = await originalFetch.call(this, input, init)

    try {
      if (shouldInspectResponse(requestUrl, response)) {
        const cloned = response.clone()
        void dumpResponse({
          response: cloned,
          timestamp,
          filePath,
          requestUrl,
          status: response.status,
        })
      }
    } catch (error) {
      debug('response dump scheduling failed', error)
    }

    return response
  }

  if (process.env.CLAUDE_DUMP_SILENT !== '1') {
    process.stderr.write(`[anthropic-dump] writing to ${filePath}\n`)
  }
}

function getDumpBaseDir() {
  if (process.env.CLAUDE_DUMP_DIR) {
    return process.env.CLAUDE_DUMP_DIR
  }
  if (process.env.CLAUDE_CONFIG_DIR) {
    return path.join(process.env.CLAUDE_CONFIG_DIR, 'dump-prompts')
  }
  return path.join(os.homedir(), '.claude', 'dump-prompts')
}

function getDumpId() {
  return (
    process.env.CLAUDE_DUMP_ID ||
    process.env.CLAUDE_CODE_SESSION_ID ||
    process.env.CLAUDE_SESSION_ID ||
    `${process.title || 'node'}-${process.pid}-${Date.now()}-${randomUUID().slice(0, 8)}`
  )
}

function resolveDumpFile(baseDir, dumpId) {
  const suffix = process.env.CLAUDE_DUMP_SPLIT_BY_PID === '1' ? `-${process.pid}` : ''
  return path.join(baseDir, `${dumpId}${suffix}.jsonl`)
}

function getOrCreateState(store, key) {
  let state = store.get(key)
  if (state) return state
  state = {
    initialized: false,
    messageCountSeen: 0,
    lastInitDataHash: '',
    lastInitFingerprint: '',
  }
  store.set(key, state)
  return state
}

function shouldInspectRequest(requestUrl, init) {
  const method = ((init && init.method) || 'GET').toUpperCase()
  if (method !== 'POST') return false
  if (!requestUrl) return true

  const onlyAnthropic = process.env.CLAUDE_DUMP_ONLY_ANTHROPIC !== '0'
  if (!onlyAnthropic) return true

  try {
    const url = new URL(requestUrl)
    return (
      url.hostname.includes('anthropic.com') ||
      url.pathname.endsWith('/v1/messages') ||
      url.pathname.includes('/anthropic/') ||
      url.pathname.includes('/messages')
    )
  } catch {
    return true
  }
}

function shouldInspectResponse(requestUrl, response) {
  if (!response || !response.ok) return false
  return shouldInspectRequest(requestUrl, { method: 'POST' })
}

function getRequestUrl(input) {
  try {
    if (typeof input === 'string') return input
    if (input instanceof URL) return input.toString()
    if (typeof Request !== 'undefined' && input instanceof Request) return input.url
  } catch {
    return undefined
  }
  return undefined
}

function getStringBody(init) {
  if (!init || init.body == null) return undefined
  if (typeof init.body === 'string') return init.body
  if (Buffer.isBuffer(init.body)) return init.body.toString('utf8')
  if (init.body instanceof Uint8Array) return Buffer.from(init.body).toString('utf8')
  return undefined
}

function hashString(value) {
  return createHash('sha256').update(value).digest('hex')
}

function initFingerprint(req) {
  const tools = Array.isArray(req.tools) ? req.tools : undefined
  const system = req.system
  const sysLen =
    typeof system === 'string'
      ? system.length
      : Array.isArray(system)
        ? system.reduce((n, block) => n + ((block && block.text) || '').length, 0)
        : 0
  const toolNames = tools ? tools.map(tool => (tool && tool.name) || '').join(',') : ''
  return `${req.model || ''}|${toolNames}|${sysLen}`
}

function dumpRequest({ body, timestamp, state, filePath, requestUrl, method }) {
  try {
    const req = JSON.parse(body)
    if (!looksLikeAnthropicMessagesRequest(req)) return

    const entries = []
    const messages = Array.isArray(req.messages) ? req.messages : []
    const fingerprint = initFingerprint(req)

    if (!state.initialized || fingerprint !== state.lastInitFingerprint) {
      const initData = { ...req }
      delete initData.messages
      const initDataStr = JSON.stringify(initData)
      const initDataHash = hashString(initDataStr)
      state.lastInitFingerprint = fingerprint
      if (!state.initialized) {
        state.initialized = true
        state.lastInitDataHash = initDataHash
        entries.push(
          JSON.stringify({
            type: 'init',
            timestamp,
            requestUrl,
            method,
            data: initData,
          }),
        )
      } else if (initDataHash !== state.lastInitDataHash) {
        state.lastInitDataHash = initDataHash
        entries.push(
          JSON.stringify({
            type: 'system_update',
            timestamp,
            requestUrl,
            method,
            data: initData,
          }),
        )
      }
    }

    for (const msg of messages.slice(state.messageCountSeen)) {
      if (msg && msg.role === 'user') {
        entries.push(
          JSON.stringify({
            type: 'message',
            timestamp,
            requestUrl,
            method,
            data: msg,
          }),
        )
      }
    }
    state.messageCountSeen = messages.length

    appendToFile(filePath, entries)
  } catch (error) {
    debug('request dump failed', error)
  }
}

async function dumpResponse({ response, timestamp, filePath, requestUrl, status }) {
  try {
    const contentType = response.headers.get('content-type') || ''
    let data

    if (contentType.includes('text/event-stream') && response.body) {
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      try {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
        }
        buffer += decoder.decode()
      } finally {
        reader.releaseLock()
      }
      data = {
        stream: true,
        chunks: parseSSEChunks(buffer),
      }
    } else {
      const text = await response.text()
      data = tryParseJson(text)
    }

    await appendToFile(
      filePath,
      [
        JSON.stringify({
          type: 'response',
          timestamp,
          requestUrl,
          status,
          data,
        }),
      ],
    )
  } catch (error) {
    debug('response dump failed', error)
  }
}

function parseSSEChunks(buffer) {
  const chunks = []
  for (const event of buffer.split('\n\n')) {
    for (const line of event.split('\n')) {
      if (!line.startsWith('data: ')) continue
      if (line === 'data: [DONE]') continue
      const payload = line.slice(6)
      chunks.push(tryParseJson(payload))
    }
  }
  return chunks
}

function looksLikeAnthropicMessagesRequest(req) {
  return !!(
    req &&
    typeof req === 'object' &&
    typeof req.model === 'string' &&
    Array.isArray(req.messages)
  )
}

function tryParseJson(text) {
  if (typeof text !== 'string') return text
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

async function appendToFile(filePath, entries) {
  if (!entries || entries.length === 0) return
  await fs.mkdir(path.dirname(filePath), { recursive: true })
  await fs.appendFile(filePath, `${entries.join('\n')}\n`, 'utf8')
}

function debug(message, error) {
  if (process.env.CLAUDE_DUMP_DEBUG !== '1') return
  const detail = error instanceof Error ? ` ${error.stack || error.message}` : error ? ` ${String(error)}` : ''
  process.stderr.write(`[anthropic-dump] ${message}${detail}\n`)
}
