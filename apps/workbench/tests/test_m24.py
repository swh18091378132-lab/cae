# -*- coding: utf-8 -*-
"""
test_m24.py -- 票24 验收：surrogate（代理模型快筛，只排序、永不判定合格）

验收点（对应 issue #24）：
  1. 在已知关系的数据集上排序正确（代理把实际更好的点排在前面）
  2. 报出误差指标（留一交叉验证 MAE/RMSE/max），且**能被独立复算**
  3. 样本不足时明确返回"不可用"并说明**所需最少样本数**（不能拿 3 个点拟合假模型）
  4. 误差超标时返回"不可用"，调用方可据此降级为"不排序"而不阻塞闭环
  5. **返回值结构中不存在任何合格判定字段**（单测直接断言字段集合）
     —— 这是 ADR-0002 的结构性保证，不是注释里的约定
  6. 代理的建议与实际评估结果在数据上可区分（来源标记 source）
  7. 处理几何变量与铺层变量的**量纲差异**（缩放），不做缩放会让大数值变量主导拟合
  8. 纯标准库（不 import numpy）、Python 3.8 兼容

## 关于第 5 条：这是本票最重要的一条

ADR-0002（AI 三层）规定：**强度合格判定永不由 LLM/代理模型完成**。
代理模型只能排序与建议，合格与否由 `strength_check` 的确定性计算给出。
本文件用**字段集合断言**把这件事钉死：代理结果里根本不存在 `feasible` /
`all_pass` / `pass` / `verdict` 这类位置，于是任何调用方都无法从代理结果里
读出"这个设计合格"——这比在注释里写"请不要这么做"强得多。

注意：`available` / `degraded` 描述的是**模型本身能不能用**（拟合样本够不够、
误差可不可信），与"设计合不合格"完全无关，故不在禁止字段之列。

运行 (在 apps/workbench 下): python tests/test_m24.py
"""

import os
import sys
import json
import math
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
sys.path.insert(0, CORE)

import surrogate


# ---------------------------------------------------------------------------
# 造已评估点（口径与 optimizer.point_record / objectives.evaluate 一致）
# ---------------------------------------------------------------------------

def _point(vector, value, group=0):
    """一条"已评估点"，字段与 optimizer.point_record 的产物一致。"""
    group_name = {0: "feasible", 1: "infeasible", 2: "failed"}[group]
    ok = group != 2
    feasible = True if group == 0 else (False if group == 1 else None)
    return {
        "vector": dict(vector),
        "key": surrogate.vector_key(vector) if hasattr(surrogate, "vector_key")
               else _key(vector),
        "source": "doe",
        "group": group,
        "group_name": group_name,
        "ok": ok,
        "feasible": feasible,
        "value": None if group == 2 else value,
        "error": ({"kind": "run_failed", "message": "模拟故障"} if group == 2
                  else None),
        "evaluation": {
            "ok": ok,
            "feasible": feasible,
            "error": None if ok else {"kind": "run_failed"},
            "objective": {"name": "mass", "direction": "minimize",
                          "value": None if group == 2 else value,
                          "unit": "kg", "components": ["skin"]},
            "constraints": None,
        },
    }


def _key(vector):
    parts = []
    for name in sorted(vector.keys()):
        v = vector[name]
        parts.append("%s=%s" % (name, ("%.12g" % float(v))
                                 if not isinstance(v, int) else "%d" % v))
    return "|".join(parts)


VARS = [
    {"name": "c_root", "type": "continuous", "min": 1.0, "max": 3.0,
     "category": "geometry"},
    {"name": "n_plies", "type": "discrete", "candidates": [4, 6, 8],
     "category": "ply"},
]


def _fresh(name):
    d = os.path.join(WORKBENCH, ".tmp_test", "m24", name)
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d)
    return d


def _grid_points(fn, nx=7, nplies=(4, 6, 8)):
    """在二维网格上造点：fn(c_root, n_plies) -> mass。"""
    pts = []
    for i in range(nx):
        c = 1.0 + 2.0 * i / float(nx - 1)
        for n in nplies:
            pts.append(_point({"c_root": c, "n_plies": n}, fn(c, n)))
    return pts


# ---------------------------------------------------------------------------
# 1) 样本不足 -> 明确拒绝，并说明所需最少样本数
# ---------------------------------------------------------------------------

