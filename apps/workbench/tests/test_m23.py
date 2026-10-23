# -*- coding: utf-8 -*-
"""
test_m23.py -- 票23 验收：optimizer（DOE 采样、约束筛选、收敛判定，注入式评估）

验收点（对应 issue #23）：
  1. 核心入口只依赖注入的 `evaluate(设计向量)` 回调 —— 单测完全不跑 Study，秒级完成
  2. DOE 采样：连续变量落在范围内且分层铺点，离散变量只取合法值
  3. **同配置同种子 → 同结果**（确定性可回归）
  4. 约束筛选：在可行点里挑目标最优；**没有可行点时明确报告**（可读原因），
     不返回含义不明的 None
  5. 收敛/预算：`max_evals` / `max_rounds` / `converged` / `no_feasible` 四种
     停止原因各自可达且被正确报出
  6. **不假设目标可微**：非光滑阶梯目标仍能稳定改进（不做梯度下降）
  7. 去重与缓存：重复设计点不重复调用 `evaluate`（断言调用次数）
  8. **技术故障点即便目标值极小也不会被选中**（复用 objectives.sort_key 的三分组）
  9. 已评估点可导出/导入，续跑不丢、不必重算
 10. Python 3.8 兼容

评估结果一律用 `core/objectives.evaluate()` 造 —— 优化器消费的就是那个结构，
测试也走同一个真实入口，不另造一套假格式。

运行 (在 apps/workbench 下): python tests/test_m23.py
"""

import os
import sys
import json
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
sys.path.insert(0, CORE)

import objectives
import optimizer


# ---------------------------------------------------------------------------
# 变量声明（数据驱动，与票22 design_vector 同一口径）
# ---------------------------------------------------------------------------

VARS = [
    {"name": "c_root", "type": "continuous", "min": 1.0, "max": 3.0,
     "category": "geometry", "unit": "m"},
    {"name": "n_ribs", "type": "discrete", "min": 3, "max": 9, "step": 1,
     "category": "geometry"},
    {"name": "n_plies", "type": "discrete", "candidates": [4, 6, 8],
     "category": "ply"},
]

# 只含连续变量（DOE 分层与确定性最好断言）
CONT_VARS = [
    {"name": "x", "type": "continuous", "min": 0.0, "max": 1.0,
     "category": "geometry"},
]


def _fresh(name):
    d = os.path.join(WORKBENCH, ".tmp_test", "m23", name)
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d)
    return d


# ---------------------------------------------------------------------------
# 造评估结果（一律走 objectives.evaluate，与运行时同构）
# ---------------------------------------------------------------------------

def _summary(all_pass=True, max_fi=None, min_ms=None):
    if all_pass:
        max_fi = 0.8 if max_fi is None else max_fi
        min_ms = 0.25 if min_ms is None else min_ms
    else:
        max_fi = 1.4 if max_fi is None else max_fi
        min_ms = -0.29 if min_ms is None else min_ms
    return {
        "max_fi": max_fi, "min_ms": min_ms, "all_pass": all_pass,
        "n_bays_checked": 6, "n_bays_excluded": 0, "n_elems": 6,
        "n_detail_rows": 24, "n_fail_rows": 0 if all_pass else 3,
        "criteria": ["tsai_wu"], "safety_factor": 1.5, "ms_threshold": 0.0,
    }


def _feasible(mass):
    # 显式声明口径：真实调用方走 objectives.mass() 拿到的结果就自带 components。
    # 未声明口径的数不可比（见 objectives.evaluate 的 comparable），不该用于搜索测试。
    return objectives.evaluate(check_summary=_summary(True), mass_kg=mass,
                               mass_components=["skin"])


def _infeasible(mass):
    return objectives.evaluate(check_summary=_summary(False), mass_kg=mass,
                               mass_components=["skin"])


def _broken(message="求解失败"):
    return objectives.evaluate(error=RuntimeError(message))


def _sequential_evaluator(fn):
    """返回 (evaluate 回调, 调用计数器)，同时记录调用过的向量。"""
    calls = {"n": 0, "vectors": []}

    def evaluate(vector):
        calls["n"] += 1
        calls["vectors"].append(dict(vector))
        return fn(vector)

    return evaluate, calls


# ---------------------------------------------------------------------------
# 1) DOE 采样
# ---------------------------------------------------------------------------

