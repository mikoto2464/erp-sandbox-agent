import { ref } from 'vue';
export interface Toast {
  id: number;
  text: string;
  kind: string;
  action?: () => void;
  actionLabel?: string;
}
export function useToasts() {
  const messages = ref<Toast[]>([]);
  const timers = new Map<number, ReturnType<typeof setTimeout>>();
  let sequence = 0;
  function remove(id: number) {
    clearTimeout(timers.get(id));
    timers.delete(id);
    messages.value = messages.value.filter((item) => item.id !== id);
  }
  function notify(text: string, kind = 'info', action?: () => void, actionLabel = '查看任务') {
    if (messages.value.some((item) => item.text === text && item.kind === kind)) return;
    if (messages.value.length >= 4) remove(messages.value[0].id);
    const id = ++sequence;
    messages.value.push({ id, text, kind, action, actionLabel });
    resume(id);
  }
  function pause(id: number) {
    clearTimeout(timers.get(id));
  }
  function resume(id: number) {
    const item = messages.value.find((item) => item.id === id);
    if (item)
      timers.set(
        id,
        setTimeout(() => remove(id), item.kind === 'error' ? 10000 : 5000),
      );
  }
  function dispose() {
    timers.forEach(clearTimeout);
    timers.clear();
    messages.value = [];
  }
  return { messages, notify, remove, pause, resume, dispose };
}
