# -*- coding: utf-8 -*-
"""
strength_check.py -- 强度校核引擎（票 06）

**确定性 L0 引擎**（ADR-0007）。输入：抽取结果(元素×铺层 的应力) + 声明式规则
+ 自然格拓扑。输出：明细表、单元级判定、自然格级判定、危险部位。

校核层级（依 GLOSSARY / ADR-0008）：
  1. 单元 × 铺层 × 准则 -> FI / MS
  2. 单元级：取该单元**最差铺层**（FI 最大 / MS 最小）
  3. 自然格级：取该格**最差单元**
  4. 不连续格：**不参与自然格级聚合**（但单元级照常），并显式列出

规则（YAML/JSON 均可，本实现用 JSON 以零依赖）示例：
{
  "criteria": ["tsai_wu", "hashin"],
  "allowables": {"Xt": 1500, "Xc": 1200, "Yt": 50, "Yc": 200, "S": 70},
  "safety_factor": 1.5,
  "ms_threshold": 0.0,
  "stress_source": {"field": "S", "components": ["c1", "c2", "c4"],
                    "map": ["s11", "s22", "s12"]}
}

兼容 Python 3.8。
"""

import json
import os

try:
    from . import criteria
except ImportError:
    import criteria


class RuleError(ValueError):
    pass


DEFAULT_RULES = {
    "criteria": ["tsai_wu", "hashin", "max_stress"],
    "allowables": dict(criteria.DEFAULT_ALLOWABLES),
    "safety_factor": 1.0,
    "ms_threshold": 0.0,
    "stress_source": {
        "field": "S",
        "components": ["c1", "c2", "c4"],   # s11, s22, s12（Abaqus 壳 S: S11,S22,S12,...）
        "map": ["s11", "s22", "s12"],
    },
}


def load_rules(path=None, overrides=None):
    """加载并校验规则；未提供则用默认规则。"""
    rules = json.loads(json.dumps(DEFAULT_RULES))  # deep copy
    if path:
        if not os.path.isfile(path):
            raise RuleError("规则文件不存在: %s" % path)
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
        _merge(rules, user)
    if overrides:
        _merge(rules, overrides)
    _validate(rules)
    return rules


def _merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def _validate(rules):
    if not rules.get("criteria"):
        raise RuleError("规则必须至少指定一种失效准则")
    for name in rules["criteria"]:
        criteria.get(name)  # 抛错即未知名
    al = rules.get("allowables") or {}
    for k in ("Xt", "Xc", "Yt", "Yc", "S"):
        if k not in al:
            raise RuleError("许用值缺少 %s" % k)
    src = rules.get("stress_source") or {}
    if not src.get("components") or not src.get("map"):
        raise RuleError("stress_source 必须给 components 与 map")
    if len(src["components"]) != len(src["map"]):
        raise RuleError("stress_source 的 components 与 map 长度必须一致")
    if float(rules.get("safety_factor", 1.0)) <= 0:
        raise RuleError("safety_factor 必须 > 0")


