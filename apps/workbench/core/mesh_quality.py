# -*- coding: utf-8 -*-
"""
mesh_quality.py -- 网格质量门与自动加密（票 15）

对应 ADR-0010（网格与拓扑对齐：每个单元唯一归属一个自然格，禁止跨自然格单元）
与 ADR-0008（自然格一等公民）。术语见 GLOSSARY：自然格 / 结构拓扑模型。

## 为什么需要质量门

网格已按自然格切分（`meshgen`），"归属正确"只是必要条件；**每个自然格内部**
网格形状是否可用，仍需评估：一条又长又窄的格子（长宽比过大）或严重剪切/翘曲的
单元，会让 S4R 壳单元的弯曲刚度失真，强度校核的极值也就不可信。所以：

  * 逐自然格给出 **畸变度（scaled jacobian）**、**长宽比（aspect ratio）**、
    **翘曲度（warp）**；
  * 超标的自然格**自动加密**，直到达标或达到加密上限 `max_refine`；
  * 加密**只能改每个自然格的细分数**，不许动格边界 —— 因此跨自然格单元恒为 0
    （ADR-0010 硬约束，本模块每次重建后都自检，破戒直接抛错，绝不静默交付）。

## 物理尺寸（不要把 0..1 当物理长度）

参数空间是 (y, xi)，其中 xi 是 0..1 的无量纲量。物理长度换算一律走
`TopologyModel.extents()`：

    物理 X = (xi - xi_min) * xi_scale
    物理 Y = y

`xi_scale` 由拓扑层按部件族给出（翼面类 = 根弦长；筒段类 = 展开周长），
所以本模块**部件族无关**，翼面/尾翼/机身筒段共用同一套指标。

## 指标定义（可手算对照）

每个单元 4 个角点 p0..p3（按网格约定逆时针：(y0,xi0) (y0,xi1) (y1,xi1) (y1,xi0)），
先换到物理坐标，再算：

1. **畸变度 jacobian（= scaled jacobian，门限 `jacobian_min`）**
   四个角上相邻两边的**有符号夹角正弦**，取最小值：

       q = min_i  ( e_{i-1} x e_i · n ) / (|e_{i-1}| |e_i|)

   `n` 是单元参考法向。矩形/平行四边形夹角 90 度 -> q = 1.0；
   被剪成 45 度 -> q = sin45 = 0.7071；凹/翻转 -> q < 0。取值 (-1, 1]。
2. **雅可比比 jacobian_ratio**（参考用，不设门限）
   双线性映射在 4 个 Gauss 点的 |J| 的 min/max；矩形 = 1.0。
3. **长宽比 aspect_ratio（门限 `aspect_max`）**
   单元四条物理边长 max/min；矩形 a×b 即 max(a,b)/min(a,b)。
4. **翘曲度 warp（门限 `warp_max`）**
   节点 p3 到平面 (p0,p1,p2) 的距离 h，除以平均边长：

       warp = h / mean(|e_i|)

   平面网格恒为 0；曲面壳（如带弯度的翼面）上曲面偏离越大 warp 越大。
   曲面高度可由 `policy["surface"]`（callable(y, xi) -> z）或节点 4 元组
   (id, y, xi, z) 提供；都没有则视为平面。

注意：本节指标只从**实际节点坐标**算，所以对 `meshgen` 产出的规则网格与
手工构造的坏网格（剪切、翘曲）都成立，便于单测用手算值对照。

## 自动加密的两条硬规则

1. **各向异性加密**：矩形自然格里，等比细化 n×n 不会改变单元长宽比
   （长宽比 = 格的长宽比）。所以**只对长宽比超标**时按**长边方向**加倍；
   畸变度/翘曲度超标则两方向同时加倍（这两者是"单元越小越好"的收敛型指标，
   但若物理形状本身病态，会一路报到上限，给出"该改拓扑"的明确警告）。
2. **界面协调**：加密一个格会改变它与邻居共用边界上的节点数，直接重划会产生
   **悬挂节点**（界面不连续 -> 载荷传不过去）。因此加密请求会按"行（同 rib_j 的
   ny）/ 列（同 str_i 的 nx）"传播，保证相邻格界面节点一致；
   `check_conformity()` 可独立校验。

纯标准库、纯算法，不依赖 CATIA / Abaqus。兼容 Python 3.8（禁用 3.9+ 语法）。
"""