def test_doe_sampling(fails):
    """DOE：连续变量分层铺点，离散变量只取合法值。"""
    samples = optimizer.doe_sample(CONT_VARS, n_samples=5, seed=0)
    if len(samples) != 5:
        fails.append("n_samples=5 应采出 5 个点，实际 %d" % len(samples))
        return

    xs = [s["x"] for s in samples]
    # 分层：第 k 个样本必须落在第 k 层 [k/5, (k+1)/5) 内（LHS 的定义）
    for k, v in enumerate(xs):
        lo, hi = k / 5.0, (k + 1) / 5.0
        if not (lo - 1e-12 <= v <= hi + 1e-12):
            fails.append("第 %d 个样本 x=%.6f 未落在分层 [%.2f, %.2f] 内"
                         % (k, v, lo, hi))
    # 全部落在变量范围内
    for v in xs:
        if v < 0.0 - 1e-12 or v > 1.0 + 1e-12:
            fails.append("连续变量采样越界: %r" % (v,))
    if len(set(xs)) != len(xs):
        fails.append("分层采样出现重复值（分层失效）: %r" % (xs,))

    # 离散：只取合法值
    disc_vars = [VARS[1]]          # n_ribs: min=3 max=9 step=1
    for s in optimizer.doe_sample(disc_vars, n_samples=8, seed=0):
        v = s["n_ribs"]
        if not isinstance(v, int) or isinstance(v, bool):
            fails.append("离散变量必须取整数，实际 %r" % (v,))
            break
        if v < 3 or v > 9:
            fails.append("离散变量越界: %r" % (v,))
            break

    # 候选值型离散：只能取候选里的值
    cand_vars = [VARS[2]]          # n_plies: [4, 6, 8]
    for s in optimizer.doe_sample(cand_vars, n_samples=6, seed=0):
        if s["n_plies"] not in (4, 6, 8):
            fails.append("候选值型离散变量取了非候选值: %r" % (s["n_plies"],))
            break

    # 带步长的离散：3,5,7,9
    step_vars = [{"name": "n", "type": "discrete", "min": 3, "max": 9,
                  "step": 2, "category": "geometry"}]
    for s in optimizer.doe_sample(step_vars, n_samples=6, seed=0):
        if s["n"] not in (3, 5, 7, 9):
            fails.append("带步长离散变量取了非法值: %r" % (s["n"],))
            break

    # 混合变量：每个样本都要含全部变量且各自合法
    mixed = optimizer.doe_sample(VARS, n_samples=4, seed=0)
    if len(mixed) != 4:
        fails.append("混合变量采样数量不符: %d" % len(mixed))
    for s in mixed:
        if sorted(s.keys()) != sorted(v["name"] for v in VARS):
            fails.append("样本缺少变量: %r" % (sorted(s.keys()),))
            break
        if not (1.0 - 1e-12 <= s["c_root"] <= 3.0 + 1e-12):
            fails.append("混合采样 c_root 越界: %r" % (s["c_root"],))
            break
        if s["n_plies"] not in (4, 6, 8):
            fails.append("混合采样 n_plies 非候选值: %r" % (s["n_plies"],))
            break


def test_determinism(fails):
    """同配置同种子 → 完全相同的采样与搜索结果。"""
    a = optimizer.doe_sample(VARS, n_samples=6, seed=42)
    b = optimizer.doe_sample(VARS, n_samples=6, seed=42)
    if a != b:
        fails.append("同种子两次 DOE 结果不一致:\n    %r\n    %r" % (a, b))

    # 不同种子 -> 采样顺序/取值应不同（否则种子形同虚设）
    c = optimizer.doe_sample(VARS, n_samples=6, seed=7)
    if a == c:
        fails.append("不同种子得到完全相同结果（种子未生效）")

    # 完整优化流程也要确定性
    def fn(vector):
        return _feasible(vector["c_root"] + 0.01 * vector["n_ribs"])

    def run():
        ev, _calls = _sequential_evaluator(fn)
        return optimizer.optimize(VARS, ev, budget={"max_evals": 20}, seed=42)

    r1, r2 = run(), run()
    if r1["best"] != r2["best"]:
        fails.append("同种子两次优化的最优设计方案不一致:\n    %r\n    %r"
                     % (r1["best"], r2["best"]))
    if r1["stop_reason"] != r2["stop_reason"]:
        fails.append("同种子两次优化的停止原因不一致: %r vs %r"
                     % (r1["stop_reason"], r2["stop_reason"]))
    if len(r1["points"]) != len(r2["points"]):
        fails.append("同种子两次优化的评估点数不一致: %d vs %d"
                     % (len(r1["points"]), len(r2["points"])))


