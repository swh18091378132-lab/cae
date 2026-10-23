# -*- coding: utf-8 -*-
"""
model_config.py -- 型号配置：全机层级与参数分解（票16）

问题（对应 issue #16 / P2 spec 缺口 2）：
    P1 的 Study 是**扁平**的，装不下「全机 → 部件」的层级。真实型号的输入是
    **全机总体参数**（翼展、机身长、尾容量……），工程师不该手工逐个部件填参；
    而各部件 Study 之间也需要一个共同的父节点，血缘才能跨部件追溯。

本模块提供（纯 Python，不依赖 UI / CATIA / Abaqus，可直接单测）：

    create_model_config(name, overall_params, rule=None, ...) -> model_id
        记录全机总体参数 + 把**分解规则快照**落 rule_json（可追溯、可重放），
        并把分解出的各部件参数集落到 model_config_parts。

    decompose(overall_params, rule=None) -> {part_key: 参数集}
    decompose_parts(overall_params, rule=None) -> [ {part_key, template_id, ...} ]
        按规则把总体参数**自动分解**成机翼 / 尾翼 / 机身各自的参数集，
        且每个参数集都用 `template_registry.load(template_id).validate(params)`
        过一次参数契约 + 参数化包络；不通过就报错，并说清是**哪个部件、哪个参数**。

    attach_study(conn, model_id, part_key, study_id)
        把某部件的 Study 关联到型号配置（写 model_config_parts.study_id
        + studies.model_id / studies.part_key）。

    get_model_tree(conn, model_id)
        返回「全机 → 部件 → Study」层级结构（含各部件当前参数集与关联 Study，
        以及 parent_part 表达的部件内嵌层级）。

    model_lineage(conn, model_id)
        按型号配置汇总各部件 Study 及其产物数（供血缘跨部件追溯使用）。

数据库沿用 db.py 已建好的 model_configs / model_config_parts 两张表与
studies.model_id / studies.part_key 两列（P1 老库由 db._migrate 幂等补齐）。

兼容 Python 3.8（禁用 3.9+ 语法），仅标准库。
"""

import ast
import json
import math
import uuid
import datetime

try:
    from . import template_registry
    from .db import connect
except ImportError:  # 允许作为脚本/在 core 目录内直接运行
    import template_registry
    from db import connect


class ModelConfigError(ValueError):
    """
    型号配置错误：分解规则不合法、总体参数缺失/非数值、
    或分解结果不满足某部件的参数契约（消息里必须指出部件与参数）。
    """

    pass


