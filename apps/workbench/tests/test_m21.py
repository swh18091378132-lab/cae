# -*- coding: utf-8 -*-
"""
test_m21.py -- 票21 验收：objectives（质量目标与约束口径，纯算法）

验收点（对应 issue #21）：
  1. 给定已知几何与铺层，质量可手算对照（含容差）
  2. 质量使用物理尺度换算（y 已是米；xi 必须乘 xi_scale），
     **不把参数空间 0..1 当物理长度** —— 用 xi_scale ≠ 1 的用例断言
  3. 质量结果自带 components 声明，且与实际计入项一致
  4. 未声明加筋截面假设时只算蒙皮并明确标注；声明后才计入加筋
  5. 约束口径复用既有强度校核结果（max_fi / min_ms / all_pass /
     n_bays_excluded），不另起判定
  6. **技术故障与设计不可行在数据结构上可区分**
     （ok/error/feasible 的组合不同，优化器不会把故障读成"设计很差"）
  7. 纯算法模块，不依赖 CATIA / Abaqus / DB，可独立单测
  8. Python 3.8 兼容

运行 (在 apps/workbench 下): python tests/test_m21.py
"""

import os
import sys
import json
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
sys.path.insert(0, CORE)

import topology
import objectives


# 手算用几何：span=6.0 m / c_root=2.0 m / 3 肋 / 2 长桁
#   -> 2 个自然格，每格 y 跨 3.0 m、xi 跨 0..1（物理弦向 = 1.0 * c_root = 2.0 m）
#   -> 每格面积 3.0 * 2.0 = 6.0 m2，总面积 12.0 m2
HAND_PARAMS = {
    "span": 6.0, "sweep_deg": 10.0, "c_root": 2.0, "c_tip": 1.0,
    "n_ribs": 3, "n_stringers": 2,
}

# 铺层总厚 0.2（平台口径：铺层厚度以 mm 计，见 fe_build 默认单层 0.125 mm）
HAND_LAMINATE = {"plies": [{"angle": 0.0, "thickness": 0.1},
                           {"angle": 90.0, "thickness": 0.1}],
                 "n_plies": 2,
                 "total_thickness": 0.2}

DENSITY = 1600.0        # kg/m3（CFRP）

# 手算期望：面积 12.0 m2 × 厚度 0.2 mm(=2e-4 m) × 1600 kg/m3 = 3.84 kg
HAND_AREA = 12.0
HAND_MASS = HAND_AREA * (0.2 * 1e-3) * DENSITY


def _fresh(name):
    d = os.path.join(WORKBENCH, ".tmp_test", "m21", name)
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d)
    return d


def _close(a, b, tol=1e-9):
    return abs(float(a) - float(b)) <= tol


def _wing(span=6.0, c_root=2.0, n_ribs=3, n_stringers=2):
    params = dict(HAND_PARAMS)
    params["span"] = span
    params["c_root"] = c_root
    params["c_tip"] = c_root / 2.0
    params["n_ribs"] = n_ribs
    params["n_stringers"] = n_stringers
    return topology.build_topology("wingbox_v1", params)


# ---------------------------------------------------------------------------
# 1) 质量手算对照
# ---------------------------------------------------------------------------

def test_skin_mass(fails):
    """质量 = 自然格蒙皮面积 × 层合板总厚 × 密度，可与手算对照。"""
    model = _wing()
    m = objectives.mass(model, HAND_LAMINATE, density=DENSITY)

    if not _close(m["value"], HAND_MASS, 1e-9):
        fails.append("总质量手算不符: 期望 %.6f kg，实际 %r"
                     % (HAND_MASS, m["value"]))
    if m["unit"] != "kg":
        fails.append("质量单位应为 kg，实际 %r" % m["unit"])

    skin = m["skin"]
    if not _close(skin["area"], HAND_AREA, 1e-9):
        fails.append("蒙皮总面积手算不符: 期望 %.6f m2，实际 %r"
                     % (HAND_AREA, skin["area"]))
    if skin["n_bays"] != len(model.bays):
        fails.append("计入的自然格数应为 %d，实际 %r"
                     % (len(model.bays), skin["n_bays"]))
    if not _close(skin["thickness"], 0.2 * 1e-3, 1e-15):
        fails.append("铺层总厚应换算为米(2e-4)，实际 %r" % skin["thickness"])

    # 面积权重与格数一致：每格面积相等时，单格质量 = 总质量 / 格数
    per_bay = m["skin_by_bay"] if "skin_by_bay" in m else None
    if per_bay is None:
        fails.append("质量结果缺少逐格明细 skin_by_bay（无法核查口径）")
    else:
        if len(per_bay) != len(model.bays):
            fails.append("逐格明细条数应为 %d，实际 %d"
                         % (len(model.bays), len(per_bay)))
        else:
            ids = sorted(r["bay_id"] for r in per_bay)
            expect_ids = sorted(b.id for b in model.bays)
            if ids != expect_ids:
                fails.append("逐格明细的自然格 ID 不符: %r != %r"
                             % (ids, expect_ids))
            for r in per_bay:
                if not _close(r["area"], HAND_AREA / len(model.bays), 1e-9):
                    fails.append("格 %s 面积手算不符: 期望 %.6f，实际 %r"
                                 % (r["bay_id"], HAND_AREA / len(model.bays),
                                    r["area"]))
                    break


