# 04: 有限元模型构建：几何抽象 + 网格（与拓扑对齐）+ 铺层（CAE 属性）+ 载荷/边界

**What to build:** 由拓扑与参数集构建一个可求解的有限元模型：几何抽象成分析用曲面/体；网格沿长桁线与肋线切分、与自然格对齐（跨自然格边界的不一致单元不得作为交付网格）；复合材料**铺层**（顺序/角度/厚度/材料）作为 FE 截面属性定义；施加指定载荷与边界条件。产出 .inp 并记入血缘（拓扑 → 网格 → inp）。

**Blocked by:** 03

**Status:** done

## 完成情况

实现：
- `apps/workbench/core/meshgen.py` —— **逐自然格划分 + 相邻格共边复用节点**，因此结构上不可能产生跨自然格边界的单元（ADR-0010）；`verify_no_straddle()` 做防御性校验。
- `abaqus_capabilities/fe_build.py` —— 铺层作为 `*SHELL SECTION, COMPOSITE` 截面属性（ADR-0005）；`*ELASTIC, TYPE=LAMINA` 材料；按自然格输出 `*ELSET` 便于逐格校核；载荷/边界/输出请求。
- `abaqus_capabilities/compat_check.py` —— **Python 2.6.2 兼容静态检查器**（f-string / dict·set 推导 / yield from / nonlocal / 变量注解 / print 多参数），AST 实现，可作为后续票 05 的门槛。
- `apps/workbench/core/fe_model.py` —— 编排侧封装：拓扑 → 网格 → .inp。
- `run_study` 接入 `with_fe_model`，血缘 `topology → mesh → inp`。

测试：`apps/workbench/tests/test_m3.py`（网格对齐、单元数、节点共用去重、铺层厚度、.inp 关键卡片、2.6.2 兼容、编排器血缘链）。

- [x] 由拓扑生成网格，网格边界与长桁/肋线一致（跨界单元为 0）
- [x] 铺层作为截面属性定义，可参数化修改且不改动几何
- [x] 载荷与边界条件按工况施加
- [x] 产出 .inp 作为产物入库，血缘 拓扑→网格→inp 正确
- [x] FE 构建脚本在 Abaqus 内置 Python 2.6.2 语法下可运行（通过兼容性静态检查）

**实测**：30 个自然格 × 2×2 = 120 单元；节点共边去重生效；`*SHELL SECTION, COMPOSITE` 铺层行数 = 铺层数；`compat_check` 通过。

**待现场**：以真实 Abaqus 6.13-4 读入该 .inp 并确认可求解（本环境无 Abaqus）。
