# fake-expert

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
- **Science 咨询层（source-candidate）**：多领域科学提案、离线索引、只读查询、受控标量计算与盲评验收的增量实现（M1–M4），answer-free、不授权执行、不改变正式门禁。

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
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py doctor \
  --profile native-text --source /absolute/path/to/book.pdf --json

PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py plan \
  --source /absolute/path/to/book.pdf --workspace /absolute/path/to/job \
  --profile native-text --name domain-reference \
  --display-name "Domain Reference" --domain domain-name --version 0.1.0 --json

PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py compile \
  --job /absolute/path/to/job/fake-expert-job.json --progress --json
```

统一 CLI 还提供 `status`、`resume` 与只读的 `review list / locate / status / validate-submission` 子命令。没有完整 semantic workpack 时，流程会在 `semantic_authoring_required` 处暂停——编译器不写语义答案。

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

## License

[MIT](./LICENSE)。本仓库当前处于测试阶段，发布内容不构成对任何书籍知识的认证；输出的 Reference Draft 仅 reference-only、non-decision、non-executable。
