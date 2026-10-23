# -*- coding: utf-8 -*-
"""
test_m5.py -- 票 06 验收：强度校核引擎

验证：
  1. 准则算法正确性（含手算对照）
  2. 单元 x 铺层 x 准则 遍历
  3. 单元取最差铺层
  4. 自然格聚合取最差单元
  5. 不连续格排除出自然格级、单元级照常
  6. 危险部位定位
  7. 边界：FI=1 / MS=0
  8. 真机数据：解析真实 ODB 抽取结果（若存在）

运行 (在 apps/workbench 下): python tests/test_m5.py
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
sys.path.insert(0, CORE)

import criteria
import strength_check as sc
import topology


def _write_csv(path, rows):
    """rows: [(elem, sp, s11, s22, s12)]"""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["field", "elem", "node", "ip", "sp", "component", "value"])
        for (e, sp, s11, s22, s12) in rows:
            w.writerow(["S", e, "", 1, sp, "c1", "%.9g" % s11])
            w.writerow(["S", e, "", 1, sp, "c2", "%.9g" % s22])
            w.writerow(["S", e, "", 1, sp, "c4", "%.9g" % s12])


def test_criteria(fails):
    al = {"Xt": 1000.0, "Xc": 800.0, "Yt": 50.0, "Yc": 200.0, "S": 100.0}

    # 单轴拉伸刚好到 Xt -> max_stress FI = 1
    fi = criteria.max_stress({"s11": 1000.0, "s22": 0.0, "s12": 0.0}, al)
    if abs(fi - 1.0) > 1e-9:
        fails.append("max_stress: expected FI=1.0, got %r" % fi)

    # 单轴压缩到 Xc -> FI = 1
    fi = criteria.max_stress({"s11": -800.0, "s22": 0.0, "s12": 0.0}, al)
    if abs(fi - 1.0) > 1e-9:
        fails.append("max_stress compression: expected 1.0, got %r" % fi)

    # 纯剪切到 S -> FI = 1
    fi = criteria.max_stress({"s11": 0.0, "s22": 0.0, "s12": 100.0}, al)
    if abs(fi - 1.0) > 1e-9:
        fails.append("max_stress shear: expected 1.0, got %r" % fi)

    # Tsai-Wu：单轴 s11 = Xt 应得 FI = 1
    fi = criteria.tsai_wu({"s11": 1000.0, "s22": 0.0, "s12": 0.0}, al)
    if abs(fi - 1.0) > 1e-6:
        fails.append("tsai_wu at Xt: expected 1.0, got %r" % fi)

    # Tsai-Wu：单轴压缩 s11 = -Xc 应得 FI = 1
    fi = criteria.tsai_wu({"s11": -800.0, "s22": 0.0, "s12": 0.0}, al)
    if abs(fi - 1.0) > 1e-6:
        fails.append("tsai_wu at Xc: expected 1.0, got %r" % fi)

    # Hashin：纤维拉伸 (s11/Xt)^2=1 -> FI=1
    fi = criteria.hashin({"s11": 1000.0, "s22": 0.0, "s12": 0.0}, al)
    if abs(fi - 1.0) > 1e-6:
        fails.append("hashin fiber tension: expected 1.0, got %r" % fi)

    # 单调性：应力增加 FI 应增加
    f1 = criteria.tsai_wu({"s11": 100.0, "s22": 0.0, "s12": 0.0}, al)
    f2 = criteria.tsai_wu({"s11": 500.0, "s22": 0.0, "s12": 0.0}, al)
    if not (f2 > f1):
        fails.append("tsai_wu not monotonic: %r -> %r" % (f1, f2))

    # 未知准则抛错
    try:
        criteria.get("nope")
        fails.append("unknown criterion not rejected")
    except criteria.CriteriaError:
        pass


def test_engine(fails):
    tmp = os.path.join(WORKBENCH, ".tmp_test", "m5")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)

    # 拓扑：3 肋 4 长桁 -> 2x3=6 自然格
    params = {"span": 5.0, "sweep_deg": 10.0, "c_root": 1.0, "c_tip": 0.6,
              "n_ribs": 3, "n_stringers": 4}
    model = topology.build_topology("wingbox_v1", params)

    # 造 6 个单元，每个 2 层；单元 5 的铺层 2 是热点
    rows = []
    for e in range(1, 7):
        for sp in (1, 2):
            base = 50.0 + e * 5
            if e == 5 and sp == 2:
                base = 900.0
            rows.append((e, sp, base, base * 0.1, base * 0.05))
    csv_path = os.path.join(tmp, "ext.csv")
    _write_csv(csv_path, rows)

    rules = sc.load_rules(overrides={
        "criteria": ["tsai_wu"],
        "safety_factor": 1.0,
        "allowables": {"Xt": 1500.0, "Xc": 1200.0, "Yt": 60.0,
                       "Yc": 200.0, "S": 80.0},
    })
    stress = sc.read_extraction(csv_path, rules)
    if len(stress) != 12:
        fails.append("read_extraction: expected 12 (elem,sp) keys, got %d"
                     % len(stress))

    # 每个单元归属一个自然格（按序分配，模拟 bay_of_elem）
    bay_ids = [b.id for b in model.bays]
    bay_of_elem = {}
    for i, e in enumerate(range(1, 7)):
        bay_of_elem[e] = bay_ids[i % len(bay_ids)]

    res = sc.check(stress, rules, model, bay_of_elem)

    # 明细行数 = 单元x铺层x准则 = 6*2*1 = 12
    if len(res["detail_rows"]) != 12:
        fails.append("detail rows: expected 12, got %d" % len(res["detail_rows"]))

    # 单元取最差铺层：单元5 的最差 sp 应为 2
    if res["elem_best"].get(5, {}).get("sp") != 2:
        fails.append("elem 5 worst ply should be sp=2, got %r"
                     % res["elem_best"].get(5))

    # 危险部位第一名应是单元5
    if not res["hotspots"] or res["hotspots"][0]["elem"] != 5:
        fails.append("hotspot #1 should be elem 5, got %r"
                     % (res["hotspots"][:1]))

    # 自然格聚合：应小于等于单元数
    if len(res["bay_best"]) > len(bay_ids):
        fails.append("bay_best exceeds bay count")

    # 每个格的值应等于该格内最差单元
    for bay_id, b in res["bay_best"].items():
        members = [e for e, bid in bay_of_elem.items() if bid == bay_id]
        if members:
            worst = max(res["elem_best"][e]["fi"] for e in members
                        if e in res["elem_best"])
            if abs(b["fi"] - worst) > 1e-9:
                fails.append("bay %s aggregation wrong: %r vs %r"
                             % (bay_id, b["fi"], worst))

    # 输出文件
    csv_out, json_out = sc.write_outputs(res, tmp)
    if not os.path.isfile(csv_out) or not os.path.isfile(json_out):
        fails.append("outputs not written")
    else:
        with open(json_out, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if "summary" not in payload or "hotspots" not in payload:
            fails.append("summary json missing keys")


def test_invalid_bays(fails):
    """不连续格：排除自然格级，单元级照常。"""
    tmp = os.path.join(WORKBENCH, ".tmp_test", "m5b")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)

    params = {"span": 12.0, "sweep_deg": 25.0, "c_root": 3.0, "c_tip": 1.2,
              "n_ribs": 6, "n_stringers": 7}
    # 制造 runout：索引3长桁只到60%展长 -> 4 个不连续格
    model = topology.build_topology(
        "wingbox_v1", params, stringer_runout={3: 0.6 * params["span"]})
    if len(model.invalid_bays) != 4:
        fails.append("setup: expected 4 invalid bays, got %d"
                     % len(model.invalid_bays))

    invalid_ids = set(b.id for b in model.invalid_bays)
    rows = []
    for e in range(1, 5):
        rows.append((e, 1, 100.0, 10.0, 5.0))
    csv_path = os.path.join(tmp, "ext2.csv")
    _write_csv(csv_path, rows)

    rules = sc.load_rules(overrides={"criteria": ["max_stress"]})
    stress = sc.read_extraction(csv_path, rules)

    # 把单元1 映射到一个不连续格，单元2 映射到有效格
    valid_ids = [b.id for b in model.valid_bays]
    bay_of_elem = {1: list(invalid_ids)[0], 2: valid_ids[0],
                   3: list(invalid_ids)[1], 4: valid_ids[1]}

    res = sc.check(stress, rules, model, bay_of_elem)

    # 不连续格不应出现在 bay_best
    for bad in invalid_ids:
        if bad in res["bay_best"]:
            fails.append("invalid bay %s should be excluded from bay_best" % bad)

    # 但单元级必须都有
    if len(res["elem_best"]) != 4:
        fails.append("elem level should still cover all 4 elems, got %d"
                     % len(res["elem_best"]))

    if res["summary"]["n_bays_excluded"] != 4:
        fails.append("n_bays_excluded should be 4, got %r"
                     % res["summary"]["n_bays_excluded"])


def test_real_data(fails):
    """若已有真机抽取结果（test_m4 产出），用它跑一遍。"""
    p = os.path.join(WORKBENCH, ".tmp_test", "m4real", "solve", "realjob_elem_ply.csv")
    if not os.path.isfile(p):
        print("  [SKIP] 无真机抽取结果（先跑 test_m4）")
        return

    rules = sc.load_rules()
    stress = sc.read_extraction(p, rules)
    if not stress:
        fails.append("real data: no stress parsed")
        return

    res = sc.check(stress, rules, None, None)
    if not res["detail_rows"]:
        fails.append("real data: no detail rows")
    if res["summary"]["max_fi"] <= 0:
        fails.append("real data: max_fi should be > 0")
    print("  [REAL] parsed %d (elem,sp) keys, %d detail rows, max_FI=%.4g, all_pass=%s"
          % (len(stress), len(res["detail_rows"]),
             res["summary"]["max_fi"], res["summary"]["all_pass"]))


def main():
    fails = []
    print("1. criteria ...")
    test_criteria(fails)
    print("2. engine ...")
    test_engine(fails)
    print("3. invalid bays ...")
    test_invalid_bays(fails)
    print("4. real data ...")
    test_real_data(fails)

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M5 (票房06) SELF-TEST: ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
