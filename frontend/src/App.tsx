import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Background, Controls, MarkerType, ReactFlow, useEdgesState, useNodesState,
  type Edge, type NodeMouseHandler, type ReactFlowInstance,
} from '@xyflow/react';
import { api } from './api';
import { AgentNode, type AgentFlowNode } from './AgentNode';
import type { AgentRun, CanvasState, RouteRun, Selection, TeamAgent, TeamEvent, Workstream } from './types';

const initialPositions: Record<string, { x: number; y: number }> = {
  jev: { x: 310, y: 50 },
  'ask-user': { x: 310, y: 405 },
};

function initialPosition(id: string) {
  const match = /^agent-(\d+)$/.exec(id);
  const agentNumber = Number(match?.[1]);
  if (Number.isInteger(agentNumber) && agentNumber >= 1) {
    return { x: (agentNumber - 1) * 190, y: 245 };
  }
  return initialPositions[id] ?? { x: 300, y: 300 };
}

const nodeTypes = { agent: AgentNode };

export function App() {
  const [state, setState] = useState<CanvasState | null>(null);
  const [selection, setSelection] = useState<Selection>(null);
  const [error, setError] = useState('');
  const [taskEditorOpen, setTaskEditorOpen] = useState(false);
  const [leftPanelWidth, setLeftPanelWidth] = useState(320);
  const [nodes, setNodes, onNodesChange] = useNodesState<AgentFlowNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const flow = useRef<ReactFlowInstance<AgentFlowNode, Edge> | null>(null);

  const refresh = useCallback(async () => {
    try { setState(await api.state()); }
    catch (cause) { setError(messageOf(cause)); }
  }, []);

  useEffect(() => {
    void refresh();
    const source = new EventSource('/api/events');
    source.onmessage = () => void refresh();
    source.onerror = () => setTimeout(() => void refresh(), 1000);
    return () => source.close();
  }, [refresh]);

  useEffect(() => {
    if (!state) return;
    setNodes(current => {
      const previous = new Map(current.map(node => [node.id, node]));
      const update = (id: string, data: AgentFlowNode['data']): AgentFlowNode => {
        const existing = previous.get(id);
        if (existing) return { ...existing, data };
        return {
          id, type: 'agent', position: initialPosition(id), data,
        };
      };
      return [
        update('jev', { label: 'JEV Router', subtitle: 'Event-driven routing', status: state.task.routing_count > 0 ? 'running' : 'idle', kind: 'router' }),
        update('ask-user', { label: 'Ask User', subtitle: 'Collect missing human context', status: state.task.pending_human_question ? 'waiting' : 'idle', kind: 'human' }),
        ...state.agents.map(agent => update(agent.id, {
          label: agent.name,
          subtitle: agent.current_todo_id ?? (agent.status === 'assigned' ? 'Queued assignment' : 'Available peer'),
          status: agent.status,
          kind: 'agent',
        })),
      ];
    });
    const selectedAgent = state.task.last_decision?.choice.startsWith('assign_')
      ? state.task.last_decision.choice.replace('assign_', '').replaceAll('_', '-') : null;
    setEdges([...state.agents.map(agent => ({
      id: `jev-${agent.id}`, source: 'jev', target: agent.id,
      animated: selectedAgent === agent.id,
      label: selectedAgent === agent.id ? `${Math.round((state.task.last_decision?.confidence ?? 0) * 100)}%` : undefined,
      markerEnd: { type: MarkerType.ArrowClosed },
      className: selectedAgent === agent.id ? 'active-edge' : '',
    })), {
      id: 'jev-ask-user', source: 'jev', target: 'ask-user',
      animated: Boolean(state.task.pending_human_question),
      label: state.task.pending_human_question ? 'needs human' : undefined,
      markerEnd: { type: MarkerType.ArrowClosed },
      className: state.task.pending_human_question ? 'active-edge' : '',
    }]);
  }, [state, setEdges, setNodes]);

  useEffect(() => {
    if (!nodes.length || !flow.current) return;
    const timer = window.setTimeout(() => void flow.current?.fitView({ padding: .28 }), 20);
    return () => window.clearTimeout(timer);
  }, [nodes.length]);

  const onNodeClick: NodeMouseHandler<AgentFlowNode> = (_, node) => {
    setSelection(node.id === 'jev' ? { kind: 'router', id: 'jev' } : node.id === 'ask-user' ? { kind: 'human', id: 'ask-user' } : { kind: 'agent', id: node.id });
  };

  const act = async (action: () => Promise<CanvasState>) => {
    try { setError(''); setState(await action()); }
    catch (cause) { setError(messageOf(cause)); await refresh(); }
  };

  const startPanelResize = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = leftPanelWidth;
    const move = (pointerEvent: PointerEvent) => setLeftPanelWidth(Math.min(480, Math.max(250, startWidth + pointerEvent.clientX - startX)));
    const stop = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', stop);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop);
  };

  if (!state) return <div className="loading">Loading Agent Team Canvas…</div>;
  const completed = state.task.todos.filter(todo => ['completed', 'skipped'].includes(todo.status)).length;
  const hasRunnableWork = state.task.todos.some(todo => todo.status === 'assigned');
  const onlyWaitingForHuman = Boolean(state.task.pending_human_question) && !hasRunnableWork;

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><span>AR</span><div><h1>Agent Team Canvas</h1><p>Local orchestration demo</p></div></div>
        <div className="stats">
          <Pill>{state.agents.length} agents</Pill>
          <Pill>JEV {state.task.jev_call_count} / {state.task.max_jev_calls}</Pill>
          <Pill>{state.task.status}</Pill>
        </div>
        <div className="actions">
          <button className="ghost" onClick={() => confirm('Reset the demo and today\'s chat history?') && void act(api.reset)}>Reset</button>
          <button disabled={state.running} onClick={() => void act(() => api.setExecutionMode(state.task.execution_mode === 'step' ? 'auto' : 'step'))}>
            Mode: {state.task.execution_mode === 'step' ? 'Step' : 'Auto'}
          </button>
          <button onClick={() => void act(() => api.pause(!state.paused))}>{state.paused ? 'Resume' : 'Pause'}</button>
          <button className="primary" disabled={state.running || state.paused || state.task.status === 'completed' || onlyWaitingForHuman} onClick={() => void act(api.runNext)}>
            {state.running ? 'Running…' : state.task.status === 'completed' ? 'Completed' : onlyWaitingForHuman ? 'Answer required' : state.task.execution_mode === 'auto' ? 'Run workflow' : 'Run next step'}
          </button>
        </div>
      </header>

      <div className={`workspace ${selection ? 'has-selection' : ''}`} style={{ '--left-panel-width': `${leftPanelWidth}px` } as React.CSSProperties}>
        <aside className="left-panel panel">
          <p className="eyebrow">Parallel demo</p><h2>{state.task.goal}</h2>
          <button className="link" onClick={() => setTaskEditorOpen(value => !value)}>{taskEditorOpen ? 'Hide task editor' : 'Customize tasks'}</button>
          {taskEditorOpen && <TaskEditor state={state} onSave={(goal, workstreams) => act(() => api.updateTask(goal, workstreams))} />}
          <div className="section-title"><span>Workstreams</span><span>{completed} / {state.task.todos.length} steps</span></div>
          {state.task.workstreams.map(workstream => <section className="workstream" key={workstream.id}>
            <div className="section-title"><span>{workstream.title}</span><span>{workstream.owner} · {workstream.status}</span></div>
            <div className="todos">{state.task.todos.filter(todo => todo.workstream_id === workstream.id).map(todo => <div className={`todo ${todo.status}`} key={todo.id}><i>{todo.status === 'completed' ? '✓' : todo.status === 'skipped' ? '–' : ''}</i><div><b>{todo.title}</b><small>Step {todo.step} · {todo.owner ?? 'Unassigned'} · {todo.status}</small></div></div>)}</div>
          </section>)}
          <div className="add-agent"><button disabled={state.agents.length >= state.max_agents || state.running} onClick={async () => { try { await api.addAgent(); await refresh(); } catch (cause) { setError(messageOf(cause)); } }}>{state.agents.length >= state.max_agents ? `${state.max_agents} agents added` : '+ Add agent'}</button><p>All agents are peer workers. Routing uses live status, not predefined roles.</p></div>
        </aside>

        <div className="panel-resizer" role="separator" aria-label="Resize task panel" aria-orientation="vertical" tabIndex={0} onPointerDown={startPanelResize} onKeyDown={event => {
          if (event.key === 'ArrowLeft') setLeftPanelWidth(width => Math.max(250, width - 16));
          if (event.key === 'ArrowRight') setLeftPanelWidth(width => Math.min(480, width + 16));
        }} />

        <main className="center">
          <div className="canvas-status"><span><i />{phaseText(state)}</span><span>Drag nodes to arrange the team</span></div>
          <div className="flow-wrap">
            <ReactFlow nodes={nodes} edges={edges} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onNodeClick={onNodeClick} onPaneClick={() => setSelection(null)} onInit={instance => { flow.current = instance; window.setTimeout(() => void instance.fitView({ padding: .28 }), 20); }} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: .28 }} minZoom={.35} maxZoom={1.5}>
              <Background gap={22} size={1} /><Controls showInteractive={false} />
            </ReactFlow>
          </div>
          <Timeline events={state.events} selection={selection} onSelect={event => setSelection({ kind: 'event', id: event.id })} />
        </main>

        <Inspector state={state} selection={selection} onUpdated={refresh} onError={setError} />
      </div>
      {error && <div className="toast" onClick={() => setError('')}>{error}</div>}
    </div>
  );
}

