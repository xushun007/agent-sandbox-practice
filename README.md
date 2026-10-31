# Sandbox Practice

独立的 sandbox 生命周期实验项目。被测对象可以是 Navi Agent；Navi Agent 源码不在本项目中修改。

## 实验边界

- 外部 Python 调度器：创建/查询/销毁 sandbox，触发 snapshot/restore，记录阶段耗时和结果。
- Modal Sandbox 内：运行独立 Navi Agent 副本及状态探针。
- Navi Agent 数据目录：`NAVI_HOME=/root/.navi-agent`，包含配置、SQLite session/state、日志、记忆和技能数据。
- 第一轮用合成状态和无副作用的一次性 Agent prompt；不启动微信网关，不在镜像或结果中写入密钥。

## 实验阶段

1. `probe`：测 VM Sandbox 创建、运行进程、memory snapshot 和从快照恢复；检查 PID、内存计数器、文件状态及 HTTP probe。
2. `navi-fs`：在 Sandbox 内运行 Navi Agent 一次，持久化 `NAVI_HOME`，做 filesystem snapshot，销毁原实例后从快照创建新实例，比较 session/state DB、memory、trace 和日志文件。
3. `navi-memory`：以 Sandbox 主命令启动 Navi Agent，调度器在其运行期间尝试 memory snapshot。记录成功/失败、恢复后的进程和 Navi 数据状态。任务很快结束时保留结果，不人为把它描述为不支持。

每项记录 Sandbox 创建、Agent 启动、Agent 结束、snapshot、restore API、恢复后 probe 可执行的单调时钟耗时。Modal 不一定公开快照实际字节数；项目会记录可见的文件树大小、SQLite 大小和 RSS，无法取得的后端字节数标记为 unavailable，不推算。

## 环境

需要 Python 3.12+、`uv`、Modal 账号和本地 Modal CLI 身份认证。认证只保存在本机 Modal 配置，不写进项目。

```bash
uv sync
modal setup
```

Navi Agent 的一次性模型调用需要 Modal Secret（默认名 `navi-practice-model`），字段：

- `NAVI_API_KEY`
- `NAVI_BASE_URL`（例如 `https://api.deepseek.com`）
- `NAVI_MODEL`（例如 `deepseek-chat`）

创建 Secret 时请使用 Modal Dashboard/CLI 的交互式 secret 管理，不要把 key 写到命令行历史或本仓库。

## 运行

查看 SDK 版本；首次真正调用 sandbox 时 Modal 会验证本地身份：

```bash
uv run sandbox-practice doctor
```

纯 sandbox 状态探针（不调用模型）：

```bash
uv run sandbox-practice probe --output results/probe.json
```

Navi Agent filesystem snapshot：

```bash
uv run sandbox-practice navi-fs \
  --secret navi-practice-model \
  --prompt "请用一句话说明你正在运行的实验环境。" \
  --output results/navi-fs.json
```

从已有 filesystem snapshot 恢复，并以相同 session ID 向 Navi Agent 提交一个后续问题（需要一次模型调用）：

```bash
uv run sandbox-practice navi-fs-resume \
  --snapshot-id im-REPLACE_WITH_FILESYSTEM_SNAPSHOT_ID \
  --session-id sandbox-practice-session \
  --prompt '请复述我上一条请求以及你当时给出的运行环境描述。' \
  --output results/navi-fs-resume.json
```

这会在新 Sandbox 中启动新的 Agent 进程；若它能准确复述上一轮内容，说明 Navi 的 session/state 数据可供新进程继续使用，不代表旧进程从原指令位置恢复。

Navi Agent memory snapshot 尝试：

```bash
uv run sandbox-practice navi-memory \
  --secret navi-practice-model \
  --prompt '请执行一次 bash 命令 `sleep 10; echo snapshot-marker`，然后确认命令输出。' \
  --snapshot-after-seconds 2 \
  --output results/navi-memory.json
```

`navi-memory` 由 sandbox 的主命令启动 Navi Agent，调度器在外部发起快照；它不通过 `Sandbox.exec()` 把 Agent 放到后台。实际能否在任务进行中恢复，由 Modal 当前的 memory snapshot 能力决定，实验会如实保存错误、进程和数据表现。
Agent 结束后，guest wrapper 最多保留 120 秒，方便外部读取退出标记；恢复后默认最多等待 30 秒观察 Agent 是否完成。超时后调度器会清理恢复实例。

如果需要从已生成的快照继续观察任务是否完成，可复用结果中的 snapshot ID，不必重新发起 Agent prompt：

```bash
uv run sandbox-practice resume-navi \
  --snapshot-id sn-REPLACE_WITH_SNAPSHOT_ID \
  --timeout 60 \
  --output results/navi-resume.json
```

## 结果

结果 JSON 默认包括 provider、sandbox ID、snapshot ID、镜像/软件版本、操作时间、退出状态、Agent 原始 stdout/stderr、文件清单、文件 SHA-256、Navi state DB 与 workspace 大小，以及恢复前后文件差异。恢复后的 `/root/.navi-agent` 文件（排除含运行配置的 `config.yaml`）会同步到与 JSON 同名的 `.artifacts/navi-home/` 目录。这样文章分析需要的回复和状态文件都留在本地。snapshot 和 sandbox 会在正常退出或错误时尽量清理；`--keep` 可保留实例用于人工检查，需手动销毁并承担继续计费。

## 注意

- Modal memory snapshot 是实验性功能；快照会终止源 sandbox，restore 创建新 sandbox。开放 TCP 连接需重连，memory snapshot 有保留期及恢复实例类型限制。
- 文件系统快照不会把 API key 写入 Navi 配置文件；memory snapshot 会保存进程内存，运行时 Secret 可能因此包含在内存状态中。只用可撤销的实验 key，prompt 不得要求 Agent 输出密钥。
- 每个命令只运行一个 Agent 样本，不循环、不并发。API 调用和 sandbox 运行会产生费用。
- OpenSandbox 实践部署在远端独立 Linux 主机（优先 Alibaba Cloud ECS；若已有其他 Linux VM 也可复用）。本机仅运行 SDK/API 客户端，不需要本机 Docker daemon。学习版先在单机 Docker 部署 OpenSandbox Server 与 Docker backend；对不可信 Navi Agent 任务启用 gVisor/Kata 等安全 runtime 并限制网络出口。验证快照能力时按具体 OpenSandbox 版本/runtime 核对兼容性；生产化再考虑 Kubernetes、RuntimeClass、持久化存储和鉴权。Modal 不承载 OpenSandbox Server 或其 Docker daemon。

## 文章整理待办

- 制作一张 Excalidraw 架构与生命周期图，说明本机调度器、Modal App/SDK、Image、gVisor/VM runtime、Sandbox、Navi Agent、状态文件，以及 filesystem/memory snapshot 的交互；插入最终文章。
- 如进行 OpenSandbox 对照，另画或扩展图表示自建控制面和 Docker/Kubernetes runtime，避免把它画成 Modal 的同类托管云 provider。
