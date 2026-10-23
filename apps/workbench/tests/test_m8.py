# -*- coding: utf-8 -*-
"""
test_m8.py -- 票 08 验收：端到端编排与 UI

策略（关键设计）：**测服务层，不测 Tk 控件**。
workbench_service 承载全部编排逻辑且是纯 Python，可像其他票一样确定性回归。
UI 只做冒烟：能起窗口、能自动关闭、不崩。

运行 (在 apps/workbench 下): python tests/test_m8.py
"""

import os
import sys
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
sys.path.insert(0, CORE)
sys.path.insert(0, WORKBENCH)

import workbench_service as ws


PARAMS = {"span": 5.0, "sweep_deg": 10.0, "c_root": 1.0, "c_tip": 0.6,
          "n_ribs": 3, "n_stringers": 4}


def _fresh(name):
    d = os.path.join(WORKBENCH, ".tmp_test", name)
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d)
    return d


def test_templates(svc, fails):
    tpls = svc.list_templates()
    if "wingbox_v1" not in tpls:
        fails.append("wingbox_v1 not listed: %s" % tpls)
    tpl = svc.get_template("wingbox_v1")
    names = [p["name"] for p in tpl.parameters]
    for k in ("span", "n_ribs", "n_stringers"):
        if k not in names:
            fails.append("template missing param %s" % k)


def test_start_study(svc, fails):
    sid = svc.start_study("测试分析")
    if not sid:
        fails.append("start_study returned empty")
    studies = svc.list_studies()
    if not any(s["id"] == sid for s in studies):
        fails.append("new study not listed")
    # 重复 id 应复用而不抛
    sid2 = svc.start_study("另一个", study_id=sid)
    if sid2 != sid:
        fails.append("explicit study_id not honored")


def test_full_chain(svc, fails):
    sid = svc.start_study("全链测试")
    res = svc.run_chain(sid, PARAMS, "wingbox_v1",
                        rules={"criteria": ["tsai_wu"]})

    arts = res.get("artifacts") or {}
    for kind in ("parameter_set", "catpart", "topology", "mesh", "inp",
                 "odb", "extraction", "check_rule", "check_result", "report"):
        if kind not in arts:
            fails.append("chain: artifact %s missing" % kind)

    chk = res.get("check") or {}
    summ = chk.get("summary") or {}
    if summ.get("n_detail_rows", 0) <= 0:
        fails.append("chain: no check detail rows")
    if not chk.get("hotspots"):
        fails.append("chain: no hotspots")

    rep = res.get("report") or {}
    if not rep.get("path") or not os.path.isfile(rep["path"]):
        fails.append("chain: report missing")
    else:
        if os.path.getsize(rep["path"]) < 5000:
            fails.append("chain: report too small")

    return sid, res


def test_lineage(svc, sid, fails):
    lin = svc.get_lineage(sid)
    nodes = lin.get("nodes") or []
    if len(nodes) < 10:
        fails.append("lineage: expected >=10 nodes, got %d" % len(nodes))
    if len(lin.get("order") or []) != len(nodes):
        fails.append("lineage: order length mismatch")

    by_kind = {}
    for n in nodes:
        by_kind.setdefault(n["kind"], []).append(n)

    # 关键父子关系抽查
    def _find(kind):
        lst = by_kind.get(kind) or []
        return lst[-1] if lst else None

    pairs = [("catpart", "topology"), ("topology", "mesh"), ("mesh", "inp"),
             ("inp", "odb"), ("odb", "extraction"),
             ("extraction", "check_result"), ("check_result", "report")]
    for parent_kind, child_kind in pairs:
        p = _find(parent_kind)
        c = _find(child_kind)
        if p is None or c is None:
            fails.append("lineage: missing %s or %s" % (parent_kind, child_kind))
            continue
        if p["id"] not in c["parents"]:
            fails.append("lineage: %s should be parent of %s"
                         % (parent_kind, child_kind))

    txt = svc.format_lineage_text(sid)
    if "report" not in txt:
        fails.append("lineage text missing report")
    if sid not in txt:
        fails.append("lineage text missing study id")


def test_rerun_new_version(svc, sid, fails):
    before = svc.get_lineage(sid)["nodes"]
    n_before = len([n for n in before if n["kind"] == "report"])

    svc.run_chain(sid, PARAMS, "wingbox_v1", rules={"criteria": ["hashin"]})

    after = svc.get_lineage(sid)["nodes"]
    reports = [n for n in after if n["kind"] == "report"]
    if len(reports) != n_before + 1:
        fails.append("rerun: expected %d report versions, got %d"
                     % (n_before + 1, len(reports)))
    if reports and reports[-1]["version"] < 2:
        fails.append("rerun: report version should increment")


def test_report_path(svc, sid, fails):
    p = svc.get_report_path(sid)
    if not p or not os.path.isfile(p):
        fails.append("get_report_path returned invalid: %r" % p)
    # 不存在的 study
    if svc.get_report_path("NO-SUCH-STUDY") is not None:
        fails.append("get_report_path should return None for unknown study")


def test_errors(svc, fails):
    # 模板不存在
    sid = svc.start_study("错误测试")
    try:
        svc.run_chain(sid, PARAMS, "no_such_template")
        fails.append("unknown template not rejected")
    except ws.ServiceError:
        pass

    # 参数越界
    try:
        svc.run_chain(sid, {"span": 9999.0}, "wingbox_v1")
        fails.append("out-of-envelope param not rejected")
    except ws.ServiceError:
        pass

    # 无报告时 open_report 应报错
    sid2 = svc.start_study("空 Study")
    try:
        svc.open_report(sid2)
        fails.append("open_report on empty study should raise")
    except ws.ServiceError:
        pass


def test_ui_smoke(fails):
    """UI 冒烟：能建窗口并自动关闭。"""
    try:
        import tkinter
    except ImportError:
        print("  [SKIP] 无 tkinter")
        return

    try:
        r = tkinter.Tk()
        r.withdraw()
        r.destroy()
    except Exception as exc:
        print("  [SKIP] 无显示环境: %s" % exc)
        return

    try:
        import ui_workbench
    except Exception as exc:
        fails.append("ui_workbench import failed: %s" % exc)
        return

    root_dir = _fresh("m8ui")
    try:
        # 直接构造 App（不进 mainloop），验证控件能创建
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        svc = ws.WorkbenchService(root_dir, mode="fake")
        app = ui_workbench.WorkbenchApp(root, svc)
        # 模板已加载
        if not app.tpl_combo["values"]:
            fails.append("UI: no templates loaded")
        # 参数控件已生成
        if not app.param_vars:
            fails.append("UI: no param vars built")
        root.destroy()
        svc.close()
    except Exception as exc:
        fails.append("UI construct failed: %s" % exc)


def main():
    fails = []
    root = _fresh("m8")
    svc = ws.WorkbenchService(root, mode="fake")

    print("1. templates ...")
    test_templates(svc, fails)
    print("2. start study ...")
    test_start_study(svc, fails)
    print("3. full chain ...")
    sid, res = test_full_chain(svc, fails)
    print("4. lineage ...")
    test_lineage(svc, sid, fails)
    print("5. rerun -> new version ...")
    test_rerun_new_version(svc, sid, fails)
    print("6. report path ...")
    test_report_path(svc, sid, fails)
    print("7. errors ...")
    test_errors(svc, fails)
    print("8. ui smoke ...")
    test_ui_smoke(fails)

    svc.close()

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M8 (票房08) SELF-TEST: ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
