# -*- coding: utf-8 -*-
"""
test_m6.py -- 票 06 集成：run_study 全链（含强度校核）

fake runner 走完整链：参数 -> 数模 -> 拓扑 -> FE -> 求解 -> 抽取 -> 校核 -> 血缘
可选真机：若探测到 Abaqus，用真机跑一遍全链。

运行 (在 apps/workbench 下): python tests/test_m6.py
"""

import os
import sys
import json
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
REPO = os.path.dirname(os.path.dirname(WORKBENCH))
sys.path.insert(0, CORE)

import db
import study
import run_study
from catia_driver import FakeCatiaDriver
from abaqus_runner import FakeAbaqusRunner, RealAbaqusRunner


PARAMS = {"span": 5.0, "sweep_deg": 10.0, "c_root": 1.0, "c_tip": 0.6,
          "n_ribs": 3, "n_stringers": 4}


def full_chain(runner, tag, fails):
    tmp = os.path.join(WORKBENCH, ".tmp_test", "m6_" + tag)
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)

    conn = db.connect(os.path.join(tmp, "db.sqlite"))
    study.create_study("S-" + tag, "M6 " + tag, conn=conn)

    res = run_study.run_study(
        "S-" + tag, PARAMS, "wingbox_v1",
        FakeCatiaDriver(os.path.join(tmp, "catparts")), conn,
        params_dir=os.path.join(tmp, "params"),
        topo_dir=os.path.join(tmp, "topo"),
        with_fe_model=True, fe_dir=os.path.join(tmp, "fe"),
        subdiv_per_bay=1,
        plies=[{"angle": 0.0, "thickness": 0.1},
               {"angle": 90.0, "thickness": 0.1}],
        abaqus=runner, with_solve=True, job_name="job1",
        solve_dir=os.path.join(tmp, "solve"),
        with_check=True, check_dir=os.path.join(tmp, "check"),
        rules={"criteria": ["tsai_wu", "hashin", "max_stress"],
               "safety_factor": 1.0})

    arts = res["artifacts"]
    expected = ("parameter_set", "catpart", "topology", "mesh", "inp",
                "odb", "extraction", "check_rule", "check_result")
    for k in expected:
        if k not in arts:
            fails.append("%s: artifact %s missing" % (tag, k))

    # 完整血缘链
    chain = [("parameter_set", "catpart"), ("catpart", "topology"),
             ("topology", "mesh"), ("mesh", "inp"), ("inp", "odb"),
             ("odb", "extraction"), ("extraction", "check_result"),
             ("check_rule", "check_result")]
    for parent, child in chain:
        if parent in arts and child in arts:
            n = conn.execute(
                "SELECT COUNT(*) c FROM edges WHERE parent_id=? AND child_id=?",
                (arts[parent], arts[child])).fetchone()["c"]
            if n != 1:
                fails.append("%s: lineage %s->%s missing" % (tag, parent, child))

    # 校核结果合理性
    chk = res.get("check") or {}
    summ = chk.get("summary") or {}
    if summ.get("n_detail_rows", 0) <= 0:
        fails.append("%s: no detail rows" % tag)
    if summ.get("max_fi", 0) <= 0:
        fails.append("%s: max_fi should be > 0" % tag)
    if not chk.get("hotspots"):
        fails.append("%s: no hotspots" % tag)

    # 规则产物版本化：改规则再跑应产生 v2
    res2 = run_study.run_study(
        "S-" + tag, PARAMS, "wingbox_v1",
        FakeCatiaDriver(os.path.join(tmp, "catparts")), conn,
        params_dir=os.path.join(tmp, "params"),
        topo_dir=os.path.join(tmp, "topo"),
        with_fe_model=True, fe_dir=os.path.join(tmp, "fe"),
        subdiv_per_bay=1,
        plies=[{"angle": 0.0, "thickness": 0.1},
               {"angle": 90.0, "thickness": 0.1}],
        abaqus=runner, with_solve=True, job_name="job1",
        solve_dir=os.path.join(tmp, "solve"),
        with_check=True, check_dir=os.path.join(tmp, "check"),
        rules={"criteria": ["max_stress"], "safety_factor": 2.0})

    v1 = conn.execute("SELECT version FROM artifacts WHERE id=?",
                      (arts["check_rule"],)).fetchone()["version"]
    v2 = conn.execute("SELECT version FROM artifacts WHERE id=?",
                      (res2["artifacts"]["check_rule"],)).fetchone()["version"]
    if (v1, v2) != (1, 2):
        fails.append("%s: rerun should create rule v2, got %s,%s" % (tag, v1, v2))

    conn.close()
    return res


def main():
    fails = []
    print("A. fake full chain ...")
    res = full_chain(FakeAbaqusRunner(n_elems=6, n_plies=2), "fake", fails)
    s = (res.get("check") or {}).get("summary") or {}
    print("   rows=%s elems=%s max_FI=%.4g all_pass=%s hotspots=%d"
          % (s.get("n_detail_rows"), s.get("n_elems"), s.get("max_fi", 0),
             s.get("all_pass"), len((res.get("check") or {}).get("hotspots") or [])))

    print("B. real full chain ...")
    try:
        real = RealAbaqusRunner()
    except Exception:
        print("   [SKIP] 未探测到 Abaqus")
    else:
        try:
            res2 = full_chain(real, "real", fails)
            s2 = (res2.get("check") or {}).get("summary") or {}
            print("   rows=%s elems=%s max_FI=%.4g all_pass=%s"
                  % (s2.get("n_detail_rows"), s2.get("n_elems"),
                     s2.get("max_fi", 0), s2.get("all_pass")))
        except Exception as exc:
            fails.append("real full chain failed: %s" % exc)

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M6 (票房06集成) SELF-TEST: ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
