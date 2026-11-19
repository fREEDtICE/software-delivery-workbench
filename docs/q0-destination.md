# Q0 源码目的地授权

源码读取权和模型调用权分别授权。发送已保存的源码前，平台还核对实际源码摘要和接收方绑定。接收方由平台选定，Agent 工具只能请求预设目的地。授权记录先落盘，执行器再发送内容。

## 同一份受控源码的两条路径

源码摘要为 `09d00553415582ebe4dd69250fe4b6fcb5d7cdad2f78a5670f54bbb738913060`。

| 路径 | 策略记录 | 接收端记录 |
| --- | --- | --- |
| 指定编译端 | `allow`，匹配到指定仓库、编译工作流和主分支 | 托管 Linux 执行器收到并核对同一摘要，完成一次构建。产物验证输出 21、0、200。[真实构建](https://github.com/fREEDtICE/software-delivery-workbench/actions/runs/44498612648) · [编译回执](https://github.com/fREEDtICE/software-delivery-workbench/actions/runs/44498612648/artifacts/14516191748) |
| 未授权模型 | 固定版 Multica + DSH Agent 成功读取源码。已有 `source.read` 和 `model.invoke`，目的地检查仍为 `deny`，原因为 `source_destination_not_authorized`。 | 测试接收端记录到来自工具进程的健康检查，推理 POST 为 0。真实 Agent 的文字总结误报了读取结果，验收采用工具和接收端记录。 |

[原始策略、接收记录与 CI 证据](q0-destination-evidence.json)

## CI 自检

正常场景使用两个相互独立的本机 HTTP 接收端。编译接收端收到 1 次 POST，独立计算的源码摘要与保存版本一致。未授权模型接收端收到 0 次 POST。[正常 CI](https://github.com/fREEDtICE/software-delivery-workbench/actions/runs/44498445430) · [原始证据](https://github.com/fREEDtICE/software-delivery-workbench/actions/runs/44498445430/artifacts/14515364539)

故意放宽测试策略后，受控模型接收端收到 1 次合成源码 POST。“发送前拒绝”和“零次接收”两项同时失败，CI 以失败结束。故障注入只使用本机接收器和合成源码。[故障 CI](https://github.com/fREEDtICE/software-delivery-workbench/actions/runs/44498611848) · [原始证据](https://github.com/fREEDtICE/software-delivery-workbench/actions/runs/44498611848/artifacts/14515679868)

## 复现

```sh
python3 tools/verify_q0_destination.py --evidence evidence/destination-normal
python3 tools/verify_q0_destination.py --evidence evidence/destination-regression --overbroad-test-policy
```

第二条预期退出码为 1。对真实编译端，沿用[独立构建流程](q0-live-probe.md)的 `save`、`submit`、`reconcile`，在状态目录检查 `destination-policy.jsonl` 和 `compiled/receipt.json`。

对真实 Agent，在固定版 Multica 的 `server/` 目录重新编译 `tools/q0_agent/probe.go`。然后运行 `python3 tools/q0_agent/run_destination_probe.py <probe二进制> <已准备的Q0试验目录> <合成源码文件> <新证据目录>`。试验目录需要上一批准备好的单 Agent DSH 配置及本机 Ollama 模型。该脚本启动专用回环地址接收器，核对读取、发送前拒绝和零推理 POST。

验证范围为受控源码和经过 Q0 发送入口的工具调用。生产模型网关、后端进程和其他发送路径需要逐一接入同样的授权检查。
