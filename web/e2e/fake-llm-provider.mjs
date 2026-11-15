// Local fake LLM provider for the slice-35 E2E journey.
//
// The daemon is pointed at this server through OFT_LLM_ANTHROPIC_BASE_URL so the
// capability probe (`POST /api/v1/llm/test`) and any scheduled analysis call use
// a local fake transport. It never forwards a request to a real provider and it
// never records the request body (which carries the (fake) key in a header).
//
// Endpoints:
//   POST /v1/messages  -> a valid Anthropic messages body whose text is the
//                         strict probe payload {"ok":"ok"}.
//   GET  /__health     -> readiness probe.
//   GET  /__requests   -> request count/paths/auth-seen, used to prove the probe
//                         hit this fake and not a remote host.
import http from 'node:http'

const host = '127.0.0.1'
const port = Number(process.argv[2] ?? 0)
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  console.error('fake-llm-provider: a valid port is required')
  process.exit(2)
}

let requestCount = 0
const requestPaths = []
let authSeen = false

const server = http.createServer((request, response) => {
  const path = (request.url ?? '').split('?')[0]

  if (request.method === 'GET' && path === '/__health') {
    response.writeHead(200, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ ok: true }))
    return
  }

  if (request.method === 'GET' && path === '/__requests') {
    response.writeHead(200, { 'content-type': 'application/json' })
    response.end(
      JSON.stringify({
        count: requestCount,
        paths: requestPaths,
        auth_present: authSeen,
      }),
    )
    return
  }

  let body = ''
  request.on('data', (chunk) => {
    body += chunk
  })
  request.on('end', () => {
    if (request.method !== 'POST' || !(path === '/v1/messages' || path === '/messages')) {
      response.writeHead(404, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ error: 'not found' }))
      return
    }
    requestCount += 1
    requestPaths.push(path)
    authSeen =
      authSeen ||
      Boolean(request.headers['x-api-key'] || request.headers.authorization)

    let model = 'fake-model'
    try {
      const parsed = JSON.parse(body)
      if (typeof parsed?.model === 'string' && parsed.model.trim()) {
        model = parsed.model
      }
    } catch {
      // A malformed body still gets a valid probe payload; the adapter owns
      // request construction, not this fake.
    }

    response.writeHead(200, { 'content-type': 'application/json' })
    response.end(
      JSON.stringify({
        id: 'msg_slice35_e2e',
        type: 'message',
        role: 'assistant',
        model,
        content: [{ type: 'text', text: '{"ok":"ok"}' }],
        stop_reason: 'end_turn',
        usage: { input_tokens: 10, output_tokens: 5 },
      }),
    )
  })
})

server.listen(port, host, () => {
  console.log(`fake-llm-provider ready on http://${host}:${port}`)
})

for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => {
    server.close(() => process.exit(0))
    setTimeout(() => process.exit(0), 1000).unref()
  })
}
