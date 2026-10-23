# 参数化建模采用"模板 CATPart + 外部 Automation 驱动"，不开发自定义特征

在无 Knowledgeware(Advisor/Expert) 许可、CAA RADE 授权成本极高的前提下，参数化建模不走"自研参数化特征库"，而是每个结构族维护一套**模板 CATPart + 参数契约**（参数名/类型/范围/几何映射）与**参数化包络**（免重建的可覆盖范围）；平台用 V5 Automation(COM) 改参数并 `Part.Update()` 再生。超出包络的拓扑变更触发换模板。

## Consequences

- 新增结构族 = 建新模板，这是能力边界；不是任意几何都能"凭空参数化"。
- 模板质量与参数契约的健全度直接决定平台可用性，需作为核心资产维护。

## Considered Options

- CAA C++ 自研特征库：能力最强但需 RADE 授权 + CAA Partner 协议，成本不允许，远期按需再评估。

## 补充事实（Knowledgeware 核查，修正早期口误）

经对 DS 官方 B18 文档核查：

- **参数(Parameters)与公式/关系(Formula/Relations)是基础能力**，"available to all CATIA users"，无需 KWA/KWE；Automation 的 `CreateReal/CreateDimension/...`、`Relations.CreateFormula`、读特征树、`Part.Update()` 均为基础，不依赖 Knowledgeware。
- **Design Table 也是"provided to all CATIA users"**，并非 KWA 专属（仅其在 rules/checks 内被调用时才需 KWA）。故"Design Table 必须 KWA"不成立——是否可在零 KWA 下完全使用需在目标机实测 `Relations.CreateDesignTable`。
- 真正需要 **KWA**：Rules/Checks/Law/Reaction/SetOfEquations（`CreateCheck/CreateLaw/CreateProgram`）；**KWE**：expert rules/rule base（`CreateRuleBase`）；**PEO**：Optimizer。

**设计含义**：参数化底座 = **基础参数 + Formula + `Part.Update()`**；**业务逻辑放在外部程序，不放进 Rules/Checks**（那才需要 KWA）。批量驱动用 Automation 外层循环（Excel/CSV → set 参数 → Update → 读 Measurable/质量属性），这本身就是免 Knowledgeware 的可靠路径，Design Table 只是可选的等价载体。
