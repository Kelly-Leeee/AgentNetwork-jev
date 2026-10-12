# AgentRouter

[English](README.md)

AgentRouter 是一个面向“多个用户共享一组通用智能体”的研究原型。系统不要求预设固定工种或 lead agent：每个空闲智能体都能端到端执行任务，由轻量级 JEV router 决定下一项工作交给谁。用户也可以为单个智能体填写一句可选 role，用它调整该智能体的 prompt，但不会移除其通用能力。

仓库包含浏览器端 Agent Team Canvas、真实执行工具的 OpenAI Responses API loop、并发 workstream、人类介入提问、按智能体隔离的对话存储，以及本地文件、命令、搜索和网页读取工具。

## 启动画布

需要 Python 3.10+、OpenAI-compatible API key 和 JEV API key。运行仓库中已经构建好的页面**不需要 Node.js**；只有修改前端源码时才需要。

```bash
git clone https://github.com/Kelly-Leeee/agentrouter.git
cd agentrouter
export OPENAI_API_KEY="你的-key"
export JEV_API_KEY="你的-key"
./start.sh
```

第一次运行会创建 `.venv`、安装 Python 包、启动服务，并自动打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。无图形界面的机器使用 `./start.sh --no-open`。

兼容服务或模型可通过环境变量调整：

```bash
export OPENAI_BASE_URL="https://api.openai.com/v1"
export OPENAI_MODEL="gpt-5-mini"
export JEV_MODEL="jev-latest"
./start.sh
```

环境变量优先于文件配置。如需持久化本地配置，将 `settings.example.json` 复制为 `settings.json`；后者已被 Git 忽略。也可以设置 `AGENTROUTER_SETTINGS=/path/to/settings.json` 或传入 `--settings`。

## 给 AI 编程助手的明确指令

如果你只拿到了这个 GitHub 链接，请严格执行：

1. clone 仓库并进入仓库根目录。
2. 检查环境中是否存在 `OPENAI_API_KEY` 和 `JEV_API_KEY`；缺失时向用户索取，绝不能把 key 提交到 Git。
3. 运行 `./start.sh`。除非修改过前端源码，否则不要重建前端。
4. 等待终端出现 `Uvicorn running on http://127.0.0.1:8765`，然后为用户打开该地址。
5. 在远程或无界面环境中，运行 `./start.sh --no-open --host 0.0.0.0`，再用当前环境的标准预览方式暴露 `8765` 端口。

## Canvas 展示了什么

演示场景包含两个相互独立的三步客服 workstream。点击 **Run next step** 后，已分配的工作会并发执行。任意智能体完成后，JEV 会立即接收当前 team state，并决定把下一个 Todo 交给哪个空闲 peer、向人提问，或结束该 workstream；它不必等待其他智能体。

当前 demo 最多支持三个相互隔离的智能体。每个智能体都有一句可选 role：留空时它是普通的通用 peer；填写后，该文本会同时进入智能体 system prompt 和 JEV 可见的状态。系统没有强制的 lead-agent 层级。

对话保存在 `chats/<agent-id>/<YYYY-MM-DD>.json`，每个智能体最多保留五个日期文件。`chats/` 不会被 Git 提交。

## 单智能体 CLI

```bash
.venv/bin/agentrouter "列出当前目录的 Python 文件，并说明项目结构"
```

不提供 prompt 时进入交互模式；加 `--verbose` 可查看工具事件。工具包括 `read_file`、`list_files`、`write_file`、`run_command`、`web_search` 和 `web_fetch`。

文件路径和命令工作目录必须位于 `agent.workspace`。`run_command` 会启动真实主机进程，并不是安全沙箱；处理不可信任务时应使用容器或 OS sandbox。网页读取会拒绝 localhost、私网、link-local 和其他非公网地址。

## 前端开发与打包

为了让新 clone 的仓库无需 Node.js 就能运行，构建后的 Canvas 会作为运行时资产提交。修改 `frontend/` 后，提交前必须重新构建：

```bash
cd frontend
npm ci
npm run build
cd ..
```

构建结果写入 `agentrouter/web/dist`。请把前端源码和构建产物一起提交。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
cd frontend && npm ci && npm run build
```

核心模块：`agentrouter/agent.py`（智能体状态）、`agentrouter/loop.py`（工具循环）、`agentrouter/tools.py`（本地工具）、`agentrouter/team.py`（团队编排与路由）、`agentrouter/server.py`（Canvas API）和 `agentrouter/config.py`（配置）。

## 项目状态与安全说明

AgentRouter 是早期研究原型，不是经过加固的多租户服务。Canvas 当前包含固定演示场景，最多支持三个智能体。请把密钥放在环境变量或被忽略的本地配置文件中；将服务暴露到 localhost 之外以前，应重新检查写文件、执行命令和网络权限。

许可证：Apache-2.0，参见 [LICENSE](LICENSE)。