import json
import math
import os

try:
    from . import meshgen
except ImportError:
    import meshgen


# ---------------------------------------------------------------------------
# 策略（policy）
# ---------------------------------------------------------------------------

#: 未给键一律走这里的默认值
DEFAULT_POLICY = {
    "enabled": True,          # 关掉整道门（网格原样交出）
    "auto_refine": True,      # 只出质量报告、不自动加密
    "max_refine": 3,          # 加密轮数上限（每轮按方向加倍细分数）
    "jacobian_min": 0.6,      # 畸变度下限
    "aspect_max": 5.0,        # 长宽比上限
    "warp_max": 0.1,          # 翘曲度上限（相对平均边长）
    "require_conforming": True,   # 界面出现悬挂节点时抛错（否则只警告）
    "surface": None,          # 可选 callable(y, xi) -> z，曲面壳的翘曲评估
}

#: 参与判定的三个门限键（其余键是行为开关）
CRITERIA_KEYS = ("jacobian_min", "aspect_max", "warp_max")

_EDGE_TOL = 1e-9


class MeshQualityError(ValueError):
    """质量门无法交付合规网格（策略非法 / 加密破坏 ADR-0010 / 界面不协调）。"""


def normalize_policy(policy=None):
    """
    归一化策略 dict：未给键用 `DEFAULT_POLICY`，并把类型收紧成可判定值。

    policy 为 None / {} 时得到默认策略；未知键原样保留（向前兼容）。
    返回新的 dict（不改调用方对象）。
    """
    out = dict(DEFAULT_POLICY)
    if policy:
        if not isinstance(policy, dict):
            raise MeshQualityError("网格质量策略 policy 必须是 dict，实际 %r"
                                   % type(policy).__name__)
        for key in policy:
            out[key] = policy[key]

    out["enabled"] = bool(out.get("enabled", True))
    out["auto_refine"] = bool(out.get("auto_refine", True))
    out["require_conforming"] = bool(out.get("require_conforming", True))

    try:
        max_refine = int(out.get("max_refine", 3))
    except (TypeError, ValueError):
        raise MeshQualityError("policy.max_refine 必须是整数，实际 %r"
                               % (out.get("max_refine"),))
    if max_refine < 0:
        raise MeshQualityError("policy.max_refine 不能为负: %d" % max_refine)
    out["max_refine"] = max_refine

    for key in CRITERIA_KEYS:
        try:
            value = float(out[key])
        except (TypeError, ValueError):
            raise MeshQualityError("policy.%s 必须是数值，实际 %r"
                                   % (key, out.get(key)))
        if not value > 0.0:
            raise MeshQualityError("policy.%s 必须为正数，实际 %r" % (key, value))
        out[key] = value

    return out


def criteria_of(policy=None):
    """只取三个门限，组成判定口径 dict（写进产物的 `criteria`）。"""
    pol = normalize_policy(policy)
    return {
        "jacobian_min": pol["jacobian_min"],
        "aspect_max": pol["aspect_max"],
        "warp_max": pol["warp_max"],
    }


# ---------------------------------------------------------------------------
# 几何小工具（纯标准库）
# ---------------------------------------------------------------------------

def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _norm(a):
    return math.sqrt(_dot(a, a))


def _unit(a):
    n = _norm(a)
    if n <= 1e-15:
        return None
    return (a[0] / n, a[1] / n, a[2] / n)


def _extents(model):
    """取拓扑层的物理尺度（y / xi 换算系数）。缺失即视为调用方用错对象。"""
    try:
        return model.extents()
    except AttributeError:
        raise MeshQualityError(
            "model 必须是 topology.TopologyModel（需要 extents() 做参数空间→物理换算）")


def _node_table(mesh):
    """
    节点表 id -> (y, xi, z)。

    网格节点是 (id, y, xi)（`meshgen.Mesh`），也接受 (id, y, xi, z)
    以便表达曲面壳的离面高度（翘曲度评估用）。
    """
    table = {}
    for node in mesh.nodes:
        nid = node[0]
        z = float(node[3]) if len(node) > 3 else 0.0
        table[nid] = (float(node[1]), float(node[2]), z)
    return table