function Pill({ children }: { children: React.ReactNode }) { return <span className="pill">{children}</span>; }

type EditableWorkstream = Pick<Workstream, 'id' | 'title' | 'goal' | 'input'> & { todos: string[] };

function TaskEditor({ state, onSave }: {
  state: CanvasState;
  onSave: (goal: string, workstreams: EditableWorkstream[]) => Promise<void>;
}) {
  const [goal, setGoal] = useState(state.task.goal);
  const [workstreams, setWorkstreams] = useState<EditableWorkstream[]>(() => editableWorkstreams(state));
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setGoal(state.task.goal);
    setWorkstreams(editableWorkstreams(state));
  }, [state.task.goal, state.task.workstreams]);

  const update = (index: number, field: 'title' | 'goal' | 'input', value: string) => {
    setWorkstreams(current => current.map((item, itemIndex) => itemIndex === index ? { ...item, [field]: value } : item));
  };
  const updateTodo = (taskIndex: number, todoIndex: number, value: string) => {
    setWorkstreams(current => current.map((item, itemIndex) => itemIndex === taskIndex ? {
      ...item, todos: item.todos.map((todo, index) => index === todoIndex ? value : todo),
    } : item));
  };
  const canEdit = !state.running && state.task.jev_call_count === 0 && state.agents.every(agent => agent.runs.length === 0);
  const valid = Boolean(goal.trim()) && workstreams.every(item => item.title.trim() && item.goal.trim() && item.input.trim() && item.todos.length > 0 && item.todos.every(todo => todo.trim()));

  return <div className="task-editor">
    <label>Network goal<textarea rows={3} maxLength={500} disabled={!canEdit} value={goal} onChange={event => setGoal(event.target.value)} /></label>
    {workstreams.map((item, index) => <fieldset key={item.id} disabled={!canEdit}>
      <legend>Task {index + 1}{index < 2 ? ' · demo' : ''}</legend>
      <label>Title<input maxLength={120} value={item.title} onChange={event => update(index, 'title', event.target.value)} /></label>
      <label>Goal<textarea rows={3} maxLength={500} value={item.goal} onChange={event => update(index, 'goal', event.target.value)} /></label>
      <label>Input<textarea rows={5} maxLength={8000} value={item.input} onChange={event => update(index, 'input', event.target.value)} /></label>
      <div className="todo-editor"><span>Todos</span>{item.todos.map((todo, todoIndex) => <div className="todo-edit-row" key={todoIndex}>
        <b>{todoIndex + 1}</b><input maxLength={200} value={todo} placeholder="Describe this step…" onChange={event => updateTodo(index, todoIndex, event.target.value)} />
        <button type="button" aria-label={`Remove Todo ${todoIndex + 1}`} disabled={item.todos.length === 1} onClick={() => setWorkstreams(current => current.map((task, taskIndex) => taskIndex === index ? { ...task, todos: task.todos.filter((_, itemIndex) => itemIndex !== todoIndex) } : task))}>×</button>
      </div>)}<button type="button" onClick={() => setWorkstreams(current => current.map((task, taskIndex) => taskIndex === index ? { ...task, todos: [...task.todos, ''] } : task))}>+ Add Todo</button></div>
      {index >= 2 && <button className="danger-link" type="button" onClick={() => setWorkstreams(current => current.filter((_, itemIndex) => itemIndex !== index))}>Remove task</button>}
    </fieldset>)}
    <button type="button" disabled={!canEdit || workstreams.length >= 10} onClick={() => setWorkstreams(current => [...current, {
      id: `new-${Date.now()}`, title: '', goal: '', input: '', todos: [''],
    }])}>+ Add task</button>
    <button className="primary full" disabled={!canEdit || !valid || saving} onClick={async () => {
      setSaving(true);
      try { await onSave(goal, workstreams); } finally { setSaving(false); }
    }}>{saving ? 'Saving…' : 'Save tasks'}</button>
    {!canEdit && <p className="muted">Reset the demo to customize tasks after execution has started.</p>}
  </div>;
}