def test_xi_scale(fails):
    """xi 必须乘 xi_scale：把 0..1 当米会让质量算少一半。"""
    model = _wing(c_root=2.0)
    ext = model.extents()
    if _close(ext["xi_scale"], 1.0):
        fails.append("本用例的 xi_scale 应 ≠ 1（否则测不出换算错误），实际 %r"
                     % ext["xi_scale"])

    m = objectives.mass(model, HAND_LAMINATE, density=DENSITY)

    # 正确：xi 跨度 1.0 × c_root 2.0 = 2.0 m
    expect = 2 * 3.0 * 2.0 * (0.2e-3) * DENSITY
    if not _close(m["value"], expect, 1e-9):
        fails.append("含 xi_scale 的质量手算不符: 期望 %.6f kg，实际 %r"
                     % (expect, m["value"]))

    # 错误口径（把 xi 当 1 米）会得到一半 —— 断言我们不是那个值
    wrong = 2 * 3.0 * 1.0 * (0.2e-3) * DENSITY
    if _close(m["value"], wrong, 1e-9):
        fails.append("质量把参数空间 0..1 当物理长度了（得到 %r，应为 %r）"
                     % (wrong, expect))

    # 径/弦尺度参数一变，质量必须跟着按比例变（证明真的用了 xi_scale）
    m2 = objectives.mass(_wing(c_root=4.0), HAND_LAMINATE, density=DENSITY)
    ratio = m2["value"] / m["value"]
    if not _close(ratio, 2.0, 1e-9):
        fails.append("c_root 加倍后质量应加倍，实际比值 %r" % ratio)

    # 筒段类（xi_scale = 2πr）同样适用，说明本模块部件族无关
    cyl = topology.build_topology(
        "cyl_m21", {"length": 6.0, "radius": 1.5, "n_frames": 3, "n_stringers": 4},
        rule={"mode": topology.MODE_CYLINDER_SECTION})
    mc = objectives.mass(cyl, HAND_LAMINATE, density=DENSITY)
    ce = cyl.extents()
    if not _close(ce["xi_scale"], 3.141592653589793 * 3.0, 1e-9):
        fails.append("筒段 xi_scale 应为 2πr，实际 %r" % ce["xi_scale"])
    # 筒段（4 框 × 4 桁条 -> 2*4=8 格），逐格面积 sum((y1-y0)*(xi1-xi0)*scale)
    expect_cyl = sum((b.y1 - b.y0) * (b.xi1 - b.xi0) * ce["xi_scale"]
                     for b in cyl.bays) * (0.2e-3) * DENSITY
    if not _close(mc["value"], expect_cyl, 1e-9):
        fails.append("筒段质量手算不符: 期望 %.6f，实际 %r"
                     % (expect_cyl, mc["value"]))


# ---------------------------------------------------------------------------
# 2) components 声明
# ---------------------------------------------------------------------------

def test_components_declared(fails):
    """质量结果必须声明包含了哪些部件，且声明与实际计入项一致。"""
    model = _wing()
    m = objectives.mass(model, HAND_LAMINATE, density=DENSITY)

    comps = m.get("components")
    if not comps:
        fails.append("质量结果缺少 components 声明（只给一个数不说包含什么是不可接受的）")
    elif comps != ["skin"]:
        fails.append("未声明加筋时 components 应为 ['skin']，实际 %r" % (comps,))

    # 声明一致：components 里的每一项都在 breakdown 里有对应条目与质量
    breakdown = m.get("breakdown") or {}
    for name in (comps or []):
        if name not in breakdown:
            fails.append("components 声明了 %r 但 breakdown 里没有它" % name)
        elif not _close(breakdown[name]["mass"],
                        (m["skin"]["mass"] if name == "skin" else 0.0), 1e-9):
            fails.append("breakdown[%r].mass 与明细不一致" % name)

    # 各分量质量之和必须等于总量（不能有来源不明的质量）
    total = sum(v["mass"] for v in breakdown.values())
    if not _close(total, m["value"], 1e-9):
        fails.append("分量质量之和 %.9f != 总质量 %.9f" % (total, m["value"]))

    # 未计入的部件也要如实列出（不是默默漏掉）
    excluded = m.get("excluded") or []
    names = [e.get("component") for e in excluded]
    if "stiffeners" not in names:
        fails.append("未声明加筋时应把 stiffeners 列进 excluded，实际 %r"
                     % (excluded,))
    else:
        item = [e for e in excluded if e.get("component") == "stiffeners"][0]
        if not item.get("reason"):
            fails.append("excluded 条目缺少原因说明: %r" % (item,))

    # 单位也要声明（跨单位体系时质量会差 1000 倍以上）
    units = m.get("units") or {}
    for key in ("length", "thickness", "density", "mass"):
        if key not in units:
            fails.append("质量结果缺少单位声明 units[%r]" % key)


