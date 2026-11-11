import { computed, createApp, nextTick, onMounted, ref } from 'vue'
import './style.css'
import IngestionPanel from './IngestionPanel.js'
import { authHeaders, requestApi } from './ingestion.js'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

// 把模型 markdown 文本安全渲染为 HTML（DOMPurify 消毒防 XSS）。流式场景下由 scheduleRender
// 节流调用，避免每个 token 都对整篇重解析（O(n^2) 卡顿）。
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A' && node.getAttribute('href')) {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})
const renderMarkdown = (src) => {
  if (!src) return ''
  return DOMPurify.sanitize(marked.parse(src, { gfm: true, breaks: true }))
}

const demoPreferences = [
  { id: 'pref-language-v2', key: 'response_language', value: '默认使用中文回答，并优先给出可执行步骤。', scene: 'coding', version: 2, active: true, confidence: 0.98 },
  { id: 'pref-language-v1', key: 'response_language', value: '使用中文进行回答。', scene: 'coding', version: 1, active: false, confidence: 0.91 },
  { id: 'pref-download', key: 'download_path', value: '下载文件默认保存到 D:/Downloads。', scene: 'os_operation', version: 1, active: true, confidence: 0.96 },
  { id: 'pref-code', key: 'programming_style', value: '代码示例偏好 Python，并添加必要注释。', scene: 'coding', version: 3, active: true, confidence: 0.94 },
]

const demoMemories = [
  { id: 'tool-024', type: 'knowledge', layer: 'long', content: 'pip 安装依赖超时：可使用清华镜像。', source: 'tool_result', scene: 'coding' },
  { id: 'doc-007', type: 'knowledge', layer: 'long', content: '麒麟系统缺少 pip 时，可先安装 python3-pip。', source: 'document', scene: 'os_operation' },
  { id: 'ctx-015', type: 'knowledge', layer: 'long', content: '项目后端使用 FastAPI 提供记忆摄取、检索、偏好及遗忘接口。', source: 'conversation', scene: 'coding' },
]

const demoStats = () => ({
  total_count: 18,
  total_preference_count: 6,
  total_knowledge_count: 12,
  layers: {
    short: { count: 3, capacity: 20 },
    mid: { count: 5, capacity: 50 },
    long: { count: 4 },
    milvus: { count: 6 },
  },
})

const initialMessages = [
  { id: 1, role: 'assistant', content: '你好，我是麒忆助理。你可以直接问我问题，也可以告诉我需要长期记住或遗忘的内容。', time: '刚刚' },
  { id: 2, role: 'user', content: '以后请用中文回答，并优先给出可以直接执行的步骤。', time: '刚刚' },
  { id: 3, role: 'assistant', content: '记住了。我会默认使用中文，并把可执行步骤放在前面。右侧已经同步生成的偏好记忆。', time: '刚刚' },
]