def test_rejects_insufficient_samples(fails):
    """样本不足时返回不可用，且说明所需最少样本数（不能拿 3 个点拟合假模型）。"""
    # 单变量线性模型：2 个系数（截距 + 斜率），最少样本数必然 > 3
    three = [
        _point({"c_root": 1.0}, 10.0),
        _point({"c_root": 2.0}, 8.0),
        _point({"c_root": 3.0}, 6.0),
    ]
    res = surrogate.fit(three, variables=[VARS[0]])

    if res["available"] is not False:
        fails.append("3 个点的线性拟合应判为不可用，实际 available=%r"
                     % (res["available"],))
    if not res.get("reason"):
        fails.append("不可用时必须给出可读原因")
    else:
        if str(res["min_samples"]) not in str(res["reason"]):
            fails.append("原因里应说明所需最少样本数（%d），实际 %r"
                         % (res["min_samples"], res["reason"]))
    if res.get("min_samples", 0) <= 3:
        fails.append("最少样本数应 > 3（否则 3 个点也能拟合），实际 %r"
                     % (res.get("min_samples"),))
    if res.get("n_samples") != 3:
        fails.append("应报告实际可用样本数 3，实际 %r" % (res.get("n_samples"),))

    # 不可用时不得给出排序（降级为"不排序"，而不是给个瞎猜的顺序）
    if res.get("ranking"):
        fails.append("不可用时不应给出排序: %r" % (res["ranking"],))
    # 也不能给个假的误差值（没有 LOO 就没有误差）
    if (res.get("quality") or {}).get("mae") is not None:
        fails.append("不可用时不应报误差指标: %r" % (res.get("quality"),))

    # 空输入同样要拒绝
    res2 = surrogate.fit([], variables=[VARS[0]])
    if res2["available"] is not False:
        fails.append("空输入应判为不可用")
    if not res2.get("reason"):
        fails.append("空输入应给出原因")

    # 样本刚好够（min_samples）时应能用
    enough = []
    for i in range(res["min_samples"]):
        c = 1.0 + 2.0 * i / float(res["min_samples"] - 1)
        enough.append(_point({"c_root": c}, 10.0 - 2.0 * c))
    res3 = surrogate.fit(enough, variables=[VARS[0]])
    if res3["available"] is not True:
        fails.append("样本数达到 min_samples=%d 时应可用，实际 available=%r（%r）"
                     % (res["min_samples"], res3["available"],
                        res3.get("reason")))


# ---------------------------------------------------------------------------
# 2) 已知关系上排序正确
# ---------------------------------------------------------------------------

def test_ranking_correct(fails):
    """已知关系数据集上，代理把实际更好的点排在前面。"""
    # 真实目标（解析）：mass = 2*c_root + 0.5*n_plies
    #   c_root 越小、n_plies 越少 -> 越轻
    def truth(c, n):
        return 2.0 * c + 0.5 * n

    pts = _grid_points(truth)
    res = surrogate.fit(pts, variables=VARS)
    if res["available"] is not True:
        fails.append("已知线性关系+充足样本时应可用，实际 %r（%r）"
                     % (res["available"], res.get("reason")))
        return

    # 候选池：代理给它们排序，然后与真实目标对照
    candidates = []
    for i in range(6):
        c = 1.0 + 2.0 * i / 5.0
        for n in (4, 6, 8):
            candidates.append({"c_root": c, "n_plies": n})

    sug = surrogate.suggest(res, candidates)
    ranking = sug["ranking"]
    if len(ranking) != len(candidates):
        fails.append("排序条数 %d 应等于候选数 %d"
                     % (len(ranking), len(candidates)))
        return

    predicted_order = [item["key"] for item in ranking]
    # 真实目标升序（最小的在前）
    true_sorted = sorted(candidates, key=lambda v: (truth(v["c_root"],
                                                        v["n_plies"]),
                                                    _key(v)))
    true_order = [_key(v) for v in true_sorted]

    if predicted_order != true_order:
        fails.append("代理排序与真实目标排序不一致:\n    代理 %r\n    真实 %r"
                     % (predicted_order[:6], true_order[:6]))

    # 最好与最差必须分得开（不能全挤在一起）
    best_pred = ranking[0]["predicted"]
    worst_pred = ranking[-1]["predicted"]
    if not (worst_pred - best_pred) > 1.0:
        fails.append("排序首尾的预测值几乎相同（%.6f vs %.6f），排序无意义"
                     % (best_pred, worst_pred))

    # rank 字段从 1 开始且连续
    ranks = [item["rank"] for item in ranking]
    if ranks != list(range(1, len(ranking) + 1)):
        fails.append("rank 应为 1..n 连续，实际 %r" % (ranks[:8],))

    # 预测值与解析真值应接近（线性关系上线性模型应精确）
    for item in ranking[:5]:
        v = item["vector"]
        expect = truth(v["c_root"], v["n_plies"])
        if abs(item["predicted"] - expect) > 0.05:
            fails.append("候选 %r 的预测 %.4f 与解析真值 %.4f 偏差过大"
                         % (item["key"], item["predicted"], expect))
            break


# ---------------------------------------------------------------------------
# 3) 误差指标可独立复算
# ---------------------------------------------------------------------------

