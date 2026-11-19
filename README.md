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

## Q0 剩余验证

接下来用固定的 Multica 和 DSH 跑通真实模型、工作区服务及独立编译集群，验证集群接单后回执丢失时的恢复、数据目的地授权，以及沙箱释放后的接续。第一批受控用例提供可重复执行的验证入口，完整 Q0 以真实入口和独立执行环境的证据为准。