def rules_checksum(rules):
    import hashlib
    return hashlib.sha256(
        json.dumps(rules, sort_keys=True).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 读取抽取结果
# ---------------------------------------------------------------------------

def read_extraction(csv_path, rules=None):
    """
    读取抽取 CSV，返回 stress[(elem, sp)][comp] = value。
    CSV 列：field,elem,node,ip,sp,component,value
    """
    import csv as _csv
    rules = rules or DEFAULT_RULES
    field = (rules.get("stress_source") or {}).get("field", "S")

    stress = {}
    with open(csv_path, "r", encoding="utf-8") as fh:
        rd = _csv.DictReader(fh)
        for row in rd:
            if row.get("field") != field:
                continue
            comp = row.get("component", "")
            try:
                elem = int(row["elem"])
            except (ValueError, TypeError):
                continue
            try:
                sp = int(row["sp"]) if row.get("sp") not in ("", None) else 0
            except (ValueError, TypeError):
                sp = 0
            val = row.get("value", "")
            if val in ("", None):
                continue
            try:
                fval = float(val)
            except (ValueError, TypeError):
                continue
            stress.setdefault((elem, sp), {})[comp] = fval
    return stress


# ---------------------------------------------------------------------------
# 校核
# ---------------------------------------------------------------------------

def _to_stress_tuple(comp_dict, src):
    """按 stress_source 映射成 s11/s22/s12。缺项视为 0。"""
    out = {}
    for comp_name, target in zip(src["components"], src["map"]):
        out[target] = float(comp_dict.get(comp_name, 0.0))
    for k in ("s11", "s22", "s12"):
        out.setdefault(k, 0.0)
    return out


def check(stress_map, rules, model, bay_of_elem=None):
    """
    执行强度校核。

    stress_map    : {(elem, sp): {comp: value}}
    rules         : load_rules() 的结果
    model         : topology.TopologyModel（用于自然格与不连续格）
    bay_of_elem   : {elem_id: bay_id}；None 时从 model 无法推得，则只做单元级

    返回 dict：
      detail_rows   : [ {elem, sp, criterion, FI, MS, valid} ... ]
      elem_best     : {elem: {fi, ms, sp, criterion}}
      bay_best      : {bay_id: {fi, ms, elem, sp, criterion}}  (仅有效格)
      hotspots      : [ {elem, sp, criterion, FI, MS} ... ] (FI 最大 / MS 最小)
      invalid_bays  : [bay_id...]  被排除出自然格级的格
      summary       : {...}
    """
    src = rules["stress_source"]
    allow = rules["allowables"]
    sf = float(rules.get("safety_factor", 1.0))
    ms_thr = float(rules.get("ms_threshold", 0.0))
    crit_names = list(rules["criteria"])
    crit_fns = [criteria.get(n) for n in crit_names]

    detail_rows = []
    elem_best = {}

    for (elem, sp), comps in sorted(stress_map.items()):
        s = _to_stress_tuple(comps, src)
        # 安全系数：把施加应力放大 sf 后再算 FI（工程上等价于许用值折减）
        s_sf = {"s11": s["s11"] * sf, "s22": s["s22"] * sf, "s12": s["s12"] * sf}

        worst_fi = -1.0
        worst_crit = None
        for name, fn in zip(crit_names, crit_fns):
            try:
                fi = fn(s_sf, allow)
            except Exception:
                continue
            ms = (1.0 / fi - 1.0) if fi > 1e-12 else 1e9
            detail_rows.append({
                "elem": elem, "sp": sp, "criterion": name,
                "FI": fi, "MS": ms,
                "pass": (fi < 1.0) and (ms >= ms_thr),
            })
            if fi > worst_fi:
                worst_fi = fi
                worst_crit = name

        if worst_crit is not None:
            elem_best[elem] = {
                "fi": worst_fi,
                "ms": (1.0 / worst_fi - 1.0) if worst_fi > 1e-12 else 1e9,
                "sp": sp,
                "criterion": worst_crit,
            }

    # ---- 自然格聚合（仅有效格） ----
    invalid_bays = []
    if model is not None:
        invalid_bays = [b.id for b in model.invalid_bays]

    bay_best = {}
    if bay_of_elem:
        for elem, best in elem_best.items():
            bay_id = bay_of_elem.get(elem)
            if bay_id is None:
                continue
            if bay_id in invalid_bays:
                continue          # 不连续格：排除自然格级（ADR-0008）
            cur = bay_best.get(bay_id)
            if cur is None or best["fi"] > cur["fi"]:
                bay_best[bay_id] = {
                    "fi": best["fi"], "ms": best["ms"],
                    "elem": elem, "sp": best["sp"],
                    "criterion": best["criterion"],
                }

    # ---- 危险部位 ----
    hotspots = []
    if elem_best:
        ranked = sorted(elem_best.items(), key=lambda kv: -kv[1]["fi"])
        for elem, b in ranked[:10]:
            hotspots.append({
                "elem": elem, "sp": b["sp"], "criterion": b["criterion"],
                "FI": b["fi"], "MS": b["ms"],
            })

    n_fail = sum(1 for r in detail_rows if not r["pass"])
    summary = {
        "n_detail_rows": len(detail_rows),
        "n_elems": len(elem_best),
        "n_bays_checked": len(bay_best),
        "n_bays_excluded": len(invalid_bays),
        "n_fail_rows": n_fail,
        "max_fi": max([r["FI"] for r in detail_rows]) if detail_rows else 0.0,
        "min_ms": min([r["MS"] for r in detail_rows]) if detail_rows else None,
        "all_pass": n_fail == 0,
        "criteria": crit_names,
        "safety_factor": sf,
        "ms_threshold": ms_thr,
        "allowables": allow,
        "rules_checksum": rules_checksum(rules),
    }

    return {
        "detail_rows": detail_rows,
        "elem_best": elem_best,
        "bay_best": bay_best,
        "hotspots": hotspots,
        "invalid_bays": invalid_bays,
        "summary": summary,
    }


def write_outputs(result, out_dir):
    """把校核结果写成明细 CSV + 汇总 JSON，返回 (csv_path, json_path)。"""
    import csv as _csv
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    csv_path = os.path.join(out_dir, "check_detail.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["elem", "sp", "criterion", "FI", "MS", "pass"])
        for r in result["detail_rows"]:
            w.writerow([r["elem"], r["sp"], r["criterion"],
                        "%.9g" % r["FI"], "%.9g" % r["MS"],
                        "PASS" if r["pass"] else "FAIL"])

    json_path = os.path.join(out_dir, "check_summary.json")
    payload = {
        "summary": result["summary"],
        "hotspots": result["hotspots"],
        "invalid_bays": result["invalid_bays"],
        "bay_best": result["bay_best"],
    }
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)

    return csv_path, json_path
