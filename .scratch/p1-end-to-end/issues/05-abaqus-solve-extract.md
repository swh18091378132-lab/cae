# 05: Abaqus 求解与结果抽取（能力库 + 2.6.2 兼容门槛）

**What to build:** 平台用一组可组合的**能力库**函数（几何前处理/网格/材料铺层/载荷边界/求解/抽取）驱动 Abaqus 6.13-4 运行指定分析（P1 首批：静力、模态、屈曲），把 ODB 结果抽取成中性的 CSV/JSON + manifest（element×ply×场量，manifest 记录来源 ODB 指纹、section point 口径、单位），入库并记血缘（inp → odb → 抽取）。所有 Abaqus 侧脚本必须通过 Python 2.6.2 兼容静态检查。

**Blocked by:** 04

**Status:** ready-for-agent

- [ ] 能力库函数在 Abaqus 内置 Python 2.6.2 下可运行（通过静态兼容检查 + 冒烟）
- [ ] 能提交并完成一次静力/模态/屈曲求解，产出 ODB
- [ ] 抽取器把应力/位移/铺层结果写成 CSV/JSON + manifest
- [ ] manifest 含来源 ODB 指纹、section point 口径、单位，可复现
- [ ] 平台宿主与 Abaqus 之间仅交换文本（JSON/CSV），不传对象
- [ ] 血缘 inp → odb → 抽取 正确；抽取产物入库