def _recompute_loo(points, names, ridge=1e-8):
    """
    测试侧**独立实现**一遍留一交叉验证（不调用 surrogate 的任何内部函数）。

    口径：标准化后最小二乘；留出第 i 个样本，用其余样本重拟合，预测留出者，
    误差 e_i = pred_i - y_i，MAE = mean(|e_i|)、RMSE = sqrt(mean(e_i^2))。
    这是"独立真值来源"——如果实现里悄悄换了口径，这里就会对不上。
    """
    vals = [float(p["value"]) for p in points]
    # 每个变量的中心与跨度（与实现同口径：连续取范围中值/跨度）
    scales = {}
    for name in names:
        xs = [float(p["vector"][name]) for p in points]
        lo, hi = min(xs), max(xs)
        scales[name] = ((lo + hi) / 2.0, (hi - lo) if hi > lo else 1.0)

    def row(vector):
        r = [1.0]
        for name in names:
            c, s = scales[name]
            r.append((float(vector[name]) - c) / s)
        return r

    def solve(design, targets):
        m = len(design[0])
        a = [[0.0] * m for _ in range(m)]
        b = [0.0] * m
        for r, y in zip(design, targets):
            for i in range(m):
                b[i] += r[i] * y
                for j in range(m):
                    a[i][j] += r[i] * r[j]
        for i in range(1, m):
            a[i][i] += ridge
        # 高斯消元（独立实现）
        n = m
        mat = [list(a[i]) + [b[i]] for i in range(n)]
        for col in range(n):
            piv = max(range(col, n), key=lambda r: abs(mat[r][col]))
            if abs(mat[piv][col]) < 1e-14:
                return None
            mat[col], mat[piv] = mat[piv], mat[col]
            for r in range(col + 1, n):
                f = mat[r][col] / mat[col][col]
                for c in range(col, n + 1):
                    mat[r][c] -= f * mat[col][c]
        x = [0.0] * n
        for i in range(n - 1, -1, -1):
            s = mat[i][n]
            for j in range(i + 1, n):
                s -= mat[i][j] * x[j]
            x[i] = s / mat[i][i]
        return x

    errs = []
    for i in range(len(points)):
        train = points[:i] + points[i + 1:]
        design = [row(p["vector"]) for p in train]
        targets = [float(p["value"]) for p in train]
        coef = solve(design, targets)
        if coef is None:
            return None
        r = row(points[i]["vector"])
        pred = sum(c * v for c, v in zip(coef, r))
        errs.append(pred - float(points[i]["value"]))

    n = len(errs)
    mae = sum(abs(e) for e in errs) / float(n)
    rmse = math.sqrt(sum(e * e for e in errs) / float(n))
    ys = [float(p["value"]) for p in points]
    span = max(ys) - min(ys)
    return {"mae": mae, "rmse": rmse, "max_abs": max(abs(e) for e in errs),
            "n_folds": n, "target_span": span}


def test_error_recomputable(fails):
    """误差指标可被独立复算（测试侧用另一份实现对照）。"""
    # 带噪声的关系：y 有非线性成分，LOO 误差是个有意义的正数
    def truth(c, n):
        return 2.0 * c + 0.5 * n + 0.3 * (c * c)

    pts = _grid_points(truth)
    model = surrogate.fit(pts, variables=VARS)
    if model["available"] is not True:
        fails.append("该数据集应能拟合，实际 %r（%r）"
                     % (model["available"], model.get("reason")))
        return

    names = ["c_root", "n_plies"]
    expect = _recompute_loo(pts, names)
    got = model["quality"]

    for key in ("mae", "rmse", "max_abs"):
        if expect[key] is None:
            continue
        if abs(got[key] - expect[key]) > 1e-9:
            fails.append("%s 无法独立复算: 实现 %.12g，独立重算 %.12g"
                         % (key, got[key], expect[key]))
    if got["n_folds"] != expect["n_folds"]:
        fails.append("LOO 折数不符: %d != %d"
                     % (got["n_folds"], expect["n_folds"]))
    if abs(got["target_span"] - expect["target_span"]) > 1e-12:
        fails.append("目标跨度不符: %.12g != %.12g"
                     % (got["target_span"], expect["target_span"]))

    # ratio 的定义必须是 mae/span（可独立核验）
    if abs(got["mae_ratio"] - got["mae"] / got["target_span"]) > 1e-12:
        fails.append("mae_ratio 不等于 mae/target_span（口径不一致）")
    if abs(got["rmse_ratio"] - got["rmse"] / got["target_span"]) > 1e-12:
        fails.append("rmse_ratio 不等于 rmse/target_span（口径不一致）")

    # 误差指标必须为有限非负数
    for key in ("mae", "rmse", "max_abs"):
        v = got[key]
        if v is None or v < 0 or v != v or v == float("inf"):
            fails.append("%s 不是有限的非负数: %r" % (key, v))

    # 排序关系：max_abs >= rmse >= mae（数学上必然）
    if not (got["max_abs"] + 1e-12 >= got["rmse"] + 1e-12 >= got["mae"] - 1e-12):
        fails.append("误差指标之间的大小关系不成立: max=%.6g rmse=%.6g mae=%.6g"
                     % (got["max_abs"], got["rmse"], got["mae"]))


# ---------------------------------------------------------------------------
# 4) 误差超标 -> 降级为不可用（不阻塞闭环）
# ---------------------------------------------------------------------------

