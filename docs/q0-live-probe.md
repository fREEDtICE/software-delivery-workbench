# Q0 独立构建与回执恢复

固定版 Multica 的生产 DSH 后端通过协议 v1 启动固定版 DSH。真实模型在本机通过保存工具生成受控快照，由独立的 GitHub 托管 Linux 运行器编译和上传。平台记录请求标识，按该标识查询远端任务。两种场景的三个产物行为检查均通过。

| 场景 | 验证结果 | 证据 |
| --- | --- | --- |
| 正常 | Agent 通过保存工具产出快照。编译端核对源码摘要，运行产物得到 21、0、200。远端记录一次受理、一次编译。 | [运行](https://github.com/fREEDtICE/software-delivery-workbench/actions/runs/44477034391) · [产物和回执](https://github.com/fREEDtICE/software-delivery-workbench/actions/runs/44477034391/artifacts/14506159331) |
| 回执丢失 | 远端已接单时派发进程退出。本地只有请求标识。新进程查回原任务，重复派发被拒绝，复查仍为同一回执和一次编译。重建临时工作目录后，原 DSH 会话读到了该构建结果。 | [运行](https://github.com/fREEDtICE/software-delivery-workbench/actions/runs/44477133149) · [产物和回执](https://github.com/fREEDtICE/software-delivery-workbench/actions/runs/44477133149/artifacts/14506502550) |

[完整取证 JSON](q0-live-evidence.json) 包含真实模型用量、保存事件、源码与产物摘要、故障后的本地记录及恢复后的会话和任务号。

## 复现

1. 按 [版本锁定文件](../upstreams.lock.json) 检出 Multica 和 DSH。桥接源码使用 [固定提交](https://github.com/multica-ai/dsh-multica-runtime/tree/571d7c036d15f58ee88c89fd80afb0a6c7f0023c)。
2. 使用 Node 24.12.0（需含 Node-API 开发头文件）和 pnpm 11.28.5，在 DSH 检出目录执行 `pnpm install --frozen-lockfile` 和 `pnpm run build:artifacts`。桥接目录使用其锁文件安装依赖并执行 `pnpm run build`。本次运行从官方 CDN 取得 xlsx 依赖并核对锁文件完整性后，通过本机镜像供 pnpm 读取同一份文件。
3. 在工作台仓库根目录运行 `python3 tools/q0_agent/prepare_profile.py <DSH目录> <桥接目录> <Node绝对路径> <新的试验目录>`。启动本机 Ollama，准备 `qwen3:1.7b`。由试验目录的 `dsh --profile multica --probe` 检查协议。配置只保留此用例所需的保存能力，把外连限制到本机模型端口。
4. 在 Multica 检出的 `server/` 目录执行 `go build -o <试验二进制> <工作台>/tools/q0_agent/probe.go`。调用二进制的 `-help` 可见全部参数。首轮传入 `-dsh`、`-model q0-ollama/qwen3:1.7b`、`-cwd`、`-session-root`、`-python`、`-tool` 和 `-saved`。外部会话目录与保存文件均位于试验目录内，和本轮临时工作目录分开。检查返回的成功保存事件与文件摘要相符。
5. 运行 `python3 tools/q0_external_job.py <状态目录> save <快照>`，再用同一个状态目录依次执行 `submit` 和 `reconcile`。故障用一个新的状态目录，在正常用例通过后执行 `submit --lose-receipt`。该进程应以 75 退出，`request.json` 应为 `awaiting_receipt` 且没有 `run_id`。在新进程执行 `reconcile`，核对远端任务号和实际编译次数。用同一会话目录、同一路径下新建的工作目录以及 `-resume <原Session> -job <查回的任务号>` 启动新的 DSH 进程。

## 适用范围

官方桥接构建通过，但其子 Agent 收尾代码调用了固定版 DSH 已移除的 `waitForChildren`。该问题在一次真实保存后复现。试验配置停用子 Agent 服务，保留两份上游代码与桥接源码版本，覆盖本次单 Agent 链路。完整 Multica 部署、生产编译集群和允许任意代码的工作区均需各自验证。

编译端使用 Q0 专用的托管工作流和受限算术样本。恢复只查询已发送的请求。请求查不到时保持待核对，避免盲重发。这里验证的是本次受理后崩溃的结果，未对第三方调度 API 作普遍的恰好一次保证。