function editableWorkstreams(state: CanvasState): EditableWorkstream[] {
  return state.task.workstreams.map(({ id, title, goal, input }) => ({
    id, title, goal, input,
    todos: state.task.todos.filter(todo => todo.workstream_id === id).sort((a, b) => a.step - b.step).map(todo => todo.title),
  }));
}

function Timeline({ events, selection, onSelect }: { events: TeamEvent[]; selection: Selection; onSelect: (event: TeamEvent) => void }) {
  const visible = events.filter(event => !['model_request', 'tool_start', 'tool_end', 'stream_end'].includes(event.type));
  return <section className="timeline panel"><div className="section-title"><span>Workflow timeline</span><span>Click a step for details</span></div><div className="event-row">{[...visible].reverse().map((event, reverseIndex) => <button className={selection?.kind === 'event' && selection.id === event.id ? 'selected' : ''} key={event.id} onClick={() => onSelect(event)}><small>Step {visible.length - reverseIndex} · {new Date(event.timestamp).toLocaleTimeString()}</small><b>{timelineTitle(event)}</b><span>{timelineSummary(event)}</span></button>)}</div></section>;
}

function Inspector({ state, selection, onUpdated, onError }: { state: CanvasState; selection: Selection; onUpdated: () => Promise<void>; onError: (value: string) => void }) {
  if (!selection) return <aside className="inspector panel"><p className="eyebrow">Inspector</p><div className="empty">Select an Agent, JEV Router, or event.</div></aside>;
  if (selection.kind === 'agent') {
    const agent = state.agents.find(item => item.id === selection.id);
    return <aside className="inspector panel"><p className="eyebrow">Inspector</p>{agent ? <AgentEditor agent={agent} todos={state.task.todos} onUpdated={onUpdated} onError={onError} /> : <div className="empty">Agent unavailable.</div>}</aside>;
  }
  if (selection.kind === 'router') {
    const decision = state.task.last_decision;
    return <aside className="inspector panel"><p className="eyebrow">JEV Router</p><h2>Decision history</h2><p className="muted">Every routing call keeps its input, question, output, probabilities, and token usage.</p>{state.task.route_history.length ? <RouteHistory routes={state.task.route_history} /> : <div className="empty compact">No JEV decisions yet.</div>}{decision && <div className="metrics compact-metrics"><Metric name="Total calls" value={String(state.task.route_history.length)} /><Metric name="Latest choice" value={decision.choice} /></div>}</aside>;
  }
  if (selection.kind === 'human') {
    return <HumanPanel state={state} onError={onError} />;
  }
  const event = state.events.find(item => item.id === selection.id);
  return <aside className="inspector panel"><p className="eyebrow">Workflow step</p>{event && <><h2>{timelineTitle(event)}</h2><p className="event-explanation">{timelineSummary(event)}</p><p className="muted">{event.actor_id} · {new Date(event.timestamp).toLocaleString()}</p><ConsoleBlock title="Event payload" value={JSON.stringify(event.payload, null, 2)} /></>}</aside>;
}

