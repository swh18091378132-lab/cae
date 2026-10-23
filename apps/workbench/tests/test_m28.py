# -*- coding: utf-8 -*-
"""
test_m28.py -- 票28 验收：优化汇总视图接入工作台

验收点（对应 issue #28）：
  1. 工作台可展示优化会话列表与汇总视图（**测服务层**）
  2. 汇总按「可行 / 不可行 / 评估失败」分组，能区分技术故障与设计不可行
  3. 设计点可点开查完整血缘（"这个数是怎么来的"）
  4. 优化前后对比数值正确（质量变化、最小 MS 变化）
  5. 采用方案入口**只对已批准**；未批准一律拒绝（服务层断言 + UI 置灰双层）
  6. 「维持原设计」可操作且被记录
  7. 工作台逻辑在服务层，可脱离 UI 单测（除 UI 冒烟外不 import tkinter）
  8. UI 冒烟可构造并销毁；无显示环境则 SKIP
  9. 既有「分析」页与「审批与型号汇总」页零回归
 10. Python 3.8 兼容

## 这一票的关键设计：为什么"采用"是两层的

票25 已经把 L2 门建在 `optimization_loop` 里：`adopted_point()` 未经人批准恒为 None、
`require_adopted_point()` 直接抛错。票28 要做的不是**重造那道门**，而是把它
**暴露成工作台可用的入口**，并且：

  * 服务层：非「签发」角色不得采用（与报告签发同级的权力）；未批准时任何
    "取采用方案"的调用必须**明确拒绝**，而不是返回一个空值让 UI 显示成"已采用"。
  * UI 层：采用按钮在不可采用时**置灰**，并把服务层给的可读原因显示出来
    —— 于是"能不能点"这件事在**两层**上都是同一个判据（都来自服务层），
    UI 不复制权限表。

这样做的理由：UI 置灰只是体验，服务层拒绝才是合规。只做其中一层，
要么用户能点出一个失败的按钮，要么脚本/别的入口能绕过门。

运行 (在 apps/workbench 下): python tests/test_m28.py
"""

import os
import sys
import ast
import json
import shutil
import inspect

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
sys.path.insert(0, CORE)
sys.path.insert(0, WORKBENCH)

import db
import design_vector
import optimization_loop as optloop
import workbench_service as ws
import compat_py38
from run_study import RunOptions
from catia_driver import FakeCatiaDriver
from abaqus_runner import FakeAbaqusRunner


TMP_ROOT = os.path.join(WORKBENCH, ".tmp_test", "m28")

TEMPLATE_ID = "wingbox_v1"

# 设计向量声明（与票25 自测同一口径）：3 个几何连续 + 2 个几何离散 + 铺层参数化
DV_SPEC = {
    "id": "m28_dv_v1",
    "template_id": TEMPLATE_ID,
    "variables": [
        {"name": "span", "category": "geometry", "type": "continuous",
         "min": 5.0, "max": 12.0, "default": 5.0},
        {"name": "sweep_deg", "category": "geometry", "type": "continuous",
         "min": 0.0, "max": 40.0, "default": 10.0},
        {"name": "c_root", "category": "geometry", "type": "continuous",
         "min": 0.6, "max": 1.6, "default": 1.0},
        {"name": "c_tip", "category": "geometry", "type": "continuous",
         "min": 0.3, "max": 0.9, "default": 0.6},
        {"name": "n_ribs", "category": "geometry", "type": "discrete",
         "min": 2, "max": 5, "step": 1, "default": 3},
        {"name": "n_stringers", "category": "geometry", "type": "discrete",
         "min": 3, "max": 5, "step": 1, "default": 4},
        {"name": "n_plies", "category": "ply", "type": "discrete",
         "min": 4, "max": 10, "step": 2, "default": 8},
        {"name": "angle_0", "category": "ply", "type": "discrete",
         "candidates": [0, 1, 2, 3], "default": 0},
        {"name": "angle_1", "category": "ply", "type": "discrete",
         "candidates": [0, 1, 2, 3], "default": 1},
        {"name": "ply_thickness", "category": "ply", "type": "continuous",
         "min": 0.08, "max": 0.2, "default": 0.1},
    ],
    "ply_sequence": {
        "n_plies": "n_plies",
        "angles": ["angle_0", "angle_1"],
        "thickness": "ply_thickness",
        "material": "CFRP",
        "order": "cyclic",
        "angle_table": [0.0, 45.0, -45.0, 90.0],
    },
}

INTENT = {
    "objective": {"name": "mass", "direction": "minimize"},
    "constraints": [{"name": "strength", "require": "all_pass"}],
    "note": "M28 单测：强度合格前提下减重",
}

BUDGET = {"max_evals": 3, "max_rounds": 2, "improvement_threshold": 1e-6}

RULES = {"criteria": ["tsai_wu"], "safety_factor": 1.0}

# 该向量**越出参数化包络**（c_tip=0.9 >= c_root=0.6 破坏 c_tip < c_root），
# 用来在会话里制造一个**技术故障**点（参数越界属故障，不是"设计不可行"）。
BAD_VECTOR = {
    "span": 5.0, "sweep_deg": 10.0, "c_root": 0.6, "c_tip": 0.9,
    "n_ribs": 3, "n_stringers": 4,
    "n_plies": 8, "angle_0": 0, "angle_1": 1, "ply_thickness": 0.1,
}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _fresh(name):
    d = os.path.join(TMP_ROOT, name)
    if os.path.isdir(d):
        try:
            shutil.rmtree(d)
        except OSError:
            pass
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def _near(a, b, tol=1e-9):
    if a is None or b is None:
        return a is b
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)), abs(float(b)))


