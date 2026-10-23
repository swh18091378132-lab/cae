# -*- coding: utf-8 -*-
"""
test_m11.py -- 票11 验收：拓扑生成策略化（翼面类/筒段类），机翼零回归

验收点（对应 issue #11）：
  1. 模板可声明拓扑模式（翼面类 / 筒段类）及布置规则，新增族无需改拓扑代码
  2. 机翼盒段零回归：自然格数、四类场景的失效格 ID 与数量与原实现逐项一致
  3. 不连续格（长桁终止 / 开口 / 部分肋）识别与标记保持有效
  4. 自然格稳定 ID 可重复（同参数集两次生成一致）
  5. 筒段类拓扑可生成（桁条 × 框围合），有单测覆盖
  6. 纯算法模块，可脱离 CATIA/Abaqus 独立单测
  7. Python 3.8 兼容

运行 (在 apps/workbench 下): python tests/test_m11.py
"""

import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
REPO = os.path.dirname(os.path.dirname(WORKBENCH))
sys.path.insert(0, CORE)

import topology
import template_registry


# P1 基线参数（与 test_m2 完全一致，用于零回归对照）
BASE_PARAMS = {
    "span": 12.0,
    "sweep_deg": 25.0,
    "c_root": 3.0,
    "c_tip": 1.2,
    "n_ribs": 6,
    "n_stringers": 7,
}

# P1 的硬基线：四类场景的失效格 ID 与数量（用基线提交 main 的 topology.py 实测得出，
# 逐项不许变——这是"机翼零回归"的判据）。
P1_BASELINE = {
    "clean": (30, 0, []),
    "runout": (30, 4, ["NB-r3-s2", "NB-r3-s3", "NB-r4-s2", "NB-r4-s3"]),
    "cutout": (30, 1, ["NB-r2-s2"]),
    "partial_rib": (30, 6, ["NB-r1-s0", "NB-r1-s1", "NB-r1-s2",
                            "NB-r2-s0", "NB-r2-s1", "NB-r2-s2"]),
}

CYL_PARAMS = {
    "length": 6.0,
    "radius": 1.5,
    "n_frames": 4,
    "n_stringers": 8,
}


def _ids(model):
    return [b.id for b in model.invalid_bays]


def test_template_declares_mode(fails):
    """1) 模板能声明拓扑模式；未声明时回退翼面类。"""
    tpl = template_registry.load("wingbox_v1")
    rule = topology.topology_rule(template=tpl)
    if rule.get("mode") != topology.MODE_WING_SURFACE:
        fails.append("wingbox_v1 应声明 wing_surface，实际 %r" % rule.get("mode"))

    # 声明缺失 -> 回退默认，且默认就是机翼用的翼面类（P1 零回归的前提）
    rule2 = topology.topology_rule(template={"id": "x", "parameters": []})
    if rule2.get("mode") != topology.MODE_WING_SURFACE:
        fails.append("未声明时默认模式应为 wing_surface")

    # 未知模式必须报错，不能静默走默认
    try:
        topology.topology_rule(template={"id": "y", "topology": {"mode": "bogus"}})
        fails.append("未知拓扑模式未被拒绝")
    except topology.TopologyError:
        pass

    # 登记可用的模式
    modes = topology.layout_modes()
    for m in (topology.MODE_WING_SURFACE, topology.MODE_CYLINDER_SECTION):
        if m not in modes:
            fails.append("布置模式 %r 未登记（现有 %r）" % (m, modes))


