> **已发布到 GitHub Issue：** swh18091378132-lab/cae#8（本文件为本地原文/交接要点，工单状态以 GitHub 为准）

# 08: Study 端到端编排与 UI：一条链跑通 + 可追溯展示

**What to build:** 单个工程师在本地工作台（Tkinter 界面或一条命令）从参数集出发，一路跑完 参数化建模 → 拓扑/自然格 → FE 模型 → 求解 → 结果抽取 → 强度校核 → 报告，全流程一条链完成；过程中每一步的产物与版本在界面上可见，血缘 DAG 可查看、可回溯，危险部位与报告可打开。达到 P1 完成定义。

**Blocked by:** 07

**Status:** done

## 关键设计：把 UI 的可测性变成服务层的可测性

UI 控件难以自动化测试，因此分成两层：

- `core/workbench_service.py` —— **应用服务层**（纯 Python）：`list_templates` / `start_study` / `run_chain` / `get_lineage` / `format_lineage_text` / `get_report_path` / `open_report`。全部编排逻辑在此，可完整单测。
- `ui_workbench.py` —— **Tkinter 视图**：只调用服务层，自身几乎无逻辑。

这样测试测服务层（外部行为），UI 只做冒烟。

## 完成情况

实现：
- `core/workbench_service.py` —— 服务层；`mode="fake"|"real"` 选择适配器（fake 无需 CATIA/Abaqus，可演示）
- `ui_workbench.py` —— 三栏布局（左：模板+参数+准则+模式+运行；右上：日志；右下：血缘+打开报告）；**后台线程 + 队列**轮询，长任务不冻 UI
- `main.py` —— 新增 `run [模板] [--real]` 与 `ui [--real]` 子命令，一条命令跑完整链
- `tests/test_m8.py` —— 8 组：模板列表 / 建 Study / 全链十类产物 / 血缘父子关系抽查 / 重跑版本递增 / 报告路径 / 错误路径 / UI 冒烟

## 验证结果

CLI 一条命令跑通（fake 模式）：
```
Study STUDY-20261023-110034 血缘 (10 个产物):
  parameter_set#1 -> catpart#2 -> topology#3 -> mesh#4 -> inp#5 -> odb#6
    -> extraction#7 --+
  check_rule#8 ------+-> check_result#9 -> report#10
校核汇总: {"all_pass": false, "max_fi": 3.6, "n_detail_rows": 24, "n_elems": 6, ...}
报告: ...\report\strength_report.docx
```

UI 冒烟：能构造窗口、模板已加载、参数控件已生成、可正常销毁。

## 真机模式现状（ADR-0013）

- ✅ **Abaqus 2023 真机全链已跑通**（求解 → ODB → 抽取 → 校核 → 报告）
- ❌ **CATIA V5-6R2022 已安装但 COM 不可用**：注册表无 `CATIA.Application`；用 `CATSTART -env CATIA_P3.V5-6R2022.B32` 启动后 CNEXT 卡在「许可证管理器」对话框 → 当前无可用 CATIA 许可证，COM 类因此未注册。
- 故 `--real` 模式下 CATIA 环节暂不可用；**P1 其余环节不受影响**（铺层作 CAE 属性，本就绕开 CATIA 复材模块）。fake 为默认模式，开发与回归不阻塞。

- [x] 单用户可从参数集出发一条链跑完并得到报告
- [x] 界面/命令展示血缘 DAG 与各产物版本，可回溯
- [x] 危险部位与报告可在工作台查看/打开
- [x] 端到端编排以 fake 适配器可确定性回归
- [x] P1 DoD：机翼盒段全链跑通、全程可追溯、单用户可操作

**未覆盖（P2）**：真机 CATIA 环节（待现场解决许可证）、多角色审批 UI、并行任务队列。