def _options(tmp, sid):
    """一个设计点的标准全链选项（各阶段目录逐点独立）。"""
    def dir_for(sub):
        return os.path.join(tmp, sid, sub)
    return RunOptions.full_chain(dir_for, with_report=False,
                                 abaqus=None).replace(rules=RULES)


def _run_session(svc, tmp, sid, inject_hotspot=False, max_evals=3,
                 seed=0):
    """
    建会话并跑一次优化，返回 summary。

    inject_hotspot=True 时 fake 求解器制造高应力热点 -> **所有点都不可行**，
    用来验证「不可行」分组（与「可行」分开）。
    """
    session = optloop.create_session(
        svc.conn, DV_SPEC, intent=INTENT, budget=BUDGET,
        name="M28 " + sid, session_id=sid)
    return optloop.run_optimization(
        svc.conn, session["session_id"],
        FakeCatiaDriver(os.path.join(tmp, sid, "catparts")),
        options=None, work_dir=os.path.join(tmp, sid, "work"),
        abaqus=FakeAbaqusRunner(n_elems=6, n_plies=2,
                                inject_hotspot=inject_hotspot),
        seed=seed, max_evals=max_evals)


def _add_failed_point(svc, tmp, sid):
    """往会话里加一个**技术故障**点（参数越包络），返回落库后的点。"""
    return optloop.evaluate_point(
        svc.conn, sid, BAD_VECTOR,
        FakeCatiaDriver(os.path.join(tmp, sid, "catparts")),
        work_dir=os.path.join(tmp, sid, "work"),
        abaqus=FakeAbaqusRunner(n_elems=6, n_plies=2))


def _point_of(summary, point_id):
    for p in summary.get("points") or []:
        if p["point_id"] == point_id:
            return p
    return None


def _baseline_of(summary):
    for p in summary.get("points") or []:
        if p.get("source") == optloop.SOURCE_BASELINE:
            return p
    return None


def _expect_service_error(fails, fn, must_contain, label):
    """调用 fn 必须抛 ServiceError，且原因里含某个关键词（不静默、可读）。"""
    try:
        fn()
    except ws.ServiceError as exc:
        if must_contain and must_contain not in str(exc):
            fails.append("%s：报错原因不可读（缺 %r）: %s"
                         % (label, must_contain, exc))
        return
    except Exception as exc:
        fails.append("%s：应抛 ServiceError，实际 %s: %s"
                     % (label, type(exc).__name__, exc))
        return
    fails.append("%s：未被拒绝（应抛 ServiceError）" % label)


# ---------------------------------------------------------------------------
# 1) 会话列表与汇总视图（测服务层）
# ---------------------------------------------------------------------------

def test_session_list_and_summary(fails, svc, tmp):
    """1) 会话列表与汇总视图：项目负责人一页掌握整次优化。"""
    sid = "OPT-M28-A"
    summary = _run_session(svc, tmp, sid)

    # ---- 会话列表 ----
    rows = svc.list_optimization_sessions()
    if not isinstance(rows, list) or not rows:
        fails.append("list_optimization_sessions 未返回会话列表")
        return
    row = None
    for r in rows:
        if r.get("session_id") == sid:
            row = r
            break
    if row is None:
        fails.append("会话列表里找不到刚跑的会话 %r" % sid)
        return
    for key in ("session_id", "name", "template_id", "status", "created_at",
                "n_points", "counts", "adopted", "label"):
        if key not in row:
            fails.append("会话列表行缺少字段 %r" % key)
    if row["n_points"] != summary["n_points"]:
        fails.append("会话列表的设计点数与汇总不一致: %r != %r"
                     % (row["n_points"], summary["n_points"]))
    if row["adopted"] is not False:
        fails.append("未经人批准的会话不应在列表里显示为已采用: %r"
                     % row.get("adopted"))
    if not row.get("label"):
        fails.append("会话列表行缺少可读 label（UI 无法直接显示）")

    # ---- 汇总视图 ----
    view = svc.get_optimization_summary(sid)
    if view.get("session_id") != sid:
        fails.append("汇总视图的会话 id 不符: %r" % view.get("session_id"))
    if view.get("template_id") != TEMPLATE_ID:
        fails.append("汇总视图未带模板族: %r" % view.get("template_id"))
    for key in ("n_points", "counts", "groups", "best", "points",
                "recommendation", "adoption", "intent", "budget", "text"):
        if key not in view:
            fails.append("汇总视图缺少字段 %r" % key)

    # 意图/预算可追溯（"当初想优化什么"记录在案）
    if (view.get("intent") or {}).get("objective", {}).get("name") != "mass":
        fails.append("汇总视图未带优化目标: %r" % (view.get("intent"),))
    if (view.get("budget") or {}).get("max_evals") != BUDGET["max_evals"]:
        fails.append("汇总视图未带预算: %r" % (view.get("budget"),))

    # 三个分组的键必须在（哪怕某组为空），UI 才能稳定渲染三栏
    for g in (optloop.GROUP_FEASIBLE, optloop.GROUP_INFEASIBLE,
              optloop.GROUP_FAILED):
        if g not in (view.get("groups") or {}):
            fails.append("汇总视图缺少分组 %r" % g)
        if g not in (view.get("counts") or {}):
            fails.append("汇总视图的 counts 缺少分组 %r" % g)

    # 计数自洽：三组之和 == 总点数
    total = sum((view["counts"] or {}).get(g, 0) for g in
                (optloop.GROUP_FEASIBLE, optloop.GROUP_INFEASIBLE,
                 optloop.GROUP_FAILED))
    if total != view["n_points"]:
        fails.append("分组计数之和 %d != 设计点总数 %d" % (total, view["n_points"]))

    # 每个设计点都带 UI 需要的字段
    if not view["points"]:
        fails.append("汇总视图没有任何设计点")
    for p in view["points"]:
        for key in ("point_id", "seq", "vector", "study_id", "group",
                    "group_name", "ok", "feasible", "objective_value",
                    "is_best", "source", "label"):
            if key not in p:
                fails.append("设计点 #%s 缺少字段 %r"
                             % (p.get("point_id"), key))
                break
        if not p.get("label"):
            fails.append("设计点 #%s 缺少可读 label" % (p.get("point_id"),))

    # 可行点有质量与约束值；最优点唯一且是可行点里最优的
    best = view.get("best")
    if best is None:
        fails.append("有可行点时应给出最优可行点，实际 None")
    else:
        if not best.get("is_best"):
            fails.append("best 点未标记 is_best")
        if not best.get("feasible"):
            fails.append("最优点必须是可行点")
        feas = [p for p in view["groups"][optloop.GROUP_FEASIBLE]]
        if feas:
            min_val = min(float(p["objective_value"]) for p in feas)
            if not _near(best["objective_value"], min_val):
                fails.append("最优点不是可行点里目标值最小的: %r != %r"
                             % (best["objective_value"], min_val))
        n_best = len([p for p in view["points"] if p.get("is_best")])
        if n_best != 1:
            fails.append("is_best 应恰好有 1 个点，实际 %d" % n_best)

    # 可读渲染能直接摆到 UI 上
    text = view.get("text") or ""
    if not text.strip():
        fails.append("汇总视图没有可读文本渲染")
    for frag in ("优化会话", "设计点", "可行", "不可行", "评估失败"):
        if frag not in text:
            fails.append("汇总文本缺少 %r" % (frag,))

    # 不存在的会话 -> 明确可读错误
    _expect_service_error(fails,
                          lambda: svc.get_optimization_summary("OPT-NOT-EXIST"),
                          "不存在", "不存在的会话汇总")


