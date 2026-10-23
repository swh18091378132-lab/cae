# -*- coding: utf-8 -*-
"""
topology.py -- 结构拓扑模型与自然格生成（票 03 / 票 11）

对应 ADR-0008（自然格一等公民）、ADR-0010（网格与拓扑对齐）。
术语见 GLOSSARY：自然格 / 结构拓扑模型 / 参数契约。

## 票 11 起的结构（生成策略化）

拓扑生成不再硬编码"机翼盒段专用"规则，而是**按模板声明的布置规则生成**：

  模板 JSON 增补一节::

      "topology": {
        "mode": "wing_surface",           // 翼面类：长桁(等 xi 线) × 肋(等 y 线)
        "params": {...}                   // 可选，覆盖默认取参方式
      }

      "topology": {
        "mode": "cylinder_section",       // 筒段类：桁条(周向等分) × 框(轴向等分)
        "params": {"axial": "length", "circumferential": "n_frames"}
      }

`build_topology(template_id, params, ...)` 先取模板声明，再分派到对应的
**布置规则函数**（`_layout_wing_surface` / `_layout_cylinder_section`）。
两类布置都产出统一的数据结构：

    纵梁/桁条 (stringers) : [{'xi': 沿"横向"位置, 'y0': 起, 'y1': 止}]
    框/肋 (ribs)          : [{'y': 沿"纵向"位置, 'xi0': 起, 'xi1': 止}]

也就是**参数空间 (y, xi)** 仍是一张二维规则网格 —— 两类部件族只是把物理参数
（展长/弦长 vs 轴向长度/半径）映射到同一个 (y, xi) 空间的不同方式。
因此自然格连续性校验 `build_bays` 只有一份实现：候选自然格的四边必须被
连续的长桁/肋围合（ADR-0008），内部有开口(cutout)则判为不连续。

## 零回归约束（票 11 验收）

机翼盒段（`wingbox_v1`，展向 6 肋 × 弦向 7 长桁）必须逐项与原实现一致：
自然格总数 30、四类场景的失效格 ID 与数量
（runout 4 格 / cutout 1 格 / partial rib 6 格）、自然格 ID 可重复。

## 与后端的解耦

纯算法，不依赖 CATIA / Abaqus。兼容 Python 3.8。
"""

import json
import math

# 不连续原因常量
REASON_RUNOUT = "runout"            # 长桁终止，未完整跨越展向区间
REASON_PARTIAL_RIB = "partial rib"  # 肋未完整跨越弦向区间
REASON_CUTOUT = "cutout"            # 格内有开口，传力路径中断

# 拓扑生成模式（票 11）
MODE_WING_SURFACE = "wing_surface"        # 翼面类：长桁 × 肋
MODE_CYLINDER_SECTION = "cylinder_section"  # 筒段类：桁条 × 框
DEFAULT_MODE = MODE_WING_SURFACE

_TOL = 1e-9


class TopologyError(ValueError):
    """模板声明的拓扑规则不成立（参数缺失/模式未知）。"""


class Bay(object):
    """一个候选自然格（可能因不连续而不成立）。"""

    def __init__(self, bay_id, rib_j, str_i, y0, y1, xi0, xi1):
        self.id = bay_id
        self.rib_j = rib_j        # 下边界肋索引
        self.str_i = str_i        # 左边界长桁索引
        self.y0 = y0
        self.y1 = y1
        self.xi0 = xi0
        self.xi1 = xi1
        self.valid = True
        self.reasons = []

    def to_dict(self):
        return {
            "id": self.id,
            "rib_j": self.rib_j,
            "str_i": self.str_i,
            "y0": self.y0, "y1": self.y1,
            "xi0": self.xi0, "xi1": self.xi1,
            "valid": self.valid,
            "reasons": list(self.reasons),
        }

    # 物理尺寸（报告/网格质量门用；参数空间 → 物理长度由 topo 层提供）
    def size(self):
        return (self.y1 - self.y0, self.xi1 - self.xi0)


