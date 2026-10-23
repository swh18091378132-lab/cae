# -*- coding: utf-8 -*-
"""
optimizer.py -- DOE 采样、约束筛选、收敛判定（票 23）

对应 ADR-0002（AI 三层：优化器是 L0 确定性搜索）与 ADR-0009（优化闭环）。
术语见 GLOSSARY：参数化包络 / 强度校核 / 安全裕度。

## 为什么「注入 evaluate」而不是直接跑 Study

优化器的职责是**搜索**，不是建模也不是求解。所以核心入口只接受一个回调：

    evaluate(设计向量) -> 评估结果

它不知道 Study、不知道 Abaqus、不碰数据库 —— 于是单测可以完全不跑求解，
用解析可算的假目标在秒级验证搜索逻辑本身对不对（真目标要几分钟一次，
拿它测搜索逻辑只能测出"跑得通"，测不出"搜得对"）。

评估结果的形状由 `core/objectives.evaluate()` 固定（票21），本模块只消费它。

## 三条不假设目标光滑的规则

FI/MS 来自离散求解，目标是**阶梯状/带噪声**的，梯度没有意义。所以：

  1. **离散变量做邻域搜索**：候选值集合里逐个试相邻值（±1 步），不做插值。
  2. **连续变量做步长收缩**：在当前最优点两侧按步长试探；连续若干轮无改进就把
     步长减半，直到步长低于下限（或预算耗尽）。不做梯度下降、不估计斜率。
  3. **只接受严格改进**：新点必须比当前最优更好才移动中心。阶梯目标上"看起来
     平"（等值平台）不算改进，因此不会在平台上乱漂。

## 技术故障绝不进入"最优"比较

评估结果有三种形态（objectives.evaluate 的口径）：

    ok=True,  feasible=True   可行      -> 可以参与"最优"比较
    ok=True,  feasible=False  不可行    -> 只记录，不参与
    ok=False, feasible=None   技术故障  -> 只记录，不参与，**也不当作"目标很差"**

最后一条是本模块最容易写错的地方：如果按目标值排序，一个"求解失败、目标值恰好
很小"的点会被选成最优方案。因此排序一律走 `objectives.sort_key()` 的三分组语义
`(group, value)`（可行=0 / 不可行=1 / 故障=2，故障的 value 是 +inf），
**不直接按 value 排序**。

## 去重与缓存

离散变量下撞点极常见（9 个肋数 × 3 个铺层组合）。同一个设计向量只评估一次，
命中缓存时**不调用 evaluate**（调用次数是可观测的验收点）。

## 可中断可续跑

已评估点可用 `export_points()` / `import_points()` 导出/导入 JSON。
续跑时把导入的点喂回 `optimize(..., history=...)`，命中缓存的点不会重算。

纯标准库、纯算法，不依赖 CATIA / Abaqus / DB / 第三方库。兼容 Python 3.8。
"""

import json
import math
import os
import random

try:
    from . import objectives
except ImportError:
    import objectives


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 默认预算三元组（最大评估次数 / 最大轮数 / 改进量阈值）
DEFAULT_BUDGET = {
    "max_evals": 60,
    "max_rounds": 12,
    "improvement_threshold": 1e-6,
}

#: 停止原因（优化结束的唯一合法出口，绝不返回含义不明的 None）
STOP_MAX_EVALS = "max_evals"          # 评估次数用尽
STOP_MAX_ROUNDS = "max_rounds"        # 轮数用尽
STOP_CONVERGED = "converged"          # 改进量低于阈值
STOP_NO_FEASIBLE = "no_feasible"      # 跑通了，但没有一个满足约束的设计
STOP_ALL_FAILED = "all_failed"        # 全部评估都技术故障（算不出来，不是设计不好）

STOP_REASONS = (STOP_MAX_EVALS, STOP_MAX_ROUNDS, STOP_CONVERGED,
                STOP_NO_FEASIBLE, STOP_ALL_FAILED)

STOP_REASON_TEXT = {
    STOP_MAX_EVALS: "评估次数达到上限",
    STOP_MAX_ROUNDS: "迭代轮数达到上限",
    STOP_CONVERGED: "目标改进量低于阈值，已收敛",
    STOP_NO_FEASIBLE: "未找到任何满足约束的可行设计",
    STOP_ALL_FAILED: ("全部设计点评估失败（求解/网格/参数等技术故障），"
                      "不是设计不达标；请先排查运行环境"),
}