# ---------------------------------------------------------------------------
# 2) 三分组：技术故障 != 设计不可行
# ---------------------------------------------------------------------------

def test_groups(fails, svc, tmp):
    """2) 可行 / 不可行 / 评估失败三组可分，故障原因可查。"""
    # 会话 B：全部点不可行（fake 求解器注入热点 -> FI 超 1）
    sid_b = "OPT-M28-B"
    _run_session(svc, tmp, sid_b, inject_hotspot=True)
    # 再往里加一个**技术故障**点（参数越包络）
    failed_point = _add_failed_point(svc, tmp, sid_b)

    view = svc.get_optimization_summary(sid_b)
    groups = view["groups"]

    if not groups[optloop.GROUP_INFEASIBLE]:
        fails.append("注入热点后应有「不可行」设计点，实际为空")
    if not groups[optloop.GROUP_FAILED]:
        fails.append("加了越包络的点后应有「评估失败」设计点，实际为空")

    # 不可行点：ok=True / feasible=False（跑通了，但设计不满足约束）
    for p in groups[optloop.GROUP_INFEASIBLE]:
        if p["ok"] is not True:
            fails.append("不可行点 #%s 的 ok 应为 True，实际 %r"
                         % (p["point_id"], p["ok"]))
        if p["feasible"] is not False:
            fails.append("不可行点 #%s 的 feasible 应为 False，实际 %r"
                         % (p["point_id"], p["feasible"]))
        if p["objective_value"] is None:
            fails.append("不可行点也应有权衡用的目标值（设计不好 ≠ 算不出）: #%s"
                         % (p["point_id"],))

    # 故障点：ok=False / feasible=None（**不是** False）+ 有可读原因
    for p in groups[optloop.GROUP_FAILED]:
        if p["ok"] is not False:
            fails.append("故障点 #%s 的 ok 应为 False，实际 %r"
                         % (p["point_id"], p["ok"]))
        if p["feasible"] is not None:
            fails.append("故障点的 feasible 必须为 None（不能与「设计不可行」"
                         "混淆）: #%s 实际 %r" % (p["point_id"], p["feasible"]))
        if p["objective_value"] is not None:
            fails.append("故障点不应有目标值: #%s" % (p["point_id"],))

    # 故障原因在汇总里可见（"哪些点算不出来、为什么"）
    reasons = view.get("failed_reasons") or []
    if not reasons:
        fails.append("汇总未给出评估失败的原因清单")
    ids = [r.get("point_id") for r in reasons]
    if failed_point["point_id"] not in ids:
        fails.append("新加的故障点未出现在失败原因清单里: %r" % (ids,))
    for r in reasons:
        if not r.get("message"):
            fails.append("失败原因缺少可读 message: %r" % (r,))
    kinds = [r.get("kind") for r in reasons]
    if "param_error" not in kinds:
        fails.append("越包络的故障应归类为 param_error，实际 %r" % (kinds,))

    # 故障点绝不参与"最优"比较
    best = view.get("best")
    if best is not None and best["point_id"] in [p["point_id"]
                                                 for p in groups[optloop.GROUP_FAILED]]:
        fails.append("故障点被选成了最优点")

    # 可读文本要能区分三者（项目负责人据此分清"设计不好"与"算不出来"）
    text = view.get("text") or ""
    for frag in ("可行", "不可行", "评估失败"):
        if frag not in text:
            fails.append("汇总文本缺少 %r 分组说明" % (frag,))
    if "算不出" not in text and "故障" not in text:
        fails.append("汇总文本未区分技术故障: %s" % text[:200])


