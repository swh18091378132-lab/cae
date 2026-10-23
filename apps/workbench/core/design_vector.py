# -*- coding: utf-8 -*-
"""
design_vector.py -- 设计向量声明与映射（票22）

对应 issue #22 / ADR-0009（优化闭环：单一设计向量驱动双模型）、
ADR-0005（铺层是 CAE 属性 -> 纯铺层变动不必再生数模）。

## 为什么是"声明驱动"

优化器要搜索的变量会随部件族和项目而变。若把变量写进代码，每加一个变量就得改优化器
——优化器于是被绑死在某个具体模板上。本模块把变量声明做成**纯数据**（dict 或 JSON 文件）：

    {
      "template_id": "wingbox_v1",
      "variables": [
        {"name": "span",     "category": "geometry", "type": "continuous",
         "min": 5.0, "max": 40.0, "default": 12.0},
        {"name": "n_ribs",   "category": "geometry", "type": "discrete",
         "min": 2, "max": 40, "step": 1, "default": 6},
        {"name": "n_plies",  "category": "ply", "type": "discrete",
         "min": 2, "max": 16, "step": 2, "default": 8},
        {"name": "angle_0",  "category": "ply", "type": "discrete",
         "candidates": [0.0, 45.0, -45.0, 90.0], "default": 0.0},
        {"name": "ply_thickness", "category": "ply", "type": "continuous",
         "min": 0.05, "max": 0.3, "default": 0.125}
      ],
      "ply_sequence": {"n_plies": "n_plies",
                       "angles": ["angle_0", ...],
                       "thickness": "ply_thickness",
                       "order": "cyclic"}
    }

优化器只认 `variable_names / default_vector / discrete_values / coerce_vector`，
不认任何具体变量名。**新增可优化变量 = 改这份声明，代码零改动**。

## 三类映射

1. **向量 → 参数集**：`category == "geometry"` 的变量进参数集（变量名默认就是模板参数名，
   可用 `"param"` 显式改名 —— 这样设计变量命名不必迁就 CATIA 里的参数名）。
2. **向量 → 铺层**：铺层不把"不定长列表"当数组搜索，而是参数化成
   **层数 + 定长角度槽 + 单层厚度**：第 k 层角度取 `angles[k % len(angles)]`
   （`order="cyclic"`）或按镜像叠层展开（`order="mirror"`）。搜索空间因此是定长的，
   可去重、可缓存。
3. **是否需要再生数模**：几何变量全未变、只有铺层变 -> `False`（ADR-0005）。

## 离散整数性

肋数、长桁数、层数不允许出现 6.37。`discrete_values` 给出合法取值集合，
`snap_value` 把任意实数吸附到最近合法值（整型离散变量吸附后**仍是 int**），
`validate_vector` 对不合法的离散值**直接拒绝**（不接受静默吸附过的脏数据）。

## 包络守卫

越出参数化包络**不静默穿越**：复用模板已声明的参数契约 + 跨参数包络
（`template_registry.Template.validate`），不另造第二套包络口径。越界时给出可读告警并
提示"包络外应更换模板"（GLOSSARY：参数化包络）。

纯标准库、无第三方依赖、兼容 Python 3.8。
"""

import json

try:
    from . import template_registry
except ImportError:  # 允许作为脚本/在 core 目录内直接运行
    import template_registry


# 变量类型
TYPE_CONTINUOUS = "continuous"
TYPE_DISCRETE = "discrete"
_TYPES = (TYPE_CONTINUOUS, TYPE_DISCRETE)

# 变量类别
CATEGORY_GEOMETRY = "geometry"
CATEGORY_PLY = "ply"
_CATEGORIES = (CATEGORY_GEOMETRY, CATEGORY_PLY)

# 铺层展开顺序
ORDER_CYCLIC = "cyclic"
ORDER_MIRROR = "mirror"
_ORDERS = (ORDER_CYCLIC, ORDER_MIRROR)

_NUM_TOL = 1e-9


class DesignVectorError(ValueError):
    """声明不合法，或向量取值不合法（类型/范围/离散集合）。"""


