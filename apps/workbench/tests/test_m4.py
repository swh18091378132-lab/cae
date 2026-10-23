# -*- coding: utf-8 -*-
"""
test_m4.py -- 票 05 验收：Abaqus 求解与结果抽取

两级验证：
  A. Fake runner：确定性回归（任意环境可跑）
  B. Real runner：真机 Abaqus 2023（若探测到才跑，否则 SKIP）

运行 (在 apps/workbench 下): python tests/test_m4.py
"""

import os
import sys
import csv
import json
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
REPO = os.path.dirname(os.path.dirname(WORKBENCH))
for p in (CORE, os.path.join(REPO, "abaqus_capabilities")):
    if p not in sys.path:
        sys.path.insert(0, p)

import db
import study
import topology
import fe_model
import run_study
import compat_check
from catia_driver import FakeCatiaDriver
from abaqus_runner import FakeAbaqusRunner, RealAbaqusRunner


BASE_PARAMS = {
    "span": 5.0, "sweep_deg": 10.0, "c_root": 1.0, "c_tip": 0.6,
    "n_ribs": 3, "n_stringers": 4,
}


def _fresh(path):
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path)
    return path


def test_fake(fails):
    tmp = os.path.join(WORKBENCH, ".tmp_test", "m4")
    _fresh(tmp)

    dbpath = os.path.join(tmp, "m4.db")
    conn = db.connect(dbpath)
    study.create_study("S-M4", "M4 fake", conn=conn)

    res = run_study.run_study(
        "S-M4", BASE_PARAMS, "wingbox_v1",
        FakeCatiaDriver(os.path.join(tmp, "catparts")), conn,
        params_dir=os.path.join(tmp, "params"),
        topo_dir=os.path.join(tmp, "topo"),
        with_fe_model=True, fe_dir=os.path.join(tmp, "fe"),
        subdiv_per_bay=1,
        plies=[{"angle": 0.0, "thickness": 0.1},
               {"angle": 90.0, "thickness": 0.1}],
        abaqus=FakeAbaqusRunner(n_elems=6, n_plies=2),
        with_solve=True, job_name="fake1", solve_dir=os.path.join(tmp, "solve"))

    arts = res["artifacts"]
    for kind in ("inp", "odb", "extraction"):
        if kind not in arts:
            fails.append("fake: artifact %s missing" % kind)

    # 血缘 inp->odb->extraction
    for parent, child in (("inp", "odb"), ("odb", "extraction")):
        if parent in arts and child in arts:
            n = conn.execute(
                "SELECT COUNT(*) c FROM edges WHERE parent_id=? AND child_id=?",
                (arts[parent], arts[child])).fetchone()["c"]
            if n != 1:
                fails.append("fake: lineage %s->%s missing" % (parent, child))

    # manifest 口径齐全
    man = res["extraction"]["manifest"]
    for k in ("source_odb_sha256", "n_section_points", "position",
              "python_version", "units_note", "csv"):
        if k not in man:
            fails.append("fake: manifest missing %r" % k)
    if man["n_section_points"] != 2:
        fails.append("fake: section points wrong")

    # CSV 可解析，且含 S/U
    csv_path = res["extraction"]["csv_path"]
    if not os.path.isfile(csv_path):
        fails.append("fake: csv missing")
    else:
        with open(csv_path, "r", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            fails.append("fake: csv empty")
        fields = set(r["field"] for r in rows)
        if "S" not in fields:
            fails.append("fake: csv missing S field")
    conn.close()


def test_compat(fails):
    for fn in ("extract.py", "fe_build.py"):
        p = os.path.join(REPO, "abaqus_capabilities", fn)
        v = compat_check.check_file(p)
        if v:
            fails.append("%s not 2.7-compatible: %s" % (fn, [str(x) for x in v]))


def test_real(fails):
    """真机：只有探测到 Abaqus 才跑。"""
    try:
        runner = RealAbaqusRunner()
    except Exception:
        print("  [SKIP] 未探测到 Abaqus，跳过真机测试")
        return

    tmp = os.path.join(WORKBENCH, ".tmp_test", "m4real")
    _fresh(tmp)

    # 直接用已生成的拓扑->FE 产物，跑一次小模型真机求解
    model = topology.build_topology("wingbox_v1", BASE_PARAMS)
    fe = fe_model.build_fe_model(
        model, os.path.join(tmp, "fe"), subdiv_per_bay=1,
        plies=[{"angle": 0.0, "thickness": 0.1},
               {"angle": 90.0, "thickness": 0.1}],
        load={"type": "pressure", "magnitude": 0.1})

    sol = runner.solve(fe.inp_path, "realjob", os.path.join(tmp, "solve"))
    if sol.status != "COMPLETED":
        fails.append("real: solve failed: %s\n%s" % (sol.status, sol.log_tail))
        return

    ext = runner.extract(sol.odb_path, os.path.join(tmp, "solve"))
    man = ext.manifest
    if not os.path.isfile(ext.csv_path):
        fails.append("real: csv missing")
    if man.get("n_section_points", 0) < 2:
        fails.append("real: section points < 2 (%r)" % man.get("n_section_points"))
    if not man.get("source_odb_sha256") or len(man["source_odb_sha256"]) != 64:
        fails.append("real: odb sha256 invalid")
    if man.get("python_version", "").split(".")[0] != "2":
        fails.append("real: expected py2 in Abaqus, got %r" % man.get("python_version"))
    print("  [REAL] solve=COMPLETED, rows=%s, section_points=%s, py=%s"
          % (man.get("n_rows"), man.get("section_points"),
             man.get("python_version")))


def main():
    fails = []
    print("A. fake runner ...")
    test_fake(fails)
    print("B. compat check ...")
    test_compat(fails)
    print("C. real abaqus ...")
    test_real(fails)

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M4 (票房05) SELF-TEST: ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
