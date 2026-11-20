<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { 批状态, 封印架构, 封印作用域, 标 } from '../labels'
const data = ref(null)
const err = ref('')
onMounted(async () => {
  try { data.value = await api.overview() } catch (e) { err.value = e.message }
})
</script>
<template>
  <div>
    <h1>总览</h1>
    <p class="sub">当前账期估抄、实抄、对齐与封印状态</p>
    <p v-if="err" class="msg err">{{ err }}</p>
    <div class="grid" v-if="data">
      <div class="stat">当前账期<b>{{ data.current_period }}</b></div>
      <div class="stat">封印架构<b>{{ 标(封印架构, data.seal_mode) }}</b></div>
      <div class="stat">开放账期<b>{{ data.open_periods }}</b></div>
      <div class="stat">已整期封印<b>{{ data.sealed_periods }}</b></div>
      <div class="stat">表具数<b>{{ data.meter_count }}</b></div>
      <div class="stat">楼栋数 / 已分区封<b>{{ data.buildings_total }} / {{ data.sealed_building_count }}</b></div>
      <div class="stat">未决差额<b>{{ data.pending_variances }}</b></div>
      <div class="stat">封印覆盖<b>{{ data.seal_coverage ?? '—' }}</b></div>
      <div class="stat">封印摘要<b>{{ data.seal_digest ?? '—' }}</b></div>
    </div>
    <div class="panel" v-if="data">
      <h3>最近对账批</h3>
      <table>
        <thead><tr><th>批号</th><th>账期</th><th>作用域</th><th>楼栋</th><th>匹配</th><th>差额</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="b in data.recent_batches" :key="b.batch_id">
            <td>{{ b.batch_id }}</td>
            <td>{{ b.period_id }}</td>
            <td>{{ 标(封印作用域, b.sealed_scope) }}</td>
            <td>{{ b.building || '—' }}</td>
            <td>{{ b.matched_count }}</td>
            <td>{{ b.variance_count }}</td>
            <td><span class="tag" :class="b.status">{{ 标(批状态, b.status) }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
