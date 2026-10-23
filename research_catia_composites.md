# CATIA V5R18 复合材料模块事实核查（用于支撑 ADR-0005 / ADR-0004）

> 研究型事实报告。⚠️=UNCERTAIN。

## 1. 产品命名
- **CONFIRMED**：V5 时代为 **CPD = Composites Design 3**（P3，"CD3"）、**CPE = Composites Engineering (Design) 2**（P2）、**CPM = Composites Design for Manufacturing**。
- **UNCERTAIN**：**CPT / "Composites Part Design"** 在 V5 产品目录中无对应缩写（仅培训课程名）。

## 2. 授权
- **CONFIRMED**：CPD/CPE/CPM 均为**独立计费产品**，不含在 Part Design 内；CPD 为 P3 级、CPE/CPM 为 P2 级附加。
- 具体价格无可靠公开数据（UNCERTAIN）。

## 3. V5 Automation 能力（决定性）
- **CONFIRMED（强证据）**：V5 自动化类型库中复合材料对象**仅有 `CompositesMaterial`（R20 才加入）**，**不存在 Ply / Stacking / Zone / Rosette / Draping 对象**。pycatia（由 R28 全量 V5Automation 接口树生成）的 composites_interfaces 仅含 composites_material；V5R18/R19/R20 What's New 均未新增铺层类对象。
- ⇒ **R18 下铺层几何/边界/铺覆无法用 COM/VB 创建或参数化驱动，只能 GUI**；`Part.Update` 只能重算已建好的特征。

## 4. 与 Abaqus 衔接
- **CONFIRMED**：V5 自带仅 **IGES/DXF 铺层轮廓 + XML 堆叠信息**（材料/角度/rosette/2D&3D 轮廓离散点；XML 明写 "CATIA V5R18 - XML export of CPD data"），且该导出在 CPE/CPM 中提供；**V5 无原生 Abaqus/INP 导出**。
- 行业通行：Simulayt **Composites Link**（.layup）→ **Composites Modeler for Abaqus/CAE** 按单元重映射；Simulayt 2011 才被 DS 收购，R18(2007) 时代为第三方付费插件。
- ⇒ "须经 layup/XML 中转 + 单元重映射，非原生直连" **CONFIRMED**；"R18 具体插件版本矩阵" UNCERTAIN。

## 5. 痛点
- License 成本高（独立 P2/P3；具体价 UNCERTAIN）。
- Draping：R18 仅基础 producibility/fiber simulation；增强版（CFM/FEFlatten、seed curve、order of drape）文档注明 "V5R19 onwards"；高级 Simulayt 求解器 2011 后深度集成。
- ply book：R18 多处缺陷（APAR HD77224，R18SP4 复现，R20 才修）。
- R18 在区域/实体生成、制造同步、导出格式上均弱于后期版本。

## So what（(a) CAE 属性建铺层 vs (b) CATIA 复合材料建铺层）
- **License 依赖**：a 只需 Abaqus（+可选 Composites Modeler），零 CATIA 复材授权；b 必须新增 CPD/CPE（P2/P3），采购周期是硬门槛。
- **变更-重生成成本**：a 改铺层=改 inp/脚本，秒级、可 diff、易批量参数化；b 改铺层基本靠人手点 GUI，**R18 无 COM 自动化**，批量变体成本极高。
- **可追溯 / 移交保真**：b 铺层=真实几何，可出 ply book/展开图/激光投影，DMF 同源，追溯最好；a 只是单元属性。但移交保真：a 直接是 Abaqus 原生 section，零转换损失；b 要经 .layup/XML 中转并按单元重采样，有离散化/角度/厚度映射误差。

## 结论
- **团队无 CPD/CPE 授权且需频繁参数化/优化 → 选 (a)**（正是本平台情形，ADR-0005）。
- 若必须交付可制造几何/ply book/铺覆验证/设计-制造同源 → 才需 (b)，且须接受额外授权 + 手工建模 + Abaqus 侧仍需 Composites Link/Modeler 映射。
- ⚠️ 关键推论：即使 P2 想接入 CATIA 复合材料模块做"铺层几何"，**R18 无法用自动化驱动铺层**，只能手工 GUI —— 与平台"自动化/可追溯迭代"的定位冲突，故 (b) 在 R18 上不是可自动化路线。
