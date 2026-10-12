// 仅复制 JSON 接口数据，序列化时同时展开嵌套的 Vue 响应式对象。
export function cloneJson<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}
