<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { 表类型, 表状态, 标 } from '../labels'
const rows = ref([])
const writability = ref(null)
const periodId = ref('')
const err = ref('')
onMounted(async () => {
  try {
    rows.value = await api.meters()
    const s = await api.settings()
    periodId.value = s.current_period || ''
    if (periodId.value) writability.value = await api.writability(periodId.value)
  } catch (e) { err.value = e.message }
})
function hintFor(building) {
  if (!writability.value) return ''
  const item = writability.value.buildings.find(b => b.building === building)
  return item ? item.reason : ''
}
function writableOf(building) {
  const item = writability.value?.buildings.find(b => b.building === building)
  return item?.writable
}
</script>
<template>
  <div>
    <h1>表具</h1>
    <p class="sub">园区表具：楼栋、户号、水表电表；可写性口径与封印页一致（账期 {{ periodId }}）</p>
    <p v-if="err" class="msg err">{{ err }}</p>
    <div class="panel" v-if="writability">
      <h3>楼栋可写性</h3>
      <table>
        <thead><tr><th>楼栋</th><th>估抄/实抄</th><th>原因</th></tr></thead>
        <tbody>
          <tr v-for="b in writability.buildings" :key="b.building">
            <td>{{ b.building }}</td>
            <td><span class="tag" :class="b.writable ? 'open' : 'sealed'">{{ b.writable ? '可写' : '禁写' }}</span></td>
            <td>{{ b.reason }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel">
      <table>
        <thead><tr><th>表号</th><th>楼栋</th><th>户号</th><th>类型</th><th>状态</th><th>本账期写闸</th></tr></thead>
        <tbody>
          <tr v-for="m in rows" :key="m.meter_id">
            <td>{{ m.meter_id }}</td>
            <td>{{ m.building }}</td>
            <td>{{ m.unit_label }}</td>
            <td>{{ 标(表类型, m.meter_type) }}</td>
            <td>{{ 标(表状态, m.status) }}</td>
            <td>
              <span v-if="writability" class="tag" :class="writableOf(m.building) ? 'open' : 'sealed'">
                {{ writableOf(m.building) ? '可写' : '禁写：' + hintFor(m.building) }}
              </span>
              <span v-else>—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