def test_wing_zero_regression(fails):
    """2) 机翼盒段四类场景逐项零回归。"""
    # 干净
    m = topology.build_topology("wingbox_v1", BASE_PARAMS)
    total, n_invalid, ids = P1_BASELINE["clean"]
    if len(m.bays) != total:
        fails.append("clean: 候选格应为 %d，实际 %d" % (total, len(m.bays)))
    if len(m.invalid_bays) != n_invalid:
        fails.append("clean: 失效格应为 %d，实际 %d（%r）"
                     % (n_invalid, len(m.invalid_bays), _ids(m)))
    if sorted(_ids(m)) != ids:
        fails.append("clean: 失效格 ID 不符 %r != %r" % (_ids(m), ids))

    # 长桁终止
    m = topology.build_topology("wingbox_v1", BASE_PARAMS,
                                stringer_runout={3: 0.6 * BASE_PARAMS["span"]})
    total, n_invalid, ids = P1_BASELINE["runout"]
    if len(m.bays) != total or len(m.invalid_bays) != n_invalid:
        fails.append("runout: 期望 %d 候选 / %d 失效，实际 %d / %d"
                     % (total, n_invalid, len(m.bays), len(m.invalid_bays)))
    if sorted(_ids(m)) != sorted(ids):
        fails.append("runout: 失效格 ID 不符\n    期望 %r\n    实际 %r"
                     % (sorted(ids), sorted(_ids(m))))
    for bay in m.invalid_bays:
        types = set(r["type"] for r in bay.reasons)
        if topology.REASON_RUNOUT not in types:
            fails.append("runout: 格 %s 缺少 runout 原因" % bay.id)

    # 开口
    m = topology.build_topology(
        "wingbox_v1", BASE_PARAMS,
        holes=[{"cy": 5.3 / BASE_PARAMS["span"], "cxi": 0.42}])
    total, n_invalid, ids = P1_BASELINE["cutout"]
    if len(m.invalid_bays) != n_invalid or sorted(_ids(m)) != sorted(ids):
        fails.append("cutout: 期望 %r，实际 %r" % (ids, _ids(m)))
    for bay in m.invalid_bays:
        if topology.REASON_CUTOUT not in set(r["type"] for r in bay.reasons):
            fails.append("cutout: 格 %s 缺少 cutout 原因" % bay.id)

    # 部分肋
    m = topology.build_topology("wingbox_v1", BASE_PARAMS,
                                partial_ribs={2: (0.4, 1.0)})
    total, n_invalid, ids = P1_BASELINE["partial_rib"]
    if len(m.invalid_bays) != n_invalid or sorted(_ids(m)) != sorted(ids):
        fails.append("partial_rib: 期望 %r，实际 %r" % (sorted(ids), sorted(_ids(m))))
    for bay in m.invalid_bays:
        if topology.REASON_PARTIAL_RIB not in set(r["type"] for r in bay.reasons):
            fails.append("partial_rib: 格 %s 缺少 partial rib 原因" % bay.id)

    # 有效格标记自洽
    if any(b.valid for b in m.invalid_bays):
        fails.append("invalid_bays 里混进了 valid 格")
    if any(not b.valid for b in m.valid_bays):
        fails.append("valid_bays 里混进了 invalid 格")


def test_stable_ids(fails):
    """4) 自然格稳定 ID：同参数集两次生成一致、且唯一。"""
    a = topology.build_topology("wingbox_v1", BASE_PARAMS)
    b = topology.build_topology("wingbox_v1", BASE_PARAMS)
    ids_a = [x.id for x in a.bays]
    ids_b = [x.id for x in b.bays]
    if ids_a != ids_b:
        fails.append("同参数集两次生成的自然格 ID 不一致")
    if len(set(ids_a)) != len(ids_a):
        fails.append("自然格 ID 不唯一")

    # 序列化可往返（血缘产物要落 topology.json）
    data = json.loads(a.to_json())
    if data["n_bays_total"] != 30 or data["n_bays_valid"] != 30:
        fails.append("to_json 计数错误: %r"
                     % {k: data[k] for k in ("n_bays_total", "n_bays_valid")})
    if data.get("topology_mode") != topology.MODE_WING_SURFACE:
        fails.append("to_json 未记录拓扑模式")


def test_cylinder_mode(fails):
    """5) 筒段类拓扑可生成（桁条 × 框围合）。"""
    rule = {"mode": topology.MODE_CYLINDER_SECTION}
    m = topology.build_topology("cyl_test", CYL_PARAMS, rule=rule)

    if m.mode != topology.MODE_CYLINDER_SECTION:
        fails.append("筒段模型未记录模式: %r" % m.mode)

    # 框 = n_frames 根（轴向等分），桁条 = n_stringers 根 + 接缝 1 根
    n_frames = CYL_PARAMS["n_frames"]
    n_str = CYL_PARAMS["n_stringers"]
    if len(m.ribs) != n_frames:
        fails.append("筒段框数应为 %d，实际 %d" % (n_frames, len(m.ribs)))
    if len(m.stringers) != n_str + 1:
        fails.append("筒段桁条数应为 %d（含接缝），实际 %d"
                     % (n_str + 1, len(m.stringers)))

    # 自然格 = (框数-1) × (桁条围合段数) = (n_frames-1) × n_str
    expected_bays = (n_frames - 1) * n_str
    if len(m.bays) != expected_bays:
        fails.append("筒段自然格数应为 %d，实际 %d" % (expected_bays, len(m.bays)))
    if len(m.invalid_bays) != 0:
        fails.append("筒段干净参数不应有失效格: %r" % _ids(m))

    # 框沿轴向等分、桁条沿周向等分
    ys = [r["y"] for r in m.ribs]
    if abs(ys[0]) > 1e-9 or abs(ys[-1] - CYL_PARAMS["length"]) > 1e-9:
        fails.append("筒段框未覆盖全轴向: %r" % ys)
    xis = [s["xi"] for s in m.stringers]
    if abs(xis[0]) > 1e-9 or abs(xis[-1] - 1.0) > 1e-9:
        fails.append("筒段桁条未覆盖全周向: %r" % xis)

    # 开口 -> 不连续格（与翼面类同一套判据）；cxi 取在两根桁条之间
    mh = topology.build_topology(
        "cyl_test", CYL_PARAMS, rule=rule,
        holes=[{"cy": 0.4, "cxi": 0.56}])
    if len(mh.invalid_bays) != 1:
        fails.append("筒段开口应造成 1 个不连续格，实际 %d（%r）"
                     % (len(mh.invalid_bays), _ids(mh)))
    elif topology.REASON_CUTOUT not in set(
            r["type"] for r in mh.invalid_bays[0].reasons):
        fails.append("筒段开口格缺少 cutout 原因")

    # 缺参数要报错（不能静默产出空拓扑）
    try:
        topology.build_topology("cyl_test", {"length": 6.0}, rule=rule)
        fails.append("筒段缺参数 n_frames/n_stringers 未被拒绝")
    except topology.TopologyError:
        pass