def test_error_over_threshold_degrades(fails):
    """误差超标时返回不可用并说明原因，调用方据此降级但不阻塞。"""
    # 纯噪声目标：c_root 和 n_plies 跟 y 毫无关系，任何线性模型都拟合不了
    def noise(c, n):
        return ((c * 1000.0) % 7.0) + ((n * 13.0) % 5.0)

    pts = _grid_points(noise)

    # 默认阈值下多半可用（噪声也可能碰巧线性相关），所以显式把阈值压到极严
    strict = surrogate.fit(pts, variables=VARS,
                           max_mae_ratio=1e-9, max_rmse_ratio=1e-9)
    if strict["available"] is not False:
        fails.append("阈值压到 1e-9 时任何模型都该判不可用（MAE 不可能为 0），"
                     "实际 available=%r（MAE ratio=%r）"
                     % (strict["available"],
                        (strict["quality"] or {}).get("mae_ratio")))
    if not strict.get("reason"):
        fails.append("误差超标时必须给出原因")
    else:
        if "MAE" not in strict["reason"] and "RMSE" not in strict["reason"]:
            fails.append("误差超标的原因应点明是哪个指标超了: %r"
                         % (strict["reason"],))
    if strict.get("degraded") is not True:
        fails.append("误差超标时应标 degraded=True")
    # 仍然要报出误差本身（人要能看到"到底差多少"）
    if (strict.get("quality") or {}).get("mae") is None:
        fails.append("误差超标时仍应报出误差指标（供人判断）")
    # 但不许给排序
    if strict.get("ranking"):
        fails.append("误差超标时不应给出排序")

    # 降级路径：suggest 必须能安全调用（不抛异常），返回空排序
    sug = surrogate.suggest(strict, [{"c_root": 1.0, "n_plies": 4}])
    if sug["available"] is not False:
        fails.append("模型不可用时 suggest 应返回 available=False")
    if sug["ranking"]:
        fails.append("模型不可用时 suggest 不应给排序: %r" % (sug["ranking"],))
    if not sug.get("reason"):
        fails.append("suggest 降级时应说明原因")
    # predict 在不可用模型上必须抛错（不能编一个数出来）
    try:
        surrogate.predict(strict, {"c_root": 1.0, "n_plies": 4})
        fails.append("不可用模型上调用 predict 应抛错，而不是返回编造的值")
    except surrogate.SurrogateError:
        pass

    # 阈值放宽后应恢复可用（证明降级确实是"按阈值"，不是被写死）
    loose = surrogate.fit(pts, variables=VARS,
                          max_mae_ratio=10.0, max_rmse_ratio=10.0)
    if loose["available"] is not True:
        fails.append("阈值放宽到 10 后应可用，实际 %r（%r）"
                     % (loose["available"], loose.get("reason")))


# ---------------------------------------------------------------------------
# 5) 量纲缩放：几何 vs 铺层
# ---------------------------------------------------------------------------

def test_scaling_handles_units(fails):
    """几何变量与铺层变量量纲差很大时，缩放让两者贡献可比。"""
    # 真实关系里，n_plies 的系数很大但在数值上"跨度小"，
    # c_root 的系数小但跨度也不大 —— 若不做标准化（直接用原始数值），
    # 设计矩阵会病态，系数估计会被数值大的变量绑架。
    #
    # 这里构造：y = 100*n_plies + 0.001*c_root
    #   原始数值里 c_root(1..3) 与 n_plies(4..8) 尺度相当，但系数差 5 个数量级；
    #   标准化后两者都被压到 [-0.5, 0.5]，拟合才能稳定恢复系数。
    def truth(c, n):
        return 100.0 * n + 0.001 * c

    pts = _grid_points(truth)
    model = surrogate.fit(pts, variables=VARS)
    if model["available"] is not True:
        fails.append("缩放后应能拟合，实际不可用: %r" % (model.get("reason"),))
        return

    # 缩放口径必须落进结果（可复算、可解释）
    scaling = model.get("scaling") or {}
    for name in ("c_root", "n_plies"):
        if name not in scaling:
            fails.append("scaling 里缺少变量 %r 的口径" % (name,))
            continue
        if not scaling[name].get("span"):
            fails.append("scaling[%r].span 应为正数: %r"
                         % (name, scaling[name]))
    # c_root 的跨度是 2.0（1..3），n_plies 是 4（4..8）
    if abs(scaling["c_root"]["span"] - 2.0) > 1e-9:
        fails.append("c_root 跨度应缩放为 2.0，实际 %r"
                     % (scaling["c_root"]["span"],))
    if abs(scaling["n_plies"]["span"] - 4.0) > 1e-9:
        fails.append("n_plies 跨度应缩放为 4.0，实际 %r"
                     % (scaling["n_plies"]["span"],))

    # 关键断言：预测必须精确（线性关系 + 正确缩放 -> 残差为 0）
    for v in ({"c_root": 1.0, "n_plies": 4}, {"c_root": 2.5, "n_plies": 6},
              {"c_root": 3.0, "n_plies": 8}):
        pred = surrogate.predict(model, v)
        expect = truth(v["c_root"], v["n_plies"])
        if abs(pred - expect) > 1e-6 * max(1.0, abs(expect)):
            fails.append("预测 %r 不符: %r vs 解析 %r" % (v, pred, expect))
            break
    # LOO 误差应接近 0（精确线性关系）
    if model["quality"]["mae"] > 1e-6 * model["quality"]["target_span"]:
        fails.append("精确线性关系下 LOO MAE 应接近 0，实际 %r"
                     % (model["quality"]["mae"],))

    # 反证：把缩放全部设成 1（相当于不缩放），在病态数据上应出问题。
    # 我们用"跨量纲极大差异"的数据：c_root 1..3，但另一个变量取值 1e6..2e6。
    big_vars = [
        {"name": "a", "type": "continuous", "min": 1.0, "max": 3.0},
        {"name": "b", "type": "continuous", "min": 1e6, "max": 2e6},
    ]
    big_pts = []
    for i in range(8):
        a = 1.0 + 2.0 * i / 7.0
        b = 1e6 + 1e6 * i / 7.0
        big_pts.append(_point({"a": a, "b": b}, 3.0 * a + 1e-6 * b))
    big_model = surrogate.fit(big_pts, variables=big_vars)
    if big_model["available"] is not True:
        fails.append("跨 6 个数量级的变量应能拟合（靠缩放），实际 %r（%r）"
                     % (big_model["available"], big_model.get("reason")))
    else:
        pred = surrogate.predict(big_model, {"a": 2.0, "b": 1.5e6})
        expect = 3.0 * 2.0 + 1e-6 * 1.5e6
        if abs(pred - expect) > 1e-6 * max(1.0, abs(expect)):
            fails.append("跨量级数据的预测不符: %r vs %r" % (pred, expect))


