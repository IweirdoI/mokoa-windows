import test from 'node:test'
import assert from 'node:assert/strict'
import { MAX_FILE_BYTES, validateFile, createFileForm, createSourceRequest, requestApi, extractionLabel, setApiToken, authHeaders } from '../src/ingestion.js'

test('file boundary validation: supported formats, old Word, empty, exact 8MB and oversize', () => {
  for (const ext of ['PDF', 'docx', 'png', 'jpg', 'jpeg', 'webp', 'bmp', 'gif']) assert.equal(validateFile({ name: `test.${ext}`, size: 20 }), '')
  assert.match(validateFile({ name: 'old.doc', size: 20 }), /另存为/)
  assert.match(validateFile({ name: 'text.txt', size: 20 }), /仅支持/)
  assert.match(validateFile({ name: 'empty.pdf', size: 0 }), /为空/)
  assert.equal(validateFile({ name: 'ok.pdf', size: MAX_FILE_BYTES }), '')
  assert.match(validateFile({ name: 'large.pdf', size: MAX_FILE_BYTES + 1 }), /8MB/)
})

test('file envelope preserves file, user, scene, caption and omits source', () => {
  const file = new File(['pdf'], 'test.pdf')
  const form = createFileForm(file, ' user-a ', ' upload ', ' 扫描件内容 ')
  assert.equal(form.get('file').name, 'test.pdf')
  assert.equal(form.get('user_id'), 'user-a')
  assert.equal(form.get('scene_id'), 'upload')
  assert.equal(form.get('caption'), '扫描件内容')
  assert.equal(form.has('source'), false)
  assert.throws(() => createFileForm(file, ' ', '', ''), /用户 ID/)
})

test('four structured sources use backend fields and validate required data', () => {
  const tool = createSourceRequest('tool_result', { tool_name: 'pip', status: 'failed', output: 'timeout', action_id: 'event-1' }, 'u', 'coding')
  assert.equal(tool.payload.action_id, 'event-1')
  assert.equal(tool.payload.output, 'timeout')
  assert.equal(tool.user_id, 'u')
  assert.equal(tool.scene_id, 'coding')
  const behavior = createSourceRequest('user_behavior', { action: '打开', target: 'IDE', occurrence_count: '5' }, 'u', '')
  assert.equal(behavior.payload.occurrence_count, 5)
  for (const count of [0, -1, 'x', 1.5]) assert.throws(() => createSourceRequest('user_behavior', { action: 'a', target: 'b', occurrence_count: count }, 'u', ''), /正整数/)
  assert.equal(createSourceRequest('manual_config', { key: 'lang', value: '中文' }, 'u', '').payload.scope, 'global')
  assert.throws(() => createSourceRequest('manual_config', { key: 'lang', value: '中文', scope: 'scene' }, 'u', ''), /场景名称/)
  assert.equal(createSourceRequest('conversation', { content: '记住我' }, 'u', '').payload.role, 'user')
  assert.throws(() => createSourceRequest('conversation', { content: ' ' }, 'u', ''), /对话内容/)
  assert.throws(() => createSourceRequest('unknown', {}, 'u', ''), /数据来源/)
})

test('request transport leaves multipart headers to the browser and returns business rejection', async t => {
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({ url, options })
    return new Response(JSON.stringify({ accepted: false, reject_code: 'duplicate' }), { status: 200 })
  })
  const form = createFileForm(new File(['data'], 'guide.pdf'), 'u', '', '')
  const result = await requestApi('/ingest/file', { method: 'POST', body: form })
  assert.equal(requests[0].url, '/api/ingest/file')
  assert.equal(requests[0].options.headers.has('Content-Type'), false)
  assert.equal(requests[0].options.body, form)
  assert.equal(result.accepted, false)
  await requestApi('/ingest', { method: 'POST', body: '{}' })
  assert.equal(requests[1].options.headers.get('Content-Type'), 'application/json')
})

test('HTTP validation errors and timeouts retain actionable messages', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ detail: [{ msg: 'Field required' }] }), { status: 422 }))
  await assert.rejects(requestApi('/ingest'), /Field required/)
  globalThis.fetch = async (_url, { signal }) => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('abort', 'AbortError'))))
  await assert.rejects(requestApi('/ingest', {}, 10), /后端可能仍在处理/)
})

test('extraction labels distinguish actual vision, OCR and caption-only fallback', () => {
  assert.match(extractionLabel({ vision_used: true, ocr_used: false }), /视觉/)
  assert.match(extractionLabel({ ocr_used: true }), /OCR/)
  assert.match(extractionLabel({ extract_status: 'empty', caption: 'caption' }), /未提取到正文/)
  assert.match(extractionLabel({ file_kind: 'image', char_count: 0, caption: 'caption', ocr_used: false, vision_used: false }), /未提取到正文/)
  assert.match(extractionLabel({ file_kind: 'pdf', extract_status: 'ok' }), /文档文字/)
})

test('optional API token is attached as Bearer only when configured', async t => {
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({ url, options })
    return new Response(JSON.stringify({ ok: true }), { status: 200 })
  })
  t.after(() => setApiToken(''))

  // Unset: keep local development working without an Authorization header.
  setApiToken('')
  await requestApi('/health')
  assert.equal(requests[0].options.headers.has('Authorization'), false)

  // Configured: REST and multipart uploads both carry the bearer token.
  setApiToken('secret-token')
  await requestApi('/stats/demo_user')
  assert.equal(requests[1].options.headers.get('Authorization'), 'Bearer secret-token')
  const form = createFileForm(new File(['data'], 'guide.pdf'), 'u', '', '')
  await requestApi('/ingest/file', { method: 'POST', body: form })
  assert.equal(requests[2].options.headers.get('Authorization'), 'Bearer secret-token')
  assert.equal(requests[2].options.headers.has('Content-Type'), false)

  // A caller-provided Authorization header wins over the configured token.
  assert.equal(authHeaders({ Authorization: 'Bearer custom' }).get('Authorization'), 'Bearer custom')
  setApiToken('')
})