class TopologyModel(object):
    """结构拓扑模型：长桁/肋/自然格/不连续格。"""

    def __init__(self, template_id, params, stringers, ribs, holes, mode=None,
                 physical=None):
        self.template_id = template_id
        self.params = params
        self.stringers = stringers   # [{'xi':, 'y0':, 'y1':}]
        self.ribs = ribs             # [{'y':, 'xi0':, 'xi1':}]
        self.holes = holes           # [{'cy':, 'cxi':}]
        self.mode = mode or DEFAULT_MODE
        # 物理尺度：y 轴本身就是物理长度（m）；xi 是无量纲 0..1，
        # 乘 xi_scale 换成物理长度（翼面类=根弦长，筒段类=展开周长）。
        # 供 FE 建模与网格质量门把参数空间量换算成物理尺寸用。
        self.physical = physical or {"xi_scale": 1.0, "y_unit": "m",
                                     "xi_unit": "m"}
        self.bays = []

    @property
    def valid_bays(self):
        return [b for b in self.bays if b.valid]

    @property
    def invalid_bays(self):
        return [b for b in self.bays if not b.valid]

    def bounds(self):
        """参数空间包围盒 (y_min, y_max, xi_min, xi_max)；无格时退回 (0,0,0,1)。"""
        if not self.bays:
            return (0.0, 0.0, 0.0, 1.0)
        y_min = min(b.y0 for b in self.bays)
        y_max = max(b.y1 for b in self.bays)
        xi_min = min(b.xi0 for b in self.bays)
        xi_max = max(b.xi1 for b in self.bays)
        return (y_min, y_max, xi_min, xi_max)

    def extents(self):
        """
        FE 建模需要的物理尺度 dict：

          y_min / y_max / xi_min / xi_max : 参数空间包围盒
          xi_scale                         : xi → 物理长度 的换算系数
          y_span / xi_span                 : 物理长度跨度

        让 FE 建模块不必硬编码"翼面族才有 span/c_root"这类假设（票12/13 前提）。
        """
        y0, y1, xi0, xi1 = self.bounds()
        scale = float(self.physical.get("xi_scale", 1.0))
        return {
            "y_min": y0, "y_max": y1,
            "xi_min": xi0, "xi_max": xi1,
            "xi_scale": scale,
            "y_span": y1 - y0,
            "xi_span": (xi1 - xi0) * scale,
            "y_unit": self.physical.get("y_unit", "m"),
            "xi_unit": self.physical.get("xi_unit", "m"),
        }

    def to_dict(self):
        return {
            "template_id": self.template_id,
            "topology_mode": self.mode,
            "params": self.params,
            "physical": self.physical,
            "extents": self.extents(),
            "n_stringers": len(self.stringers),
            "n_ribs": len(self.ribs),
            "stringers": self.stringers,
            "ribs": self.ribs,
            "holes": self.holes,
            "n_bays_total": len(self.bays),
            "n_bays_valid": len(self.valid_bays),
            "n_bays_invalid": len(self.invalid_bays),
            "bays": [b.to_dict() for b in self.bays],
        }

    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), ensure_ascii=False,
                          sort_keys=True, indent=indent)


def _covers(rng, a, b, tol=_TOL):
    """区间 rng=(r0,r1) 是否完整覆盖 [a,b]。"""
    return rng[0] <= a + tol and rng[1] >= b - tol


def _bay_id(rib_j, str_i):
    """稳定 ID：只依赖排序后的索引，与时间/随机无关。"""
    return "NB-r%d-s%d" % (rib_j, str_i)