def _to_physical(ext, y, xi, z=0.0):
    """参数空间 (y, xi) -> 物理坐标 (X, Y, Z)；xi 乘 xi_scale（不是 0..1）。"""
    return ((float(xi) - ext["xi_min"]) * ext["xi_scale"], float(y), float(z))


def _bay_index(mesh):
    """bay_id -> [elem]（一次扫描，逐格评估复用）。"""
    index = {}
    for elem in mesh.elems:
        index.setdefault(elem["bay_id"], []).append(elem)
    return index


# ---------------------------------------------------------------------------
# 单元指标
# ---------------------------------------------------------------------------

def _dP_dr(pts, r, s):
    """双线性形函数的 dP/dr（点序 p0(-1,-1) p1(+1,-1) p2(+1,+1) p3(-1,+1)）。"""
    out = []
    for k in range(3):
        out.append((-(1.0 - s) * pts[0][k] + (1.0 - s) * pts[1][k]
                    + (1.0 + s) * pts[2][k] - (1.0 + s) * pts[3][k]) / 4.0)
    return (out[0], out[1], out[2])


def _dP_ds(pts, r, s):
    """双线性形函数的 dP/ds。"""
    out = []
    for k in range(3):
        out.append((-(1.0 - r) * pts[0][k] - (1.0 + r) * pts[1][k]
                    + (1.0 + r) * pts[2][k] + (1.0 - r) * pts[3][k]) / 4.0)
    return (out[0], out[1], out[2])


def _jacobian_ratio(pts):
    """4 个 Gauss 点 |J| 的 min/max（矩形为 1.0，退化/翻转 <= 0 时返回 0.0）。"""
    g = 1.0 / math.sqrt(3.0)
    dets = []
    for r in (-g, g):
        for s in (-g, g):
            jac = _cross(_dP_dr(pts, r, s), _dP_ds(pts, r, s))
            dets.append(_norm(jac))
    highest = max(dets)
    if highest <= 1e-15:
        return 0.0
    return min(dets) / highest


def element_metrics(model, mesh, elem, surface=None, node_table=None, ext=None):
    """
    单个单元的物理质量指标（票15 验收：可计算且可手算对照）。

    elem        : mesh.elems 里的一项（含 nodes / bay_id）
    surface     : 可选 callable(y, xi) -> z（曲面壳离面高度）；None 用节点自带 z
    node_table  : 可选，`_node_table(mesh)` 的缓存（批量评估时避免重复建表）
    ext         : 可选，`model.extents()` 的缓存

    返回 dict：
      jacobian / scaled_jacobian : 畸变度（同一数值，见模块 docstring 定义）
      jacobian_ratio             : 参考指标
      aspect_ratio               : 长宽比（max/min 物理边长）
      warp                       : 翘曲度（离面高 / 平均边长）
    """
    ext = ext if ext is not None else _extents(model)
    table = node_table if node_table is not None else _node_table(mesh)
    return _metrics_from_nodes(ext, table, elem, surface)


def _metrics_from_nodes(ext, table, elem, surface=None):
    """已备好 ext / 节点表时的指标计算内核（批量评估走这里，避免重复建表）。"""
    pts = []
    for nid in elem["nodes"]:
        y, xi, z_node = table[nid]
        z = float(surface(y, xi) or 0.0) if surface is not None else z_node
        pts.append(_to_physical(ext, y, xi, z))

    edges = [_sub(pts[(i + 1) % 4], pts[i]) for i in range(4)]
    lens = [_norm(e) for e in edges]
    min_len = min(lens)
    max_len = max(lens)
    mean_len = sum(lens) / 4.0
    if min_len > 1e-12:
        aspect = max_len / min_len
    else:
        aspect = float("inf")

    normal = _cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0]))
    n_hat = _unit(normal)
    if n_hat is None or mean_len <= 1e-12:
        # 三点共线 / 零尺寸单元：彻底退化
        scaled_jac = 0.0
        warp = 0.0
        jac_ratio = 0.0
    else:
        sines = []
        for i in range(4):
            a = edges[(i - 1) % 4]
            b = edges[i]
            denom = _norm(a) * _norm(b)
            if denom <= 1e-15:
                sines.append(0.0)
            else:
                sines.append(_dot(_cross(a, b), n_hat) / denom)
        scaled_jac = min(sines)
        height = abs(_dot(_sub(pts[3], pts[0]), n_hat))
        warp = height / mean_len
        jac_ratio = _jacobian_ratio(pts)

    return {
        "elem_id": elem["id"],
        "bay_id": elem["bay_id"],
        "jacobian": scaled_jac,
        "scaled_jacobian": scaled_jac,
        "jacobian_ratio": jac_ratio,
        "aspect_ratio": aspect,
        "warp": warp,
    }


