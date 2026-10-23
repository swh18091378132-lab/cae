# -*- coding: utf-8 -*-
"""
test_m15.py -- 票15 验收：网格质量门与自动加密（不破坏拓扑对齐）

验收点（对应 issue #15）：
  1. 可计算每个自然格的网格质量指标（畸变度 / 长宽比 / 翘曲度），物理尺寸经
     `TopologyModel.extents()` 换算（不把参数空间 0..1 当物理长度）
  2. 指标数值正确：构造**已知质量好/坏**的单元，与手算值逐项对照
  3. 超标自然格可自动加密重划，且加密后**每个单元仍唯一归属一个自然格**
     （跨自然格单元数为 0，ADR-0010 硬约束）；界面无悬挂节点
  4. 达到加密上限仍不达标 -> 产生明确警告并**在产物中留痕**
  5. 可关闭自动加密（仅出质量报告），且交付网格与不加门时完全一致
  6. 默认策略下机翼盒段既有网格行为**零回归**（同参数单元数/节点与 P1 完全一致）
  7. Python 3.8 兼容

运行 (在 apps/workbench 下): python tests/test_m15.py
"""

import os
import sys
import json
import shutil
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
sys.path.insert(0, CORE)
sys.path.insert(0, WORKBENCH)

import topology
import meshgen
import mesh_quality as mq
import fe_model
import db
import study
import run_study
from catia_driver import FakeCatiaDriver


TMP = os.path.join(WORKBENCH, ".tmp_test", "m15")

# P1 机翼盒段基线参数（与 test_m2 / test_m3 / test_m11 完全一致）
BASE_PARAMS = {
    "span": 12.0,
    "sweep_deg": 25.0,
    "c_root": 3.0,
    "c_tip": 1.2,
    "n_ribs": 6,
    "n_stringers": 7,
}

#: P1 网格口径：每自然格 4×4，30 个自然格 -> 480 单元（subdiv 3 时为 30*9=270）
P1_SUBDIV = 4
P1_N_BAYS = 30

TOL = 1e-9
TOL_METRIC = 1e-9


# ---------------------------------------------------------------------------
# 辅助：构造只含几何信息的迷你拓扑模型（手算对照用）
# ---------------------------------------------------------------------------

def _mini_model(y_lo, y_hi, xi_lo, xi_hi, xi_scale):
    """
    造一个 1 个自然格的最小 TopologyModel：

      y  ∈ [y_lo, y_hi]         （y 本身就是物理长度）
      xi ∈ [xi_lo, xi_hi]       （乘 xi_scale 才是物理长度）

    走真实的 `topology.build_bays`，所以自然格 ID / 区间口径与主链一致。
    """
    stringers = [{"xi": xi_lo, "y0": y_lo, "y1": y_hi},
                 {"xi": xi_hi, "y0": y_lo, "y1": y_hi}]
    ribs = [{"y": y_lo, "xi0": xi_lo, "xi1": xi_hi},
            {"y": y_hi, "xi0": xi_lo, "xi1": xi_hi}]
    model = topology.TopologyModel(
        "mini", {}, stringers, ribs, [],
        physical={"xi_scale": xi_scale, "y_unit": "m", "xi_unit": "m"})
    model.bays = topology.build_bays(stringers, ribs)
    return model


def _mesh_from_quads(model, quads):
    """
    用**显式节点坐标**造网格（单元 4 点可任意形状，手算对照用）。

    quads: [((y0,xi0),(y0,xi1),(y1,xi1),(y1,xi0), bay_id)]
    """
    mesh = meshgen.Mesh()
    for spec in quads:
        p0, p1, p2, p3 = spec[0], spec[1], spec[2], spec[3]
        bay_id = spec[4]
        ids = [mesh.node(y, xi) for (y, xi) in (p0, p1, p2, p3)]
        mesh.elems.append({"id": len(mesh.elems) + 1, "nodes": tuple(ids),
                           "bay_id": bay_id, "bay_valid": True})
    return mesh


# ---------------------------------------------------------------------------
# 1+2) 指标计算与手算对照
# ---------------------------------------------------------------------------

