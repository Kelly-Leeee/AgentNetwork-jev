# AgentRouter

一个真实执行工具的 OpenAI agent loop。它沿用 `agentmem/replay_agent_loop.py` 的基本循环：模型决定工具调用，本地执行，再把 `function_call_output` 返回模型，直到最终答复。

区别是这里不再消费历史 `debugMsg`，工具会真的在本机执行。实现使用 OpenAI Responses API，并将工具注册、执行策略和 loop 分开，后续可以接 MCP、HTTP API 或业务工具。

## 快速开始

要求 Python 3.9+。

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

- `read_file` / `list_files` / `search_files`：读取和定位 workspace 内容；
- `write_file`：创建或完整覆写文件；
- `run_command`：以 argv 数组运行脚本、测试或构建命令。

文件路径和命令工作目录都必须位于 `settings.agent.workspace`。命令不用 shell 解释，因此不支持管道、重定向或 `&&`，但可以直接执行 `python3 script.py`、`git status`、`npm test` 等程序。执行有超时和输出长度限制。

`allow_write`、`allow_exec` 可以分别关闭写入和执行能力。需要注意：`run_command` 是真实主机进程能力，并不是容器沙箱；它启动的程序仍可能自行访问 workspace 以外的资源。生产环境应把整个 agent 放入容器或 OS sandbox。

代码侧可随时调用 `loop.stop()` 请求停止。loop 会在模型/工具边界收口；如果正在运行本地命令，会先 terminate，必要时 kill。同步 OpenAI 请求本身无法从 SDK 通用地强制取消，因此恰好正在等待模型响应时，会在该请求返回或超时后停止，不会进入下一轮。

环境变量 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL` 优先于文件配置。可用 `--settings path/to/settings.json` 切换配置。

## 开发

```bash
python -m unittest discover -s tests -v
```

核心文件：`agentrouter/loop.py`（agent loop）、`agentrouter/tools.py`（真实读/写/执行工具）、`agentrouter/config.py`（配置）、`agentrouter/cli.py`（CLI）。工具分层参考了 OpenClaw 的 filesystem/runtime 分类。

OpenAI 官方文档：[Responses API](https://developers.openai.com/api/reference/responses/overview)、[Function calling](https://developers.openai.com/api/docs/guides/agents-api/tools/functions)、[Conversation state](https://developers.openai.com/api/docs/guides/conversation-state)。
