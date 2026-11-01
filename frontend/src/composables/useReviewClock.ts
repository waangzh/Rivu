import { onBeforeUnmount, onMounted, ref } from 'vue';

export function useReviewClock() {
  const now = ref(Date.now());
  let timer: ReturnType<typeof setInterval> | undefined;
  onMounted(() => { timer = setInterval(() => { now.value = Date.now(); }, 1000); });
  onBeforeUnmount(() => { if (timer) clearInterval(timer); });
  return now;
}
