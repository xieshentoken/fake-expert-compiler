# fake-expert 术语表

括号中的标签表示主要边界：对象、证据、状态或权限。

- **source / 源文件（证据）**：原始 PDF；job 只保存路径和 SHA-256，不保存正文。
- **source hash（证据）**：源文件内容指纹；变化会使 job 和下游绑定失效。
- **native text（对象/证据）**：由固定 `pypdf==6.10.0` 提取的原生文本证据。
- **scan candidate（对象）**：扫描页 OCR/布局候选；不是 canonical truth。
- **render receipt（证据）**：由 `pdftoppm` 产生的页图及参数指纹。
- **OCR observation（对象/证据）**：绑定页、BBox、模型、配置和 runtime 的候选观察。
- **scan structure review（权限）**：外部 reviewer 对标题、页码、顺序和 BBox 的确认。
- **semantic workpack（对象）**：带 source、unit、draft、assertion 和 review 计划的输入包。
- **knowledge object（对象）**：可查询的原子知识记录，必须有 source evidence anchor。
- **evidence anchor（证据）**：对象正文对应的页、字符范围或视觉定位承诺。
- **visual object（对象）**：图、表、公式等视觉候选；需独立视觉 Review。
- **workbench（权限）**：给人类 reviewer 使用的离线、answer-free 传输目录。
- **submission（证据）**：reviewer 导出的人工表单；单独不等于 Gold。
- **attestation（权限）**：外部身份/独立性声明；编译器不能自写。
- **Gold（状态/证据）**：经既有独立 reviewer 与导入门禁接受的参考记录。
- **candidate（状态）**：尚未完成全部独立门禁的候选结果。
- **Draft（状态）**：可查询但 Review deferred 的 Reference Skill；不能作决策或执行依据。
- **verified（状态）**：特定验证器确认的契约状态，不表示知识准确或 Gold 已完成。
- **ready（状态/权限）**：通过语义、视觉、支持和 competency 等既有门禁的可用状态。
- **promoted（状态/权限）**：通过既有 promotion gate 的状态；统一 CLI 不授予。
- **executable（状态/权限）**：只有 safe-formula execution gate 可授予的执行权限。
- **job（对象/证据）**：私有、闭合、canonical-JSON hash-bound 的操作意图文件。
- **profile（权限）**：显式选择的 `native-text|scan-text|scan-structure|visual` 路由；doctor 的建议不能替代选择。
- **backend（权限/证据）**：与 profile 一起显式冻结在 job 中的 parser/renderer 身份；统一入口不自动选择、切换或 fallback。
- **runtime receipt（证据）**：既有专项 runtime qualification 的本地收据；job 只绑定路径和 SHA-256，缺失或未由权威 validator 确认时保持暂停。
- **pause / 暂停（状态）**：缺少人工、runtime 或独立证据时的 fail-closed 状态。
- **source candidate version（状态）**：历史 `1.2.0-ux` 身份；只读保留，不用于新 job。
- **required reading（证据/权限）**：按 profile、目标 stage 与 Gold mode 生成的有序参考文档清单；路径和 SHA-256 随 job 绑定，缺失或漂移即暂停。
- **document contract（证据）**：`doctor/plan` 对 SKILL 路由、关键错误码和版本矩阵执行的机器检查。
- **formal release（状态）**：当前为 `v1.2.0`；历史 v1.1.0 及更早目录和 ZIP 保持不可变。
- **exact-package Codex certification（证据）**：外部 sidecar 对精确 compiler ZIP 的负向路由行为认证；不证明任何书本的知识或 OCR 准确率。
