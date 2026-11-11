import { computed, onBeforeUnmount, ref } from 'vue'
import { FILE_ACCEPT, sources, validateFile, createFileForm, createSourceRequest, resultReason, extractionLabel, requestApi } from './ingestion.js'
import './ingestion.css'

export default {
  props: { user: String, live: Boolean, blocked: Boolean },
  emits: ['busy', 'completed'],
  setup(props, { emit }) {
    const source = ref('document')
    const scene = ref('document_upload')
    const queue = ref([])
    const fields = ref({ status: 'failed', occurrence_count: 5, scope: 'global' })
    const working = ref(false)
    const phase = ref('')
    const slow = ref(false)
    const error = ref('')
    const results = ref([])
    const dragging = ref(false)
    const fileInput = ref(null)
    let sequence = 0
    let alive = true
    onBeforeUnmount(() => { alive = false })
    const locked = computed(() => props.blocked || working.value)
    const pending = computed(() => queue.value.filter(item => item.state === 'pending'))
    const canSubmit = computed(() => props.live && props.user.trim() && !locked.value && (source.value !== 'document' || pending.value.length > 0))
    const sourceName = id => sources.find(item => item.id === id)?.label || id
    const fileSize = size => size < 1024 * 1024 ? `${Math.ceil(size / 1024)} KB` : `${(size / 1024 / 1024).toFixed(1)} MB`
    const changeSource = () => {
      error.value = ''
      scene.value = source.value === 'document' ? 'document_upload' : source.value === 'manual_config' ? '' : 'coding'
    }
    const addFiles = files => {
      if (locked.value) return
      error.value = ''
      for (const file of Array.from(files || [])) {
        if (queue.value.length >= 10) { error.value = '队列最多 10 个文件，请移除已完成项后再添加。'; break }
        const issue = validateFile(file)
        queue.value.push({ id: ++sequence, file, caption: '', state: issue ? 'invalid' : 'pending', note: issue })
      }
    }
    const selectFiles = event => { addFiles(event.target.files); event.target.value = '' }
    const dropFiles = event => { dragging.value = false; addFiles(event.dataTransfer.files) }
    const sample = () => {
      const examples = {
        tool_result: { tool_name: 'pip', status: 'failed', output: '安装依赖时连接超时，切换国内镜像后成功。', action_id: '' },
        user_behavior: { action: '打开', target: 'VSCode', occurrence_count: 5 },
        manual_config: { key: 'reply_language', value: '中文', scope: 'global' },
        conversation: { content: '以后请用中文回答，并优先给出可执行的步骤。' },
      }
      fields.value = { ...examples[source.value] }
    }

    const processOne = async (path, body, label, sourceId, uid) => {
      phase.value = `正在处理 ${label}`
      const item = { id: ++sequence, label, source: sourceId, status: 'processing', result: null, note: '', promotion: '' }
      results.value.unshift(item)
      results.value = results.value.slice(0, 30)
      // Use the reactive instance so incremental status changes render immediately.
      const row = results.value[0]
      try {
        const result = await requestApi(path, { method: 'POST', body })
        if (typeof result.accepted !== 'boolean') throw new Error('服务端返回格式不完整，请同步记忆确认是否已写入。')
        row.result = result
        row.status = result.accepted ? 'accepted' : 'rejected'
        row.note = result.accepted ? (result.updated_existing ? '已更新既有记忆' : '已接收并写入记忆') : resultReason(result)
        if (result.accepted && result.memory_type === 'knowledge') {
          phase.value = '正在同步长期记忆…'
          try {
            const promoted = await requestApi(`/consolidate/${encodeURIComponent(uid)}`, { method: 'POST' })
            row.promotion = promoted.stored_memory_ids?.includes(result.memory_id)
              ? '已进入长期记忆，可在对话中检索。'
              : '已触发记忆整理；本条是否进入长期库，以后续检索结果为准。'
          } catch (err) {
            row.promotion = `内容已接收，但长期记忆同步失败：${err.message}`
          }
        }
      } catch (err) {
        row.status = 'error'
        row.note = `${err.message} 若请求已发出，服务端可能已接收，请同步后再重试。`
      }
      if (alive) emit('completed', { ...row, user: uid })
      return row
    }

    const submit = async () => {
      if (!canSubmit.value) return
      const uid = props.user.trim()
      const sceneId = scene.value
      const sourceId = source.value
      let request
      try {
        if (sourceId !== 'document') request = createSourceRequest(sourceId, fields.value, uid, sceneId)
      } catch (err) { error.value = err.message; return }
      working.value = true
      emit('busy', true)
      error.value = ''
      slow.value = false
      const timer = setTimeout(() => { slow.value = true }, 2500)
      try {
        if (sourceId === 'document') {
          const batch = [...pending.value]
          for (const entry of batch) {
            if (!alive) break
            entry.state = 'processing'
            const row = await processOne('/ingest/file', createFileForm(entry.file, uid, sceneId, entry.caption), entry.file.name, sourceId, uid)
            entry.state = row.status
            entry.note = row.note
          }
        } else {
          await processOne('/ingest', JSON.stringify(request), sourceName(sourceId), sourceId, uid)
        }
      } catch (err) { error.value = err.message }
      finally {
        clearTimeout(timer)
        working.value = false
        slow.value = false
        if (alive) emit('busy', false)
      }
    }
    const statusLabel = status => ({ pending: '待上传', processing: '处理中', invalid: '文件不符合要求', accepted: '已接收', rejected: '未接收', error: '请求异常' }[status])
    return { sources, FILE_ACCEPT, source, scene, fields, queue, pending, locked, working, phase, slow, error, results, dragging, fileInput, canSubmit,
      selectFiles, dropFiles, changeSource, sample, submit, sourceName, fileSize, statusLabel, extractionLabel }
  },
  template: `
    <section class="ingestion-panel" aria-label="多源数据接入">
      <div class="section-title"><h3>多源数据接入</h3><span>统一写入 · 实时反馈</span></div>
      <p class="ingest-intro">把文档、图片、工具经验和使用习惯交给记忆。处理结果会同步到对话和活动记录。</p>
      <p class="ingest-notice" v-if="!live">当前为演示模式。可以准备数据，连接 API 后才能提交；文件尚未上传。</p>
      <form @submit.prevent="submit" class="ingest-form">
        <fieldset :disabled="locked">
          <div class="ingest-fields">
            <label>数据来源<select v-model="source" @change="changeSource" data-testid="ingest-source"><option v-for="item in sources" :key="item.id" :value="item.id">{{ item.label }}</option></select></label>
            <label>场景（选填）<input v-model="scene" maxlength="128" placeholder="如 coding" data-testid="ingest-scene" /></label>
          </div>
          <template v-if="source === 'document'">
            <div class="file-drop" :class="{ dragging }" @dragover.prevent="dragging = true" @dragleave.prevent="dragging = false" @drop.prevent="dropFiles">
              <span class="drop-symbol" aria-hidden="true">↥</span><b>把文件拖到这里</b>
              <button type="button" class="ingest-secondary" @click="fileInput.click()">选择文件</button>
              <input ref="fileInput" class="visually-hidden" type="file" multiple :accept="FILE_ACCEPT" @change="selectFiles" data-testid="ingest-files" aria-label="上传文件" />
              <small>PDF / Word（.docx）/ 图片 · 每个不超过 8MB</small>
            </div>
            <p class="ingest-hint">支持多选，逐个处理。扫描件请补充内容说明；图片会按后端能力尝试视觉识别或 OCR。</p>
            <article class="upload-item" v-for="item in queue" :key="item.id" :data-state="item.state" data-testid="upload-item">
              <header><div><b>{{ item.file.name }}</b><small>{{ fileSize(item.file.size) }} · {{ statusLabel(item.state) }}</small></div><button type="button" class="ingest-text-button" :aria-label="'移除 ' + item.file.name" @click="queue = queue.filter(x => x.id !== item.id)">移除</button></header>
              <label v-if="item.state !== 'invalid'">内容说明（选填）<textarea v-model="item.caption" :disabled="item.state === 'accepted'" rows="2" placeholder="例如：截图中 pip 安装超时，切换镜像后解决。"></textarea></label>
              <p v-if="item.note" class="ingest-hint">{{ item.note }}</p>
              <button v-if="item.state === 'rejected' || item.state === 'error'" type="button" class="ingest-text-button" @click="item.state = 'pending'; item.note = ''">修改后加入待上传队列</button>
            </article>
          </template>
          <template v-else>
            <div class="source-description"><span>{{ sources.find(item => item.id === source)?.hint }}</span><button class="ingest-text-button" type="button" data-testid="ingest-sample" @click="sample">填入示例</button></div>
            <template v-if="source === 'tool_result'">
              <div class="ingest-fields"><label>工具名称<input v-model="fields.tool_name" placeholder="例如 pip" required /></label><label>执行状态<select v-model="fields.status"><option value="failed">失败</option><option value="completed">完成</option><option value="timeout">超时</option></select></label></div>
              <label>工具输出<textarea v-model="fields.output" rows="4" placeholder="粘贴关键输出、报错及解决方式" required></textarea></label>
              <label>事件编号（选填）<input v-model="fields.action_id" placeholder="同一事件重复投递时使用相同编号" /></label>
            </template>
            <template v-else-if="source === 'user_behavior'">
              <div class="ingest-fields"><label>行为<input v-model="fields.action" placeholder="例如 打开" required /></label><label>操作对象<input v-model="fields.target" placeholder="例如 VSCode" required /></label></div>
              <label>出现次数<input v-model.number="fields.occurrence_count" type="number" min="1" step="1" required /></label><p class="ingest-hint">填写真实出现次数。单次行为可能因记忆价值不足而不被接收。</p>
            </template>
            <template v-else-if="source === 'manual_config'">
              <label>配置键<input v-model="fields.key" placeholder="例如 reply_language" required /></label><label>配置值<input v-model="fields.value" placeholder="例如 中文" required /></label><label>作用范围<select v-model="fields.scope"><option value="global">全局</option><option value="scene">当前场景</option></select></label>
            </template>
            <label v-else>对话内容<textarea v-model="fields.content" rows="5" placeholder="输入希望记住的内容" required></textarea></label>
          </template>
        </fieldset>
        <p class="ingest-error" role="alert" v-if="error">{{ error }}</p>
        <div class="ingest-progress" v-if="working" role="status"><b>{{ phase }}</b><span>{{ slow ? '模型初始化或内容解析中，首次处理可能较慢，请耐心等待。' : '正在等待处理结果…' }}</span></div>
        <button class="ingest-submit" data-testid="ingest-submit" :disabled="!canSubmit">{{ working ? '处理中…' : source === 'document' ? '上传并写入记忆（' + pending.length + '）' : '写入记忆' }}</button>
      </form>
      <div class="section-title history-title"><h3>本次会话处理记录</h3><span>{{ results.length }} 条</span></div>
      <p class="empty-state" v-if="!results.length">提交后可查看提取正文、质量评分与处理原因。</p>
      <article v-for="item in results" :key="item.id" class="ingest-result" :data-state="item.status" data-testid="ingest-result" aria-live="polite">
        <header><b>{{ item.label }}</b><span>{{ statusLabel(item.status) }}</span></header>
        <p>{{ item.note || '正在等待处理结果…' }}</p>
        <p class="ingest-hint" v-if="item.promotion">{{ item.promotion }}</p>
        <template v-if="item.result">
          <div class="result-tags"><span>{{ sourceName(item.source) }}</span><span v-if="item.result.quality_score != null">质量 {{ Math.round(item.result.quality_score * 100) }}%</span><span v-if="item.result.processing_ms != null">处理 {{ Math.round(item.result.processing_ms) }} ms</span></div>
          <p v-if="item.source === 'document' && item.result.record">{{ extractionLabel(item.result.record.metadata) }}</p>
          <p class="ingest-hint" v-if="item.result.memory_id">记忆编号：{{ item.result.memory_id }}</p>
          <p class="ingest-hint" v-if="item.result.reject_code">原因代码：{{ item.result.reject_code }}</p>
          <details v-if="item.result.record?.content"><summary>查看处理后的正文</summary><pre>{{ item.result.record.content }}</pre></details>
          <details v-if="item.result.record?.metadata?.quality_components"><summary>质量评分明细</summary><dl><template v-for="(value, key) in item.result.record.metadata.quality_components" :key="key"><dt>{{ key }}</dt><dd>{{ value }}</dd></template></dl></details>
        </template>
      </article>
    </section>
  `,
}
