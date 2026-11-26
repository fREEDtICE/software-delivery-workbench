# AWS EC2 与阿里云 ECS Agent 云运行时

工作区管理员选择云厂商并完成账号授权、地域与网络配置后，框架自动开通 Linux 实例、在首启时完成 Agent 配置并通过正式守护进程入口自注册，随后可设为工作区默认运行时。已有 Agent 沿用原绑定，新建 Agent 才走云端；云端不可用时显式报错，绝不静默回退到本地执行。

## 交付边界与上游关系

本仓库（软件交付工作台）交付可审查的控制面、云厂商适配器、Linux 首启脚本和端到端验收，全部仅依赖 Python 标准库，无需 Docker。

- 正式 Multica 服务端属于上游仓库 `multica-ai/multica`（提交锁定见 `upstreams.lock.json`），当前工作区对该仓库没有写权限。
- 云运行时复用 Q0 已验证的正式守护进程契约 `/api/daemon/register`（与 `pilot/run.py` 中真实后端注册同一入口）。
- 控制面的开通激活记录接口 `POST /api/cloud-runtimes/activations` 是本交付假设的工作区管理契约，目前由本地假工作区实现；接入真实上游服务端需要在上游仓库增加该路由或确认等价能力，维护方为 Multica 后端负责人。这是本工单唯一需要上游改动的点，其余控制面逻辑不依赖上游变更。

信任边界延续 Q0：云实例只加入工作区与模型网关可达的网络，实例上不放置长期云管理凭据，仅引用 IAM 实例 Profile / RAM 角色并持有可撤销、最长 900 秒的任务租约。

## 模块

| 模块 | 职责 |
| --- | --- |
| `cloud_runtime/providers.py` | 统一厂商接口；AWS EC2 走 SigV4 query API（含 STS 身份核验），阿里云 ECS 走 RPC HMAC-SHA1；签名、编码、响应解析在 fake/live 下是同一条代码路径 |
| `cloud_runtime/bootstrap.py` | 渲染 cloud-init 兼容的 Linux user-data，写运行时配置与短期租约并启动 Agent 自注册；只含实例角色引用，不含长期密钥 |
| `cloud_runtime/controller.py` | 阶段状态机、原子持久化、幂等开通、就绪/注册等待、超时重试、取消与停用回收、默认运行时与调度决策、孤儿资源审计 |
| `cloud_runtime/fakes.py` | 进程内假 EC2、假 ECS 与假工作区；校验真实请求签名，模拟 pending→running、首启自注册、故障注入与 ClientToken 去重 |
| `cloud_runtime/cli.py` | 管理员入口：`configure / provision / retry / status / set-default / smoke-task / teardown / audit / dispatch-check` |
| `tools/verify_cloud_runtime.py` | 背靠背端到端验收，驱动真实 CLI 子进程，输出 JSON 证据 |

## 本地复跑（模拟云，零外部依赖）

仓库根目录执行，不需要云账号、不需要 Docker：

```sh
python3 tools/verify_cloud_runtime.py
# 单厂商
python3 tools/verify_cloud_runtime.py --provider aws
python3 tools/verify_cloud_runtime.py --provider aliyun
```

证据默认写入 `evidence/cloud-runtime/e2e.json`（该目录被 git 忽略，CI 作为构件上传）。本仓库随附一份本地复跑快照 `docs/cloud-runtime-e2e-evidence.json`，其 `mode` 字段为 `fake-cloud`，明确属于模拟验证。

### 验收覆盖（两家同一口径，共 65 项检查）

- **开通与默认运行时**：授权核验 → 网络校验 → 激活 → 开机 → 就绪 → 自注册 → 可设默认；新建 Agent 调度到云端并携带实例身份，已有绑定保持原运行时；合成任务的发起端可退出，新进程凭任务编号续等，回执包含云实例 ID、地域、镜像与结果。
- **故障与回收**：授权不足返回 `authorization_failed` 且不开机、不计费；开通超时保留实例 ID 并可重试；取消与停用回收实例与工作区注册；审计无孤儿资源；同一 ClientToken 重复开机只产生一台实例。
- **隔离约束**：user-data 回读校验，无长期云管理密钥，仅实例角色引用与短期租约；未知/过期租约在正式注册入口被拒；伪造签名在线路层被拒；云端不可用时返回 `runtime_unavailable`，无本地回退。
- **签名锚点**：AWS SigV4 用 botocore 自带的官方测试套件向量 `get-vanilla` 与 `get-vanilla-with-session-token` 校验完整 Authorization 头。

## 真实云复跑（当前环境受阻，列出解锁条件）

本运行环境没有 AWS/阿里云 CLI，也没有任何云凭据，因此未执行真实云实测。代码已按两家真实线上协议实现，解锁后用同一 CLI、切换 `--mode live` 即可复跑，命令形状与模拟版完全一致，仅端点和凭据来源不同。

### AWS

- 凭据环境变量：`HELIOS_AWS_AK` / `HELIOS_AWS_SK`（临时凭据再加会话令牌），工作区管理令牌 `HELIOS_ADMIN_TOKEN`。
- IAM 最小权限：`sts:GetCallerIdentity`、`ec2:DescribeSubnets`、`ec2:DescribeSecurityGroups`、`ec2:RunInstances`、`ec2:DescribeInstances`、`ec2:DescribeInstanceAttribute`、`ec2:GetConsoleOutput`、`ec2:TerminateInstances`；实例 Profile 仅授予 Agent 运行所需的最小读取权限，不授予上述管理权限。
- 地域与配额：先确认目标地域（如 `us-east-1`）该账号有 vCPU/EC2 配额、可用子网与安全组，以及一台可访问工作区与模型网关的 Linux 镜像。
- 责任方：工作区云管理员提供测试账号、Profile、子网、安全组与额度；Multica 后端负责人落地激活接口。

### 阿里云 ECS

- 凭据环境变量：`HELIOS_ALIYUN_AK` / `HELIOS_ALIYUN_SK`，工作区管理令牌 `HELIOS_ADMIN_TOKEN`。
- RAM 最小权限：`ecs:DescribeRegions`、`vpc:DescribeVSwitches`、`ecs:DescribeSecurityGroups`、`ecs:RunInstances`、`ecs:DescribeInstances`、`ecs:DescribeUserData`、`ecs:DeleteInstance`；实例绑定的 RAM 角色仅含 Agent 运行权限。
- 地域与配额：确认地域（如 `cn-hangzhou`）有 ECS 实例配额、可用交换机与安全组、可连通工作区/网关的 Linux 镜像。
- 责任方同上。

### 真实云实测的通过判据

1. 从 `configure` 到 `provision` 全程不登录主机、不手工装依赖、不逐个注册执行器，`status` 展示阶段与可操作失败原因。
2. `smoke-task` 的回执实例 ID 与云控制台实例一致，控制台日志含注册标记，发起端关闭后任务仍能完成并回传。
3. 注入授权不足与开机超时后重试，账单相关资源不重复；`teardown` 后云控制台无残留实例，`audit` 无孤儿。
4. 实例元数据/系统中查无长期云管理密钥，未授权目的地拦截与已保存结果恢复沿用 Q0 能力。

真实云证据应另存为 `mode=live-cloud` 的 JSON 与控制台截图，不得与模拟证据混放。