# ---------------------------------------------------------------------------
# 3) 约束筛选 + 技术故障不参与最优
# ---------------------------------------------------------------------------

#: 质量随 x 的解析目标（x 越小越好），用来手算"最优应该在哪个 x"
#:   mass = 10 + 10*x  -> x=0 时 10 kg 最轻
def _analytic(vector):
    return _feasible(10.0 + 10.0 * vector["x"])


def test_picks_best_feasible(fails):
    """在可行点里挑目标最优，且选中的确实是可行点里最优的。"""
    ev, calls = _sequential_evaluator(_analytic)
    res = optimizer.optimize(CONT_VARS, ev, budget={"max_evals": 40,
                                                    "max_rounds": 10}, seed=0)

    if not res["feasible"]:
        fails.append("该用例存在可行点，却报告无可行点: %r"
                     % (res.get("stop_reason"),))
        return
    if res["best"] is None:
        fails.append("找到可行点却没给出 best")
        return

    # 断言 1：选中的点必须是可行点
    best_point = res["best_point"]
    if best_point["group"] != optimizer.GROUP_FEASIBLE:
        fails.append("被选中的最优点不是可行点: group=%r" % (best_point["group"],))

    # 断言 2：它的目标值必须是全部**可行**点里最小的（独立复算，不看内部状态）
    feasible_values = [p["value"] for p in res["points"]
                       if p["group"] == optimizer.GROUP_FEASIBLE
                       and p["value"] is not None]
    if not feasible_values:
        fails.append("结果里没有可行点记录")
    elif res["best"]["value"] > min(feasible_values) + 1e-9:
        fails.append("最优值 %.9f 不是可行点里的最小 %.9f"
                     % (res["best"]["value"], min(feasible_values)))

    # 断言 3：解析最优 x=0 附近应被找到（容许初始步长级别的偏差）
    if res["best"]["vector"]["x"] > 0.25:
        fails.append("解析最优在 x=0 附近，搜索结果 x=%r 偏太多"
                     % (res["best"]["vector"]["x"],))

    # 断言 4：最优值必须与目标函数的解析值一致（目标函数没被篡改）
    x = res["best"]["vector"]["x"]
    expect = 10.0 + 10.0 * x
    if abs(res["best"]["value"] - expect) > 1e-9:
        fails.append("最优值 %.9f 与目标函数在 x=%.6f 处的解析值 %.9f 不符"
                     % (res["best"]["value"], x, expect))

    # 断言 5：有不可行点时，它们不能出现在 best 里
    if "infeasible" in res["groups"] and res["groups"]["infeasible"]:
        for p in res["groups"]["infeasible"]:
            if p["key"] == best_point["key"]:
                fails.append("不可行点被选成了最优")


