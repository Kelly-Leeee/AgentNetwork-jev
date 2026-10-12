# AgentRouter

**一个简单的、role-optional 的 multi-agent router：一个 JEV router + 一组通用智能体。**

[English](README.md)

AgentRouter 把 JEV 接入了 agent team。它用一个快速的 JEV router 代替复杂 routing model、固定专家角色和 lead-agent 层级。JEV 读取实时 team state，把任务交给当前可用的通用智能体。

多个智能体可以并行工作。任何智能体完成任务后，JEV 都能立刻找到空闲智能体并分配下一项任务，不需要等待整个 team。Role 只是可选的 prompt 提示，不是固定的能力边界。

这个仓库演示了：

- 基于 JEV 的 multi-agent routing
- 并行 agent orchestration
- role-free 和 role-optional agent team
- 动态寻找空闲智能体
- human-in-the-loop workflow
- 使用真实工具的 OpenAI Responses API agent

## Quickstart

需要 Python 3.10+、OpenAI-compatible API key 和 JEV API key。不需要 Node.js。

### 1. Clone

```bash
git clone https://github.com/Kelly-Leeee/agentrouter.git
cd agentrouter
```

### 2. 配置 API key

```bash
export OPENAI_API_KEY="你的-openai-key"
export JEV_API_KEY="你的-jev-key"
```

如果需要修改模型或 API endpoint：

```bash
export OPENAI_BASE_URL="https://api.openai.com/v1"
export OPENAI_MODEL="gpt-5-mini"
export JEV_MODEL="jev-latest"
```

### 3. 启动

```bash
./start.sh
```

脚本会自动安装 Python 环境、启动 AgentRouter，并打开：

```text
http://127.0.0.1:8765
```

远程或无图形界面的机器使用：

```bash
./start.sh --no-open --host 0.0.0.0
```

AgentRouter 目前是早期研究原型，内置一个 demo workflow，最多支持三个智能体。API key 应保存在环境变量中，切勿提交到 Git。

Apache-2.0 license。
