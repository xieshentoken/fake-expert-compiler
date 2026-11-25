# fake-expert 快速开始

本页只给出可复制的入口。所有路径均为占位符；请替换为本机已有文件。
以下自动入口例子以当前 Skill 目录为工作目录；旧例中的 `skills/fake-expert/` 前缀也须替换
为当前 Skill 路径。统一入口是标准库脚本，运行在项目根目录时可使用：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py --help
```

## 1. 自动 intake 与普通编译

普通调用无需猜 profile。只读检查后，`plan` 自行执行同一预检并冻结新 workflow：

```bash
python3 scripts/fake_expert.py intake inspect --source /absolute/path/to/book.pdf --json
python3 scripts/fake_expert.py plan --source /absolute/path/to/book.pdf \
  --workspace /absolute/path/to/new-workspace --name domain-reference --figures none --json
python3 scripts/fake_expert.py compile --workflow /absolute/path/to/new-workspace/intake-workflow.json --json
```

结果包含同序 `page_ledger`、`page_summary`、显式 `confirmed_blank_pages`。`native` 和
`confirmed_blank` 可组成安全原生范围；真正的 `needs_ocr` 页数决定 OCR 批量建议与实际请求预算。
`review_required` 保留物理页及原因，不通过改写 PDF 或占位文字绕过。不可信封面首行只是提示，
扫描封面无法原生读取时为 unknown；印刷页标签 null 合法。

原生任务在 `children/pages-START-END/` 暂停 `semantic_authoring_required`；按返回的 workpack
路径使用既有 authoring/ingest，完成 drafts 后 `resume --workflow ...` 构建 Draft。结构定位失败
会把任务和 IR 保存在明确的 `audit/*-failed-*` 私有路径；该目录不是知识包。

需要 OCR 但环境不可用时，返回持久化 choice_request。按其 ID 只询问一次，不循环提问：

```bash
python3 scripts/fake_expert.py intake choose --workflow /path/to/intake-workflow.json \
  --choice configure_environment --ocr-config /absolute/path/to/local-ocr.json --json
# 或用户明确选择可行降级：
python3 scripts/fake_expert.py intake choose --workflow /path/to/intake-workflow.json \
  --choice native_ranges --json
python3 scripts/fake_expert.py intake run --workflow /path/to/intake-workflow.json --json
```

`native_ranges` 实际建立原件上的连续原生+确认空白子任务；顶层始终保留完整页账本、
`partial=true` 和 `omitted_pages`。旧 v0.2 的整源 native gate 不会被 workpack 范围绕过。
全扫描可选 `inspection_only`，不会生成知识包；全空白直接 `no_content`；纯人工待审来源
只提供 review/checklist，不提示安装 OCR。

显式 `--ocr-config` 优先于 `FAKE_EXPERT_OCR_CONFIG`。后者仅复用指向的已有 JSON，不搜索或
安装环境。合同与例子见 `scanned-pdf-ocr.md`。配置可用不等于已获模型执行授权：

```bash
# 仅在用户已授权该 source/config/backend 模型 OCR 后：
python3 scripts/fake_expert.py intake run --workflow /path/to/intake-workflow.json \
  --authorize-ocr --exclusive-heavy-model --json
python3 scripts/fake_expert.py intake resume --workflow /path/to/intake-workflow.json --json
```

每次 run/resume 只预处理当前连续原物理范围，并有界执行 5 个 OCR 请求（OCR 页数超过 20 时为 20 个），继承资格计划的
startup/per-request/total timeout，保存既有 checkpoint；只复用完整且绑定匹配的页/区域。
`ocr_progress` 给出实际 completed/remaining 页号。仅预算耗尽且有进展时可按
`next_action=resume_next_ocr_batch` 继续；真实 worker 错误或零进展要求修复后显式恢复。
全部 OCR 完成仍暂停 `structure_review_required`，继续既有独立结构/语义 Review。
`--dry-run` 在 intake run/resume 或 compile/resume 均不写 workflow/子任务，也不执行 worker，
即使同时传入授权参数。`doctor --json` 报告 active Skill root、构建 fingerprint 和能力。

## 保留的显式 v0.2 原生路径

先只读检查环境，再创建显式 profile 的私有 job：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py doctor \
  --profile native-text --source /absolute/path/to/book.pdf --json

PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py plan \
  --source /absolute/path/to/book.pdf \
  --workspace /absolute/path/to/empty-job-workspace \
  --profile native-text \
  --name domain-reference \
  --display-name "Domain Reference" \
  --domain domain-name \
  --version 0.1.0 --json

PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py compile \
  --job /absolute/path/to/empty-job-workspace/fake-expert-job.json \
  --progress --json
```

Native `plan` and `compile` both enforce the canonical source/profile gate. The PDF
must be parseable, unencrypted, and have enough native text on every content page.
Both whole-book checks and scoped extraction exempt only pages with a replayable
nonpainting-content or restricted opaque-white rectangle proof plus exact-white full-page local `pdftoppm` render. Confirmed
blank pages keep their original physical numbers/range and supply no semantic
units, body spans or evidence anchors. Zero text or white pixels alone never
qualify; image-only, short-text, annotation and uncertain pages keep their gates.
An entirely blank source reports `source_contains_no_native_text`. A non-PDF,
encrypted, parse-error, or any `pages_needing_ocr` result pauses as
`source_profile_mismatch` with next action `select_explicit_scan_profile`, even when
a matching semantic workpack is present. The job keeps only source/hash bindings;
`compile` reruns the check and revalidates the pinned pypdf/Python and tool hashes.
New jobs and extraction checkpoints also bind `native_blank_pages.py`. Older v0.2
jobs remain readable but a missing binding reports `tool_hash_mismatch:native_blank`;
preserve them and replan, without editing their hashes.
`doctor` 和 `plan` 输出当前路径的 `required_reading`；开始下一步前应读取列出的文档。
该有序清单及每个 SHA-256 写入 v0.2 job，文档变化后旧 job 会以
`required_reference_hash_mismatch` 停止，必须重新阅读并创建新 plan。旧 v0.1 job
只返回 `job_schema_upgrade_required`，不会原地升级。

没有绑定 workpack 时，首次 `compile` 会把 IR、workpack、图表 crops 和 authoring
packets 写入 `<workspace>/extraction/`，然后暂停在 `semantic_authoring_required`；
它不会替你写语义对象或调用模型。用你显式指定的模型命令补齐 drafts，再次 `compile`：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/semantic_authoring.py run \
  --workpack /absolute/path/to/empty-job-workspace/extraction/workpack \
  --command "<读取 stdin prompt、输出 JSON 的模型 CLI>" \
  --proposer-instance my-model-1 --source /absolute/path/to/book.pdf --json
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py compile \
  --job /absolute/path/to/empty-job-workspace/fake-expert-job.json --json
```

新建 native-text `plan` 默认采用 `--figures index+images`，把来源派生的图表索引及图片打进
Draft，仅供私有使用，发布打包会拒绝。已有完整、哈希一致的 semantic workpack
仍可在 `plan` 时用 `--workpack` 绑定。结果仍是
`status=draft`、`review_state=deferred`、`knowledge_verified=false`。详见
`references/authoring-and-figures.md`。

## 2. 扫描 PDF Draft

使用 `scan-text` 或 `scan-structure`，必须用 `--backend` 显式冻结 backend；运行时、
模型和 worker 仍在既有扫描契约中绑定。统一入口只检查已绑定的本地 receipt，并在
缺少资格 receipt 或结构 Review 时暂停：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py plan \
  --source /absolute/path/to/scan.pdf \
  --workspace /absolute/path/to/scan-job \
  --profile scan-structure \
  --backend paddleocr-ppstructure-v3 \
  --runtime-receipt /absolute/path/to/existing-qualification-receipt.json --json
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py compile \
  --job /absolute/path/to/scan-job/fake-expert-job.json --json
```

此入口不会运行 OCR、加载模型、下载资产、启动 resident service 或把候选当作
canonical truth。典型暂停顺序包含 `runtime_qualification_required`、
`structure_review_required` 和 `independent_review_required`。
缺少 `--backend`、backend 不适用于 profile、receipt 不能通过既有专项 validator，
分别保持为 `backend_required_for_profile`、`backend_invalid_for_profile` 或
`runtime_qualification_unverified`；统一入口不会推荐后自动落地，也不会切换 backend。

`scan-text` 可显式选择 `paddleocr-ppocrv6`、`pymupdf-tesseract` 或 `docling`；
`scan-structure` 选择 `paddleocr-ppstructure-v3`；`visual` 选择 `pdftoppm`、
`paddleocr-ppstructure-v3` 或 `docling`。选择记录在 job 的 `backend.name`，可选的
`backend.runtime_receipt` 只保存路径和 SHA-256，不保存 receipt payload。

## 3. 已有 Gold / Review

Gold 只能由既有外部 reviewer、attestation 和 import/finalize 门禁产生。创建 job
时可用 `--gold-mode reuse --gold-revision ...` 绑定已存在 revision；不能把
`create` 当作自动制 Gold 命令。

统一只读发现已有 workbench：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py review list \
  --workspace /absolute/path/to/workspace --json
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py review locate \
  --workbench /absolute/path/to/workbench --json
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py review status \
  --workbench /absolute/path/to/workbench --json
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py review validate-submission \
  --workbench /absolute/path/to/workbench \
  --submission /absolute/path/to/submission.json --json
```

这些命令不创建 reviewer/session ID，不显示其他 reviewer 的结果，不生成 Gold 或
attestation，也不修改 workbench。实际 `finalize/import` 仍使用原专项脚本。

## 4. Extend

已有 direct Reference Draft 必须通过既有脚本以新目录、新 package version 扩展：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/direct_reference_skill.py extend \
  --base /absolute/path/to/old-fake-skill \
  --source /absolute/path/to/same-source.pdf \
  --workpack /absolute/path/to/additional-workpack \
  --output /absolute/path/to/new-fake-skill \
  --version 0.2.0 --created-at 2026-08-31T00:00:00+08:00
```

旧目录不覆盖，未 Review 的 Draft 不进入 reviewed-draft composer。
确认空白页仍属于原物理页范围：范围 `[1,3]` 与 `[4,6]` 可以连接，`[1,2]` 与 `[4,6]`
即使第 3 页已确认空白也继续报 `non_contiguous_source_scope_requires_separate_package`。
先按原范围重新抽取，再扩展；不要手改 scope。新版本在同级临时目录完整构建并验证后原子发布，
连续性或写入失败不会在目标留下旧包或半包。不存在或已存在的空目录均可作目标；非空目录和
symlink 保持拒绝，原底包不变。

## 5. Ready / Executable

统一入口不能升级状态。`reviewed`、`ready` 和 `executable` 目标只会把缺少的
`independent_review_required`、`competency_required`、`seal_required` 或执行门禁
报告为 next action。必须继续使用既有独立 Review、promotion、competency、seal、
host certification 和 safe-formula execution 契约；不能编辑状态字段绕过门禁。

## 只读诊断与恢复

```bash
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py status \
  --job /absolute/path/to/fake-expert-job.json --json
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py status \
  --workspace /absolute/path/to/explicit-workspace --all --json
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py resume \
  --job /absolute/path/to/fake-expert-job.json --dry-run --progress --json
```

`--dry-run` 不创建、修改或删除文件；progress 是 stderr 上的 JSONL 诊断，不是
知识、Gold、准确率或发布证据。

需要纯文本、source-free Draft 时显式传 `plan --figures none`；已有 job 不会改用新的带图默认值。查询相关图像位于 `figures[].image`，相对生成 Skill 目录解析，先查看后内联展示，并保留图号、物理页和未审核说明。

## 选择原物理范围与只读复用

自动 intake v0.3 支持在原 PDF 上选择一个连续范围，不裁剪、不改写、不重编号：

```bash
python3 scripts/fake_expert.py plan --source /absolute/path/to/book.pdf \
  --start-page 10 --end-page 80 --workspace /path/to/fresh-workspace --json
# 已有包可重复传入，按原件 SHA、物理范围和证据验证，必须无重叠且完整覆盖所选范围：
python3 scripts/fake_expert.py plan --source /absolute/path/to/book.pdf \
  --start-page 10 --end-page 80 --workpack /path/to/workpack-a \
  --workpack /path/to/workpack-b --workspace /path/to/fresh-reuse --json
```

`page_ledger/page_summary` 仍覆盖原件全部物理页；`selection` 单独给出所选范围、范围外页、
非知识页、知识候选页和选中页分类。`selection_state` 区分范围外、选中候选、已处置非知识和
未解决页。目录识别的 frontmatter/backmatter/navigation 只是 `outline_context_hint`；
Index/References unit 继续使用原有 non-knowledge 规则。显式 start/end 是排除范围外页面的
授权，不是知识 Review；`whole_book_complete=false` 始终保留。范围内未知页仍是缺口，不能
靠排除标志桥接连续性；选择 `native_ranges` 会分成独立包，`extend` 仍拒绝跳页。

复用会检查 workpack/source/scope/pypdf normalization、原页证明、资源、proposal 和逐文件
inventory。完整包直接构建，零重提取、零重复 authoring；旧纯原生包可按原证据回放验证。
包含不兼容页面证明的旧包明确要求重建。多包按各自物理范围建立子包。
不完整包保持只读，返回已有 workpack 和 `author_existing_workpack_then_replan_fresh_workflow`；
在该包原有 authoring 路径完成后显式重新 plan，不能修改已冻结 workflow 的 inventory。
对尚未提取的连续原生范围，`plan --structure-selections` 可绑定本书正文标题的
原页、字符跨度和 SHA-256，不能与 `--workpack` 同用。
`native_focus.preview` 报告撰写单元数、分片章节数和正文跨度哈希。超过 20,000
字符时优先在物理页边界分片；单页仍超限时在句段或词边界分片。工作包保留真实标题
和父章节身份，开始撰写前核对计划与实际跨度。
`plan` 和 `intake plan` 均接受 `--term-aliases /path/to/aliases.json`，也可与
`--workpack` 同用。计划内冻结词表内容和原文件哈希，`compile` / `resume`
遇到输入漂移时拒绝继续。词表用于原生 Draft 检索，文件路径只留在私有 workflow 中。

错源、范围缺失/重叠、收据或资源漂移均拒绝。旧 intake v0.1/v0.2 只读 `status`，执行报
`intake_workflow_upgrade_required`，须保留原任务并在新目录重建；旧显式 profile/v0.2 job 接口不变。
