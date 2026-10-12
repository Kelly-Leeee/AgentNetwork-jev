import type { CanvasState, TeamAgent, Workstream } from './types';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail ?? `Request failed: ${response.status}`);
  return body as T;
}

export const api = {
  state: () => request<CanvasState>('/api/state'),
  runNext: () => request<CanvasState>('/api/run-next', { method: 'POST' }),
  pause: (paused: boolean) => request<CanvasState>('/api/pause', {
    method: 'POST', body: JSON.stringify({ paused }),
  }),
  setExecutionMode: (mode: 'step' | 'auto') => request<CanvasState>('/api/execution-mode', {
    method: 'POST', body: JSON.stringify({ mode }),
  }),
  reset: () => request<CanvasState>('/api/reset', { method: 'POST' }),
  updateTask: (goal: string, workstreams: Array<Pick<Workstream, 'id' | 'title' | 'goal' | 'input'> & { todos: string[] }>) => request<CanvasState>('/api/task', {
    method: 'PATCH', body: JSON.stringify({ goal, workstreams }),
  }),
  answerHuman: (answer: string) => request<CanvasState>('/api/human-answer', {
    method: 'POST', body: JSON.stringify({ answer }),
  }),
  addAgent: () => request<TeamAgent>('/api/agents', { method: 'POST' }),
  updateAgent: (id: string, name: string, role: string) => request<TeamAgent>(`/api/agents/${id}`, {
    method: 'PATCH', body: JSON.stringify({ name, role }),
  }),
};
