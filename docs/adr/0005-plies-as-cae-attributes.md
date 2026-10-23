# 复合材料铺层作为 CAE 属性定义，不进入 CATIA 复合材料模块

平台以复合材料为主，但铺层（顺序/角度/厚度/层数）作为 **FE 截面/section 的 layup 属性**在 CAE 侧管理，CATIA 只负责外形壳面几何。这样"改铺层"完全不触发几何再生、不依赖 CATIA 复合材料模块(CPD/CPT/CPE)的额外许可，迭代最快、追溯最清晰。将来若需出正式铺层几何与 ply book，再单独评估是否引入 CATIA 复合材料模块。

## Consequences

- 改铺层 = 改属性表，不碰 CATIA；改外形 = 改参数+再生 CATIA。两条路解耦。
- 不做铺层几何/铺覆(draping)的真几何表达；可制造性(ply book)不在 P1 范围。

## Considered Options

- 铺层在 CATIA 复合材料模块中作为 ply 几何定义再由 CAE 继承：几何真实性更强，但引入额外许可、再生代价与铺层数据交接风险，故不选为 P1 主线。

## 补充事实（R18 自动化核查）

经核查：V5 自动化类型库中复合材料对象**仅有 `CompositesMaterial`（R20 才加入）**，**没有 Ply/Stacking/Zone/Rosette/Draping 对象**；V5R18/R19 What's New 均无铺层类对象。因此 **R18 下铺层几何只能 GUI 手工建模，无法用 COM/VB 参数化驱动**；`Part.Update` 只能重算已建好的特征。且 V5 无原生 Abaqus 导出，仅 IGES/DXF/XML，Abaqus 侧须经 Simulayt Composites Link(.layup) + Composites Modeler 按单元重映射。

**推论**：即使将来采购 CPD/CPE 许可，R18 的铺层路线**仍然无法自动化**，与平台"自动化、可追溯迭代"的定位冲突。故若确需铺层几何/ply book/可制造性，正确做法是**升级 CATIA 版本或改用支持自动化的工具链**，而非在 R18 上手工建模。(a) 因此不只是"P1 权宜"，而是 R18 时代最合理的长期路线。
