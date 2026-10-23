# -*- coding: utf-8 -*-
"""
test_m1.py -- M1 自测：参数契约 / 包络校验 / CATIA(fake) 再生 / 血缘

运行 (在 apps/workbench 下):
    python tests/test_m1.py
不依赖 CATIA/Abaqus 真机。测试库写在仓库内 .tmp_test/。
"""

import os
import sys
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
sys.path.insert(0, CORE)

import db
import study
import template_registry
import run_study
from catia_driver import FakeCatiaDriver


def _fresh_db(name):
    tmp = os.path.join(WORKBENCH, ".tmp_test")
    if not os.path.isdir(tmp):
        os.makedirs(tmp)
    path = os.path.join(tmp, name)
    if os.path.isfile(path):
        os.remove(path)
    return path


def main():
    fails = []
    dbpath = _fresh_db("m1.db")
    conn = db.connect(dbpath)

    # --- 模板加载与契约 ---
    tpl = template_registry.load("wingbox_v1")
    if tpl.id != "wingbox_v1":
        fails.append("template id wrong")
    if "n_ribs" not in tpl.contract():
        fails.append("contract missing n_ribs")
    if 6 not in [p.get("default") for p in tpl.parameters if p["name"] == "n_ribs"]:
        pass

    # --- 校验：合法参数（含默认补全） ---
    ok = tpl.validate({"span": 12.0, "sweep_deg": 25.0, "c_root": 3.0, "c_tip": 1.2})
    if ok["n_ribs"] != 6 or ok["n_stringers"] != 7:
        fails.append("defaults not filled: %r" % ok)

    # --- 校验：未知参数 ---
    try:
        tpl.validate({"span": 12.0, "bogus": 1})
        fails.append("unknown param not rejected")
    except template_registry.ParamError:
        pass

    # --- 校验：越界 ---
    try:
        tpl.validate({"span": 999.0})
        fails.append("range violation not rejected")
    except template_registry.ParamError:
        pass

    # --- 校验：包络约束 c_tip < c_root ---
    try:
        tpl.validate({"c_root": 1.0, "c_tip": 2.0})
        fails.append("envelope constraint c_tip<c_root not enforced")
    except template_registry.ParamError:
        pass

    # --- 编排器：fake 端到端（M1 片段） ---
    study.create_study("S-M1", "M1 测试", conn=conn)
    out_dir = os.path.join(WORKBENCH, ".tmp_test", "catparts")
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    driver = FakeCatiaDriver(out_dir)
    res = run_study.run_study(
        "S-M1",
        {"span": 12.0, "sweep_deg": 25.0, "c_root": 3.0, "c_tip": 1.2, "n_ribs": 6, "n_stringers": 7},
        "wingbox_v1", driver, conn,
        params_dir=os.path.join(WORKBENCH, ".tmp_test", "params"))

    if not os.path.isfile(res["catpart_path"]):
        fails.append("catpart file not produced")
    if len(res["catpart_checksum"]) != 64:
        fails.append("checksum not sha256")

    # 血缘：parameter_set -> catpart 应有边
    ps = res["artifacts"]["parameter_set"]
    ct = res["artifacts"]["catpart"]
    n = conn.execute("SELECT COUNT(*) c FROM edges WHERE parent_id=? AND child_id=?",
                     (ps, ct)).fetchone()["c"]
    if n != 1:
        fails.append("lineage edge parameter_set->catpart missing")

    # 拓扑产物已生成，且血缘 catpart->topology 正确（票 03 接入）
    tp = res["artifacts"].get("topology")
    if tp is None:
        fails.append("topology artifact not created by run_study")
    else:
        n = conn.execute("SELECT COUNT(*) c FROM edges WHERE parent_id=? AND child_id=?",
                         (ct, tp)).fetchone()["c"]
        if n != 1:
            fails.append("lineage edge catpart->topology missing")
        if res["topology"]["n_total"] != 30 or res["topology"]["n_invalid"] != 0:
            fails.append("topology summary wrong: %r" % res["topology"])

    # --- 重跑：新增版本，不覆盖 ---
    res2 = run_study.run_study(
        "S-M1",
        {"span": 13.0, "sweep_deg": 25.0, "c_root": 3.0, "c_tip": 1.2, "n_ribs": 6, "n_stringers": 7},
        "wingbox_v1", driver, conn)
    v_old = conn.execute("SELECT version FROM artifacts WHERE id=?", (ct,)).fetchone()["version"]
    v_new = conn.execute("SELECT version FROM artifacts WHERE id=?",
                         (res2["artifacts"]["catpart"],)).fetchone()["version"]
    if (v_old, v_new) != (1, 2):
        fails.append("rerun should create v2, got %s,%s" % (v_old, v_new))

    # --- CATIA 失败应抛 StudyRunError，且不落 catpart ---
    bad = FakeCatiaDriver(out_dir, fail_on=["span"])
    before = conn.execute("SELECT COUNT(*) c FROM artifacts WHERE study_id='S-M1' AND kind='catpart'").fetchone()["c"]
    try:
        run_study.run_study("S-M1", {"span": 12.0}, "wingbox_v1", bad, conn)
        fails.append("catia failure not raised")
    except run_study.StudyRunError:
        pass
    after = conn.execute("SELECT COUNT(*) c FROM artifacts WHERE study_id='S-M1' AND kind='catpart'").fetchone()["c"]
    if after != before:
        fails.append("catparts should not be added on regeneration failure (param_set may be, catpart must not)")

    conn.close()

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M1 SELF-TEST: ALL PASSED (%s)" % dbpath)
    return 0


if __name__ == "__main__":
    sys.exit(main())