createApp({
  components: { IngestionPanel },
  setup() {
    const user = ref('demo_user')
    // Draft for the header field so typing does not sync / disable the input mid-keystroke.
    const userDraft = ref(user.value)
    const live = ref(false)
    const syncing = ref(false)
    const sending = ref(false)
    const ingesting = ref(false)
    const sessionKey = ref(0)
    const modelSlow = ref(false)
    const activeTab = ref('overview')
    const draft = ref('')
    const agentHistory = ref([])
    const useMemory = ref(true)
    const saveMemory = ref(true)
    const newChat = () => { agentHistory.value = []; messages.value = [] }
    const messages = ref([...initialMessages])
    const memories = ref([...demoMemories])
    const preferences = ref([...demoPreferences])
    const profile = ref({ features: [] })
    const stats = ref(demoStats())
    const latency = ref('演示数据')
    const lastSynced = ref('尚未连接')
    const events = ref([
      { id: 1, type: 'preference', title: '识别到回答偏好', detail: 'response_language · v2', time: '刚刚' },
      { id: 2, type: 'memory', title: '沉淀项目上下文', detail: 'conversation · long', time: '2 分钟前' },
    ])
    const forgetInstruction = ref('')
    const forgetResult = ref('等待遗忘指令…')
    const toast = ref('')
    const chatScroll = ref(null)

    const tabs = [
      ['overview', '总览'],
      ['ingestion', '接入'],
      ['preferences', '偏好'],
      ['memories', '记忆'],
      ['forget', '遗忘'],
    ]

    const activePreferences = computed(() => {
      const features = profile.value?.features || []
      if (features.length) {
        return features.map((item) => ({
          id: item.feature_id || item.preference_key,
          key: item.preference_key,
          value: item.value,
          scene: item.scene_id || item.scope || 'global',
          version: item.version || 1,
          active: true,
          confidence: item.confidence ?? 0,
        }))
      }
      return preferences.value.filter((item) => item.active)
    })

    const statusText = computed(() => live.value ? '实时 API 已连接' : '演示数据模式')

    const tell = (message) => {
      toast.value = message
      window.clearTimeout(tell.timer)
      tell.timer = window.setTimeout(() => { toast.value = '' }, 2600)
    }

    const fetchWithTimeout = requestApi
    const call = (path, options, timeout) => {
      if (!live.value) throw new Error('当前为演示数据模式')
      return requestApi(path, options, timeout)
    }

    const mapPreference = (item) => ({
      id: item.id ?? item.memory_id,
      key: item.metadata?.preference_key || '未命名偏好',
      value: item.metadata?.preference_value ?? item.content,
      scene: item.scene_id || item.metadata?.task_type || 'global',
      version: item.version ?? item.metadata?.version ?? 1,
      active: item.metadata?.is_latest ?? true,
      confidence: item.metadata?.confidence ?? item.confidence ?? 0,
    })

    const mapMemory = (item) => ({
      id: item.id ?? item.memory_id,
      type: item.memory_type || 'knowledge',
      layer: item.layer || 'long',
      content: item.content,
      source: item.metadata?.source || item.metadata?.source_type,
      scene: item.scene_id || item.metadata?.task_type,
    })

    const now = () => new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(new Date())

    // Clear user-specific and demo content before displaying a live session.
    const clearLiveSession = () => {
      agentHistory.value = []
      sessionKey.value++
      memories.value = []
      preferences.value = []
      profile.value = { features: [] }
      stats.value = {
        total_count: 0, total_preference_count: 0, total_knowledge_count: 0,
        layers: { short: { count: 0 }, mid: { count: 0 }, long: { count: 0 }, milvus: { count: 0 } },
      }
      messages.value = [{ ...initialMessages[0] }]
      events.value = []
      latency.value = '尚未检索'
      lastSynced.value = '尚未同步'
      forgetInstruction.value = ''
      forgetResult.value = '等待遗忘指令…'
    }

    const pushEvent = (type, title, detail) => {
      events.value.unshift({ id: Date.now() + Math.random(), type, title, detail, time: now() })
      events.value = events.value.slice(0, 12)
    }

    const scrollToBottom = async () => {
      await nextTick()
      if (chatScroll.value) chatScroll.value.scrollTop = chatScroll.value.scrollHeight
    }

    // 流式 markdown 渲染：用 requestAnimationFrame 合并，最多每帧重解析一次，
    // 避免后端逐 token 推送时每 token 全量重解析造成的 O(n^2) 卡顿；滚动也并入此处。
    const renderRaf = { id: null, msg: null }
    const flushRender = () => {
      renderRaf.id = null
      const msg = renderRaf.msg
      renderRaf.msg = null
      if (msg) msg.html = renderMarkdown(msg.content)
      if (chatScroll.value) chatScroll.value.scrollTop = chatScroll.value.scrollHeight
    }
    const scheduleRender = (msg) => {
      renderRaf.msg = msg
      if (renderRaf.id == null) renderRaf.id = requestAnimationFrame(flushRender)
    }
    const renderNow = (msg) => {
      if (renderRaf.id != null && renderRaf.msg === msg) {
        cancelAnimationFrame(renderRaf.id)
        renderRaf.id = null
        renderRaf.msg = null
      }
      msg.html = renderMarkdown(msg.content)
    }

    const loadDashboard = async ({ quiet = false } = {}) => {
      if (!live.value) return
      if (!user.value.trim()) { syncing.value = false; return }
      syncing.value = true
      const encodedUser = encodeURIComponent(user.value.trim())
      const [statsResult, historyResult, profileResult] = await Promise.allSettled([
        call(`/stats/${encodedUser}`, {}, 30000),
        call(`/preferences/${encodedUser}?latest_only=false&top_k=100`, {}, 180000),
        call(`/preferences/${encodedUser}/profile`, {}, 30000),
      ])
      if (encodedUser !== encodeURIComponent(user.value.trim()) || !live.value) return
      if (statsResult.status === 'fulfilled') stats.value = statsResult.value
      if (historyResult.status === 'fulfilled') preferences.value = historyResult.value.items.map(mapPreference).sort((a, b) => b.version - a.version)
      if (profileResult.status === 'fulfilled') profile.value = profileResult.value.profile
      lastSynced.value = now()
      syncing.value = false
      if (!quiet) {
        const failed = [statsResult, historyResult, profileResult].filter((item) => item.status === 'rejected').length
        tell(failed ? `数据已刷新，${failed} 项接口暂时不可用` : '记忆面板已同步')
      }
    }

    const connect = async ({ quiet = false } = {}) => {
      if (sending.value || ingesting.value) return
      if (!user.value.trim()) return tell('请先填写用户 ID')
      if (live.value && !quiet) {
        sessionKey.value++
        live.value = false
        stats.value = demoStats()
        preferences.value = [...demoPreferences]
        profile.value = { features: [] }
        memories.value = [...demoMemories]
        lastSynced.value = '尚未连接'
        tell('已切换为演示数据模式')
        return
      }
      syncing.value = true
      const attempts = quiet ? 3 : 1
      const timeout = quiet ? 10000 : 15000
      let lastError = null
      try {
        for (let i = 0; i < attempts; i += 1) {
          try {
            await fetchWithTimeout('/health', {}, timeout)
            clearLiveSession()
            live.value = true
            await loadDashboard({ quiet: true })
            if (!quiet) tell('已连接本地 API')
            lastError = null
            break
          } catch (error) {
            lastError = error
            live.value = false
            if (i + 1 < attempts) {
              await new Promise((resolve) => window.setTimeout(resolve, 800))
            }
          }
        }
        if (lastError && !quiet) tell(`连接失败：${lastError.message}`)
      } finally {
        syncing.value = false
      }
    }

    const streamChat = async (payload, handlers, timeout = 240000) => {
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), timeout)
      try {
        const response = await fetch('/api/agent/chat/stream', {
          method: 'POST',
          headers: authHeaders({ 'Content-Type': 'application/json' }),
          body: JSON.stringify(payload),
          signal: controller.signal,
        })
        if (!response.ok) {
          let detail = ''
          try { detail = (await response.json()).detail } catch {}
          throw new Error(typeof detail === 'string' && detail ? detail : `请求失败（${response.status}）`)
        }
        const reader = response.body.getReader()
        const decoder = new TextDecoder('utf-8')
        let buffer = ''
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          let sep
          while ((sep = buffer.indexOf('\n\n')) !== -1) {
            const raw = buffer.slice(0, sep)
            buffer = buffer.slice(sep + 2)
            const dataLine = raw.split('\n').find((l) => l.startsWith('data:'))
            if (!dataLine) continue
            const data = dataLine.slice(5).trim()
            if (!data) continue
            let event
            try { event = JSON.parse(data) } catch { continue }
            handlers(event)
          }
        }
      } catch (error) {
        if (error.name === 'AbortError') throw new Error(`请求超过 ${Math.ceil(timeout / 1000)} 秒，后端可能仍在处理。`)
        throw error
      } finally { clearTimeout(timer) }
    }

    const sendMessage = async () => {
      if (!user.value.trim()) return tell('请先填写用户 ID')
      const text = draft.value.trim()
      if (!text || sending.value || ingesting.value || syncing.value) return
      messages.value.push({ id: Date.now(), role: 'user', content: text, time: now() })
      draft.value = ''
      sending.value = true
      modelSlow.value = false
      await scrollToBottom()

      if (!live.value) {
        await new Promise((resolve) => window.setTimeout(resolve, 350))
        const reply = text.includes('忘')
          ? '你可以在右侧打开“遗忘”，确认需要删除的范围。演示模式不会伪造删除成功。'
          : '我会结合已保存的偏好和记忆来回答。当前是演示模式，连接本地 API 后将显示真实召回与写入结果。'
        messages.value.push({ id: Date.now() + 1, role: 'assistant', content: reply, time: now() })
        pushEvent('conversation', '完成一轮演示对话', '连接 API 后将实时写入')
        sending.value = false
        await scrollToBottom()
        return
      }

      const assistantMsg = { id: Date.now() + 1, role: 'assistant', content: '', steps: [], time: now() }
      let pushed = false
      const ensureMsg = () => { if (!pushed) { messages.value.push(assistantMsg); pushed = true } }
      const slowTimer = window.setTimeout(() => { modelSlow.value = true }, 2500)
      try {
        await streamChat({
          user_id: user.value.trim(), message: text,
          history: agentHistory.value.slice(-20),
          use_memory: useMemory.value, save_memory: saveMemory.value,
        }, (event) => {
          if (event.type === 'token') {
            ensureMsg()
            assistantMsg.content += event.text
            scheduleRender(assistantMsg)
          } else if (event.type === 'tool_start') {
            ensureMsg(); assistantMsg.steps.push({ name: event.name, args: event.args || {}, status: 'running', result: '' })
            scrollToBottom()
          } else if (event.type === 'tool_result') {
            const step = assistantMsg.steps.find((s) => s.name === event.name && s.status === 'running')
              || assistantMsg.steps[assistantMsg.steps.length - 1]
            if (step) { step.status = event.ok ? 'done' : 'failed'; step.result = event.content || '' }
            scrollToBottom()
          } else if (event.type === 'done') {
            ensureMsg()
            if (event.answer) assistantMsg.content = assistantMsg.content || event.answer
            memories.value = (event.memories || []).map(mapMemory)
            latency.value = `${Math.round(event.recall_ms)} ms`
            pushEvent('recall', '完成智能体回答', `${(event.memories || []).length} 条记忆 · 总耗时 ${Math.round(event.latency_ms)} ms`)
            const write = event.memory_write
            if (write && write.status === 'accepted') pushEvent('memory', '本轮内容已接收', write.memory_id || '已进入记忆处理流程')
            if (write && write.status === 'rejected') pushEvent('memory', '本轮内容未写入', write.reasons?.join('；') || '未通过记忆策略')
            for (const warning of event.warnings || []) pushEvent('error', '记忆处理提示', warning)
            if (event.reset_history) pushEvent('forget', '遗忘已处理，聊天上下文已清空', `${(event.deleted_ids || []).length} 条记忆已删除`)
            agentHistory.value = event.reset_history
              ? []
              : [...agentHistory.value, { role: 'user', content: text }, { role: 'assistant', content: assistantMsg.content }].slice(-20)
            if (event.reset_history) messages.value = []
            renderNow(assistantMsg)
            scrollToBottom()
          } else if (event.type === 'error') {
            ensureMsg()
            assistantMsg.content = assistantMsg.content || `智能体调用失败：${event.message}`
            pushEvent('error', '智能体调用失败', event.message)
            renderNow(assistantMsg)
            scrollToBottom()
          }
        })
        await loadDashboard({ quiet: true })
      } catch (error) {
        ensureMsg()
        assistantMsg.content = assistantMsg.content || `智能体调用失败：${error.message}`
        pushEvent('error', '智能体调用失败', error.message)
      } finally {
        window.clearTimeout(slowTimer)
        sending.value = false
        modelSlow.value = false
        await scrollToBottom()
      }
    }

    const handleComposerKeydown = (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault()
        sendMessage()
      }
    }

    const usePrompt = (prompt) => {
      draft.value = prompt
      nextTick(() => document.querySelector('.chat-composer textarea')?.focus())
    }

    const openIngestion = async () => {
      activeTab.value = 'ingestion'
      await nextTick()
      if (window.matchMedia('(max-width: 1050px)').matches) document.querySelector('.memory-panel')?.scrollIntoView({ behavior: 'smooth' })
    }

    const prepareForget = (item, kind = 'memory') => {
      forgetInstruction.value = kind === 'preference' ? `忘掉关于 ${item.key} 的偏好` : `忘掉记忆 ${item.id}`
      activeTab.value = 'forget'
    }

    const forget = async () => {
      if (sending.value || syncing.value || ingesting.value) return
      if (!user.value.trim()) return tell('请先填写用户 ID')
      const instruction = forgetInstruction.value.trim()
      if (!instruction) return tell('请先输入需要遗忘的内容')
      if (!live.value) {
        forgetResult.value = `演示模式不会模拟删除成功。\n连接 API 后将发送：${instruction}`
        return tell('请先连接本地 API')
      }
      syncing.value = true
      try {
        const result = await call('/forget', { method: 'POST', body: JSON.stringify({ user_id: user.value.trim(), instruction }) }, 180000)
        forgetResult.value = `已处理：${result.instruction}\n删除数量：${result.count}\n删除 ID：${result.deleted_ids.length ? result.deleted_ids.join(', ') : '无'}`
        newChat()
        const deleted = new Set(result.deleted_ids)
        memories.value = memories.value.filter((item) => !deleted.has(item.id))
        pushEvent('forget', '执行安全遗忘', `${result.count} 条记忆已删除`)
        await loadDashboard({ quiet: true })
        tell('遗忘结果已同步')
      } catch (error) {
        forgetResult.value = `遗忘失败：${error.message}`
        pushEvent('error', '安全遗忘失败', error.message)
        tell('遗忘请求失败')
      } finally {
        syncing.value = false
      }
    }

    const onIngestionCompleted = (item) => {
      if (!live.value || item.user !== user.value.trim()) return
      const accepted = item.status === 'accepted'
      pushEvent(accepted ? 'memory' : 'error', accepted ? '多源数据已写入' : '多源数据未确认写入', item.label + ' · ' + item.note)
      messages.value.push({ id: Date.now() + Math.random(), role: 'assistant',
        content: item.label + '：' + item.note + (item.promotion ? '\n' + item.promotion : ''), time: now() })
      scrollToBottom()
    }
    const onIngestionBusy = async (value) => {
      ingesting.value = value
      if (!value && live.value) await loadDashboard({ quiet: true })
    }

    const commitUser = async ({ quiet = false } = {}) => {
      if (sending.value || ingesting.value || syncing.value) return
      const next = userDraft.value.trim()
      if (!next) {
        tell('请先填写用户 ID')
        userDraft.value = user.value
        return
      }
      if (next === user.value.trim()) {
        if (live.value) await loadDashboard({ quiet })
        return
      }
      user.value = next
      if (live.value) {
        clearLiveSession()
        await loadDashboard({ quiet })
      } else {
        sessionKey.value++
      }
    }

    const onUserKeydown = (event) => {
      if (event.key === 'Enter') {
        event.preventDefault()
        commitUser()
      }
    }

    onMounted(() => {
      scrollToBottom()
      connect({ quiet: true })
    })

    return { useMemory, saveMemory, newChat, user, userDraft, live, syncing, sending, ingesting, sessionKey, onIngestionBusy, onIngestionCompleted, modelSlow, activeTab, draft, messages, memories, preferences, stats, latency, lastSynced, events, forgetInstruction, forgetResult, toast, chatScroll, tabs, activePreferences, statusText, connect, loadDashboard, commitUser, onUserKeydown, sendMessage, handleComposerKeydown, openIngestion, usePrompt, prepareForget, forget, renderMarkdown }
  },
  template: `
    <div class="app-shell">
      <header class="topbar">
        <div class="brand-mark"><span>◇</span><div><b>麒忆智脑</b><small>OS Agent Memory</small></div></div>
        <div class="top-actions">
          <label>用户 <input v-model.trim="userDraft" data-testid="user-input" aria-label="用户 ID" title="改完后按回车或点「同步」再切换用户" @keydown="onUserKeydown" @blur="commitUser({ quiet: true })" /></label>
          <button class="sync-button" data-testid="sync-button" :disabled="syncing || ingesting || sending" @click="commitUser()">{{ syncing ? '同步中…' : '↻ 同步' }}</button>
          <button class="connection-button" data-testid="connection-button" :class="{ live }" :disabled="sending || syncing || ingesting" @click="connect()"><i></i>{{ statusText }}</button>
        </div>
      </header>

      <main class="dual-layout">
        <section class="chat-panel">
          <div class="panel-heading">
            <div><span class="eyebrow">MEMORY ASSISTANT</span><h1>和麒忆助理对话</h1><p>对话会参与记忆召回，并在右侧实时形成偏好与记忆记录。</p></div>
            <div class="assistant-avatar">麒</div>
          </div>
          <div class="chat-stream" ref="chatScroll">
            <div class="message-row" v-for="message in messages" :key="message.id" :class="message.role">
              <div class="avatar">{{ message.role === 'assistant' ? '麒' : 'Z' }}</div>
              <div class="message-wrap">
                <div class="message-meta"><b>{{ message.role === 'assistant' ? '麒忆助理' : '你' }}</b><span>{{ message.time }}</span></div>
                <div class="bubble" v-html="message.html || renderMarkdown(message.content)"></div>
                <div class="tool-steps" v-if="message.steps && message.steps.length">
                  <div class="tool-step" v-for="step in message.steps" :key="step.name + '-' + step.status + '-' + (step.result || '').length">
                    <span class="tool-dot" :class="step.status"></span>
                    <span class="tool-name">{{ step.name }}</span>
                    <code class="tool-args" v-if="step.args && Object.keys(step.args).length">{{ JSON.stringify(step.args) }}</code>
                    <span class="tool-state" :class="step.status">{{ step.status === 'running' ? '运行中' : step.status === 'done' ? '完成' : '失败' }}</span>
                    <pre class="tool-output" v-if="step.result">{{ step.result }}</pre>
                  </div>
                </div>
              </div>
            </div>
            <div class="message-row assistant" v-if="sending && !(messages.length && messages[messages.length - 1].role === 'assistant')">
              <div class="avatar">麒</div><div class="message-wrap"><div class="message-meta"><b>麒忆助理</b><span>处理中</span></div><div class="bubble thinking"><i></i><i></i><i></i><span>{{ modelSlow ? '正在等待模型回答和记忆处理…' : '正在检索记忆并生成回答…' }}</span></div></div>
            </div>
          </div>
          <div class="prompt-chips">
            <label><input type="checkbox" v-model="useMemory" :disabled="sending" /> 使用记忆</label>
            <label><input type="checkbox" v-model="saveMemory" :disabled="sending" /> 保存本轮内容</label>
            <button @click="newChat" :disabled="sending">新对话</button>
          </div>
          <div class="prompt-chips">
            <button @click="usePrompt('根据我的偏好，给出一个 Python 项目搭建方案')">项目搭建建议</button>
            <button @click="usePrompt('你记得我的哪些使用习惯？')">查看我的习惯</button>
            <button @click="usePrompt('以后回答请更加简洁')">新增回答偏好</button>
          </div>
          <form class="chat-composer" @submit.prevent="sendMessage">
            <textarea v-model="draft" data-testid="chat-input" @keydown="handleComposerKeydown" placeholder="输入问题，或告诉我需要记住的内容…" aria-label="对话输入"></textarea>
            <div>
              <span>Enter 发送 · 可上传 PDF / Word / 图片</span>
              <div class="composer-actions">
                <button type="button" class="file-button" data-testid="open-ingestion" @click="openIngestion">＋ 多源接入</button>
                <button data-testid="chat-send" :disabled="!draft.trim() || sending || syncing || ingesting">{{ sending ? '处理中' : '发送 ↑' }}</button>
              </div>
            </div>
          </form>
        </section>

        <aside class="memory-panel">
          <div class="memory-header">
            <div><span class="eyebrow">LIVE MEMORY</span><h2>实时记忆面板</h2></div>
            <div class="live-state"><i :class="{ live }"></i><span>{{ live ? '实时同步' : '演示模式' }}<small>上次同步 {{ lastSynced }}</small></span></div>
          </div>
          <nav class="memory-tabs" aria-label="记忆面板导航">
            <button v-for="tab in tabs" :key="tab[0]" :data-testid="'tab-' + tab[0]" :class="{ active: activeTab === tab[0] }" @click="activeTab = tab[0]">{{ tab[1] }}</button>
          </nav>
          <div class="memory-content">
            <IngestionPanel v-show="activeTab === 'ingestion'" :key="sessionKey" :user="user" :live="live" :blocked="sending || syncing" @busy="onIngestionBusy" @completed="onIngestionCompleted" />
            <template v-if="activeTab === 'overview'">
              <div class="stat-grid"><div><span>总记忆</span><b>{{ stats.total_count }}</b><small>跨层统一视图</small></div><div><span>偏好记忆</span><b>{{ stats.total_preference_count }}</b><small>版本可追溯</small></div><div><span>知识记忆</span><b>{{ stats.total_knowledge_count }}</b><small>可供语义召回</small></div></div>
              <section class="side-section"><div class="section-title"><h3>记忆分层</h3><span>{{ live ? '来自 /stats' : '演示数据' }}</span></div><div class="layer-row"><span>短期记忆</span><div><i :style="{ width: Math.min(100, stats.layers.short.count / (stats.layers.short.capacity || 20) * 100) + '%' }"></i></div><b>{{ stats.layers.short.count }} / {{ stats.layers.short.capacity || '∞' }}</b></div><div class="layer-row"><span>中期记忆</span><div><i :style="{ width: Math.min(100, stats.layers.mid.count / (stats.layers.mid.capacity || 50) * 100) + '%' }"></i></div><b>{{ stats.layers.mid.count }} / {{ stats.layers.mid.capacity || '∞' }}</b></div><div class="layer-row"><span>长期待入库</span><div><i :style="{ width: Math.min(100, stats.layers.long.count * 10) + '%' }"></i></div><b>{{ stats.layers.long.count }}</b></div><div class="layer-row"><span>知识库</span><div><i :style="{ width: Math.min(100, stats.layers.milvus.count * 8) + '%' }"></i></div><b>{{ stats.layers.milvus.count }}</b></div></section>
              <section class="side-section"><div class="section-title"><h3>最近活动</h3><span>自动刷新</span></div><div class="event-item" v-for="event in events" :key="event.id"><i :class="event.type">{{ event.type === 'forget' ? '⌫' : event.type === 'preference' ? '◌' : event.type === 'error' ? '!' : '◇' }}</i><span><b>{{ event.title }}</b><small>{{ event.detail }}</small></span><time>{{ event.time }}</time></div></section>
            </template>
            <template v-else-if="activeTab === 'preferences'">
              <div class="section-title sticky-title"><h3>当前生效偏好</h3><span>{{ activePreferences.length }} 项</span></div><div class="empty-state" v-if="!activePreferences.length">当前用户还没有生效偏好</div><article class="preference-card" data-testid="preference-card" v-for="item in activePreferences" :key="item.id || item.key"><div><code>{{ item.key }}</code><span>v{{ item.version }}</span></div><p>{{ item.value }}</p><footer><small>{{ item.scene }} · 置信度 {{ Math.round((item.confidence || 0) * 100) }}%</small><button @click="prepareForget(item, 'preference')">准备遗忘</button></footer></article><div class="section-title history-title"><h3>版本历史</h3><span>{{ preferences.length }} 条</span></div><div class="history-item" v-for="item in preferences" :key="(item.id || item.key) + '-' + item.version"><b>v{{ item.version }}</b><span><strong>{{ item.key }}</strong><small>{{ item.active ? '当前版本' : '已被替换' }} · {{ item.value }}</small></span></div>
            </template>
            <template v-else-if="activeTab === 'memories'">
              <div class="section-title sticky-title"><h3>本轮召回记忆</h3><span>{{ latency }} · {{ memories.length }} 条</span></div><div class="empty-state" v-if="!memories.length">发送一条消息后，这里会显示真实召回结果</div><article class="memory-card" data-testid="memory-card" v-for="item in memories" :key="item.id"><div><code>{{ item.type }}</code><span>{{ item.layer }}</span><small>{{ item.id }}</small></div><p>{{ item.content }}</p><footer><span>{{ item.scene || '通用场景' }} · {{ item.source || '未知来源' }}</span><button @click="prepareForget(item)">准备遗忘</button></footer></article>
            </template>
            <template v-else-if="activeTab === 'forget'">
              <div class="forget-intro"><span>SECURE FORGETTING</span><h3>安全遗忘</h3><p>用自然语言描述需要删除的记忆。只有后端真实删除成功后，右侧数据才会更新。</p></div><textarea class="forget-input" data-testid="forget-input" v-model="forgetInstruction" placeholder="例如：忘掉关于 Java 的偏好"></textarea><button class="danger-button" data-testid="forget-submit" :disabled="syncing || ingesting || sending" @click="forget">{{ syncing ? '处理中…' : '执行遗忘' }}</button><pre class="forget-result" data-testid="forget-result">{{ forgetResult }}</pre><div class="safety-note"><b>隐私提示</b><span>敏感信息由后端安全模块解析和清理；前端不会伪造成功结果。</span></div>
            </template>
          </div>
        </aside>
      </main>
      <Transition><div class="toast" v-if="toast">{{ toast }}</div></Transition>
    </div>
  `,
}).mount('#app')
