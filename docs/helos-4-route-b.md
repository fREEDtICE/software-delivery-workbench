# HELIOS-4 路线 B（角色授权）可复跑说明

适用仓库：`fREEDtICE/multica` 分支 `feat/cloud-runtime-aws-aliyun`（在其首个提交之上续作）。
本文记录云运行时从「部署环境变量直连」升级为「管理员在产品内连接云账号、产品只存角色 ARN、全部操作走 STS 短期凭据」的验证方式。

## 1. 产品流程与后端契约

入口：Team → Resources → Runtimes → 添加 → 云运行时（Web 由 `NEXT_PUBLIC_ENABLE_CLOUD_RUNTIME=true` 开启）。

1. 选厂商（AWS EC2 / 阿里云 ECS）。
2. 连接云账号：页面给出可复制的信任策略、最小权限模板、控制台指引和部署身份；云账号管理员在控制台授权角色后，回填角色 ARN，点「验证授权」。
3. 验证通过后填写名称、地域、子网、规格、磁盘，确认计费归属并开通。
4. 节点经历 `provisioning（开通中）→ connecting（接入中）→ ready（已就绪）`，失败展示错误码与可操作提示，可重试（同一 ClientToken，不产生重复实例）或取消（终止半开实例）。
5. 授权失效导致回收失败时进入 `cleanup_required`，在「待清理资源」中恢复授权后重试，或去云控制台手动清理。

内置驱动（`MULTICA_CLOUD_BUILTIN=true` 且 `MULTICA_CLOUD_URL` 为空）新增的 REST 契约，均经 `/api/cloud-runtime` 代理到 `/api/v1/*`：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/accounts/trust?provider=` | 信任信息、最小权限模板、部署身份是否就绪 |
| POST | `/api/v1/accounts/verify` | AssumeRole 换取短期凭据并回显核实身份，只持久化角色 ARN |
| GET | `/api/v1/accounts` | 已连接账号 |
| DELETE | `/api/v1/accounts` | 断开账号（仍有运行时时拒绝） |
| POST | `/api/v1/nodes` | 基于已验证账号开通节点（无账号返回 `no_verified_account`） |
| POST | `/api/v1/nodes/cancel` | 取消在途开通并回收 |
| GET/POST | `/api/v1/cleanup`、`/api/v1/cleanup/retry` | 待清理资源与恢复后重试 |
| GET/PUT | `/api/v1/default-runtime` | 工作区默认云运行时 |

部署身份缺失时，验证与开通统一返回 `409 deployment_identity_missing`，前端禁用入口并提示联系部署管理员。

## 2. 本地复跑（假云，无需真实云账号）

假云通过真实 SigV4 / 阿里云 HMAC-SHA1 签名校验请求：部署身份密钥无 session token，AssumeRole 返回的短期密钥必须与 session token 同时出现，否则签名校验直接拒绝——因此测试能证明开机路径确实运行在角色短期凭据上。

### 后端

```bash
cd multica/server
go build ./...
go vet ./internal/cloudruntime/... ./internal/handler/... ./cmd/server/...
go test ./internal/cloudruntime/... -count=1
go test ./internal/handler/ -run 'TestBuiltinCloudRuntime' -count=1
```

关键测试：

- `provision/route_b_test.go`：空白工作区门禁、部署身份缺失、验证失败后重试去重、两家厂商短期凭据开机与磁盘透传、默认运行时持久化与重启恢复、取消回收、授权失效后的待清理队列与恢复、账号断开保护。
- `handler/cloud_runtime_builtin_test.go::TestBuiltinCloudRuntimeRouteBFlow`：经真实 HTTP handler 跑通两家厂商的连接→验证→开通→设默认→回收。

### 前端

```bash
cd multica
pnpm install --no-frozen-lockfile   # 仓库锁定文件与本机 pnpm 的 patchedDependencies 哈希格式不同时使用
pnpm --filter @multica/core typecheck
pnpm --filter @multica/views typecheck
pnpm --filter @multica/views exec vitest run runtimes locales
pnpm --filter @multica/views exec eslint \
  runtimes/components/cloud-runtime-dialog.tsx \
  runtimes/components/cloud-account-connect.tsx \
  runtimes/components/cloud-runtime-configure.tsx
```

组件测试 `cloud-account-connect.test.tsx`、`cloud-runtime-dialog.test.tsx` 覆盖信任信息复制、验证成功/失败、身份缺失禁用、完整向导、失败重试与待清理队列。8 个语言包的 `cloud_runtime` 键由 `locales/parity.test.ts` 强制对齐。

## 3. 真实云验证缺口（本环境无法完成）

本机没有 AWS / 阿里云凭据、账号配额和可访问的云控制台，所有验证均为进程内假云。真实云验收还需要：

1. 自托管部署配置具备 `sts:AssumeRole`（AWS）/ `sts:AssumeRole`（阿里云 RAM）权限的部署身份密钥。
2. 云账号管理员按页面模板配置信任策略与最小权限，并提供真实角色 ARN。
3. 两家各用一个真实账号从空白工作区跑通：连接→验证→开通→实例身份与日志核对→派发合成任务并在关闭发起端后取回结果→停用回收→重复操作确认无残留计费资源。
4. 真实地域、配额、镜像 ID、子网与安全组的联调；假云不校验这些网络参数的真实性。

## 4. 安全边界

- 产品数据库（JSON 状态文件）只保存角色 ARN、验证结果和节点元数据，不保存任何长期 AK/SK；AssumeRole 得到的短期会话仅存内存并在到期前自动刷新。
- 实例 user-data 仍只包含实例角色引用和 ≤900 秒的短期任务租约，测试断言其中不含部署密钥、短期会话密钥或 session token。
- 托管 Fleet（配置了 `MULTICA_CLOUD_URL`）路径不变，新路由仅内置驱动实现。