def test_metrics_handcheck(fails):
    """
    1) 每个自然格可算指标；2) 数值与手算一致。

    手算口径（单元 4 角点按 (y0,xi0)(y0,xi1)(y1,xi1)(y1,xi0)，物理 X=(xi-xi_min)*xi_scale）：

      * 1m × 1m 正方形          -> jacobian 1.0 / aspect 1.0 / warp 0
      * 剪切 45 度的平行四边形  -> jacobian sin45° = 0.70710678 / aspect sqrt(2)
      * 4m × 1m 长方形          -> aspect 4.0 / jacobian 1.0
      * 第 4 点抬高 h 的翘曲单元 -> warp = h / 平均边长
    """
    scale = 3.0

    # --- 正方形：y[0,1] × xi[0, 1/3] -> 物理 1m × 1m ---
    model = _mini_model(0.0, 1.0, 0.0, 1.0 / scale, scale)
    mesh = _mesh_from_quads(model, [
        ((0.0, 0.0), (0.0, 1.0 / scale), (1.0, 1.0 / scale), (1.0, 0.0),
         model.bays[0].id)])
    m = mq.element_metrics(model, mesh, mesh.elems[0])
    if abs(m["jacobian"] - 1.0) > TOL_METRIC:
        fails.append("正方形畸变度应为 1.0，实际 %r" % m["jacobian"])
    if abs(m["aspect_ratio"] - 1.0) > TOL_METRIC:
        fails.append("正方形长宽比应为 1.0，实际 %r" % m["aspect_ratio"])
    if abs(m["warp"]) > TOL_METRIC:
        fails.append("平面正方形翘曲度应为 0，实际 %r" % m["warp"])

    # --- 剪切 45 度：p2/p3 沿 y 平移 1m ---
    model = _mini_model(0.0, 2.0, 0.0, 2.0 / scale, scale)
    mesh = _mesh_from_quads(model, [
        ((0.0, 0.0), (0.0, 1.0 / scale), (1.0, 2.0 / scale), (1.0, 1.0 / scale),
         model.bays[0].id)])
    m = mq.element_metrics(model, mesh, mesh.elems[0])
    expect_jac = 0.5 ** 0.5          # sin45°
    if abs(m["jacobian"] - expect_jac) > 1e-12:
        fails.append("剪切 45 度单元畸变度应为 sin45=%.15f，实际 %.15f"
                     % (expect_jac, m["jacobian"]))
    if abs(m["aspect_ratio"] - expect_jac * 2.0) > 1e-12:
        fails.append("剪切单元长宽比应为 2*sin45=%.15f，实际 %.15f"
                     % (expect_jac * 2.0, m["aspect_ratio"]))

    # --- 极端长宽比：物理 4m × 1m ---
    model = _mini_model(0.0, 4.0, 0.0, 1.0 / scale, scale)
    good = meshgen.build_mesh(model, subdiv_per_bay=1)
    m = mq.element_metrics(model, good, good.elems[0])
    if abs(m["aspect_ratio"] - 4.0) > 1e-12:
        fails.append("4m×1m 单元长宽比应为 4.0，实际 %r" % m["aspect_ratio"])
    if abs(m["jacobian"] - 1.0) > 1e-12:
        fails.append("4m×1m 矩形畸变度应为 1.0，实际 %r" % m["jacobian"])
    rec = mq.bay_metrics(model, good, model.bays[0].id)
    if abs(rec["aspect_ratio"] - 4.0) > 1e-12:
        fails.append("自然格聚合长宽比应为 4.0，实际 %r" % rec["aspect_ratio"])
    if rec["n_elems"] != 1:
        fails.append("自然格单元数应为 1，实际 %d" % rec["n_elems"])
    if abs(rec["size"][0] - 4.0) > 1e-12 or abs(rec["size"][1] - 1.0) > 1e-12:
        fails.append("自然格物理尺寸应为 (4.0, 1.0)，实际 %r" % (rec["size"],))

    # --- 翘曲：正方形，第 4 点抬高 0.25m；手算 warp = h / 平均边长 ---
    model = _mini_model(0.0, 1.0, 0.0, 1.0 / scale, scale)
    mesh = meshgen.Mesh()
    mesh.nodes = [(1, 0.0, 0.0, 0.0),
                  (2, 0.0, 1.0 / scale, 0.0),
                  (3, 1.0, 1.0 / scale, 0.0),
                  (4, 1.0, 0.0, 0.25)]
    mesh.elems = [{"id": 1, "nodes": (1, 2, 3, 4),
                   "bay_id": model.bays[0].id, "bay_valid": True}]
    m = mq.element_metrics(model, mesh, mesh.elems[0])
    h = 0.25
    dx = 0.25 ** 2
    mean_len = (1.0 + 1.0 + (1.0 + dx) ** 0.5 + (1.0 + dx) ** 0.5) / 4.0
    expect_warp = h / mean_len
    if abs(m["warp"] - expect_warp) > 1e-12:
        fails.append("翘曲度应为 h/mean=%.15f，实际 %.15f"
                     % (expect_warp, m["warp"]))
    if not (0.9 < m["jacobian"] < 1.0):
        fails.append("翘曲单元畸变度应略小于 1，实际 %r" % m["jacobian"])

    # --- 物理换算断言：xi 的 0..1 不是物理长度 ---
    # 若错把 xi 当物理长度，机翼盒段一格会是 2.4m × 0.125 -> aspect 19.2；
    # 乘 xi_scale=c_root=3.0 后应为 2.4 / 0.5 = 4.8。
    wing = topology.build_topology("wingbox_v1", BASE_PARAMS)
    wing_mesh = meshgen.build_mesh(wing, subdiv_per_bay=P1_SUBDIV)
    rec = mq.bay_metrics(wing, wing_mesh, "NB-r0-s0")
    if abs(rec["aspect_ratio"] - 4.8) > 1e-9:
        fails.append("机翼自然格长宽比应为 4.8（已乘 xi_scale=3.0），实际 %r"
                     % rec["aspect_ratio"])
    if abs(rec["size"][1] - 0.5) > 1e-9:
        fails.append("机翼自然格物理弦向尺寸应为 0.5m，实际 %r" % rec["size"][1])
    if abs(rec["size"][0] - 2.4) > 1e-9:
        fails.append("机翼自然格物理展向尺寸应为 2.4m，实际 %r" % rec["size"][0])

    # --- 指标类型齐全且整网格可评估 ---
    report = mq.evaluate(wing, wing_mesh)
    for key in ("jacobian", "aspect_ratio", "warp"):
        if key not in (report["bays"][0] if report["bays"] else {}):
            fails.append("自然格指标缺少 %r" % key)
    if report["criteria"] != {"jacobian_min": 0.6, "aspect_max": 5.0,
                              "warp_max": 0.1}:
        fails.append("默认门限不符: %r" % (report["criteria"],))
    if len(report["bays"]) != P1_N_BAYS:
        fails.append("整网格评估应覆盖 %d 个自然格，实际 %d"
                     % (P1_N_BAYS, len(report["bays"])))