def test_stiffeners_optional(fails):
    """未声明加筋假设只算蒙皮并标注；声明后才计入加筋，且如实列出。"""
    model = _wing()

    # 未声明：只有蒙皮，加筋在 excluded 里
    skin_only = objectives.mass(model, HAND_LAMINATE, density=DENSITY)
    if skin_only["components"] != ["skin"]:
        fails.append("未声明加筋时应只算蒙皮，实际 %r"
                     % (skin_only["components"],))
    if not any(e["component"] == "stiffeners"
               for e in skin_only["excluded"]):
        fails.append("未声明加筋时应在 excluded 里标注 stiffeners")

    # 声明后：按 per_bay 截面积折算，加筋质量可手算
    #   每个自然格分到 1.2e-3 m2 的加筋截面，2 格
    #   -> 加筋质量 = 2 × 1.2e-3 m2 × 1600 kg/m3 = 3.84 kg
    assum = {"area_per_bay": 1.2e-3, "basis": "per_bay"}
    with_stiff = objectives.mass(model, HAND_LAMINATE, density=DENSITY,
                                 stiffeners=assum)
    expect_stiff = len(model.bays) * 1.2e-3 * DENSITY
    if "stiffeners" not in with_stiff["components"]:
        fails.append("声明加筋后 components 里应含 stiffeners，实际 %r"
                     % (with_stiff["components"],))
    if not _close(with_stiff["breakdown"]["stiffeners"]["mass"], expect_stiff,
                  1e-9):
        fails.append("加筋质量手算不符: 期望 %.6f，实际 %r"
                     % (expect_stiff,
                        with_stiff["breakdown"]["stiffeners"]["mass"]))
    if not _close(with_stiff["value"], HAND_MASS + expect_stiff, 1e-9):
        fails.append("含加筋总质量手算不符: 期望 %.6f，实际 %r"
                     % (HAND_MASS + expect_stiff, with_stiff["value"]))
    # 加筋不再出现在 excluded 里（计入与排除不能同时成立）
    if any(e["component"] == "stiffeners" for e in with_stiff["excluded"]):
        fails.append("加筋已计入却仍列在 excluded 里")

    # 声明了但用 include 只要蒙皮 -> 口径必须如实反映（分数不会虚高）
    skin_only2 = objectives.mass(model, HAND_LAMINATE, density=DENSITY,
                                 stiffeners=assum, include=["skin"])
    if skin_only2["components"] != ["skin"]:
        fails.append("include=['skin'] 时 components 应只含 skin，实际 %r"
                     % (skin_only2["components"],))
    if not _close(skin_only2["value"], HAND_MASS, 1e-9):
        fails.append("include=['skin'] 时总量应等于纯蒙皮质量，实际 %r"
                     % skin_only2["value"])

    # per_length 口径：加筋截面积 × 沿 y 的物理长度
    per_len = objectives.mass(model, HAND_LAMINATE, density=DENSITY,
                              stiffeners={"area": 5e-4, "basis": "per_length"})
    expect_len = sum((b.y1 - b.y0) for b in model.bays) * 5e-4 * DENSITY
    if not _close(per_len["breakdown"]["stiffeners"]["mass"], expect_len, 1e-9):
        fails.append("per_length 加筋质量手算不符: 期望 %.6f，实际 %r"
                     % (expect_len,
                        per_len["breakdown"]["stiffeners"]["mass"]))

    # 未声明的分量不能靠 include 强行计入
    try:
        objectives.mass(model, HAND_LAMINATE, density=DENSITY,
                        include=["stiffeners"])
        fails.append("未声明加筋却用 include 计入，未被拒绝")
    except objectives.ObjectiveError:
        pass


def test_mass_rejects_bad_input(fails):
    """口径不明或输入非法时必须明确报错，不能静默给出一个数。"""
    model = _wing()

    # 厚度单位不可识别
    try:
        objectives.mass(model, {"total_thickness": 0.2,
                                "thickness_unit": "inch"}, density=DENSITY)
        fails.append("未知厚度单位未被拒绝")
    except objectives.ObjectiveError:
        pass

    # 缺层合板
    try:
        objectives.mass(model, None, density=DENSITY)
        fails.append("缺层合板信息未被拒绝")
    except objectives.ObjectiveError:
        pass

    # 密度非正
    try:
        objectives.mass(model, HAND_LAMINATE, density=0.0)
        fails.append("密度 0 未被拒绝")
    except objectives.ObjectiveError:
        pass

    # 传一个没有 extents 的对象（典型的"用错对象"）
    try:
        objectives.mass({"bays": []}, HAND_LAMINATE, density=DENSITY)
        fails.append("缺 extents 的对象未被拒绝")
    except objectives.ObjectiveError:
        pass

    # thickness_unit="m" 显式声明时不换算（0.0002 m 与 0.2 mm 等价）
    m = objectives.mass(model, {"total_thickness": 0.0002,
                                "thickness_unit": "m"}, density=DENSITY)
    if not _close(m["value"], HAND_MASS, 1e-9):
        fails.append("thickness_unit='m' 时质量应与 0.2mm 口径一致，实际 %r"
                     % m["value"])


