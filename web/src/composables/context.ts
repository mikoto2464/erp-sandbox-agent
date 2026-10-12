import { inject, type InjectionKey } from 'vue';
import type { useWorkspace } from './useWorkspace';
export type Workspace = ReturnType<typeof useWorkspace>;
export const workspaceKey: InjectionKey<Workspace> = Symbol('workspace');
export function useWorkspaceContext() {
  const value = inject(workspaceKey);
  if (!value) throw new Error('Workspace provider missing');
  return value;
}