# ---------------------------------------------------------------------------
# 自然格指标
# ---------------------------------------------------------------------------

def _bay_record(bay, elems, surface, ext, table):
    """
    一个自然格的聚合指标：畸变度取最小、长宽比/翘曲度取最大（最坏单元说话）。
    空格（未划网格）返回中性值且 n_elems = 0，不参与判定。
    """
    if elems:
        worst_jac = None
        worst_aspect = None
        worst_warp = None
        for elem in elems:
            m = _metrics_from_nodes(ext, table, elem, surface)
            if worst_jac is None or m["jacobian"] < worst_jac:
                worst_jac = m["jacobian"]
            if worst_aspect is None or m["aspect_ratio"] > worst_aspect:
                worst_aspect = m["aspect_ratio"]
            if worst_warp is None or m["warp"] > worst_warp:
                worst_warp = m["warp"]
    else:
        worst_jac, worst_aspect, worst_warp = 1.0, 1.0, 0.0

    return {
        "bay_id": bay.id,
        "rib_j": bay.rib_j,
        "str_i": bay.str_i,
        "valid": bay.valid,
        "n_elems": len(elems),
        "jacobian": worst_jac,
        "scaled_jacobian": worst_jac,
        "aspect_ratio": worst_aspect,
        "warp": worst_warp,
        "size": [bay.y1 - bay.y0, (bay.xi1 - bay.xi0) * ext["xi_scale"]],
    }


def bay_metrics(model, mesh, bay_id, policy=None, surface=None):
    """
    单个自然格的网格质量指标（票15 验收：可计算每个自然格的指标）。

    返回 dict（键见 `_bay_record`）；未知 bay_id 抛 KeyError。
    """
    pol = normalize_policy(policy)
    if surface is None:
        surface = pol.get("surface")
    ext = _extents(model)
    table = _node_table(mesh)
    for bay in model.bays:
        if bay.id == bay_id:
            return _bay_record(bay, _bay_index(mesh).get(bay_id, []),
                               surface, ext, table)
    raise KeyError("未知自然格 %r" % (bay_id,))


def _violations(record, criteria):
    """按门限判定一个自然格，返回 [{'criterion','value','limit'}]。"""
    out = []
    if record["n_elems"] <= 0:
        return out
    if record["jacobian"] < criteria["jacobian_min"]:
        out.append({"criterion": "jacobian", "value": record["jacobian"],
                    "limit": criteria["jacobian_min"]})
    if record["aspect_ratio"] > criteria["aspect_max"]:
        out.append({"criterion": "aspect_ratio", "value": record["aspect_ratio"],
                    "limit": criteria["aspect_max"]})
    if record["warp"] > criteria["warp_max"]:
        out.append({"criterion": "warp", "value": record["warp"],
                    "limit": criteria["warp_max"]})
    return out


def _worst_of(records):
    """整网格最坏值（写进产物的 `worst`）。空网格时各项为 None。"""
    if not records:
        return {"jacobian": None, "aspect_ratio": None, "warp": None,
                "bay_id": None, "by_metric": {}}

    by_metric = {}
    min_jac = None
    min_jac_bay = None
    max_aspect = None
    max_aspect_bay = None
    max_warp = None
    max_warp_bay = None
    for rec in records:
        if rec["n_elems"] <= 0:
            continue
        if min_jac is None or rec["jacobian"] < min_jac:
            min_jac, min_jac_bay = rec["jacobian"], rec["bay_id"]
        if max_aspect is None or rec["aspect_ratio"] > max_aspect:
            max_aspect, max_aspect_bay = rec["aspect_ratio"], rec["bay_id"]
        if max_warp is None or rec["warp"] > max_warp:
            max_warp, max_warp_bay = rec["warp"], rec["bay_id"]

    by_metric["jacobian"] = min_jac_bay
    by_metric["aspect_ratio"] = max_aspect_bay
    by_metric["warp"] = max_warp_bay

    flagged = [r["bay_id"] for r in records if r.get("violations")]
    return {
        "jacobian": min_jac,
        "aspect_ratio": max_aspect,
        "warp": max_warp,
        "bay_id": flagged[0] if flagged else max_aspect_bay,
        "by_metric": by_metric,
    }


