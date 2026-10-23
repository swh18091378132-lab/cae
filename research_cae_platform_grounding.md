# CAE 平台设计事实核查报告（CATIA V5R18 / Abaqus 6.13-4 / AI 后处理 / 复合材料强度）

> 目标：为设计评审提供**事实依据**，不做产品决策。标注 ⚠️ 者为不确定或与前提冲突处。

## 1) CATIA V5R18 自动化与参数化能力

- **版本**：V5R18 于 **2007-02-10** 发布（[Wikipedia/CATIA](https://wiki.bitter.house/content/wikipedia_en_all_maxi_2024-01/A///CATIA)）。属 32-bit 老版本，进程内存受限（大装配易 OOM），API 覆盖弱于后续版本。
- **外部集成方式**（[DS 官方 *Invoking CATIA from a Scripting Language*](https://catiadesign.org/_doc/V5Automation/online/CAAScdInfTechArticles/CAAInfInvoking.htm)）：
  - **V5 Automation（COM/OLE）**：CATIA 是 OLE Automation server。进程外可用 `CreateObject("CATIA.Application")` / `GetObject(,"CATIA.Application")`，语言含 VBA、VB6、VBScript、Windows Script Host（`cscript`）、以及 Python（`win32com.client.Dispatch`）。进程内为 VBA / CATScript 宏。
  - **CATScript**：为 Unix/Windows 兼容的 BASIC 方言，V5R7 后仅在 Windows 由 VBScript 引擎去类型后执行。
  - **命令行驱动**：`CNEXT -macro X.CATScript`、`CNEXT -batch -macro X.CATScript`（batch 免刷新、跑完自退）。
  - **CAA C++（RADE）**：能力上限最高（可加 Workbench、定义新特征），但需 **RADE license + CAA Partner 协议**（[Chenshi 指南](https://qdchenshi.com/en/blog/catia-development-guide-en)）。
- **Knowledgeware / KBE**（[DS Knowledge Advisor Automation](https://www.maruf.ca/files/caadoc/CAAScdKniTechArticles/CAAKniAutomationPrinciples.htm)）：CATPart 上可脚本操作 `Parameters`、`Relations` 集合；`CreateDimension/CreateInteger` 建参数，`Relations.CreateFormula` 绑公式，Rules/Checks/Design Table 做规则校验。改参后 `Part.Update()` 重算再生。
- **文件格式**：`CATPart`（零件）、`CATProduct`（装配）、`CATDrawing`、`.catvba`、`.CATScript`；均为**私有二进制**，无官方文本交换格式。
- **外部程序能否"建/改参数化零件并再生"**：**能**——打开 CATPart → 改参数 → `Part.Update()`（[官方 Helicoidal Stair 示例](https://catiadesign.org/_doc/V5Automation/online/CAAScdGsiUseCases/CAAGsiCreateStair.htm) 演示参数+公式+Update 驱动几何）。参数集交换标准做法：**Design Table 由 Excel/CSV 驱动**，或 Automation 直接读写 Parameters。
- **R18 现实痛点**：Automation 单线程同步、循环中 UI 冻结；`Update()` 代价随特征数增长，须循环外一次性 Update；宏录制/回放对 fillet、"current solid" 等易脆（历史 APAR [HC87383](https://www.ibm.com/support/pages/node/5592981) 即此类）；部分高级模块（曲面/管路）Automation 覆盖不全；不能扩展 UI 或新特征类型（需 CAA）；非 ASCII/空格路径不稳。
- **许可前提**：**是**——外部程序必须驱动**本机已安装并授权的 CATIA 实例**（Automation 会启动 CATIA 进程）；无免许可 headless 模式；Knowledgeware 另需 KW Advisor/Expert 许可。
- **So what**：平台若走"外部参数化驱动"，本质是"**起一个 CATIA 进程 + 改参 + Update**"，强依赖本机 CATIA 授权与 32-bit 稳定性；宜把参数集设计为 Excel/CSV/Design Table 这类**中性载体**，并做单进程批处理与失败重试。

## 2) Abaqus 6.13-4 脚本与结果访问

- ⚠️ **重要更正**：**6.13 内置 Python 是 2.6.2，不是 2.7.x**。多方独立来源一致：6.12/6.13=Python **2.6.2**，6.14/2016 起才升到 2.7.3（[1cae 教程](http://www.1cae.com/a/abaqus/48/abaqus-cae-6375.htm)、[腾讯云/SO 6.13-2 问答](https://cloud.tencent.com/developer/ask/sof/100807426)）。前提中"2.7.x"不成立，方案须按 **2.6.2 语法/库**设计。
- **命令**：`abaqus cae`、`abaqus cae noGUI=script.py`、`abaqus python script.py`、`abaqus job=J interactive|background cpus=N mp_mode=threads|mpi`、`abaqus viewer`、`abaqus fetch`、`abaqus python -m upgradeScript`（[Abaqus Scripting User's Guide 6.13](http://orpheus.nchc.org.tw:2080/v6.13/pdf_books/SCRIPT_USER.pdf)）。
- **ODB 访问**（[官方 ODB 读取文档](https://hailin.wang/abqpy/en/dev/user/output/python/read.html)）：`from odbAccess import openOdb` → `odb.steps[name].frames[-1].fieldOutputs['S'/'U'/'E'/'LE'/'RF']`。`FieldValue` 成员含 `elementLabel/nodeLabel/integrationPoint/sectionPoint/data/mises/maxPrincipal` 等；**铺层/section point 用 `sectionPoint` 区分**；`getSubset(region=..., position=INTEGRATION_POINT, elementType=..., sectionPoint=...)` 取子集；`instances[name].nodeSets/elementSets` 按名取集合。历史量：`historyRegions['Node ...'].historyOutputs`。
- **相关单元族**：`S4R`（4 节点减缩积分壳）、`SC8R`（8 节点连续壳 continuum shell）、`C3D8R`（8 节点减缩砖）、`C3D8I`、`CSS8`、内聚单元 `COH3D8`（分层）。6.13 新增 **VCCT 增强**用于复合材料分层（[6.13 发布说明](https://3dswym.3dexperience.3ds.com/post/simulia-community/now-available-abaqus-6-13-isight-5-8-and-simulation-execution-engine-5-8_3477)）。
- **提交与许可**：需连接 FLEXnet/DSLS 许可服务器；Abaqus/Standard token 数随核数增长，约 **INT(5 × Ncore^0.422)**（[HKU HPC 文档](https://hpc.hku.hk/hpc/software/abaqus/)）。Abaqus **无内置队列**，批量/排队靠外部调度器（SLURM/PBS/LSF）。
- **纯脚本驱动的现实限制**：CAE kernel 是 Python，但部分复杂几何网格/交互仍需 GUI；ODB 逐单元循环在 2.6.2 下慢（百万单元级分钟~十几分钟），大数据需向量化/限集合；matplotlib 受 tkinter 限制；自定义本构须 Fortran 子程序（非 Python）。
- **So what**：结果抽取器应**按 section point/ply 写死索引口径**、只读所需集合、输出 CSV/JSON 中性中间层；用 Python 2.6.2 兼容语法；token 与排队策略要显式建模。

## 3) AI/LLM 用于 CAE 后处理（2024–2025 现状）

- **LLM 不能原生读二进制 .odb**：成立。ODB 为私有二进制，标准管线是**先用 Python/odbAccess 抽成结构化数据（CSV/JSON/Parquet），再交给 ML/LLM**。
- **热点/危险点识别是算法问题**：峰值应力、von Mises、failure index、margin<1 的定位靠 `argmax`/阈值遍历单元与铺层——确定性，不需要 LLM。
- **ML/LLM 真正增值处**：①**代理模型 surrogate** 快速复算；②异常检测；③**自然语言解释/摘要**；④自动标注；⑤**tool-calling LLM 调用 Python 抽取器**编排；⑥视觉模型读云图 PNG。
- **现实工具/路线**：**Ansys SimAI**（2024-01 发布，云端生成式 AI，基于仿真数据训练、近实时预测，[Ansys 新闻](https://www.ansys.com/en-in/news-center/press-releases/1-9-24-ansys-launches-simai)）、**optiSLang**（DOE/优化流程）、**Abaqus+Python 管线**、**GNN 代理模型**研究（如 [abaqus-ml-skills](https://github.com/Haibarikiku/abaqus-ml-skills)：Claude Code 技能桥接 Abaqus 与 ML，LHS 批量造数 + ODB→网格 CSV）。
- **hype vs 可用**：SimAI 类代理=商用但依赖训练数据、仅在插值域可信；"LLM 直接读 ODB/直接判定强度"=**hype**；"LLM 工具调用抽取+摘要+标注"与"VL 模型读云图"=**生产可行但需校验**（基准 [OpenSeeSimE](https://www.researchsquare.com/article/rs-8389251/v1.pdf) 显示 VLM 在工程仿真上仍偏弱）。
- **So what**：平台应把 AI 定位为**抽取后的解释/编排层**，强度判定留在确定性算法层；代理模型作为"快筛"而非"定论"。

## 4) 复合材料强度校核

- **失效准则**：Max Stress / Max Strain（简单、无耦合，初筛）；**Tsai-Hill**（二次型、单式、不分拉压）；**Tsai-Wu**（光滑、含 F12 双轴项）；**Hashin**（按纤维/基体拉压分模式，Abaqus 内置损伤起始）；**Puck**（物理型基体/纤维失效、Mohr 型）；**LaRC**（NASA Langley，物理型基体开裂）。用途：Max/Tsai-Hill/Tsai-Wu 作快速筛；Hashin/Puck/LaRC 作损伤起始与渐进损伤。
- **Abaqus 内置**：提供 5 种 2D 正交各向异性应力/应变失效准则作**后处理输出（不退化材料）**——Max Stress、Max Strain、Tsai-Hill、Tsai-Wu、Azzi-Tsai-Hill；failure index 归一到 0–1，≥1 判失效；另有 Hashin 损伤模型做渐进损伤。
- **标准/参考**：**MIL-HDBK-17 → CMH-17**《复合材料手册》**第 3 卷（材料应用、设计与分析）**为设计/失效准则主参考；另有 FAA/DOT、NASA 报告。
- **许用裕度**：`MS = 许用/施加 − 1`（或 强度/应力 − 1）；`FI = 施加/许用`，FI≥1 失效。
- **"每单元每铺层"校核结构**：对**每个单元 × 每个 section point（铺层）× 每种准则**循环算 FI，取最大 FI / 最小 MS 及临界位置，输出含 element id、ply/section point、准则、FI、MS 的明细表——这是标准 ply-by-ply 后处理。**⚠️ "自然格"非已知术语，不做假设/不采纳。**
- **So what**：强度模块应做成**遍历 element×ply×criterion 的确定性引擎**，输出可追溯明细；LLM 只做汇总解释，不参与判据计算。

## Facts vs assumptions

**已核实（有出处）**
- CATIA V5R18 = 2007-02-10；Automation(COM/VB/CATScript/Python-win32com)、CAA RADE、Knowledgeware(参数/公式/Design Table)、CATPart/CATProduct 私有二进制。
- 外部程序须驱动**本机已授权 CATIA**；`CNEXT -batch -macro`、`Part.Update()` 再生。
- **Abaqus 6.13 内置 Python = 2.6.2**（非 2.7.x）；`abaqus cae/python/job` 用法；odbAccess/sectionPoint 访问铺层应力；S4R/SC8R/C3D8R/COH3D8；token≈INT(5·Ncore^0.422)；无内置队列。
- Abaqus 5 种失效准则为后处理输出；Hashin 损伤模型存在；CMH-17 Vol.3 为主参考。

**假设 / 不确定（需确认）**
- ⚠️ 前提"6.13 用 Python 2.7.x"**错误**，本报告按 2.6.2 处理；若现场实际为 6.13-x 某补丁，请以 `abaqus python` 打印版本为准。
- V5R18 是否 32-bit-only 需按现场安装确认（影响大模型稳定性）。
- CAA RADE 授权获得难度、R18 API 具体覆盖度未逐条验证。
- "自然格"含义不明，未纳入。
- SimAI/代理模型的精度与适用边界未量化（厂商宣称，需实测）。