class EnvelopeError(DesignVectorError):
    """设计点越出参数化包络（包络外应更换模板，不是靠搜索穿越）。"""


# ---------------------------------------------------------------------------
# 声明归一化与校验
# ---------------------------------------------------------------------------

def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _key_order(spec):
    """声明里的变量顺序（优化器采样、去重、导出都按同一顺序，保证确定性）。"""
    return [v["name"] for v in spec.get("variables") or []]


def _norm_variable(raw, idx):
    """归一化 + 校验单个变量声明。"""
    if not isinstance(raw, dict):
        raise DesignVectorError("变量声明 #%d 应为对象，实际 %r" % (idx, type(raw).__name__))
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise DesignVectorError("变量声明 #%d 缺少合法的 name: %r" % (idx, name))

    category = raw.get("category")
    if category not in _CATEGORIES:
        raise DesignVectorError(
            "变量 %s 的 category 应为 %s 之一，实际 %r"
            % (name, "/".join(_CATEGORIES), category))

    typ = raw.get("type")
    if typ not in _TYPES:
        raise DesignVectorError(
            "变量 %s 的 type 应为 %s 之一，实际 %r"
            % (name, "/".join(_TYPES), typ))

    out = {"name": name, "category": category, "type": typ,
           # 变量名默认即模板参数名；"param" 可显式改名
           "param": raw.get("param") or name}

    if typ == TYPE_DISCRETE and raw.get("candidates") is not None:
        cands = raw.get("candidates")
        if not isinstance(cands, (list, tuple)) or not cands:
            raise DesignVectorError("变量 %s 的 candidates 应为非空列表" % name)
        for c in cands:
            if not _is_num(c):
                raise DesignVectorError("变量 %s 的候选值应为数值，实际 %r" % (name, c))
        out["candidates"] = list(cands)
    else:
        for k in ("min", "max"):
            if k not in raw or not _is_num(raw[k]):
                raise DesignVectorError("变量 %s 缺少合法的 %s" % (name, k))
            out[k] = raw[k]
        if not out["max"] > out["min"]:
            raise DesignVectorError(
                "变量 %s 的 max(%r) 必须大于 min(%r)" % (name, out["max"], out["min"]))
        if typ == TYPE_DISCRETE:
            step = raw.get("step", 1)
            if not _is_num(step) or step <= 0:
                raise DesignVectorError("变量 %s 的 step 应为正数，实际 %r" % (name, step))
            out["step"] = step

    if "default" not in raw:
        raise DesignVectorError("变量 %s 缺少 default" % name)
    out["default"] = raw["default"]
    if "unit" in raw:
        out["unit"] = raw["unit"]
    if "note" in raw:
        out["note"] = raw["note"]
    return out