def build_bays(stringers, ribs, holes=None):
    """
    由长桁/肋/开口生成候选自然格并做连续性校验。

    与部件族无关——两类拓扑模式都映射到同一 (y, xi) 参数空间，
    因此连续性判据只有这一份实现（ADR-0008）。

    stringers: [{'xi': float, 'y0': float, 'y1': float}]  已按 xi 升序
    ribs     : [{'y': float, 'xi0': float, 'xi1': float}]  已按 y   升序
    holes    : [{'cy': float, 'cxi': float}]

    返回 [Bay]
    """
    holes = holes or []
    bays = []

    for j in range(len(ribs) - 1):
        rj, rj1 = ribs[j]["y"], ribs[j + 1]["y"]
        for i in range(len(stringers) - 1):
            si, si1 = stringers[i]["xi"], stringers[i + 1]["xi"]
            bay = Bay(_bay_id(j, i), j, i, rj, rj1, si, si1)

            # 左右边界：长桁必须完整跨越该展向区间
            left = stringers[i]
            right = stringers[i + 1]
            if not _covers((left["y0"], left["y1"]), rj, rj1):
                bay.reasons.append({
                    "type": REASON_RUNOUT,
                    "detail": "left stringer xi=%.4f spans y[%.3f,%.3f], "
                              "bay needs y[%.3f,%.3f]"
                              % (left["xi"], left["y0"], left["y1"], rj, rj1)})
            if not _covers((right["y0"], right["y1"]), rj, rj1):
                bay.reasons.append({
                    "type": REASON_RUNOUT,
                    "detail": "right stringer xi=%.4f spans y[%.3f,%.3f], "
                              "bay needs y[%.3f,%.3f]"
                              % (right["xi"], right["y0"], right["y1"], rj, rj1)})

            # 上下边界：肋必须完整跨越该弦向区间
            lower = ribs[j]
            upper = ribs[j + 1]
            if not _covers((lower["xi0"], lower["xi1"]), si, si1):
                bay.reasons.append({
                    "type": REASON_PARTIAL_RIB,
                    "detail": "lower rib y=%.3f spans xi[%.4f,%.4f], "
                              "bay needs xi[%.4f,%.4f]"
                              % (lower["y"], lower["xi0"], lower["xi1"], si, si1)})
            if not _covers((upper["xi0"], upper["xi1"]), si, si1):
                bay.reasons.append({
                    "type": REASON_PARTIAL_RIB,
                    "detail": "upper rib y=%.3f spans xi[%.4f,%.4f], "
                              "bay needs xi[%.4f,%.4f]"
                              % (upper["y"], upper["xi0"], upper["xi1"], si, si1)})

            # 内部开口：传力路径被中断
            for h in holes:
                if rj + _TOL < h["cy"] < rj1 - _TOL and si + _TOL < h["cxi"] < si1 - _TOL:
                    bay.reasons.append({
                        "type": REASON_CUTOUT,
                        "detail": "cutout at y=%.3f, xi=%.4f inside bay"
                                  % (h["cy"], h["cxi"])})

            bay.valid = (len(bay.reasons) == 0)
            bays.append(bay)

    return bays


def _resolve_bound(spec, params, default):
    """
    把模板 `ranges` 里的一个端点解析成数值。

    spec 可以是：
      * None            -> 用 default
      * 数字            -> 直接用
      * 参数名字符串    -> 取 params[名字]
      * {"param": "..."} / {"value": 0.3}  -> 显式形式
    """
    if spec is None:
        return float(default)
    if isinstance(spec, bool):
        raise TopologyError("ranges 端点不能是布尔值: %r" % (spec,))
    if isinstance(spec, (int, float)):
        return float(spec)
    if isinstance(spec, str):
        if spec not in params:
            raise TopologyError("ranges 端点引用参数 %r，但参数集里只有 %s"
                                % (spec, ", ".join(sorted(params))))
        return float(params[spec])
    if isinstance(spec, dict):
        if "param" in spec:
            return _resolve_bound(spec["param"], params, default)
        if "value" in spec:
            return float(spec["value"])
    raise TopologyError("无法解析 ranges 端点: %r" % (spec,))


def _resolve_ranges(rule, params, y_default, xi_default):
    """
    解析模板声明的参数空间子区间（票12/13 用）。

    翼面类/筒段类的默认区间都是 y: [0, 全长] 与 xi: [0, 1]；某些部件族只占其中一段
    （例：尾翼盒段只取前后梁之间的弦向段），于是模板声明::

        "topology": {
          "mode": "wing_surface",
          "ranges": {"xi": {"from": "spar_front_frac", "to": "spar_rear_frac"}}
        }

    未声明时两端都用默认值 —— 这正是 P1 机翼盒段的规则，故零回归。
    """
    ranges = (rule or {}).get("ranges") or {}
    if not isinstance(ranges, dict):
        raise TopologyError("模板 topology.ranges 应为对象，实际 %r" % (ranges,))

    y_rng = ranges.get("y") or {}
    xi_rng = ranges.get("xi") or {}
    y0 = _resolve_bound(y_rng.get("from"), params, y_default[0])
    y1 = _resolve_bound(y_rng.get("to"), params, y_default[1])
    xi0 = _resolve_bound(xi_rng.get("from"), params, xi_default[0])
    xi1 = _resolve_bound(xi_rng.get("to"), params, xi_default[1])

    if not y1 > y0:
        raise TopologyError("拓扑 y 区间不成立: [%r, %r]" % (y0, y1))
    if not xi1 > xi0:
        raise TopologyError("拓扑 xi 区间不成立: [%r, %r]" % (xi0, xi1))
    return (y0, y1), (xi0, xi1)


