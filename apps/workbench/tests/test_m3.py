# -*- coding: utf-8 -*-
"""
test_m3.py -- 票 04 验收：FE 模型构建（网格对齐 + 铺层 + .inp + 2.6.2 兼容）

运行 (在 apps/workbench 下): python tests/test_m3.py
"""

import os
import sys
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
import meshgen
import fe_model
import run_study
import fe_build
import compat_check
from catia_driver import FakeCatiaDriver


BASE_PARAMS = {
    "span": 12.0, "sweep_deg": 25.0, "c_root": 3.0, "c_tip": 1.2,
    "n_ribs": 6, "n_stringers": 7,
}


def main():
    fails = []

    # ---- 1) 网格与自然格对齐（ADR-0010） ----
    model = topology.build_topology("wingbox_v1", BASE_PARAMS)
    mesh = meshgen.build_mesh(model, subdiv_per_bay=3)
    ok, problems = meshgen.verify_no_straddle(model, mesh)
    if not ok:
        fails.append("mesh straddles bay boundaries: %s" % problems[:3])

    # 期望单元数 = 自然格数 * 3*3
    expected = len(model.bays) * 9
    if len(mesh.elems) != expected:
        fails.append("expected %d elems, got %d" % (expected, len(mesh.elems)))

    # 每个单元都归属一个已知自然格
    bay_ids = set(b.id for b in model.bays)
    for e in mesh.elems:
        if e["bay_id"] not in bay_ids:
            fails.append("elem %d -> unknown bay %r" % (e["id"], e["bay_id"]))
            break

    # 相邻格共边：节点总数应远小于 (每格独立节点数之和)
    naive = len(model.bays) * (4 ** 2)
    if len(mesh.nodes) >= naive:
        fails.append("shared nodes not deduped (%d >= %d)" % (len(mesh.nodes), naive))

    # ---- 2) 铺层作 CAE 属性 ----
    lam = fe_build.make_laminate([
        {"angle": 0.0, "thickness": 0.125},
        {"angle": 45.0, "thickness": 0.125},
        {"angle": -45.0, "thickness": 0.125},
        {"angle": 90.0, "thickness": 0.125},
    ])
    if lam["n_plies"] != 4:
        fails.append("laminate n_plies wrong")
    if abs(lam["total_thickness"] - 0.5) > 1e-9:
        fails.append("laminate total_thickness wrong: %r" % lam["total_thickness"])

    qi = fe_build.default_quasi_isotropic(n_plies=8)
    if qi["n_plies"] != 8:
        fails.append("quasi-isotropic n_plies wrong")

    # ---- 3) .inp 写出 + 内容检查 ----
    out = os.path.join(WORKBENCH, ".tmp_test", "fe")
    if os.path.isdir(out):
        shutil.rmtree(out)
    fe = fe_model.build_fe_model(model, out, subdiv_per_bay=3,
                                 plies=[{"angle": 0.0, "thickness": 0.2},
                                        {"angle": 90.0, "thickness": 0.2}],
                                 load={"type": "pressure", "magnitude": 0.05})
    if not os.path.isfile(fe.inp_path):
        fails.append(".inp not written")
    else:
        txt = open(fe.inp_path, "r").read()
        for token in ("*HEADING", "*NODE", "*ELEMENT, TYPE=S4R",
                      "*MATERIAL", "*ELASTIC, TYPE=LAMINA",
                      "*SHELL SECTION, ELSET=ALL, COMPOSITE",
                      "*STEP", "*BOUNDARY", "*DLOAD", "*END STEP"):
            if token not in txt:
                fails.append(".inp missing %r" % token)
        # 铺层行数应等于 plies 数
        if txt.count(", 3, CFRP,") != 2:
            fails.append(".inp composite plies lines wrong: %d"
                         % txt.count(", 3, CFRP,"))

    # ---- 4) 2.6.2 兼容 ----
    v = compat_check.check_file(os.path.join(REPO, "abaqus_capabilities", "fe_build.py"))
    if v:
        fails.append("fe_build.py not 2.6.2 compatible: %s" % [str(x) for x in v])

    # ---- 5) 编排器端到端 + 血缘 ----
    dbpath = os.path.join(WORKBENCH, ".tmp_test", "m3.db")
    if os.path.isfile(dbpath):
        os.remove(dbpath)
    conn = db.connect(dbpath)
    study.create_study("S-M3", "M3 测试", conn=conn)

    cpdir = os.path.join(WORKBENCH, ".tmp_test", "m3_catparts")
    if os.path.isdir(cpdir):
        shutil.rmtree(cpdir)
    res = run_study.run_study(
        "S-M3", BASE_PARAMS, "wingbox_v1", FakeCatiaDriver(cpdir), conn,
        params_dir=os.path.join(WORKBENCH, ".tmp_test", "m3_params"),
        topo_dir=os.path.join(WORKBENCH, ".tmp_test", "m3_topo"),
        with_fe_model=True,
        fe_dir=os.path.join(WORKBENCH, ".tmp_test", "m3_fe"),
        subdiv_per_bay=2,
        plies=[{"angle": 0.0, "thickness": 0.2},
               {"angle": 90.0, "thickness": 0.2}])

    arts = res["artifacts"]
    for kind in ("parameter_set", "catpart", "topology", "mesh", "inp"):
        if kind not in arts:
            fails.append("artifact %s missing" % kind)

    # 血缘链参数集->数模->拓扑->网格->inp
    chain = [("parameter_set", "catpart"), ("catpart", "topology"),
             ("topology", "mesh"), ("mesh", "inp")]
    for parent, child in chain:
        if parent in arts and child in arts:
            n = conn.execute(
                "SELECT COUNT(*) c FROM edges WHERE parent_id=? AND child_id=?",
                (arts[parent], arts[child])).fetchone()["c"]
            if n != 1:
                fails.append("lineage edge %s->%s missing" % (parent, child))

    if res["fe"]["n_plies"] != 2:
        fails.append("fe n_plies wrong")
    if res["fe"]["n_elems"] != len(model.bays) * 4:
        fails.append("fe n_elems wrong: %d" % res["fe"]["n_elems"])

    conn.close()

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M3 (票房04) SELF-TEST: ALL PASSED "
          "(mesh aligned, %d elems, layup=%d plies, inp ok, 2.6.2 ok, lineage ok)"
          % (res["fe"]["n_elems"], res["fe"]["n_plies"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