#: 设计点的分组（与 objectives.sort_key 的 group 一一对应）
GROUP_FEASIBLE = 0
GROUP_INFEASIBLE = 1
GROUP_FAILED = 2

GROUP_NAMES = {
    GROUP_FEASIBLE: "feasible",
    GROUP_INFEASIBLE: "infeasible",
    GROUP_FAILED: "failed",
}

#: 连续变量步长收缩的下限（低于它就不再缩，改用离散邻域）
DEFAULT_STEP_MIN = 1e-3

#: 连续变量初始步长占范围的比例
DEFAULT_STEP_FRACTION = 0.25


class OptimizerError(ValueError):
    """优化配置不合法（变量声明有误、预算不可用等）。"""


# ---------------------------------------------------------------------------
# 变量声明（数据驱动）
# ---------------------------------------------------------------------------

def normalize_variables(variables):
    """
    归一化变量声明，返回新列表（不改调用方对象）。

    每个变量：
      name        : 变量名（必填、唯一）
      type        : "continuous" | "discrete"
      category    : "geometry" | "ply"（票22 口径；仅用于分组统计，本模块不解释它）
      min/max     : 边界（必填）
      step        : 离散步长（可选，默认 1）
      candidates  : 候选值列表（离散可选；给了就不看 step）
    """
    if not variables:
        raise OptimizerError("变量声明不能为空")
    if not isinstance(variables, (list, tuple)):
        raise OptimizerError("变量声明应为列表，实际 %r" % type(variables).__name__)

    out = []
    seen = set()
    for idx, raw in enumerate(variables):
        if not isinstance(raw, dict):
            raise OptimizerError("变量声明[%d] 应为 dict，实际 %r" % (idx, raw))
        name = raw.get("name")
        if not name:
            raise OptimizerError("变量声明[%d] 缺少 name" % idx)
        if name in seen:
            raise OptimizerError("变量名重复: %r" % (name,))
        seen.add(name)

        typ = str(raw.get("type") or "continuous").lower()
        if typ not in ("continuous", "discrete"):
            raise OptimizerError(
                "变量 %r 的 type 无法识别: %r（应为 continuous / discrete）"
                % (name, raw.get("type")))

        spec = {
            "name": name,
            "type": typ,
            "category": str(raw.get("category") or "geometry"),
        }

        if typ == "discrete" and raw.get("candidates"):
            cands = list(raw["candidates"])
            if not cands:
                raise OptimizerError("变量 %r 的 candidates 为空" % (name,))
            # 整数性：candidates 里每一项都必须是整数（层数/肋数不允许 6.37）
            norm_cands = []
            for c in cands:
                if isinstance(c, bool) or not isinstance(c, int):
                    raise OptimizerError(
                        "离散变量 %r 的候选值必须是整数，实际 %r" % (name, c))
                norm_cands.append(int(c))
            spec["candidates"] = sorted(set(norm_cands))
            spec["min"] = spec["candidates"][0]
            spec["max"] = spec["candidates"][-1]
            out.append(spec)
            continue

        for key in ("min", "max"):
            if key not in raw:
                raise OptimizerError("变量 %r 缺少 %s" % (name, key))
        lo = float(raw["min"])
        hi = float(raw["max"])
        if not hi > lo:
            raise OptimizerError("变量 %r 的 max 必须大于 min: [%r, %r]"
                                 % (name, lo, hi))
        spec["min"] = lo
        spec["max"] = hi

        if typ == "discrete":
            step = raw.get("step", 1)
            if isinstance(step, bool) or not isinstance(step, int):
                raise OptimizerError("离散变量 %r 的 step 必须是整数，实际 %r"
                                     % (name, step))
            if step <= 0:
                raise OptimizerError("离散变量 %r 的 step 必须为正" % (name,))
            spec["step"] = int(step)
            # 边界与步长要能整除对齐，否则取不到上界
            if (int(math.floor(hi)) - int(math.ceil(lo))) % step != 0:
                spec["max"] = float(int(math.ceil(lo))
                                    + ((int(math.floor(hi)) - int(math.ceil(lo)))
                                       // step) * step)

        out.append(spec)
    return out


def _discrete_values(spec):
    """离散变量的全部合法取值（有序）。"""
    if spec.get("candidates"):
        return list(spec["candidates"])
    step = int(spec.get("step", 1))
    lo = int(math.ceil(float(spec["min"])))
    hi = int(math.floor(float(spec["max"])))
    return list(range(lo, hi + 1, step))


def _var_by_name(specs):
    return dict((s["name"], s) for s in specs)


# ---------------------------------------------------------------------------
# 设计向量的规范化与去重键
# ---------------------------------------------------------------------------

def normalize_vector(vector, specs):
    """
    把设计向量夹进声明范围、离散化到合法值（**守住整数性**）。

    返回新的 dict。越界值被夹到最近的合法值 —— 但调用方应先用 `in_envelope()`
    判断，超出参数化包络属于"该换模板"，不是靠搜索穿越（见票22）。
    """
    by_name = _var_by_name(specs)
    out = {}
    for name, spec in by_name.items():
        if name not in vector:
            raise OptimizerError("设计向量缺少变量 %r" % (name,))
        raw = vector[name]
        if spec["type"] == "discrete":
            values = _discrete_values(spec)
            iv = int(round(float(raw)))
            # 夹进范围
            iv = max(values[0], min(values[-1], iv))
            if iv in values:
                out[name] = iv
            else:
                # 落到最近的合法值（等距时的确定性：取较小者）
                out[name] = min(values, key=lambda v: (abs(v - iv), v))
        else:
            lo, hi = spec["min"], spec["max"]
            v = float(raw)
            out[name] = max(lo, min(hi, v))
    return out


def vector_key(vector):
    """
    设计向量的去重键（字符串，可 JSON 序列化、可跨进程比较）。

    浮点用 `%.12g`：避免 0.1+0.2 之类的末位噪声把同一个点当成两个点。
    """
    parts = []
    for name in sorted(vector.keys()):
        parts.append("%s=%s" % (name, _num_key(vector[name])))
    return "|".join(parts)


def _num_key(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return "%d" % value
    try:
        return "%.12g" % float(value)
    except (TypeError, ValueError):
        return str(value)


def in_envelope(vector, specs):
    """
    向量是否全部落在变量声明范围内（参数化包络守卫）。

    返回 (ok, violations)：violations 是 [{'variable','value','min','max'}]。
    离散变量还要求取值在合法值集合里。
    """
    by_name = _var_by_name(specs)
    bad = []
    for name, spec in by_name.items():
        if name not in vector:
            bad.append({"variable": name, "reason": "missing"})
            continue
        value = vector[name]
        if spec["type"] == "discrete":
            values = _discrete_values(spec)
            iv = int(round(float(value)))
            if iv not in values:
                bad.append({"variable": name, "value": value,
                            "reason": "not_in_candidates",
                            "allowed": values[:8]})
            continue
        try:
            fv = float(value)
        except (TypeError, ValueError):
            bad.append({"variable": name, "value": value,
                        "reason": "not_numeric"})
            continue
        if fv < spec["min"] - 1e-12 or fv > spec["max"] + 1e-12:
            bad.append({"variable": name, "value": fv,
                        "min": spec["min"], "max": spec["max"],
                        "reason": "out_of_range"})
    return (len(bad) == 0), bad


# ---------------------------------------------------------------------------
# DOE 采样
# ---------------------------------------------------------------------------

def doe_sample(variables, n_samples=8, seed=0):
    """
    拉丁超立方（分层）DOE 采样。

    variables : 变量声明（见 `normalize_variables`）
    n_samples : 样本数
    seed      : 随机种子（**同种子同结果**）

    连续变量：把 [min, max] 分 n 层，每层随机取一点（LHS），保证覆盖；
    离散变量：在合法取值集合里随机取（无放回地尽量铺开）。
    返回 [vector]，顺序确定。
    """
    specs = normalize_variables(variables)
    try:
        n = int(n_samples)
    except (TypeError, ValueError):
        raise OptimizerError("n_samples 必须是整数，实际 %r" % (n_samples,))
    if n <= 0:
        raise OptimizerError("n_samples 必须为正，实际 %d" % n)

    rng = random.Random(seed)

    # 每个变量一条"分层索引"序列；用同一个 rng 顺序抽，保证确定性
    columns = {}
    for spec in specs:
        if spec["type"] == "discrete":
            values = _discrete_values(spec)
            # 样本数不超过取值数时尽量不重复；超过则允许重复（必然是撞点）
            if n <= len(values):
                picked = rng.sample(values, n)
                columns[spec["name"]] = picked
            else:
                # 先覆盖一遍全部合法值，再随机补足
                picked = list(values)
                while len(picked) < n:
                    picked.append(rng.choice(values))
                rng.shuffle(picked)
                columns[spec["name"]] = picked
        else:
            lo, hi = spec["min"], spec["max"]
            width = (hi - lo) / float(n)
            col = []
            for k in range(n):
                u = rng.random()
                col.append(lo + width * (k + u))
            columns[spec["name"]] = col

    samples = []
    for i in range(n):
        vector = {}
        for spec in specs:
            vector[spec["name"]] = columns[spec["name"]][i]
        samples.append(normalize_vector(vector, specs))
    return samples


# ---------------------------------------------------------------------------
# 邻域（不假设可微：离散邻域 + 连续步长收缩）
# ---------------------------------------------------------------------------

def _initial_steps(specs, fraction=DEFAULT_STEP_FRACTION):
    """连续变量的初始试探步长（范围的一个比例）。"""
    steps = {}
    for spec in specs:
        if spec["type"] == "continuous":
            span = spec["max"] - spec["min"]
            steps[spec["name"]] = max(span * float(fraction), DEFAULT_STEP_MIN)
    return steps


def discrete_neighbors(vector, specs):
    """
    离散变量的邻域：每个离散变量各自 ±1 个合法值，其余不动。

    不在候选集合里插值 —— 肋数没有 6.5 根。返回 [vector]（已规范化、已去重）。
    """
    out = []
    base = normalize_vector(vector, specs)
    for spec in specs:
        if spec["type"] != "discrete":
            continue
        values = _discrete_values(spec)
        name = spec["name"]
        try:
            idx = values.index(base[name])
        except ValueError:
            continue
        for delta in (-1, 1):
            j = idx + delta
            if 0 <= j < len(values):
                nv = dict(base)
                nv[name] = values[j]
                out.append(normalize_vector(nv, specs))
    return _dedupe_vectors(out)


def continuous_neighbors(vector, specs, steps):
    """连续变量的邻域：每个连续变量按当前步长 ±，其余不动。"""
    out = []
    base = normalize_vector(vector, specs)
    for spec in specs:
        if spec["type"] != "continuous":
            continue
        name = spec["name"]
        step = steps.get(name, DEFAULT_STEP_MIN)
        for delta in (-1.0, 1.0):
            nv = dict(base)
            nv[name] = base[name] + delta * step
            out.append(normalize_vector(nv, specs))
    return _dedupe_vectors(out)


def _dedupe_vectors(vectors):
    """按 vector_key 去重，保持顺序。"""
    seen = set()
    out = []
    for v in vectors:
        key = vector_key(v)
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


# ---------------------------------------------------------------------------
# 评估结果的读取（一律走 objectives 的口径）
# ---------------------------------------------------------------------------

def _group_of(ev):
    """设计点分组：0 可行 / 1 跑通但不可行 / 2 技术故障（= objectives.sort_key）"""
    return objectives.sort_key(ev)[0]


def _value_of(ev):
    """目标值；技术故障会拿到 +inf（使其永不参与最优比较）。"""
    return objectives.sort_key(ev)[1]


def _is_better(candidate_ev, current_ev):
    """候选点是否严格优于当前点（按 objectives.sort_key 的三分组语义）。"""
    if current_ev is None:
        return True
    return objectives.sort_key(candidate_ev) < objectives.sort_key(current_ev)


# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------

def normalize_budget(budget=None):
    """归一化预算三元组；未给键用 `DEFAULT_BUDGET`。"""
    out = dict(DEFAULT_BUDGET)
    if budget:
        if not isinstance(budget, dict):
            raise OptimizerError("budget 必须是 dict，实际 %r"
                                 % type(budget).__name__)
        for key, value in budget.items():
            out[key] = value

    for key, floor in (("max_evals", 1), ("max_rounds", 1)):
        try:
            iv = int(out[key])
        except (TypeError, ValueError):
            raise OptimizerError("budget.%s 必须是整数，实际 %r"
                                 % (key, out.get(key)))
        if iv < floor:
            raise OptimizerError("budget.%s 必须 >= %d，实际 %d"
                                 % (key, floor, iv))
        out[key] = iv

    try:
        thr = float(out["improvement_threshold"])
    except (TypeError, ValueError):
        raise OptimizerError("budget.improvement_threshold 必须是数值，实际 %r"
                             % (out.get("improvement_threshold"),))
    if thr < 0.0:
        raise OptimizerError("budget.improvement_threshold 不能为负，实际 %r" % thr)
    out["improvement_threshold"] = thr
    return out


# ---------------------------------------------------------------------------
# 设计点记录（导出/导入的载体）
# ---------------------------------------------------------------------------

def point_record(vector, evaluation, source="doe"):
    """
    一条已评估点的记录（可 JSON 序列化，供导出/导入/汇总）。

    vector     : 设计向量
    evaluation : objectives.evaluate() 的结果
    source     : 该点从哪来（"doe" / "local"）
    """
    ev = evaluation or {}
    err = ev.get("error")
    group = _group_of(ev)
    return {
        "vector": dict(vector),
        "key": vector_key(vector),
        "source": source,
        "group": group,
        "group_name": GROUP_NAMES.get(group, "unknown"),
        "ok": ev.get("ok"),
        "feasible": ev.get("feasible"),
        "value": (ev.get("objective") or {}).get("value"),
        "error": err,
        "evaluation": ev,
    }


def export_points(points):
    """把已评估点导出成可存档的 JSON 结构（版本化，便于将来演进）。"""
    return {
        "schema": "cae-optimizer-points/1",
        "n_points": len(points),
        "points": [dict(p) for p in points],
    }


def import_points(payload):
    """
    从 `export_points()` 的产物恢复已评估点列表。

    容错：缺 evaluation 的记录（比如只存了摘要）按摘要重建一个最小评估结果，
    续跑时它的目标值/分组仍可用，只是拿不到 breakdown 明细。
    """
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise OptimizerError("导入的评估点必须是 dict（export_points 的产物）")
    raw = payload.get("points")
    if raw is None:
        raise OptimizerError("导入数据缺少 points 字段")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rec = dict(item)
        if not rec.get("evaluation"):
            rec["evaluation"] = {
                "ok": rec.get("ok"),
                "feasible": rec.get("feasible"),
                "error": rec.get("error"),
                "objective": {"value": rec.get("value"), "name": "mass",
                              "direction": "minimize"},
            }
            rec["evaluation"]["constraints"] = None
        rec.setdefault("key", vector_key(rec.get("vector") or {}))
        rec.setdefault("group", _group_of(rec["evaluation"]))
        rec.setdefault("group_name",
                       GROUP_NAMES.get(rec["group"], "unknown"))
        out.append(rec)
    return out


def write_points(path, points):
    """把已评估点写成 JSON 文件（中断后可从磁盘续跑）；返回路径。"""
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    payload = export_points(points)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True, indent=2)
    return path


def read_points(path):
    """从磁盘读回已评估点（`write_points` 的逆操作）。"""
    with open(path, "r", encoding="utf-8") as fh:
        return import_points(json.load(fh))


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

def optimize(variables, evaluate, budget=None, seed=0, history=None,
             step_min=DEFAULT_STEP_MIN, step_fraction=DEFAULT_STEP_FRACTION):
    """
    优化主入口：DOE 起步 -> 邻域/步长收缩 -> 挑可行最优。

    variables : 变量声明（数据驱动，见 `normalize_variables`）
    evaluate  : 注入的评估回调 `evaluate(vector) -> objectives 评估结果`
                **优化器不知道 Study/Abaqus**，这是本模块可秒级单测的原因
    budget    : {'max_evals', 'max_rounds', 'improvement_threshold'}
    seed      : 随机种子（同种子同结果）
    history   : 可选，之前已评估的点（续跑；命中缓存的点不会重算）
    step_min  : 连续变量步长的收缩下限
    step_fraction : 连续变量初始步长占范围的比例

    返回 dict：
      stop_reason   : 停止原因（`STOP_REASONS` 之一，必有值，绝不返回 None）
      stop_text     : 停止原因的可读说明
      best          : 最优**可行**设计（vector + 评估摘要）；无可行点时为 None
      best_point    : 最优可行点的完整记录（None 时说明原因见 stop_reason）
      feasible      : 是否找到了可行点
      groups        : {'feasible': [...], 'infeasible': [...], 'failed': [...]}
      points        : 全部已评估点（按评估顺序）
      n_evals       : **实际调用 evaluate 的次数**（不含缓存命中）
      n_cached      : 缓存命中次数
      n_rounds      : 实际迭代轮数
      budget        : 归一后的预算
      variables     : 归一后的变量声明
      seed          : 本次种子

    **绝不返回含义不明的 None**：没有可行点时 `best=None` 且
    `stop_reason=no_feasible`、`stop_text` 给出可读原因。
    """
    specs = normalize_variables(variables)
    if not callable(evaluate):
        raise OptimizerError("evaluate 必须是可调用对象（注入的评估回调）")
    bud = normalize_budget(budget)

    n_evals = [0]          # 实际调用次数（突变在闭包里）
    n_cached = [0]
    cache = {}             # key -> point record（含缓存的评估结果）
    order = []             # 本次新评估的点（按评估顺序）
    known = []             # 全部已评估点（history + 本次新增，按评估顺序）

    for rec in (history or []):
        key = rec.get("key") or vector_key(rec.get("vector") or {})
        cache[key] = rec
        known.append(rec)

    def _evaluate_one(vector, source):
        """评估一个点（命中缓存则不调用回调）。返回 point record。"""
        key = vector_key(vector)
        if key in cache:
            n_cached[0] += 1
            return cache[key]
        n_evals[0] += 1
        ev = evaluate(dict(vector))
        rec = point_record(vector, ev, source=source)
        cache[key] = rec
        order.append(rec)
        known.append(rec)
        return rec

    def _budget_exhausted():
        return n_evals[0] >= bud["max_evals"]

    # ---- 1) DOE 起步 ----
    n_doe = max(len(specs) + 1, min(bud["max_evals"], 8))
    doe = doe_sample(specs, n_samples=n_doe, seed=seed)
    for vector in doe:
        if _budget_exhausted():
            break
        _evaluate_one(vector, "doe")

    best_rec = _pick_best(known)
    steps = _initial_steps(specs, step_fraction)
    rounds = 0
    prev_best_value = None
    stall_rounds = 0
    stop_reason = None

    # ---- 2) 邻域搜索 + 步长收缩 ----
    while True:
        if _budget_exhausted():
            stop_reason = STOP_MAX_EVALS
            break
        if rounds >= bud["max_rounds"]:
            stop_reason = STOP_MAX_ROUNDS
            break

        rounds += 1
        improved = False

        # 中心点：当前最优**可行**点；还没有可行点时退到最近评估的点，
        # 否则搜索会卡死在第一个不可行点上、永远找不到可行域。
        center_rec = best_rec or (known[-1] if known else None)
        if center_rec is None:
            stop_reason = STOP_NO_FEASIBLE
            break
        center = center_rec["vector"]

        candidates = (discrete_neighbors(center, specs)
                      + continuous_neighbors(center, specs, steps))
        if not candidates:
            # 没有邻域可走（变量全是单点候选）：只能收敛
            stop_reason = STOP_CONVERGED
            break

        for vector in candidates:
            if _budget_exhausted():
                break
            rec = _evaluate_one(vector, "local")
            if _is_better(rec["evaluation"],
                          best_rec["evaluation"] if best_rec else None):
                best_rec = rec
                improved = True

        if improved:
            stall_rounds = 0
            new_value = best_rec["value"] if best_rec else None
            # 改进量阈值：相对上一轮最优的**目标改进**小于阈值即认为收敛。
            # 阶梯目标上这等价于"再怎么挪也就这么多了"，不会在等值平台上乱漂。
            if (prev_best_value is not None and new_value is not None
                    and (prev_best_value - new_value)
                    <= bud["improvement_threshold"]):
                prev_best_value = new_value
                stop_reason = STOP_CONVERGED
                break
            prev_best_value = new_value
        else:
            stall_rounds += 1
            # 无改进 -> 连续变量步长减半（离散变量没有"步长"可缩，只能走邻域）
            shrunk = False
            for name, step in list(steps.items()):
                new_step = step * 0.5
                if new_step >= step_min:
                    steps[name] = new_step
                    shrunk = True
                else:
                    steps[name] = step_min

            discrete_movable = any(
                s["type"] == "discrete" and len(_discrete_values(s)) > 1
                for s in specs)
            if shrunk:
                continue           # 步长刚缩小，再给一轮机会
            if not discrete_movable:
                # 连续步长到下限、没有离散变量可挪 -> 已收敛
                stop_reason = STOP_CONVERGED
                break
            if stall_rounds >= 2:
                # 纯离散变量（或步长已到底）：连续两轮无改进即认为邻域走遍
                stop_reason = STOP_CONVERGED
                break

        if _budget_exhausted():
            stop_reason = STOP_MAX_EVALS
            break

    # ---- 3) 结算 ----
    if stop_reason is None:
        stop_reason = STOP_MAX_ROUNDS

    best_rec = _pick_best(known)
    feasible_points = [r for r in known if r["group"] == GROUP_FEASIBLE]
    if not feasible_points:
        # 一个可行点都没找到：明确报告，绝不给一个含义不明的"最优"。
        # 但**必须区分"设计都不达标"与"算都算不出来"** —— 两者对工程师的
        # 含义完全不同：前者要改设计，后者要查环境（求解器/网格/参数）。
        # 全都技术故障时报 no_feasible 会把人引向错误方向（spec 用户故事 20）。
        grouped = _grouped(known)
        n_failed = len(grouped.get("failed") or [])
        if known and n_failed == len(known):
            stop_reason = STOP_ALL_FAILED
        else:
            stop_reason = STOP_NO_FEASIBLE
        best_rec = None

    return {
        "stop_reason": stop_reason,
        "stop_text": STOP_REASON_TEXT.get(stop_reason, stop_reason),
        "best": ({"vector": best_rec["vector"], "value": best_rec["value"],
                  "key": best_rec["key"]} if best_rec else None),
        "best_point": best_rec,
        "feasible": bool(best_rec),
        # 分组覆盖全部已评估点（含续跑导入的 history），否则续跑的结论会"消失"
        "groups": _grouped(known),
        "points": order,
        "history": list(history or []),
        "n_evals": n_evals[0],
        "n_cached": n_cached[0],
        "n_rounds": rounds,
        "budget": bud,
        "variables": specs,
        "seed": seed,
    }


def _pick_best(points):
    """挑"最优可行点"：只在 group=0 里比，且按 objectives.sort_key 排序。"""
    feasible = [p for p in points if p["group"] == GROUP_FEASIBLE]
    if not feasible:
        return None
    return min(feasible, key=lambda p: objectives.sort_key(p["evaluation"]))


def _grouped(points):
    """按三类分组（可行 / 不可行 / 技术故障）。"""
    out = {"feasible": [], "infeasible": [], "failed": []}
    for p in points:
        name = GROUP_NAMES.get(p["group"], "failed")
        out[name].append(p)
    return out


def summarize(result):
    """人类可读摘要（日志 / 报告 / UI 共用）。"""
    if not result:
        return "优化: 无结果"
    groups = result.get("groups") or {}
    best = result.get("best") or {}
    parts = ["优化: %s（%s）" % (result.get("stop_reason"),
                                result.get("stop_text"))]
    if best:
        parts.append("最优质量 %s kg" % best.get("value"))
    else:
        parts.append("未找到可行设计")
    parts.append("评估 %d 次（缓存命中 %d）" % (result.get("n_evals", 0),
                                              result.get("n_cached", 0)))
    parts.append("可行 %d / 不可行 %d / 故障 %d"
                 % (len(groups.get("feasible") or []),
                    len(groups.get("infeasible") or []),
                    len(groups.get("failed") or [])))
    return "，".join(parts)
