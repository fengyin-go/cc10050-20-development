<template>
  <section class="review-flow" data-testid="review-handoff-panel">
    <div class="review-flow-head">
      <div>
        <h3>转派审单演练：M72 / 候选 600g</h3>
        <p>同一原任务在甲、主管、乙之间交接；归属版本和幂等回执决定谁能读证据、写结论。</p>
      </div>
      <div class="review-controls">
        <label>
          当前身份
          <select :value="reviewSession.currentUser" data-testid="review-current-user" @change="changeUser">
            <option>甲</option>
            <option>乙</option>
            <option>主管</option>
          </select>
        </label>
        <button class="btn ghost" type="button" data-testid="reset-m72" @click="resetScenario">重置 M72 演练</button>
      </div>
    </div>

    <div class="review-grid">
      <article class="entry-card" data-testid="entry-jia" :data-completed="contextA?.completed" :data-can-submit="contextA?.can_submit">
        <h4>甲的原任务入口</h4>
        <p class="entry-state">
          <span>是否办结：<strong>{{ yesNo(contextA?.completed) }}</strong></span>
          <span>能否提交：<strong>{{ yesNo(contextA?.can_submit) }}</strong></span>
        </p>
        <p class="entry-message" data-testid="jia-context-message">{{ contextA?.message || '正在读取甲的入口状态…' }}</p>
        <div class="button-row">
          <button class="btn" type="button" data-testid="jia-refresh" @click="refresh">刷新入口</button>
          <button class="btn" type="button" data-testid="jia-read-sample" @click="readSample('甲')">读取原证据</button>
          <button class="btn primary" type="button" data-testid="jia-submit-late" @click="submitA">
            提交/超时重提 600g
          </button>
        </div>
      </article>

      <article class="entry-card" data-testid="entry-supervisor">
        <h4>主管转派</h4>
        <label class="target-line">
          指定复核员
          <input v-model="targetReviewer" data-testid="transfer-target" />
        </label>
        <div class="button-row">
          <button class="btn primary" type="button" data-testid="transfer-review" @click="transfer">
            执行转派给乙
          </button>
          <button class="btn ghost" type="button" data-testid="update-candidate-v2" @click="updateCandidate">
            模拟候选更新到 v2
          </button>
          <RouterLink class="btn" to="/sku_match" data-testid="open-match-page">打开规格匹配器</RouterLink>
        </div>
        <p v-if="handover" class="receipt-line" data-testid="handover-id">
          接手回执：{{ handover.receipt_id }}（归属 v{{ handover.ownership_version }}）
        </p>
      </article>

      <article class="entry-card" data-testid="entry-yi" :data-completed="contextB?.completed" :data-can-submit="contextB?.can_submit">
        <h4>乙从自己的待办进入</h4>
        <p class="entry-state">
          <span>是否办结：<strong>{{ yesNo(contextB?.completed) }}</strong></span>
          <span>能否提交：<strong>{{ yesNo(contextB?.can_submit) }}</strong></span>
        </p>
        <p class="entry-message" data-testid="yi-context-message">{{ contextB?.message || '转派后，乙的待办会出现 M72。' }}</p>
        <div class="button-row">
          <button class="btn" type="button" data-testid="yi-open-todo" @click="openTodo">从待办进入</button>
          <button class="btn" type="button" data-testid="yi-read-sample" @click="readSample('乙')">打开原证据</button>
          <button class="btn primary" type="button" data-testid="yi-submit" @click="submitB">乙提交 600g</button>
        </div>
      </article>
    </div>

    <div v-if="flowMessage" class="flow-message" data-testid="flow-message" :data-ok="lastOk">
      <strong>{{ lastOk ? '操作结果' : '权限/冲突结果' }}：</strong>{{ flowMessage }}
    </div>

    <div v-if="handover" class="receipt-panel" data-testid="handover-scope">
      <h4>接手回执 · 移交范围</h4>
      <ul>
        <li v-for="item in handover['移交范围'] || handover.scope || []" :key="item">{{ item }}</li>
      </ul>
    </div>

    <div v-if="sampleResult" class="sample-panel" data-testid="sample-result" :data-authorized="sampleResult.ok">
      <h4>原证据 / 样本详情</h4>
      <p v-if="!sampleResult.ok" class="error-text" data-testid="sample-denied">
        {{ sampleResult.message }}
      </p>
      <template v-else>
        <p>证据：{{ sampleResult.evidence?.evidence_id }} · {{ sampleResult.evidence?.version }} · {{ sampleResult.evidence?.candidate_spec }}</p>
        <pre data-testid="sample-html">{{ sampleResult.sample_detail?.html || sampleResult.sample?.['原始HTML摘要'] }}</pre>
      </template>
    </div>

    <div v-if="lastReceipt" class="receipt-panel" data-testid="confirm-receipt">
      <h4>确认回执</h4>
      <p>回执号：{{ lastReceipt.receipt_id }}；实际负责人：{{ lastReceipt.effective_reviewer }}；证据版本：{{ lastReceipt.version }}；候选：{{ lastReceipt.approved_candidate_spec }}</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'

import { getJson, postJson } from '@/api/backend-service'
import { notifyReviewFlowChanged, reviewSession, setReviewUser } from '@/domain/reviewSession'

const emit = defineEmits<{ changed: [] }>()

const contextA = ref<any>(null)
const contextB = ref<any>(null)
const handover = ref<any>(null)
const sampleResult = ref<any>(null)
const lastReceipt = ref<any>(null)
const flowMessage = ref('')
const lastOk = ref(true)
const targetReviewer = ref('乙')
const todo = ref<any>(null)

function yesNo(value: unknown): string {
  if (value === true) return '是'
  if (value === false) return '否'
  return '—'
}