# ---------------------------------------------------------------------------
# 分解规则（**可替换的纯数据 dict**）
# ---------------------------------------------------------------------------
#
# 规则结构（JSON 可序列化，因此可原样落 rule_json 做快照）::
#
#     {
#       "version": 1,
#       "name": "...", "note": "...",
#       "vars":  {"<中间量名>": <公式字符串或数字>, ...},
#       "parts": {
#           "<部件键>": {
#               "template_id": "<模板 id>",
#               "parent_part": <上级部件键，可选；缺省 = 直挂全机>,
#               "params": {"<模板参数名>": <公式字符串或数字>, ...},
#               "note": "..."
#           }, ...
#       }
#     }
#
# 求值口径：
#   * 环境 = 总体参数 + 前面已算出的 vars（vars 可引用总体参数与更早的 vars；
#     vars 同名时覆盖总体参数）；
#   * 公式是**受控表达式**（白名单函数，无内建），可用：
#     max / min / abs / round / int / float / sqrt / pi / sin / cos / ceil / floor；
#   * 整型参数（如 n_ribs / n_stringers / n_frames）请在公式里用 int(...) 收口
#     —— 模板契约要求 int，给 float 会被参数契约拒绝（这是刻意的：不猜意图）。
#
# 默认规则表达的是「总体参数 → 部件参数」的工程推导链：
#   机翼：半展长 = 翼展/2；展弦比 8 定平均弦长；锥度 0.5 定根/尖弦长；
#         肋距 0.8m、长桁距 0.45m 定肋数与长桁数。
#   尾翼：尾容量 tail_volume = S_h·l_t /(S_w·c_w)，尾臂 l_t = 0.55×机身长；
#         解出平尾面积 → 展弦比 4 定全展长 → 半展长即尾翼盒段 span；
#         前后梁取 15%/85% 弦向位置；操纵面切口刻意落在肋距/桁距的**中点**
#         （0.5 + 0.25/(n-1)，保证永不落在结构线上——模板包络的硬要求）。
#   机身：取前机身筒段，长度 = 机身长/2；半径 = 当量直径/2；
#         框距 1.5m、桁条周向间距 1.0m 定框数与桁条数；开口取在格中间。
#
# 适用范围（超出即由参数契约拒绝，不是静默给坏值）：本规则覆盖**常规运输机量级**，
# 实测在「翼展约 14~39 m、机身长约 0.9×翼展、尾容量 0.85~0.9」的范围内三个部件的
# 参数集都落在模板包络内；更小（轻型机）/更大（大型机）的型号应**换一份规则**
# （rule 参数），而不是改模板上下限——模板上下限代表参数化包络，不能为单个型号放宽。
DEFAULT_RULE = {
    "version": 1,
    "name": "标准全机分解规则（翼展主导 + 尾容量定尾翼）",
    "note": ("总体参数 -> 各部件参数集的工程推导。改这里即改分解口径；"
             "规则快照会原样落 model_configs.rule_json，保证历史型号可重放。"),
    "vars": {
        # --- 机翼 ---
        "wing_semispan": "span / 2.0",
        "wing_aspect_ratio": 8.0,
        "wing_mean_chord": "span / wing_aspect_ratio",
        "wing_taper": 0.5,
        "wing_area": "span * wing_mean_chord",
        "wing_c_root": "2.0 * wing_mean_chord / (1.0 + wing_taper)",
        "wing_c_tip": "wing_taper * wing_c_root",
        "wing_rib_pitch": 0.8,
        "wing_n_ribs": "int(round(wing_semispan / wing_rib_pitch)) + 1",
        "wing_stringer_pitch": 0.45,
        "wing_n_stringers": "int(max(4.0, round(wing_mean_chord / wing_stringer_pitch)))",
        # --- 尾翼（由尾容量反推面积，再由展弦比定半展长）---
        "tail_sweep_deg": "min(sweep_deg + 8.0, 45.0)",
        "tail_arm": "0.55 * fuselage_length",
        "tail_area": "tail_volume * wing_area * wing_mean_chord / tail_arm",
        "tail_aspect_ratio": 4.0,
        "tail_taper": 0.45,
        "tail_span_full": "sqrt(tail_aspect_ratio * tail_area)",
        "tail_semispan": "tail_span_full / 2.0",
        "tail_c_root": "2.0 * tail_area / (tail_span_full * (1.0 + tail_taper))",
        "tail_c_tip": "tail_taper * tail_c_root",
        "tail_rib_pitch": 1.2,
        "tail_n_ribs": "int(round(tail_semispan / tail_rib_pitch)) + 1",
        "tail_stringer_pitch": 0.35,
        "tail_n_stringers": "int(max(4.0, round(tail_c_root / tail_stringer_pitch)))",
        # 切口落在肋距 / 桁距中点：× (n-1) 后距最近整数恒为 0.25，不落在结构线上
        "tail_cutout_span_frac": "0.5 + 0.25 / max(tail_n_ribs - 1.0, 1.0)",
        "tail_cutout_chord_frac": "0.5 + 0.25 / max(tail_n_stringers - 1.0, 1.0)",
        # --- 机身（前机身筒段）---
        "fuselage_half_length": "fuselage_length / 2.0",
        "fuselage_radius": "fuselage_diameter / 2.0",
        "fuselage_frame_pitch": 1.5,
        "fuselage_n_frames": "int(round(fuselage_half_length / fuselage_frame_pitch)) + 1",
        "fuselage_stringer_pitch": 1.0,
        "fuselage_n_stringers": (
            "int(max(4.0, round(2.0 * pi * fuselage_radius / fuselage_stringer_pitch)))"),
        "fuselage_hole_axial_frac": "0.5 + 0.25 / max(fuselage_n_frames - 1.0, 1.0)",
        "fuselage_hole_circ_frac": "0.5 + 0.25 / max(fuselage_n_stringers * 1.0, 1.0)",
    },
    "parts": {
        "wing": {
            "template_id": "wingbox_v1",
            "note": "机翼盒段：翼展取半展长口径（翼面族 span 是单侧盒段展长）",
            "params": {
                "span": "wing_semispan",
                "sweep_deg": "sweep_deg",
                "c_root": "wing_c_root",
                "c_tip": "wing_c_tip",
                "n_ribs": "wing_n_ribs",
                "n_stringers": "wing_n_stringers",
            },
        },
        "tail": {
            "template_id": "tailbox_v1",
            "note": "尾翼盒段：由尾容量定面积，再定半展长与弦长",
            "params": {
                "span": "tail_semispan",
                "sweep_deg": "tail_sweep_deg",
                "c_root": "tail_c_root",
                "c_tip": "tail_c_tip",
                "n_ribs": "tail_n_ribs",
                "n_stringers": "tail_n_stringers",
                "spar_front_frac": 0.15,
                "spar_rear_frac": 0.85,
                "cutout_span_frac": "tail_cutout_span_frac",
                "cutout_chord_frac": "tail_cutout_chord_frac",
            },
        },
        "fuselage": {
            "template_id": "fuselage_section_v1",
            "note": "机身筒段：取前机身筒段（长度 = 机身长/2）",
            "params": {
                "length": "fuselage_half_length",
                "radius": "fuselage_radius",
                "n_frames": "fuselage_n_frames",
                "n_stringers": "fuselage_n_stringers",
                "hole_axial_frac": "fuselage_hole_axial_frac",
                "hole_circ_frac": "fuselage_hole_circ_frac",
                "hole_enabled": 1,
            },
        },
    },
}

