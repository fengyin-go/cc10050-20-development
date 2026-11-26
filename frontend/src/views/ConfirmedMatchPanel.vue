<template>
  <section class="match-result-panel" data-testid="confirmed-match-panel">
    <div class="panel-head">
      <div>
        <h3>已确认复核归属 → 商品匹配结果</h3>
        <p>规格匹配器只读取 M72 的确认回执，并采用实际通过的那一版候选证据。</p>
      </div>
      <button class="btn primary" type="button" data-testid="load-confirmed-match" @click="loadResult">
        读取已确认结果
      </button>
    </div>

    <p v-if="message" class="result-message" :data-ok="result?.ok" data-testid="confirmed-match-message">{{ message }}</p>

    <div v-if="result?.match_result" class="result-grid" data-testid="match-result-data">
      <div><span>实际负责人</span><strong data-testid="match-effective-reviewer">{{ result.match_result.effective_reviewer }}</strong></div>
      <div><span>采用候选</span><strong data-testid="match-approved-spec">{{ result.match_result.candidate_spec }}</strong></div>
      <div><span>证据版本</span><strong data-testid="match-evidence-version">{{ result.match_result.version }}</strong></div>
      <div><span>回执号</span><strong data-testid="match-receipt-id">{{ result.match_result.receipt_id }}</strong></div>
      <div><span>匹配状态</span><strong>已匹配 / 复核核准</strong></div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { postJson } from '@/api/backend-service'
import { notifyReviewFlowChanged } from '@/domain/reviewSession'

const emit = defineEmits<{ changed: [] }>()
const result = ref<any>(null)
const message = ref('正在读取已确认复核数据…')

async function loadResult() {
  try {
    result.value = await postJson('/api/sku_match/apply-confirmed-review', { review_ref: 'M72' })
    message.value = result.value?.message || ''
    if (result.value?.ok) {
      emit('changed')
      notifyReviewFlowChanged()
    }
  } catch (error) {
    result.value = null
    message.value = error instanceof Error ? error.message : '匹配结果接口暂不可用'
  }
}

onMounted(loadResult)
</script>

<style scoped>
.match-result-panel {
  margin: 16px 0;
  padding: 16px;
  border: 1px solid #c6f6d5;
  border-radius: 12px;
  background: #f0fff4;
}
.panel-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
  flex-wrap: wrap;
}
h3 {
  margin: 0 0 6px;
}
p {
  margin: 0;
  color: #276749;
}
.result-message {
  margin-top: 10px;
}
.result-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
  margin-top: 12px;
}
.result-grid div {
  background: #fff;
  border-radius: 8px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.result-grid span {
  color: #718096;
  font-size: 12px;
}
.result-grid strong {
  color: #1a202c;
}
</style>
