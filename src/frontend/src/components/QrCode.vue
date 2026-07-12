<template>
  <div class="flex flex-col items-center">
    <img
      v-if="dataUrl"
      :src="dataUrl"
      :alt="alt"
      class="rounded-lg border border-gray-200 dark:border-gray-700 bg-white p-2"
      width="200"
      height="200"
    />
    <div
      v-else
      class="w-[200px] h-[200px] flex items-center justify-center rounded-lg border border-dashed border-gray-300 dark:border-gray-600 text-xs text-gray-500 dark:text-gray-400 text-center px-3"
    >
      {{ error ? 'QR unavailable — use the manual code below' : 'Generating QR…' }}
    </div>
  </div>
</template>

<script setup>
// Renders a QR for a TOTP otpauth:// URI (#5). The `qrcode` package is
// imported dynamically and best-effort: if it isn't installed yet the
// component degrades to a hint and the caller's manual-entry secret carries
// the enrollment. The secret never leaves the browser — no external QR service.
import { ref, watch, onMounted } from 'vue'

const props = defineProps({
  value: { type: String, required: true },
  alt: { type: String, default: 'Authenticator QR code' },
})

const dataUrl = ref('')
const error = ref(false)

const render = async () => {
  dataUrl.value = ''
  error.value = false
  if (!props.value) return
  try {
    const QR = await import('qrcode')
    dataUrl.value = await (QR.default || QR).toDataURL(props.value, { width: 200, margin: 1 })
  } catch (e) {
    console.warn('[QrCode] render failed:', e?.message || e)
    error.value = true
  }
}

onMounted(render)
watch(() => props.value, render)
</script>
