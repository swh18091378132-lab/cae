> **已发布到 GitHub Issue：** swh18091378132-lab/cae#3（本文件为本地原文/交接要点，工单状态以 GitHub 为准）

# 03: 结构拓扑与自然格：由参数生成自然格 + 不连续格标记

**What to build:** 由参数集与模板生成本部件的**结构拓扑模型**（长桁线 × 肋线），在参数空间建拓扑再映射到物理空间，自动生成**自然格**并赋予稳定 ID；对长桁终止、蒙皮开口、部分肋等无法由四边连续长桁/肋围合的候选格，判为**不连续格**并说明原因。产出可入库的拓扑产物（自然格表 + 不连续格清单）。算法以已验证的原型（`prototype/natural-bay`）为来源。

**Blocked by:** 02

**Status:** done

## 交接要点（给干净窗口接手）

- **算法来源**：`prototype/natural-bay/gen_natural_bays.py` 已验证可行（一次性原型，参照思路但代码不直接搬）。核心做法：在**参数空间 (η=展向, ξ=弦向 0..1)** 建拓扑 —— 长桁 = 等 ξ 线（可只覆盖部分展长 → runout），肋 = 等 η 线（可只覆盖部分弦长 → partial rib）—— 再映射到物理空间。**不要从裸网格反推自然格。**
- **原型实测基线（必须对齐）**：展向 6 肋 / 弦向 7 长桁 → 5×6=30 个候选自然格；场景 A 干净=0 失效；B 长桁终止(中间 `xi=0.5` 只到 60% 展长)=**4 格失效**；C 开口(位于 `y=5.3, xi=0.42`)=**1 格失效**；D 部分肋(第 3 根肋只从 `xi=0.4` 延伸到后梁)=**6 格失效**。
- **连续性校验规则**：格的左/右边界长桁必须完整跨越该展向区间；下/上边界肋必须完整跨越该弦向区间；任一开口落在格内部即判不连续。任一不满足 → 不连续格并记录原因类型(runout / partial rib / cutout)。
- **稳定 ID 要求**：同参数集重复生成必须得到相同自然格 ID 与相同排序（建议 `NB-r{肋索引}-s{长桁索引}`，索引基于排序后的肋/长桁列表，不吃时间/随机）。
- **术语**：自然格 / 结构拓扑模型 / 参数契约 —— 见 `GLOSSARY.md`；并遵守 ADR-0008（自然格一等公民、不连续格排除自然格级但保留单元级）。
- **代码位置**：`apps/workbench/core/topology.py`（新模块），测试 `apps/workbench/tests/test_m2.py`，统一跑 `python run_tests.py`。
- **环境**：Python 3.8 兼容（不用 3.9+ 语法）；纯标准库即可（不需要 numpy，但用也行，1.24.4）；**不得依赖 CATIA/Abaqus**。
- **入库**：拓扑产物以 kind=`topology` 写入血缘，params 里带自然格数/不连续格数，content_uri 指向序列化 JSON。

## 完成情况

- 实现 `apps/workbench/core/topology.py`：`layout_from_params` / `build_bays` / `build_topology` / `TopologyModel`。
- 接入编排器 `run_study`：数模 → 拓扑产物，血缘 `catpart -> topology`。
- 测试 `apps/workbench/tests/test_m2.py`，并在 `test_m1.py` 中补了编排器级血缘断言。

- [x] 由参数集生成自然格，每格有稳定 ID（同参数集重复生成 ID 一致）
- [x] 四类场景回归：干净网格 / 长桁终止 / 开口 / 部分肋，自然格数与不连续格标记与本项目原型一致
- [x] 不连续格给出原因（runout / cutout / partial rib）
- [x] 拓扑产物（自然格表、不连续格清单）可作为产物入库并记血缘（参数集/数模 → 拓扑）
- [x] 纯算法模块可脱离 CATIA/Abaqus 独立单测

**回归实证（与原型逐格一致）**：
```
A_clean    total=30 valid=30 invalid=0  []
B_runout   total=30 valid=26 invalid=4  [NB-r3-s2, NB-r3-s3, NB-r4-s2, NB-r4-s3]
C_cutout   total=30 valid=29 invalid=1  [NB-r2-s2]
D_partial  total=30 valid=24 invalid=6  [NB-r1-s0, NB-r1-s1, NB-r1-s2, NB-r2-s0, NB-r2-s1, NB-r2-s2]
```
