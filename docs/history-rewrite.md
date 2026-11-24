# 2026-11-23 历史重写与合并门禁

本仓库于 2026-11-23 从私有仓库转为公开仓库。转为公开前，三个验收文档中的内部文档链接（`Helios Q0 取证要求`、`运行时硬规则`、`Q1 真实团队验收`）被替换为不含路径的文字引用；这些链接在同一批历史提交中仍然存在，因此对提交历史做了一次重写。

## 重写范围

重写使用 `git filter-branch` 的 tree filter，只替换 `docs/q0-pilot-lookup.md`、`docs/q0-model-gateway.md`、`docs/q0-pilot-recovery.md` 中的内部文档 URL，其余文件内容不变。从最早涉及内部链接的提交起，其后所有提交的 SHA 均发生变化；树内容除上述替换外保持一致。

## 被引用提交的新旧对照

| 原 SHA | 新 SHA | 引用位置 |
|-|-|-|
| `c456208` | `92b2d7b` | 最早涉及内部链接的提交 |
| `27eb520` | `01a1277` | [隔离试点部署与验收](q0-pilot.md) |
| `4ab258e` | `9fa600a` | [恢复与撤权复验](q0-pilot-recovery.md) |
| `010d2c2` | `ea7973b` | [查询暂不可用复验](q0-pilot-lookup.md) |
| `7ff7510` | `6899793` | 查询故障证据归档 |

## 对证据的影响

`docs/*-evidence.json` 中的 `source_revision` 保留重写前的原始 SHA，不随重写更新，因为它们是当次运行时实际提交的记录。这些旧 SHA 已不在任何分支上，但对应 GitHub Actions 运行记录（按 run ID 访问）仍然有效，可继续作为证据核对入口。按旧 SHA 直接访问提交页面在 GitHub 回收悬空对象之前仍可能成功。

## 合并门禁

公开仓库在 Free 套餐下可以强制分支规则。`main` 由仓库级 ruleset `main-merge-gate` 保护：

- 必须通过 PR 合入，禁止直接推送、强制推送与分支删除；
- 必须通过三项必需检查：`Verify Q0 baseline`、`Verify source destination authorization`、`Verify gateway request authorization and receiver evidence`；
- 要求线性历史，分支在通过检查前必须保持最新；
- bypass 列表为空，管理员同样不能绕过。

当前 `required_approving_review_count` 为 0，因为仓库只有一名维护者且 GitHub 不允许自我审批。Q1 准入前需指定独立审批人并将该值改为 1。
