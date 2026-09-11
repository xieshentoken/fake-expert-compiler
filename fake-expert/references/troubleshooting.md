# fake-expert 错误与恢复

统一入口将错误保持为稳定 code，并给出 next action。恢复应创建新 job 或使用
既有专项 validator；不要直接编辑 hash、状态、Gold 或 Review 文件。

| code | 含义 | next action |
|---|---|---|
| `workspace_too_broad` | workspace 是 `/`、用户 home 或项目根，扫描范围不明确 | 传入一个明确的私有子目录 |
| `workspace_symlink_forbidden` | workspace 或其扫描树含 symlink | 移除/隔离链接后重新指定私有目录 |
| `status_workspace_requires_all` | `status --workspace` 没有明确 `--all` | 明确加 `--all` |
| `job_schema_invalid` | job 顶层或嵌套字段不是闭合契约 | 保留原文件，重新运行 `plan` |
| `job_schema_upgrade_required` | job 属于旧的 v0.1 闭合契约 | 保留旧 job 审计，不原地改写；使用当前 CLI 重新 `plan` |
| `job_hash_mismatch` | canonical job 内容被改动 | 不手工修 hash；从原输入重新 plan |
| `job_drift` | source、workpack、Gold revision、工具 hash 或路径已变化 | 确认输入，创建新 job |
| `required_reference_missing` | 当前 profile/stage/Gold 路由要求的参考文档不存在 | 修复 Skill 文档树后重新运行 `doctor` 和 `plan` |
| `required_reference_hash_mismatch` | job 绑定的必读文档内容已变化 | 保留旧 job，阅读新文档并重新 `plan`；禁止手工修 hash |
| `document_contract_invalid` | SKILL 路由、关键错误码或版本矩阵与当前代码不一致 | 修正文档契约并通过 `doctor` 后再新建 job |
| `source_missing` | source 路径不存在或不是普通文件 | 提供已有 PDF；不自动获取 |
| `source_hash_mismatch` | PDF 内容与计划指纹不同 | 重新 plan，保留旧 job 作为审计记录 |
| `source_profile_mismatch` | native profile 的 source 不是可解析未加密 PDF，或存在 native-text 不足的页面 | 选择显式 scan profile 并重新 plan；matching workpack 不能绕过此门禁 |
| `pypdf_pin_mismatch` | canonical native-text runtime 不是 `6.10.0` | 在已有环境中使用固定 pin；不自动安装 |
| `renderer_missing` | `pdftoppm` 不可用 | 指定已有 Poppler 可执行文件 |
| `optional_backend_unavailable` | 可选适配器不可导入 | 只在显式选择且另有 qualification 时使用；不下载 |
| `backend_required_for_profile` | scan/visual profile 没有显式 backend | 重新 plan 并传入适用于该 profile 的 `--backend`；不要把 doctor 推荐当成选择 |
| `backend_invalid_for_profile` | backend 不在所选 profile 的允许集合内 | 保留旧 job，使用正确 backend 创建新 job；不自动 fallback |
| `runtime_qualification_validator_required` | 已绑定 backend 没有统一 shortcut validator | 使用该 backend 的既有专项 validator 并重新绑定结果；保持当前 job 暂停 |
| `runtime_qualification_unverified` | hash-bound runtime receipt 未通过既有专项 validator | 修复或重新取得外部 qualification receipt；不要启动模型重试 |
| `semantic_authoring_required` | semantic proposal 不完整或不存在 | 通过既有 workpack 流程补齐 proposal |
| `workpack_format_unrecognized` | 输入不是可识别 semantic workpack | 检查目录边界和既有 schema |
| `runtime_qualification_required` | 扫描路由缺少匹配的本地 runtime receipt | 由有权限的 operator 完成既有 qualification；不自动启动模型 |
| `structure_review_required` | 扫描候选缺少外部结构 Review | 使用现有 scan structure workbench |
| `visual_review_required` | 视觉候选缺少人工视觉 Review | 使用现有 visual/Gold workbench |
| `gold_review_required` | job 请求 Gold 但没有外部 revision | 由独立 reviewer 完成既有 Gold 流程 |
| `independent_review_required` | 单 reviewer 或 candidate 不能建立独立门禁 | 提供外部 fragment/attestation 并走原 validator |
| `competency_required` | ready 目标缺少 competency receipt | 运行既有 competency gate |
| `seal_required` | ready/executable 目标缺少 seal | 走既有 seal/certification gate |
| `workbench_schema_unrecognized` | 目录不是 semantic、visual、scan-structure 或 scan-Gold workbench | 传入正确的现有 workbench 根目录 |
| `workbench_manifest_hash_mismatch` | 工作台 manifest 被改动 | 保留原目录，重新生成新的工作台 |
| `submission_item_hash_mismatch` | submission 的 item/input 指纹不匹配 | 重新从该工作台导出 submission |
| `submission_item_coverage_invalid` | submission 缺项、重复项或未知项 | 补齐人工表单后重新导出 |
| `submission_hidden_field` | submission 含 prediction、model 或 hidden answer 字段 | 删除该 submission 并从干净工作台重导；不要隐藏字段 |
| `semantic_submission_declarations_invalid` | semantic submission 的声明缺失、增加或为 false | 由 reviewer 在既有 workbench 中完整确认后重新导出 |
| `visual_submission_declarations_invalid` | visual submission 的声明缺失、增加或为 false | 由 reviewer 在既有 workbench 中完整确认后重新导出 |
| `visual_submission_hidden_field` | visual submission 含 prediction/model/split 隐藏字段 | 丢弃该文件并从 answer-free workbench 重新导出 |
| `draft_output_invalid` | output 目录存在但未通过 direct Reference 权威 validator | 保留原目录，使用新 output 路径重新 plan；不能仅凭目录存在宣称完成 |
| `gold_update_requires_existing_base` | 初始 unified job 请求 Gold update | 使用既有 validated base 的专项 bind-gold/update 路由；不在初始 job 中降级为 create/reuse |

## 这些情况不算成功

- `draft`、`candidate`、`transport verified` 或 candidate benchmark 不能称为 Gold、accuracy、ready 或 executable。
- schema 通过、计划存在或 dry-run 输出不能替代 source/render/worker/runtime/Review 的实际绑定。
- doctor 的推荐不等于 backend 选择；统一入口不会 fallback。
- `status --all` 只接受显式 workspace，不接受 home、项目根或宽目录。