# ---------------------------------------------------------------------------
# 3) 加密后仍唯一归属一个自然格
# ---------------------------------------------------------------------------

def _cross_bay_count(model, mesh):
    """独立复核：数出四角点不全落在所属自然格区间内的单元。"""
    bay_by_id = dict((b.id, b) for b in model.bays)
    node_by_id = dict((n[0], (n[1], n[2])) for n in mesh.nodes)
    bad = 0
    for elem in mesh.elems:
        bay = bay_by_id.get(elem["bay_id"])
        if bay is None:
            bad += 1
            continue
        for nid in elem["nodes"]:
            y, xi = node_by_id[nid]
            if not (bay.y0 - TOL <= y <= bay.y1 + TOL
                    and bay.xi0 - TOL <= xi <= bay.xi1 + TOL):
                bad += 1
                break
    return bad


def test_refine_keeps_alignment(fails):
    """3) 超标格自动加密；加密后跨自然格单元数 = 0，界面无悬挂节点。"""
    model = topology.build_topology("wingbox_v1", BASE_PARAMS)
    base = meshgen.build_mesh(model, subdiv_per_bay=P1_SUBDIV)
    if len(base.elems) != P1_N_BAYS * P1_SUBDIV ** 2:
        fails.append("基准网格单元数应为 %d，实际 %d"
                     % (P1_N_BAYS * P1_SUBDIV ** 2, len(base.elems)))

    quality = mq.apply_gate(model, base, {"aspect_max": 3.0, "max_refine": 3})

    if not quality["refined_bays"]:
        fails.append("aspect_max=3.0 时应有自然格被加密，实际未加密任何格")
    if quality["n_elems"] <= len(base.elems):
        fails.append("加密后单元数应增加（基准 %d，实际 %d）"
                     % (len(base.elems), quality["n_elems"]))
    if quality["warn_bays"]:
        fails.append("加密后不应仍有超标格，实际 %r" % (quality["warn_bays"],))

    # ADR-0010：跨界单元数必须为 0（三方独立复核）
    if quality["n_cross_bay_elems"] != 0:
        fails.append("加密后跨自然格单元数应为 0，实际 %d"
                     % quality["n_cross_bay_elems"])
    ok, problems = meshgen.verify_no_straddle(model, quality["mesh"])
    if not ok:
        fails.append("加密后 verify_no_straddle 不通过: %r" % (problems[:3],))
    if _cross_bay_count(model, quality["mesh"]) != 0:
        fails.append("加密后存在四角点越出所属自然格的单元")

    # 每个单元仍归属一个**已知**自然格，且自然格数量不变
    known = set(b.id for b in model.bays)
    unknown = sorted(set(e["bay_id"] for e in quality["mesh"].elems) - known)
    if unknown:
        fails.append("加密后出现未知自然格: %r" % (unknown,))
    if len(set(e["bay_id"] for e in quality["mesh"].elems)) != P1_N_BAYS:
        fails.append("加密后自然格覆盖数应仍为 %d" % P1_N_BAYS)

    # 界面协调：相邻格共用边界节点一致（否则载荷传不过去）
    n_hanging, details = mq.check_conformity(model, quality["mesh"])
    if n_hanging != 0:
        fails.append("加密后界面有 %d 个悬挂节点: %r" % (n_hanging, details[:2]))
    if quality["n_hanging_nodes"] != 0:
        fails.append("质量门自报悬挂节点数应为 0，实际 %d"
                     % quality["n_hanging_nodes"])

    # 各向异性加密：长边方向加倍才真的降长宽比（等比加密不改变矩形长宽比）
    for item in quality["refined_bays"]:
        before, after = item["subdiv_before"], item["subdiv_after"]
        if before == after:
            fails.append("加密留痕里 %s 前后细分数相同" % item["bay_id"])
        if after[0] < before[0] or after[1] < before[1]:
            fails.append("加密后细分数不应减少: %s %r -> %r"
                         % (item["bay_id"], before, after))
    b0 = dict((t["bay_id"], t) for t in quality["refined_bays"]).get("NB-r0-s0")
    if b0 is not None and b0["subdiv_after"] != [8, 4]:
        fails.append("展向长边应加倍为 [8,4]，实际 %r" % (b0["subdiv_after"],))


