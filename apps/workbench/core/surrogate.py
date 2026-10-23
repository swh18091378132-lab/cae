# -*- coding: utf-8 -*-
"""
surrogate.py -- 代理模型快筛（票 24）

## 这张票的边界（ADR-0002 硬约束）

代理模型是 **L1 辅助**：它只做两件事——

  1. **排序**：在候选设计里猜"哪个可能更轻"
  2. **建议下一轮看哪里**：把预测最优的若干点交给真实评估

它**绝不宣告任何合格判定**。"这个设计过不过"永远由 `strength_check` 的
确定性 FI/MS 计算给出（ADR-0007），代理模型连这个位置都没有——本模块所有
返回结构的字段里**不存在** `feasible` / `all_pass` / `pass` / `verdict` /
`ms` / `fi` 之类的键。这不是靠注释约定，而是靠数据结构本身：调用方拿不到
代理的"合格结论"，因为它压根不存在。

## 为什么必须报误差

一个不报误差的代理模型是危险的：它看起来能排序，但错得离谱时也没人知道。
所以本模块**每拟合一次就做留一交叉验证（LOO）**，给出 MAE / RMSE / 最大绝对
误差，并根据可配置的阈值判定"这个模型这批数据上靠不靠谱"。不可靠就返回
`available=False`，调用方据此**退化为"不排序"**（而不是用一个坏模型乱指路），
优化闭环照常跑完（这是"降级不阻塞"）。

LOO 的每个折都是**可独立复算**的：留出一个样本，其余样本重拟合，预测留出者，
误差定义即普通的 MAE/RMSE。测试里自己按这个定义重算一遍对照（防止我们
在实现里悄悄用了别的口径）。

## 模型形式（纯标准库，无 numpy）

核心是**标准化后的线性最小二乘**：

    ẑ = w0 + Σ wi · ξi        其中 ξi = (xi - mean_i) / scale_i

    scale_i 取该变量的**取值范围**（离散变量取候选跨度），于是
    几何变量（c_root ~ 1..3）与铺层变量（n_plies ~ 4..8）的贡献可比，
    不会让数值大的变量主导拟合。

为什么要缩放：`c_root` 数值上约 2，而 `n_plies` 约 6，且量纲完全不同
（米 vs 层数）。不缩放时，任何基于原始数值的距离/正则都会偏向数值大的变量。

最小二乘用**正规方程 + 高斯消元**求解（变量维度通常 < 10，够用且确定）。
正规方程在病态数据上会有数值风险，故加了**对角 Tikhonov 正则**（ridge）：
既稳定求解，也抑制过拟合，且正则强度可配置（`ridge`）。

## 输入/输出形态

输入点就是 `optimizer.point_record()` 的产物（`vector` / `value` / `group`）——
本模块不重新定义"已评估点"。

**只用跑通的点拟合**（`group` 为可行或不可行、`value` 非空）：
技术故障点根本没有可信的目标值，拿它训练等于把噪声当信号。

输出：
    fit(...)      -> 模型 dict（可用时含 predictors/quality/...；不可用时含 reason）
    suggest(...)  -> 排序 + 建议（字段里没有任何合格判定）
    predict(...)  -> 单个向量的预测值

兼容 Python 3.8，仅标准库。
"""

import json
import math
import os

#: 结果的来源标记：告诉汇总视图"这条是代理建议，不是实测"
SOURCE_SURROGATE = "surrogate"

#: 可用于拟合的最低样本数下限（即使变量很少也不许少于它 —— 3 个点拟合不出模型）
MIN_SAMPLES_FLOOR = 4

#: 默认误差阈值（MAE 相对目标值跨度；超过即判不可用）
DEFAULT_MAX_MAE_RATIO = 0.15      # MAE 超过目标跨度 15% -> 不可信
DEFAULT_MAX_RMSE_RATIO = 0.25     # RMSE 超过目标跨度 25% -> 不可信