# ---------------------------------------------------------------------------
# 6) ADR-0002 结构性保证：返回值里不存在任何"合格判定"字段
# ---------------------------------------------------------------------------

def _all_keys(obj):
    """递归收集一个结构里出现的全部字典键（含嵌套）。"""
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _all_keys(v)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            keys |= _all_keys(item)
    return keys


def test_no_verdict_fields(fails):
    """
    返回值结构中**根本不存在**任何合格判定字段（ADR-0002 硬约束）。

    这是靠数据结构保证的，不是靠注释约定：调用方拿不到代理的"合格结论"，
    因为那个位置压根不存在。
    """
    def truth(c, n):
        return 2.0 * c + 0.5 * n

    pts = _grid_points(truth)
    model = surrogate.fit(pts, variables=VARS)
    candidates = [{"c_root": 1.0 + 0.4 * i, "n_plies": n}
                  for i in range(4) for n in (4, 6, 8)]
    sug = surrogate.suggest(model, candidates)

    # 代理模型本身声明的禁止字段清单，必须真的覆盖这些直觉上的"判定"词
    for name in ("feasible", "all_pass", "pass", "verdict", "max_fi", "min_ms"):
        if name not in surrogate.FORBIDDEN_VERDICT_KEYS:
            fails.append("FORBIDDEN_VERDICT_KEYS 未覆盖 %r" % (name,))

    # fit / suggest / predict 的产物都要逐个检查
    to_check = [("fit", model), ("suggest", sug)]
    for label, payload in to_check:
        keys = _all_keys(payload)
        bad = sorted(keys & set(surrogate.FORBIDDEN_VERDICT_KEYS))
        if bad:
            fails.append("%s 的返回结构里出现了合格判定字段 %r —— 违反 ADR-0002"
                         % (label, bad))

    # 排序条目里也不许有（逐项检查，防止只在外层漏过）
    for item in sug["ranking"]:
        keys = _all_keys(item)
        bad = sorted(keys & set(surrogate.FORBIDDEN_VERDICT_KEYS))
        if bad:
            fails.append("排序条目 %r 里出现了合格判定字段 %r"
                         % (item.get("key"), bad))
            break

    # predict 返回的是纯数值，不是判定对象
    pred = surrogate.predict(model, {"c_root": 1.5, "n_plies": 6})
    if not isinstance(pred, (int, float)) or isinstance(pred, bool):
        fails.append("predict 应返回纯数值，实际 %r" % (type(pred).__name__,))

    # 顶层键集合必须是白名单内的（新增字段要有意识地加，不能顺手塞判定）
    allowed_top = set([
        "available", "degraded", "source", "reason", "n_samples",
        "n_excluded", "n_variables", "min_samples", "variables", "scaling",
        "coefficients", "quality", "thresholds", "ranking", "next_batch",
        "n_candidates", "model_quality",
    ])
    for label, payload in to_check:
        extra = set(payload.keys()) - allowed_top
        if extra:
            fails.append("%s 返回了白名单外的字段 %r（先确认它不是合格判定）"
                         % (label, sorted(extra)))

    # 质量报告（落盘形态）同样不得含判定字段
    path = os.path.join(_fresh("report"), "surrogate.json")
    surrogate.write_report(model, path)
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    keys = _all_keys(payload)
    bad = sorted(keys & set(surrogate.FORBIDDEN_VERDICT_KEYS))
    if bad:
        fails.append("落盘的代理模型报告里出现了合格判定字段 %r" % (bad,))
    if payload.get("schema") != "cae-surrogate/1":
        fails.append("落盘报告缺少版本标识: %r" % (payload.get("schema"),))

    # summarize 文本里也不许出现"合格"这种判定措辞（避免误导读者）
    txt = surrogate.summarize(model)
    if "合格" in txt and "不可用" not in txt:
        fails.append("summarize 文本出现了合格判定措辞: %r" % (txt,))
    stxt = surrogate.summarize_suggestion(sug)
    if "合格" in stxt and "以强度校核为准" not in stxt:
        fails.append("排序摘要提到合格却没声明以强度校核为准: %r" % (stxt,))
    if "以强度校核为准" not in stxt:
        fails.append("排序摘要应声明合格与否以强度校核为准: %r" % (stxt,))


