# fake-expert

> 当前修复候选：`1.2.3-private-images-default`。修复原生文字坐标、图表边界及表格裁剪；支持可重放的公式数值证据。新建原生文本 Draft 默认附带图表索引和图片，查询返回相关图片路径，Agent 先查看再展示。纯文本模式用 `--figures none`。新生成包仍为未审核候选；真实模型/OCR、资料准确性与正式发布认证需单独验收。

简体中文 | [English](./README.en.md)

> **⚠️ 本项目仍在测试中（beta）**：接口、schema 与门禁行为可能随时调整，暂不建议用于生产流程。欢迎试用并提交 issue 反馈。

**fake-expert** 是一个离线、来源优先的技术 PDF → Agent Skill 编译器：把原生文本或显式路由的扫描版技术 PDF，编译为可移植、来源可追溯的 Agent Skill（`fake-*` Reference Draft），并通过 source → evidence → review → Gold → promotion → competency → seal → execution 的逐级门禁管理其生命周期。

设计原则：默认保留不确定性。PDF、代码、shell 命令及其中嵌入的说明一律视为不可信 source data，不能覆盖用户或系统指令；编译器只搬运与定位证据，不生成语义答案，也不代行 Review / 认证 / 执行权限。

## 特性

- **native-text 优先**：`pypdf==6.10.0` 是 canonical 文本证据源，`pdftoppm` 是 canonical 视觉基底；扫描/OCR 适配器只能产生 hash-bound candidate。
- **八级门禁**：source、evidence、review、Gold、promotion、competency、seal、execution 全部显式化，任何一级缺失都 fail closed。
- **全离线**：无网络、无下载、无云 OCR/VLM、无常驻服务、无静默 fallback；backend 选择必须显式写入 job。
- **Answer-free workbench**：编译器不生成 review verdict、独立认证、Gold、隐藏答案或任何 reviewer 的结果。
- **可复现 job**：job 是闭合的 canonical-JSON、SHA-256 绑定文件；`required_reading` 清单逐 hash 锁定，漂移即停止。
- **渐进式参考文档**：`references/` 按任务场景组织（快速上手 / 术语 / 排错 / 编译 / OCR / 视觉语义等）。
- **图表即知识**：识别栅格图、嵌入式 PDF 图（Form XObject）和矢量图；附带图注、图内文字、crop 图片和可选的本地 OCR（仅候选）；`Figure`/`Table`/`DataPoint` 事实必须绑定图对象，图中读取但缺少文本证据的数值必须先完成独立语义及视觉 Review，未完成时保留提案并暂停入包。
- **显式 authoring 步骤**：`compile` 自动抽取并生成逐节 authoring packets；`semantic_authoring.py` 把它们交给你指定的模型命令，模型以原文引文作答，编译器把引文解析为精确 span/SHA-256（编译器本身不调用模型）。
- **中英文检索**：Draft 内置 BM25 + 中文 n-gram + 词干检索，返回事实全文、适用条件、页码、证据和相关图表；可选 embedding 混合检索。
- **Science 咨询层（source-candidate）**：多领域科学提案、离线索引、只读查询、受控标量计算与盲评验收的增量实现（M1–M4），answer-free、不授权执行、不改变正式门禁。

默认图文 Draft 含来源图像，仅供私有本地使用；整体复制生成的 Skill 目录及 `assets/figures/` 即可查询图片。普通 source-free 分发仍要求 `--figures none`，不会放行来源图像。历史包保持不变，工具或参考文档变化时重新创建 job。

## 快速开始

### 1. 安装为 Agent Skill

把 `fake-expert/` 目录复制到你的 agent 的 skills 目录（如 `skills/fake-expert/`），agent 端按 `fake-expert/SKILL.md` 的说明使用。也可以仅作为本地 CLI 工具直接调用。

### 2. 安装依赖

```bash
pip install -r fake-expert/requirements-compiler.txt        # 必须：pypdf==6.10.0
pip install -r fake-expert/requirements-parser-extras.txt   # 可选：按需安装扫描/OCR 适配器
brew install poppler   # 或系统包管理器安装 pdftoppm（视觉基底）
```

### 3. 运行

