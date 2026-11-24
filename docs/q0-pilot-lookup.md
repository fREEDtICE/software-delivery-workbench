# Q0 编译任务暂不可查

编译端已受理原构建，平台丢失提交回执，随后编译端查询返回 HTTP 503。试点操作台显示「待核对」，保留负责人和「重新查询」入口。故障期间重复查询只读取原请求。查询恢复后，操作台关联同一构建并取得最终回执。

| 阶段 | 操作台状态 | 编译端记录 |
| --- | --- | --- |
| 已接单，回执丢失 | 按持久化保存的原请求标识自动查询。HTTP 503 后进入待核对。 | 已登记原构建一次。 |
| 查询仍不可用 | 负责人可见。「重新查询」两次均保留待核对，原构建字段显示待确认。 | 连同自动查询，共返回三次 503。整个观察窗口只有最初一次构建提交。 |
| 查询恢复 | 同一入口关联原构建号，并可打开「查看原构建」。 | 查询恢复一次，仍使用已登记的原构建。 |
| 原构建完成 | 页面显示已完成，回执关联同一构建。Multica 任务为 `completed`。 | 实际编译一次，产物直存一次，重复提交为零。 |

用例沿用 Multica、DSH、外置编译服务和存储组成的隔离试点，使用合成源码和试点账号。页面由使用只读编译权限的试点操作台提供。故障开关只暂停该请求的查询，受理记录与构建执行仍由编译端保存。

在仓库根目录执行 `python3 pilot/run.py --build --lookup-outage` 可复跑。[Q0 pilot deployment](https://github.com/fREEDtICE/software-delivery-workbench/actions/workflows/q0-pilot.yml) 也使用此命令。证据包中的 `deployment.json` 保留 `lookup_outage` 状态流转与页面检查、`receiver_events.compiler` 编译端事件，以及 `receive_storage_sha256` 落盘摘要。HTML 文件是隔离环境实际返回的页面快照。

## 验收证据

本机 Linux 和 GitHub Linux CI 都对提交 `010d2c2` 跑通。CI 的试点负责人是 `q0-pilot-e8aae15d`，原请求标识为 `09d7afcc0bd504cd64c10a393f80ad9b`，查回的构建号为 `461df5b8-b5a9-4d15-b7da-4b695749bd58`。三个产物用例得到 `21、0、200`，产物摘要 `1e363bdd…` 与本机和实际落盘文件一致。

[待核对页面快照](q0-pilot-lookup-pending.html) · [关联原构建页面快照](q0-pilot-lookup-linked.html) · [状态与编译端原始记录](q0-pilot-lookup-ci-evidence.json) · [通过的 CI](https://github.com/fREEDtICE/software-delivery-workbench/actions/runs/45127946521) · [完整证据包](https://github.com/fREEDtICE/software-delivery-workbench/actions/runs/45127946521/artifacts/14796807625)

## Q1 还缺什么

本轮关闭「暂时查不到任务」的试点用例。正式启动仍需关闭已有准入项。

- **选定真实团队与验收对象。** 确定团队、已有仓库、指定编译环境、测试工单、初版 Contract、独立验收人和比较基线。
- **在目标部署补齐运行时硬规则。** 用真实身份验证各入口授权与跨团队隔离，在同一部署验证原 DSH 会话接续。Runner 失联、取消竞态和版本兼容继续按既有验收表取证。
- **落实受保护的合并门禁。** 当前私有仓库的 CI 可运行并留证，分支规则查询仍返回 HTTP 403，需落实约定的合并门禁。

内部参考文档（未随仓库公开）：Q0 取证要求、运行时硬规则、Q1 真实团队验收。