function HumanPanel({ state, onError }: { state: CanvasState; onError: (value: string) => void }) {
  const [answer, setAnswer] = useState('');
  const pending = state.task.pending_human_question;
  return <aside className="inspector panel"><p className="eyebrow">Ask User</p><h2>Human input</h2><p className="muted">This system Agent collects missing context and returns it to JEV for another routing decision.</p>{pending ? <div className="human-question"><ConsoleBlock title="Question" value={pending.question} /><label>Your answer<textarea value={answer} rows={6} maxLength={4000} onChange={event => setAnswer(event.target.value)} placeholder="Provide the missing context in English…" /></label><button className="primary full" disabled={!answer.trim()} onClick={async () => { try { await api.answerHuman(answer); setAnswer(''); } catch (cause) { onError(messageOf(cause)); } }}>Submit answer to JEV</button></div> : <div className="empty compact">No question is waiting for an answer.</div>}{state.task.human_interactions.length > 0 && <div className="detail"><h3>Interaction history</h3>{state.task.human_interactions.map((item, index) => <div className="human-history" key={item.id}><b>Question {index + 1}</b><p>{item.question}</p><b>Answer</b><p>{item.answer}</p></div>)}</div>}</aside>;
}

function AgentEditor({ agent, todos, onUpdated, onError }: { agent: TeamAgent; todos: CanvasState['task']['todos']; onUpdated: () => Promise<void>; onError: (value: string) => void }) {
  const [name, setName] = useState(agent.name);
  const [role, setRole] = useState(agent.role);
  useEffect(() => { setName(agent.name); setRole(agent.role); }, [agent]);
  const assignment = todos.find(todo => todo.id === agent.current_todo_id)?.title ?? 'No active assignment.';
  return <><div className="agent-heading"><span>{name.replace(/[^a-zA-Z0-9]/g, '').slice(0,2).toUpperCase()}</span><div><h2>{agent.name}</h2><p>{agent.status} · {agent.runs.length} runs</p></div></div><label>Name<input value={name} maxLength={40} onChange={event => setName(event.target.value)} /></label><label>Optional role<input value={role} maxLength={240} onChange={event => setRole(event.target.value)} placeholder="e.g. Careful reviewer" /></label><p className="muted">JEV considers this role when routing. Leave blank to keep this agent role-free.</p><button className="primary full" onClick={async () => { try { await api.updateAgent(agent.id, name, role); await onUpdated(); } catch (cause) { onError(messageOf(cause)); } }}>Save agent</button><div className="detail"><h3>Current assignment</h3><p>{assignment}</p></div><div className="detail"><h3>CLI history</h3>{agent.runs.length ? <AgentRunHistory runs={agent.runs} /> : <p>No runs yet. The next execution will appear here with input, process, and output.</p>}</div></>;
}