# ---------------------------------------------------------------------------
# 3) 设计点血缘
# ---------------------------------------------------------------------------

def test_point_lineage(fails, svc, tmp):
    """3) 点开一个设计点能看到它的完整血缘（这个数是怎么来的）。"""
    sid = "OPT-M28-C"
    summary = _run_session(svc, tmp, sid)
    best = summary.get("best")
    if best is None:
        fails.append("前置条件不满足：没有最优可行点，无法测血缘")
        return

    detail = svc.get_optimization_point_lineage(sid, best["point_id"])
    if detail.get("session_id") != sid:
        fails.append("设计点血缘的会话 id 不符")
    if detail.get("point_id") != best["point_id"]:
        fails.append("设计点血缘的点 id 不符")
    if detail.get("study_id") != best["study_id"]:
        fails.append("设计点血缘未带关联 Study: %r" % detail.get("study_id"))
    for key in ("vector", "group", "group_name", "objective_value",
                "lineage", "lineage_text", "text"):
        if key not in detail:
            fails.append("设计点血缘缺少字段 %r" % key)

    # 血缘是**真的**产物链：从数模到校核结果都在
    lin = detail.get("lineage") or {}
    nodes = lin.get("nodes") or []
    if len(nodes) < 5:
        fails.append("设计点血缘节点过少: %d" % len(nodes))
    kinds = [n.get("kind") for n in nodes]
    for want in ("parameter_set", "catpart", "topology", "mesh", "inp",
                 "odb", "extraction", "check_result"):
        if want not in kinds:
            fails.append("设计点血缘缺少产物 %r（现有 %r）" % (want, kinds))
    if not (detail.get("lineage_text") or "").strip():
        fails.append("设计点血缘没有可读渲染")

    # 关联 Study 与设计点一致（artifact 的 study_id 必须就是那个 Study）
    for n in nodes:
        row = svc.conn.execute(
            "SELECT study_id FROM artifacts WHERE id=?", (n["id"],)).fetchone()
        if row is None or row["study_id"] != best["study_id"]:
            fails.append("设计点血缘混进了别的 Study 的产物: %r" % (n,))
            break

    # 校核结论也能看到（"这个数是怎么来的"= 目标值背后的校核口径）
    check = detail.get("check")
    if check is not None:
        if check.get("study_id") != best["study_id"]:
            fails.append("设计点校核明细的 Study 不符")
        if check.get("n_detail_rows", 0) <= 0:
            fails.append("设计点校核明细没有行")

    # 不存在的设计点 -> 明确可读错误
    _expect_service_error(
        fails,
        lambda: svc.get_optimization_point_lineage(sid, 999999),
        "不存在", "不存在的设计点血缘")


# ---------------------------------------------------------------------------
# 4) 优化前后对比
# ---------------------------------------------------------------------------