# ---------------------------------------------------------------------------
# 7) 来源标记：代理建议 vs 实测，在数据上可区分
# ---------------------------------------------------------------------------

def test_source_marking(fails):
    """代理建议的来源可标记，供票27/28 区分"代理建议"与"实测"。"""
    def truth(c, n):
        return 2.0 * c + 0.5 * n

    pts = _grid_points(truth)
    model = surrogate.fit(pts, variables=VARS)
    sug = surrogate.suggest(model, [{"c_root": 1.0, "n_plies": 4},
                                    {"c_root": 2.0, "n_plies": 6}])

    if model.get("source") != surrogate.SOURCE_SURROGATE:
        fails.append("模型应标记 source=%r，实际 %r"
                     % (surrogate.SOURCE_SURROGATE, model.get("source")))
    if sug.get("source") != surrogate.SOURCE_SURROGATE:
        fails.append("建议应标记 source=%r，实际 %r"
                     % (surrogate.SOURCE_SURROGATE, sug.get("source")))
    # 每条排序条目也要带来源（下钻到条目级才不会被汇总时丢掉）
    for item in sug["ranking"]:
        if item.get("source") != surrogate.SOURCE_SURROGATE:
            fails.append("排序条目缺少来源标记: %r" % (item,))
            break

    # 与"实测"点对照：优化器产出的点 source 是 doe/local，两者能区分开
    measured_sources = set(p["source"] for p in pts)
    if surrogate.SOURCE_SURROGATE in measured_sources:
        fails.append("实测点里出现了 surrogate 来源标记，两者无法区分")

    # 排序条目必须保留原始的 vector（供下一轮真实评估用）
    for item in sug["ranking"]:
        if not item.get("vector"):
            fails.append("排序条目缺少 vector，无法拿去真实评估: %r" % (item,))
            break
        if item.get("predicted") is None:
            fails.append("排序条目缺少 predicted: %r" % (item,))
            break

    # next_batch：建议下一轮优先真实评估的候选，且是 ranking 的前几个
    nb = sug.get("next_batch") or []
    if not nb:
        fails.append("可用模型应给出 next_batch 建议")
    else:
        top_keys = [item["key"] for item in sug["ranking"][:len(nb)]]
        if [item["key"] for item in nb] != top_keys:
            fails.append("next_batch 应是 ranking 的前几个: %r vs %r"
                         % ([i["key"] for i in nb], top_keys))
        # next_batch 同样必须带来源标记
        for item in nb:
            if item.get("source") != surrogate.SOURCE_SURROGATE:
                fails.append("next_batch 条目缺少来源标记")
                break


# ---------------------------------------------------------------------------
# 8) 只用跑通的点拟合（技术故障点不污染模型）
# ---------------------------------------------------------------------------

def test_excludes_failed_points(fails):
    """技术故障点没有可信目标值，不参与拟合；排除数要如实报告。"""
    def truth(c, n):
        return 2.0 * c + 0.5 * n

    good = _grid_points(truth)

    # 掺入若干故障点：它们的目标值存在但 ok=False（最危险的情形——
    # 若按 value 训练，等于把"求解失败时的垃圾值"当信号）
    poisoned = list(good)
    for i in range(4):
        p = _point({"c_root": 1.0 + i * 0.5, "n_plies": 4}, 999.0, group=2)
        poisoned.append(p)

    clean_model = surrogate.fit(good, variables=VARS)
    poisoned_model = surrogate.fit(poisoned, variables=VARS)

    if poisoned_model.get("n_excluded") != 4:
        fails.append("应报告排除了 4 个技术故障点，实际 %r"
                     % (poisoned_model.get("n_excluded"),))
    if poisoned_model.get("n_samples") != len(good):
        fails.append("拟合样本数应仍为 %d（故障点被排除），实际 %r"
                     % (len(good), poisoned_model.get("n_samples")))

    # 混入故障点后，预测结果不该被拉到 999 附近
    pred_clean = surrogate.predict(clean_model, {"c_root": 2.0, "n_plies": 6})
    pred_poison = surrogate.predict(poisoned_model, {"c_root": 2.0,
                                                     "n_plies": 6})
    if abs(pred_poison - pred_clean) > 1e-6:
        fails.append("技术故障点影响了拟合结果: 干净 %.6f vs 掺入后 %.6f"
                     % (pred_clean, pred_poison))
    expect = truth(2.0, 6)
    if abs(pred_poison - expect) > 1e-6:
        fails.append("掺入故障点后预测偏离解析真值: %.6f vs %.6f"
                     % (pred_poison, expect))

    # 全是故障点时应明确不可用（没有可信目标值可训练）
    all_bad = [_point({"c_root": 1.0 + 0.3 * i}, 1.0, group=2)
               for i in range(8)]
    m = surrogate.fit(all_bad, variables=[VARS[0]])
    if m["available"] is not False:
        fails.append("没有任何可用点时模型应不可用，实际 %r" % (m["available"],))
    if "样本不足" not in (m.get("reason") or ""):
        fails.append("全故障点的原因应说明样本不足: %r" % (m.get("reason"),))