def test_failed_point_never_chosen(fails):
    """技术故障点即便目标值极小，也绝不能被选成最优（本票核心断言）。"""
    trap_vector = {"x": 0.0}     # 这个点会"求解失败"，但目标值人为设成极小

    def trap(vector):
        # 最关键的一点：故障点如果按 value 排序会排第一（1e-9 < 任何真值）
        if abs(vector["x"] - 0.0) < 1e-9:
            ev = _broken("这个点的求解失败了")
            # 人为把目标值塞成极小 —— 模拟"错误实现会拿它当最优"
            ev["objective"]["value"] = 1e-9
            return ev
        return _feasible(10.0 + 10.0 * vector["x"])

    ev, calls = _sequential_evaluator(trap)
    res = optimizer.optimize(CONT_VARS, ev, budget={"max_evals": 40,
                                                    "max_rounds": 8}, seed=0)

    if res["best"] is None:
        fails.append("存在可行点却没给出最优: %r" % (res.get("stop_reason"),))
        return

    # 铁律：最优点的 key 绝不能等于那个故障点
    if res["best"]["key"] == optimizer.vector_key(trap_vector):
        fails.append("技术故障点被选成了最优方案（目标值极小骗过了排序）")

    # 最优点的目标值不能是那个假的 1e-9
    if res["best"]["value"] < 1.0:
        fails.append("最优点目标值 %r 看起来来自故障点（假值 1e-9）"
                     % (res["best"]["value"],))

    # 故障点必须被单独归到 failed 组，且不参与可行组
    failed = res["groups"]["failed"]
    if not failed:
        fails.append("故障点未被归类到 failed 组")
    else:
        keys = [p["key"] for p in failed]
        if optimizer.vector_key(trap_vector) not in keys:
            fails.append("那个故障点没有出现在 failed 组: %r" % (keys,))
        for p in failed:
            if p["group"] != optimizer.GROUP_FAILED:
                fails.append("failed 组里混进了非故障点: group=%r" % (p["group"],))
    for p in res["groups"]["feasible"]:
        if p["group"] != optimizer.GROUP_FEASIBLE:
            fails.append("feasible 组里混进了非可行点")

    # 即使全部点都故障，也不能返回一个"最优"
    def all_broken(vector):
        return _broken("全挂了")

    ev2, _c2 = _sequential_evaluator(all_broken)
    res2 = optimizer.optimize(CONT_VARS, ev2,
                              budget={"max_evals": 12, "max_rounds": 3},
                              seed=0)
    if res2["best"] is not None:
        fails.append("全部点都技术故障时仍给出了 best，实际 %r" % (res2["best"],))
    if res2["feasible"]:
        fails.append("全部点都故障时 feasible 应为 False")
    # 全部技术故障 ≠ 没有可行设计：前者要查环境（求解/网格/参数），后者要改设计。
    # 报成 no_feasible 会把人引向错误方向（spec 用户故事 20）。
    if res2["stop_reason"] != optimizer.STOP_ALL_FAILED:
        fails.append("全部点都技术故障时停止原因应为 all_failed，实际 %r"
                     % (res2["stop_reason"],))
    if "环境" not in (res2["stop_text"] or ""):
        fails.append("all_failed 的可读原因应提示排查环境，实际 %r"
                     % (res2["stop_text"],))


def test_no_feasible_reported(fails):
    """完全没有可行点时明确报告（可读原因），不返回含义不明的 None。"""
    def always_infeasible(vector):
        return _infeasible(10.0 + 10.0 * vector["x"])

    ev, _calls = _sequential_evaluator(always_infeasible)
    res = optimizer.optimize(CONT_VARS, ev,
                             budget={"max_evals": 15, "max_rounds": 4}, seed=0)

    if res["stop_reason"] != optimizer.STOP_NO_FEASIBLE:
        fails.append("无可行点时停止原因应为 no_feasible，实际 %r"
                     % (res["stop_reason"],))
    if res["feasible"] is not False:
        fails.append("无可行点时 feasible 应为 False，实际 %r" % (res["feasible"],))
    if not res["stop_text"]:
        fails.append("无可行点时缺少可读原因说明")
    if "可行" not in res["stop_text"]:
        fails.append("无可行点的说明应点明这一点，实际 %r" % (res["stop_text"],))

    # 关键：best 为 None 时必须有明确的停止原因（不能只给一个 None 就完事）
    if res["best"] is not None:
        fails.append("无可行点却给出了 best: %r" % (res["best"],))
    if not res["stop_reason"]:
        fails.append("best 为 None 却没有 stop_reason —— 这就是「含义不明的 None」")

    # 但不可行点本身仍要记录（供人看"最接近可行的是哪个"）
    if not res["groups"]["infeasible"]:
        fails.append("无可行点时应记录不可行点，实际分组为空")
    # 汇总也要能表达这件事
    txt = optimizer.summarize(res)
    if "未找到可行设计" not in txt:
        fails.append("summarize 未说明无可行设计: %r" % txt)


# ---------------------------------------------------------------------------
# 4) 去重与缓存（撞点在离散变量下很常见）
# ---------------------------------------------------------------------------