def test_comparison(fails, svc, tmp):
    """4) 对比数值正确：质量降低多少、最小 MS 变化多少。"""
    sid = "OPT-M28-D"
    summary = _run_session(svc, tmp, sid)
    baseline = _baseline_of(summary)
    if baseline is None:
        fails.append("前置条件不满足：会话里没有基线点（source=baseline）")
        return

    cmp = svc.get_optimization_comparison(sid)
    for key in ("before", "after", "mass_before", "mass_after",
                "mass_delta", "mass_reduction_pct",
                "min_ms_before", "min_ms_after", "min_ms_delta",
                "text"):
        if key not in cmp:
            fails.append("对比结果缺少字段 %r" % key)

    before = cmp.get("before") or {}
    after = cmp.get("after") or {}

    # 「优化前」必须就是基线点（设计向量默认值 = 当前设计）
    if before.get("point_id") != baseline["point_id"]:
        fails.append("对比的「优化前」不是基线点: %r != %r"
                     % (before.get("point_id"), baseline["point_id"]))
    if before.get("study_id") != baseline["study_id"]:
        fails.append("对比的「优化前」Study 不符")

    # 「优化后」：未采用时明确标出它只是**建议**（不是已采用方案）
    if cmp.get("basis") not in ("adopted", "best_feasible", "none"):
        fails.append("对比未说明「优化后」的取数依据: %r" % (cmp.get("basis"),))
    if cmp.get("basis") == "best_feasible":
        if after.get("point_id") != (summary.get("best") or {}).get("point_id"):
            fails.append("对比的「优化后」不是最优可行点")
        if cmp.get("adopted") is not False:
            fails.append("未采用时对比应标 adopted=False: %r"
                         % (cmp.get("adopted"),))

    # 数值与点本身一致（质量取自 objective_value，最小 MS 取自约束值）
    if not _near(cmp.get("mass_before"), baseline.get("objective_value")):
        fails.append("mass_before 与基线点目标值不一致: %r != %r"
                     % (cmp.get("mass_before"), baseline.get("objective_value")))
    if after and not _near(cmp.get("mass_after"), after.get("objective_value")):
        fails.append("mass_after 与选中点目标值不一致: %r != %r"
                     % (cmp.get("mass_after"), after.get("objective_value")))

    base_ms = ((baseline.get("constraints") or {}).get("values") or {}).get("min_ms")
    if not _near(cmp.get("min_ms_before"), base_ms):
        fails.append("min_ms_before 与基线点约束值不一致: %r != %r"
                     % (cmp.get("min_ms_before"), base_ms))

    # 变化量按定义算：delta = after - before；降幅 = (before-after)/before*100
    if cmp.get("mass_after") is not None and cmp.get("mass_before"):
        want_delta = float(cmp["mass_after"]) - float(cmp["mass_before"])
        if not _near(cmp.get("mass_delta"), want_delta):
            fails.append("mass_delta 不符: %r != %r"
                         % (cmp.get("mass_delta"), want_delta))
        want_pct = (float(cmp["mass_before"]) - float(cmp["mass_after"])) \
            / float(cmp["mass_before"]) * 100.0
        if not _near(cmp.get("mass_reduction_pct"), want_pct, 1e-7):
            fails.append("mass_reduction_pct 不符: %r != %r"
                         % (cmp.get("mass_reduction_pct"), want_pct))
    if cmp.get("min_ms_after") is not None and base_ms is not None:
        want = float(cmp["min_ms_after"]) - float(base_ms)
        if not _near(cmp.get("min_ms_delta"), want, 1e-7):
            fails.append("min_ms_delta 不符: %r != %r"
                         % (cmp.get("min_ms_delta"), want))

    # 可读渲染：质量降幅与 MS 变化都要出现
    text = cmp.get("text") or ""
    if not text.strip():
        fails.append("对比结果没有可读渲染")
    for frag in ("优化前", "优化后", "质量"):
        if frag not in text:
            fails.append("对比文本缺少 %r" % (frag,))

    # 采用之后，「优化后」切换成被采用的那个点（不再是建议）
    rec = summary.get("recommendation") or {}
    if rec.get("point_id") is not None:
        svc.adopt_optimization_point(sid, rec["point_id"],
                                    role=ws.OPTIMIZE_ROLE_SIGNER,
                                    operator="张工", rationale="M28 对比测试")
        cmp2 = svc.get_optimization_comparison(sid)
        if cmp2.get("basis") != "adopted":
            fails.append("采用后对比依据应变为 adopted，实际 %r"
                         % (cmp2.get("basis"),))
        if cmp2.get("adopted") is not True:
            fails.append("采用后对比应标 adopted=True")
        if (cmp2.get("after") or {}).get("point_id") != rec["point_id"]:
            fails.append("采用后「优化后」应是被采用的点")

    # 没有基线点的会话（手工加点）-> 明确说明无法对比，而不是编数字
    sid_empty = "OPT-M28-D2"
    optloop.create_session(svc.conn, DV_SPEC, intent=INTENT, budget=BUDGET,
                           name="M28 无基线", session_id=sid_empty)
    cmp3 = svc.get_optimization_comparison(sid_empty)
    if cmp3.get("basis") != "none":
        fails.append("没有设计点时应明确 basis=none，实际 %r"
                     % (cmp3.get("basis"),))
    if cmp3.get("mass_delta") is not None:
        fails.append("没有设计点时不应编造质量变化量: %r"
                     % (cmp3.get("mass_delta"),))


# ---------------------------------------------------------------------------
# 5) 采用门控（服务层断言）
# ---------------------------------------------------------------------------