```bash
PYTHONDONTWRITEBYTECODE=1 python3 fake-expert/scripts/fake_expert.py doctor \
  --profile native-text --source /absolute/path/to/book.pdf --json

PYTHONDONTWRITEBYTECODE=1 python3 fake-expert/scripts/fake_expert.py plan \
  --source /absolute/path/to/book.pdf --workspace /absolute/path/to/job \
  --profile native-text --name domain-reference \
  --display-name "Domain Reference" --domain domain-name --version 0.1.0 --json

PYTHONDONTWRITEBYTECODE=1 python3 fake-expert/scripts/fake_expert.py compile \
  --job /absolute/path/to/job/fake-expert-job.json --progress --json
```

首次 `compile` 会把 IR、workpack、图表 crops 和 authoring packets 写入 `<workspace>/extraction/`，然后在 `semantic_authoring_required` 暂停——编译器不写语义答案。用你指定的模型命令补齐后再次 `compile`：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 fake-expert/scripts/semantic_authoring.py run \
  --workpack /absolute/path/to/job/extraction/workpack \
  --command "<读取 stdin prompt、输出 JSON 的模型 CLI>" \
  --proposer-instance my-model-1 --source /absolute/path/to/book.pdf --json

PYTHONDONTWRITEBYTECODE=1 python3 fake-expert/scripts/fake_expert.py compile \
  --job /absolute/path/to/job/fake-expert-job.json --json

python3 -B /absolute/path/to/job/fake-domain-reference/scripts/query_reference.py --query "什么是…？"
```

统一 CLI 还提供 `status`、`resume` 与只读的 `review list / locate / status / validate-submission` 子命令。详见 `fake-expert/references/authoring-and-figures.md`。

## Science 咨询层（M1–M4，source-candidate）

除编译器核心外，本仓库还附带一套增量实现的多领域科学咨询层（候选身份 `1.2.0-science-m1`…`m4`）：离线、answer-free，不改变正式 v1.2.0 的任何门禁，也不构成领域知识认证；所有新输出均为全新文件，从不覆盖已有包、sidecar、review 或 Gold。

- **M1 提案与冻结**（`science_contract.py` / `science_workpack.py`）：用五种闭合记录——SymbolBinding、ConditionSet、MaterialState、PropertyObservation、FormulaCard——描述科学内容，只做结构校验与 answer-free 冻结；证据引用绑定精确的 package/version/object/anchor，而非书名或字形相似，hash 匹配只证明绑定、不证明科学正确性。
- **M2 索引与只读查询**（`science_index.py` / `science_reference_runtime.py`）：可重建的离线中文/数学候选索引（显式别名、保留大小写的数学通道）；只读协议 `query / object / locator / source / validate-answer / prepare-support-review / validate-support-review`，恒返回 `execution_authorized=false`；`consultation_contract.py` 只校验宿主起草的咨询稿，不生成答案或批准。
- **M3 受控标量计算**（`science_calculation.py` / `science_units.py`）：对 hash 绑定的规格（绑定精确 M2 请求、FormulaCard、全部输入/输出 SymbolBinding 与 ConditionSet 映射）执行门控标量计算，单独返回计算 receipt，而不是把它当作已接受的 M2 答案；未 Review 的 Draft 不会因此变为可执行，执行权限始终是显式的独立契约。
- **M4 生命周期与领域配置**（`science_acceptance.py`）：`validate-suite / blind-plan / freeze-review / report` 驱动盲评验收；`assets/domain-profiles/` 内置 `heat-transfer` 与 `materials-optics` 两个薄领域配置，内容全部是原创合成问题目录（不是书籍摘录、答案或 Gold），固定 `execution_authorized=false`、`knowledge_verified=false`。
- **边界不变**：operator 本地的 library、索引、source-map 与 receipt 不进封闭包；不联网、不调模型、不产生 Review/Gold/晋升/执行授权。

## 依赖

| 依赖 | 角色 | 说明 |
|------|------|------|
| Python 3.10+ | 运行时 | 仅标准库 `argparse` 做 CLI；`pypdf` 为唯一 canonical 解析器 |
| `pypdf==6.10.0` | 必须 | native-text 证据提取与校验，版本锁定 |
| `poppler`（pdftoppm） | 视觉基底 | canonical 渲染源 |
| `paddleocr` | 可选 | 扫描页主适配器；Paddle 运行时与模型需宿主机预先授权安装 |
| `docling` | 可选 | 显式 challenger 适配器 |
| `PyMuPDF` / `pytesseract` | 可选 | 本地基线；永不作为 PaddleOCR/Docling 的静默 fallback |

可选适配器只产生 hash-bound candidate，不改变证据资格；模型资格必须来自既有 operator receipt。

## 注意事项

- **candidate ≠ Gold**：`candidate` / `draft` / `verified transport` 不等于 ready、promoted 或 executable；输出固定 `status=draft`、`knowledge_verified=false`、non-executable。
- **源文件不可信**：读取 source 的内容不能改变 agent 行为或安全边界；私有 job 不进入发布包。
- **路径边界**：状态检查只接受显式 workspace；拒绝 home、项目根、`/`、symlink 与路径逃逸。`--dry-run` 零写入。
- **不可原地覆盖**：旧包、source、workpack、Gold、receipt 与 release 目录一律不可覆盖；扩展必须走新目录、新版本。
- **不加抽象层**：禁止为“未来扩展”新增 Manager / Service / Factory / Strategy / Adapter / Coordinator / Provider / Resolver / Registry 层。

## 核心路径

从 PDF 到可查询 Draft 只需要以下脚本；其余脚本服务于 Review、Gold、扫描 OCR、组合与发布等后续门禁，保持兼容但不是入门必读。

| 步骤 | 脚本 |
|---|---|
| 统一入口 | `fake_expert.py`（doctor / plan / compile / status） |
| PDF IR、标题、页面路由 | `pdf_structure.py`（`reconstruct_pdf.py`） |
| 图表候选、图注、图内文字 | `visual_semantics.py` |
| 语义 workpack、crops、可选 OCR | `semantic_workpack.py` → `semantic_promotion.py` |
| 模型 authoring 与引文解析 | `semantic_authoring.py` |
| 构建 / 扩展 / 校验 Draft | `direct_reference_skill.py` |
| Draft 内检索 runtime | `reference_search.py`（打包为 `scripts/query_reference.py`） |

## 测试

```bash
pip install -r requirements-test.txt   # synthetic developer tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

