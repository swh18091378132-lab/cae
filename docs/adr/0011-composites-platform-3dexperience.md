# 复合材料铺层的程序化驱动采用 3DEXPERIENCE (V6)，V5 与 CAA 不作为主线

**Status: proposed（待 GATE-1 验证后转 accepted）**

需求要求"连复合材料铺层也能程序化驱动"。经一手文档核查：**CATIA V5 任何版本的 Automation 类型库中复合材料仅有 `CompositesMaterial`（材料属性），不存在 Ply/Stacking/Zone/Rosette/Draping 对象**——V5 无法用 API 驱动铺层，绕行只能靠 Excel/ply 表导入或宏驱动 GUI，属脆弱变通，非平台契约。

**3DEXPERIENCE 的 Composites Design** 原生支持参数化铺层，并暴露 `CATCompPly`/`CATCompCore`/`CATCompCutPiece`/`CATCompSample` 等 Exposed Types；配合 **EKL + Knowledge Pattern**（V5 Knowledgeware 的超集）、Excel 导入、Composites Link 批处理（`ExportFileFormat=Abaqus/Nastran/Layup`），是**唯一有官方机制与文档支撑的产品化"参数化驱动铺层"路线**。CAA C++ 虽可行，但需 CAA Partner 协议/NDA + RADE 许可 + 逐版本重编译，成本最高。

故选定：**目标平台为 3DEXPERIENCE (V6) Composites Design + EKL**；V5 降级为"仅外形/工装几何来源"或历史兼容；CAA 仅在 3DEXPERIENCE 验证失败且必须离线自研时再评估。

## Considered Options

- **V5 + 宏/GUI 驱动**：天花板极低（无 ply API）且脆弱，否决。
- **V5 + CAA C++ 自研**：能力可行但 Partner 协议 + RADE + 长期重编译维护，成本与人才风险过高，不作为主线。
- **回退（铺层作 CAE 属性）**：无需铺层几何时可用，是 P1 现行基线；在用户明确要求 (b) 后不作为长期方案。

## Consequences

- **架构级变更**：本地工作台从"V5 COM 驱动"改为"3DEXPERIENCE + EKL/VB/Python 编排"，涉及新许可（Composites Design Role）、EKL 开发技能、平台迁移。
- **Abaqus 交付链**：仍须 Composites Link（`.layup` 或 `ExportFileFormat=Abaqus`）+ 可能需 Composites Modeler 做 mesh 映射，非原生命中。
- **两处必须 PoC 钉死（GATE-1）**：① EKL/Knowledge Pattern 能否 CreateOrModify `CATCompPly` 且保持 associativity；② `ExportFileFormat=Abaqus` 输出能否被现有 Abaqus 流程直接消费。二者缺公开样例，是首要风险。
- **不阻塞 P1**：P1 维持"铺层作 CAE 属性"，验证通过后再在 P2/P3 升级。