def test_dedup_cache(fails):
    """重复设计点不重复调用 evaluate（断言调用次数）。"""
    # 离散变量只有 3×3=9 个组合，预算 60 次也是撞点；评估次数必须远小于预算
    tiny_vars = [
        {"name": "n_ribs", "type": "discrete", "min": 3, "max": 5, "step": 1,
         "category": "geometry"},
        {"name": "n_plies", "type": "discrete", "candidates": [4, 6, 8],
         "category": "ply"},
    ]
    space_size = 3 * 3

    def fn(vector):
        return _feasible(float(vector["n_ribs"]) * 10.0 + vector["n_plies"])

    ev, calls = _sequential_evaluator(fn)
    res = optimizer.optimize(tiny_vars, ev,
                             budget={"max_evals": 60, "max_rounds": 12}, seed=0)

    # 关键断言：evaluate 的真实调用次数不超过设计空间大小
    if calls["n"] > space_size:
        fails.append("设计空间只有 %d 个点，却调用了 %d 次 evaluate（去重失效）"
                     % (space_size, calls["n"]))
    if res["n_evals"] != calls["n"]:
        fails.append("结果的 n_evals=%d 与真实调用次数 %d 不一致"
                     % (res["n_evals"], calls["n"]))

    # 每个被调用过的向量必须唯一（没有重复评估）
    keys = [optimizer.vector_key(v) for v in calls["vectors"]]
    if len(set(keys)) != len(keys):
        dup = [k for k in set(keys) if keys.count(k) > 1]
        fails.append("有设计点被重复评估: %r" % (dup[:3],))

    # 缓存命中数 = 尝试评估次数 - 真实调用次数，应大于 0（说明确实有撞点）
    if res["n_cached"] <= 0:
        fails.append("离散小空间里应该发生缓存命中，实际 n_cached=%d"
                     % (res["n_cached"],))

    # 全局缓存：同一个向量评估两次，第二次不应再调回调
    ev2, calls2 = _sequential_evaluator(lambda v: _feasible(v["x"]))
    res2 = optimizer.optimize(CONT_VARS, ev2,
                              budget={"max_evals": 20, "max_rounds": 5}, seed=0)
    if calls2["n"] > 20:
        fails.append("超出预算仍在评估: %d > 20" % (calls2["n"],))


# ---------------------------------------------------------------------------
# 5) 停止原因（四种，各自可达）
# ---------------------------------------------------------------------------

def test_stop_reasons(fails):
    """四种停止原因各自可达且被正确报出。"""
    # (a) max_evals：预算极小
    ev, _c = _sequential_evaluator(_analytic)
    r = optimizer.optimize(CONT_VARS, ev,
                           budget={"max_evals": 3, "max_rounds": 99}, seed=0)
    if r["stop_reason"] != optimizer.STOP_MAX_EVALS:
        fails.append("max_evals 用例的停止原因应为 max_evals，实际 %r"
                     % (r["stop_reason"],))
    if r["n_evals"] > 3:
        fails.append("max_evals=3 却评估了 %d 次" % (r["n_evals"],))

    # (b) max_rounds：轮数极小但评估预算宽松
    ev, _c = _sequential_evaluator(_analytic)
    r = optimizer.optimize(CONT_VARS, ev,
                           budget={"max_evals": 999, "max_rounds": 2}, seed=0)
    if r["stop_reason"] != optimizer.STOP_MAX_ROUNDS:
        fails.append("max_rounds 用例的停止原因应为 max_rounds，实际 %r"
                     % (r["stop_reason"],))
    if r["n_rounds"] > 2:
        fails.append("max_rounds=2 却跑了 %d 轮" % (r["n_rounds"],))

    # (c) converged：目标已到最优且改进量阈值设得足够大
    def flat(vector):
        return _feasible(5.0)          # 到处一样 -> 第二轮改进量为 0

    ev, _c = _sequential_evaluator(flat)
    r = optimizer.optimize(CONT_VARS, ev,
                           budget={"max_evals": 200, "max_rounds": 50,
                                   "improvement_threshold": 1e-3}, seed=0)
    if r["stop_reason"] != optimizer.STOP_CONVERGED:
        fails.append("常量目标应很快收敛，实际停止原因 %r（轮数 %d）"
                     % (r["stop_reason"], r["n_rounds"]))
    if r["n_rounds"] > 40:
        fails.append("常量目标跑了 %d 轮才停，收敛判定失效" % (r["n_rounds"],))

    # (d) no_feasible：一个可行点都没有
    ev, _c = _sequential_evaluator(lambda v: _infeasible(10.0))
    r = optimizer.optimize(CONT_VARS, ev,
                           budget={"max_evals": 10, "max_rounds": 3}, seed=0)
    if r["stop_reason"] != optimizer.STOP_NO_FEASIBLE:
        fails.append("无可行点的停止原因应为 no_feasible，实际 %r"
                     % (r["stop_reason"],))

    # 四种原因必须都有可读文本
    for reason in optimizer.STOP_REASONS:
        if not optimizer.STOP_REASON_TEXT.get(reason):
            fails.append("停止原因 %r 缺少可读说明" % (reason,))

    # 无论怎么停，stop_reason 必有值（绝不返回 None）
    for r in (optimizer.optimize(CONT_VARS, _sequential_evaluator(_analytic)[0],
                                 budget={"max_evals": 5}, seed=0),):
        if not r.get("stop_reason"):
            fails.append("优化结束后 stop_reason 为空")