# ---------------------------------------------------------------------------
# 整网格评估
# ---------------------------------------------------------------------------

def evaluate(model, mesh, policy=None, surface=None):
    """
    评估整网格质量（门限只用于判定，不加密）。

    返回 dict：
      criteria   : 本次判定用的门限
      bays       : 每个自然格一条记录（含 violations）
      warn_bays  : 超标的自然格 id（按 model.bays 顺序）
      worst      : 整网格最坏值 {jacobian, aspect_ratio, warp, bay_id, by_metric}
      n_elems / n_nodes / n_bays
    """
    pol = normalize_policy(policy)
    if surface is None:
        surface = pol.get("surface")
    criteria = criteria_of(pol)

    ext = _extents(model)
    table = _node_table(mesh)
    index = _bay_index(mesh)

    records = []
    for bay in model.bays:
        rec = _bay_record(bay, index.get(bay.id, []), surface, ext, table)
        rec["violations"] = _violations(rec, criteria)
        records.append(rec)

    return {
        "criteria": criteria,
        "bays": records,
        "warn_bays": [r["bay_id"] for r in records if r["violations"]],
        "worst": _worst_of(records),
        "n_elems": len(mesh.elems),
        "n_nodes": len(mesh.nodes),
        "n_bays": len(records),
    }


# ---------------------------------------------------------------------------
# 界面协调（悬挂节点）校验
# ---------------------------------------------------------------------------

def _bay_side_nodes(model, index, table):
    """
    (bay_id, 'y0'|'y1'|'xi0'|'xi1') -> 该侧边界上的节点 id 集合。
    """
    sides = {}
    for bay in model.bays:
        keys = {}
        for side in ("y0", "y1", "xi0", "xi1"):
            keys[side] = set()
        for elem in index.get(bay.id, []):
            for nid in elem["nodes"]:
                y, xi, _z = table[nid]
                if abs(y - bay.y0) <= _EDGE_TOL:
                    keys["y0"].add(nid)
                if abs(y - bay.y1) <= _EDGE_TOL:
                    keys["y1"].add(nid)
                if abs(xi - bay.xi0) <= _EDGE_TOL:
                    keys["xi0"].add(nid)
                if abs(xi - bay.xi1) <= _EDGE_TOL:
                    keys["xi1"].add(nid)
        for side in keys:
            sides[(bay.id, side)] = keys[side]
    return sides


def check_conformity(model, mesh):
    """
    检查相邻自然格共用边界上的节点是否一致（无悬挂节点）。

    网格按格独立划分、边上节点共坐标去重，所以只要两侧沿该边的细分数相同，
    节点集合就完全相同；加密若只改一格就会打破这一点（载荷传不过去）。

    返回 (n_mismatch, details)：n_mismatch 为不一致节点数之和，0 表示协调。
    """
    table = _node_table(mesh)
    index = _bay_index(mesh)
    sides = _bay_side_nodes(model, index, table)
    grid = {}
    for bay in model.bays:
        grid[(bay.rib_j, bay.str_i)] = bay

    n_mismatch = 0
    details = []
    for bay in model.bays:
        below = grid.get((bay.rib_j - 1, bay.str_i))
        if below is not None:
            left = sides.get((below.id, "y1"), set())
            right = sides.get((bay.id, "y0"), set())
            diff = left.symmetric_difference(right)
            if diff:
                n_mismatch += len(diff)
                details.append("%s 与 %s 在 y=%.6f 界面有 %d 个节点不一致"
                               % (below.id, bay.id, bay.y0, len(diff)))
        ahead = grid.get((bay.rib_j, bay.str_i - 1))
        if ahead is not None:
            left = sides.get((ahead.id, "xi1"), set())
            right = sides.get((bay.id, "xi0"), set())
            diff = left.symmetric_difference(right)
            if diff:
                n_mismatch += len(diff)
                details.append("%s 与 %s 在 xi=%.6f 界面有 %d 个节点不一致"
                               % (ahead.id, bay.id, bay.xi0, len(diff)))
    return n_mismatch, details


