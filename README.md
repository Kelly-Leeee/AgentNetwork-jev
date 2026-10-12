# AgentRouter

一个真实执行工具的 OpenAI agent loop。它沿用 `agentmem/replay_agent_loop.py` 的基本循环：模型决定工具调用，本地执行，再把 `function_call_output` 返回模型，直到最终答复。

区别是这里不再消费历史 `debugMsg`，工具会真的在本机执行。实现使用 OpenAI Responses API，并将工具注册、执行策略和 loop 分开，后续可以接 MCP、HTTP API 或业务工具。

## 快速开始

要求 Python 3.10+；项目默认使用 Python 3.12。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

编辑 `settings.json` 填入 API key，或推荐使用环境变量：

```bash
export OPENAI_API_KEY="..."
agentrouter "列出当前目录的 Python 文件，并说明结构"
```

交互模式运行 `agentrouter`；单次运行可加 `--verbose` 查看工具事件。API key 为空时只会给出明确错误，不会发请求。

默认底层能力包括：

- `read_file` / `list_files`：读取和定位 workspace 内容；
- `write_file`：创建或完整覆写文件；
- `run_command`：以 argv 数组运行脚本、测试或构建命令。
- `web_search`：通过 `ddgs` 免费搜索公开网页，不需要 API key；
- `web_fetch`：抓取公开 URL，并通过 `trafilatura` 提取适合模型阅读的正文。

文件路径和命令工作目录都必须位于 `settings.agent.workspace`。命令不用 shell 解释，因此不支持管道、重定向或 `&&`，但可以直接执行 `python3 script.py`、`git status`、`npm test` 等程序。执行有超时和输出长度限制。

`allow_write`、`allow_exec` 可以分别关闭写入和执行能力。需要注意：`run_command` 是真实主机进程能力，并不是容器沙箱；它启动的程序仍可能自行访问 workspace 以外的资源。生产环境应把整个 agent 放入容器或 OS sandbox。

`allow_web` 可以关闭网络工具。`web_fetch` 只允许 HTTP(S)，会拒绝 localhost、私网、link-local 和其他非公网地址，并限制超时、重定向、下载体积与返回正文长度。默认搜索后端是无需 key 的 [DDGS](https://github.com/deedy5/duckduckgo_search)，正文抽取使用 [Trafilatura](https://github.com/adbar/trafilatura)。

代码侧可随时调用 `loop.stop()` 请求停止。loop 会在模型/工具边界收口；如果正在运行本地命令，会先 terminate，必要时 kill。同步 OpenAI 请求本身无法从 SDK 通用地强制取消，因此恰好正在等待模型响应时，会在该请求返回或超时后停止，不会进入下一轮。

环境变量 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL` 优先于文件配置。可用 `--settings path/to/settings.json` 切换配置。

`settings.agent.stream` 默认为 `true`。CLI 会消费 Responses API 的 `response.output_text.delta` 事件并实时打印，同时在 `response.completed` 后保存完整输出和 transcript；关闭时改为普通阻塞响应。

## 开发

```bash
python -m unittest discover -s tests -v
```

核心文件：`agentrouter/agent.py`（Agent 与 AgentState）、`agentrouter/loop.py`（底层单次 agent loop）、`agentrouter/tools.py`（真实读/写/执行工具）、`agentrouter/prompt.py`（系统提示词字符串）、`agentrouter/config.py`（配置）、`agentrouter/cli.py`（CLI）。工具分层参考了 OpenClaw 的 filesystem/runtime 分类，Agent/loop 分层参考 Pi Agent。

默认 system prompt 是 `agentrouter/prompt.py` 中的 `SYSTEM_PROMPT` 字符串。如需为某个部署单独定制，可在 `settings.json` 的 `agent` 对象中增加字符串字段 `system_prompt` 覆盖默认值。当前不会加载任何 Markdown prompt 文件。

## Agent Team Canvas

本地画布使用 React + TypeScript、React Flow 和 FastAPI。先构建前端：

```bash
cd frontend
npm install
npm run build
cd ..
```

确认 `settings.json` 同时配置了 `openai.api_key` 和 `jev.api_key`，然后启动：

```bash
source .venv/bin/activate
agentrouter --canvas
```

浏览器打开 `http://127.0.0.1:8765`。画布预置两个相互独立的三步客服 workstream：Agent 1 从投诉回复开始，Agent 2 从会议改期确认开始。每次点击 `Run next step` 会真正并发执行两个 workstream 当前已分配的步骤；任一 Agent 先完成时，其 workstream 会立即进入 JEV 路由，不会等待另一个 Agent。JEV 只根据实时状态决定下一步交给哪个空闲 peer 或提前结束，不使用预设角色。画布分别显示 Todo、Agent run、JEV decision 和事件状态，并支持最多三个隔离 Agent。

用户可以分别为每个 Agent 配置一行可选 role。配置后，该 role 会附在对应 Agent 的信息中进入 JEV 路由 state，同时作为可调整部分加入该 Agent 的 system prompt，以引导它后续执行任务时的行为侧重；它不会移除 Agent 的通用能力或工具。未配置 role 的 Agent 保持原有 prompt，并按普通空闲 peer 参与无角色路由。

Agent 对话保存在 `chats/<agent-id>/<YYYY-MM-DD>.json`，每个 Agent 最多保留 5 个日期文件；`chats/` 已被 Git 忽略。

OpenAI 官方文档：[Responses API](https://developers.openai.com/api/reference/responses/overview)、[Function calling](https://developers.openai.com/api/docs/guides/agents-api/tools/functions)、[Conversation state](https://developers.openai.com/api/docs/guides/conversation-state)。
