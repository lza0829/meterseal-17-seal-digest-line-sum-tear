<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import { 账期状态, 封印架构, 封印作用域, 批状态, 标 } from '../labels'

const periods = ref([])
const buildings = ref([])
const periodId = ref('2026-10')
const building = ref('')
const batchId = ref('')
const mode = ref('period')
const sealedList = ref([])
const writability = ref(null)
const msg = ref('')
const err = ref('')

const isBuildingMode = computed(() => mode.value === 'building')

async function loadMeta() {
  const s = await api.settings()
  mode.value = s.seal_mode || 'period'
  const ms = await api.meters()
  buildings.value = [...new Set(ms.map(m => m.building))].sort()
  if (!building.value && buildings.value.length) building.value = buildings.value[0]
}

async function refresh() {
  periods.value = await api.periods()
  const bs = await api.batches(
    periodId.value,
    isBuildingMode.value ? building.value : null,
  )
  batchId.value = bs.find(b => b.status === 'open')?.batch_id || bs[0]?.batch_id || ''
  sealedList.value = await api.sealedBuildings(periodId.value)
  writability.value = await api.writability(periodId.value)
}

async function seal() {
  err.value = ''; msg.value = ''
  try {
    const body = { period_id: periodId.value, batch_id: batchId.value || null }
    if (isBuildingMode.value) {
      if (!building.value) throw new Error('分区封印必须选择楼栋')
      body.building = building.value
    }
    const r = await api.seal(body)
    // 成功文案必须与实际作用域同口径，禁止“分区封成功”却整期禁写
    msg.value = r.sealed_scope === 'building'
      ? `已分区封印 ${r.period_id} · ${r.building}，绑定批 ${r.batch_id}；其他楼栋仍可装载`
      : `已整账期封印 ${r.period_id}，绑定批 ${r.batch_id}；该期全部楼栋禁止写入`
    await refresh()
  } catch (e) { err.value = e.message }
}

onMounted(async () => {
  await loadMeta()
  await refresh()
})
</script>
<template>
  <div>
    <h1>账期封印</h1>
    <p class="sub">
      当前架构：<b>{{ 标(封印架构, mode) }}</b>
      <span v-if="isBuildingMode">——封印须带楼栋，只关该栋写闸，其他栋同账期仍可写</span>
      <span v-else>——一次封印关闭该账期全部楼栋的估抄/实抄写闸</span>
    </p>

    <div class="row">
      <label>账期<input v-model="periodId" @change="refresh" /></label>
      <label v-if="isBuildingMode">楼栋
        <select v-model="building" @change="refresh">
          <option v-for="b in buildings" :key="b" :value="b">{{ b }}</option>
        </select>
      </label>
      <label>对账批<input v-model="batchId" placeholder="可空取同作用域最新开放批" /></label>
      <button class="warn" @click="seal">
        {{ isBuildingMode ? `封印楼栋 ${building}` : '封印整账期' }}
      </button>
      <button class="secondary" @click="refresh">刷新</button>
    </div>
    <p class="msg ok" v-if="msg">{{ msg }}</p>
    <p class="msg err" v-if="err">{{ err }}</p>

    <div class="panel" v-if="writability">
      <h3>同账期各楼栋可写性（{{ 标(封印架构, writability.seal_mode) }}）</h3>
      <table>
        <thead><tr><th>楼栋</th><th>可否装载估抄/实抄</th><th>原因</th></tr></thead>
        <tbody>
          <tr v-for="b in writability.buildings" :key="b.building">
            <td>{{ b.building }}</td>
            <td>
              <span class="tag" :class="b.writable ? 'open' : 'sealed'">
                {{ b.writable ? '可写' : '禁写' }}
              </span>
            </td>
            <td>{{ b.reason }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" v-if="isBuildingMode">
      <h3>分区封印清单（账期 {{ periodId }}）</h3>
      <table>
        <thead><tr><th>楼栋</th><th>作用域</th><th>绑定批</th><th>封印时刻</th></tr></thead>
        <tbody>
          <tr v-if="!sealedList.length"><td colspan="4">暂无分区封印</td></tr>
          <tr v-for="s in sealedList" :key="s.building">
            <td>{{ s.building }}</td>
            <td>{{ 标(封印作用域, s.sealed_scope) }}</td>
            <td>{{ s.batch_id }}</td>
            <td>{{ s.sealed_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h3>账期状态</h3>
      <table>
        <thead><tr><th>账期</th><th>状态</th><th>封印时刻</th><th>绑定批</th></tr></thead>
        <tbody>
          <tr v-for="p in periods" :key="p.period_id">
            <td>{{ p.period_id }}</td>
            <td><span class="tag" :class="p.status">{{ 标(账期状态, p.status) }}</span></td>
            <td>{{ p.sealed_at || '—' }}</td>
            <td>{{ p.batch_no || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
