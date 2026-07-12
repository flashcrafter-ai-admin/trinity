<template>
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
    <div class="px-6 py-5 border-b border-gray-200 dark:border-gray-700">
      <h2 class="text-lg font-medium text-gray-900 dark:text-white">Two-Factor Authentication</h2>
      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
        Add a TOTP second factor (Google Authenticator, 1Password, Authy…) to your account.
      </p>
    </div>

    <div class="px-6 py-5 space-y-5">
      <div v-if="loading" class="text-sm text-gray-500 dark:text-gray-400">Loading…</div>

      <template v-else>
        <!-- Enrolled state -->
        <div v-if="status.enrolled" class="space-y-4">
          <div class="flex items-center gap-2 text-sm">
            <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300">
              ✓ Enabled
            </span>
            <span class="text-gray-500 dark:text-gray-400">
              {{ status.recovery_codes_remaining }} recovery code(s) remaining
            </span>
          </div>

          <div class="flex flex-wrap gap-3">
            <button
              @click="regenerate"
              :disabled="busy"
              class="px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
            >Regenerate recovery codes</button>
            <button
              @click="showDisable = true"
              class="px-3 py-2 text-sm rounded-lg border border-red-300 dark:border-red-700 text-red-700 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-900/30"
            >Disable 2FA</button>
          </div>

          <!-- Disable confirm -->
          <div v-if="showDisable" class="p-4 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 space-y-3">
            <p class="text-sm text-red-800 dark:text-red-300">Enter a current code (or a recovery code) to confirm disabling 2FA.</p>
            <input v-model="disableCode" type="text" inputmode="numeric" placeholder="123456"
              class="block w-40 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
            <div class="flex gap-2">
              <button @click="disable" :disabled="busy || !disableCode"
                class="px-3 py-2 text-sm rounded-lg text-white bg-red-600 hover:bg-red-700 disabled:opacity-50">Confirm disable</button>
              <button @click="showDisable = false; disableCode = ''"
                class="px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300">Cancel</button>
            </div>
          </div>
        </div>

        <!-- Not enrolled: enable flow -->
        <div v-else class="space-y-4">
          <div v-if="!enroll">
            <button @click="startEnroll" :disabled="busy"
              class="px-4 py-2 text-sm rounded-lg text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50">
              {{ busy ? 'Starting…' : 'Enable 2FA' }}
            </button>
          </div>

          <div v-else class="space-y-4">
            <p class="text-sm text-gray-600 dark:text-gray-400">
              Scan the QR with your authenticator app, then enter the 6-digit code to confirm.
            </p>
            <QrCode :value="enroll.otpauth_uri" />
            <div class="text-xs text-gray-500 dark:text-gray-400">
              Can't scan? Enter this key manually:
              <code class="block mt-1 select-all font-mono text-sm text-gray-800 dark:text-gray-200 break-all">{{ enroll.secret }}</code>
            </div>
            <div class="flex items-end gap-2">
              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Verification code</label>
                <input v-model="confirmCode" type="text" inputmode="numeric" maxlength="6" placeholder="123456"
                  class="mt-1 block w-40 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
              </div>
              <button @click="confirmEnroll" :disabled="busy || confirmCode.length < 6"
                class="px-4 py-2 text-sm rounded-lg text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50">Confirm</button>
              <button @click="enroll = null; confirmCode = ''"
                class="px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300">Cancel</button>
            </div>
          </div>
        </div>

        <!-- Recovery codes (shown once) -->
        <div v-if="recoveryCodes.length" class="p-4 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 space-y-2">
          <p class="text-sm font-medium text-amber-800 dark:text-amber-300">
            Save these recovery codes now — they won't be shown again.
          </p>
          <div class="grid grid-cols-2 gap-1 font-mono text-sm text-gray-800 dark:text-gray-200">
            <code v-for="c in recoveryCodes" :key="c" class="select-all">{{ c }}</code>
          </div>
          <button @click="recoveryCodes = []" class="text-xs text-amber-700 dark:text-amber-400 underline">I've saved them</button>
        </div>

        <p v-if="error" class="text-sm text-red-600 dark:text-red-400">{{ error }}</p>

        <!-- Admin policy -->
        <div v-if="isAdmin" class="pt-5 mt-2 border-t border-gray-200 dark:border-gray-700 space-y-3">
          <h3 class="text-sm font-medium text-gray-900 dark:text-white">Organization policy</h3>
          <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
            <input type="checkbox" v-model="policy.require_for_admin" @change="savePolicy" />
            Require 2FA for admins
          </label>
          <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
            <input type="checkbox" v-model="policy.require_for_creator" @change="savePolicy" />
            Require 2FA for creators
          </label>
          <p class="text-xs text-gray-500 dark:text-gray-400">
            Users in a required role who haven't enrolled will be prompted to set up 2FA at their next login.
          </p>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup>