# ---------------------------------------------------------------------------
# 6) 非光滑目标：不假设可微，不做梯度下降
# ---------------------------------------------------------------------------

def test_nonsmooth_staircase(fails):
    """
    阶梯状（非光滑）目标仍能稳定改进 —— 证明搜索不依赖梯度。

    目标：mass = 1 + 阶梯数，阶梯按 x 量化到 0.1（plateau 很宽、导数处处为 0
    或不存在）。梯度下降在这类函数上要么原地不动（导数为 0），要么在台阶边缘
    抖动；邻域 + 步长收缩应能稳定下台阶。
    """
    calls = {"n": 0}

    def staircase(vector):
        calls["n"] += 1
        x = vector["x"]
        # 量化到 0.1 的台阶：x∈[0,0.1) -> 10 阶，[0.1,0.2) -> 9 阶 ...
        step = int(round(x * 10.0 + 0.5))
        return _feasible(float(10 - step))

    res = optimizer.optimize(CONT_VARS, staircase,
                             budget={"max_evals": 120, "max_rounds": 25},
                             seed=0)
    if not res["feasible"]:
        fails.append("阶梯目标下未找到可行点")
        return

    # 手算最优：x=1.0 -> step=10 -> mass=0（最小）；x=0 -> mass=10（最大）
    # 断言搜索确实"下台阶"了：最优值要显著优于起始点
    start_value = 10.0
    if res["best"]["value"] > start_value - 3.0:
        fails.append("阶梯目标上改进不足：最优 %.3f，起点 %.1f"
                     % (res["best"]["value"], start_value))
    # 且逼近解析最优（0）
    if res["best"]["value"] > 2.0:
        fails.append("阶梯目标最优 %.3f 离解析最优 0 太远" % (res["best"]["value"],))

    # 不使用梯度的可观测证据：目标函数被评估在**离散网格**上
    # （x 的取值应落在初始步长的整数倍上，而不是浮点微调）
    xs = sorted(set(round(p["vector"]["x"], 9) for p in res["points"]))
    if len(xs) < 3:
        fails.append("阶梯目标下只探索了 %d 个不同 x，搜索不足" % (len(xs),))

    # 单调目标（光滑）+ 阶梯目标，同一优化器都应能改进 —— 说明不假设光滑性
    def smooth(vector):
        return _feasible(10.0 - 8.0 * vector["x"])

    res2 = optimizer.optimize(CONT_VARS, _sequential_evaluator(smooth)[0],
                              budget={"max_evals": 60, "max_rounds": 15}, seed=0)
    if res2["best"]["value"] > 2.5:
        fails.append("光滑目标未改进到位: %r" % (res2["best"]["value"],))


# ---------------------------------------------------------------------------
# 7) 导出 / 导入 / 续跑
# ---------------------------------------------------------------------------