# ---------------------------------------------------------------------------
# 布置规则：翼面类（长桁 × 肋）
# ---------------------------------------------------------------------------

def _layout_wing_surface(params, rule=None, holes=None, stringer_runout=None,
                         partial_ribs=None):
    """
    翼面类布置（P1 机翼盒段规则，**逐项保持不变**）。

    params: 需含 span / n_ribs / n_stringers（见 wingbox_v1 参数契约）
    holes: 可选，[{'cy': frac_of_span, 'cxi': 0..1}]  —— cy 用**展向分数**更稳定
    stringer_runout: 可选，{stringer_index: y_end} 令某长桁提前终止
    partial_ribs: 可选，{rib_index: (xi0, xi1)} 令某肋只覆盖部分弦长

    返回 (stringers, ribs, holes_abs)
    """
    rule = rule or {}
    pp = rule.get("params") or {}
    span_key = pp.get("axial", "span")
    ribs_key = pp.get("n_frames", "n_ribs")
    str_key = pp.get("n_stringers", "n_stringers")

    for k in (span_key, ribs_key, str_key):
        if k not in params:
            raise TopologyError(
                "翼面类拓扑需要参数 %r，但参数集里只有 %s"
                % (k, ", ".join(sorted(params))))

    span = float(params[span_key])
    n_ribs = int(params[ribs_key])
    n_str = int(params[str_key])

    # 参数空间子区间：未声明则为 y[0,全长] × xi[0,1]（= P1 机翼盒段规则）
    (y0, y1), (xi_lo, xi_hi) = _resolve_ranges(
        rule, params, (0.0, span), (0.0, 1.0))

    # 肋：n_ribs 根（含两端），展向等分布置
    ribs = []
    for k in range(n_ribs):
        y = y0 + (y1 - y0) * k / float(n_ribs - 1) if n_ribs > 1 else y0
        ribs.append({"y": y, "xi0": xi_lo, "xi1": xi_hi})

    # 长桁：n_stringers 根（含前后梁），弦向等分
    stringers = []
    for k in range(n_str):
        xi = xi_lo + (xi_hi - xi_lo) * k / float(n_str - 1) if n_str > 1 else xi_lo
        stringers.append({"xi": xi, "y0": y0, "y1": y1})

    # 施加人为不连续（用于回归测试/真实缺陷建模）
    if stringer_runout:
        for idx, y_end in stringer_runout.items():
            stringers[int(idx)]["y1"] = float(y_end)
    if partial_ribs:
        for idx, rng in partial_ribs.items():
            ribs[int(idx)]["xi0"] = float(rng[0])
            ribs[int(idx)]["xi1"] = float(rng[1])

    # 开口位置：cy 是**沿 y 的分数**（0=起点，1=终点），cxi 是沿 xi 的分数
    holes_abs = []
    if holes:
        for h in holes:
            holes_abs.append({"cy": y0 + float(h["cy"]) * (y1 - y0),
                              "cxi": xi_lo + float(h["cxi"]) * (xi_hi - xi_lo)})

    stringers.sort(key=lambda s: s["xi"])
    ribs.sort(key=lambda r: r["y"])
    return stringers, ribs, holes_abs


# ---------------------------------------------------------------------------
# 布置规则：筒段类（桁条 × 框）
# ---------------------------------------------------------------------------