// Settings → Security self-service 2FA panel (#5). Rendered only when the
// `2fa` enterprise feature is entitled (gated by the parent Settings view).
import { ref, reactive, computed, onMounted } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../../stores/auth'
import QrCode from '../QrCode.vue'

const authStore = useAuthStore()
const isAdmin = computed(() => authStore.role === 'admin')

const loading = ref(true)
const busy = ref(false)
const error = ref('')
const status = reactive({ enrolled: false, pending: false, recovery_codes_remaining: 0 })
const enroll = ref(null)            // { secret, otpauth_uri }
const confirmCode = ref('')
const recoveryCodes = ref([])
const showDisable = ref(false)
const disableCode = ref('')
const policy = reactive({ require_for_admin: false, require_for_creator: false })

const cfg = () => ({ headers: authStore.authHeader })

const loadStatus = async () => {
  try {
    const r = await axios.get('/api/enterprise/2fa/status', cfg())
    Object.assign(status, r.data)
  } catch (e) { error.value = e.response?.data?.detail || 'Failed to load status' }
}

const loadPolicy = async () => {
  if (!isAdmin.value) return
  try {
    const r = await axios.get('/api/enterprise/2fa/policy', cfg())
    Object.assign(policy, r.data)
  } catch (e) { /* non-fatal */ }
}

onMounted(async () => {
  await Promise.all([loadStatus(), loadPolicy()])
  loading.value = false
})

const startEnroll = async () => {
  busy.value = true; error.value = ''
  try {
    const r = await axios.post('/api/enterprise/2fa/enroll/start', {}, cfg())
    enroll.value = r.data
  } catch (e) { error.value = e.response?.data?.detail || 'Failed to start enrollment' }
  finally { busy.value = false }
}

const confirmEnroll = async () => {
  busy.value = true; error.value = ''
  try {
    const r = await axios.post('/api/enterprise/2fa/enroll/confirm', { code: confirmCode.value }, cfg())
    recoveryCodes.value = r.data.recovery_codes || []
    enroll.value = null; confirmCode.value = ''
    await loadStatus()
  } catch (e) { error.value = e.response?.data?.detail || 'Invalid code' }
  finally { busy.value = false }
}

const regenerate = async () => {
  busy.value = true; error.value = ''
  try {
    const r = await axios.post('/api/enterprise/2fa/recovery-codes', {}, cfg())
    recoveryCodes.value = r.data.recovery_codes || []
    await loadStatus()
  } catch (e) { error.value = e.response?.data?.detail || 'Failed to regenerate' }
  finally { busy.value = false }
}

const disable = async () => {
  busy.value = true; error.value = ''
  try {
    await axios.post('/api/enterprise/2fa/disable', { code: disableCode.value }, cfg())
    showDisable.value = false; disableCode.value = ''
    recoveryCodes.value = []
    await loadStatus()
  } catch (e) { error.value = e.response?.data?.detail || 'Invalid code' }
  finally { busy.value = false }
}

const savePolicy = async () => {
  try {
    await axios.put('/api/enterprise/2fa/policy', {
      require_for_admin: policy.require_for_admin,
      require_for_creator: policy.require_for_creator,
    }, cfg())
  } catch (e) { error.value = e.response?.data?.detail || 'Failed to save policy' }
}
</script>