def _norm_ply_sequence(raw, variables):
    """归一化 + 校验铺层参数化声明。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DesignVectorError("ply_sequence 应为对象，实际 %r" % (type(raw).__name__,))

    by_name = dict((v["name"], v) for v in variables)

    def _ref(field, allow_many=False):
        val = raw.get(field)
        if val is None:
            raise DesignVectorError("ply_sequence 缺少 %s" % field)
        names = list(val) if allow_many else [val]
        if allow_many and not names:
            raise DesignVectorError("ply_sequence.%s 不能为空" % field)
        for n in names:
            if n not in by_name:
                raise DesignVectorError(
                    "ply_sequence.%s 引用不存在的变量 %r（已声明 %s）"
                    % (field, n, ", ".join(sorted(by_name))))
            if by_name[n]["category"] != CATEGORY_PLY:
                raise DesignVectorError(
                    "ply_sequence.%s 引用的变量 %r 不是铺层类变量" % (field, n))
        return names if allow_many else names[0]

    out = {
        "n_plies": _ref("n_plies"),
        "angles": _ref("angles", allow_many=True),
        "thickness": _ref("thickness"),
        "material": raw.get("material", "CFRP"),
        "order": raw.get("order", ORDER_CYCLIC),
    }
    if out["order"] not in _ORDERS:
        raise DesignVectorError(
            "ply_sequence.order 应为 %s 之一，实际 %r"
            % ("/".join(_ORDERS), out["order"]))

    # 角度档位表（spec 用户故事 10 的「角度档位」）：
    # 给了 angle_table 时，角度变量存的是**档位索引**（整数），展开时查表换算成角度。
    # 为什么需要它：优化器的离散候选值只接受整数（它要为离散变量做 ±1 步的邻域搜索），
    # 而铺向角 0/45/-45/90 在领域里天然带小数。用"整数档位 + 角度表"把两件事解耦：
    # 搜索空间是整数（优化器原生可搜），语义上是角度（人可读、可落库）。
    table = raw.get("angle_table")
    if table is not None:
        if not isinstance(table, (list, tuple)) or not table:
            raise DesignVectorError("ply_sequence.angle_table 应为非空列表")
        for a in table:
            if not _is_num(a):
                raise DesignVectorError("angle_table 的角度应为数值，实际 %r" % (a,))
        out["angle_table"] = [float(a) for a in table]
        for n in out["angles"]:
            if by_name[n]["type"] != TYPE_DISCRETE:
                raise DesignVectorError(
                    "使用 angle_table 时角度变量 %r 必须是离散档位（整数索引）" % n)
            legal = _variable_discrete_values(by_name[n])
            if not all(_is_num(v) and float(v) == int(float(v))
                       and 0 <= int(float(v)) < len(table) for v in legal):
                raise DesignVectorError(
                    "使用 angle_table（共 %d 档）时角度变量 %r 的候选值必须是 "
                    "0..%d 的整数档位索引，实际 %r"
                    % (len(table), n, len(table) - 1, legal))

    # 反向完整性：声明成 ply 类的变量必须都被铺层参数化引用，
    # 否则它既不进参数集、也不进铺层 —— 是个静默失效的"假设计变量"。
    used = set([out["n_plies"], out["thickness"]] + list(out["angles"]))
    orphans = [v["name"] for v in variables
               if v["category"] == CATEGORY_PLY and v["name"] not in used]
    if orphans:
        raise DesignVectorError(
            "铺层类变量未被 ply_sequence 引用（不会进入任何映射）: %s"
            % ", ".join(sorted(orphans)))
    return out


def normalize_spec(spec):
    """
    归一化并校验设计向量声明，返回可直接使用的 spec（内部带 `_normalized` 标记）。

    声明是数据，所以**数据的错要在这里早报**：未知类型/类别、重名、缺范围、
    铺层参数化引用不一致，都在此抛 DesignVectorError，而不是等采样到一半才炸。
    """
    if not isinstance(spec, dict):
        raise DesignVectorError("设计向量声明应为对象，实际 %r"
                                % (type(spec).__name__,))

    raw_vars = spec.get("variables")
    if not isinstance(raw_vars, (list, tuple)) or not raw_vars:
        raise DesignVectorError("设计向量声明至少要有一个变量（variables 为空）")

    variables = []
    seen = set()
    for i, raw in enumerate(raw_vars):
        var = _norm_variable(raw, i)
        if var["name"] in seen:
            raise DesignVectorError("变量名重复: %r" % var["name"])
        seen.add(var["name"])
        variables.append(var)

    ply_seq = _norm_ply_sequence(spec.get("ply_sequence"), variables)

    # 几何变量不得出现在铺层参数化里（否则同一个变量两条映射路径，血缘会对不上）
    if ply_seq is not None:
        ply_refs = set([ply_seq["n_plies"], ply_seq["thickness"]] + ply_seq["angles"])
        bad = [v["name"] for v in variables
               if v["category"] == CATEGORY_GEOMETRY and v["name"] in ply_refs]
        if bad:
            raise DesignVectorError(
                "几何类变量不应出现在 ply_sequence 中: %s" % ", ".join(sorted(bad)))

    out = {
        "_normalized": True,
        "id": spec.get("id") or "design_vector",
        "template_id": spec.get("template_id"),
        "variables": variables,
        "ply_sequence": ply_seq,
    }
    for k in ("name", "note", "description"):
        if k in spec:
            out[k] = spec[k]

    # 默认值必须自洽（否则 default_vector 会产出一个不合法设计点）
    default_vector(out)
    return out


def load_spec(path):
    """从 JSON 文件加载声明（声明可落盘、可版本化、可随模板一起交付）。"""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return normalize_spec(data)


def _as_spec(spec):
    """接受「已归一化」或「原始声明」两种输入。"""
    if isinstance(spec, dict) and spec.get("_normalized"):
        return spec
    return normalize_spec(spec)


# ---------------------------------------------------------------------------
# 声明查询
# ---------------------------------------------------------------------------

def variable_names(spec):
    return _key_order(_as_spec(spec))


def variables(spec):
    return list(_as_spec(spec).get("variables") or [])


def variable(spec=None, name=None):
    """按名字取变量声明；找不到抛 DesignVectorError（不返回 None）。"""
    s = _as_spec(spec)
    for v in s["variables"]:
        if v["name"] == name:
            return dict(v)
    raise DesignVectorError("声明中没有变量 %r（已声明 %s）"
                            % (name, ", ".join(_key_order(s))))


def geometry_variable_names(spec):
    return [v["name"] for v in _as_spec(spec)["variables"]
            if v["category"] == CATEGORY_GEOMETRY]


def ply_variable_names(spec):
    return [v["name"] for v in _as_spec(spec)["variables"]
            if v["category"] == CATEGORY_PLY]


def has_ply_variables(spec):
    return bool(ply_variable_names(spec))


def ply_sequence(spec):
    return _as_spec(spec).get("ply_sequence")


# ---------------------------------------------------------------------------
# 取值域：离散集合 / 吸附
# ---------------------------------------------------------------------------

def _variable_discrete_values(var):
    """单个变量声明 -> 合法离散取值集合（升序）。归一化之前的声明也可用。"""
    cands = var.get("candidates")
    if cands is not None:
        return list(cands)

    lo, hi, step = var["min"], var["max"], var.get("step", 1)
    ints = all(float(x) == int(float(x)) for x in (lo, hi, step))
    if ints:
        return list(range(int(lo), int(hi) + 1, int(step)))

    out = []
    k = 0
    while True:
        v = lo + k * step
        if v > hi + _NUM_TOL:
            break
        out.append(round(float(v), 12))
        k += 1
    return out


def discrete_values(spec, name):
    """
    离散变量的合法取值集合（升序）。

    显式 `candidates` -> 原样；否则按 `min/max/step` 生成。**整型变量产出 int**
    —— 这是"肋数不会出现 6.37"的第一道保险。
    """
    var = variable(spec, name)
    if var["type"] != TYPE_DISCRETE:
        raise DesignVectorError("变量 %r 不是离散变量，没有取值集合" % name)
    return _variable_discrete_values(var)


def snap_value(spec, name, raw):
    """
    把任意数值吸附到该变量的合法取值。

    离散变量 -> 最近合法值（整型变量的结果**一定是 int**）；
    连续变量 -> 夹到 [min, max] 区间内（不量化，连续就是连续）。
    """
    var = variable(spec, name)
    if not _is_num(raw):
        raise DesignVectorError("变量 %s 的取值应为数值，实际 %r" % (name, raw))

    if var["type"] == TYPE_CONTINUOUS:
        return _clamp(var, raw)

    cands = var.get("candidates")
    if cands is not None:
        best = None
        best_d = None
        for c in cands:
            d = abs(float(c) - float(raw))
            if best_d is None or d < best_d - _NUM_TOL:
                best, best_d = c, d
        return best

    lo, hi, step = var["min"], var["max"], var["step"]
    v = min(max(float(raw), float(lo)), float(hi))
    k = round((v - float(lo)) / float(step))
    val = float(lo) + k * float(step)
    if val > float(hi) + _NUM_TOL:
        val = float(lo) + int((float(hi) - float(lo)) // float(step)) * float(step)
    if val < float(lo) - _NUM_TOL:
        val = float(lo)
    if all(float(x) == int(float(x)) for x in (lo, step)):
        return int(round(val))
    return round(val, 12)


def _clamp(var, raw):
    lo, hi = float(var["min"]), float(var["max"])
    v = float(raw)
    if v < lo - _NUM_TOL:
        v = lo
    if v > hi + _NUM_TOL:
        v = hi
    return v


# ---------------------------------------------------------------------------
# 向量：默认值 / 吸附 / 严格校验
# ---------------------------------------------------------------------------

def default_vector(spec):
    """声明里的默认设计点（变量顺序与声明一致，可直接当 DOE 起点）。"""
    s = _as_spec(spec)
    out = {}
    for v in s["variables"]:
        val = v["default"]
        if v["type"] == TYPE_DISCRETE:
            got = snap_value(s, v["name"], val)
            if not _same(got, val):
                raise DesignVectorError(
                    "变量 %s 的 default=%r 不是合法离散取值（最近合法值 %r）"
                    % (v["name"], val, got))
            val = got
        else:
            val = _clamp(v, val)
            if not _same(val, v["default"]):
                raise DesignVectorError(
                    "变量 %s 的 default=%r 超出声明范围 [%r, %r]"
                    % (v["name"], v["default"], v["min"], v["max"]))
        out[v["name"]] = val
    return out


def coerce_vector(spec, vector):
    """
    宽松归一：缺省变量补默认值，离散变量吸附到最近合法值，连续变量夹到范围内。

    用途是"把任意采样结果收拾成一个能用的设计点"；**严格校验请用 validate_vector**
    —— 二者的分工就是"生成"与"验收"。
    """
    s = _as_spec(spec)
    if not isinstance(vector, dict):
        raise DesignVectorError("设计向量应为 dict，实际 %r" % (type(vector).__name__,))
    unknown = [k for k in vector if k not in _key_order(s)]
    if unknown:
        raise DesignVectorError(
            "向量里有声明外的变量: %s（声明只有 %s）"
            % (", ".join(sorted(unknown)), ", ".join(_key_order(s))))

    out = {}
    for v in s["variables"]:
        name = v["name"]
        raw = vector[name] if name in vector else v["default"]
        if v["type"] == TYPE_DISCRETE:
            out[name] = snap_value(s, name, raw)
        else:
            if not _is_num(raw):
                raise DesignVectorError("变量 %s 的取值应为数值，实际 %r" % (name, raw))
            out[name] = _clamp(v, raw)
    return out


def validate_vector(spec, vector):
    """
    严格校验一个设计向量，返回补全后的完整向量；不合法抛 DesignVectorError。

    与 coerce_vector 的关键区别：**离散变量必须已经落在合法取值上**。
    吸附过的值能通过，6.37 这样的值不能 —— 这样"离散整数性"不会被上游的宽容悄悄破坏。
    """
    s = _as_spec(spec)
    if not isinstance(vector, dict):
        raise DesignVectorError("设计向量应为 dict，实际 %r" % (type(vector).__name__,))
    unknown = [k for k in vector if k not in _key_order(s)]
    if unknown:
        raise DesignVectorError(
            "向量里有声明外的变量: %s（声明只有 %s）"
            % (", ".join(sorted(unknown)), ", ".join(_key_order(s))))

    out = {}
    errors = []
    for v in s["variables"]:
        name = v["name"]
        raw = vector[name] if name in vector else v["default"]
        if not _is_num(raw):
            errors.append("变量 %s 的取值应为数值，实际 %r" % (name, raw))
            continue

        if v["type"] == TYPE_DISCRETE:
            legal = discrete_values(s, name)
            hit = [c for c in legal if _same(c, raw)]
            if not hit:
                errors.append(
                    "变量 %s=%r 不是合法离散取值（最近合法值 %r，肋数/长桁数/层数"
                    "这类变量不允许出现小数）"
                    % (name, raw, snap_value(s, name, raw)))
                continue
            out[name] = hit[0]
        else:
            lo, hi = float(v["min"]), float(v["max"])
            if float(raw) < lo - _NUM_TOL or float(raw) > hi + _NUM_TOL:
                errors.append("变量 %s=%r 超出范围 [%r, %r]"
                              % (name, raw, v["min"], v["max"]))
                continue
            out[name] = float(raw)

    if errors:
        raise DesignVectorError("设计向量不合法:\n  - " + "\n  - ".join(errors))
    return out


def _same(a, b, tol=_NUM_TOL):
    try:
        return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)), abs(float(b)))
    except (TypeError, ValueError):
        return a == b


# ---------------------------------------------------------------------------
# 向量 -> (参数集, 铺层)
# ---------------------------------------------------------------------------

def to_params(spec, vector):
    """几何变量 -> 参数集（键名按 `param` 映射，默认即变量名）。"""
    s = _as_spec(spec)
    dv = coerce_vector(s, vector)
    out = {}
    for v in s["variables"]:
        if v["category"] == CATEGORY_GEOMETRY:
            out[v["param"]] = dv[v["name"]]
    return out


def ply_angles(spec, vector):
    """按「定长角度槽 + 层数」展开每层角度（参数化铺层的核心一步）。"""
    s = _as_spec(spec)
    seq = s.get("ply_sequence")
    if not seq:
        return []
    dv = coerce_vector(s, vector)
    n = int(round(float(dv[seq["n_plies"]])))
    if n <= 0:
        return []

    # 有 angle_table 时角度变量是**整数档位索引**，展开前查表换算成角度
    table = seq.get("angle_table")
    if table is not None:
        slots = [float(table[int(round(float(dv[a])))]) for a in seq["angles"]]
    else:
        slots = [float(dv[a]) for a in seq["angles"]]
    if not slots:
        raise DesignVectorError("ply_sequence.angles 不能为空")

    if seq["order"] == ORDER_MIRROR:
        # 镜像叠层：前半按角度槽循环，后半镜像 —— 仍是定长槽，可去重可缓存
        half = (n + 1) // 2
        first = [slots[k % len(slots)] for k in range(half)]
        return (first + list(reversed(first))[:n - half])[:n]
    return [slots[k % len(slots)] for k in range(n)]


def to_plies(spec, vector):
    """
    铺层变量 -> run_study 的 `plies` 列表；无铺层声明时返回 None
    （None = 沿用 run_study/FeModel 的默认铺层，保持 P1/P2 口径）。
    """
    s = _as_spec(spec)
    seq = s.get("ply_sequence")
    if not seq:
        return None
    dv = coerce_vector(s, vector)
    thickness = float(dv[seq["thickness"]])
    material = seq.get("material") or "CFRP"
    return [{"angle": a, "thickness": thickness, "material": material}
            for a in ply_angles(s, dv)]


def to_inputs(spec, vector, template=None, strict=False):
    """
    向量 -> 一次 run_study 所需的设计输入。

    返回::

        {"template_id": ...,            # 该设计点用哪个模板族
         "params": {...},               # 进参数契约校验的参数集
         "plies": [...] | None,         # 进 RunOptions.plies 的铺层
         "vector": {...},               # 归一后的完整向量（落库/导出用）
         "warnings": [...],             # 越包络等告警（不静默）
         "envelope": {...} | None}      # 包络检查明细

    template 给出时做**包络守卫**：
      * strict=True  -> 越界直接抛 EnvelopeError（拒绝产出非法设计点）
      * strict=False -> 把告警放进 warnings，仍返回可用映射
        （优化器要把这个点记成"越包络"后跳过，而不是中断整次搜索）
    """
    s = _as_spec(spec)
    dv = coerce_vector(s, vector)
    out = {
        "template_id": s.get("template_id"),
        "params": to_params(s, dv),
        "plies": to_plies(s, dv),
        "vector": dv,
        "warnings": [],
        "envelope": None,
    }
    if template is None:
        return out

    rep = envelope_report(s, dv, template)
    out["envelope"] = rep
    if not rep["ok"]:
        if strict:
            raise EnvelopeError("\n".join(rep["messages"]))
        out["warnings"].extend(rep["messages"])
    return out


# ---------------------------------------------------------------------------
# 包络守卫
# ---------------------------------------------------------------------------

_ENVELOPE_HINT = "包络外应更换模板，不应靠优化搜索穿越"


def envelope_report(spec, vector, template):
    """
    检查设计点是否越出该模板的**参数化包络**。

    直接复用 `template_registry.Template.validate`：参数契约（类型/范围/未知参数）
    与跨参数包络约束（`envelope.constraints`）都在那里，是唯一口径。
    这里只负责把异常翻译成"可读告警 + 处置建议"。

    返回 {"ok": bool, "violations": [str], "messages": [str], "params": {...}}
    """
    if template is None:
        raise DesignVectorError("包络检查需要模板（template 为 None）")
    s = _as_spec(spec)
    params = to_params(s, vector)
    violations = []
    messages = []
    try:
        template.validate(params)
    except template_registry.ParamError as exc:
        msg = str(exc)
        violations.append(msg)
        messages.append("越出参数化包络: %s（%s）" % (msg, _ENVELOPE_HINT))
    except Exception as exc:  # 契约求值炸了也算"不可用的设计点"，不吞
        violations.append(str(exc))
        messages.append("参数化包络检查失败: %s（%s）" % (exc, _ENVELOPE_HINT))
    return {"ok": not violations, "violations": violations,
            "messages": messages, "params": params}


# ---------------------------------------------------------------------------
# 是否需要再生数模（ADR-0005 的成本优化）
# ---------------------------------------------------------------------------

def needs_regeneration(spec, current, previous):
    """
    是否需要重新再生参数化数模。

    ADR-0005：铺层是 CAE 属性，几何没变就不必再生。于是优化里"纯铺层轮次"可以省掉
    整次 CATIA 再生 —— 这是本 spec 明确点名的一条成本优化。

    参数顺序是 (当前设计点, 上一个设计点)。

    返回::

        {"needs_regeneration": bool,   # 有几何变量变化才需要
         "changed_geometry": [变量名], # 变了哪些几何变量
         "changed_ply": [变量名],      # 变了哪些铺层变量
         "changed_params": [参数名]}   # 需要重新进 CATIA 的参数名
    """
    s = _as_spec(spec)
    cur = coerce_vector(s, current)
    prev = coerce_vector(s, previous)

    changed_geo = [n for n in geometry_variable_names(s)
                   if not _same(cur[n], prev[n])]
    changed_ply = [n for n in ply_variable_names(s)
                   if not _same(cur[n], prev[n])]
    changed_params = [variable(s, n)["param"] for n in changed_geo]

    return {
        "needs_regeneration": bool(changed_geo),
        "changed_geometry": changed_geo,
        "changed_ply": changed_ply,
        "changed_params": changed_params,
    }


# ---------------------------------------------------------------------------
# 便捷封装：设计点导出 / 比较
# ---------------------------------------------------------------------------

def describe(spec, vector):
    """人类可读的一行摘要（日志/汇总用）。"""
    s = _as_spec(spec)
    dv = coerce_vector(s, vector)
    parts = []
    for v in s["variables"]:
        parts.append("%s=%s" % (v["name"], dv[v["name"]]))
    return ", ".join(parts)


def changed_summary(spec, current, previous):
    """一行说明"这一版比上一版改了什么"（票22 用户故事 13：事后知道改了什么）。"""
    d = needs_regeneration(spec, current, previous)
    if not d["changed_geometry"] and not d["changed_ply"]:
        return "与上一版相同"
    bits = []
    if d["changed_geometry"]:
        bits.append("几何: " + ", ".join(d["changed_geometry"]))
    if d["changed_ply"]:
        bits.append("铺层: " + ", ".join(d["changed_ply"]))
    bits.append("需要再生数模" if d["needs_regeneration"] else "无需再生数模")
    return "；".join(bits)