def _layout_cylinder_section(params, rule=None, holes=None, stringer_runout=None,
                             partial_ribs=None):
    """
    筒段类布置（票 11 新增；票 13 机身筒段使用）。

    物理直觉：蒙皮卷成筒，**框(frame)** 沿轴向等分（对应翼面的"肋"），
    **桁条(stringer)** 沿周向等分（对应翼面的"长桁"）。参数空间里依旧是
    (y=轴向, xi=周向 0..1)，于是自然格就是"框 × 桁条"围合的蒙皮块。

    取参口径由模板声明 `topology.params` 指定（默认见下），
    这样新增筒段类模板只需写 JSON，无需改本文件：

        axial         : 轴向长度参数名（默认 "length"）
        n_frames      : 框参数名（默认 "n_frames"）
        n_stringers   : 桁条参数名（默认 "n_stringers"）
        hole_axial    : 开口轴向位置参数名（可选，用默认 0.5）
        hole_circ     : 开口周向位置参数名（可选，用默认 0.5）

    holes 若以**轴向分数 + 周向分数**给出 {'cy': frac, 'cxi': 0..1}，
    与翼面类完全一致——于是开口导致的"不连续格"口径在两类部件族里统一。
    """
    rule = rule or {}
    pp = rule.get("params") or {}
    axial_key = pp.get("axial", "length")
    frames_key = pp.get("n_frames", "n_frames")
    str_key = pp.get("n_stringers", "n_stringers")

    for k in (axial_key, frames_key, str_key):
        if k not in params:
            raise TopologyError(
                "筒段类拓扑需要参数 %r，但参数集里只有 %s"
                % (k, ", ".join(sorted(params))))

    length = float(params[axial_key])
    n_frames = int(params[frames_key])
    n_str = int(params[str_key])

    # 参数空间子区间：未声明则为 y[0,全长] × xi[0,1]（整圈周向）
    (y0, y1), (xi_lo, xi_hi) = _resolve_ranges(
        rule, params, (0.0, length), (0.0, 1.0))

    # 框：n_frames 根（含两端），轴向等分
    ribs = []
    for k in range(n_frames):
        y = y0 + (y1 - y0) * k / float(n_frames - 1) if n_frames > 1 else y0
        ribs.append({"y": y, "xi0": xi_lo, "xi1": xi_hi})

    # 桁条：n_str 根，周向等分；筒段是**闭口**，故首尾同线
    stringers = []
    span_xi = xi_hi - xi_lo
    for k in range(n_str):
        xi = xi_lo + span_xi * k / float(n_str) if n_str > 0 else xi_lo
        stringers.append({"xi": xi, "y0": y0, "y1": y1})
    # 闭口：补上接缝那一根，使周向最后一段也由两根桁条围合
    if n_str > 0:
        stringers.append({"xi": xi_hi, "y0": y0, "y1": y1})

    if stringer_runout:
        for idx, y_end in stringer_runout.items():
            stringers[int(idx)]["y1"] = float(y_end)
    if partial_ribs:
        for idx, rng in partial_ribs.items():
            ribs[int(idx)]["xi0"] = float(rng[0])
            ribs[int(idx)]["xi1"] = float(rng[1])

    holes_abs = []
    if holes:
        for h in holes:
            holes_abs.append({"cy": y0 + float(h["cy"]) * (y1 - y0),
                              "cxi": xi_lo + float(h["cxi"]) * span_xi})

    stringers.sort(key=lambda s: s["xi"])
    ribs.sort(key=lambda r: r["y"])
    return stringers, ribs, holes_abs


# 模式 -> 布置规则函数（新增部件族只需在这里登记一行 + 写模板 JSON）
LAYOUT_MODE_REGISTRY = {
    MODE_WING_SURFACE: _layout_wing_surface,
    MODE_CYLINDER_SECTION: _layout_cylinder_section,
}


def register_layout_mode(mode, fn):
    """登记一个新的拓扑布置模式（供后续部件族扩展，不必改 build_topology）。"""
    if not mode or not callable(fn):
        raise TopologyError("register_layout_mode 需要 (mode:str, fn:callable)")
    LAYOUT_MODE_REGISTRY[str(mode)] = fn


def layout_modes():
    return sorted(LAYOUT_MODE_REGISTRY.keys())


# ---------------------------------------------------------------------------
# 模板声明 -> 布置
# ---------------------------------------------------------------------------

def topology_rule(template=None, template_id=None, templates_dir=None):
    """
    取模板声明的拓扑规则 dict。

    未声明则回退到翼面类默认（这正是 P1 机翼盒段的规则，故零回归）。
    模板可以是 Template 实例，也可只给 template_id（此时按需加载）。
    """
    data = None
    if template is not None:
        data = getattr(template, "data", None)
        if data is None and isinstance(template, dict):
            data = template
    elif template_id is not None:
        try:
            from . import template_registry as _tr
        except ImportError:
            import template_registry as _tr
        data = _tr.load(template_id, templates_dir).data

    rule = (data or {}).get("topology") or {}
    if not isinstance(rule, dict):
        raise TopologyError("模板 topology 声明应为对象，实际 %r" % (rule,))
    out = dict(rule)
    out.setdefault("mode", DEFAULT_MODE)
    if out["mode"] not in LAYOUT_MODE_REGISTRY:
        raise TopologyError("未知拓扑模式 %r（可用: %s）"
                            % (out["mode"], ", ".join(layout_modes())))
    return out