def count_cross_bay_elems(model, mesh):
    """
    跨自然格单元数（ADR-0010 硬指标，必须恒为 0）。

    单元四角点有一半落在所属自然格区间外即计一个；另有未知 bay_id 的单元也算违规。
    返回 (count, problems)。
    """
    ok, problems = meshgen.verify_no_straddle(model, mesh)
    if ok:
        return 0, []
    return len(problems), problems


# ---------------------------------------------------------------------------
# 自动加密
# ---------------------------------------------------------------------------

def _initial_subdiv(model, mesh):
    """当前每格细分数 {bay_id: (ny, nx)}：优先用 Mesh.subdiv 元数据，否则反推。"""
    meta = getattr(mesh, "subdiv", None) or {}
    index = _bay_index(mesh)
    out = {}
    for bay in model.bays:
        value = meta.get(bay.id)
        if isinstance(value, (tuple, list)) and len(value) >= 2:
            ny, nx = int(value[0]), int(value[1])
        elif isinstance(value, int):
            ny, nx = value, value
        else:
            # 无元数据（手工网格）：按单元数反推各向同性细分数
            count = len(index.get(bay.id, []))
            k = int(round(math.sqrt(count))) if count > 0 else 1
            ny = nx = k
        out[bay.id] = (max(ny, 1), max(nx, 1))
    return out


def _plan_refinement(model, report, subdiv, ext):
    """
    由超标格算出新的细分数 {bay_id: (ny, nx)}。

    1. 按指标方向性原则选加倍方向：
       * 畸变度（jacobian）/ 翘曲度（warp）：单元尺寸越小两者越小，故**两方向都加倍**；
       * 仅长宽比超标：**只加倍长边方向** —— 矩形按 n×n 等比加密不改变单元长宽比
         （格的长宽比就是单元的长宽比），等比加密是纯浪费。
    2. 界面协调传播：ny 在"行"(同 rib_j) 取最大、nx 在"列"(同 str_i) 取最大，
       保证相邻格共用边上的节点数一致（不产生悬挂节点）。
    """
    bay_by_id = dict((b.id, b) for b in model.bays)
    want_ny = {}
    want_nx = {}
    for bay in model.bays:
        want_ny[bay.id], want_nx[bay.id] = subdiv[bay.id]

    for rec in report["bays"]:
        kinds = set(v["criterion"] for v in rec["violations"])
        if not kinds:
            continue
        bay = bay_by_id[rec["bay_id"]]
        ny, nx = subdiv[bay.id]
        if "jacobian" in kinds or "warp" in kinds:
            want_ny[bay.id] = ny * 2
            want_nx[bay.id] = nx * 2
        else:
            len_y = (bay.y1 - bay.y0) / float(ny)
            len_x = (bay.xi1 - bay.xi0) * ext["xi_scale"] / float(nx)
            if len_y >= len_x:
                want_ny[bay.id] = ny * 2
            else:
                want_nx[bay.id] = nx * 2

    rows = {}
    cols = {}
    for bay in model.bays:
        rows.setdefault(bay.rib_j, []).append(bay.id)
        cols.setdefault(bay.str_i, []).append(bay.id)

    out = {}
    for bay in model.bays:
        ny = max(want_ny[bid] for bid in rows[bay.rib_j])
        nx = max(want_nx[bid] for bid in cols[bay.str_i])
        out[bay.id] = (ny, nx)
    return out


def _refined_trace(model, initial, final, rounds):
    """加密留痕：[{bay_id, subdiv_before, subdiv_after, rounds}]，按 model 顺序。"""
    trace = []
    for bay in model.bays:
        before = initial[bay.id]
        after = final[bay.id]
        if before != after:
            trace.append({
                "bay_id": bay.id,
                "subdiv_before": [before[0], before[1]],
                "subdiv_after": [after[0], after[1]],
                "rounds": rounds,
            })
    return trace


