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
| `automatic_intake_option_requires_explicit_profile` | 无 profile 的自动 plan 收到了只适用于旧 job 的参数 | 对 Gold、runtime receipt、backend 或自定义 job 路径明确传 profile；不静默忽略 |
| `automatic_intake_options_require_automatic_route` | 旧显式 profile plan 收到了新自动 intake 的 OCR config/capability/renderer/阈值选择 | 使用无 profile 的自动 plan 或 intake plan；不向闭合旧 v0.2 job 塞入新字段 |
| `intake_binding_drift_replan` | 自动 workflow 的 source、工具、文档、schema、renderer、配置、模型或资格已漂移 | 保留旧 workflow/候选，重新 plan；配置修复须在执行前明确选择 configure_environment 重绑 |
| `ocr_environment_unavailable` | 所需 OCR 环境不可运行 | 按 checks 区分 interpreter/deps/model/renderer/qualification missing、stale 或 unusable；选择配置修复或可行降级 |
| `ocr_execution_authorization_required` | 已配置环境可用，但尚无当前 source/config/backend 的执行授权 | 仅在用户授权该模型 OCR 后传 authorize-ocr 和 exclusive-heavy-model；inspect/status/dry-run 不执行 |
| `ocr_checkpoint_incomplete` | 已保存有界批次，整源还有 OCR 页待处理 | 有进展且仅预算耗尽时按 next_action resume 下一连续物理 chunk；完整 source 页账本始终保留 |
| `ocr_worker_repair_required` | worker 返回实际错误或本批零进展 | 保留候选和 checkpoint，检查错误码、修复后显式 resume；禁止无限自动重试 |
| `intake_workflow_schema_invalid` | 新 workflow 的闭合顶层或嵌套 shape 非法 | 保留原文件；不要手改 hash、scope、choice 或授权来绕过校验 |
| `scan_execution_failed` | 扫描执行/权威完整性检查失败，或运行环境出现异常 | 保留已成功 chunk；根据 failure_detail 修复后显式恢复，不能把失败称为结构 Review 完成 |
| `source_contains_no_native_text` | 全部页均确认空白，没有可提取的正文 | 选择包含可用 native 正文的原物理页范围；不为白纸启用 OCR |
| `blank_page_proof_invalid` / `blank_page_proof_mismatch` | 空页证明缺失、错页、重复、漂移，或被附加到有正文的页 | 保留旧 workpack/IR，检查原件、renderer 和页号后重新抽取；不手工修 hash |
| `non_contiguous_source_scope_requires_separate_package` | 同源 `extend` 跳过了原物理页，包括已确认空页 | 重新提取包含空页的连续范围，或维持分包；失败目标不会留下可验证的旧包 |
| `pypdf_pin_mismatch` | canonical native-text runtime 不是 `6.10.0` | 在已有环境中使用固定 pin；不自动安装 |
| `renderer_missing` | `pdftoppm` 不可用 | 指定已有 Poppler 可执行文件 |
| `optional_backend_unavailable` | 可选适配器不可导入 | 只在显式选择且另有 qualification 时使用；不下载 |
| `backend_required_for_profile` | scan/visual profile 没有显式 backend | 重新 plan 并传入适用于该 profile 的 `--backend`；不要把 doctor 推荐当成选择 |
| `backend_invalid_for_profile` | backend 不在所选 profile 的允许集合内 | 保留旧 job，使用正确 backend 创建新 job；不自动 fallback |
| `runtime_qualification_validator_required` | 已绑定 backend 没有统一 shortcut validator | 使用该 backend 的既有专项 validator 并重新绑定结果；保持当前 job 暂停 |
| `runtime_qualification_unverified` | hash-bound runtime receipt 未通过既有专项 validator | 修复或重新取得外部 qualification receipt；不要启动模型重试 |
| `semantic_authoring_required` | semantic proposal 不完整或不存在 | 新 workflow 使用返回的 `children/pages-START-END/workpack`；旧显式 v0.2 未绑定 workpack 时在 `<workspace>/extraction/workpack`。用返回的 workpack 路径运行既有 authoring/ingest，补齐 drafts 后恢复；不自动调用模型 |
| `extraction_failed` | 统一入口的自动抽取（IR / workpack / authoring packets）失败，例如标题定位需要人工处理或 `extraction/` 非空 | 按附带的原错误码处理（如 `heading_resolution_required` 走既有 heading override）；保留旧目录，换新 workspace 重新 plan |
| `extraction_workpack_mismatch` | `<workspace>/extraction/workpack` 绑定的 source SHA-256 与 job 不一致 | 不要覆盖；为该 source 新建 workspace 并重新 plan |
| `numeric_token_untraced` | draft 的数字不在文本证据中，且未绑定图对象（现为 error） | 在 evidence 中引用含该数字的原文，或用 `figure_object_ids` 绑定读数所在的 figure/table |
| `numeric_token_figure_derived` | 数字来自已绑定的图对象（warning） | 可接受；打包后记录 `value_basis=figure-unreviewed`，查询结果带 `figure_value_unreviewed` |
| `figure_object_unresolved` / `figure_fact_object_missing` | `figure_object_ids` 不属于该对象的 visual task，或 Figure/Table/DataPoint 缺少图对象 | 从 packet 的 FIGURES 中选择同页 `vo-…` ID |
| `figure_ocr_backend_missing` | 显式选择了 `--figure-ocr tesseract` 但本机没有 tesseract | 安装本地 tesseract 或去掉该选项；不会自动回退 |
| `candidate_source_derived_content_not_distributable` | Draft 含图表索引/图片（来源派生内容） | 仅私有使用；如需发布，重新 build 时不加 `--figure-index` |
| `runtime_self_check_failed` | 某个对象的 title 无法检索到自身，或决策请求未 abstain | 检查该对象 title 是否只有停用词/单字符；修改 draft 后重建 |
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