def layout_from_params(params, holes=None, stringer_runout=None,
                       partial_ribs=None, rule=None):
    """
    按**模板声明的布置规则**生成结构布置（票 11）。

    params: 参数集（至少含该模式要求的参数）
    holes: 可选，[{'cy': 轴向/展向分数, 'cxi': 周向/弦向 0..1}]
    stringer_runout: 可选，{stringer_index: y_end}
    partial_ribs: 可选，{rib_index: (xi0, xi1)}
    rule: 模板 topology 声明（None 时用翼面类默认，兼容 P1 调用）

    返回 (stringers, ribs, holes_abs)
    """
    rule = rule or {"mode": DEFAULT_MODE}
    mode = rule.get("mode", DEFAULT_MODE)
    fn = LAYOUT_MODE_REGISTRY.get(mode)
    if fn is None:
        raise TopologyError("未知拓扑模式 %r（可用: %s）"
                            % (mode, ", ".join(layout_modes())))
    return fn(params, rule=rule, holes=holes, stringer_runout=stringer_runout,
              partial_ribs=partial_ribs)


def physical_from_rule(rule, params, mode=None):
    """
    从模板声明推导参数空间的**物理尺度**。

    xi 是无量纲 0..1，换算成物理长度需要一族一个系数：

      * 翼面类（wing_surface）  : xi 沿弦向 → xi_scale = 根弦长 c_root
      * 筒段类（cylinder_section）: xi 沿周向 → xi_scale = 2π×半径（展开周长）

    模板可用 `topology.physical = {"xi_scale": <数字或参数名>}` 显式覆盖；
    未声明时按模式走上面的默认。这样 FE 建模与网格质量门不必区分部件族。
    """
    rule = rule or {}
    mode = mode or rule.get("mode", DEFAULT_MODE)
    pp = rule.get("params") or {}
    phys = rule.get("physical") or {}

    if "xi_scale" in phys:
        scale = _resolve_bound(phys["xi_scale"], params, 1.0)
    elif mode == MODE_CYLINDER_SECTION:
        radius_key = pp.get("radius", "radius")
        if radius_key in params:
            scale = 2.0 * math.pi * float(params[radius_key])
        else:
            scale = 1.0
    else:
        chord_key = pp.get("c_root", "c_root")
        scale = float(params[chord_key]) if chord_key in params else 1.0

    return {"xi_scale": scale,
            "y_unit": phys.get("y_unit", "m"),
            "xi_unit": phys.get("xi_unit", "m")}


def build_topology(template_id, params, holes=None, stringer_runout=None,
                   partial_ribs=None, template=None, templates_dir=None,
                   rule=None):
    """
    一站式：参数集 → TopologyModel（含自然格与不连续格）。

    template : 可选，已加载的 Template 实例（避免服务层重复读盘）
    rule     : 可选，直接给拓扑声明 dict（纯算法单测用，绕开模板文件）
               e.g. {"mode": "cylinder_section"}
    模板未声明 topology 时回退翼面类（= P1 机翼盒段规则，故零回归）。
    """
    if rule is None:
        try:
            rule = topology_rule(template=template, template_id=template_id,
                                 templates_dir=templates_dir)
        except (IOError, OSError):
            # 纯算法单测常直接给 template_id 而不落模板文件；按翼面类默认处理
            rule = {"mode": DEFAULT_MODE}
    stringers, ribs, holes_abs = layout_from_params(
        params, holes=holes, stringer_runout=stringer_runout,
        partial_ribs=partial_ribs, rule=rule)
    mode = rule.get("mode", DEFAULT_MODE)
    t = TopologyModel(template_id, params, stringers, ribs, holes_abs,
                      mode=mode, physical=physical_from_rule(rule, params, mode))
    t.bays = build_bays(stringers, ribs, holes_abs)
    return t


def summarize(model):
    """人类可读摘要。"""
    return ("自然格: 候选 %d, 有效 %d, 不连续 %d"
            % (len(model.bays), len(model.valid_bays), len(model.invalid_bays)))