def test_adopt_gate(fails, svc, tmp):
    """5) 采用入口只对已批准；未批准一律拒绝（服务层）。"""
    sid = "OPT-M28-E"
    summary = _run_session(svc, tmp, sid)
    rec = summary.get("recommendation") or {}
    if rec.get("point_id") is None:
        fails.append("前置条件不满足：没有可建议采用的可行点")
        return

    # ---- 未批准前：取采用方案必须**明确拒绝**，而不是返回空值 ----
    _expect_service_error(fails,
                          lambda: svc.require_adopted_optimization(sid),
                          "未", "未批准时取采用方案")

    state = svc.get_optimization_adoption(sid)
    if state.get("decided") is not False:
        fails.append("未批准时采用状态应为 decided=False: %r" % (state,))
    if state.get("point_id") is not None:
        fails.append("未批准时不应有采用点: %r" % (state.get("point_id"),))

    # 建议本身必须**标明待批准**（建议 ≠ 已采用，ADR-0002 的 L2 门）
    if rec.get("requires_approval") is not True:
        fails.append("建议采用点未标明 requires_approval（建议不能冒充决定）")
    if rec.get("approved") is not False:
        fails.append("未批准时建议不能标记为 approved")

    # ---- UI 置灰用的判据：给 (ok, reason)，未批准/无权角色都不可采用 ----
    ok, reason = svc.can_adopt_optimization(sid)
    if not ok:
        fails.append("有可行建议点时应该可以采用（只是尚未执行）: %r" % (reason,))

    ok2, reason2 = svc.can_adopt_optimization(sid, role=ws.OPTIMIZE_ROLE_DESIGN)
    if ok2:
        fails.append("「设计」角色不应有权采用优化方案")
    if not reason2:
        fails.append("无权角色的拒绝原因为空（UI 无法显示为什么置灰）")

    # ---- 无权角色执行 -> ServiceError（服务层是合规边界）----
    _expect_service_error(
        fails,
        lambda: svc.adopt_optimization_point(
            sid, rec["point_id"], role=ws.OPTIMIZE_ROLE_DESIGN),
        "无权", "「设计」角色采用优化方案")

    _expect_service_error(
        fails,
        lambda: svc.adopt_optimization_point(
            sid, rec["point_id"], role=ws.OPTIMIZE_ROLE_REVIEWER),
        "无权", "「审核」角色采用优化方案")

    # ---- 领导（签发角色）采用 -> 成功，且审计留痕 ----
    adopted = svc.adopt_optimization_point(
        sid, rec["point_id"], role=ws.OPTIMIZE_ROLE_SIGNER,
        operator="张工", rationale="M28 采用测试：减重且合格")
    if adopted.get("decision") != "adopt":
        fails.append("采用决策未记录: %r" % (adopted,))
    if adopted.get("point_id") != rec["point_id"]:
        fails.append("采用的不是指定的设计点: %r" % (adopted.get("point_id"),))

    plan = svc.require_adopted_optimization(sid)
    if plan.get("point_id") != rec["point_id"]:
        fails.append("取回的采用方案不是被批准的那个点")
    if plan.get("study_id") != rec["study_id"]:
        fails.append("采用方案未带关联 Study")

    # 审计：decisions 表里能查到这次采用（谁、动作、理由）
    hist = svc.get_optimization_decision_history(sid)
    if not hist:
        fails.append("采用决策未写入 L2 审计（decisions 表）")
    else:
        gates = [h.get("gate") for h in hist]
        if "approval_optimize_decision" not in gates:
            fails.append("审计缺 optimize 决策 gate: %r" % (gates,))
        if not any("张工" in (h.get("actor") or "") for h in hist):
            fails.append("审计未记录操作人: %r" % ([h.get("actor") for h in hist],))

    # 采用后列表/汇总里都能看到
    row = [r for r in svc.list_optimization_sessions()
           if r["session_id"] == sid]
    if row and row[0].get("adopted") is not True:
        fails.append("采用后会话列表未标记已采用")
    view = svc.get_optimization_summary(sid)
    if view.get("adopted_point_id") != rec["point_id"]:
        fails.append("汇总视图未反映采用点: %r" % (view.get("adopted_point_id"),))

    # 采用不可行点 -> 拒绝（不能把不合格设计当方案）
    infeasible = [p for p in view["groups"][optloop.GROUP_INFEASIBLE]]
    if infeasible:
        _expect_service_error(
            fails,
            lambda: svc.adopt_optimization_point(
                sid, infeasible[0]["point_id"], role=ws.OPTIMIZE_ROLE_SIGNER),
            "", "采用不可行设计点")

    # 采用不属于该会话的点 -> 拒绝
    _expect_service_error(
        fails,
        lambda: svc.adopt_optimization_point(
            sid, 999999, role=ws.OPTIMIZE_ROLE_SIGNER),
        "", "采用不存在的设计点")


def test_no_feasible_cannot_adopt(fails, svc, tmp):
    """5) 没有可行点时不能采用（只能维持原设计）。"""
    sid = "OPT-M28-F"
    _run_session(svc, tmp, sid, inject_hotspot=True)
    ok, reason = svc.can_adopt_optimization(sid)
    if ok:
        fails.append("没有可行点时不应可以采用: %r" % (reason,))
    if not reason:
        fails.append("没有可行点时的拒绝原因为空")
    _expect_service_error(
        fails,
        lambda: svc.adopt_optimization_point(sid,
                                            role=ws.OPTIMIZE_ROLE_SIGNER),
        "", "没有可行点时采用")


# ---------------------------------------------------------------------------
# 6) 维持原设计
# ---------------------------------------------------------------------------

def test_keep_original(fails, svc, tmp):
    """6) 「维持原设计」可操作、被记录，是一个一等决策。"""
    sid = "OPT-M28-G"
    summary = _run_session(svc, tmp, sid)
    baseline = _baseline_of(summary)
    if baseline is None:
        fails.append("前置条件不满足：没有基线点")
        return

    # 未决策前可以维持原设计（即使有可行建议 —— 收益不大时收尾也是合法决定）
    ok, reason = svc.can_keep_original(sid)
    if not ok:
        fails.append("应可以维持原设计，实际被拒: %r" % (reason,))

    state = svc.keep_original_design(sid, role=ws.OPTIMIZE_ROLE_SIGNER,
                                    operator="李工",
                                    rationale="M28：改进不足，维持现设计")
    if state.get("decision") != "keep_original":
        fails.append("维持原设计的决策未记录: %r" % (state,))
    if state.get("decided") is not True:
        fails.append("维持原设计应标记 decided=True")

    # 维持原设计后，**不能**再取到"采用方案"（那条路必须关闭）
    _expect_service_error(fails,
                          lambda: svc.require_adopted_optimization(sid),
                          "维持原设计", "维持原设计后取采用方案")

    # 采用点为空（维持原设计没有"采用的优化方案"）
    view = svc.get_optimization_summary(sid)
    if view.get("adopted_point_id") is not None:
        fails.append("维持原设计后不应有 adopted_point_id")

    # 审计留痕
    hist = svc.get_optimization_decision_history(sid)
    if not any(h.get("decision") == "keep_original" for h in hist):
        fails.append("维持原设计未写入审计: %r"
                     % ([h.get("decision") for h in hist],))

    # 维持原设计后 can_adopt 应变 False（已定案，不再允许改主意）
    ok2, reason2 = svc.can_adopt_optimization(sid)
    if ok2:
        fails.append("已决定维持原设计后不应再可以采用: %r" % (reason2,))

    # 对比里「优化后」= 基线本身（没有改动）
    cmp = svc.get_optimization_comparison(sid)
    if cmp.get("basis") != "keep_original":
        fails.append("维持原设计后对比应标 basis=keep_original，实际 %r"
                     % (cmp.get("basis"),))
    if cmp.get("mass_delta") is not None and abs(float(cmp["mass_delta"])) > 1e-9:
        fails.append("维持原设计时质量变化应为 0，实际 %r" % (cmp.get("mass_delta"),))