#: 默认 ridge 正则强度（相对设计矩阵尺度，纯数值稳定用）
DEFAULT_RIDGE = 1e-8

#: 禁止出现在任何返回结构里的"合格判定"字段名（ADR-0002）
FORBIDDEN_VERDICT_KEYS = (
    "feasible", "all_pass", "allpass", "pass", "passed", "verdict",
    "qualified", "conclusion", "ok_design", "strength_pass",
    "fi", "ms", "max_fi", "min_ms", "margin",
)


class SurrogateError(ValueError):
    """拟合/预测的输入不合法（变量声明有误、点结构不对等）。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _key(vector):
    """设计向量的稳定键（与 optimizer.vector_key 同口径）。"""
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


def _var_scale(spec):
    """变量的数值尺度（用于标准化）：连续取范围，离散取候选跨度。"""
    if spec.get("candidates"):
        cands = [float(c) for c in spec["candidates"]]
        span = max(cands) - min(cands)
        center = sum(cands) / float(len(cands))
        return (center, span if span > 0 else 1.0)
    lo = float(spec["min"])
    hi = float(spec["max"])
    span = hi - lo
    return ((lo + hi) / 2.0, span if span > 0 else 1.0)


def normalize_variables(variables, points=None):
    """
    归一化变量声明。

    可以从调用方声明来，也可以从点上**推断**（只给点、不给声明时）：
    推断的规则是——该字段在所有点上都是整数 -> 离散；否则连续。
    推断不可靠（无法知道真实范围），所以优先用显式声明。
    """
    if variables:
        out = []
        seen = set()
        for idx, raw in enumerate(variables):
            if not isinstance(raw, dict) or not raw.get("name"):
                raise SurrogateError("变量声明[%d] 应有 name" % idx)
            name = raw["name"]
            if name in seen:
                raise SurrogateError("变量名重复: %r" % (name,))
            seen.add(name)
            spec = {"name": name,
                    "type": str(raw.get("type") or "continuous").lower(),
                    "category": str(raw.get("category") or "geometry")}
            if raw.get("candidates"):
                spec["candidates"] = [int(c) for c in raw["candidates"]]
                spec["min"] = min(spec["candidates"])
                spec["max"] = max(spec["candidates"])
            else:
                if "min" not in raw or "max" not in raw:
                    raise SurrogateError("变量 %r 缺少 min/max" % (name,))
                spec["min"] = float(raw["min"])
                spec["max"] = float(raw["max"])
            out.append(spec)
        return out

    # 从点推断
    if not points:
        raise SurrogateError("既没有变量声明也没有样本点，无法确定模型输入")
    names = sorted(points[0].get("vector", {}).keys())
    if not names:
        raise SurrogateError("样本点缺少 vector，无法推断变量")
    out = []
    for name in names:
        values = [p["vector"][name] for p in points if name in p.get("vector", {})]
        if not values:
            continue
        all_int = all(isinstance(v, int) and not isinstance(v, bool)
                      for v in values)
        out.append({
            "name": name,
            "type": "discrete" if all_int else "continuous",
            "category": "geometry",
            "min": float(min(values)),
            "max": float(max(values)),
        })
    return out


def usable_points(points):
    """
    挑出**可用于拟合**的点：跑通且目标值非空。

    技术故障点（group=2 / ok=False / value=None）被排除 —— 它没有可信目标值，
    拿它训练等于把噪声当信号。返回 (usable, n_excluded)。
    """
    ok_points = []
    n_excluded = 0
    for p in points or []:
        if not isinstance(p, dict):
            n_excluded += 1
            continue
        if p.get("group") == 2 or p.get("ok") is False:
            n_excluded += 1
            continue
        if p.get("value") is None:
            n_excluded += 1
            continue
        if not p.get("vector"):
            n_excluded += 1
            continue
        ok_points.append(p)
    return ok_points, n_excluded


def min_samples_for(variables):
    """
    拟合所需的最少样本数。

    线性模型有 (n_vars + 1) 个系数。样本数必须**严格大于**系数个数，
    否则正规方程欠定（能穿过的解有无穷多个，"拟合成功"是假象）。
    所以取 max(系数个数 + 1, 硬性下限 MIN_SAMPLES_FLOOR)。
    """
    return max(len(variables) + 2, MIN_SAMPLES_FLOOR)


# ---------------------------------------------------------------------------
# 线性代数（纯标准库）
# ---------------------------------------------------------------------------

def _solve_normal_equations(design, targets, ridge=DEFAULT_RIDGE):
    """
    解 ridge 正则最小二乘： (XᵀX + λI) w = Xᵀy

    design : [[1.0, x1, x2, ...], ...]（已含截距列）
    targets: [y, ...]
    返回系数列表（长度 = design[0] 的长度）；奇异时返回 None。
    """
    n = len(design)
    if n == 0:
        return None
    m = len(design[0])

    # A = XᵀX, b = Xᵀy
    a = [[0.0] * m for _ in range(m)]
    b = [0.0] * m
    for row, y in zip(design, targets):
        for i in range(m):
            vi = row[i]
            b[i] += vi * y
            for j in range(m):
                a[i][j] += vi * row[j]

    # ridge：只对斜率项加（截距不加，避免整体偏移被压向 0）
    for i in range(1, m):
        a[i][i] += ridge

    return _gaussian_solve(a, b)


def _gaussian_solve(a, b):
    """带部分主元的高斯消元；奇异返回 None。"""
    n = len(b)
    m = [list(row) + [b[i]] for i, row in enumerate(a)]

    for col in range(n):
        # 部分主元：找当前列绝对值最大的行
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-14:
            return None
        if pivot != col:
            m[col], m[pivot] = m[pivot], m[col]

        pv = m[col][col]
        for r in range(col + 1, n):
            factor = m[r][col] / pv
            if factor == 0.0:
                continue
            for c in range(col, n + 1):
                m[r][c] -= factor * m[col][c]

    # 回代
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = m[i][n]
        for j in range(i + 1, n):
            s -= m[i][j] * x[j]
        x[i] = s / m[i][i]
    return x


def _feature_row(vector, specs, scales):
    """把一个设计向量换成标准化后的特征行（含截距）。"""
    row = [1.0]
    for spec in specs:
        name = spec["name"]
        if name not in vector:
            raise SurrogateError("设计向量缺少变量 %r" % (name,))
        center, span = scales[name]
        row.append((float(vector[name]) - center) / span)
    return row


def _fit_coefficients(samples, specs, scales, ridge):
    """在给定样本上拟合系数；样本不足/奇异返回 None。"""
    design = []
    targets = []
    for p in samples:
        design.append(_feature_row(p["vector"], specs, scales))
        targets.append(float(p["value"]))
    return _solve_normal_equations(design, targets, ridge)


def _predict_with(coef, vector, specs, scales):
    row = _feature_row(vector, specs, scales)
    return sum(c * v for c, v in zip(coef, row))


# ---------------------------------------------------------------------------
# 拟合 + 留一交叉验证
# ---------------------------------------------------------------------------

def _empty_result(n_samples=0, n_variables=0, min_samples=0, reason=None,
                  n_excluded=0):
    """
    "不可用"的统一形态。

    照样带齐字段（调用方不必判空），但 `ranking` 为空、误差为 None ——
    没有模型就不许报误差，更不许给排序（降级为"不排序"）。
    """
    return {
        "available": False,
        "degraded": True,
        "source": SOURCE_SURROGATE,
        "reason": reason or "样本不足，无法拟合",
        "n_samples": n_samples,
        "n_excluded": n_excluded,
        "n_variables": n_variables,
        "min_samples": min_samples,
        "variables": [],
        "scaling": {},
        "quality": {"mae": None, "rmse": None, "max_abs": None,
                    "mae_ratio": None, "rmse_ratio": None,
                    "target_span": None, "n_folds": 0},
        "thresholds": {},
        "ranking": [],
    }


def fit(points, variables=None, max_mae_ratio=DEFAULT_MAX_MAE_RATIO,
        max_rmse_ratio=DEFAULT_MAX_RMSE_RATIO, ridge=DEFAULT_RIDGE):
    """
    在已评估点上拟合代理模型，并用留一交叉验证给出误差。

    points    : [optimizer.point_record 形态]（`vector` 必填、`value` 用于训练）
    variables : 变量声明；None 时从点上**推断**（见 `normalize_variables`）
    max_mae_ratio / max_rmse_ratio : 误差阈值（相对目标值跨度）；超了判不可用
    ridge     : 正则强度（数值稳定用）

    返回 dict：
      available    : 模型能否用于排序（样本够 + 误差达标才 True）
      degraded     : True 表示调用方应退化为"不排序"（available 的反面）
      source       : 恒为 "surrogate"（区分代理建议与实测）
      reason       : available=False 时的可读原因
      n_samples / n_excluded / n_variables / min_samples
      variables / scaling : 模型输入与缩放口径（可复算）
      quality      : {mae, rmse, max_abs, mae_ratio, rmse_ratio,
                      target_span, n_folds}
      thresholds   : 本次判定用的阈值
      ranking      : 恒为空（排序由 `suggest` 生成；拟合阶段不排序）

    **返回结构里没有任何合格判定字段**（见模块 docstring 与
    `FORBIDDEN_VERDICT_KEYS`）。
    """
    usable, n_excluded = usable_points(points)
    specs = normalize_variables(variables, usable or points)
    min_s = min_samples_for(specs)

    if len(usable) < min_s:
        return _empty_result(
            n_samples=len(usable), n_variables=len(specs), min_samples=min_s,
            n_excluded=n_excluded,
            reason="样本不足：可用样本 %d 个（已排除技术故障点 %d 个），"
                   "线性模型需要至少 %d 个样本（%d 个变量 + 1 个截距，"
                   "并要求样本数严格大于系数个数）"
                   % (len(usable), n_excluded, min_s, len(specs)))

    # 目标值必须全是有限数
    for p in usable:
        v = p["value"]
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return _empty_result(
                n_samples=len(usable), n_variables=len(specs),
                min_samples=min_s, n_excluded=n_excluded,
                reason="样本 %r 的目标值不是数值: %r" % (p.get("key"), v))
        if not (v == v) or v in (float("inf"), float("-inf")):
            return _empty_result(
                n_samples=len(usable), n_variables=len(specs),
                min_samples=min_s, n_excluded=n_excluded,
                reason="样本 %r 的目标值不是有限数: %r" % (p.get("key"), v))

    scales = dict((s["name"], _var_scale(s)) for s in specs)

    # ---- 全样本拟合（最终模型） ----
    coef = _fit_coefficients(usable, specs, scales, ridge)
    if coef is None:
        return _empty_result(
            n_samples=len(usable), n_variables=len(specs), min_samples=min_s,
            n_excluded=n_excluded,
            reason="设计矩阵奇异：样本在设计空间里线性相关（同一点重复或"
                   "某些变量在全部样本上取值相同），无法唯一确定系数")

    # ---- 留一交叉验证 ----
    loo_errors = []
    for i in range(len(usable)):
        train = usable[:i] + usable[i + 1:]
        c = _fit_coefficients(train, specs, scales, ridge)
        if c is None:
            # 某折拟合失败：不静默当 0 误差，直接判这批数据不可拟合
            return _empty_result(
                n_samples=len(usable), n_variables=len(specs),
                min_samples=min_s, n_excluded=n_excluded,
                reason="留一交叉验证第 %d 折无法拟合（样本在设计空间里"
                       "线性相关），模型不可信" % (i + 1))
        pred = _predict_with(c, usable[i]["vector"], specs, scales)
        loo_errors.append(pred - float(usable[i]["value"]))

    n_folds = len(loo_errors)
    abs_err = [abs(e) for e in loo_errors]
    mae = sum(abs_err) / float(n_folds)
    rmse = math.sqrt(sum(e * e for e in loo_errors) / float(n_folds))
    max_abs = max(abs_err)

    target_values = [float(p["value"]) for p in usable]
    target_span = max(target_values) - min(target_values)
    if target_span <= 0.0:
        # 目标在所有样本上相同：任何"排序"都没有信息量，且相对误差无定义
        return _empty_result(
            n_samples=len(usable), n_variables=len(specs), min_samples=min_s,
            n_excluded=n_excluded,
            reason="目标值在全部样本上相同（跨度 0），无法定义相对误差、"
                   "排序也没有信息量")

    mae_ratio = mae / target_span
    rmse_ratio = rmse / target_span

    quality = {
        "mae": mae,
        "rmse": rmse,
        "max_abs": max_abs,
        "mae_ratio": mae_ratio,
        "rmse_ratio": rmse_ratio,
        "target_span": target_span,
        "n_folds": n_folds,
    }
    thresholds = {"max_mae_ratio": float(max_mae_ratio),
                  "max_rmse_ratio": float(max_rmse_ratio)}

    result = {
        "available": True,
        "degraded": False,
        "source": SOURCE_SURROGATE,
        "reason": None,
        "n_samples": len(usable),
        "n_excluded": n_excluded,
        "n_variables": len(specs),
        "min_samples": min_s,
        "variables": specs,
        "scaling": dict((name, {"center": c, "span": s})
                        for name, (c, s) in scales.items()),
        "coefficients": list(coef),
        "quality": quality,
        "thresholds": thresholds,
        "ranking": [],
    }

    # ---- 误差不达标 -> 降级（不是抛异常：闭环必须能继续） ----
    problems = []
    if mae_ratio > float(max_mae_ratio):
        problems.append("MAE 占目标跨度 %.1f%% > 阈值 %.1f%%"
                        % (mae_ratio * 100.0, float(max_mae_ratio) * 100.0))
    if rmse_ratio > float(max_rmse_ratio):
        problems.append("RMSE 占目标跨度 %.1f%% > 阈值 %.1f%%"
                        % (rmse_ratio * 100.0, float(max_rmse_ratio) * 100.0))
    if problems:
        result["available"] = False
        result["degraded"] = True
        result["reason"] = ("代理模型误差超标（%s）：这批数据上不可信，"
                            "调用方应退化为不排序（流程照常继续）"
                            % "；".join(problems))
    return result


# ---------------------------------------------------------------------------
# 预测 / 排序 / 建议
# ---------------------------------------------------------------------------

def predict(model, vector):
    """
    预测单个设计向量的目标值。

    模型不可用时**抛 `SurrogateError`**（不是返回一个编出来的数）：
    拿坏模型预测出来的值一旦流进决策，比没有预测更糟。
    """
    if not model or not model.get("available"):
        raise SurrogateError("代理模型不可用，不能预测：%s"
                             % ((model or {}).get("reason"),))
    specs = model["variables"]
    scales = dict((name, (v["center"], v["span"]))
                  for name, v in model["scaling"].items())
    return _predict_with(model["coefficients"], vector, specs, scales)


def suggest(model, candidates):
    """
    给候选设计排序并给出"下一轮优先看哪些"的建议。

    model      : `fit()` 的返回
    candidates : [{'vector': {...}}, ...] 或 [{变量名: 值}, ...]

    返回 dict：
      available  : 模型是否可用（不可用时 ranking 为空 —— 调用方退化为不排序）
      degraded   : available 的反面
      source     : 恒为 "surrogate"
      reason     : 不可用原因
      ranking    : [{rank, key, vector, predicted, source}, ...] 按预测目标升序
                   （P3 是**最小化**质量，故预测越小排越前）
      next_batch : 建议下一轮真实评估的若干候选（ranking 的前 N 个）
      n_candidates / model_quality / thresholds

    **排序只表示"预测更轻"，不表示"合格"**：返回结构的字段里没有
    feasible/all_pass/verdict 之类的位置（见 `FORBIDDEN_VERDICT_KEYS`）。
    """
    base = {
        "available": False,
        "degraded": True,
        "source": SOURCE_SURROGATE,
        "reason": None,
        "ranking": [],
        "next_batch": [],
        "n_candidates": len(candidates or []),
        "model_quality": (model or {}).get("quality"),
        "thresholds": (model or {}).get("thresholds"),
    }
    if not model or not model.get("available"):
        base["reason"] = ((model or {}).get("reason")
                          or "代理模型不可用，无法排序（退化为不排序）")
        return base

    specs = model["variables"]
    scales = dict((name, (v["center"], v["span"]))
                  for name, v in model["scaling"].items())

    ranked = []
    for idx, cand in enumerate(candidates or []):
        vector = cand.get("vector") if isinstance(cand, dict) and "vector" in cand \
            else cand
        if not isinstance(vector, dict):
            continue
        try:
            pred = _predict_with(model["coefficients"], vector, specs, scales)
        except SurrogateError:
            continue
        ranked.append({
            "key": _key(vector) if not (isinstance(cand, dict)
                                        and cand.get("key"))
                    else cand["key"],
            "vector": dict(vector),
            "predicted": pred,
            "index": idx,
            "source": SOURCE_SURROGATE,
        })

    # 最小化质量：预测值升序；同值时按 key 保证确定性
    ranked.sort(key=lambda item: (item["predicted"], item["key"]))
    for i, item in enumerate(ranked, 1):
        item["rank"] = i

    base["available"] = True
    base["degraded"] = False
    base["ranking"] = ranked
    base["next_batch"] = ranked[:max(1, min(3, len(ranked)))]
    return base


# ---------------------------------------------------------------------------
# 落盘（产物留痕，供票27/28 展示）
# ---------------------------------------------------------------------------

def to_report_dict(model):
    """
    把模型转成可 JSON 序列化的报告 dict（去掉不可序列化的东西，补版本号）。

    不可用的模型同样可以落盘：把"这次为什么不可用"记进产物，比什么都不写
    更有用（申请人能看到代理模型在此轮确实缺位）。
    """
    if not model:
        return {}
    out = {}
    for key in ("available", "degraded", "source", "reason", "n_samples",
                "n_excluded", "n_variables", "min_samples", "variables",
                "scaling", "coefficients", "quality", "thresholds"):
        if key in model:
            out[key] = model[key]
    out["schema"] = "cae-surrogate/1"
    return out


def write_report(model, path):
    """把模型报告写成 JSON 文件；返回路径。"""
    payload = to_report_dict(model)
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True, indent=2)
    return path


def summarize(model):
    """人类可读一行摘要（日志 / 报告 / UI 共用）。"""
    if not model:
        return "代理模型: 未建立"
    if not model.get("available"):
        return "代理模型: 不可用（%s）" % (model.get("reason") or "原因未说明")
    q = model.get("quality") or {}
    return ("代理模型: 可用，样本 %d 个，留一交叉验证 MAE=%.4g（占目标跨度 "
            "%.1f%%）, RMSE=%.4g" % (model.get("n_samples", 0),
                                     q.get("mae") or 0.0,
                                     (q.get("mae_ratio") or 0.0) * 100.0,
                                     q.get("rmse") or 0.0))


def summarize_suggestion(sug):
    """人类可读的排序摘要。"""
    if not sug:
        return "代理建议: 无"
    if not sug.get("available"):
        return "代理建议: 不可用（%s）" % (sug.get("reason") or "原因未说明")
    ranking = sug.get("ranking") or []
    if not ranking:
        return "代理建议: 无候选"
    top = ranking[0]
    return ("代理建议: 共 %d 个候选，预测最优 %s（%.4g kg）——仅供排序参考，"
            "合格与否以强度校核为准"
            % (len(ranking), top.get("key"), top.get("predicted")))
