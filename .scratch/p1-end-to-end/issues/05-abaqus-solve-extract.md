> **已发布到 GitHub Issue：** swh18091378132-lab/cae#5（本文件为本地原文/交接要点，工单状态以 GitHub 为准）

# 05: Abaqus 求解与结果抽取（能力库 + Python 2.7 + manifest）

**What to build:** 平台用一组可组合的**能力库**函数驱动 Abaqus 运行指定分析（P1 首批：静力、模态、屈曲），把 ODB 结果抽取成中性的 CSV/JSON + manifest（element×ply×场量，manifest 记录来源 ODB 指纹、section point 口径、单位），入库并记血缘（inp → odb → 抽取）。所有 Abaqus 侧脚本须通过兼容性静态检查。

**Blocked by:** 04

**Status:** done

## ⚠️ 前提修正（实测取代假设）

原票基于 ADR-0003 的"Abaqus 6.13-4 / Python 2.6.2"假设。**2026-10-23 真机实测推翻**：

```
D:\abaqus2023\Commands\abaqus.bat information=release  -> Abaqus 2023
abaqus python -c "import sys; print(sys.version)"     -> 2.7.15
```

据此新增 **ADR-0012**（取代 ADR-0003 的版本结论），脚本基线改为 **Python 2.7**。`compat_check.py` 同步放宽：2.7 支持的 dict/set 推导、`print()` 函数式、`str.format` 不再拦截；仍拦截 3.x-only（f-string / yield from / nonlocal / 注解 / async / `[*a]` / `{**a}` / `1_000`）。

## 完成情况

实现：
- `abaqus_capabilities/extract.py` —— ODB 抽取器（在 Abaqus Python 2.7 内运行）：按 `CENTROID` 位置取 `S`/`E`，按 `sectionPoint`（铺层）区分，`U` 按节点；输出 CSV + manifest（来源 ODB sha256、Abaqus/Python 版本、step/frame、section points、口径、单位说明、时间戳）。
- `apps/workbench/core/abaqus_runner.py` —— 宿主侧 `AbaqusRunner` 协议 + `FakeAbaqusRunner`（确定性回归）+ `RealAbaqusRunner`（调 `abaqus job=... interactive` 与 `abaqus python extract.py`；自动探测 `D:\abaqus2023`）。
- `run_study` 接入 `with_solve`：血缘 `inp → odb → extraction`。

## 真机验证结果 ✅

```
A. fake runner ... OK
B. compat check ... OK
C. real abaqus ...
  [REAL] solve=COMPLETED, rows=156, section_points=[1, 6], py=2.7.15
```

- 求解：`Abaqus JOB ... COMPLETED`（许可证正常，5 tokens）
- ODB 读取：`odbAccess` OK，`S` 含 `mises`/`maxPrincipal`/`minPrincipal`，`U` 可取
- **铺层**：2 层 → section points `[1, 6]`，逐层提取成立（票 06 的数据基础）

## 真机暴露并修复的 bug

```
***ERROR: INVALID LOAD TYPE ON ELEMENT 1. LOAD TYPE P4 IS NOT VALID FOR ELEMENT TYPE S4R
```
S4R 壳单元面压必须用 `P`，`P4` 是实体单元面压。已在 `fe_build.py` 修正——**这类问题纯代码审查发现不了，必须真机跑**。

## 兼容性检查器实测

| 输入 | 结果 |
|---|---|
| f-string / 变量注解 / 返回注解 / `[*a]` / `1_000` | 5 项全部拦截 ✅ |
| dict 推导 / set 推导 / `print(a,b)` / `str.format` | 放行 ✅（2.7 合法）|

- [x] 能力库函数在 Abaqus 内置 Python 2.7 下可运行（静态检查 + 真机冒烟）
- [x] 能提交并完成一次静力求解，产出 ODB（真机 `COMPLETED`）
- [x] 抽取器把应力/位移/铺层结果写成 CSV/JSON + manifest
- [x] manifest 含来源 ODB 指纹、section point 口径、单位，可复现
- [x] 平台宿主与 Abaqus 之间仅交换文本（JSON/CSV），不传对象
- [x] 血缘 inp → odb → 抽取 正确；抽取产物入库

**未覆盖（P2）**：模态/屈曲的真实求解验证、历史量(historyOutputs)、Explicit。