# ---------------------------------------------------------------------------
# 7) 逻辑在服务层（可脱离 UI 单测）
# ---------------------------------------------------------------------------

def test_logic_in_service_layer(fails):
    """
    7) 优化页的全部判据都在服务层：
      * workbench_service 源码不引用 tkinter；
      * 本测试文件除 test_ui_smoke 外不 import tkinter（AST 断言）；
      * 服务层入口返回纯数据（无控件对象）。
    """
    src = inspect.getsource(ws)
    if "tkinter" in src or "tk." in src:
        fails.append("服务层源码出现了 UI 依赖（tkinter/tk.）")

    with open(os.path.abspath(__file__), "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    def _is_tk_import(node):
        if isinstance(node, ast.Import):
            for a in node.names:
                if (a.name or "").split(".")[0] == "tkinter":
                    return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "tkinter":
                return True
        return False

    for node in tree.body:               # 只看顶层语句
        if _is_tk_import(node):
            fails.append("测试文件顶层 import 了 tkinter（逻辑应可脱离 UI 单测）")

    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and any(
                _is_tk_import(sub) for sub in ast.walk(node)):
            bad.append(node.name)
    if bad != ["test_ui_smoke"]:
        fails.append("tkinter 只允许在 test_ui_smoke 里 import，实际出现在 %r"
                     % (bad,))

    # 服务层入口齐全且可调用
    for name in ("list_optimization_sessions", "get_optimization_summary",
                 "get_optimization_point_lineage",
                 "get_optimization_comparison", "can_adopt_optimization",
                 "adopt_optimization_point", "can_keep_original",
                 "keep_original_design", "require_adopted_optimization",
                 "get_optimization_adoption",
                 "get_optimization_decision_history",
                 "format_optimization_summary"):
        fn = getattr(ws.WorkbenchService, name, None)
        if fn is None:
            fails.append("服务层缺少票28 入口 %r" % (name,))
        elif not callable(fn):
            fails.append("服务层入口 %r 不可调用" % (name,))


# ---------------------------------------------------------------------------
# 8) UI 冒烟（构造/销毁 + 采用按钮门控 + 既有两页零回归）
# ---------------------------------------------------------------------------

def test_ui_smoke(fails, tmp):
    """8) UI 冒烟：能构造并销毁；采用按钮按服务层判据置灰；既有两页不破。"""
    try:
        import tkinter
    except ImportError:
        print("  [SKIP] 无 tkinter")
        return

    try:
        probe = tkinter.Tk()
        probe.withdraw()
        probe.destroy()
    except Exception as exc:
        print("  [SKIP] 无显示环境: %s" % exc)
        return

    try:
        import ui_workbench
    except Exception as exc:
        fails.append("ui_workbench import failed: %s" % exc)
        return

    root_dir = _fresh("ui")
    svc = ws.WorkbenchService(root_dir, mode="fake")
    root = None
    try:
        # 准备：一个有待批准建议点的会话 + 一个既有的分析页 Study
        sid = svc.start_study("UI 冒烟 Study")
        svc.run_chain(sid, {"span": 5.0, "sweep_deg": 10.0, "c_root": 1.0,
                           "c_tip": 0.6, "n_ribs": 3, "n_stringers": 4},
                      "wingbox_v1", rules=RULES)
        opt_sid = "OPT-M28-UI"
        _run_session(svc, root_dir, opt_sid, max_evals=2)

        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        app = ui_workbench.WorkbenchApp(root, svc)

        # ---- 既有「分析」页零回归 ----
        if not app.tpl_combo["values"]:
            fails.append("UI：模板未加载（既有分析页被破坏）")
        if not app.param_vars:
            fails.append("UI：参数控件未生成（既有分析页被破坏）")
        if not app.log or not app.lineage:
            fails.append("UI：日志/血缘控件缺失（既有分析页被破坏）")

        # ---- 既有「审批与型号汇总」页零回归 ----
        if not hasattr(app, "approval_tab"):
            fails.append("UI：审批页缺失（票19 功能被破坏）")
        if not hasattr(app, "inbox") or not hasattr(app, "detail"):
            fails.append("UI：审批页的待办/详情控件缺失")
        if not hasattr(app, "rollup_text"):
            fails.append("UI：型号汇总控件缺失")

        # ---- 新增「优化」页 ----
        if app.notebook.index("end") < 3:
            fails.append("UI：缺少「优化」页；应有 分析 / 审批与型号汇总 / 优化 三页")
        tabs = []
        for i in range(app.notebook.index("end")):
            tabs.append(app.notebook.tab(i, "text"))
        for want in ("分析", "审批与型号汇总", "优化"):
            if want not in tabs:
                fails.append("UI：标签页缺少 %r（现有 %r）" % (want, tabs))

        # 会话列表已加载
        if app.opt_session_list.size() < 1:
            fails.append("UI：优化会话列表为空（应列出刚跑的会话）")

        # 选中会话 -> 汇总视图有内容
        app.on_refresh_optimization()
        if app.opt_summary.get("1.0", tk.END).strip() == "":
            fails.append("UI：优化汇总视图为空")
        summary_text = app.opt_summary.get("1.0", tk.END)
        for frag in ("优化会话", "可行", "不可行", "评估失败"):
            if frag not in summary_text:
                fails.append("UI：优化汇总视图缺少 %r" % (frag,))

        # 选中一个设计点 -> 血缘可见
        if app.opt_point_list.size() < 1:
            fails.append("UI：设计点列表为空")
        else:
            app.opt_point_list.selection_set(0)
            app.on_opt_point_select()
            lineage_text = app.opt_detail.get("1.0", tk.END)
            if not lineage_text.strip():
                fails.append("UI：设计点血缘为空")
            for frag in ("血缘", "catpart", "check_result"):
                if frag not in lineage_text:
                    fails.append("UI：设计点血缘缺少 %r" % (frag,))

        # ---- 采用按钮门控（UI 置灰，判据来自服务层）----
        # 默认角色 = 签发：有可行建议 -> 可以采用按钮可用
        app.opt_role_var.set(ui_workbench.OPT_ROLE_CHOICES[0][1])   # 签发
        app.on_opt_role_change()
        if str(app.btn_adopt_opt["state"]) != "normal":
            fails.append("UI：签发角色 + 有可行建议时采用按钮应可用，实际 %r"
                         % (str(app.btn_adopt_opt["state"]),))
        if str(app.btn_keep_opt["state"]) != "normal":
            fails.append("UI：维持原设计按钮应可用")

        # 切成「设计」角色：无权采用 -> 置灰，并给出原因
        app.opt_role_var.set(ui_workbench.OPT_ROLE_CHOICES[1][1])   # 设计
        app.on_opt_role_change()
        if str(app.btn_adopt_opt["state"]) != "disabled":
            fails.append("UI：「设计」角色下采用按钮必须置灰，实际 %r"
                         % (str(app.btn_adopt_opt["state"]),))
        if "无权" not in (app.opt_gate_hint.cget("text") or ""):
            fails.append("UI：置灰时未显示原因（应来自服务层）: %r"
                         % (app.opt_gate_hint.cget("text"),))

        # 切回签发并真正执行采用（不弹窗：直接调服务层，UI 只刷新）
        app.opt_role_var.set(ui_workbench.OPT_ROLE_CHOICES[0][1])
        app.on_opt_role_change()
        rec = svc.get_optimization_summary(opt_sid)["recommendation"]
        if rec.get("point_id") is not None:
            svc.adopt_optimization_point(
                opt_sid, rec["point_id"], role=ws.OPTIMIZE_ROLE_SIGNER,
                operator="UI 冒烟", rationale="UI 冒烟采用")
            app.on_refresh_optimization()
            if str(app.btn_adopt_opt["state"]) != "disabled":
                fails.append("UI：已定案后采用按钮应置灰，实际 %r"
                             % (str(app.btn_adopt_opt["state"]),))
            if "已" not in (app.opt_gate_hint.cget("text") or ""):
                fails.append("UI：已定案后未显示状态说明: %r"
                             % (app.opt_gate_hint.cget("text"),))
    except Exception as exc:
        fails.append("UI 构造/操作失败: %s: %s" % (type(exc).__name__, exc))
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass
        svc.close()


# ---------------------------------------------------------------------------
# 9) Python 3.8 兼容
# ---------------------------------------------------------------------------

def test_py38(fails):
    """9) 本票改动/新增的文件必须 Python 3.8 兼容。"""
    paths = [os.path.join(CORE, "workbench_service.py"),
             os.path.join(WORKBENCH, "ui_workbench.py"),
             os.path.abspath(__file__)]
    violations = compat_py38.check_paths(paths)
    if violations:
        fails.append("Python 3.8 不兼容：\n"
                     + compat_py38.format_violations(violations))


# ---------------------------------------------------------------------------

def main():
    fails = []
    tmp = _fresh("case")
    svc = ws.WorkbenchService(os.path.join(tmp, "svc"), mode="fake")
    try:
        print("1. 会话列表与汇总视图（服务层） ...")
        test_session_list_and_summary(fails, svc, tmp)
        print("2. 三分组：可行 / 不可行 / 评估失败 ...")
        test_groups(fails, svc, tmp)
        print("3. 设计点血缘（点开查「这个数怎么来的」） ...")
        test_point_lineage(fails, svc, tmp)
        print("4. 优化前后对比（质量 / 最小 MS 变化） ...")
        test_comparison(fails, svc, tmp)
        print("5. 采用门控（未批准一律拒绝） ...")
        test_adopt_gate(fails, svc, tmp)
        print("6. 无可行点时不可采用 ...")
        test_no_feasible_cannot_adopt(fails, svc, tmp)
        print("7. 维持原设计（一等决策 + 留痕） ...")
        test_keep_original(fails, svc, tmp)
        print("8. 逻辑在服务层（AST 断言不 import tkinter） ...")
        test_logic_in_service_layer(fails)
        print("9. UI 冒烟（三页共存 + 采用置灰 + 既有两页零回归） ...")
        test_ui_smoke(fails, tmp)
        print("10. Python 3.8 兼容 ...")
        test_py38(fails)
    finally:
        svc.close()

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M28 (票28) SELF-TEST: ALL PASSED "
          "(会话列表+汇总 / 三分组 / 设计点血缘 / 前后对比 / 采用门控 / "
          "维持原设计 / 服务层可单测 / UI 冒烟 / py3.8)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