function showResult(result: any) {
  lastOk.value = Boolean(result?.ok)
  flowMessage.value = result?.message || '未收到结果'
  if (result?.receipt?.type === '转派审单' || result?.handover) {
    handover.value = result.handover || result.receipt
  }
  if (result?.receipt && result.receipt.confirmed) {
    lastReceipt.value = result.receipt
  }
  notifyReviewFlowChanged()
  emit('changed')
  void refresh()
}

function changeUser(event: Event) {
  setReviewUser((event.target as HTMLSelectElement).value)
}

async function resetScenario() {
  const result = await postJson('/api/match_review/scenario/reset')
  handover.value = null
  sampleResult.value = null
  lastReceipt.value = null
  flowMessage.value = result.message
  lastOk.value = true
  notifyReviewFlowChanged()
  emit('changed')
  await refresh()
}

async function refresh() {
  const [a, b] = await Promise.all([
    getJson('/api/match_review/M72/context?reviewer=甲'),
    getJson('/api/match_review/M72/context?reviewer=乙'),
  ])
  contextA.value = a
  contextB.value = b
  if (b?.receipt?.confirmed) lastReceipt.value = b.receipt
  if (a?.last_handover) handover.value = a.last_handover
}

async function openTodo() {
  const accepted = await postJson('/api/match_review/M72/accept', { reviewer: '乙' })
  todo.value = await getJson('/api/match_review/todo/乙')
  contextB.value = await getJson('/api/match_review/M72/context?reviewer=乙')
  flowMessage.value = accepted?.ok
    ? `${accepted.message}；乙的待办含 M72：${(todo.value.records || []).join('、') || '无'}`
    : accepted?.message || '乙未能接手'
  lastOk.value = Boolean(accepted?.ok)
}

async function transfer() {
  const result = await postJson('/api/match_review/M72/transfer', {
    supervisor: reviewSession.currentUser === '主管' ? '主管' : '主管',
    target_reviewer: targetReviewer.value,
    idempotency_key: 'UI-TRANSFER-M72-001',
  })
  showResult(result)
}

async function readSample(user: string) {
  sampleResult.value = await getJson(`/api/match_review/M72/sample?reviewer=${encodeURIComponent(user)}`)
}

async function submitA() {
  const result = await postJson('/api/match_review/M72/submit', {
    reviewer: '甲',
    conclusion: '甲提交结论：600g',
    candidate_spec: '600g',
    evidence_id: 'EV-M72-600G',
    evidence_version: 1,
    expected_version: 1,
    idempotency_key: reviewSession.m72KeyA,
  })
  showResult(result)
}

async function submitB() {
  const result = await postJson('/api/match_review/M72/submit', {
    reviewer: '乙',
    conclusion: '乙接手后核准：600g',
    candidate_spec: '600g',
    evidence_id: 'EV-M72-600G',
    evidence_version: 1,
    idempotency_key: reviewSession.m72KeyB,
  })
  showResult(result)
}

async function updateCandidate() {
  const result = await postJson('/api/match_review/M72/candidate-evidence', {
    reviewer: reviewSession.currentUser,
    candidate_spec: '650g',
  })
  showResult(result)
}

function handleExternalChange() {
  void refresh()
}

onMounted(async () => {
  try {
    await postJson('/api/match_review/scenario/m72')
    await refresh()
  } catch (error) {
    flowMessage.value = error instanceof Error ? error.message : '审单流程接口暂不可用'
    lastOk.value = false
  }
  window.addEventListener('review-flow-changed', handleExternalChange)
})

onUnmounted(() => {
  window.removeEventListener('review-flow-changed', handleExternalChange)
})
</script>

<style scoped>
.review-flow {
  margin: 16px 0;
  padding: 16px;
  border: 1px solid #d9e2ec;
  border-radius: 12px;
  background: #f8fafc;
}
.review-flow-head,
.review-controls,
.review-grid,
.button-row,
.target-line,
.entry-state {
  display: flex;
  gap: 12px;
  align-items: center;
}
.review-flow-head {
  justify-content: space-between;
  align-items: flex-start;
  flex-wrap: wrap;
}
.review-flow-head h3,
.receipt-panel h4 {
  margin: 0 0 6px;
}
.review-flow-head p {
  margin: 0;
  color: #52606d;
}
.review-grid {
  align-items: stretch;
  margin: 14px 0;
  flex-wrap: wrap;
}
.entry-card,
.receipt-panel,
.sample-panel {
  background: #fff;
  border: 1px solid #e4e7eb;
  border-radius: 10px;
  padding: 12px;
}
.entry-card {
  flex: 1 1 280px;
}
.entry-card[data-can-submit='true'] {
  border-color: #2f855a;
}
.entry-card[data-completed='true'] {
  background: #f0fff4;
}
.entry-state {
  justify-content: space-between;
}
.entry-message {
  min-height: 42px;
  color: #334e68;
}
.button-row {
  flex-wrap: wrap;
}
.target-line {
  margin: 10px 0;
}
.target-line input {
  width: 120px;
  padding: 6px 8px;
}
.flow-message {
  padding: 10px 12px;
  border-radius: 8px;
  background: #edf2f7;
  margin-bottom: 12px;
}
.receipt-panel,
.sample-panel {
  margin-top: 12px;
}
.receipt-panel ul {
  margin: 8px 0 0;
  padding-left: 20px;
}
.sample-panel pre {
  white-space: pre-wrap;
  word-break: break-all;
  background: #102a43;
  color: #f0f4f8;
  padding: 10px;
  border-radius: 8px;
  max-height: 220px;
  overflow: auto;
}
.receipt-line {
  font-size: 13px;
  color: #2c5282;
}
</style>
