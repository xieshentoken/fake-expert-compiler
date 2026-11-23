# 科学侧挂兼容边界

科学入口保留旧包的 Review、Gold、promotion、competency、seal 和 execution 门禁。附加字段、calculate 意图、结构/hash 有效或测试通过，都不能增加执行权限。缺侧挂时科学语义 unavailable，原参考接口仍可使用。

## 历史 M4 与当前代际

M0 原 `fixtures/legacy-synthetic.json` 的 7 个 gate hash 是历史 M4 字节锁，保持原值。历史校验先验证原 M4 ZIP 的固定 SHA-256 `ff2d2ba9c394bb27312c98bc8db51753c6d4a4f5506f1017624f65f2d190e777`，再逐项核验归档中的 7 个文件；当前仍未演进的 5 个门禁继续对源码逐字节核验。

当前 `intake-v2` / direct-reference 1.4.1 代际的知识对象仍声明 v0.1。相对历史 M4，schema 增加可选 `figure_refs`、`value_basis` 和 Figure/Table/DataPoint 类型，原 required 列表与其他旧属性 shape 保持兼容。`semantic_promotion.py` 已扩展原生页角色与证据排除流程，因此不能再声称其源码与历史 M4 逐字节相同。回归同时验证旧有效输入可读、当前旧式有效 proposal 可接受，以及独立审核缺失时 promotion、ready publication 和执行越权仍被实际门禁阻止。Proposal 层对多余字段的既有容忍行为不构成审核或执行授权。

`RELEASE_VERSION=1.2.0`、旧 CLI/job v0.2、知识对象 v0.1 和 AST v0.1 不变。原分词与单位限制保留在原协议；没有重算旧 runtime hash。新的 `science-runtime-compat-v0.1` 按已声明 compiler/catalog/runtime 联合选择旧实现，未知版本与错代绑定拒绝；支持矩阵和派生索引迁移见 [科学咨询运行时兼容](../../fake-expert/references/science-consultation.md#package-runtime-compatibility)。canonical 布局下对应文件在 `skills/fake-expert/references/`。

旧 index、bundle、answer 必须在新目录重建；工具或 review 输入变化时重新冻结并获得独立审核，不修改 sealed 原包或旧证据。来源只支持自身对象的 evidence/focus，邻页上下文不能代替正文证据；现有资料验收不能自动为新侧挂背书。

## 便携测试与私有历史证据

科学测试自动识别 `skills/fake-expert/`（canonical）或 `fake-expert/`（公共仓库）布局，二者同时存在时拒绝猜测。运行 `python -B -m unittest discover -s tests/science -t tests -v`，并确保现有合成 PDF 集成测试依赖可用。

历史证据通过显式环境输入提供，不提交个人绝对路径或私有证据：

- `FAKE_EXPERT_SCIENCE_LEGACY_ARCHIVE`：上述固定原 M4 ZIP。
- `FAKE_EXPERT_SCIENCE_PRESERVATION_MANIFEST`：原 `m0-preservation.json`。
- `FAKE_EXPERT_SCIENCE_PRESERVATION_ROOT`：该 manifest 中相对路径的只读根目录。

未提供时，私有历史部分明确 skip，当前版本约束与 5 个字节锁仍执行；此结果不能宣称完成历史保护。本机验收必须提供全部输入，实际核验原 791 个发布文件和历史 7 锁，不能以 skip 代替。

原 M0 记录中的“Git 无有效 HEAD、全部未跟踪”和 `v1.2.0-ux` 施工路径只描述当时主工作区快照，不是当前公共仓库操作指令。回滚应停用新科学 host 路由并选择保留的旧包与匹配派生物；如需恢复施工文件，使用本轮实际备份和文件 allowlist，不执行 git reset/clean 或覆盖无关修改。任何历史发布、Gold、原件和旧 hash 锁均保持只读。