function AgentRunHistory({ runs }: { runs: AgentRun[] }) {
  const [index, setIndex] = useState(runs.length - 1);
  useEffect(() => setIndex(runs.length - 1), [runs.length]);
  const run = runs[Math.max(0, Math.min(index, runs.length - 1))];
  return <div className="run-history"><div className="run-tabs">{runs.map((item, itemIndex) => <button className={itemIndex === index ? 'selected' : ''} key={item.id} onClick={() => setIndex(itemIndex)}>Run {itemIndex + 1}</button>)}</div><div className="run-meta"><span>{run.todo_title}</span><span>{run.model_calls} model · {run.tool_calls} tools</span></div><ConsoleBlock title="Input" value={run.input} /><ConsoleBlock title="Process" value={processText(run)} /><ConsoleBlock title="Output" value={run.output || 'Waiting for output…'} /></div>;
}

function RouteHistory({ routes }: { routes: RouteRun[] }) {
  const [index, setIndex] = useState(routes.length - 1);
  useEffect(() => setIndex(routes.length - 1), [routes.length]);
  const route = routes[Math.max(0, Math.min(index, routes.length - 1))];
  return <div className="run-history"><div className="run-tabs">{routes.map((item, itemIndex) => <button className={itemIndex === index ? 'selected' : ''} key={item.id} onClick={() => setIndex(itemIndex)}>Decision {itemIndex + 1}</button>)}</div><ConsoleBlock title="Input state" value={JSON.stringify(route.input, null, 2)} /><ConsoleBlock title="Question" value={JSON.stringify(route.question, null, 2)} /><ConsoleBlock title="Output" value={JSON.stringify(route.output, null, 2)} />{route.output && <><div className="metrics"><Metric name="Choice" value={route.output.choice} /><Metric name="Confidence" value={`${Math.round(route.output.confidence * 100)}%`} /><Metric name="Input tokens" value={String(route.output.usage.input_tokens ?? '—')} /><Metric name="Output tokens" value={String(route.output.usage.output_tokens ?? '—')} /></div><div className="probabilities">{Object.entries(route.output.probabilities).sort((a,b) => b[1]-a[1]).map(([name, value]) => <div key={name}><span>{name}</span><b>{Math.round(value * 100)}%</b><i><em style={{ width: `${value * 100}%` }} /></i></div>)}</div></>}</div>;
}

