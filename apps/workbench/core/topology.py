# -*- coding: utf-8 -*-
"""
topology.py -- 结构拓扑模型与自然格生成（票 03）

对应 ADR-0008（自然格一等公民）、ADR-0010（网格与拓扑对齐）。
术语见 GLOSSARY：自然格 / 结构拓扑模型 / 参数契约。

核心做法（来自已验证原型 prototype/natural-bay）：
  在参数空间 (eta=展向弦长分数?, 实际用 y 展向 / xi 弦向 0..1) 建拓扑：
    长桁 = 等 xi 线，可只覆盖部分展长 (runout)
    肋   = 等 y  线，可只覆盖部分弦长 (partial rib)
  候选自然格 = 相邻肋 × 相邻长桁 的张量积单元。
  对每个候选格做连续性校验：四边必须有完整、连续的长桁/肋围合；
  内部有开口(cutout)则判为不连续。

不连续格不构成自然格（ADR-0008）：标记原因，后续强度校核中
排除出自然格级聚合、但单元级照常，并交 L2 人工决策。

纯算法，不依赖 CATIA / Abaqus。兼容 Python 3.8。
"""

import json
import math

# 不连续原因常量
REASON_RUNOUT = "runout"            # 长桁终止，未完整跨越展向区间
REASON_PARTIAL_RIB = "partial rib"  # 肋未完整跨越弦向区间
REASON_CUTOUT = "cutout"            # 格内有开口，传力路径中断

_TOL = 1e-9


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


class TopologyModel(object):
    """结构拓扑模型：长桁/肋/自然格/不连续格。"""

    def __init__(self, template_id, params, stringers, ribs, holes):
        self.template_id = template_id
        self.params = params
        self.stringers = stringers   # [{'xi':, 'y0':, 'y1':}]
        self.ribs = ribs             # [{'y':, 'xi0':, 'xi1':}]
        self.holes = holes           # [{'cy':, 'cxi':}]
        self.bays = []

    @property
    def valid_bays(self):
        return [b for b in self.bays if b.valid]

    @property
    def invalid_bays(self):
        return [b for b in self.bays if not b.valid]

    def to_dict(self):
        return {
            "template_id": self.template_id,
            "params": self.params,
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


# ---------------------------------------------------------------------------
# 由参数集派生结构布置（P1 的机翼盒段规则）
# ---------------------------------------------------------------------------

def layout_from_params(params, holes=None, stringer_runout=None, partial_ribs=None):
    """
    按参数集生成结构布置。

    params: 需含 span / n_ribs / n_stringers（见 wingbox_v1 参数契约）
    holes: 可选，[{'cy': frac_of_span, 'cxi': 0..1}]  —— cy 用**展向分数**更稳定
    stringer_runout: 可选，{stringer_index: y_end} 令某长桁提前终止
    partial_ribs: 可选，{rib_index: (xi0, xi1)} 令某肋只覆盖部分弦长

    返回 (stringers, ribs, holes_abs)
    """
    span = float(params["span"])
    n_ribs = int(params["n_ribs"])
    n_str = int(params["n_stringers"])

    # 肋：n_ribs 根（含两端），展向等分布置
    ribs = []
    for k in range(n_ribs):
        y = span * k / float(n_ribs - 1) if n_ribs > 1 else 0.0
        ribs.append({"y": y, "xi0": 0.0, "xi1": 1.0})

    # 长桁：n_stringers 根（含前后梁），弦向 0..1 等分
    stringers = []
    for k in range(n_str):
        xi = k / float(n_str - 1) if n_str > 1 else 0.0
        stringers.append({"xi": xi, "y0": 0.0, "y1": span})

    # 施加人为不连续（用于回归测试/真实缺陷建模）
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
            holes_abs.append({"cy": float(h["cy"]) * span,
                              "cxi": float(h["cxi"])})

    stringers.sort(key=lambda s: s["xi"])
    ribs.sort(key=lambda r: r["y"])
    return stringers, ribs, holes_abs


def build_topology(template_id, params, holes=None, stringer_runout=None,
                   partial_ribs=None):
    """一站式：参数集 → TopologyModel（含自然格与不连续格）。"""
    stringers, ribs, holes_abs = layout_from_params(
        params, holes=holes, stringer_runout=stringer_runout,
        partial_ribs=partial_ribs)
    t = TopologyModel(template_id, params, stringers, ribs, holes_abs)
    t.bays = build_bays(stringers, ribs, holes_abs)
    return t


def summarize(model):
    """人类可读摘要。"""
    return ("自然格: 候选 %d, 有效 %d, 不连续 %d"
            % (len(model.bays), len(model.valid_bays), len(model.invalid_bays)))