# 公式白名单：受控求值，无 __builtins__
_SAFE_GLOBALS = {
    "__builtins__": {},
    "max": max, "min": min, "abs": abs, "round": round,
    "int": int, "float": float,
    "sqrt": math.sqrt, "pi": math.pi,
    "sin": math.sin, "cos": math.cos, "ceil": math.ceil, "floor": math.floor,
}


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def _json_dumps(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _json_loads(text, default=None):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def _deep_copy(obj):
    """规则 dict 的深拷贝（纯 JSON 结构，用 json 往返最稳，也顺带保证可序列化）。"""
    return json.loads(json.dumps(obj, ensure_ascii=False))


def default_rule():
    """返回默认分解规则的**副本**（调用方可随意改，不影响模块内常量）。"""
    return _deep_copy(DEFAULT_RULE)


def _normalize_overall(overall_params):
    """校验并规范化总体参数：必须是"具名数值"的 dict。"""
    if not isinstance(overall_params, dict):
        raise ModelConfigError(
            "总体参数应为 dict（{参数名: 数值}），实际是 %s"
            % type(overall_params).__name__)
    if not overall_params:
        raise ModelConfigError(
            "总体参数不能为空：至少需要翼展 span、机身长 fuselage_length、"
            "尾容量 tail_volume、机身直径 fuselage_diameter、后掠角 sweep_deg")
    out = {}
    for key, val in overall_params.items():
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ModelConfigError(
                "总体参数 %r 必须是数值，实际 %r（%s）"
                % (key, val, type(val).__name__))
        if isinstance(val, float) and (val != val or val in (float("inf"), float("-inf"))):
            raise ModelConfigError("总体参数 %r 不是有限数值：%r" % (key, val))
        out[str(key)] = val
    return out


def _check_rule(rule):
    """校验规则结构（只查形状，不查数值——数值由参数契约兜）。"""
    if not isinstance(rule, dict):
        raise ModelConfigError(
            "分解规则应为 dict，实际是 %s" % type(rule).__name__)
    parts = rule.get("parts")
    if not isinstance(parts, dict) or not parts:
        raise ModelConfigError(
            "分解规则缺少 parts：至少要声明一个部件（如 wing / tail / fuselage）")
    for part_key, spec in parts.items():
        if not isinstance(spec, dict):
            raise ModelConfigError("分解规则里的部件 %r 应为 dict" % (part_key,))
        tid = spec.get("template_id")
        if not isinstance(tid, str) or not tid:
            raise ModelConfigError(
                "分解规则里的部件 %r 缺少 template_id（该部件用哪个模板族）"
                % (part_key,))
        params = spec.get("params")
        if params is not None and not isinstance(params, dict):
            raise ModelConfigError(
                "分解规则里的部件 %r 的 params 应为 dict（参数名 -> 公式/数值）"
                % (part_key,))
    return rule


def _resolve_value(expr, env, where, provided=None):
    """
    把一条公式（或数字常量）在给定环境里求值。公式错误 -> 可读的 ModelConfigError。

    provided : 报错时列出"本该可用"的名字（默认取整个环境；过长时截断）
    """
    if isinstance(expr, bool):
        raise ModelConfigError("%s 不能是布尔值（%r）" % (where, expr))
    if isinstance(expr, (int, float)):
        return expr
    if not isinstance(expr, str):
        raise ModelConfigError(
            "%s 应为数字或公式字符串，实际 %r" % (where, expr))
    try:
        return eval(expr, dict(_SAFE_GLOBALS), dict(env))  # 受控表达式，白名单已收紧
    except NameError as exc:
        missing = str(exc).split("'")[1] if "'" in str(exc) else str(exc)
        names = sorted(provided if provided is not None else env.keys())
        shown = ", ".join(names[:12]) + ("…（共 %d 个）" % len(names) if len(names) > 12 else "")
        raise ModelConfigError(
            "%s 的公式 %r 引用了未提供的名字 %r（可用名字：%s）"
            % (where, expr, missing, shown or "(空)"))
    except ZeroDivisionError:
        raise ModelConfigError(
            "%s 的公式 %r 出现除零（检查总体参数是否为 0）" % (where, expr))
    except Exception as exc:
        raise ModelConfigError(
            "%s 的公式 %r 无法求值：%s" % (where, expr, exc))


def _free_names(expr):
    """从一条公式里取出它引用的名字（含函数名），用于"总体参数缺失"的预检。"""
    if not isinstance(expr, str):
        return set()
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return set()
    return set(n.id for n in ast.walk(tree) if isinstance(n, ast.Name))


def _check_overall_provided(overall, rule):
    """
    预检：规则里的公式引用了哪些**必须由总体参数提供**的名字。

    好处是「总体参数缺了哪几个」一次全说清，而不是等 eval 到第一条公式才报一个。
    vars 允许引用更早的 vars，所以按声明顺序逐个推进可用名字集合。
    """
    available = set(overall.keys())
    missing = []
    for name, expr in (rule.get("vars") or {}).items():
        for ref in _free_names(expr):
            if ref in _SAFE_GLOBALS and ref != "__builtins__":
                continue
            if ref not in available and ref not in missing:
                missing.append(ref)
        available.add(name)
    for part_key, spec in rule["parts"].items():
        for pname, expr in (spec.get("params") or {}).items():
            for ref in _free_names(expr):
                if ref in _SAFE_GLOBALS and ref != "__builtins__":
                    continue
                if ref not in available and ref not in missing:
                    missing.append(ref)
    prov = sorted(overall.keys())
    absent = [m for m in missing if m not in overall]
    if absent:
        raise ModelConfigError(
            "分解规则需要总体参数 %s，但总体参数里没有提供（现有：%s）"
            % (", ".join(absent), ", ".join(prov) or "(空)"))
    return absent


def _eval_rule(overall, rule):
    """按顺序求 vars 与各部件参数；返回 (参数环境, vars 求值结果, 部件明细列表)。"""
    _check_overall_provided(overall, rule)
    env = {}
    for key, val in overall.items():
        env[key] = val
    vars_out = {}
    for name, expr in (rule.get("vars") or {}).items():
        val = _resolve_value(expr, env, "vars.%s" % name, provided=overall.keys())
        env[name] = val
        vars_out[name] = val

    parts = []
    for part_key, spec in rule["parts"].items():
        params = {}
        for pname, expr in (spec.get("params") or {}).items():
            params[str(pname)] = _resolve_value(
                expr, env, "%s.%s" % (part_key, pname))
        parent = spec.get("parent_part")
        if parent is not None and not isinstance(parent, str):
            raise ModelConfigError(
                "部件 %r 的 parent_part 应为字符串（上级部件键）" % (part_key,))
        parts.append({
            "part_key": part_key,
            "template_id": spec["template_id"],
            "parent_part": parent,
            "params": params,
            "note": spec.get("note"),
        })
    return env, vars_out, parts


def _validate_parts(parts, templates_dir=None):
    """
    用模板参数契约 + 参数化包络逐个校验各部件参数集（票16 硬验收项）。

    通过时用 validate() 的**规范化结果**回填参数集（补齐 default、float 化），
    于是落库的参数集本身就是"满足契约"的那一份。
    不通过则抛 ModelConfigError，消息里点明**哪个部件 / 哪个模板 / 哪个参数**。
    """
    for part in parts:
        part_key = part["part_key"]
        tid = part["template_id"]
        try:
            tpl = template_registry.load(tid, templates_dir)
        except (IOError, OSError) as exc:
            raise ModelConfigError(
                "部件 %r 引用了无法加载的模板 %r：%s" % (part_key, tid, exc))
        try:
            part["params"] = tpl.validate(part["params"])
        except template_registry.ParamError as exc:
            raise ModelConfigError(
                "部件 %r（模板 %s）的参数分解结果不满足参数契约：%s\n"
                "  规则算出的参数集：%s"
                % (part_key, tid, exc, _json_dumps(part["params"])))
        except Exception as exc:  # 模板自身损坏等
            raise ModelConfigError(
                "部件 %r（模板 %s）参数校验异常：%s" % (part_key, tid, exc))
    return parts


# ---------------------------------------------------------------------------
# 分解（纯算法，可脱离数据库单测）
# ---------------------------------------------------------------------------

def decompose(overall_params, rule=None, templates_dir=None, validate=True):
    """
    全机总体参数 -> 各部件参数集。

    返回 {part_key: 参数集}（按规则声明顺序）。

    rule     : 分解规则 dict；None = 默认规则（DEFAULT_RULE 的副本）
    validate : True（默认）用模板参数契约校验每个部件参数集；不通过抛
               ModelConfigError 并指出部件与参数
    """
    parts = decompose_parts(overall_params, rule=rule,
                            templates_dir=templates_dir, validate=validate)
    out = {}
    for p in parts:
        out[p["part_key"]] = dict(p["params"])
    return out


def decompose_parts(overall_params, rule=None, templates_dir=None, validate=True):
    """
    同 decompose，但返回带元信息的部件列表（模板、上级部件、备注）。

    每个元素::

        {"part_key": "wing", "template_id": "wingbox_v1",
         "parent_part": None, "params": {...}, "note": "..."}
    """
    overall = _normalize_overall(overall_params)
    rule = _check_rule(_deep_copy(DEFAULT_RULE) if rule is None else _deep_copy(rule))
    _env, _vars, parts = _eval_rule(overall, rule)
    if validate:
        _validate_parts(parts, templates_dir)
    return parts


def explain(overall_params, rule=None, templates_dir=None, validate=True):
    """
    分解过程的可读追踪（UI/调试/审计用）：总体参数、各中间量、各部件参数集。

    返回 {"overall": {...}, "rule_name": ..., "rule_version": ...,
          "vars": {...}, "parts": [...]}
    """
    overall = _normalize_overall(overall_params)
    rule = _check_rule(_deep_copy(DEFAULT_RULE) if rule is None else _deep_copy(rule))
    _env, vars_out, parts = _eval_rule(overall, rule)
    if validate:
        _validate_parts(parts, templates_dir)
    return {
        "overall": overall,
        "rule_name": rule.get("name"),
        "rule_version": rule.get("version"),
        "vars": vars_out,
        "parts": parts,
    }


# ---------------------------------------------------------------------------
# 型号配置：落库与查询
# ---------------------------------------------------------------------------

def _new_model_id(conn):
    base = "MODEL-%s" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    mid = base
    n = 1
    while conn.execute("SELECT 1 FROM model_configs WHERE id=?", (mid,)).fetchone():
        n += 1
        mid = "%s-%d" % (base, n)
    return mid


def _require_model(conn, model_id):
    row = conn.execute("SELECT * FROM model_configs WHERE id=?",
                       (model_id,)).fetchone()
    if row is None:
        raise ModelConfigError("型号配置不存在：%r" % (model_id,))
    return row


def create_model_config(name, overall_params, rule=None, note=None,
                        model_id=None, conn=None, templates_dir=None):
    """
    创建型号配置：记录全机总体参数，快照分解规则，落各部件参数集。

    name          : 型号配置名（如 "某型运输机-方案A"）
    overall_params: 全机总体参数 dict（数值），如
                    {"span": 34.0, "fuselage_length": 30.0, "tail_volume": 0.9,
                     "fuselage_diameter": 3.2, "sweep_deg": 25.0}
    rule          : 分解规则 dict；None = 默认规则
    note          : 备注
    model_id      : 指定 id（默认 MODEL-YYYYmmdd-HHMMSS，重复时加序号）
    conn          : 数据库连接（None 时用默认库，用完即关）

    返回 model_id。

    分解不通过（规则引用了不存在的总体参数 / 某部件参数不满足模板契约）时
    抛 ModelConfigError 且**不落任何行**（先算后写，天然无半成品）。
    """
    own = conn is None
    conn = conn or connect()
    try:
        rule_copy = _check_rule(
            _deep_copy(DEFAULT_RULE) if rule is None else _deep_copy(rule))
        overall = _normalize_overall(overall_params)
        # 先完整分解 + 契约校验，全部通过才写库
        parts = decompose_parts(overall, rule=rule_copy,
                                templates_dir=templates_dir, validate=True)
        mid = model_id or _new_model_id(conn)
        if conn.execute("SELECT 1 FROM model_configs WHERE id=?",
                        (mid,)).fetchone():
            raise ModelConfigError("型号配置 id 已存在：%r" % (mid,))

        created = _now()
        conn.execute(
            "INSERT INTO model_configs(id, name, overall_json, rule_json, note, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (mid, name, _json_dumps(overall), _json_dumps(rule_copy), note, created))
        for part in parts:
            conn.execute(
                "INSERT INTO model_config_parts(model_id, part_key, template_id, "
                "params_json, study_id, parent_part, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (mid, part["part_key"], part["template_id"],
                 _json_dumps(part["params"]), None, part["parent_part"], created))
        conn.commit()
        return mid
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if own:
            conn.close()


def get_model_config(conn, model_id):
    """读一个型号配置（含总体参数与规则快照）。不存在抛 ModelConfigError。"""
    row = _require_model(conn, model_id)
    return {
        "model_id": row["id"],
        "id": row["id"],
        "name": row["name"],
        "overall": _json_loads(row["overall_json"], {}) or {},
        "rule": _json_loads(row["rule_json"], None),
        "note": row["note"],
        "created_at": row["created_at"],
    }


def list_model_configs(conn, limit=None):
    """列型号配置（新->旧）。"""
    sql = ("SELECT c.*, "
           "(SELECT COUNT(*) FROM model_config_parts p WHERE p.model_id = c.id) "
           "AS n_parts FROM model_configs c ORDER BY c.created_at DESC, c.id DESC")
    if limit:
        sql += " LIMIT %d" % int(limit)
    out = []
    for row in conn.execute(sql).fetchall():
        out.append({
            "model_id": row["id"],
            "id": row["id"],
            "name": row["name"],
            "overall": _json_loads(row["overall_json"], {}) or {},
            "note": row["note"],
            "created_at": row["created_at"],
            "n_parts": int(row["n_parts"]),
        })
    return out


def _part_dict(row):
    return {
        "model_id": row["model_id"],
        "part_key": row["part_key"],
        "template_id": row["template_id"],
        "params": _json_loads(row["params_json"], {}) or {},
        "study_id": row["study_id"],
        "parent_part": row["parent_part"],
        "created_at": row["created_at"],
    }


def list_parts(conn, model_id):
    """列某型号配置的部件（按规则声明顺序 = 插入顺序）。"""
    _require_model(conn, model_id)
    rows = conn.execute(
        "SELECT * FROM model_config_parts WHERE model_id=? ORDER BY id",
        (model_id,)).fetchall()
    return [_part_dict(r) for r in rows]


def get_part(conn, model_id, part_key):
    """取某个部件（不存在返回 None）。"""
    row = conn.execute(
        "SELECT * FROM model_config_parts WHERE model_id=? AND part_key=?",
        (model_id, part_key)).fetchone()
    return _part_dict(row) if row is not None else None


def _require_part(conn, model_id, part_key):
    part = get_part(conn, model_id, part_key)
    if part is None:
        parts = [p["part_key"] for p in list_parts(conn, model_id)]
        raise ModelConfigError(
            "型号配置 %r 下没有部件 %r（现有部件：%s）"
            % (model_id, part_key, ", ".join(parts) or "(无)"))
    return part


def _study_info(conn, study_id):
    """Study 的基本信息 + 产物数（层级树的叶子节点）。"""
    if not study_id:
        return None
    row = conn.execute("SELECT * FROM studies WHERE id=?", (study_id,)).fetchone()
    if row is None:
        return None
    n = conn.execute("SELECT COUNT(*) c FROM artifacts WHERE study_id=?",
                     (study_id,)).fetchone()["c"]
    return {
        "study_id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "note": row["note"],
        "n_artifacts": int(n),
    }


def _require_study(conn, study_id):
    row = conn.execute("SELECT * FROM studies WHERE id=?", (study_id,)).fetchone()
    if row is None:
        raise ModelConfigError(
            "Study 不存在：%r（请先创建 Study 再关联到型号配置）" % (study_id,))
    return row


def attach_study(conn, model_id, part_key, study_id, allow_replace=True):
    """
    把某部件的 Study 关联到型号配置。

    写两处事实：
      * model_config_parts.study_id  —— 该部件**当前**关联哪个 Study
      * studies.model_id / part_key  —— 该 Study 属于哪个型号配置的哪个部件

    allow_replace=True 时允许部件改挂新 Study（改参重跑后重挂；旧 Study 的
    model_id 保留，于是型号下仍能查到该部件的历史 Study，血缘不断）。

    返回更新后的部件 dict。跨型号抢占 Study 会被拒绝（同一 Study 不能同属两个型号）。
    """
    _require_model(conn, model_id)
    part = _require_part(conn, model_id, part_key)
    study_row = _require_study(conn, study_id)

    owner = study_row["model_id"] if "model_id" in study_row.keys() else None
    owner_part = study_row["part_key"] if "part_key" in study_row.keys() else None
    if owner and owner != model_id:
        raise ModelConfigError(
            "Study %r 已属于型号配置 %r 的部件 %r，不能改挂到 %r"
            % (study_id, owner, owner_part, model_id))
    if part["study_id"] and part["study_id"] != study_id and not allow_replace:
        raise ModelConfigError(
            "部件 %r 已关联 Study %r；如需改挂请 allow_replace=True"
            % (part_key, part["study_id"]))

    conn.execute(
        "UPDATE model_config_parts SET study_id=? WHERE model_id=? AND part_key=?",
        (study_id, model_id, part_key))
    conn.execute(
        "UPDATE studies SET model_id=?, part_key=? WHERE id=?",
        (model_id, part_key, study_id))
    conn.commit()
    return get_part(conn, model_id, part_key)


def detach_study(conn, model_id, part_key, clear_study=True):
    """解除部件与 Study 的关联；clear_study=True 时同时清掉 studies 上的归属列。"""
    part = _require_part(conn, model_id, part_key)
    sid = part["study_id"]
    conn.execute(
        "UPDATE model_config_parts SET study_id=NULL WHERE model_id=? AND part_key=?",
        (model_id, part_key))
    if clear_study and sid:
        conn.execute("UPDATE studies SET model_id=NULL, part_key=NULL WHERE id=?",
                     (sid,))
    conn.commit()
    return get_part(conn, model_id, part_key)


def create_part_study(conn, model_id, part_key, name=None, study_id=None):
    """
    便捷函数：为型号配置的某部件创建 Study 并关联（返回 study_id）。

    用途：工程师在型号层级一键为各部件开 Study；创建后即可交给 run_study 跑
    （参数集用 get_part(...)["params"]），从而「全机 → 部件 → Study」闭合。
    """
    part = _require_part(conn, model_id, part_key)
    cfg = get_model_config(conn, model_id)
    sid = study_id or ("%s-%s-%s" % (cfg["model_id"], part_key, uuid.uuid4().hex[:6]))
    title = name or ("%s / %s（%s）" % (cfg["name"], part_key, part["template_id"]))
    existing = conn.execute("SELECT 1 FROM studies WHERE id=?", (sid,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO studies(id, name, created_at, note, model_id, part_key) "
            "VALUES (?,?,?,?,?,?)",
            (sid, title, _now(), cfg.get("note"), model_id, part_key))
        conn.commit()
    attach_study(conn, model_id, part_key, sid)
    return sid


# ---------------------------------------------------------------------------
# 层级查询：全机 -> 部件 -> Study
# ---------------------------------------------------------------------------

def get_model_tree(conn, model_id):
    """
    返回「全机 → 部件 → Study」层级结构。

    结构::

        {"model_id": ..., "name": ..., "overall": {...}, "rule": {...},
         "created_at": ..., "note": ...,
         "parts": [<顶层部件节点>, ...],       # parent_part 为空的部件
         "all_parts": [<全部部件节点，扁平>, ...],
         "study_ids": [...]}

    部件节点::

        {"part_key": "wing", "template_id": "wingbox_v1",
         "params": {...}, "parent_part": None, "study_id": "S-...",
         "study": {"study_id":..., "name":..., "n_artifacts":...} | None,
         "studies": [<该部件在型号下的全部 Study（含历史）>],
         "children": [<下级部件节点>]}
    """
    cfg = get_model_config(conn, model_id)
    parts = list_parts(conn, model_id)

    # 各部件在型号下的全部 Study（历史 + 当前）
    hist = {}
    for row in conn.execute(
            "SELECT * FROM studies WHERE model_id=? ORDER BY created_at, id",
            (model_id,)).fetchall():
        info = _study_info(conn, row["id"])
        if info is None:
            continue
        key = row["part_key"] if "part_key" in row.keys() else None
        hist.setdefault(key or "__unassigned__", []).append(info)

    nodes = {}
    for part in parts:
        key = part["part_key"]
        node = dict(part)
        node["children"] = []
        node["study"] = _study_info(conn, part["study_id"])
        node["studies"] = list(hist.get(key) or [])
        nodes[key] = node

    top = []
    for part in parts:
        node = nodes[part["part_key"]]
        parent = part["parent_part"]
        if parent and parent in nodes and parent != part["part_key"]:
            nodes[parent]["children"].append(node)
        else:
            # 上级部件不存在 / 自引用时按顶层处理（不让层级查询把部件"吞掉"）
            top.append(node)

    tree = {
        "model_id": cfg["model_id"],
        "name": cfg["name"],
        "overall": cfg["overall"],
        "rule": cfg["rule"],
        "note": cfg["note"],
        "created_at": cfg["created_at"],
        "parts": top,
        "all_parts": [nodes[p["part_key"]] for p in parts],
        "study_ids": [p["study_id"] for p in parts if p["study_id"]],
    }
    return tree


def iter_parts(tree):
    """深度优先遍历层级树里的全部部件节点（扁平化，含 children）。"""
    stack = list(tree.get("parts") or [])
    while stack:
        node = stack.pop(0)
        yield node
        stack = list(node.get("children") or []) + stack


def get_part_node(conn, model_id, part_key):
    """按部件键从层级树里取节点（含 children / study / studies）。"""
    for node in iter_parts(get_model_tree(conn, model_id)):
        if node["part_key"] == part_key:
            return node
    return None


def get_part_children(conn, model_id, part_key):
    """取某部件的直接下级部件（全机 -> 部件 -> 子部件）。"""
    node = get_part_node(conn, model_id, part_key)
    return list(node["children"]) if node else []


def studies_of_model(conn, model_id):
    """型号配置下的全部 Study（含各部件历史版本），带部件键与产物数。"""
    _require_model(conn, model_id)
    out = []
    for row in conn.execute(
            "SELECT * FROM studies WHERE model_id=? ORDER BY created_at, id",
            (model_id,)).fetchall():
        info = _study_info(conn, row["id"])
        if info is None:
            continue
        info["model_id"] = row["model_id"]
        info["part_key"] = row["part_key"]
        out.append(info)
    return out


def resolve_model_of_study(conn, study_id):
    """反查：某 Study 属于哪个型号配置的哪个部件（血缘查询的入口之一）。"""
    row = conn.execute("SELECT * FROM studies WHERE id=?", (study_id,)).fetchone()
    if row is None:
        return None
    mid = row["model_id"] if "model_id" in row.keys() else None
    if not mid:
        return None
    cfg_row = conn.execute("SELECT name FROM model_configs WHERE id=?",
                           (mid,)).fetchone()
    return {
        "model_id": mid,
        "model_name": cfg_row["name"] if cfg_row else None,
        "part_key": row["part_key"] if "part_key" in row.keys() else None,
        "study_id": study_id,
    }


# ---------------------------------------------------------------------------
# 血缘：按型号配置汇总各部件 Study 及产物数（跨部件追溯）
# ---------------------------------------------------------------------------

def _kind_counts(conn, study_id):
    counts = {}
    for row in conn.execute(
            "SELECT kind, COUNT(*) c FROM artifacts WHERE study_id=? "
            "GROUP BY kind ORDER BY kind", (study_id,)).fetchall():
        counts[row["kind"]] = int(row["c"])
    return counts


def model_lineage(conn, model_id):
    """
    型号级血缘汇总：每个部件的 Study 及其各类产物数（血缘可跨部件追溯）。

    返回::

        {"model_id": ..., "name": ..., "overall": {...},
         "parts": [{"part_key", "template_id", "parent_part", "study_id",
                    "study_name", "n_artifacts", "by_kind": {...},
                    "studies": [{study_id, n_artifacts, by_kind}, ...]}, ...],
         "totals": {"n_parts", "n_parts_with_study", "n_studies",
                    "n_artifacts", "by_kind": {...}}}
    """
    cfg = get_model_config(conn, model_id)
    parts = list_parts(conn, model_id)
    all_studies = studies_of_model(conn, model_id)

    totals_kind = {}
    part_rows = []
    total_studies = 0
    total_artifacts = 0
    with_study = 0

    for part in parts:
        key = part["part_key"]
        mine = [s for s in all_studies if s.get("part_key") == key]
        per = []
        for s in mine:
            counts = _kind_counts(conn, s["study_id"])
            n = sum(counts.values())
            total_studies += 1
            total_artifacts += n
            for kind, c in counts.items():
                totals_kind[kind] = totals_kind.get(kind, 0) + c
            per.append({"study_id": s["study_id"], "name": s["name"],
                        "n_artifacts": n, "by_kind": counts})
        cur = None
        if part["study_id"]:
            with_study += 1
            cur = _study_info(conn, part["study_id"])
            if cur is not None:
                cur["by_kind"] = _kind_counts(conn, part["study_id"])
        part_rows.append({
            "part_key": key,
            "template_id": part["template_id"],
            "parent_part": part["parent_part"],
            "study_id": part["study_id"],
            "study_name": cur["name"] if cur else None,
            "n_artifacts": cur["n_artifacts"] if cur else 0,
            "by_kind": cur["by_kind"] if cur else {},
            "studies": per,
        })

    return {
        "model_id": cfg["model_id"],
        "name": cfg["name"],
        "overall": cfg["overall"],
        "parts": part_rows,
        "totals": {
            "n_parts": len(parts),
            "n_parts_with_study": with_study,
            "n_studies": total_studies,
            "n_unassigned_studies": len(
                [s for s in all_studies if not s.get("part_key")]),
            "n_artifacts": total_artifacts,
            "by_kind": totals_kind,
        },
    }


def model_artifact_rows(conn, model_id):
    """
    型号下全部 Study 的产物行（带 part_key），按部件 + 产物 id 排序。

    血缘查询（如"该型号所有报告版本"/"跨部件追溯某类产物"）直接用这个。
    """
    _require_model(conn, model_id)
    rows = conn.execute(
        "SELECT a.*, s.part_key AS part_key, s.model_id AS model_id "
        "FROM artifacts a JOIN studies s ON s.id = a.study_id "
        "WHERE s.model_id = ? ORDER BY a.study_id, a.id", (model_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 人类可读摘要（UI/CLI 用）
# ---------------------------------------------------------------------------

def summarize_tree(tree):
    """把层级树渲染成缩进文本：全机 -> 部件 -> Study。"""
    lines = ["型号配置 %s  %s" % (tree["model_id"], tree["name"])]
    overall = tree.get("overall") or {}
    if overall:
        lines.append("  总体参数: " + ", ".join(
            "%s=%s" % (k, overall[k]) for k in sorted(overall)))
    for node in iter_parts(tree):
        depth = 1
        parent = node.get("parent_part")
        while parent:
            depth += 1
            pnode = None
            for n in tree.get("all_parts") or []:
                if n["part_key"] == parent:
                    pnode = n
                    break
            parent = pnode.get("parent_part") if pnode else None
        pad = "  " * depth
        study = node.get("study")
        tail = ("-> Study %s (%d artifacts)"
                % (study["study_id"], study["n_artifacts"])) if study else "-> (无 Study)"
        lines.append("%s- %s [%s] %s" % (pad, node["part_key"],
                                        node["template_id"], tail))
    return "\n".join(lines)
