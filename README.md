# AgentRouter

[中文文档](README.zh-CN.md)

AgentRouter is a research prototype for serving multiple users with a pool of general-purpose AI agents. Agents need neither fixed job titles nor a lead agent: each available agent can execute work end to end, while a lightweight JEV router assigns the next task. An optional one-line role can adjust an individual agent's prompt without removing its general capabilities.

The repository includes a browser-based Agent Team Canvas, a real OpenAI Responses API tool loop, concurrent workstreams, human-in-the-loop questions, per-agent conversations, and local file, command, search, and webpage tools.

## Run the Canvas

Requirements: Python 3.10+, an OpenAI-compatible API key, and a JEV API key. Node.js is **not** required to run the checked-in Canvas; it is only needed when changing the frontend.

```bash
git clone https://github.com/Kelly-Leeee/agentrouter.git
cd agentrouter
export OPENAI_API_KEY="your-key"
export JEV_API_KEY="your-key"
./start.sh
```

The first run creates `.venv`, installs the package, starts the server, and opens [http://127.0.0.1:8765](http://127.0.0.1:8765). Use `./start.sh --no-open` on a headless machine.

Optional provider/model overrides:

```bash
export OPENAI_BASE_URL="https://api.openai.com/v1"
export OPENAI_MODEL="gpt-5-mini"
export JEV_MODEL="jev-latest"
./start.sh
```

Environment variables override file settings. For persistent local configuration, copy `settings.example.json` to `settings.json`; the latter is ignored by Git. You may also set `AGENTROUTER_SETTINGS=/path/to/settings.json` or pass `--settings`.

## Instructions for coding agents

If you received only this GitHub URL, follow this deterministic procedure:

1. Clone the repository and enter its root.
2. Check that `OPENAI_API_KEY` and `JEV_API_KEY` are available; ask the user for missing credentials and never commit them.
3. Run `./start.sh`. Do not rebuild the frontend unless its source changed.
4. Wait for `Uvicorn running on http://127.0.0.1:8765`, then open that URL for the user.
5. In a remote/headless environment, run `./start.sh --no-open --host 0.0.0.0` and expose port `8765` using the environment's normal preview mechanism.

## What the Canvas demonstrates

The demo starts with two independent, three-step customer-service workstreams. Clicking **Run next step** executes assigned work concurrently. As soon as an agent finishes, JEV receives the current team state and can assign the next Todo to an available peer, ask a human, or finish that workstream without waiting for other agents.

The current demo supports up to three isolated agents. Each may have an optional one-line role. With no role it remains a general-purpose peer; with a role, that text enters both its system prompt and the state seen by JEV. There is no mandatory lead-agent hierarchy.

Conversations are stored under `chats/<agent-id>/<YYYY-MM-DD>.json`, with at most five date files per agent. `chats/` is ignored by Git.

## CLI agent

```bash
.venv/bin/agentrouter "List the Python files and explain the project structure"
```

Run `.venv/bin/agentrouter` without a prompt for interactive mode. Add `--verbose` for tool events. Tools include `read_file`, `list_files`, `write_file`, `run_command`, `web_search`, and `web_fetch`.

Paths and command working directories must remain inside `agent.workspace`. `run_command` launches real host processes and is not a security sandbox; use a container or OS sandbox for untrusted deployments. Network fetching rejects localhost, private, link-local, and other non-public addresses.

## Frontend development

The compiled Canvas is committed as a runtime asset so a fresh clone works without Node.js. When changing `frontend/`, rebuild it before committing:

```bash
cd frontend
npm ci
npm run build
cd ..
```

The build writes to `agentrouter/web/dist`. Commit updated source and generated assets together.

## Testing

```bash
.venv/bin/python -m unittest discover -s tests -v
cd frontend && npm ci && npm run build
```

Core modules: `agentrouter/agent.py` (agent state), `agentrouter/loop.py` (tool loop), `agentrouter/tools.py` (local tools), `agentrouter/team.py` (orchestration and routing), `agentrouter/server.py` (Canvas API), and `agentrouter/config.py` (configuration).

## Status and security

AgentRouter is an early research prototype, not a hardened multi-tenant service. The Canvas currently contains a demonstration scenario and supports at most three agents. Keep secrets in environment variables or an ignored local settings file. Review write, execution, and network permissions before exposing it beyond localhost.

License: Apache-2.0. See [LICENSE](LICENSE).

OpenAI references: [Responses API](https://developers.openai.com/api/reference/responses/overview), [function calling](https://developers.openai.com/api/docs/guides/agents-api/tools/functions), and [conversation state](https://developers.openai.com/api/docs/guides/conversation-state).
