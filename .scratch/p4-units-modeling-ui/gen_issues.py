# -*- coding: utf-8 -*-
"""生成 P4 的票文件到 .scratch/p4-units-modeling-ui/issues/。"""
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "issues")
if not os.path.isdir(OUT):
    os.makedirs(OUT)

SPEC = "spec #30（.scratch/p4-units-modeling-ui/spec.md）"

TICKETS = [
    dict(num=31, title="units —— 单位换算的唯一权威实现（纯算法）", blocked=[], scopes=[
        "apps/workbench/core/units.py", "apps/workbench/tests/test_m31.py"], body="""
**spec**：%s §实现决策/单位制。

## 背景

平台从来没有定义过基准单位制，结果 `.inp` 里节点坐标用米、壳厚用毫米、模量用 MPa、
密度用 kg/m³，四套基准混在一个模型里（#29）。更关键的是：**没有任何测试断言过量纲**，
所以这个问题从 P1 活到 P3。

本票只做一件事：**把换算做成唯一的权威实现**，先不动其它模块。
这样后续每一处改动都有同一个参照物，不会各写一份慢慢漂移。

## What to build

新建 `core/units.py`（纯算法、零第三方依赖、不 import 任何 core 模块）：

- **基准单位制固定为 `mm-N-MPa-t`**：长度 mm、力 N、应力/模量 MPa、密度 t/mm³、质量 t。
- **预设单位制表**，每套必须**同时**声明五个换算因子：length / force / stress /
  density / mass。至少三套：
  - `mm-N-MPa-t`（默认）
  - `m-N-Pa-kg`（SI 自洽）
  - `in-lbf-psi-slinch`（英制自洽，用户可选，非默认）
- **双向换算**：`to_display(value, quantity, system)` / `to_base(value, quantity, system)`。
- **带单位格式化**：把基准值渲染成"数值 + 单位"的字符串；数值精度要稳定（同值同文本）。
- **预设校验**：一套预设缺任何一个因子就**必须报错**，不允许"部分覆盖"。
  这条是本票的核心防线 —— "只改其中一个"正是 #29 的形态。
- **量的种类**（quantity）至少覆盖：length / area / volume / force / stress / density /
  mass / angle / dimensionless。角度与无量纲**不换算**（恒等）。
- 未知的 quantity 或未知的 system 要抛明确错误，**绝不静默返回原值**
  （静默透传会让一个单位 bug 一路流到报告里）。

## Acceptance criteria

- [ ] 基准↔显示**往返换算回到原值**（各量、各预设，含极小/极大值）
- [ ] 同一物理量在三个预设下换算后再换回，误差在浮点容差内
- [ ] 每套预设五个因子齐全；**构造一个缺因子的预设，断言被拒绝**
- [ ] 角度与无量纲不换算（恒等），且不因预设不同而变
- [ ] 未知 quantity / 未知 system 抛错而非静默透传
- [ ] 格式化同值同文本（确定性）；密度与质量的显示可读（不出现 1.6e-09 这种）
- [ ] 纯算法：不 import core 其它模块、不依赖 CATIA/Abaqus/DB、不 import numpy
- [ ] Python 3.8 兼容（`compat_py38`）

## 为什么先做这张

它是后面所有票的参照物。而且它**不改变任何现有行为**（还没有模块用它），
所以可以独立验证、独立合并，风险最低。
"""),

    dict(num=36, title="CATIA 能力探测（安装 / COM 注册 / 许可服务器）", blocked=[], scopes=[
        "apps/workbench/core/catia_probe.py", "apps/workbench/main.py",
        "apps/workbench/tests/test_m36.py"], body="""
**spec**：%s §实现决策/CATIA 许可证。

## 背景

CATIA 的自动化路径在本机走不通，但**失败原因混在一起**：是没装？COM 没注册？
许可服务器不可达？还是没有授权？这四种的处置完全不同，而 ADR-0013 目前只能靠人肉排查。

本票把它变成**一条命令说清楚**：许可证到位那天，跑一次就知道好了没有，
而不是重新把排查流程走一遍。

## What to build

新建 `core/catia_probe.py` + CLI 子命令（`main.py` 加一个动作，不改既有命令语义）：

探测并按固定顺序报告**每一步的结论与原因**：

1. **安装**：CATIA V5 安装目录与 `CNEXT.exe` 是否存在（从注册表
   `HKLM\\SOFTWARE\\Dassault Systemes\\B32\\0` 读 `DEST_FOLDER`，退化到常见路径）
2. **COM 注册**：`HKCR\\CATIA.Application`（含 `WOW6432Node` / `HKCU`）是否存在
3. **许可服务器可达**：DSLS 默认端口 4084/4085 是否可连；环境变量
   `DSY_LICENSE_SERVER` / `DSY_LicenseServer` 指向的主机是否可达
4. **能否 `Dispatch('CATIA.Application')`**：真正建一个 COM 对象（有 pywin32 时才做）
5. **许可文件**：常见位置是否存在 nodelock / `.lic`（并**区分** CATIA V5 与
   CATIA Composer —— 后者是另一个产品，不能用于本用途）

输出既要有**人读**的一段结论（"当前卡在第 3 步：许可服务器不可达"），
也要有**机读**的结构化结果（每步 `ok` / `reason`），供服务层与测试消费。

## Acceptance criteria

- [ ] 一条命令输出五步结论 + 每步失败原因，且给出**第一个卡住的环节**
- [ ] 结构化结果可被代码消费（每步 `{step, ok, detail}`），人读结论由它派生
- [ ] 明确**区分**"未安装 / COM 未注册 / 许可服务器不可达 / 无授权文件"
      （构造假路径与假端口断言四类结论各自可达）
- [ ] 识别到 `CATIAComposer` 的 `.lic` 时**不得**判为 CATIA V5 可用
- [ ] 不安装任何依赖、不尝试绕过许可校验、不修改系统配置（只读探测）
- [ ] 无 pywin32 / 无 CATIA 的环境下**不崩**，只是该步报不可用
- [ ] 测试用注入的假文件系统/假端口探针，不依赖真机状态（真机结果另行记入报告）
- [ ] Python 3.8 兼容（`compat_py38`）；`main.py` 既有 CLI 行为零回归

## 无阻塞

不依赖单位制与建模，可**与票31 并行开工**。
"""),

    dict(num=32, title="全链路改基准 mm-N-MPa-t（模板量纲 → 拓扑/网格 → .inp → 抽取）", blocked=[31], scopes=[
        "apps/workbench/core/template_registry.py", "apps/workbench/core/topology.py",
        "apps/workbench/core/meshgen.py", "apps/workbench/core/fe_model.py",
        "abaqus_capabilities/fe_build.py", "abaqus_capabilities/extract.py",
        "data/templates/", "apps/workbench/tests/test_m32.py"], body="""
**spec**：%s §实现决策/单位制 与 §#29 的关闭。

## 背景

这是 **#29 的核心修复**，也是全 P4 风险最高的一张：它会让基线数字变化。
关键决策（已在 spec 定死）：

- **基准单位制 = mm-N-MPa-t**。拓扑/网格/FE 全部只认基准单位。
- **模板参数契约声明"量的种类"**（length/angle/dimensionless），不再只靠单位串猜。
  模板里写的数值按模板声明的单位书写，**加载时统一换算为基准单位**。
- **参数集产物一律存基准单位**（血缘里的数无歧义）。

## What to build

- `template_registry`：参数契约增 `quantity` 字段（与既有 `unit` 并存，`quantity` 才是语义）；
  加载/校验时把模板声明的数值换算为基准单位；未知 quantity 或无法换算要报错。
  `data/templates/*.json` 补 `quantity`（长/角度/无量纲三类）。
- `topology` / `meshgen` / `fe_model`：**只接受基准单位**（米制输入不再出现）。
  `extents()` 等返回的 `y_unit` / `xi_unit` 必须与实际一致（mm）。
- `abaqus_capabilities/fe_build.py`：写 `.inp` 时**四者同制** ——
  节点坐标 mm、`*SHELL SECTION` 厚度 mm、`*ELASTIC` 模量 MPa、
  `*DENSITY` 密度 **t/mm³**。当前密度直接写 1600（kg/m³）是错的。
- `abaqus_capabilities/extract.py`：manifest 的 `units_note` **由 units 模块生成**，
  不允许硬编码字符串（现在写的是"P1 使用 mm-N-MPa"，与实际不符）。
- Abaqus 侧脚本仍须 **Python 2.7 兼容**（`compat_check.py`）。

## Acceptance criteria

- [ ] 模板加载后参数值是**基准单位**（length 类参数默认值 ×1000 后可观测）
- [ ] 参数契约能表达量纲种类；未知/不可换算时明确报错
- [ ] `.inp` 的坐标、壳厚、模量、密度**四者同制**（断言：展长 12 m ⇒ 节点最大 Y = 12000；
      厚度仍是 0.1（mm）；`*DENSITY` 写出 1.6e-9 而非 1600）
- [ ] `extract` manifest 的 `units_note` 与实际基准一致，且不是硬编码
- [ ] 拓扑/网格/FE 不再假设米（构造一个只有基准单位输入才能通过的用例）
- [ ] `compat_check.py` 仍 OK（Abaqus 侧 Py2.7）
- [ ] Python 3.8 兼容
- [ ] **基线变化的每一条都在提交信息里写明「原值 → 新值 → 为什么」**，
      不允许"改到绿为止"（涉及 test_m12 的弦长断言、真机 FI/MS 数值）

## 已知会变的基线（spec 已预告，别当回归）

坐标 ×1000、密度 1600 → 1.6e-9、真机 FI/MS 数值变化（之前几何与厚度不同制，旧值本就不可信）。
"""),

    dict(num=33, title="显示换算：报告 / 质量口径 / 优化汇总 + 单位偏好", blocked=[31], scopes=[
        "apps/workbench/core/report.py", "apps/workbench/core/report_template.py",
        "apps/workbench/core/workbench_service.py", "apps/workbench/core/objectives.py",
        "apps/workbench/core/db.py", "apps/workbench/tests/test_m33.py"], body="""
**spec**：%s §实现决策/单位制 与 用户故事 11/12/13。

## 背景

换算做出来了（票31），链路统一了（票32），但**用户看到的还是裸数字**：
报告里写死 `MPa`，质量默认按 kg 显示却没有口径说明，优化汇总是原始值。

本票把所有**面向人的输出**接到显示单位制上，并且让偏好能存下来。

## What to build

- **单位偏好持久化**：当前显示单位制存库（走既有幂等迁移），服务层 get/set；
  默认 `mm-N-MPa-t`。切换只改偏好，**不改任何落库的物理值**。
- **报告**：每个物理量带单位；显式写明"基准单位制 / 显示单位制"；
  模板占位符与章节相应调整（`TEMPLATE_VERSION` 递增，否则老模板永远不重建）。
- **质量口径**：显示为"数值 + 单位（含哪些部件）"，沿用 P3 的 `components` 口径；
  **未声明口径仍要显式标注**（那是 P3 已建立的诚实边界，不能退回去）。
- **优化汇总摘要**：目标值按显示单位渲染，但**比较仍按基准单位**（换单位不得改变谁最优）。
- 所有格式化都调 `units`，**不允许任何模块自己乘除**。

## Acceptance criteria

- [ ] 单位偏好可存取、可切换，默认 `mm-N-MPa-t`
- [ ] **切换显示单位后，同一 Study 的物理值与"谁最优"不变**（只有显示文本变）
- [ ] 报告里每个物理量带单位，且写明基准/显示两套单位制
- [ ] 质量显示带口径（含哪些部件）；未声明口径仍显式标注
- [ ] 优化汇总显示换算正确，最优点不因换单位而变
- [ ] 模板版本递增，老模板能被重建（沿用 P2 的 `template_needs_rebuild` 机制）
- [ ] 全模块无自行乘除的换算（用 AST 或 grep 断言 `1e-3`/`1e6` 之类的裸因子不出现）
- [ ] Python 3.8 兼容；P2/P3 报告与汇总相关测试零回归（除基线数值）

## 阻塞

依赖票31（units 模块）。不依赖票32，但合并顺序上在 32 之后更安全。
"""),

    dict(num=34, title="units_version —— 旧口径标记与幂等迁移", blocked=[32], scopes=[
        "apps/workbench/core/db.py", "apps/workbench/core/run_study.py",
        "apps/workbench/core/workbench_service.py", "apps/workbench/tests/test_m34.py"], body="""
**spec**：%s §实现决策/#29 的关闭与历史数据。

## 背景

票32 之后，新算出来的结果和旧结果**不是同一个物理口径**。旧结果不该被悄悄当成可比数
（它本来就是错的：几何与厚度不同制）。但也不能删——历史血缘必须还在。

## What to build

- 新增**单位口径版本** `units_version`：新 Study 记 2（自洽口径），旧 Study 视为 1（历史口径）。
- 落库走既有 `MIGRATIONS` 幂等新增，**不改既有结构**，P1/P2/P3 老库打开后仍可查。
- 查询/展示层：旧口径 Study 要能被**识别出来并显式标注**，
  其 FI/MS 标注为**不可与新结果直接比较**。
- **不做事后换算** —— 旧结果的物理意义本身不可信，换算是伪造精度（spec 明确规定）。
- 报告里若引用旧口径结果，必须带同样的标注。

## Acceptance criteria

- [ ] 新 Study 记 `units_version=2`；旧库升级后既有 Study 记 1 且数据完整
- [ ] 迁移幂等：重复打开旧库不报错、不重复加列
- [ ] 旧口径 Study 可被查询识别，并有显式标注（不是靠人猜）
- [ ] **不对旧结果做任何换算**（构造一个旧 Study，断言其 FI/MS 原封不动）
- [ ] 报告/汇总引用旧口径结果时带标注
- [ ] P1/P2/P3 既有数据可读、血缘可查（构造旧库断言）
- [ ] Python 3.8 兼容

## 阻塞

依赖票32（先有正确的口径，才谈得上传记口径）。
"""),

    dict(num=35, title="量纲自洽测试套件（跨边界，专治 #29 这类问题）", blocked=[32, 34], scopes=[
        "apps/workbench/tests/test_m35.py"], body="""
**spec**：%s §测试决策 与用户故事 18。

## 背景

#29 能活过 P1/P2/P3，根本原因是**没有任何测试断言过量纲**。
本票补的就是这道防线，而且要求它是**跨边界**的——单个模块自洽不等于链路自洽。

## What to build

一个专门的测试文件（`test_m35.py`），沿数据流逐段断言单位：

1. **模板 → 参数集**：模板声明的单位被换算成基准单位，落库值是基准单位
2. **参数集 → 拓扑/网格**：`extents()` 的单位标注与实际数值一致（mm）
3. **网格 → `.inp`**：坐标 / 壳厚 / 模量 / 密度**四者同制**
   （用可观测的物理关系断言，如"展长 12 m ⇒ 节点最大 Y = 12000"）
4. **`.inp` → 抽取 manifest**：`units_note` 与基准一致，且由 units 模块生成
5. **校核 → 报告**：每个物理量带单位；质量带口径
6. **优化汇总**：显示换算正确、比较仍按基准

外加两条"元测试"（防的是同一类错误再次发生）：

- **不允许裸换算因子**：扫描 `core/` 与 `abaqus_capabilities/`，
  禁止出现 `1e-3` / `1e6` / `÷1000` 之类的裸因子做单位换算
  （必须是 `units` 的调用）；允许清单要写清楚为什么允许。
- **不允许硬编码单位串**：报告/manifest/汇总里不得出现写死的 `MPa`/`kg/m3` 之类，
  必须由 units 生成。

## Acceptance criteria

- [ ] 六段边界各自有断言，且**失败信息能指出是哪一段不一致**（不是笼统"数值不对"）
- [ ] 两条元测试生效：人为插入一处裸因子/硬编码单位串，断言被抓住
- [ ] 测试只用可观测行为，不断言内部函数调用顺序
- [ ] 单测不依赖真机 Abaqus（真机段单独 SKIP/探测）
- [ ] Python 3.8 兼容

## 阻塞

依赖票32、票34（要断言的口径与版本标记都已就位）。
"""),

    dict(num=37, title="parametric_model —— 中性几何/特征模型 + 投影数学（纯算法）", blocked=[31], scopes=[
        "apps/workbench/core/parametric_model.py", "apps/workbench/tests/test_m37.py"], body="""
**spec**：%s §实现决策/参数化建模。

## 背景

平台最核心的"参数化建模"能力目前是**空的**：真实环境里 `catia.regenerate()` 一步都跑不了
（V5 无复材铺层 Automation + 无许可证）。本票把几何真源搬进平台内。

## What to build

新建 `core/parametric_model.py`（纯算法、零第三方依赖）：

- 由 **模板 + 基准单位参数集 + 结构拓扑模型** 生成**中性的几何/特征表示**：
  点/线/面/体的最小集合 + **特征层级**（模板 → 外形面 → 梁/肋/框 → 开口 → 铺层）
  + 属性。不依赖任何 CAD 内核。
- **复用既有的结构拓扑模型作为骨架来源**（自然格、边界、开口位置都已经在那里），
  **不重复发明拓扑**。
- 必须能表达**两类布局**：翼面类（长桁×肋）、筒段类（桁条×框），以及开口。
- **自带铺层定义与材料**（作为属性，不生成铺层几何；ADR-0005 不变）。
- **确定性**：同参数集两次生成**逐字节一致**（稳定 ID、稳定排序、固定精度）。
- 附带**投影数学**（供图形区用）：三维点 → 二维屏幕坐标，支持绕轴旋转/平移/缩放，
  正交投影即可。纯函数、可单测。

## Acceptance criteria

- [ ] 两类布局都能生成，且特征层级与参数一致（改 `n_ribs` 则肋的数量与层级同步变）
- [ ] 开口被正确表达（位置/尺寸随参数变）
- [ ] 模型自带材料与铺层属性，且内容可追溯回参数集
- [ ] **确定性**：同参数两次生成逐字节一致（含 ID 与排序）
- [ ] 几何是**基准单位**（mm）；不出现米制尺度残留
- [ ] 投影数学：正交投影正确（构造已知点断言屏幕坐标）；旋转/平移/缩放可组合
- [ ] 纯算法：不 import CATIA/Abaqus/DB，不 import numpy
- [ ] Python 3.8 兼容（`compat_py38`）

## 阻塞

依赖票31（要有 units 才能确定"几何用 mm"并用统一格式化/校验）。
"""),

    dict(num=38, title="几何写入器：STEP / IGES + 建模信息 sidecar", blocked=[37], scopes=[
        "apps/workbench/core/geometry_writers.py", "apps/workbench/tests/test_m38.py"], body="""
**spec**：%s §实现决策/参数化建模 与用户故事 24/25/28/29。

## What to build

新建 `core/geometry_writers.py`：

- **写入器协议**（可插拔，加格式不改建模逻辑）：`write(model, out_path) -> 结果`。
- **STEP 写入器**：平台自己写（纯文本生成，无第三方依赖）。
- **IGES 写入器**：同上。
- **建模信息 sidecar**：中性几何装不下的信息（材料、铺层、参数集、特征树摘要、单位制）
  写成同名 sidecar（JSON），随几何一起交付。
- 输出要**确定性**（同模型同字节），并且能被一个**独立解析器**读回关键实体。

## Acceptance criteria

- [ ] STEP / IGES 各自产出合法文本，且能被测试里的**独立解析器**读回关键实体
      （不是"文件非空"就算过）
- [ ] sidecar 含材料 / 铺层 / 参数集 / 特征树摘要 / 单位制
- [ ] 确定性：同模型两次写出逐字节一致
- [ ] 写入器可插拔：新增一个假格式不需要改 `parametric_model`
- [ ] 单位标注正确（几何按 mm 输出，sidecar 声明单位制）
- [ ] 纯标准库、Python 3.8 兼容

## 说明

**不要**试图写 CATPart（DS 私有二进制格式，没有内核写不出合法文件）——
那是票39 的 CATIA 发布路径。
"""),

    dict(num=39, title="CATIA 发布适配器：CATPart / CATProduct（不可用时显式 SKIP）", blocked=[36, 38], scopes=[
        "apps/workbench/core/catia_driver.py", "apps/workbench/core/catia_publish.py",
        "apps/workbench/tests/test_m39.py"], body="""
**spec**：%s §实现决策/CATIA 许可证 与用户故事 26/27。

## 背景

平台**不写** CATPart 二进制（DS 私有格式，没内核写不出合法文件）。
正确做法是：我们把中性几何导出去，**让 CATIA 自己打开并另存为原生格式**。

## What to build

- `catia_publish`：用票36 的探测结果决定能否发布；
  能则经 V5 Automation 打开 STEP/IGES → 另存 CATPart → 组装 CATProduct；
  不能则返回**结构化的不可用结果**（原因来自探测的哪一步、缺什么）。
- 与既有 `CatiaDriver` 的 fake/real 三件套惯例保持一致。
- **绝不生成占位文件冒充成功**。`FakeCatiaDriver` 的 `.CATPart` 只允许出现在测试临时目录，
  且文件名/元数据必须可与真实交付物区分。

## Acceptance criteria

- [ ] 探测不可用时返回 `unavailable` + 具体原因（不是抛无意义异常、不是静默跳过）
- [ ] **断言此时没有产出任何 `.CATPart` / `.CATProduct` 文件**
- [ ] 探测可用时（用假 CATIA 适配器模拟）走通发布路径：CATPart 产出 + CATProduct 组装
- [ ] fake 产物与真实交付物可区分（命名/元数据）
- [ ] sidecar 随几何一起传给发布步骤，材料与铺层信息不丢
- [ ] 测试不依赖真机 CATIA；真机结果另行记录
- [ ] Python 3.8 兼容；Abaqus 侧不受影响

## 阻塞

依赖票36（探测）与票38（要导出的中性几何）。
"""),

    dict(num=40, title="run_study 接入内置建模（复用 CatiaDriver.regenerate 切口）", blocked=[32, 37, 38], scopes=[
        "apps/workbench/core/catia_driver.py", "apps/workbench/core/run_study.py",
        "apps/workbench/tests/test_m40.py"], body="""
**spec**：%s §实现决策/参数化建模（复用既有切口）。

## 背景

`run_study` 是全平台唯一的编排入口，**不能改它的签名**（P1–P3 的纪律）。
建模这一步的切口是 `CatiaDriver.regenerate(parameter_set, template)` ——
本票提供一个"平台内建模"的实现来填同一个位置，于是 `run_study` 一行不改。

## What to build

- 新增"内置建模"适配器，实现既有 `regenerate` 契约：调 `parametric_model` 生成模型，
  落盘为中性几何（+ sidecar），返回与原来同结构的数模产物（路径 + 指纹 + 参数）。
- 数模产物要能区分**产出方式**（内置建模 / CATIA 再生 / 复用），血缘里要看得出。
- 既有 `reuse_catpart_from`（纯铺层轮次跳过再生）**必须继续有效**：
  内置建模下"几何没变就复用"仍然成立，且能断言**建模调用次数为 0**。
- 默认行为保持向后兼容：不显式指定内置建模时，既有路径不变。

## Acceptance criteria

- [ ] `run_study` 签名逐字节不变；不指定新适配器时行为与之前完全一致
- [ ] 指定内置建模后，全链路跑通（建模 → 网格 → 求解(fake) → 校核 → 报告）
- [ ] 数模产物可区分产出方式（内置 / CATIA / 复用），血缘可见
- [ ] **纯铺层轮次下建模调用次数为 0**（用会计数的假适配器断言）
- [ ] 建模产物按基准单位（mm），与票32 的链路一致
- [ ] P1–P3 既有测试零回归
- [ ] Python 3.8 兼容

## 阻塞

依赖票32（基准单位）、票37（参数化模型）、票38（写出器）。
"""),

    dict(num=41, title="模块化外壳：标题栏 / 菜单栏 / 工具栏 / 上下文栏 / 图形区 / 状态栏", blocked=[31, 37], scopes=[
        "apps/workbench/ui_workbench.py", "apps/workbench/ui_shell.py",
        "apps/workbench/core/workbench_service.py", "apps/workbench/tests/test_m41.py"], body="""
**spec**：%s §实现决策/UI 模块化 与用户故事 33–39/44.

## 背景

现在的界面是 Notebook 里几张页签，没有图形区、没有菜单、没有工具栏。
工程师看不出模型长什么样，只能读 JSON。

## What to build

按 CATIA / Abaqus 的公认布局组织（**布局写成代码里的声明式配置**，
不去抓这两个软件的界面——CATIA 无许可证、Abaqus GUI 无法稳定自动化）：

- **标题栏**：当前项目 / 型号 / Study
- **菜单栏**：文件 / 编辑 / 视图 / 模型 / 分析 / 优化 / 审批 / 帮助
- **工具栏**：高频操作（新建、打开、运行、单位切换、视图复位）
- **上下文栏**：随当前焦点变化（选中部件 → 部件动作；选中结果 → 结果动作）
- **图形区**：Canvas 线框投影（用票37 的投影数学），支持复位
- **消息区**：运行日志与警告
- **状态栏**：基准/显示单位制、fake/real 模式、忙碌状态
- **单位切换按钮**：从预设列表选（三套），走票33 偏好的服务层，**真的换算显示值**

## Acceptance criteria

- [ ] 六个区域 + 单位切换按钮都在，且构造/销毁可冒烟（无显示环境 SKIP）
- [ ] 图形区能画出参数化模型的线框（不是空白）
- [ ] 单位切换按钮改变显示值（服务层断言），且不改物理值
- [ ] 状态栏反映基准/显示单位制与 fake/real 模式
- [ ] 菜单/工具栏动作走服务层（UI 不含逻辑）
- [ ] P3 的 UI 冒烟测试与逻辑不变（新外壳不破坏既有断言）
- [ ] Python 3.8 / Tkinter 可用（无新依赖）

## 阻塞

依赖票31（单位）与票37（要画的几何）。
"""),

    dict(num=42, title="规格树（浮动） + 模型树 + 图形区选择联动", blocked=[37, 41], scopes=[
        "apps/workbench/ui_workbench.py", "apps/workbench/ui_trees.py",
        "apps/workbench/core/workbench_service.py", "apps/workbench/tests/test_m42.py"], body="""
**spec**：%s §实现决策/UI 模块化 与用户故事 40/41/42。

## What to build

- **规格树**（参数化建模模块）：`Toplevel` **浮动窗口**，可停靠/关闭，
  展示建模特征层级（模板 → 外形面 → 梁/肋/框 → 开口 → 铺层）；
  点一个特征能看到它的参数与几何范围。
- **模型树**（CAE 模块）：`ttk.Treeview` 停靠件，图形化管理全部对象
  —— **部件（继承参数化建模结果）、材料（继承参数化建模结果）**、
  载荷与边界、分析步、网格、结果、报告。
- **联动**：树里选中对象 → 图形区高亮；图形区选中 → 树定位。
- 部件与材料**单向引用**建模结果，不复制数据。

## Acceptance criteria

- [ ] 规格树是**浮动窗口**，可关闭/重新打开，含完整特征层级
- [ ] 模型树含七类节点，且部件/材料确实继承参数化建模结果（不是另建一份）
- [ ] 树 → 图形 与 图形 → 树 双向联动（服务层可断言选中状态）
- [ ] 树的数据来自服务层（UI 不含逻辑），有可脱机单测的接口
- [ ] 构造/销毁冒烟；无显示环境 SKIP
- [ ] Python 3.8 兼容

## 阻塞

依赖票37（特征层级数据）与票41（外壳与图形区）。
"""),

    dict(num=43, title="既有分析 / 审批 / 优化功能迁入新布局（零回归）", blocked=[41, 42], scopes=[
        "apps/workbench/ui_workbench.py", "apps/workbench/tests/test_m43.py"], body="""
**spec**：%s §实现决策/UI 模块化 与用户故事 45。

## 背景

新外壳做好了，但 P2 的「审批与型号汇总」页、P3 的「优化」页还在老 Notebook 里。
本票把它们**完整迁入新布局**，并且**不能丢功能**。

## What to build

- 分析 / 审批 / 优化三块功能接入新外壳（菜单、工具栏、上下文栏、树都能到达）。
- 现有交互**全部保留**：角色切换、待办、审批/退回/签发、型号汇总、
  优化会话列表、汇总与对比、设计点血缘、采用/维持原设计、单位显示。
- 归属关系要清楚：审批与优化属于 CAE 模块（模型树 / 菜单里可达），
  单位切换在工具栏与状态栏。

## Acceptance criteria

- [ ] 三块功能的**每一个既有交互**都能在新布局里完成（逐项核对清单）
- [ ] P2/P3 的 UI 冒烟与逻辑测试零回归（test_m19 / test_m28 等）
- [ ] 菜单/工具栏/上下文栏都能到达这些功能
- [ ] 单位切换与这些页面不冲突（换单位后汇总/报告显示仍正确）
- [ ] 构造/销毁冒烟；无显示环境 SKIP
- [ ] Python 3.8 兼容

## 阻塞

依赖票41、票42（新外壳与两棵树就位后才能迁）。
"""),
]

for t in TICKETS:
    deps = "\n".join("- #%d" % d for d in t["blocked"]) or "- None (can start immediately)"
    text = "# %d: %s\n\n" % (t["num"], t["title"])
    text += "## Parent\n\nPart of #30\n\n"
    text += t["body"].strip() + "\n\n"
    text += "## Blocked by\n\n" + deps + "\n\n"
    text += "## 写范围（advisory）\n\n"
    for s in t["scopes"]:
        text += "- `%s`\n" % s
    # 用票号命名，便于排序
    fn = os.path.join(OUT, "%d.md" % t["num"])
    with io.open(fn, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(fn, len(text))
