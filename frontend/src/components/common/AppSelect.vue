<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId } from 'vue';
import AppIcon from './AppIcon.vue';

type SelectOption = { value: string; label: string; detail?: string };

const props = withDefaults(defineProps<{
  modelValue: string;
  options: SelectOption[];
  disabled?: boolean;
  ariaLabel?: string;
}>(), { disabled: false, ariaLabel: '' });
const emit = defineEmits<{ 'update:modelValue': [value: string] }>();

const root = ref<HTMLElement | null>(null);
const trigger = ref<HTMLButtonElement | null>(null);
const list = ref<HTMLElement | null>(null);
const open = ref(false);
const active = ref(-1);
const id = useId();
const selected = computed(() => props.options.find(option => option.value === props.modelValue) ?? null);
const selectedIndex = computed(() => props.options.findIndex(option => option.value === props.modelValue));

function optionId(index: number) { return `${id}-option-${index}`; }
function scrollActive() {
  list.value?.querySelectorAll<HTMLElement>('[role="option"]')[active.value]?.scrollIntoView({ block: 'nearest' });
}
function show() {
  if (props.disabled) return;
  open.value = true;
  active.value = selectedIndex.value >= 0 ? selectedIndex.value : 0;
  nextTick(() => { list.value?.focus(); scrollActive(); });
}
function close(focusTrigger = false) {
  if (!open.value) return;
  open.value = false;
  if (focusTrigger) trigger.value?.focus();
}
function select(index: number) {
  const option = props.options[index];
  if (!option) return;
  emit('update:modelValue', option.value);
  close(true);
}
function move(delta: number) {
  if (!props.options.length) return;
  active.value = (active.value + delta + props.options.length) % props.options.length;
  nextTick(scrollActive);
}
function onKeydown(event: KeyboardEvent) {
  if (props.disabled) return;
  if (!open.value) {
    if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
      event.preventDefault();
      show();
    }
    return;
  }
  if (event.key === 'ArrowDown') { event.preventDefault(); move(1); }
  else if (event.key === 'ArrowUp') { event.preventDefault(); move(-1); }
  else if (event.key === 'Home') { event.preventDefault(); active.value = 0; nextTick(scrollActive); }
  else if (event.key === 'End') { event.preventDefault(); active.value = props.options.length - 1; nextTick(scrollActive); }
  else if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(active.value); }
  else if (event.key === 'Escape') { event.preventDefault(); close(true); }
  else if (event.key === 'Tab') close();
}
function onPointerDown(event: PointerEvent) {
  if (open.value && root.value && !root.value.contains(event.target as Node)) close();
}
onMounted(() => document.addEventListener('pointerdown', onPointerDown));
onBeforeUnmount(() => document.removeEventListener('pointerdown', onPointerDown));
</script>

<template>
  <div ref="root" class="app-select" @keydown="onKeydown">
    <button ref="trigger" type="button" class="app-select__trigger" :class="{ 'is-open': open }"
      :disabled="disabled" :aria-label="ariaLabel || undefined" aria-haspopup="listbox"
      :aria-expanded="open" :aria-controls="id + '-list'" @click="open ? close() : show()">
      <span class="app-select__value">
        <strong>{{ selected?.label ?? '请选择' }}</strong>
        <small v-if="selected?.detail">{{ selected.detail }}</small>
      </span>
      <span class="app-select__chevron"><AppIcon name="chevron-down" :size="16" /></span>
    </button>
    <Transition name="select-pop">
      <ul v-if="open" :id="id + '-list'" ref="list" class="app-select__options" role="listbox" tabindex="-1"
        :aria-activedescendant="active >= 0 ? optionId(active) : undefined">
        <li v-for="(option, index) in options" :id="optionId(index)" :key="option.value"
          class="app-select__option" :class="{ 'is-active': active === index, 'is-selected': option.value === modelValue }"
          role="option" :aria-selected="option.value === modelValue"
          @pointermove="active = index" @click="select(index)">
          <span class="app-select__option-copy">
            <strong>{{ option.label }}</strong>
            <small v-if="option.detail">{{ option.detail }}</small>
          </span>
          <span v-if="option.value === modelValue" class="app-select__check"><AppIcon name="check" :size="14" /></span>
        </li>
      </ul>
    </Transition>
  </div>
</template>
