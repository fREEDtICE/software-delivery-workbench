# Q0 模型网关复验与 Q1 准入

本次将目的地检查放在 DSH 的真实模型请求入口。试验用临时凭据绑定受限工作范围。请求体里的声明不改变这一范围。网关先按真实目的地记录授权结论，放行后才转发。受控 Runtime 在测试中能够访问网关，直连 Ollama 被隔离策略阻断。

## 实测与可核对证据

三条路径使用同一份合成源码，保存版本摘要为 `09d00553415582ebe4dd69250fe4b6fcb5d7cdad2f78a5670f54bbb738913060`。

| 路径 | 网关或策略记录 | 独立接收端记录 |
| --- | --- | --- |
| DSH → 授权模型 | 工作凭据含读取和调用权，目的地判定为 `allow`。真实 Ollama 模型 `qwen3:1.7b` 返回 21。 | 授权端记录 2 次模型 POST。其中 1 次包含合成源码，接收端自行计算的摘要与保存版本一致。 |
| DSH → 未授权模型 | 两项权限均存在，目的地判定为 `deny`。DSH 收到 HTTP 403。另用带 `source_class=public` 的请求复验，仍是 403。 | 接收端健康检查通过，推理 POST 为 0。 |
| 工作区 → 授权编译端 | `allow`，记录请求标识、保存版本和实际工作流绑定。 | 独立 Linux 运行器读取同一版本，编译 1 次，产物输出 21、0、200。[构建回执](https://github.com/fREEDtICE/software-delivery-workbench/actions/runs/44664711007/artifacts/14596443369) |

[网关、接收端与 CI 取证 JSON](q0-model-gateway-evidence.json)

CI 从真实 HTTP 入口运行同一网关代码，使用受控接收器。正常运行 7 项检查全部通过。故意放宽测试策略时，两个原本应拒绝的行为检查连同零接收检查失败，受控未授权接收器实际收到 1 次合成源码 POST。真实模型运行是上表另列的实测。[正常 CI](https://github.com/fREEDtICE/software-delivery-workbench/actions/runs/44664642190) · [故障注入 CI](https://github.com/fREEDtICE/software-delivery-workbench/actions/runs/44664710886)

## 尚未达到 Q1 准入

| Q0 门禁 | 已有证据 | 进入 Q1 前需要补齐 |
| --- | --- | --- |
| RT-01/03 执行位置与结果路径 | 固定版 DSH、保存摘要、独立托管运行器和产物结果均已取证。 | 在试点准备采用的 Multica 后端、工作区服务和 Runner 上复跑。验证保存确认、集群读取、产物直存和权限绑定。 |
| RT-02 临时与持久边界 | Agent 使用试验目录，凭据环境清理，本轮实测 Runtime 无法直连 Ollama。 | 在目标 Runtime 环境核验挂载、长期凭据、云存储直连和其他出口。检查保存后销毁实例能恢复，未保存内容的丢失可明确识别。 |
| RT-04/06 恢复与故障 | 丢回执后找回原任务、一次实际编译、同一 DSH 会话恢复已有证据。 | 增加“构建仍在进行时回收 Runtime”以及“任务暂不可查询”的真实验证。后者应进入待核对、显示责任人、恢复后关联原任务。补齐取消竞态、Runner 失联与日志重复。 |
| RT-05 策略与隔离 | 源码接收方授权已覆盖外部编译与实际模型请求。 | 接入目标部署的身份与策略存储，从试点会使用的 UI、后台、Agent 和 Runner 验证同一规则。核验撤权、跨团队与旧租约拦截。 |
| RT-07 版本契约 | 固定单 Agent 配置能运行。官方桥接仍存在固定 DSH 子 Agent 接口不兼容。 | 锁定试点运行配置，用最小参考 App 验证公开契约。证明确认兼容的替换配置可使用，无法恢复的旧 Session 和回执有明确阻塞。若首版需要子 Agent，先修桥接并复验。 |

优先落实一套试点形态的端到端部署和执行环境，再补齐故障、身份变更与版本验收。质量负责人独立复核实际环境证据及故意回归结果。当前私有仓库的套餐未开放分支保护，进入 Q1 前还需落实约定的合并门禁。产品与测试可并行确认团队、已有仓库、指定编译环境、验收负责人和初版可执行用例。[Helios Q0 取证要求](internal-doc-redacted · [Q1 验收表](internal-doc-redacted

## 复现

```sh
python3 tools/verify_q0_model_gateway.py --evidence evidence/gateway-normal
python3 tools/verify_q0_model_gateway.py --evidence evidence/gateway-fault --overbroad-test-policy
```

第二条预期退出 1。真实验证沿用[固定版运行环境](q0-live-probe.md)。重新编译 `tools/q0_agent/probe.go` 后，在准备好的专用试验目录运行 `python3 tools/q0_agent/run_model_gateway.py <probe二进制> <试验目录> <新证据目录>`。本机 Ollama 需具有 `qwen3:1.7b` 和 `qwen3.5:0.8b`。该脚本临时将试验 DSH 的模型入口指向网关，结束后恢复配置。请求只使用合成源码。

这个接入覆盖隔离网关中的真实模型转发和独立编译。生产网关部署、目标集群和其他外发入口仍各自需要验证。
