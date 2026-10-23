# CATIA V5 版本矩阵：求“参数化驱动”的最低版本（事实核查）

> 研究型事实报告，服务于“复合材料参数化平台最低 CATIA 版本”决策。约定：CONFIRMED=有官方/一手来源；UNCERTAIN=推断或二手。

## 1) V5 发布年表与命名
R 系列：V5R18（2007-02）、R19（2008-08）、R20（2010-02）、R21（2011-07）；自 **V5-6R2012（R22，2012-04）** 起改为“年份+内部 R 号”命名，延续到 R23(2013)…R30(2020)…R36(2026)。
- **CONFIRMED**：R18/R19/R20/R21 日期（[HandWiki CATIA](https://handwiki.org/wiki/Software:CATIA)）。
- **CONFIRMED**：命名改为 V5-6R20xx，R22=2012（[3DS V5 兼容性新闻稿 2012-04](https://www.3ds.com/assets/invest/press-releases/DS_V2V_Compatibility_PR_final_180412.pdf)）。
- **最后一个 V5 版本**：V5 仍在按年出货，当前最新为 **V5-6R2026（R36，2026）**（[软服之家 R2026 发布](https://www.ruanfujia.com/post/11700153/)、[青腾 兼容表](https://tw-qingteng.com/CATIA_V5/catia-windows.html)）。⚠️旧资料（HandWiki）曾把“stable=6R2020”当作终止——**已过时**，UNCERTAIN 者勿用。
- **仍在支持/可获取（2024-2025）**：V5 采用滚动生命周期，实测口径为 **FULL≈GA+2 年 8 个月（固定在 10 月）+ EXTENDED +1 年**（[DS LifeCycle](https://www.keonys.com/wp-content/uploads/2020/01/DS_LifeCycleInformation.pdf)：如 R2018 GA 2018-02→Full 2020-10→Ext 2021-10）。据此：**2024 年内 R2021 及以后在保；2025 年内约 R2022 及以后在保**（R2021 约 2024-10 退出 EXTENDED）。逐版精确截止日需查 DS Program Directory（登录墙），**UNCERTAIN 精确日期**。

## 2) V5 Automation 对复合材料的暴露（决定性）
- **CONFIRMED（强证据）**：V5 Automation 类型库中复合材料**只有一个对象** `CompositesMaterial`；**无 Ply / Stacking / Zone / Rosette / Draping 对象**。证据：V5 Automation 参考的 `CAACompositesItf` 框架 Object Index **仅列 CompositesMaterial**（[CAACompositesItf](https://catiadesign.org/_doc/V5Automation/generated/interfaces/CAACompositesItf.htm)、[CompositesMaterial 接口](https://catiadesign.org/_doc/V5Automation/generated/interfaces/CAACompositesItf/interface_CompositesMaterial_32217.htm)）；pycatia（由 R28 全量 V5Automation 生成）`composites_interfaces` 也**只含** composites_material（[pycatia](https://pycatia.readthedocs.io/en/0.9.4/api/index_composites_interfaces.html)）。
- 该对象只有材料属性（CuredThickness/FabricWidth/MaterialType/…）+ CreateCompositesData，**不含铺层几何**。
- ⇒ **ply 几何/边界/铺覆在全部 V5 都是 GUI-only**，`Part.Update` 只能重算已建特征。
- `CompositesMaterial` 具体“R20 引入”这一年份 **UNCERTAIN**（未见 R20 What's New 一手确认）；但“仅此一个对象、无铺层对象”**CONFIRMED**。

## 3) 基础参数化自动化（很早就具备）
- **CONFIRMED**：`Part.Parameters.CreateReal/CreateDimension/CreateInteger/CreateString/CreateBoolean`、`Parameters.Item().Value`，`Part.Relations.CreateFormula/CreateDesignTable`，`Part.Update()` 均属基础/核心接口，早期 V5 即存在（接口版权页 ©2003 = R11 时代，[Parameters](https://catiadesign.org/_doc/V5Automation/generated/interfaces/KnowledgeInterfaces/interface_Parameters_26337.htm)、[Relations](https://catiadesign.org/_doc/V5Automation/generated/interfaces/KnowledgeInterfaces/interface_Relations_25383.htm)）。
- ⇒ **基础几何“参数→再生”的最低版本极低（R7~R11 级即可）**；具体最早 R 号 **UNCERTAIN**（未逐版验证），但远早于 R18。

## 4) Knowledgeware / 优化器自动化
- **CONFIRMED**：`Relations` 暴露 `CreateRule/CreateCheck/CreateLaw/CreateProgram(Reaction)/CreateSetOfEquations`（[Relations](https://catiadesign.org/_doc/V5Automation/generated/interfaces/KnowledgeInterfaces/interface_Relations_25383.htm)）——KWA 能力可自动化；`CreateRuleBase` 属 KWE；`Relations.Optimizations` 明确“仅当 PEO 许可可用”（同页）。
- 各能力首次可自动化的**具体 R 号 UNCERTAIN**；但可确认它们**都不涉及复合材料铺层**——**没有任何 V5 版本出现过铺层自动化对象**（同 §2）。

## 5) 位宽 / OS 支持
- **CONFIRMED**：R18 仅 `win_b64` 面向 **Windows XP Professional x64**（[V5R18 LPS](https://www.3ds.com/assets/Terms/LicensedProgramSpecifications/CATIA/CATIA_V5R18.pdf)，另见既有 R18 调研）。R18 文档仅列 XP/XP x64（无 Vista）。
- **CONFIRMED**：R19 起加入 **Windows Vista x86 / Vista x64**（[V5R19 LPS](https://www.3ds.com/assets/Terms/LicensedProgramSpecifications/CATIA/CATIA_V5R19.pdf)）；R20 支持 XP SP3、Vista x86/x64 32/64-bit（[V5R20 LPS](https://www.3ds.com/assets/Terms/LicensedProgramSpecifications/CATIA/CATIA_V5R20.pdf)）。
- **Win7/Win10/Win11 64-bit**：按青腾兼容表——R23(2013)起 32/64-bit 且支持 Win7/Win8；R24-R25 限 64-bit；**R26(2016)起支持 Win10 64-bit**；**R33(2023)起支持 Win11**（[青腾表](https://tw-qingteng.com/CATIA_V5/catia-windows.html)）。⚠️ Win7 起始版本（R21 还是 R23）**UNCERTAIN**（该表从 R23 起列）。

## 6) 实际最低版本
- **技术上最低**（仅基础参数化几何驱动）：极低，早期 V5 即可，但**不支持现代 OS/64-bit，且无铺层自动化**——不实用。
- **最低“务实/可获得”**：要 Win7 64-bit 稳定 + 现代 Automation，取 **V5-6R2012(R22)/R2013(R23)**；要 **Win10 64-bit** 取 **V5-6R2016(R26)**；要 **Win11+当前在支持** 取 **V5-6R2021(R31) 或更新的 R2023**。
- **关键约束**：无论哪个 V5 版本，**复合材料铺层都无法用 COM 参数化驱动**——这不是“版本不够”，而是 V5 根本不提供。

## Confirmed vs Uncertain
**CONFIRMED**：R18-R21 年表与 V5-6 命名；V5 现役最新=R2026（故“R2020 是最后一版”为旧误传）；V5 Automation 复合材料仅 CompositesMaterial、无 Ply/Stacking/Rosette/Draping；参数/公式/DesignTable/Update 为基础接口；KWA 关系对象可自动化、Optimizations 需 PEO；R18=XP x64、R19 加 Vista x64、R20 支持 XP/Vista 32+64。
**UNCERTAIN**：各版本精确支持截止日；CompositesMaterial“R20 引入”；Win7/Win10 精确起始 R 号（依二手兼容表）；KWA 各关系首次自动化 R 号。

## So what（复合材料参数化平台选型 4 点）
1. **不要把“最低 CATIA 版本”当铺层自动化的解**：V5 全系无 Ply/Rosette/Draping 自动化，靠 CATIA 驱动铺层在任何 V5 都走不通。
2. **铺层参数化请放在 CAE 侧（Abaqus/Python）做**，CATIA 仅作外形/工装几何来源——符合 ADR-0005。
3. 若必须留 CATIA 端参数化几何，**最低务实版 = R26(2016, Win10 x64)**；要 Win11+在保用 **R31(2021)/R33(2023)**，别用 R18–R20（XP/Vista 时代，已不可得）。
4. **基础“参数→Update”门槛极低**，与铺层无关；把工程逻辑与批量变体放外部脚本，用 `Parameters.Item().Value → Part.Update()` 即可，无需 KWA/PEO。
