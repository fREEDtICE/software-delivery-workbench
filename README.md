# 软件交付工作台

Q0 第一批交付验证一个基本承诺：受控构建即使成功退出，验收仍要确认实际使用的代码版本和产物行为。

上游提交锁定于 [upstreams.lock.json](upstreams.lock.json)。CI 逐一核对提交仍可从 GitHub 解析。受控场景用 Python 固定样本和独立构建进程，GitHub Actions 固定为 Ubuntu 24.04 和 Python 3.12.12。

## 运行验证

在仓库根目录执行。建议本地使用与 CI 相同的 Python 版本。验证只使用标准库，核对上游提交另外需要已登录的 GitHub CLI。

```sh
python3 tools/verify_upstreams.py
python3 tools/verify_q0.py
python3 tools/verify_q0.py --scenario wrong-source --report evidence/q0-wrong-source.json
```

| 场景 | 验收 |
| --- | --- |
| `normal` | 构建当前版本，构建回执和实际产物摘要一致，三个独立用例得到 21、0、200。退出码为 0。 |
| `wrong-source` | 执行器成功编译旧版本，产物仍可运行。源码版本与行为均不满足当前验收。退出码为 1。 |
| 环境阻塞 | 编译器异常、产物缺失等情况报告 `BLOCKED` 并退出 2，可与业务断言失败区分。 |

向 `main` 推送或发起 PR 时自动执行正常场景。可在 Actions 的 **Q0 baseline** 里手动选择 `wrong-source` 复现红灯。即使断言失败，Actions 也保留 JSON 证据，其中包含代码提交、实际源码和产物摘要、预期与实际，以及运行编号。

## Q0 真实运行时验证

[独立构建与回执恢复](docs/q0-live-probe.md) 记录了固定版 Multica 后端、DSH 和真实模型的保存结果，以及独立 Linux 执行端的委派、取证和恢复。[源码目的地授权](docs/q0-destination.md)记录了授权编译端收到正确版本、未授权模型发送前拒绝及零接收。

[真实模型网关复验与 Q1 准入](docs/q0-model-gateway.md)记录了 DSH 实际推理请求的授权、独立接收端证据，以及尚待关闭的 Q0 门禁。

[隔离试点部署与验收](docs/q0-pilot.md)提供了 Linux 试点的复跑入口和部署、接收端证据。