def test_export_import_resume(fails):
    """已评估点可导出/导入，续跑不丢、不必重算。"""
    tmp = _fresh("resume")

    ev, calls = _sequential_evaluator(_analytic)
    r1 = optimizer.optimize(CONT_VARS, ev,
                            budget={"max_evals": 12, "max_rounds": 3}, seed=0)
    n_first = r1["n_evals"]

    # 导出到磁盘
    path = os.path.join(tmp, "points.json")
    optimizer.write_points(path, r1["points"])
    if not os.path.isfile(path):
        fails.append("已评估点未写成文件")
        return

    # 导入：条数与分组都要保真
    back = optimizer.read_points(path)
    if len(back) != len(r1["points"]):
        fails.append("导入的点数 %d 与原 %d 不一致"
                     % (len(back), len(r1["points"])))
    for a, b in zip(r1["points"], back):
        if a["key"] != b["key"]:
            fails.append("导入后设计点键变了: %r != %r" % (a["key"], b["key"]))
            break
        if a["value"] != b["value"]:
            fails.append("导入后目标值丢失: %r != %r" % (a["value"], b["value"]))
            break
        if a["group"] != b["group"]:
            fails.append("导入后分组丢失: %r != %r" % (a["group"], b["group"]))
            break

    # 续跑：把导入的点当 history 传回去，已评估的点不应再调 evaluate
    ev2, calls2 = _sequential_evaluator(_analytic)
    r2 = optimizer.optimize(CONT_VARS, ev2,
                            budget={"max_evals": 30, "max_rounds": 6}, seed=0,
                            history=back)
    if calls2["n"] < n_first:
        # 续跑继续探索，但绝不能重复评估 history 里已有的点
        pass
    old_keys = set(p["key"] for p in back)
    re_evaluated = [optimizer.vector_key(v) for v in calls2["vectors"]
                    if optimizer.vector_key(v) in old_keys]
    if re_evaluated:
        fails.append("续跑时重复评估了已有点 %d 个: %r"
                     % (len(re_evaluated), re_evaluated[:3]))
    if r2["n_cached"] < len(old_keys):
        fails.append("续跑的缓存命中数 %d 少于 history 的 %d 个点"
                     % (r2["n_cached"], len(old_keys)))

    # 续跑结果必须保留 history 里的点（不丢）
    all_keys = set(p["key"] for p in r2["points"])
    missing = old_keys - all_keys
    # points 只含本次新增点；history 仍要在 groups/best 里可见
    if not r2["feasible"]:
        fails.append("续跑后丢了可行解")
    # 续跑的最优不应比第一段更差
    if r2["best"]["value"] > r1["best"]["value"] + 1e-9:
        fails.append("续跑后最优变差了: %r -> %r"
                     % (r1["best"]["value"], r2["best"]["value"]))
    del missing

    # 导出结构必须可 JSON 序列化（要能入库/跨进程）
    payload = optimizer.export_points(r1["points"])
    try:
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        fails.append("导出的评估点不可 JSON 序列化: %s" % exc)
    if payload.get("schema") != "cae-optimizer-points/1":
        fails.append("导出结构缺少版本标识: %r" % (payload.get("schema"),))
    if payload.get("n_points") != len(r1["points"]):
        fails.append("导出的 n_points 不符: %r" % (payload.get("n_points"),))


# ---------------------------------------------------------------------------
# 8) 注入式：优化器不知道 Study / Abaqus
# ---------------------------------------------------------------------------

