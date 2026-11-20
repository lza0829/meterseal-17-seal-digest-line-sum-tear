<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { 封印架构, 标 } from '../labels'
const current = ref('')
const sealMode = ref('period')
const msg = ref('')
const err = ref('')
async function load() {
  const s = await api.settings()
  current.value = s.current_period || ''
  sealMode.value = s.seal_mode || 'period'
}
async function save() {
  err.value = ''; msg.value = ''
  try {
    await api.putSetting('current_period', current.value)
    msg.value = '已保存当前账期'
    await load()
  } catch (e) { err.value = e.message }
}
async function saveMode() {
  err.value = ''; msg.value = ''
  try {
    await api.putSetting('seal_mode', sealMode.value)
    msg.value = `封印架构已切换为：${标(封印架构, sealMode.value)}`
    await load()
  } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
<template>
  <div>
    <h1>设置</h1>
    <p class="sub">运行参数；全局只贯彻一种封印架构，切换后写闸口径整体跟随</p>
    <div class="panel">
      <div class="row">
        <label>当前账期<input v-model="current" /></label>
        <button @click="save">保存</button>
      </div>
      <div class="row">
        <label>封印架构
          <select v-model="sealMode">
            <option value="period">{{ 标(封印架构, 'period') }}</option>
            <option value="building">{{ 标(封印架构, 'building') }}</option>
          </select>
        </label>
        <button @click="saveMode">切换架构</button>
      </div>
      <p class="sub" style="margin-top:8px">
        整账期封印：一次关闭该期全部楼栋写闸；楼栋分区封印：须带楼栋，只关该栋，别栋仍可装载与对齐。
      </p>
      <p class="msg ok" v-if="msg">{{ msg }}</p>
      <p class="msg err" v-if="err">{{ err }}</p>
    </div>
  </div>
</template>
