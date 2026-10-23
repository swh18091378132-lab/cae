# -*- coding: utf-8 -*-
"""
test_m2.py -- 票 03 验收：自然格生成 + 不连续格标记

基线对齐原型 prototype/natural-bay（展向 6 肋 / 弦向 7 长桁 → 5x6=30 候选格）：
  A 干净         -> 0 失效
  B 长桁终止     -> 4 格失效（中间 xi=0.5 只到 60% 展长）
  C 开口         -> 1 格失效
  D 部分肋       -> 6 格失效（第 3 根肋只从 xi=0.4 延伸到后梁）

运行 (在 apps/workbench 下): python tests/test_m2.py
"""

import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
sys.path.insert(0, CORE)

import topology


BASE_PARAMS = {
    "span": 12.0,
    "sweep_deg": 25.0,
    "c_root": 3.0,
    "c_tip": 1.2,
    "n_ribs": 6,
    "n_stringers": 7,
}


def main():
    fails = []

    # ---- A 干净 ----
    a = topology.build_topology("wingbox_v1", BASE_PARAMS)
    if len(a.bays) != 30:
        fails.append("A: expected 30 candidate bays, got %d" % len(a.bays))
    if len(a.invalid_bays) != 0:
        fails.append("A: expected 0 invalid, got %d [%s]"
                     % (len(a.invalid_bays), [b.id for b in a.invalid_bays]))

    # ---- B 长桁终止：索引 3 的 xi=0.5 长桁只到 60% 展长 ----
    b = topology.build_topology(
        "wingbox_v1", BASE_PARAMS,
        stringer_runout={3: 0.6 * BASE_PARAMS["span"]})
    if len(b.bays) != 30:
        fails.append("B: expected 30 candidates, got %d" % len(b.bays))
    if len(b.invalid_bays) != 4:
        fails.append("B: expected 4 invalid (runout), got %d -> %s"
                     % (len(b.invalid_bays), [x.id for x in b.invalid_bays]))
    for bay in b.invalid_bays:
        types = set(r["type"] for r in bay.reasons)
        if topology.REASON_RUNOUT not in types:
            fails.append("B: bay %s missing runout reason: %s" % (bay.id, bay.reasons))

    # ---- C 开口：展向 5.3/12, 弦向 0.42 ----
    c = topology.build_topology(
        "wingbox_v1", BASE_PARAMS,
        holes=[{"cy": 5.3 / BASE_PARAMS["span"], "cxi": 0.42}])
    if len(c.invalid_bays) != 1:
        fails.append("C: expected 1 invalid (cutout), got %d -> %s"
                     % (len(c.invalid_bays), [x.id for x in c.invalid_bays]))
    if c.invalid_bays:
        types = set(r["type"] for r in c.invalid_bays[0].reasons)
        if topology.REASON_CUTOUT not in types:
            fails.append("C: missing cutout reason: %s" % c.invalid_bays[0].reasons)

    # ---- D 部分肋：第 3 根肋(index 2) 只从 xi=0.4 到后梁 ----
    d = topology.build_topology(
        "wingbox_v1", BASE_PARAMS,
        partial_ribs={2: (0.4, 1.0)})
    if len(d.invalid_bays) != 6:
        fails.append("D: expected 6 invalid (partial rib), got %d -> %s"
                     % (len(d.invalid_bays), [x.id for x in d.invalid_bays]))
    for bay in d.invalid_bays:
        types = set(r["type"] for r in bay.reasons)
        if topology.REASON_PARTIAL_RIB not in types:
            fails.append("D: bay %s missing partial rib reason" % bay.id)

    # ---- 稳定 ID：同参数集重复生成一致 ----
    a2 = topology.build_topology("wingbox_v1", BASE_PARAMS)
    ids1 = [x.id for x in a.bays]
    ids2 = [x.id for x in a2.bays]
    if ids1 != ids2:
        fails.append("IDs not stable across runs")
    if len(set(ids1)) != len(ids1):
        fails.append("Bay IDs not unique")

    # ---- 序列化可往返 ----
    js = a.to_json()
    data = json.loads(js)
    if data["n_bays_total"] != 30 or data["n_bays_valid"] != 30:
        fails.append("to_json counts wrong: %r" % {k: data[k] for k in
                     ("n_bays_total", "n_bays_valid")})
    if len(data["bays"]) != 30:
        fails.append("to_json bays length wrong")

    # ---- 有效格不参与不连续 ----
    if any(x.valid for x in d.invalid_bays):
        fails.append("invalid list contains valid bay")

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M2 (票房03) SELF-TEST: ALL PASSED "
          "(A=30/0, B=4 runout, C=1 cutout, D=6 partial-rib)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