# ---------------------------------------------------------------------------
# 4) 达加密上限仍不达标：明确警告 + 产物留痕
# ---------------------------------------------------------------------------

def test_warp_refine_with_surface(fails):
    """
    3b) 翘曲度超标也能被质量门处理：曲面壳（扭转+弯度）上逐单元非平面，
    warp 超标 -> 加密 -> warp 下降；加密后仍不跨界、无悬挂节点。

    这也是"指标真的作用到门限上"的证据（不只是 aspect 一个指标在起作用）。
    """
    import math as _math

    model = topology.build_topology("wingbox_v1", BASE_PARAMS)
    base = meshgen.build_mesh(model, subdiv_per_bay=P1_SUBDIV)
    surface = (lambda y, xi: 0.5 * _math.sin(_math.pi * xi)
               + 0.4 * (y / BASE_PARAMS["span"]) * xi)

    before = mq.evaluate(model, base, {"surface": surface})["worst"]["warp"]
    if before <= 0.0:
        fails.append("扭转+弯度曲面上的翘曲度应 > 0，实际 %r" % before)
        return

    quality = mq.apply_gate(model, base,
                            {"surface": surface, "warp_max": before * 0.5,
                             "max_refine": 3})
    if not quality["refined_bays"]:
        fails.append("翘曲度超标时应触发加密，实际未加密")
    if quality["worst"]["warp"] >= before:
        fails.append("加密后翘曲度应下降（%.6f -> %.6f）"
                     % (before, quality["worst"]["warp"]))
    if quality["n_cross_bay_elems"] != 0 or quality["n_hanging_nodes"] != 0:
        fails.append("曲面加密后跨界单元/悬挂节点应为 0，实际 %d / %d"
                     % (quality["n_cross_bay_elems"], quality["n_hanging_nodes"]))
    ok, problems = meshgen.verify_no_straddle(model, quality["mesh"])
    if not ok:
        fails.append("曲面加密后 verify_no_straddle 不通过: %r" % (problems[:3],))
    # 翘曲超标是"两方向都加倍"的收敛型指标（与只加倍长边的 aspect 不同）
    for item in quality["refined_bays"][:3]:
        before_sub, after_sub = item["subdiv_before"], item["subdiv_after"]
        if after_sub[0] <= before_sub[0] and after_sub[1] <= before_sub[1]:
            fails.append("翘曲超标时两方向应至少有一个加倍: %s %r -> %r"
                         % (item["bay_id"], before_sub, after_sub))
        break