def test_mass_from_dict_and_no_deps(fails):
    """纯算法：只吃模型数据，不依赖 CATIA/Abaqus/DB；to_dict() 形态同样可用。"""
    model = _wing()
    from_obj = objectives.mass(model, HAND_LAMINATE, density=DENSITY)

    data = model.to_dict()
    from_dict = objectives.mass(data, HAND_LAMINATE, density=DENSITY)
    if not _close(from_dict["value"], from_obj["value"], 1e-12):
        fails.append("从 to_dict() 算出的质量与对象形态不一致: %r vs %r"
                     % (from_dict["value"], from_obj["value"]))

    # 结果必须可 JSON 序列化（要落进产物/优化日志）
    try:
        json.dumps(from_obj, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        fails.append("质量结果不可 JSON 序列化: %s" % exc)

    # 模块依赖检查：源码里不得出现 CATIA/Abaqus/DB 的 import
    path = os.path.join(CORE, "objectives.py")
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    for banned in ("catia_driver", "abaqus_runner", "import db",
                   "from db", "import numpy", "win32com"):
        if banned in src:
            fails.append("objectives.py 不应依赖 %r（必须纯算法）" % banned)


# ---------------------------------------------------------------------------
# 4) 约束口径复用既有强度校核结果
# ---------------------------------------------------------------------------

def _summary(**kw):
    base = {
        "max_fi": 0.81, "min_ms": 0.23, "all_pass": True,
        "n_bays_checked": 6, "n_bays_excluded": 0, "n_elems": 6,
        "n_detail_rows": 24, "n_fail_rows": 0,
        "criteria": ["tsai_wu"], "safety_factor": 1.5, "ms_threshold": 0.0,
    }
    base.update(kw)
    return base


def test_constraints_reuse_check(fails):
    """约束口径直接复用 check.summary，不另起一套判定。"""
    c = objectives.constraints(_summary())
    if c["feasible"] is not True:
        fails.append("all_pass=True 时应 feasible=True，实际 %r" % c["feasible"])
    if c["source"] != "strength_check.check.summary":
        fails.append("约束应标明来源为 strength_check.check.summary，实际 %r"
                     % c["source"])
    # 原始值必须原样带出（优化日志要能对上校核报告）
    if not _close(c["values"]["max_fi"], 0.81, 1e-12):
        fails.append("约束未带出 max_fi: %r" % c["values"])
    if not _close(c["values"]["min_ms"], 0.23, 1e-12):
        fails.append("约束未带出 min_ms: %r" % c["values"])
    if c["values"]["n_bays_excluded"] != 0:
        fails.append("约束未带出 n_bays_excluded: %r" % c["values"])
    if c["verdict"]["all_pass"] is not True:
        fails.append("约束应记录原始 all_pass 判定: %r" % c["verdict"])

    c2 = objectives.constraints(_summary(max_fi=1.4, min_ms=-0.29,
                                         all_pass=False, n_fail_rows=3))
    if c2["feasible"] is not False:
        fails.append("all_pass=False 时应 feasible=False，实际 %r" % c2["feasible"])

    # 判定口径只能来自 all_pass：即使数值看起来"应该合格"，
    # 也不许自己重算一遍再覆盖既有结论（不另起判定）
    contradictory = _summary(all_pass=True, min_ms=-0.1, max_fi=1.2)
    c3 = objectives.constraints(contradictory)
    if c3["feasible"] is not True:
        fails.append("约束不应自行重算判定（应沿用 all_pass=True），实际 %r"
                     % c3["feasible"])
    if not c3["warnings"]:
        fails.append("all_pass 与 min_ms/max_fi 矛盾时应给出警告，实际 %r"
                     % (c3["warnings"],))

    # 缺 all_pass（没跑校核）时必须报错，而不是猜一个判定
    try:
        objectives.constraints({"max_fi": 0.5, "min_ms": 0.3})
        fails.append("check.summary 缺 all_pass 时未被拒绝")
    except objectives.ObjectiveError:
        pass


# ---------------------------------------------------------------------------
# 5) 技术故障 vs 设计不可行（本票最关键的区分）
# ---------------------------------------------------------------------------

def test_three_way_classification(fails):
    """三类结果在数据结构上可区分：ok / error / feasible 的组合各不相同。"""
    # (a) 跑通 + 满足约束
    good = objectives.evaluate(check_summary=_summary(),
                                mass_kg=3.84, mass_components=["skin"])
    # (b) 跑通 + 违反约束
    bad = objectives.evaluate(check_summary=_summary(
        all_pass=False, min_ms=-0.29, max_fi=1.4), mass_kg=3.84, mass_components=["skin"])
    # (c) 技术故障
    broken = objectives.evaluate(error=RuntimeError("Abaqus 求解未完成"))

    if not (good["ok"] is True and good["feasible"] is True and good["error"] is None):
        fails.append("合格结果应为 ok=True/feasible=True/error=None，实际 %r"
                     % {k: good.get(k) for k in ("ok", "feasible", "error")})
    if not (bad["ok"] is True and bad["feasible"] is False and bad["error"] is None):
        fails.append("不可行结果应为 ok=True/feasible=False/error=None，实际 %r"
                     % {k: bad.get(k) for k in ("ok", "feasible", "error")})
    if not (broken["ok"] is False and broken["feasible"] is None
            and broken["error"]):
        fails.append("技术故障应为 ok=False/feasible=None/error 非空，实际 %r"
                     % {k: broken.get(k) for k in ("ok", "feasible", "error")})

    # 关键：故障的 feasible 绝不能是 False（否则优化器把故障读成"设计很差"）
    if broken["feasible"] is False:
        fails.append("技术故障的 feasible 不得为 False（须为 None）")
    # 关键：故障与不可行在 error 上也必须不同
    if broken.get("error") == bad.get("error"):
        fails.append("技术故障与设计不可行应有不同的 error 表示")
    if not broken["error"].get("kind"):
        fails.append("技术故障的 error 应带可分类的 kind: %r" % (broken["error"],))
    if not broken["error"].get("message"):
        fails.append("技术故障的 error 应带可读 message: %r" % (broken["error"],))

    # 目标值：故障点没有可比的目标值（不能让优化器拿它排序）
    if good["objective"]["value"] is None or bad["objective"]["value"] is None:
        fails.append("跑通的结果必须带目标值")
    if broken["objective"]["value"] is not None:
        fails.append("技术故障不得给出目标值（否则会被当成最优点），实际 %r"
                     % (broken["objective"]["value"],))

    # 归一结构里目标方向明确是"最小化"，优化器无需猜
    for ev in (good, bad):
        if ev["objective"]["direction"] != "minimize":
            fails.append("目标方向应为 minimize，实际 %r"
                         % (ev["objective"]["direction"],))
        if ev["objective"]["unit"] != "kg":
            fails.append("目标单位应为 kg，实际 %r" % (ev["objective"]["unit"],))

    # 可 JSON 序列化（要落进优化日志/血缘）
    for ev in (good, bad, broken):
        try:
            json.dumps(ev, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            fails.append("评估结果不可 JSON 序列化: %s" % exc)


def test_error_kinds(fails):
    """技术故障按来源分类，便于优化器区分"哪类故障"而不只是"失败"。"""
    cases = [
        (objectives.ErrorKind.PARAM_ERROR,
         _fake_error("ParamError", "参数 span=9999 高于上限 40.0")),
        (objectives.ErrorKind.MESH_VIOLATION,
         _fake_error("MeshQualityError", "网格存在 3 个跨自然格单元")),
        (objectives.ErrorKind.RUN_FAILED,
         _fake_error("StudyRunError", "Abaqus 求解未完成(status=FAILED)")),
    ]
    seen = set()
    for expect_kind, exc in cases:
        ev = objectives.evaluate(error=exc)
        kind = ev["error"]["kind"]
        seen.add(kind)
        if kind != expect_kind:
            fails.append("异常 %s 应归类为 %r，实际 %r"
                         % (type(exc).__name__, expect_kind, kind))
    if len(seen) != len(cases):
        fails.append("不同技术故障被归成同一类: %r" % (seen,))

    # 未知异常也要有兜底分类，不能丢
    ev = objectives.evaluate(error=ValueError("something odd"))
    if not ev["error"]["kind"]:
        fails.append("未知异常未被分类")
    if ev["ok"] is not False:
        fails.append("未知异常也应 ok=False")

    # 即使是同一种运行失败，只要异常类名不同就应能区分 —— 用真实的类名
    # （core 里各层都抛自己声明的异常类型，优化器据此决定重试还是跳过）
    imported = _import_core_errors()
    if imported:
        param_exc = imported["ParamError"]("未知参数: bogus（模板 wingbox_v1 ...）")
        ev = objectives.evaluate(error=param_exc)
        if ev["error"]["kind"] != objectives.ErrorKind.PARAM_ERROR:
            fails.append("真实 template_registry.ParamError 未被归类为 param_error，"
                         "实际 %r" % (ev["error"]["kind"],))
        if ev["error"].get("type") != "ParamError":
            fails.append("故障描述应记录异常类型名，实际 %r"
                         % (ev["error"].get("type"),))


def _fake_error(class_name, message):
    """造一个类名为 class_name 的异常实例（模拟 core 各层的错误类型）。"""
    cls = type(class_name, (Exception,), {})
    return cls(message)


def _import_core_errors():
    """尽量导入 core 里真实存在的错误类型；缺了就跳过那段断言。"""
    try:
        import template_registry
    except ImportError:
        return None
    return {"ParamError": template_registry.ParamError}


def test_evaluate_detects_mesh_violation(fails):
    """网格破戒（ADR-0010）属技术故障：不能当成"设计不可行"。"""
    # 网格质量门报告了跨自然格单元 -> 技术故障（结论不可信）
    result = {
        "study_id": "S-M21-mesh",
        "template": "wingbox_v1",
        "parameters": {},
        "check": {"summary": _summary()},
        "mesh_quality": {"n_cross_bay_elems": 3, "n_hanging_nodes": 0,
                         "warnings": ["跨自然格单元 3 个"]},
    }
    ev = objectives.evaluate(result=result)
    if ev["ok"] is not False:
        fails.append("网格跨自然格单元应判为技术故障(ok=False)，实际 %r"
                     % (ev.get("ok"),))
    if ev["error"]["kind"] != objectives.ErrorKind.MESH_VIOLATION:
        fails.append("网格破戒应归类为 mesh_violation，实际 %r"
                     % (ev["error"]["kind"],))

    # 合格网格 + 校核不合格 -> 仍是"设计不可行"（ok=True）
    result2 = {
        "study_id": "S-M21-mesh2",
        "template": "wingbox_v1",
        "parameters": {},
        "check": {"summary": _summary(all_pass=False, min_ms=-0.5, max_fi=2.0)},
        "mesh_quality": {"n_cross_bay_elems": 0, "n_hanging_nodes": 0},
    }
    ev2 = objectives.evaluate(result=result2, mass_kg=3.84, mass_components=["skin"])
    if not (ev2["ok"] is True and ev2["feasible"] is False):
        fails.append("网格合格但强度不合格应为 ok=True/feasible=False，实际 %r"
                     % {k: ev2.get(k) for k in ("ok", "feasible")})

    # 没跑校核（结果里无 check）-> 技术故障，不是"不可行"
    ev3 = objectives.evaluate(result={"study_id": "S-M21-nocheck",
                                      "template": "wingbox_v1",
                                      "parameters": {}})
    if ev3["ok"] is not False:
        fails.append("缺校核结果应判为技术故障，实际 ok=%r" % (ev3.get("ok"),))
    if ev3["error"]["kind"] != objectives.ErrorKind.NO_CHECK:
        fails.append("缺校核结果应归类为 no_check，实际 %r"
                     % (ev3["error"]["kind"],))


# ---------------------------------------------------------------------------
# 6) 接真实 run_study 结果（口径必须对得上真实产物，不只是假数据）
# ---------------------------------------------------------------------------

#: 手算基线：span=6 / c_root=2 / 3 肋 / 2 长桁 -> 2 格，每格 3.0 m × 2.0 m
WING_PARAMS = {"span": 6.0, "sweep_deg": 10.0, "c_root": 2.0, "c_tip": 1.0,
               "n_ribs": 3, "n_stringers": 2}
WING_AREA = 12.0                                   # m2（两格合计）
WING_THICKNESS_MM = 0.2                            # 2 层 × 0.1
WING_MASS = WING_AREA * (WING_THICKNESS_MM * 1e-3) * DENSITY


def test_evaluate_real_run(fails):
    """用真实 run_study 结果评估：目标来自拓扑+铺层，约束来自校核汇总。"""
    import db
    import run_study as rs
    from run_study import RunOptions
    from catia_driver import FakeCatiaDriver
    from abaqus_runner import FakeAbaqusRunner

    tmp = _fresh("chain")
    conn = db.connect(os.path.join(tmp, "m21.db"))
    try:
        import study as study_mod
        study_mod.create_study("S-M21-chain", "M21 真实链路", conn=conn)

        def dir_for(sub):
            return os.path.join(tmp, sub)

        opts = RunOptions.full_chain(
            dir_for,
            plies=[{"angle": 0.0, "thickness": 0.1},
                   {"angle": 90.0, "thickness": 0.1}],
            subdiv_per_bay=1,
            abaqus=FakeAbaqusRunner(n_elems=2, n_plies=2),
            with_report=False)
        result = rs.run_study(
            "S-M21-chain", WING_PARAMS, "wingbox_v1",
            FakeCatiaDriver(os.path.join(tmp, "catparts")), conn, opts)

        # 目标：自动从 topology_model + fe_model.laminate 算出，不需外部喂数
        ev = objectives.evaluate(result=result, density=DENSITY)
        if ev["ok"] is not True:
            fails.append("真实链路结果评估意外失败: %r" % (ev.get("error"),))
            return
        if not _close(ev["objective"]["value"], WING_MASS, 1e-9):
            fails.append("真实链路质量手算不符: 期望 %.9f kg，实际 %r"
                         % (WING_MASS, ev["objective"]["value"]))
        if ev["objective"]["unit"] != "kg":
            fails.append("真实链路目标单位应为 kg")
        if ev["objective"]["components"] != ["skin"]:
            fails.append("默认口径应只算蒙皮，实际 %r"
                         % (ev["objective"]["components"],))
        if ev["objective"]["direction"] != "minimize":
            fails.append("目标方向应为 minimize")

        # 约束：必须与 strength_check 的汇总逐项一致（复用而非重算）
        summ = result["check"]["summary"]
        cons = ev["constraints"]
        if cons["feasible"] is not summ["all_pass"]:
            fails.append("约束判定与 check.summary.all_pass 不一致: %r vs %r"
                         % (cons["feasible"], summ["all_pass"]))
        for key in ("max_fi", "min_ms", "n_bays_excluded"):
            if cons["values"].get(key) != summ.get(key):
                fails.append("约束 values[%r] 与校核汇总不一致: %r vs %r"
                             % (key, cons["values"].get(key), summ.get(key)))
        if cons["values"].get("n_bays_excluded") != 0:
            fails.append("该用例不应有不连续格")
        # 目标值与校核结论都能追溯回同一个 Study
        if ev["study_id"] != "S-M21-chain":
            fails.append("评估结果丢失 study_id: %r" % (ev.get("study_id"),))
        if ev["template"] != "wingbox_v1":
            fails.append("评估结果丢失 template: %r" % (ev.get("template"),))

        # 加一层铺层 -> 质量必须按比例增加（目标真的由铺层驱动）
        opts2 = opts.replace(plies=[{"angle": 0.0, "thickness": 0.1},
                                    {"angle": 90.0, "thickness": 0.1},
                                    {"angle": 45.0, "thickness": 0.1}])
        study_mod.create_study("S-M21-chain2", "M21 三铺层", conn=conn)
        result2 = rs.run_study(
            "S-M21-chain2", WING_PARAMS, "wingbox_v1",
            FakeCatiaDriver(os.path.join(tmp, "catparts2")), conn, opts2)
        ev2 = objectives.evaluate(result=result2, density=DENSITY)
        expect2 = WING_AREA * (0.3 * 1e-3) * DENSITY
        if not _close(ev2["objective"]["value"], expect2, 1e-9):
            fails.append("三铺层质量手算不符: 期望 %.9f，实际 %r"
                         % (expect2, ev2["objective"]["value"]))

        # 求解失败 -> 技术故障（真实异常类型 StudyRunError 直接丢给 evaluate）
        try:
            opts3 = opts.replace(abaqus=_FailingRunner())
            study_mod.create_study("S-M21-chain3", "M21 求解失败", conn=conn)
            rs.run_study("S-M21-chain3", WING_PARAMS, "wingbox_v1",
                         FakeCatiaDriver(os.path.join(tmp, "catparts3")), conn,
                         opts3)
            fails.append("求解失败未被 run_study 抛出")
        except Exception as exc:
            ev3 = objectives.evaluate(error=exc)
            if ev3["ok"] is not False:
                fails.append("真实求解失败应评估为 ok=False，实际 %r"
                             % (ev3.get("ok"),))
            if ev3["feasible"] is not None:
                fails.append("真实求解失败的 feasible 必须是 None（不是 False），"
                             "实际 %r" % (ev3.get("feasible"),))
            if ev3["error"]["kind"] == objectives.ErrorKind.UNKNOWN:
                fails.append("真实 StudyRunError 未被识别（落到 unknown）")
    finally:
        conn.close()


class _FailingRunner(object):
    """求解必失败的 runner（模拟真实 Abaqus 失败路径）。"""

    def solve(self, inp_path, job_name, work_dir, step_name=None, analysis=None):
        class _R(object):
            status = "FAILED"
            odb_path = ""
            log_tail = "Abaqus Error: simulated failure"
        return _R()

    def extract(self, odb_path, out_dir, step_name=None):
        raise RuntimeError("不该走到抽取")


# ---------------------------------------------------------------------------
# 7) 纯算法可脱机单测 + Python 3.8 兼容
# ---------------------------------------------------------------------------

def test_offline_pure(fails):
    """评估可在没有 DB / 文件系统的情况下纯内存完成（优化器批量评估的前提）。"""
    # 完全不碰 DB、不建目录、不读模板
    ev = objectives.evaluate(
        check_summary=_summary(all_pass=True),
        mass_result=objectives.mass(_wing(), HAND_LAMINATE, density=DENSITY),
        study_id="S-OFFLINE")
    if ev["ok"] is not True:
        fails.append("纯内存评估失败: %r" % (ev.get("error"),))
    if not _close(ev["objective"]["value"], HAND_MASS, 1e-9):
        fails.append("纯内存评估的目标值不符: %r" % (ev["objective"]["value"],))

    # 便捷查询的口径要能直接驱动"挑最优"
    feasible = objectives.evaluate(check_summary=_summary(), mass_kg=10.0, mass_components=["skin"])
    infeasible = objectives.evaluate(
        check_summary=_summary(all_pass=False, min_ms=-0.1), mass_kg=1.0, mass_components=["skin"])
    broken = objectives.evaluate(error=RuntimeError("boom"))

    if not objectives.is_feasible(feasible):
        fails.append("is_feasible 未认出可行点")
    if objectives.is_feasible(infeasible):
        fails.append("is_feasible 把不可行点当成可行")
    if not objectives.is_technical_failure(broken):
        fails.append("is_technical_failure 未认出故障点")
    if objectives.is_technical_failure(infeasible):
        fails.append("is_technical_failure 把设计不可行当成技术故障")

    # 排序：可行点最优先（哪怕它更重），故障点永远垫底
    ranked = sorted([broken, infeasible, feasible], key=objectives.sort_key)
    if ranked[0] is not feasible:
        fails.append("排序应把可行点排最前（即使它更重），实际 %r"
                     % (objectives.sort_key(ranked[0]),))
    if ranked[-1] is not broken:
        fails.append("排序应把技术故障排最后，实际 %r"
                     % (objectives.sort_key(ranked[-1]),))
    # 一条铁律：故障点绝不能因为"数值小"被选成最优
    tiny_broken = objectives.evaluate(error=RuntimeError("boom"))
    tiny_broken["objective"]["value"] = 1e-9      # 人为塞一个极小值
    if objectives.sort_key(tiny_broken) <= objectives.sort_key(feasible):
        fails.append("故障点即便带极小的目标值也不得排在可行点前面")

    txt = objectives.summarize(feasible)
    if "不可行" in txt or "故障" in txt:
        fails.append("summarize 对可行点描述错误: %r" % txt)
    if "技术故障" not in objectives.summarize(broken):
        fails.append("summarize 未标明技术故障: %r" % objectives.summarize(broken))

    # 调用方直接给数值时，不得替他编造"只含蒙皮"这种口径；
    # 而且这个数**不得参与最优比较** —— 否则"少算了加筋所以最轻"的点会当选，
    # 那不是更好的设计，是作弊的度量（spec：不允许只给一个数不说包含什么）。
    bare = objectives.evaluate(check_summary=_summary(), mass_kg=3.84)
    if bare["objective"]["components"]:
        fails.append("直接给数值时不应凭空声明 components，实际 %r"
                     % (bare["objective"]["components"],))
    if bare["objective"]["declared"] is not False:
        fails.append("直接给数值时应标 declared=False，实际 %r"
                     % (bare["objective"]["declared"],))
    if bare.get("comparable") is not False:
        fails.append("未声明口径的质量应标 comparable=False，实际 %r"
                     % (bare.get("comparable"),))
    if bare.get("undeclared_reason") is None:
        fails.append("未声明口径时应给出可读原因")
    # 关键：即便它数值更小，排序也必须排在所有有口径的点之后
    declared = objectives.evaluate(
        check_summary=_summary(),
        mass_result=objectives.mass(_wing(), HAND_LAMINATE, density=DENSITY))
    if declared["objective"]["declared"] is not True:
        fails.append("走 mass() 时应标 declared=True")
    if declared["objective"]["components"] != ["skin"]:
        fails.append("走 mass() 时应带 components=['skin']，实际 %r"
                     % (declared["objective"]["components"],))
    if declared.get("comparable") is not True:
        fails.append("走 mass() 的点应是可比的")
    # 用一个更小的裸数值对比：不可比点不得因此胜出
    bare_small = objectives.evaluate(check_summary=_summary(), mass_kg=0.001)
    if objectives.sort_key(bare_small) <= objectives.sort_key(declared):
        fails.append("未声明口径的点（数值更小）不应排在声明口径的点之前："
                     "%r vs %r" % (objectives.sort_key(bare_small),
                                   objectives.sort_key(declared)))


def test_py38(fails):
    """Python 3.8 兼容：本票涉及的模块不得用 3.9+ 语法/标准库。"""
    import compat_py38

    targets = [
        os.path.join(CORE, "objectives.py"),
        os.path.join(CORE, "compat_py38.py"),
        os.path.join(HERE, "test_m21.py"),
    ]
    bad = compat_py38.check_paths(targets)
    if bad:
        fails.append("Python 3.8 兼容性检查未通过:\n%s"
                     % compat_py38.format_violations(bad))

    # 检查器自身必须能抓到 3.9+ 用法（否则这条断言只是摆设）
    probe = compat_py38.check_source(
        "probe.py", "x: list[int] = []\ns = 'a'.removeprefix('b')\n")
    if len(probe) < 2:
        fails.append("compat_py38 未检出 list[int] / removeprefix")


def main():
    fails = []
    print("1. 蒙皮质量手算对照 ...")
    test_skin_mass(fails)
    print("2. 物理尺度换算（xi_scale ≠ 1） ...")
    test_xi_scale(fails)
    print("3. components / excluded 口径声明 ...")
    test_components_declared(fails)
    print("4. 加筋可选计入 ...")
    test_stiffeners_optional(fails)
    print("5. 质量口径的非法输入拒绝 ...")
    test_mass_rejects_bad_input(fails)
    print("6. 纯算法 + to_dict() 形态 ...")
    test_mass_from_dict_and_no_deps(fails)
    print("7. 约束口径复用既有校核结果 ...")
    test_constraints_reuse_check(fails)
    print("8. 技术故障 / 设计不可行 三类可区分 ...")
    test_three_way_classification(fails)
    print("9. 技术故障分类 ...")
    test_error_kinds(fails)
    print("10. 网格破戒判为技术故障 ...")
    test_evaluate_detects_mesh_violation(fails)
    print("11. 接真实 run_study 结果 ...")
    test_evaluate_real_run(fails)
    print("12. 脱机纯算法评估 + 排序口径 ...")
    test_offline_pure(fails)
    print("13. Python 3.8 兼容 ...")
    test_py38(fails)

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M21 (票21) SELF-TEST: ALL PASSED "
          "(质量手算+xi_scale换算 / components口径 / 约束复用 / "
          "技术故障≠不可行 / 纯算法 / py3.8)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