# ---------------------------------------------------------------------------
# 9) 与 optimizer 的点结构对接（不重新定义"已评估点"）
# ---------------------------------------------------------------------------

def test_interop_with_optimizer(fails):
    """直接吃 optimizer 产出的点：无需转换，闭环里能真用上。"""
    import objectives
    import optimizer

    vars_ = [
        {"name": "c_root", "type": "continuous", "min": 1.0, "max": 3.0,
         "category": "geometry"},
        {"name": "n_ribs", "type": "discrete", "min": 3, "max": 9, "step": 1,
         "category": "geometry"},
    ]

    def evaluate(vector):
        # 真实口径的目标：质量随 c_root/n_ribs 增加
        mass = 2.0 * vector["c_root"] + 0.3 * vector["n_ribs"]
        if vector["n_ribs"] == 7:
            return objectives.evaluate(error=RuntimeError("模拟求解失败"))
        feasible = vector["c_root"] >= 1.5      # 人为造一个不可行区
        summ = {"all_pass": feasible,
                "max_fi": 0.8 if feasible else 1.4,
                "min_ms": 0.25 if feasible else -0.2,
                "n_bays_excluded": 0, "n_elems": 6, "n_detail_rows": 24,
                "n_fail_rows": 0 if feasible else 3,
                "criteria": ["tsai_wu"], "safety_factor": 1.5}
        return objectives.evaluate(check_summary=summ, mass_kg=mass)

    result = optimizer.optimize(vars_, evaluate,
                                budget={"max_evals": 50, "max_rounds": 10},
                                seed=0)
    if not result["feasible"]:
        fails.append("优化未找到可行点，无法验证代理对接")
        return

    # 关键：把 optimizer 的点**原样**喂给 surrogate（不写转换代码）
    all_points = list(result["points"]) + [
        p for group in result["groups"].values() for p in group
        if p not in result["points"]
    ]
    model = surrogate.fit(all_points, variables=vars_)

    if model["n_excluded"] <= 0:
        fails.append("这批点里有技术故障点，代理想必排除了若干，实际 n_excluded=%r"
                     % (model.get("n_excluded"),))
    if model["available"] is not True:
        fails.append("优化器产出的点应足以拟合代理模型，实际 %r（%r）"
                     % (model["available"], model.get("reason")))
        return

    # 预测最优的候选，其预测值应与真实目标同向（c_root/n_ribs 越小越轻）
    sug = surrogate.suggest(model, [
        {"c_root": 1.0, "n_ribs": 3},
        {"c_root": 2.5, "n_ribs": 9},
        {"c_root": 2.0, "n_ribs": 5},
    ])
    if not sug["available"]:
        fails.append("suggest 应可用")
        return
    best = sug["ranking"][0]["vector"]
    if not (best["c_root"] <= 2.0 and best["n_ribs"] <= 6):
        fails.append("代理把明显更重的点排在前面: %r" % (best,))

    # 代理建议只做排序：它不能替优化器判定可行性
    for item in sug["ranking"]:
        if "feasible" in item or "all_pass" in item:
            fails.append("排序条目里带了可行性判定（ADR-0002 违规）: %r" % (item,))
            break

    # 落盘 + 摘要都能用
    tmp = _fresh("interop")
    path = os.path.join(tmp, "surrogate.json")
    surrogate.write_report(model, path)
    if not os.path.isfile(path):
        fails.append("代理模型报告未落盘")
    for fn in (surrogate.summarize,):
        txt = fn(model)
        if not txt:
            fails.append("摘要为空")


# ---------------------------------------------------------------------------
# 10) 输入校验（配置非法要明确报错，不静默产假模型）
# ---------------------------------------------------------------------------

