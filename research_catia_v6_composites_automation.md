# CATIA 3DEXPERIENCE (V6) 复合材料参数化铺层自动化 — 事实调研

调研时间：2026-10-23 ｜ 结论以官方帮助文档与一手 API 文档为主

---

## 1) 3DEXPERIENCE (V6) 的复合材料模块

**CONFIRMED**：3DEXPERIENCE 平台上的产品名为 **Composites Design**（配套 **Composites Manufacturing Preparation**），以"Role + App"形式交付，常见 Role 有 *Airframe Composites Designer*、*Consumer Products Composites Designer*、*Composites Designer for Industrial Equipment (CODFI)*、*Composites Structures Engineer* 等；V5 侧对应工作台是 **CPD (Composites Design)**。

支持能力（官方 Role 描述逐条列出）：完整 stacking 定义与管理、由 virtual stacking 生成 plies、ply by ply 与 grid/zone 两种设计法、preliminary grid & zone design、rosette 驱动的纤维方向、Dynamic Draping / fiber simulation、drop-off / splice / dart / no-splice zone、ply book。
- https://3dswym.3dexperience.3ds.com/wiki/catia-user-community/catia-consumer-products-composites-designer_YFKE1ZWjRJiHet7Igk_qcw
- https://help-3dexperience.aesvietnam.com/English/CpeUserMap/cpe-c-ov.htm
- https://help-3dexperience.aesvietnam.com/English/CpeUserMap/cpe-t-VSSA.htm

→ **参数化铺层定义（plies / stacking / zones / rosettes / draping）在 V6 是原生一等能力**，这一点与 V5 无本质差别（V5-6R2015 起增强同步进入 V5，见 Inceptra 2015 文章）。

## 2) 3DEXPERIENCE 上的自动化 / API

三层现实（**CONFIRMED**，均为官方文档）：

1. **Composites "Exposed Types"**：官方列出 `CATCompPly`、`CATCompCore`、`CATCompCutPiece`、`CATCompSample` 等类型及其属性（`Axis`=rosette、`Direction`、`Material`、`FatherPly`）。这说明 ply/core 特征**已作为可被知识语言访问的类型暴露**，而非纯 GUI 黑盒。
   https://help-3dexperience.aesvietnam.com/English/CpeUserMap/cpe-r-CPEexposedTypes.htm
2. **EKL + Knowledge Pattern**：官方文档明确 Knowledge Pattern 用标准 EKL 语言 + 生成式函数，可批量实例化/修改/删除模板与 datum、用 List 维护关联性、并入 part update。
   https://help-3dexperience.aesvietnam.com/English/KwaUserMap/kwa-c-KnowledgePatternObjectAbout.htm
3. **两层数据驱动入口（最实用）**：Composites Design 支持直接从 **Excel** 导入复合材料定义，以及 **Export/Import Ply Design Data**（ply 表）；Composites Link 另有 `CATSLTAnaTranBatchCore.exe` 批处理（无 GUI，读 stacking → 导出 layup）。
   https://3dswym.3dexperience.3ds.com/wiki/catia-user-community/success-path-airframe-composites-designer_8cf9KC5-ScOWWaMBiL8cmg
   https://help-3dexperience.aesvietnam.com/English/CpeUserMap/cpe-t-CCLBatchExport.htm

**UNCERTAIN**：是否存在与 V5 对等的、面向 ply 的 COM Automation 对象模型（如 `Part.Composites.Plies.Add(...)`）。公开文档中没有检索到；平台侧自动化主要被描述为 EKL/VB（新版含 Python）与 CAA，而不是 V5 式 COM 对象树。没有官方"能/不能"的明文断语。

## 3) 三条路线对比（能否真正程序化驱动铺层）

| 路线 | 能否驱动 ply | 成本/复杂度 |
|---|---|---|
| V5 Automation (COM/VBA/Python) | **不能直接驱动 ply**。V5 类型库中 composites 仅 `CompositesMaterial`（cured/uncured thickness、fabric width、material type 等材料属性），无 ply/stacking 对象。**CONFIRMED** | 极低，但天花板就在材料层 |
| V5 变通：CPD 的 Excel/Text 铺层表 + 宏 | 可"生成"铺层，但是以驱动 GUI 命令 / 导入 ply 表的方式，非 API 语义。2015 年有论文用 VB6.0 对 V5R19 CPD 二次开发做"铺层自动创建"。**CONFIRMED**（存在），稳定性/可维护性 **UNCERTAIN** | 低-中 |
| 3DEXPERIENCE：EKL/Knowledge Pattern + Exposed Types + Excel/ply 表 + Composites Link 批处理 | **可以**，是唯一有官方机制与官方文档支撑的"参数化驱动铺层"路线。**CONFIRMED**（机制存在），端到端工程可行性 **UNCERTAIN**（缺公开样例） | 中（需 Role 许可 + EKL 能力） |
| CAA C++ (V5 RADE / 3DE CAA) | **可以**，可定义自定义特征、新 Workbench、批处理。**CONFIRMED**（能力），但需 CAA Partner 协议/NDA 与 RADE 许可，接口随版本断裂需重编 | 最高 |

