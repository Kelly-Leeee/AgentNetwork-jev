export type TodoStatus = 'pending' | 'assigned' | 'running' | 'completed' | 'skipped' | 'failed';

export interface Todo {
  id: string;
  workstream_id: string;
  step: number;
  title: string;
  status: TodoStatus;
  owner: string | null;
  result: string;
  agent_output: { artifact: unknown; unresolved: string[] } | null;
}

export interface Workstream {
  id: string;
  title: string;
  goal: string;
  input: string;
  owner: string;
  status: string;
  final_output: string;
}

export interface RouteDecision {
  choice: string;
  confidence: number;
  probabilities: Record<string, number>;
  model?: string;
  usage: { input_tokens?: number; output_tokens?: number };
}

export interface ProcessEntry {
  type: string;
  timestamp: string;
  payload: Record<string, unknown>;
}

export interface AgentRun {
  id: string;
  todo_id: string;
  todo_title: string;
  input: string;
  process: ProcessEntry[];
  output: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  model_calls: number;
  tool_calls: number;
}

export interface RouteRun {
  id: string;
  workstream_id?: string;
  input: Record<string, unknown>;
  question: { type: string; instructions: string; criteria: Record<string, string> };
  output: RouteDecision | null;
  started_at: string;
  completed_at: string | null;
}

export interface Task {
  id: string;
  goal: string;
  transcript: string;
  status: string;
  phase: string;
  execution_mode: 'step' | 'auto';
  jev_call_count: number;
  max_jev_calls: number;
  routing_count: number;
  handoff_count: number;
  final_output: string;
  last_decision: RouteDecision | null;
  route_history: RouteRun[];
  pending_human_question: { id: string; workstream_id?: string; question: string; asked_at: string } | null;
  pending_human_questions: Array<{ id: string; workstream_id: string; question: string; asked_at: string }>;
  human_interactions: Array<{
    id: string;
    question: string;
    answer: string;
    asked_at: string;
    answered_at: string;
  }>;
  workstreams: Workstream[];
  todos: Todo[];
}

export interface TeamAgent {
  id: string;
  name: string;
  role: string;
  status: string;
  current_todo_id: string | null;
  last_output: string;
  message_count: number;
  runs: AgentRun[];
}

export interface TeamEvent {
  id: string;
  type: string;
  actor_id: string;
  payload: Record<string, unknown>;
  timestamp: string;
}

export interface CanvasState {
  task: Task;
  agents: TeamAgent[];
  events: TeamEvent[];
  paused: boolean;
  running: boolean;
}

export type Selection =
  | { kind: 'agent'; id: string }
  | { kind: 'router'; id: 'jev' }
  | { kind: 'human'; id: 'ask-user' }
  | { kind: 'event'; id: string }
  | null;