def _warning_lines(bay_ids, report, policy, refined):
    """为仍未达标的自然格生成明确警告（写进 result["warnings"] 与 mesh 产物）。"""
    by_id = dict((r["bay_id"], r) for r in report["bays"])
    reasons = []
    for bay_id in bay_ids[:5]:
        rec = by_id.get(bay_id) or {}
        parts = ["%s=%s(门限 %s)"
                 % (v["criterion"],
                    ("%.3f" % v["value"]) if v["criterion"] != "aspect_ratio"
                    else ("%.2f" % v["value"]),
                    ("%.3f" % v["limit"]) if v["criterion"] != "aspect_ratio"
                    else ("%.2f" % v["limit"]))
                 for v in rec.get("violations") or []]
        reasons.append("%s 的 %s" % (bay_id, "、".join(parts)))
    more = ""
    if len(bay_ids) > 5:
        more = "（另有 %d 个自然格同样超标）" % (len(bay_ids) - 5)

    if not policy["auto_refine"]:
        head = ("网格质量未达标且未自动加密（policy.auto_refine=False，仅出质量报告）："
                "%d 个自然格超标" % len(bay_ids))
    else:
        head = ("网格质量达加密上限 max_refine=%d 仍不达标：%d 个自然格超标"
                "（结论仅对该网格口径有效，需调整拓扑/长宽比或放宽策略）"
                % (policy["max_refine"], len(bay_ids)))
    if refined:
        head += "；已加密 %d 个自然格" % len(refined)
    return [head + "：" + "；".join(reasons) + more]


def _result(mesh, report, warnings, refined, policy, criteria, rounds,
            n_cross, n_hanging, enabled=True):
    """统一结果 dict（run_study / fe_model 只认这里约定的键）。"""
    worst = (report or {}).get("worst") or {
        "jacobian": None, "aspect_ratio": None, "warp": None, "bay_id": None,
        "by_metric": {}}
    return {
        "mesh": mesh,
        "enabled": bool(enabled),
        "auto_refine": bool(policy["auto_refine"]),
        "max_refine": int(policy["max_refine"]),
        "criteria": criteria,
        "bays": (report or {}).get("bays") or [],
        "warn_bays": (report or {}).get("warn_bays") or [],
        "refined_bays": refined,
        "worst": worst,
        "warnings": warnings,
        "n_rounds": rounds,
        "n_elems": len(mesh.elems),
        "n_nodes": len(mesh.nodes),
        "n_cross_bay_elems": n_cross,
        "n_hanging_nodes": n_hanging,
    }


def apply_gate(model, mesh, policy=None):
    """
    网格质量门：评估 -> 超标格自动加密 -> 复核（ADR-0010 不得破坏）。

    model  : topology.TopologyModel
    mesh   : meshgen.Mesh（通常是基准网格，如 subdiv_per_bay=4）
    policy : dict，见 `DEFAULT_POLICY`；None = 默认策略（会加密）

    返回 dict，至少含：
      mesh         : 交付用网格（可能已加密；auto_refine=False 时原样返回）
      warnings     : [str] 明确警告（达上限/未加密 时说明适用边界）
      warn_bays    : [bay_id] 复核后仍超标的自然格
      refined_bays : [{bay_id, subdiv_before, subdiv_after, rounds}] 加密留痕
      worst        : {jacobian, aspect_ratio, warp, bay_id, by_metric}
      criteria     : 本次判定用的门限
    另含 enabled / auto_refine / max_refine / bays / n_rounds / n_elems /
    n_cross_bay_elems / n_hanging_nodes（跨自然格单元数与悬挂节点数必须为 0）。

    加密每轮后都重跑 `meshgen.verify_no_straddle`：出现跨自然格单元（ADR-0010
    被破坏）立即抛 `MeshQualityError`，绝不静默交付。界面出现悬挂节点时，
    `require_conforming=True`（默认）同样抛错。
    """
    pol = normalize_policy(policy)
    criteria = criteria_of(pol)
    surface = pol.get("surface")

    if not pol["enabled"]:
        # 整道门关闭：网格原样交出，不给任何判定结论
        n_cross, _ = count_cross_bay_elems(model, mesh)
        return _result(mesh, None, [], [], pol, criteria, 0, n_cross, 0,
                       enabled=False)

    ext = _extents(model)
    initial = _initial_subdiv(model, mesh)
    current = mesh
    report = evaluate(model, current, pol, surface)

    rounds = 0
    while report["warn_bays"]:
        if not pol["auto_refine"]:
            break
        if rounds >= pol["max_refine"]:
            break
        planned = _plan_refinement(model, report, _initial_subdiv(model, current),
                                   ext)
        rebuilt = meshgen.build_mesh(model, subdiv_per_bay=1,
                                     subdiv_by_bay=planned)
        ok, problems = meshgen.verify_no_straddle(model, rebuilt)
        if not ok:
            raise MeshQualityError(
                "加密后出现跨自然格单元（ADR-0010 被破坏，禁止交付）: %s"
                % problems[:3])
        current = rebuilt
        rounds += 1
        report = evaluate(model, current, pol, surface)

    final_subdiv = _initial_subdiv(model, current)
    refined = _refined_trace(model, initial, final_subdiv, rounds)

    n_cross, problems = count_cross_bay_elems(model, current)
    if n_cross:
        raise MeshQualityError(
            "网格存在 %d 个跨自然格单元（ADR-0010 硬约束不成立，禁止交付）: %s"
            % (n_cross, problems[:3]))

    n_hanging, details = check_conformity(model, current)
    warnings = []
    if report["warn_bays"]:
        warnings.extend(_warning_lines(report["warn_bays"], report, pol, refined))
    if n_hanging:
        msg = ("加密后界面出现 %d 个悬挂节点（相邻自然格边界节点不一致）: %s"
               % (n_hanging, details[:2]))
        if pol["require_conforming"]:
            raise MeshQualityError(msg + "；可设 policy.require_conforming=False 降级为警告")
        warnings.append(msg)

    return _result(current, report, warnings, refined, pol, criteria, rounds,
                   n_cross, n_hanging)


