// Optional API token, mirroring the backend's API_AUTH_TOKEN (api/server.py).
// Injected by Vite as VITE_API_TOKEN; when unset the header is omitted so local
// development keeps working without authentication. Export setApiToken so tests
// (and embedding contexts without import.meta.env) can configure it explicitly.
const env = import.meta.env || {}
let apiToken = String(env.VITE_API_TOKEN || '').trim()

export function setApiToken(token) {
  apiToken = String(token ?? '').trim()
}

// Attach Authorization without clobbering a caller-provided header.
export function authHeaders(base) {
  const headers = base instanceof Headers ? base : new Headers(base)
  if (apiToken && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${apiToken}`)
  return headers
}

// HTTP contract shared by the multi-source forms and file queue.
export const MAX_FILE_BYTES = 8 * 1024 * 1024
export const FILE_ACCEPT = '.pdf,.docx,.png,.jpg,.jpeg,.webp,.bmp,.gif'
export const sources = [
  { id: 'document', label: '文件 / 图片', hint: 'PDF、Word 和截图中的知识' },
  { id: 'tool_result', label: '工具结果', hint: '执行日志、报错与解决经验' },
  { id: 'user_behavior', label: '用户行为', hint: '重复操作与使用习惯' },
  { id: 'manual_config', label: '手动配置', hint: '明确的设置与偏好' },
  { id: 'conversation', label: '文本对话', hint: '需要记住的文字内容' },
]

export function validateFile(file) {
  const ext = '.' + file.name.split('.').pop().toLowerCase()
  if (ext === '.doc') return '暂不支持旧版 Word，请另存为 .docx。'
  if (!FILE_ACCEPT.split(',').includes(ext)) return '仅支持 PDF、Word（.docx）及 PNG/JPG/JPEG/WebP/BMP/GIF 图片。'
  if (!file.size) return '文件为空，请重新选择。'
  if (file.size > MAX_FILE_BYTES) return '文件超过 8MB 上限，请压缩或拆分后上传。'
  return ''
}

export function createFileForm(file, user, scene, caption) {
  if (!user.trim()) throw new Error('请先填写用户 ID。')
  const error = validateFile(file)
  if (error) throw new Error(error)
  const form = new FormData()
  form.append('file', file)
  form.append('user_id', user.trim())
  if (scene.trim()) form.append('scene_id', scene.trim())
  if (caption.trim()) form.append('caption', caption.trim())
  return form
}

export function createSourceRequest(source, fields, user, scene) {
  if (!user.trim()) throw new Error('请先填写用户 ID。')
  const required = (key, label) => {
    const value = String(fields[key] ?? '').trim()
    if (!value) throw new Error(`请填写${label}。`)
    return value
  }
  let payload
  if (source === 'tool_result') {
    payload = { tool_name: required('tool_name', '工具名称'), status: fields.status || 'failed',
      output: required('output', '工具输出'), confidence: 0.8 }
    if (fields.action_id?.trim()) payload.action_id = fields.action_id.trim()
  } else if (source === 'user_behavior') {
    const count = Number(fields.occurrence_count)
    if (!Number.isSafeInteger(count) || count < 1) throw new Error('出现次数必须为正整数。')
    payload = { action: required('action', '行为'), target: required('target', '操作对象'), occurrence_count: count, confidence: 0.7 }
  } else if (source === 'manual_config') {
    if (fields.scope === 'scene' && !scene.trim()) throw new Error('选择当前场景时，请填写场景名称。')
    payload = { key: required('key', '配置键'), value: required('value', '配置值'), scope: fields.scope || 'global', confidence: 0.9 }
  } else if (source === 'conversation') {
    payload = { content: required('content', '对话内容'), role: 'user', confidence: 0.8 }
  } else throw new Error('请选择支持的数据来源。')
  return { user_id: user.trim(), source, scene_id: scene.trim() || null, payload }
}

export function resultReason(result) {
  return result.reasons?.join('；') || ({ duplicate: '这条内容刚刚已接收，无需重复写入。',
    empty_content: '未识别到有效内容，请补充说明后重试。', low_quality: '有效信息不足，请补充可复用的内容。' }[result.reject_code]) || result.reject_code || '未通过校验'
}

export function extractionLabel(metadata = {}) {
  if (metadata.vision_used) return '视觉模型识别'
  if (metadata.ocr_used) return 'OCR 文字识别'
  if ((metadata.char_count === 0 || metadata.extract_status === 'empty') && metadata.caption) return '使用用户说明（未提取到正文）'
  if (metadata.file_kind === 'pdf' || metadata.file_kind === 'docx') return '文档文字提取'
  return '按来源清洗与标准化'
}

export async function requestApi(path, options = {}, timeout = 180000) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeout)
  try {
    const headers = authHeaders(options.headers)
    // Multipart boundaries belong to the browser, never set JSON for FormData.
    if (!(options.body instanceof FormData) && options.body != null && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
    const response = await fetch(`/api${path}`, { ...options, headers, signal: controller.signal })
    if (!response.ok) {
      let detail
      try { detail = (await response.json()).detail } catch {}
      if (Array.isArray(detail)) detail = detail.map(item => item.msg).join('；')
      throw new Error(typeof detail === 'string' ? detail : `请求失败（${response.status}）`)
    }
    return await response.json()
  } catch (error) {
    if (error.name === 'AbortError') throw new Error(`请求超过 ${Math.ceil(timeout / 1000)} 秒，后端可能仍在处理。请先同步记忆确认结果，再决定是否重试。`)
    throw error
  } finally { clearTimeout(timer) }
}