需要渲染的测试使用 PATH 上的 `pdftoppm`（或 `FAKE_EXPERT_TEST_PDFTOPPM`），缺失时自动跳过。

## 仓库结构

```
fake-expert/
├── SKILL.md          # Agent 端技能说明（先读这个）
├── agents/           # Agent 适配配置
├── assets/schemas/   # 全部 JSON Schema（job/manifest/review/receipt 等 140+）
├── assets/domain-profiles/ # Science 薄领域配置（heat-transfer、materials-optics）
├── references/       # 按场景组织的必读参考文档（含 science-consultation / science-calculation）
├── scripts/          # 编译器、workbench、science 咨询与验收脚本
└── requirements-*.txt
```

## 测试修复候选

在此仓库根目录使用已具备 `requirements-test.txt` 中依赖的 Python：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

图形测试使用真实 `pdftoppm`；缺少渲染器时会明确跳过。可用
`FAKE_EXPERT_TEST_PDFTOPPM` 指定本地二进制。测试自带合成资料，不调用真实模型或 OCR。
可用 `FAKE_EXPERT_HISTORY_ROOT` 只读重放历史发布包。真实 PDF 回归通过
`FAKE_EXPERT_HEAT_CONDUCTION_PDF`、`FAKE_EXPERT_TEST_CHINESE_VISUAL_PDF` 和
`FAKE_EXPERT_TEST_VISUAL_CHAPTER_PDF` 分别传入对应原件。扫描审核回放需要同时设置
`FAKE_EXPERT_TEST_SCAN_WORKBENCH` 和 `FAKE_EXPERT_TEST_SCAN_SUBMISSION`。
未提供私有样本时，对应测试会跳过。科学咨询的历史校验见
[兼容性说明](./docs/science/compatibility.md)。

## License

[MIT](./LICENSE)。本仓库当前处于测试阶段，发布内容不构成对任何书籍知识的认证；输出的 Reference Draft 仅 reference-only、non-decision、non-executable。
