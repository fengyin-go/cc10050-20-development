<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <ReviewHandoffPanel v-if="meta.key === 'match_review'" @changed="reload" />
    <ConfirmedMatchPanel v-if="meta.key === 'sku_match'" @changed="reload" />

    <div v-if="meta.key === 'match_review'" class="identity-bar" data-testid="review-identity-bar">
      <span>行内办理身份：</span>
      <select :value="reviewSession.currentUser" aria-label="行内办理身份" @change="changeReviewUser">
        <option>甲</option>
        <option>乙</option>
        <option>主管</option>
      </select>
      <span class="hint">只有当前认领人或接手人可通过/驳回；转派请使用“转派审单”。</span>
    </div>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="showTransfer" class="modal-backdrop" data-testid="transfer-modal">
      <form class="modal" @submit.prevent="submitTransfer">
        <h3>转派审单：{{ transferRow?.['记录编号'] }}</h3>
        <p class="modal-tip">转派后原任务号不变；旧认领人的提交凭证失效，接手人可打开同一批原证据继续办理。</p>
        <label class="create-field">
          <span>主管/操作人</span>
          <input v-model="transferSupervisor" data-testid="transfer-supervisor" required />
        </label>
        <label class="create-field">
          <span>指定复核员</span>
          <input v-model="transferTarget" data-testid="transfer-target-input" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showTransfer = false">取消</button>
          <button class="btn primary" data-testid="submit-transfer" type="submit" :disabled="actionLoading">
            {{ actionLoading ? '转派中…' : '确认转派' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'
import { reviewSession, setReviewUser } from '@/domain/reviewSession'
import ReviewHandoffPanel from './ReviewHandoffPanel.vue'
import ConfirmedMatchPanel from './ConfirmedMatchPanel.vue'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const showTransfer = ref(false)
const transferRow = ref<EntryRow | null>(null)
const transferTarget = ref('乙')
const transferSupervisor = ref('主管')
const actionLoading = ref(false)
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

function changeReviewUser(event: Event) {
  setReviewUser((event.target as HTMLSelectElement).value)
}

function openTransfer(row: EntryRow) {
  transferRow.value = row
  transferTarget.value = '乙'
  transferSupervisor.value = reviewSession.currentUser === '主管' ? '主管' : '主管'
  errorMessage.value = ''
  showTransfer.value = true
}

async function submitTransfer() {
  if (!transferRow.value) return
  actionLoading.value = true
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(transferRow.value.id), '转派审单', {
      supervisor: transferSupervisor.value,
      target_reviewer: transferTarget.value,
      idempotency_key: `ROW-TRANSFER-${transferRow.value.id}-${transferTarget.value}`,
    })
    if (!result.ok) throw new Error(result.message)
    showTransfer.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '转派失败'
  } finally {
    actionLoading.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  if (action === '转派审单') {
    openTransfer(row)
    return
  }
  errorMessage.value = ''
  try {
    const extra: Record<string, unknown> = { reviewer: reviewSession.currentUser }
    if (action === '通过记录' || action === '驳回记录') {
      extra.conclusion = row['复核结论'] || undefined
      extra.idempotency_key = `ROW-${row.id}-${action}-${reviewSession.currentUser}`
    }
    const result = await applyAction(meta.key, Number(row.id), action, extra)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.identity-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 0 14px;
  padding: 10px 12px;
  border-radius: 10px;
  background: #eef6ff;
  border: 1px solid #bee3f8;
}
.identity-bar select {
  padding: 6px 8px;
}
.identity-bar .hint {
  color: #4a5568;
  font-size: 13px;
}
.modal-tip {
  color: #4a5568;
  line-height: 1.6;
}
</style>