def test_input_validation(fails):
    """变量声明/点结构非法时明确报错。"""
    def truth(c, n):
        return 2.0 * c + 0.5 * n

    pts = _grid_points(truth, nx=6)

    # 变量声明缺 min/max 且无 candidates
    try:
        surrogate.fit(pts, variables=[{"name": "c_root"}])
        fails.append("变量声明缺 min/max 未被拒绝")
    except surrogate.SurrogateError:
        pass

    # 重复变量名
    try:
        surrogate.fit(pts, variables=[
            {"name": "c_root", "min": 1, "max": 3},
            {"name": "c_root", "min": 1, "max": 3}])
        fails.append("重复变量名未被拒绝")
    except surrogate.SurrogateError:
        pass

    # 点里缺变量 -> 预测/拟合要报错（不能当 0 处理）
    model = surrogate.fit(pts, variables=VARS)
    try:
        surrogate.predict(model, {"c_root": 2.0})     # 缺 n_plies
        fails.append("设计向量缺变量时 predict 未报错")
    except surrogate.SurrogateError:
        pass

    # 既无声明又无点
    try:
        surrogate.fit([])
        fails.append("既无变量声明又无样本点未被拒绝")
    except surrogate.SurrogateError:
        pass

    # 不给变量声明时能从点推断（容错路径，但要能跑）
    inferred = surrogate.fit(pts)
    if inferred["available"] is not True:
        fails.append("不给变量声明时应能从点推断并拟合，实际 %r"
                     % (inferred.get("reason"),))
    else:
        names = sorted(v["name"] for v in inferred["variables"])
        if names != ["c_root", "n_plies"]:
            fails.append("推断的变量名不符: %r" % (names,))

    # 目标值不是有限数 -> 拒绝
    bad = list(pts)
    bad[0] = dict(bad[0])
    bad[0]["value"] = float("inf")
    m = surrogate.fit(bad, variables=VARS)
    if m["available"] is not False:
        fails.append("目标值含 inf 时应判不可用，实际 %r" % (m["available"],))

    # 目标值在全部样本上相同 -> 无法定义相对误差，应判不可用并说清楚
    flat = [_point({"c_root": 1.0 + 0.2 * i, "n_plies": 6}, 5.0)
            for i in range(8)]
    m2 = surrogate.fit(flat, variables=VARS)
    if m2["available"] is not False:
        fails.append("目标全相同时应判不可用（相对误差无定义），实际 %r"
                     % (m2["available"],))
    if "跨度" not in (m2.get("reason") or ""):
        fails.append("目标全相同的理由应说明跨度问题: %r" % (m2.get("reason"),))


# ---------------------------------------------------------------------------
# 11) Python 3.8 兼容 + 纯标准库
# ---------------------------------------------------------------------------

def test_py38(fails):
    """Python 3.8 兼容：本票涉及的模块不得用 3.9+ 语法/标准库。"""
    import compat_py38

    targets = [
        os.path.join(CORE, "surrogate.py"),
        os.path.join(CORE, "compat_py38.py"),
        os.path.join(HERE, "test_m24.py"),
    ]
    bad = compat_py38.check_paths(targets)
    if bad:
        fails.append("Python 3.8 兼容性检查未通过:\n%s"
                     % compat_py38.format_violations(bad))

    probe = compat_py38.check_source(
        "probe.py", "x: list[int] = []\ns = 'a'.removeprefix('b')\n")
    if len(probe) < 2:
        fails.append("compat_py38 未检出 list[int] / removeprefix")

    # 纯标准库：**import 语句**里不得出现第三方/后端子模块
    # （只在注释/docstring 里提到名字是正常的——比如"合格判定由 strength_check
    #   给出"这句话本身就该写进说明里，所以这里检查 import 而不是字符串包含）
    import ast
    path = os.path.join(CORE, "surrogate.py")
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
    banned = {"numpy", "scipy", "sklearn", "pandas", "run_study",
              "catia_driver", "abaqus_runner", "db", "workbench_service",
              "strength_check", "optimizer", "objectives"}
    hit = sorted(imported & banned)
    if hit:
        fails.append("surrogate.py 不应 import %r（纯算法、零第三方、不依赖后端）"
                     % (hit,))


def main():
    fails = []
    print("1. 样本不足 -> 拒绝并说明所需样本数 ...")
    test_rejects_insufficient_samples(fails)
    print("2. 已知关系上排序正确 ...")
    test_ranking_correct(fails)
    print("3. 误差指标可独立复算（LOO MAE/RMSE） ...")
    test_error_recomputable(fails)
    print("4. 误差超标 -> 降级为不可用（不阻塞闭环） ...")
    test_error_over_threshold_degrades(fails)
    print("5. 量纲缩放（几何 vs 铺层） ...")
    test_scaling_handles_units(fails)
    print("6. ADR-0002：返回值不含任何合格判定字段 ...")
    test_no_verdict_fields(fails)
    print("7. 来源标记（代理建议 vs 实测） ...")
    test_source_marking(fails)
    print("8. 技术故障点不参与拟合 ...")
    test_excludes_failed_points(fails)
    print("9. 与 optimizer 点结构对接 ...")
    test_interop_with_optimizer(fails)
    print("10. 输入校验 ...")
    test_input_validation(fails)
    print("11. Python 3.8 兼容 + 纯标准库 ...")
    test_py38(fails)

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M24 (票24) SELF-TEST: ALL PASSED "
          "(排序正确 / LOO 误差可复算 / 样本不足拒绝 / 误差超标降级 / "
          "无判定字段(ADR-0002) / 量纲缩放 / 来源标记 / py3.8)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
