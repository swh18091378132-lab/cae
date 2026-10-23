# CATIA V5R18 / Windows：无 Knowledgeware 许可下的参数化与 V5 Automation 核查

> 要点：**参数(Parameters)与公式/关系(Formula)是基础能力，"available to all CATIA users"**；需 **Knowledge Advisor (KWA)** 的是 **Rules/Checks/Law/Reaction/SetOfEquations**；**Knowledge Expert (KWE)** 才提供 expert rules/rule base/types。**Design Table 在 R18 文档明确"provided to all CATIA users"**（基础设施），与"Design Table 必须 KWA"的前提**相反**。

## 1) Parameters / Relations / Formulas 是否基础能力
**CONFIRMED（基础，不需 KWA/KWE）。** R18 官方原文："Unlike the parameter and formula capabilities which are available to all CATIA users, the rule and check capabilities require the Knowledge Advisor product."（[Using Rules, B18](http://ps-2.kev009.com/CATIA-B18/kwrug_C2/kwruggs04.htm)）。参数/公式/Design Table 收录在 **"Knowledgeware Infrastructure"** 指南而非 KWA 指南（[3002](http://ps-2.kev009.com/CATIA-B18/cfyugkwr_C2/cfyugkwr3002.htm)、[About Parameters](http://ps-2.kev009.com/CATIA-B18/cfyugkwr_C2/cfyugkwr3003.htm)）；类型含 Length/Angle/Integer/String/Boolean 等。

## 2) V5 Automation 对 CATPart 能与许可
API 属 `KnowledgeInterfaces`/`MecModInterfaces`；"方法存在≠有许可"，未授权产品调用会报错。
- (a) 加/取/设参数：`Part.Parameters.CreateReal/CreateDimension/CreateInteger/CreateString/CreateBoolean`、`Item().Value`（[Parameters](https://catiadesign.org/_doc/V5Automation/generated/interfaces/KnowledgeInterfaces/interface_Parameters_26337.htm)）——**基础**。
- (b) 建公式：`Part.Relations.CreateFormula(name,comment,outParam,body)`（[Relations](https://catiadesign.org/_doc/V5Automation/generated/interfaces/KnowledgeInterfaces/interface_Relations_25383.htm)）——**基础**。
- (c) 读特征树：`Part.Bodies/Shapes/HybridBodies/FindObjectByName`、`Selection`——**核心建模**。
- (d) `Part.Update()` 再生：核心 MecMod 方法（[pycatia Part.update](https://pycatia.readthedocs.io/en/0.6.2/api/pycatia/mec_mod_interfaces/part.html)）——**无需 Knowledgeware**。
- 需 **KWA**：`CreateCheck/CreateLaw/CreateProgram/CreateSetOfEquations`；需 **KWE**：`CreateRuleBase`；**PEO**：`Relations.Optimizations`（注明"only when the Product Engineering Optimizer license is available"）。

## 3) 各能力归属
- **Design Table**：**基础设施**——"provided to all CATIA users"，KWA 只是"make the best use of it"（[About Design Tables](http://ps-2.kev009.com/CATIA-B18/cfyugkwr_C2/cfyugkwr3005.htm)）。→ **"Design Table 是 KWA 特性"不成立**（仅其读函数用于 rules/checks 时才需 KWA）。
- **Rules / Checks**：**KWA**（§1）。**Law**：**KWA**（[1008](http://ps-2.kev009.com/CATIA-B18/kwrug_C2/kwrugat1008.htm)）。**Reaction/Actions/SetOfEquations/VBScript**：**KWA**（[KWA 包](http://ps-2.kev009.com/CATIA-B18/kwxug_C2/kwxughlpknowledgeadvisor.htm)）。
- **Knowledge types / Expert rules / rule base**：**KWE**（[KWE 2](https://3dswym.3dexperience.3ds.com/wiki/catia-user-community/catia-knowledge-expert-2-kwe_tgqmO4ZmTgSzwoTX9Za9cA)）。**Optimizer**：独立 **PEO**（[R18 配置矩阵](https://public.dhe.ibm.com/software/plm/uk/v5r18_configurations_en.pdf) 中 KWA/KWE/PEO 分列）。

## 4) 免 Knowledgeware 的 "Design-Table 式" 外部驱动
**CONFIRMED 可行**：循环 `设参数 → Part.Update() → 读几何/质量属性` 只用基础参数 + 核心 `Update`，**不需 KWA/KWE/PEO**。前提是**本机有已授权 CATIA 会话**（Automation 拉起 `CNEXT` 进程）——"license-free"仅指**免 Knowledgeware 许可**，非零许可。

## 5) V5R18 是否有 64-bit Windows
**CONFIRMED**：R18 安装指南列出 `B18\win_b64`（"64-bit code on Windows XP Professional x64 Edition"）与 `intel_a`（32-bit）并存（[About the Environment](http://ps-2.kev009.com/CATIA-B18/basil_C2/basilgs12.htm)）；64-bit 早在 V5R16 由 Toyota 部署（[Microsoft/DS 2006](https://news.microsoft.com/source/2006/07/17/toyota-motor-experiences-the-power-of-catia-v5-on-microsoft-windows-xp-64-bit-edition/)）。**内存**：DS 白皮书给 32-bit 进程 XP 上 2GB+1GB≈**3GB 上限**；64-bit 上限=RAM+swap（**8TB 级**）（[DS Memory Allocation](https://www.haycad-infotech.bg/uploads/haycad/Support_docs/DS_WhitePapers_MemoryAllocationOnCATIAV5.pdf)）→ 大装配须用 `win_b64`。

## Confirmed vs Uncertain
**CONFIRMED**：参数/公式=基础，"available to all"；Rules/Checks/Law/Reaction/SetOfEquations 需 KWA；expert rules/rule base 需 KWE；Optimizer 需独立 PEO；Design Table 标注"provided to all"；Automation 可建/读/改参数、`CreateFormula`、读特征树、`Update()`；R18 有 `intel_a` 与 `win_b64`（XP x64），32-bit≈3GB vs 64-bit 近乎无上限。

**UNCERTAIN**
- ⚠️ **Design Table 在"完全无 KWA"下是否 100% 可用**：文档称"provided to all"，但部分版本图标/读函数与 KWA 绑定，**建议实测 `Relations.CreateDesignTable`**。
- Automation API 名取自 R28 文档；**R18 是否逐条同名同签名**未逐项验证（参数/公式/Update 有 2003 版文档佐证，风险低）。
- 无 KWA 时 `CreateCheck/CreateLaw` 的**具体报错**未实测（按归属推断会失败）。
- R18 64-bit 仅面向 **Windows XP Professional x64**（非 Vista/7）。

## So what
- **参数化底座 = 基础参数 + Formula + Update**：可驱动项全暴露为 user parameters，公式只做纯参数约束；**业务逻辑别放 Rules/Checks**，放外部程序。
- **Design-Table 式批量驱动用 Automation 外层循环**（Excel/CSV → `Parameters.Item().Value=…` → `Part.Update()` → `Measurable/Analyze` 取质量/包围盒），是**唯一不需 KWA/KWE 的可靠路径**；批量提交+重试。
- **一律 `win_b64` 启动**突破 32-bit ~3GB；接受"依赖本机已授权 CATIA、无 headless 免许可"的现实，做进程守护与宏健壮性。