def test_max_refine_warning_and_trace(fails):
    """4) 达上限仍不达标 -> 警告明确，且写进质量报告与 mesh 产物 params。"""
    # 物理 100m × 1m 的单格：长宽比 100，1 轮加密只能到 50，必然不达标
    model = _mini_model(0.0, 100.0, 0.0, 1.0, 1.0)
    base = meshgen.build_mesh(model, subdiv_per_bay=1)
    quality = mq.apply_gate(model, base,
                            {"aspect_max": 2.0, "max_refine": 1, "enabled": True})

    if not quality["warn_bays"]:
        fails.append("达上限仍不达标时必须报出 warn_bays，实际为空")
    if quality["n_rounds"] != 1:
        fails.append("应恰好加密 1 轮（max_refine=1），实际 %d" % quality["n_rounds"])
    if not quality["warnings"]:
        fails.append("达上限仍不达标必须产生明确警告，实际无 warnings")
    else:
        blob = "".join(quality["warnings"])
        if "max_refine" not in blob or "不达标" not in blob:
            fails.append("警告未说明'达上限仍不达标'，实际: %s" % blob[:200])
    if not quality["refined_bays"]:
        fails.append("触到上限前应已加密过自然格（留痕）")

    # 留痕：报告可序列化并落盘，内容含超标格与门限
    report_path = os.path.join(TMP, "max_refine", "mesh_quality.json")
    mq.write_report(quality, report_path)
    if not os.path.isfile(report_path):
        fails.append("质量报告未落盘: %s" % report_path)
    else:
        with open(report_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if payload.get("acceptable") is not False:
            fails.append("留痕报告应标记 acceptable=False")
        if not payload.get("warn_bays"):
            fails.append("留痕报告缺少 warn_bays")
        if payload.get("criteria", {}).get("aspect_max") != 2.0:
            fails.append("留痕报告缺少判定门限 criteria")
        if payload.get("max_refine") != 1:
            fails.append("留痕报告缺少 max_refine")
        if not payload.get("warnings"):
            fails.append("留痕报告缺少 warnings")

    # 异常策略必须被拒绝（不能静默按默认跑）
    for bad in ({"max_refine": -1}, {"aspect_max": 0}, {"jacobian_min": "x"}):
        try:
            mq.normalize_policy(bad)
            fails.append("非法策略未被拒绝: %r" % (bad,))
        except mq.MeshQualityError:
            pass


# ---------------------------------------------------------------------------
# 5) 可关闭自动加密（仅出质量报告）
# ---------------------------------------------------------------------------

def test_auto_refine_off(fails):
    """5) auto_refine=False：只出报告，网格与不加门时完全一致。"""
    model = topology.build_topology("wingbox_v1", BASE_PARAMS)
    base = meshgen.build_mesh(model, subdiv_per_bay=P1_SUBDIV)

    off = mq.apply_gate(model, base, {"auto_refine": False, "aspect_max": 1.5})

    if off["refined_bays"]:
        fails.append("auto_refine=False 时不应加密任何自然格，实际 %r"
                     % (off["refined_bays"],))
    if off["n_rounds"] != 0:
        fails.append("auto_refine=False 时轮数应为 0，实际 %d" % off["n_rounds"])
    if off["auto_refine"] is not False:
        fails.append("结果应记录 auto_refine=False")
    # 网格与不加门时逐项一致（同一对象内容，不是重建）
    if off["n_elems"] != len(base.elems):
        fails.append("auto_refine=False 时单元数应不变（%d），实际 %d"
                     % (len(base.elems), off["n_elems"]))
    if [tuple(e["nodes"]) for e in off["mesh"].elems] != \
            [tuple(e["nodes"]) for e in base.elems]:
        fails.append("auto_refine=False 时应交付原网格，实际单元连接被改动")
    if off["mesh"] is not base:
        fails.append("auto_refine=False 时应直接交出传入网格")

    # 质量报告仍然给出（超标格被报出 + 明确警告）
    if not off["warn_bays"]:
        fails.append("auto_refine=False 时仍应报出超标格（aspect_max=1.5）")
    if not off["warnings"]:
        fails.append("auto_refine=False 且不达标时应有明确警告")
    elif "auto_refine" not in "".join(off["warnings"]):
        fails.append("警告未说明'未自动加密（auto_refine=False）'口径")
    if off["criteria"].get("aspect_max") != 1.5:
        fails.append("关闭加密不应改变判定门限，实际 %r" % (off["criteria"],))

    # enabled=False：整道门关闭，网格原样交出
    closed = mq.apply_gate(model, base, {"enabled": False})
    if closed["enabled"] is not False or closed["mesh"] is not base:
        fails.append("enabled=False 时应原样交出网格")
    if closed["warnings"]:
        fails.append("enabled=False 时不应产生质量结论/警告")


# ---------------------------------------------------------------------------
# 6) 默认策略下机翼盒段零回归
# ---------------------------------------------------------------------------

def _mesh_signature(mesh):
    nodes = tuple((n[0], round(n[1], 12), round(n[2], 12)) for n in mesh.nodes)
    elems = tuple((e["id"], tuple(e["nodes"]), e["bay_id"], e["bay_valid"])
                  for e in mesh.elems)
    return nodes, elems


def test_default_policy_zero_regression(fails):
    """6) 默认策略下机翼既有网格零回归：单元/节点/连接与 P1 逐项一致。"""
    model = topology.build_topology("wingbox_v1", BASE_PARAMS)
    p1 = meshgen.build_mesh(model, subdiv_per_bay=P1_SUBDIV)
    if len(p1.elems) != P1_N_BAYS * P1_SUBDIV ** 2:
        fails.append("P1 基线单元数应为 %d，实际 %d"
                     % (P1_N_BAYS * P1_SUBDIV ** 2, len(p1.elems)))

    # 默认策略（不给任何键）
    default = mq.apply_gate(model, p1, None)
    if default["refined_bays"]:
        fails.append("默认策略下机翼网格不应被加密（既有网格本就达标），实际 %r"
                     % ([t["bay_id"] for t in default["refined_bays"][:3]],))
    if default["warn_bays"]:
        fails.append("默认策略下机翼网格不应有超标格，实际 %r"
                     % (default["warn_bays"][:3],))
    if default["n_elems"] != len(p1.elems):
        fails.append("默认策略下单元数变化: %d != %d"
                     % (default["n_elems"], len(p1.elems)))
    if default["n_nodes"] != len(p1.nodes):
        fails.append("默认策略下节点数变化: %d != %d"
                     % (default["n_nodes"], len(p1.nodes)))
    if _mesh_signature(default["mesh"]) != _mesh_signature(p1):
        fails.append("默认策略下网格节点/连接与 P1 不一致（零回归被破坏）")
    if default["n_cross_bay_elems"] != 0 or default["n_hanging_nodes"] != 0:
        fails.append("默认策略下跨界单元/悬挂节点应为 0，实际 %d / %d"
                     % (default["n_cross_bay_elems"],
                        default["n_hanging_nodes"]))

    # 端到端：带门与不带门写出的 .inp 必须逐字节一致
    out_off = os.path.join(TMP, "regress_off")
    out_on = os.path.join(TMP, "regress_on")
    fe_off = fe_model.build_fe_model(model, out_off, subdiv_per_bay=P1_SUBDIV)
    fe_on = fe_model.build_fe_model(model, out_on, subdiv_per_bay=P1_SUBDIV,
                                    mesh_quality={"enabled": True})
    if fe_on.mesh_quality is None:
        fails.append("启用质量门后 FeModel.mesh_quality 应记录报告")
    if fe_off.mesh_dict["n_elems"] != fe_on.mesh_dict["n_elems"]:
        fails.append("带门与不带门单元数不一致: %d != %d"
                     % (fe_off.mesh_dict["n_elems"], fe_on.mesh_dict["n_elems"]))

    def _digest(path):
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    if os.path.isfile(fe_off.inp_path) and os.path.isfile(fe_on.inp_path):
        if _digest(fe_off.inp_path) != _digest(fe_on.inp_path):
            fails.append("默认策略下 .inp 与 P1 不一致（零回归被破坏）")
    else:
        fails.append(".inp 未写出: %r / %r" % (fe_off.inp_path, fe_on.inp_path))


# ---------------------------------------------------------------------------
# 破戒时必须抛错（不得静默交付跨界网格）
# ---------------------------------------------------------------------------

def test_gate_refuses_straddle(fails):
    """自检非摆设：加密若产出跨自然格单元，质量门必须抛错而不是静默交付。"""
    # 沿 y 方向两块自然格（肋 y=0/1/2），横跨两者的单元即"跨界单元"
    y_lo, y_hi, xi_lo, xi_hi = 0.0, 2.0, 0.0, 1.0
    stringers = [{"xi": xi_lo, "y0": y_lo, "y1": y_hi},
                 {"xi": xi_hi, "y0": y_lo, "y1": y_hi}]
    ribs = [{"y": 0.0, "xi0": xi_lo, "xi1": xi_hi},
            {"y": 1.0, "xi0": xi_lo, "xi1": xi_hi},
            {"y": 2.0, "xi0": xi_lo, "xi1": xi_hi}]
    model = topology.TopologyModel(
        "mini2", {}, stringers, ribs, [],
        physical={"xi_scale": 1.0, "y_unit": "m", "xi_unit": "m"})
    model.bays = topology.build_bays(stringers, ribs)
    if len(model.bays) != 2:
        fails.append("跨界自检夹具应含 2 个自然格，实际 %d" % len(model.bays))
        return

    base = meshgen.build_mesh(model, subdiv_per_bay=1)

    # 先证明检测本身有效：手工造一个纵跨两格的单元（归属下格）
    bad = meshgen.Mesh()
    ids = [bad.node(0.0, xi_lo), bad.node(0.0, xi_hi),
           bad.node(2.0, xi_hi), bad.node(2.0, xi_lo)]
    bad.elems.append({"id": 1, "nodes": tuple(ids),
                      "bay_id": model.bays[0].id, "bay_valid": True})
    n_bad, _problems = mq.count_cross_bay_elems(model, bad)
    if n_bad != 1:
        fails.append("跨自然格单元应被检出 1 个，实际 %d" % n_bad)
    ok, _ = meshgen.verify_no_straddle(model, bad)
    if ok:
        fails.append("verify_no_straddle 未检出跨界单元（自检形同摆设）")

    # 再证明质量门会拒绝：把重建函数临时换成"总是产出跨界网格"
    orig = meshgen.build_mesh

    def _straddling(*_args, **_kwargs):
        return bad

    meshgen.build_mesh = _straddling
    try:
        try:
            # aspect_max=0.5：正方形单元必然"超标"，从而触发一次重划
            mq.apply_gate(model, base, {"aspect_max": 0.5, "max_refine": 1})
            fails.append("加密产出跨界网格时质量门未抛错（会静默交付，违反 ADR-0010）")
        except mq.MeshQualityError:
            pass
    finally:
        meshgen.build_mesh = orig


def test_conformity_check_is_effective(fails):
    """界面协调校验不是摆设：手工造悬挂节点必须被检出。"""
    y_lo, y_hi, xi_lo, xi_hi = 0.0, 2.0, 0.0, 1.0
    stringers = [{"xi": xi_lo, "y0": y_lo, "y1": y_hi},
                 {"xi": xi_hi, "y0": y_lo, "y1": y_hi}]
    ribs = [{"y": 0.0, "xi0": xi_lo, "xi1": xi_hi},
            {"y": 1.0, "xi0": xi_lo, "xi1": xi_hi},
            {"y": 2.0, "xi0": xi_lo, "xi1": xi_hi}]
    model = topology.TopologyModel(
        "mini3", {}, stringers, ribs, [],
        physical={"xi_scale": 1.0, "y_unit": "m", "xi_unit": "m"})
    model.bays = topology.build_bays(stringers, ribs)

    # 下格 2x2，上格 3x3：y=1.0 界面上节点数不同 -> 悬挂节点
    good = meshgen.build_mesh(model, subdiv_by_bay={model.bays[0].id: 2,
                                                    model.bays[1].id: 2})
    n_ok, _ = mq.check_conformity(model, good)
    if n_ok != 0:
        fails.append("协调网格被误判为有悬挂节点（%d）" % n_ok)

    bad = meshgen.build_mesh(model, subdiv_by_bay={model.bays[0].id: 2,
                                                   model.bays[1].id: 3})
    n_bad, details = mq.check_conformity(model, bad)
    if n_bad <= 0:
        fails.append("界面节点数不一致时 check_conformity 未检出悬挂节点")
    if not details:
        fails.append("检出悬挂节点时未给出说明")


# ---------------------------------------------------------------------------
# 端到端：run_study 产物留痕
# ---------------------------------------------------------------------------

def test_end_to_end_trace(fails):
    """4) 端到端：不达标结论进入 result["warnings"] 与 mesh 产物 params。"""
    dbfile = os.path.join(TMP, "m15.db")
    if os.path.isfile(dbfile):
        os.remove(dbfile)
    conn = db.connect(dbfile)
    study.create_study("S-M15", "M15 质量门", conn=conn)

    # aspect_max=2.0：机翼格 4.8 超标；max_refine=0 -> 不加密，必然留痕
    res = run_study.run_study(
        "S-M15", BASE_PARAMS, "wingbox_v1",
        FakeCatiaDriver(os.path.join(TMP, "catparts")), conn,
        params_dir=os.path.join(TMP, "params"),
        with_fe_model=True, fe_dir=os.path.join(TMP, "fe"),
        subdiv_per_bay=P1_SUBDIV,
        mesh_quality={"enabled": True, "auto_refine": True, "max_refine": 0,
                      "aspect_max": 2.0})

    q = res.get("mesh_quality")
    if not q:
        fails.append("result 缺少 mesh_quality 报告")
    else:
        if not q.get("warn_bays"):
            fails.append("端到端结果应报出超标格")
        if q.get("criteria", {}).get("aspect_max") != 2.0:
            fails.append("端到端结果应记录判定门限")
        if q.get("n_cross_bay_elems") != 0:
            fails.append("端到端跨自然格单元数应为 0")

    # 汇入全链 warnings（报告可用它说明结论边界）
    if not any("网格质量" in str(w) for w in (res.get("warnings") or [])):
        fails.append("mesh_quality.warnings 未汇入 result['warnings']: %r"
                     % (res.get("warnings"),))

    # mesh 产物 params 留痕
    row = conn.execute(
        "SELECT params_json FROM artifacts WHERE id=?",
        (res["artifacts"]["mesh"],)).fetchone()
    params = json.loads(row["params_json"])
    if params.get("mesh_quality_enabled") is not True:
        fails.append("mesh 产物未记录 mesh_quality_enabled")
    if not params.get("mesh_quality_n_warn_bays"):
        fails.append("mesh 产物未记录超标格数（n_warn_bays）")
    if params.get("mesh_quality_max_aspect") is None:
        fails.append("mesh 产物未记录最坏长宽比")

    # 默认策略（未给策略）端到端零回归：产物与 P1 同口径
    study.create_study("S-M15-NONE", "M15 无门", conn=conn)
    res2 = run_study.run_study(
        "S-M15-NONE", BASE_PARAMS, "wingbox_v1",
        FakeCatiaDriver(os.path.join(TMP, "catparts2")), conn,
        params_dir=os.path.join(TMP, "params2"),
        with_fe_model=True, fe_dir=os.path.join(TMP, "fe2"),
        subdiv_per_bay=P1_SUBDIV)
    if res2.get("mesh_quality"):
        fails.append("未启用质量门时 result 不应有 mesh_quality")
    if res2["fe"]["n_elems"] != res["fe"]["n_elems"]:
        fails.append("不加密时单元数应与无门一致: %d != %d"
                     % (res2["fe"]["n_elems"], res["fe"]["n_elems"]))
    if res2["fe"]["n_elems"] != P1_N_BAYS * P1_SUBDIV ** 2:
        fails.append("机翼 P1 单元数应为 %d，实际 %d"
                     % (P1_N_BAYS * P1_SUBDIV ** 2, res2["fe"]["n_elems"]))

    conn.close()


# ---------------------------------------------------------------------------
# 7) Python 3.8 兼容
# ---------------------------------------------------------------------------

def test_py38(fails):
    """7) Python 3.8 兼容：本票涉及的模块不得用 3.9+ 语法/标准库。"""
    import compat_py38

    targets = [
        os.path.join(CORE, "mesh_quality.py"),
        os.path.join(CORE, "meshgen.py"),
        os.path.join(CORE, "fe_model.py"),
        os.path.join(HERE, "test_m15.py"),
    ]
    bad = compat_py38.check_paths(targets)
    if bad:
        fails.append("Python 3.8 兼容性检查未通过:\n%s"
                     % compat_py38.format_violations(bad))


def main():
    if os.path.isdir(TMP):
        shutil.rmtree(TMP)
    os.makedirs(TMP)

    fails = []
    print("1-2. 质量指标计算与手算对照 ...")
    test_metrics_handcheck(fails)
    print("3. 自动加密后仍唯一归属自然格（ADR-0010） ...")
    test_refine_keeps_alignment(fails)
    print("3b. 翘曲度超标的自动加密（曲面壳） ...")
    test_warp_refine_with_surface(fails)
    print("4. 达加密上限仍不达标：警告 + 产物留痕 ...")
    test_max_refine_warning_and_trace(fails)
    print("5. 可关闭自动加密（仅出报告） ...")
    test_auto_refine_off(fails)
    print("6. 默认策略下机翼零回归 ...")
    test_default_policy_zero_regression(fails)
    print("7. 破戒必须抛错（ADR-0010 自检） ...")
    test_gate_refuses_straddle(fails)
    print("7b. 界面悬挂节点校验 ...")
    test_conformity_check_is_effective(fails)
    print("8. 端到端留痕 ...")
    test_end_to_end_trace(fails)
    print("9. Python 3.8 兼容 ...")
    test_py38(fails)

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M15 (票15) SELF-TEST: ALL PASSED "
          "(指标手算对照 / 加密不跨界 / 达上限留痕 / 可关加密 / 机翼零回归 / "
          "破戒抛错 / py3.8)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
