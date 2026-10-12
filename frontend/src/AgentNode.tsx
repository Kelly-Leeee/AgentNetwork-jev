import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';

export type AgentNodeData = {
  label: string;
  subtitle: string;
  status: string;
  kind: 'agent' | 'router' | 'human';
};

export type AgentFlowNode = Node<AgentNodeData, 'agent'>;

export function AgentNode({ data, selected }: NodeProps<AgentFlowNode>) {
  const initials = data.kind === 'router'
    ? 'J' : data.kind === 'human' ? '?'
    : data.label.replace(/[^a-zA-Z0-9]/g, '').slice(0, 2).toUpperCase();
  return (
    <div className={`flow-node ${data.kind} ${data.status} ${selected ? 'selected' : ''}`}>
      <Handle type="target" position={Position.Top} />
      <span className="node-avatar">{initials}</span>
      <span className="node-copy"><b>{data.label}</b><small>{data.subtitle}</small></span>
      <span className="node-status">{data.status}</span>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}