function ConsoleBlock({ title, value }: { title: string; value: string }) {
  return <section className="console-block"><h3><span className="terminal-dot" />{title}</h3><pre>{value}</pre></section>;
}

function processText(run: AgentRun) {
  if (!run.process.length) return 'No intermediate model or tool events.';
  return run.process.map((entry, index) => {
    const time = new Date(entry.timestamp).toLocaleTimeString();
    if (entry.type === 'model_request') return `[${index + 1}] ${time} MODEL REQUEST · ${String(entry.payload.input_items ?? '?')} input items`;
    if (entry.type === 'tool_start') return `[${index + 1}] ${time} TOOL START · ${String(entry.payload.name)}\n${String(entry.payload.arguments ?? '')}`;
    if (entry.type === 'tool_end') return `[${index + 1}] ${time} TOOL END · ${String(entry.payload.name)}\n${String(entry.payload.output ?? '')}`;
    return `[${index + 1}] ${time} ${entry.type.toUpperCase()}\n${JSON.stringify(entry.payload, null, 2)}`;
  }).join('\n\n');
}

function Metric({ name, value }: { name: string; value: string }) { return <div><span>{name}</span><b>{value}</b></div>; }
function label(value: string) { return value.split('_').map(part => part[0].toUpperCase() + part.slice(1)).join(' '); }
function timelineTitle(event: TeamEvent) {
  const titles: Record<string, string> = { task_ready: 'Task prepared', task_customized: 'Tasks customized', execution_mode_changed: 'Execution mode changed', agent_added: 'Agent joined the team', agent_updated: 'Agent settings updated', agent_started: 'Agent started a Todo', agent_completed: 'Agent completed its Todo', router_started: 'JEV received routing input', router_decided: 'JEV chose the next action', workstream_completed: 'Workstream completed', human_question: 'User input requested', human_answered: 'User answered the team', todo_assigned: 'Todo handed to an Agent', fallback_route: 'Fallback routing used', task_completed: 'Task completed', task_paused: 'Task paused', task_resumed: 'Task resumed', step_failed: 'Step failed' };
  return titles[event.type] ?? label(event.type);
}
function timelineSummary(event: TeamEvent) {
  const payload = event.payload;
  switch (event.type) {
    case 'task_ready': return 'The predefined call-review task and Todo list are ready.';
    case 'task_customized': return 'The task network was customized before execution.';
    case 'execution_mode_changed': return `Execution mode is now ${String(payload.mode)}.`;
    case 'agent_added': return `${String(payload.name ?? event.actor_id)} is now available for routing.`;
    case 'agent_updated': return `${event.actor_id}'s name changed.`;
    case 'agent_started': return `${event.actor_id} is working on “${String(payload.title ?? payload.todo_id)}”.`;
    case 'agent_completed': return `${event.actor_id} returned an output after ${String(payload.model_calls ?? '?')} model call(s).`;
    case 'router_started': return `${String(payload.workstream_id ?? 'A workstream')} entered JEV routing.`;
    case 'router_decided': return `JEV selected ${String(payload.choice)} with ${Math.round(Number(payload.confidence ?? 0) * 100)}% confidence.`;
    case 'human_question': return 'Ask User is waiting for missing context.';
    case 'human_answered': return 'The answer was added to state and will be reconsidered by JEV.';
    case 'todo_assigned': return `“${String(payload.title ?? payload.todo_id)}” was assigned to ${event.actor_id}.`;
    case 'fallback_route': return `The budget-safe fallback assigned the next Todo to ${event.actor_id}.`;
    case 'task_completed': return 'The latest approved response became the final result.';
    case 'step_failed': return String(payload.error ?? 'The workflow step failed.');
    default: return `${event.actor_id} emitted ${label(event.type)}.`;
  }
}
function messageOf(cause: unknown) { return cause instanceof Error ? cause.message : String(cause); }
function phaseText(state: CanvasState) {
  if (state.running) return state.task.phase === 'router' ? 'JEV is routing' : 'Agent is working';
  if (state.task.status === 'completed') return 'Task completed';
  if (state.task.status === 'failed') return 'Step failed';
  if (state.task.pending_human_question) return 'Human input requested; other work can continue';
  if (state.task.status === 'needs_human') return 'Human decision required';
  return state.task.phase === 'router' ? 'Ready for JEV routing' : 'Ready for next Agent';
}