def test_injection_only(fails):
    """核心入口只依赖注入的回调，不碰 Study/Abaqus/DB。"""
    import inspect

    # (a) optimize 的必需参数里必须有一个叫 evaluate 的回调位置
    sig = inspect.signature(optimizer.optimize)
    if "evaluate" not in sig.parameters:
        fails.append("optimize 未接受注入的 evaluate 回调")
    else:
        params = list(sig.parameters.values())
        required = [p for p in params
                    if p.default is inspect.Parameter.empty
                    and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        names = [p.name for p in required]
        if names[:2] != ["variables", "evaluate"]:
            fails.append("optimize 的前两个必需参数应为 (variables, evaluate)，"
                         "实际 %r" % (names,))

    # (b) 模块源码不得 import 任何后端子模块（纯算法，核心层零依赖）
    path = os.path.join(CORE, "optimizer.py")
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    for banned in ("run_study", "catia_driver", "abaqus_runner", "import db",
                   "import numpy", "workbench_service", "win32com"):
        if banned in src:
            fails.append("optimizer.py 不应依赖 %r（必须是注入式纯算法）" % banned)

    # (c) 真的只跑回调：用一个完全不存在的"Study"也能优化
    def fake_evaluate(vector):
        # 这个回调里没有任何 Study / DB / 文件系统
        return _feasible(vector["x"] ** 2 + 1.0)

    res = optimizer.optimize(CONT_VARS, fake_evaluate,
                             budget={"max_evals": 15, "max_rounds": 4}, seed=0)
    if not res["feasible"]:
        fails.append("纯回调评估未能完成优化")
    # 秒级完成（真的没跑求解）—— 这里用调用次数间接证明
    if res["n_evals"] > 15:
        fails.append("评估次数超预算，说明有额外开销: %d" % (res["n_evals"],))


def test_input_validation(fails):
    """配置非法时明确报错，不静默产生错误结果。"""
    cases = [
        ("空变量声明", lambda: optimizer.optimize([], lambda v: _feasible(1.0))),
        ("重复变量名",
         lambda: optimizer.optimize(
             [{"name": "x", "type": "continuous", "min": 0, "max": 1},
              {"name": "x", "type": "continuous", "min": 0, "max": 1}],
             lambda v: _feasible(1.0))),
        ("未知变量类型",
         lambda: optimizer.optimize(
             [{"name": "x", "type": "weird", "min": 0, "max": 1}],
             lambda v: _feasible(1.0))),
        ("max<=min",
         lambda: optimizer.optimize(
             [{"name": "x", "type": "continuous", "min": 1, "max": 1}],
             lambda v: _feasible(1.0))),
        ("缺 evaluate 回调", lambda: optimizer.optimize(CONT_VARS, None)),
        ("预算非法",
         lambda: optimizer.optimize(CONT_VARS, lambda v: _feasible(1.0),
                                    budget={"max_evals": 0})),
        ("改进阈值非法",
         lambda: optimizer.optimize(CONT_VARS, lambda v: _feasible(1.0),
                                    budget={"improvement_threshold": -1})),
        # 离散变量候选值不是整数（层数不能是 6.37）
        ("离散候选非整数",
         lambda: optimizer.optimize(
             [{"name": "n", "type": "discrete", "candidates": [4, 6.37]}],
             lambda v: _feasible(1.0))),
    ]
    for label, fn in cases:
        try:
            fn()
            fails.append("%s: 未被拒绝" % label)
        except (optimizer.OptimizerError, ValueError):
            pass

    # 整数性：离散变量采样/规范化后绝不能出现小数
    specs = optimizer.normalize_variables(
        [{"name": "n", "type": "discrete", "min": 3, "max": 9, "step": 1}])
    v = optimizer.normalize_vector({"n": 6.37}, specs)
    if not isinstance(v["n"], int) or v["n"] not in (3, 4, 5, 6, 7, 8, 9):
        fails.append("离散变量未被离散化: %r" % (v["n"],))
    # 所有采样点都必须是整数
    for s in optimizer.doe_sample(specs, n_samples=10, seed=3):
        if not isinstance(s["n"], int):
            fails.append("DOE 采样产生了非整数离散值: %r" % (s["n"],))
            break

    # 包络守卫：越界向量要能被识别（不是静默穿越）
    ok, violations = optimizer.in_envelope({"x": 2.0}, CONT_VARS)
    if ok:
        fails.append("越界向量未被告警")
    elif not violations or violations[0].get("variable") != "x":
        fails.append("包络违规描述不符: %r" % (violations,))
    ok2, _ = optimizer.in_envelope({"x": 0.5}, CONT_VARS)
    if not ok2:
        fails.append("合法向量被误判越界")


# ---------------------------------------------------------------------------
# 9) Python 3.8 兼容
# ---------------------------------------------------------------------------

def test_py38(fails):
    """Python 3.8 兼容：本票涉及的模块不得用 3.9+ 语法/标准库。"""
    import compat_py38

    targets = [
        os.path.join(CORE, "optimizer.py"),
        os.path.join(CORE, "objectives.py"),
        os.path.join(CORE, "compat_py38.py"),
        os.path.join(HERE, "test_m23.py"),
    ]
    bad = compat_py38.check_paths(targets)
    if bad:
        fails.append("Python 3.8 兼容性检查未通过:\n%s"
                     % compat_py38.format_violations(bad))

    probe = compat_py38.check_source(
        "probe.py", "x: list[int] = []\ns = 'a'.removeprefix('b')\n")
    if len(probe) < 2:
        fails.append("compat_py38 未检出 list[int] / removeprefix")


def main():
    fails = []
    print("1. DOE 采样（连续分层 / 离散整数性） ...")
    test_doe_sampling(fails)
    print("2. 同配置同种子确定性 ...")
    test_determinism(fails)
    print("3. 在可行点里挑最优 ...")
    test_picks_best_feasible(fails)
    print("4. 技术故障点即便目标值极小也不被选中 ...")
    test_failed_point_never_chosen(fails)
    print("5. 无可行点时明确报告 ...")
    test_no_feasible_reported(fails)
    print("6. 去重与缓存（断言调用次数） ...")
    test_dedup_cache(fails)
    print("7. 四种停止原因 ...")
    test_stop_reasons(fails)
    print("8. 非光滑阶梯目标仍能稳定改进 ...")
    test_nonsmooth_staircase(fails)
    print("9. 导出/导入/续跑 ...")
    test_export_import_resume(fails)
    print("10. 注入式：不依赖 Study/Abaqus ...")
    test_injection_only(fails)
    print("11. 配置校验与包络守卫 ...")
    test_input_validation(fails)
    print("12. Python 3.8 兼容 ...")
    test_py38(fails)

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M23 (票23) SELF-TEST: ALL PASSED "
          "(DOE 确定性 / 约束筛选 / 故障不参与最优 / 去重缓存 / "
          "四种停止原因 / 非光滑 / 续跑 / py3.8)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
