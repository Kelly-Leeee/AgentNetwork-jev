# AgentNetwork - Jev as Router

https://github.com/user-attachments/assets/cf139790-4a4c-49d3-9909-50828d144e2e

**A role-optional multi-agent network built to scale from two agents to thousands with one JEV router and general-purpose AI agents.**

[中文](README.zh-CN.md)

AgentRouter connects JEV to an agent team. It replaces complex routing models, fixed expert roles, and lead-agent hierarchies with one fast JEV router that reads live team state and sends each task to an available general-purpose agent.

Agents work in parallel. Whenever one finishes, JEV can immediately find an idle agent and assign the next task without waiting for the rest of the team. Roles are optional prompt hints, not fixed job boundaries.

This repository demonstrates:

- JEV-based multi-agent routing
- parallel agent orchestration
- role-free and role-optional agent teams
- dynamic idle-agent selection
- human-in-the-loop workflows
- OpenAI Responses API agents with real tools

## Quickstart

Requirements: Python 3.10+, an OpenAI-compatible API key, and a JEV API key.

### 1. Clone

```bash
git clone https://github.com/Kelly-Leeee/agentrouter.git
cd agentrouter
```

### 2. Add API keys

```bash
export OPENAI_API_KEY="your-openai-key"
export JEV_API_KEY="your-jev-key"
```

Optional model or endpoint configuration:

```bash
export OPENAI_BASE_URL="https://api.openai.com/v1"
export OPENAI_MODEL="gpt-5-mini"
export JEV_MODEL="jev-latest"
```

### 3. Start

```bash
./start.sh
```

The script installs the Python environment, starts AgentRouter, and opens:

```text
http://127.0.0.1:8765
```

For a remote or headless machine:

```bash
./start.sh --no-open --host 0.0.0.0
```

AgentRouter is an early research prototype. It currently ships with a demo workflow and supports up to three agents. Keep API keys in environment variables; never commit them.

Apache-2.0 license.