- V5 材料对象证据：https://pycatia.readthedocs.io/en/developement/api/pycatia/composites_interfaces/composites_material.html （`pycatia.composites_interfaces` 下仅此一个模块）
- CAA 门槛与能力：https://qdchenshi.com/en/blog/catia-development-guide-en
- V5 CPD 铺层表（Excel/Text）：http://www.mfcad.com/catia/1216/20582.html ；铺层自动创建论文：https://www.cnki.com.cn/Article/CJFDTotal-ZXDB201517044.htm

## 4) 导出到 Abaqus

**CONFIRMED**：没有"原生命中 Abaqus"的通道被文档证实。官方路径仍是 **Composites Link**：批处理 `ExportPlies` 读 part 的 ply stacking，按 tessellation 导出 layup，再由 **Composites Modeler for Abaqus/CAE** 映射到已有网格。
`ExportFileFormat` 合法值为 **`Layup` / `Abaqus` / `Nastran`**（默认 Layup）；`MeshFileFormat` 支持 `BDF, Layup, CDB, INP`；`ExportFileName` 扩展名由用户给定。即：既可直出 Abaqus 文本，也可走 `.Layup` 中间件。
https://help-3dexperience.aesvietnam.com/English/CpeUserMap/cpe-c-CCLBatchExport.htm
https://help-3dexperience.aesvietnam.com/English/CpeUserMap/cpe-t-CCLImportPlies.htm

**UNCERTAIN**：所谓 "CPD XML" 作为公开格式名未能证实；文档侧只出现 `.Layup` 与 `ExportFileFormat=Abaqus`。

## 5) EKL 与 V5 Knowledgeware

**CONFIRMED**：V5 的 Knowledgeware 语言体系是 M-EKL / C-EKL（Mathematical / Core EKL），用于 formulas、design tables、rules、checks、reactions；规则受"只操作参数、不改对象属性"的约束。3DEXPERIENCE 的 EKL 在此基础上叠加 Knowledge Pattern 的生成式函数与列表机制，并能访问 Composites 暴露类型（见第 2 节）——**能力上是 V5 Knowledgeware 的超集**。另外，3DE 侧 EKL 与 VB/Python 可互操作扩展。
- V5 C-EKL：https://www.maruf.ca/files/catiahelp/kwrug_C2/kwrugat5002.htm
- 3DE EKL 能力清单（建产品/PLM 对象、实例化模板、导出 Excel/XML、UI 创建）：https://www.atoz.com.cn/service/wenzhang328.html

**UNCERTAIN**：官方没有"EKL 可创建 ply 特征"的示例代码公开发布；只能从"Exposed Types + Knowledge Pattern 可实例化模板"推断可行。

---

## 结论速览

| 问题 | 判定 |
|---|---|
| V6 有 Composites Design 且支持参数化铺层 | CONFIRMED |
| V6 有可编程驱动铺层的机制（EKL/Exposed Types/Excel/批处理） | CONFIRMED（机制）／UNCERTAIN（工程可行性） |
| V5 Automation 无 ply/stacking 对象 | CONFIRMED（类型库仅 CompositesMaterial） |
| 3DE 原生导出 Abaqus | CONFIRMED 为 Composites Link + ExportFileFormat=Abaqus/Nastran/Layup，而非独立原生命中 |
| EKL 强于 V5 Knowledgeware | CONFIRMED（超集） |
| "CPD XML" 格式 | UNCERTAIN |

## So what（4 条）

- **V5（任何 release）都不足以"用 API 参数化驱动铺层"**：V5 Automation 的 composites 面只有 `CompositesMaterial`，ply/stacking 不在对象模型内；想绕，只能靠 Excel/Text 铺层表导入或宏驱动 GUI 命令，属于脆弱变通，不是平台契约。
- **要真正的"参数化驱动铺层"，必须换到 3DEXPERIENCE（EKL/Knowledge Pattern + Exposed Types + Excel/ply 表 + Composites Link 批处理）或上 CAA C++**；前者是产品化路线，后者是自建能力路线。
- **成本排序：V5 变通（低，但天花板低且不稳）< 3DE + EKL（中，需相应 Role 许可与 EKL 技能，官方机制齐全）<< CAA（高，Partner 协议/NDA + RADE 许可 + 版本重编译）**。若目标是长期产品，投入 3DE+EKL 的性价比高于在 V5 上堆宏。
- **落地前必须先做 PoC 钉死两件事**：① 在目标 release 上验证 EKL/Knowledge Pattern 能否 `CreateOrModify` 出 `CATCompPly` 并保持 associativity；② 验证 Composites Link 的 `ExportFileFormat=Abaqus` 输出能否被你们的 Abaqus 流程直接吃下（否则仍要 Composites Modeler 做 layup→mesh 映射）。这两点目前都缺公开证据，属于项目首要风险。
