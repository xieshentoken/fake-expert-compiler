# fake-expert 快速开始

本页只给出可复制的入口。所有路径均为占位符；请替换为本机已有文件。
统一入口是标准库脚本，运行在项目根目录时使用：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py --help
```

## 1. 原生文本 Draft

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
must be parseable, unencrypted, and have enough native text on every page. A non-PDF,
encrypted, parse-error, or any `pages_needing_ocr` result pauses as
`source_profile_mismatch` with next action `select_explicit_scan_profile`, even when
a matching semantic workpack is present. The job keeps only source/hash bindings;
`compile` reruns the check and revalidates the pinned pypdf/Python and tool hashes.
`doctor` 和 `plan` 输出当前路径的 `required_reading`；开始下一步前应读取列出的文档。
该有序清单及每个 SHA-256 写入 v0.2 job，文档变化后旧 job 会以
`required_reference_hash_mismatch` 停止，必须重新阅读并创建新 plan。旧 v0.1 job
只返回 `job_schema_upgrade_required`，不会原地升级。

没有完整 proposal workpack 时，`compile` 会暂停在
`semantic_authoring_required`；它不会替你写语义对象或调用模型。已有完整、哈希
一致的 semantic workpack 可在 `plan` 时用 `--workpack` 绑定，目标为 `draft` 时由
统一入口路由到既有 `direct_reference_skill.py`，结果仍是
`status=draft`、`review_state=deferred`、`knowledge_verified=false`。

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