def test_new_family_needs_no_topology_code(fails):
    """1)+5) 新增部件族只需登记布置模式 + 写模板，不碰 build_topology。"""

    def _custom_layout(params, rule=None, holes=None, stringer_runout=None,
                       partial_ribs=None):
        # 一个假想的"平板类"：横向 2 根、纵向 2 根，正好 1 个自然格
        rule = rule or {}
        ribs = [{"y": 0.0, "xi0": 0.0, "xi1": 1.0},
                {"y": float(params.get("h", 1.0)), "xi0": 0.0, "xi1": 1.0}]
        stringers = [{"xi": 0.0, "y0": 0.0, "y1": float(params.get("h", 1.0))},
                     {"xi": 1.0, "y0": 0.0, "y1": float(params.get("h", 1.0))}]
        return stringers, ribs, []

    topology.register_layout_mode("plate_test", _custom_layout)
    try:
        m = topology.build_topology("plate_test", {"h": 2.0},
                                    rule={"mode": "plate_test"})
        if len(m.bays) != 1:
            fails.append("新登记模式未能生成自然格（实际 %d）" % len(m.bays))
    finally:
        topology.LAYOUT_MODE_REGISTRY.pop("plate_test", None)

    # 布置规则是纯函数：可以直接调，不碰模板文件/文件系统
    s, r, h = topology.layout_from_params(
        CYL_PARAMS, rule={"mode": topology.MODE_CYLINDER_SECTION})
    if not s or not r:
        fails.append("layout_from_params 直接调用失败")

    # 翼面类规则也可直接调（脱离模板）
    s, r, h = topology.layout_from_params(BASE_PARAMS, rule={"mode": "wing_surface"})
    if len(r) != BASE_PARAMS["n_ribs"] or len(s) != BASE_PARAMS["n_stringers"]:
        fails.append("翼面类纯函数调用结果不符: %d ribs / %d stringers"
                     % (len(r), len(s)))


def test_py38(fails):
    """7) Python 3.8 兼容：本票涉及的模块不得用 3.9+ 语法/标准库。

    用 `core/compat_py38.py` 做静态检查（ast 按 3.8 语法解析 + 3.9+ 用法黑名单），
    而不是粗糙的子串匹配——子串匹配会把 `fe.mesh_dict["n_nodes"]` 里的
    `dict[` 误判成泛型注解。
    """
    import compat_py38

    targets = [
        os.path.join(CORE, "topology.py"),
        os.path.join(CORE, "run_study.py"),
        os.path.join(CORE, "template_registry.py"),
        os.path.join(CORE, "compat_py38.py"),
        os.path.join(HERE, "test_m11.py"),
    ]
    bad = compat_py38.check_paths(targets)
    if bad:
        fails.append("Python 3.8 兼容性检查未通过:\n%s"
                     % compat_py38.format_violations(bad))

    # 检查器自身必须能抓到 3.9+ 用法（否则它只是摆设）
    probe = compat_py38.check_source(
        "probe.py", "x: list[int] = []\ns = 'a'.removeprefix('b')\n")
    if len(probe) < 2:
        fails.append("compat_py38 未检出 list[int] / removeprefix")


def main():
    fails = []
    print("1. 模板声明拓扑模式 ...")
    test_template_declares_mode(fails)
    print("2. 机翼盒段零回归（四类场景） ...")
    test_wing_zero_regression(fails)
    print("3. 自然格稳定 ID ...")
    test_stable_ids(fails)
    print("4. 筒段类拓扑 ...")
    test_cylinder_mode(fails)
    print("5. 新增族无需改拓扑代码 ...")
    test_new_family_needs_no_topology_code(fails)
    print("6. Python 3.8 兼容 ...")
    test_py38(fails)

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M11 (票11) SELF-TEST: ALL PASSED "
          "(wing_surface 零回归 / cylinder_section / 可登记新模式 / 稳定 ID)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