# ---------------------------------------------------------------------------
# 报告落盘（产物留痕）
# ---------------------------------------------------------------------------

def to_report_dict(quality):
    """
    把 `apply_gate` 的返回转成可 JSON 序列化的报告 dict（去掉 Mesh 对象）。

    这样"达上限仍不达标"的结论能**落到文件里**（不只停在内存），
    报告中可据此说明结论的适用边界。
    """
    if not quality:
        return {}
    out = {}
    for key in ("enabled", "auto_refine", "max_refine", "criteria", "bays",
                "warn_bays", "refined_bays", "worst", "warnings", "n_rounds",
                "n_elems", "n_nodes", "n_cross_bay_elems", "n_hanging_nodes"):
        if key in quality:
            out[key] = quality[key]
    out["acceptable"] = is_acceptable(quality)
    out["summary"] = summarize(quality)
    return out


def write_report(quality, path):
    """
    把质量报告写成 JSON 文件（产物留痕）；返回写入路径。
    目录不存在则创建。quality 为空/未启用时也会写出一份"未启用"的记录。
    """
    payload = to_report_dict(quality)
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True, indent=2)
    return path


# ---------------------------------------------------------------------------
# 便捷查询
# ---------------------------------------------------------------------------

def is_acceptable(quality):
    """质量门结论是否可直接交付（无超标格、无跨界单元、无悬挂节点）。"""
    if not quality or not quality.get("enabled"):
        return True
    return (not quality.get("warn_bays")
            and not quality.get("n_cross_bay_elems")
            and not quality.get("n_hanging_nodes"))


def summarize(quality):
    """人类可读一行摘要（日志/报告用）。"""
    if not quality:
        return "网格质量门: 未启用"
    if not quality.get("enabled"):
        return "网格质量门: 已关闭（网格原样交付）"
    worst = quality.get("worst") or {}
    return ("网格质量门: 单元 %d, 超标格 %d, 已加密格 %d, 轮数 %d, "
            "最坏 jacobian=%.3f aspect=%.2f warp=%.3f, 跨界单元 %d"
            % (quality.get("n_elems", 0), len(quality.get("warn_bays") or []),
               len(quality.get("refined_bays") or []), quality.get("n_rounds", 0),
               worst.get("jacobian") if worst.get("jacobian") is not None else 1.0,
               worst.get("aspect_ratio") if worst.get("aspect_ratio") is not None else 1.0,
               worst.get("warp") if worst.get("warp") is not None else 0.0,
               quality.get("n_cross_bay_elems", 0)))