## Native evidence repair candidate

- `numeric_review_required`: proposed figure values remain in the workpack. Use the existing independent semantic and visual Review route, then resume; a figure ID, origin label, boolean or presence-only inspection cannot authorize numbers.
- `legacy_numeric_revalidation_required`: old packages remain readable. Rebuild from a correctly reviewed workpack to migrate untraced numbers; never patch the sealed old package.
- `structure_review_required` after native extraction: inspect the retained IR and bind actual source-text boundaries with `plan --start-page START --end-page END --structure-selections` for a continuous native scope. The explicit `--profile native-text` route remains available for whole-source native jobs. Scoped children retain failed structure tasks at the reported `audit_path`.
- `extraction_workpack_mismatch`: source/tool/render/checkpoint content drifted, or an unbound workspace contains drafts. Preserve the directory and replan into a fresh workspace.
- Failed or interrupted extraction directories remain recoverable as `extraction-failed-*` / `extraction-incomplete-*`; no partial directory clears completion.
- `authoring_packet_drift` / `authoring_response_binding_drift`: preserve the responses and prepare a fresh source-bound workpack, or explicitly rerun the configured command. Existing drafts require `--replace-drafts` for replacement.


## Scoped native intake and page dispositions

- `intake_workflow_upgrade_required`: inspect v0.1 and v0.2 with read-only `intake status`. Run `plan` in a fresh workspace to get a v0.3 intake workflow.
- `intake_selected_scope_invalid`: supply one valid original physical start/end pair.
- `reused_workpack_scope_gap` / `reused_workpack_scope_overlap`: existing workpacks must cover the selected range exactly without overlap.
- `reused_workpack_disposition_mismatch` / `reused_workpack_blank_proof_stale`: preserve the old workpack and rebuild incompatible page evidence in a fresh path.
- `reused_workpack_inventory_drift`: bound input changed; finish its existing authoring flow, then explicitly replan rather than rewriting the frozen workflow.
- `page_disposition_operator_required` / `page_disposition_decision_required`: actual operator input is missing; the compiler cannot provide it.
- `page_disposition_binding_mismatch` / `page_disposition_receipt_invalid`: source/render/geometry/rule or submission commitment differs. Do not repair a receipt in place.
- `page_disposition_proof_missing` / `page_disposition_commitment_mismatch`: a retained role lost its bound proof; preserve the affected evidence and rebuild explicitly.
- `page_disposition_receipt_required`: package has hash-only operator disposition evidence; pass the matching private receipt to source replay. No full replay is claimed without it.
- `non_knowledge_page_evidence`: retained context/structure/blank pages cannot support knowledge focus spans or anchors.
