# -*- coding: utf-8 -*-
"""
test_m27.py -- 票27 验收：优化闭环接入代理模型快筛（可选开关）

验收点（对应 issue #27 / ADR-0002）：
  1. 代理模型作为**可选开关**暴露，**默认关闭**；关闭时闭环行为与 #25 一致
  2. 累积到最少样本数后自动启用；样本不足时明确不启用并说明原因
  3. 误差不达标时自动降级为不排序，原因在汇总中可见（不静默）
  4. 汇总能区分「代理模型建议」与「确定性搜索」来源
  5. **代理模型绝不改变任一设计点的合格判定**（用"说谎的代理"断言）
  6. 启用快筛不改变"最终最优点必须是实测可行点"这条性质
  7. Python 3.8 兼容

## 本票最关键的断言怎么写

"代理不影响合格判定"最容易写成空转：跑一遍，看到 best 可行，就宣布过关。
但那种断言在"代理偷偷改了判定"时**仍然会通过**（只要改得巧）。所以这里用
**说谎的代理**：一个把预测值故意搞反的模型。若合格判定真被代理影响，
最优点就会变成"代理说好的那个"而不是"实测最轻的可行点"；反之，最优点、
每点的 feasible、分组计数都必须与**纯确定性路径逐点一致**。

## 为什么关掉快筛要与 #25 完全一致

同一预算 + 同一 seed 下，关闭快筛的轨迹必须逐点相同（点的向量序列、分组、
最优点）。这既是"默认关闭"的验收项，也是"快筛不是悄悄改行为的暗通道"的证据。

运行 (在 apps/workbench 下): python tests/test_m27.py
"""

import os
import sys
import json
import shutil
import inspect

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
sys.path.insert(0, CORE)
sys.path.insert(0, WORKBENCH)

import db
import study
import surrogate
import optimizer
import objectives
import design_vector
import optimization_loop as optloop
from run_study import RunOptions
from catia_driver import FakeCatiaDriver
from abaqus_runner import FakeAbaqusRunner


TMP_BASE = os.path.join(WORKBENCH, ".tmp_test", "m27")
TMP = os.path.join(TMP_BASE, "run_%d" % os.getpid())

TEMPLATE_ID = "wingbox_v1"

# 设计向量声明：**变量数刻意压到 4 个**。
#
#  1. 功能上：`surrogate.min_samples_for()` = max(n_vars+2, 4)，变量少才可能在
#     小预算测试里真的攒够样本、走到"拟合成功 -> 激活排序"这条分支。
#  2. 成本上：每个设计点都要跑完整 Study（建模+拓扑+网格+求解+校核），
#     约 6 秒/点；验收要覆盖多条策略，必须把点数控制在能跑完的规模。
DV_SPEC = {
    "id": "m27_dv_v1",
    "template_id": TEMPLATE_ID,
    "variables": [
        {"name": "span", "category": "geometry", "type": "continuous",
         "min": 5.0, "max": 7.0, "default": 5.0},
        {"name": "n_plies", "category": "ply", "type": "discrete",
         "min": 6, "max": 10, "step": 2, "default": 8},
        {"name": "angle_0", "category": "ply", "type": "discrete",
         "candidates": [0, 1], "default": 0},
        {"name": "ply_thickness", "category": "ply", "type": "continuous",
         "min": 0.09, "max": 0.13, "default": 0.1},
    ],
    "ply_sequence": {
        "n_plies": "n_plies",
        "angles": ["angle_0"],
        "thickness": "ply_thickness",
        "material": "CFRP",
        "order": "cyclic",
        "angle_table": [0.0, 45.0],
    },
}

INTENT = {
    "objective": {"name": "mass", "direction": "minimize"},
    "constraints": [{"name": "strength", "require": "all_pass"}],
    "note": "M27 单测：强度合格前提下减重",
}

BUDGET = {"max_evals": 10, "max_rounds": 2, "improvement_threshold": 1e-6}
RULES = {"criteria": ["tsai_wu"], "safety_factor": 1.0}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

class CountingCatiaDriver(FakeCatiaDriver):
    """计数 fake 驱动（本票不重点测 CATIA，但保持与 #25 一致的口径）。"""

    def __init__(self, out_dir, fail_on=None):
        FakeCatiaDriver.__init__(self, out_dir, fail_on=fail_on)
        self.calls = 0

    def regenerate(self, parameter_set, template):
        self.calls += 1
        return FakeCatiaDriver.regenerate(self, parameter_set, template)


class LyingSurrogate(object):
    """
    **说谎的代理模型**：专门把"必然不合格"的设计排到最前面。

    为什么这样设计（这是本票测试有效性的关键）：

      * 目标是质量最小化，而质量 ∝ 层数 × 单层厚度。于是**最薄**的设计质量最小、
        最"诱人"，同时应力最高、**大概率不合格**。一个诚实的好代理若只看目标值，
        就会把它们排前面；而这里故意这样做 —— 这就是"说谎"。
      * 它使代理预取到的点里**含有不可行点**，于是"代理偷偷把 feasible 改成 True"
        这种篡改一定会被抓住（若预取点全都本来就可行，篡改没有任何可观测差别 ——
        实测中那个变异正是这样存活过一轮的）。

    它满足 `surrogate.fit` 的契约（available/degraded/source/reason/quality/
    thresholds 等字段齐全，且**不含任何合格判定字段**），所以拿它替换真实拟合
    是公平的：测的是调用侧的边界，不是模型本身的准确性。
    """

    def __init__(self, flip=True, claim_available=True):
        self.flip = flip
        self.claim_available = claim_available
        self.calls = 0

    def __call__(self, points, variables=None, max_mae_ratio=None,
                 max_rmse_ratio=None):
        self.calls += 1
        usable, n_excluded = surrogate.usable_points(points)
        values = [float(p["value"]) for p in usable]
        span = (max(values) - min(values)) if values else 0.0

        # 预测 = Σ (+1)·标准化特征：层数/厚度越大 -> 预测越大 -> 排序越靠后，
        # 于是**最薄（最可能不合格）的设计被排到最前面**。
        # 注意变量名是数据驱动的，这里不假设具体变量，只按"取值越大越重"处理。
        variables = list(variables or [])
        coef = []
        scaling = {}
        for spec in variables:
            lo = float(spec.get("min", 0.0))
            hi = float(spec.get("max", 1.0))
            center = (lo + hi) / 2.0
            span_i = (hi - lo) if hi > lo else 1.0
            scaling[spec["name"]] = {"center": center, "span": span_i}
            coef.append(1.0 if self.flip else -1.0)

        return {
            "available": self.claim_available,
            "degraded": not self.claim_available,
            "source": surrogate.SOURCE_SURROGATE,
            "reason": None if self.claim_available else "说谎代理：自称不可用",
            "n_samples": len(usable),
            "n_excluded": n_excluded,
            "n_variables": len(variables),
            "min_samples": surrogate.min_samples_for(variables),
            "variables": variables,
            "scaling": scaling,
            "coefficients": [0.0] + coef,
            "quality": {"mae": 0.0, "rmse": 0.0, "max_abs": 0.0,
                        "mae_ratio": 0.0, "rmse_ratio": 0.0,
                        "target_span": span, "n_folds": len(usable)},
            "thresholds": {"max_mae_ratio": max_mae_ratio,
                           "max_rmse_ratio": max_rmse_ratio},
            "ranking": [],
        }


class ThicknessAwareRunner(FakeAbaqusRunner):
    """
    让**合格与否真的随设计而变**的求解器替身。

    为什么需要它：标准 `FakeAbaqusRunner` 的应力是写死的（或对所有点统一注入
    热点），于是"所有点都可行"或"所有点都不可行"——在这种数据上，
    "代理偷偷把 feasible 篡改成 True"**看不出任何差别**，断言就成了空转
    （实测：这个变异存活过一轮，正是因为这个缺口）。

    这里的做法：从 .inp 的壳截面里读出**总厚度**，令应力 ∝ 1/厚度 ——
    薄的设计应力高、可能不合格，厚的合格。于是同一批设计点里**既有可行也有不可行**，
    合格判定才有区分度，篡改判定才一定会被抓住。

    注意：这仍然完全来自"实测"（读的是实际写进 .inp 的铺层），
    代理模型看不到厚度与应力的对应关系，只能瞎猜排序。
    """

    #: 应力 = STRESS_K / 总厚度(m)。
    #: 0.14 是实测标定值：铺层厚度跨 0.54mm..1.30mm 时，同一批设计点里
    #: 大致一半可行、一半不可行（4 可行 / 4 不可行），合格判定才有区分度。
    #: 取得太小（如 0.10）会全部合格、太大（0.35）会全部不合格 —— 两种极端
    #: 都会让"代理篡改判定"的变异观察不到。
    STRESS_K = 0.14

    def solve(self, inp_path, job_name, work_dir, step_name=None, analysis=None):
        self._thickness = _inp_total_thickness(inp_path)
        return FakeAbaqusRunner.solve(self, inp_path, job_name, work_dir,
                                      step_name=step_name, analysis=analysis)

    def extract(self, odb_path, out_dir, step_name=None):
        res = FakeAbaqusRunner.extract(self, odb_path, out_dir,
                                       step_name=step_name)
        thickness = getattr(self, "_thickness", None)
        if not thickness:
            return res
        # 按厚度重写应力列：薄的应力高（可能超许用值），厚的低
        stress = self.STRESS_K / float(thickness)
        if not os.path.isfile(res.csv_path):
            return res
        lines, header, rows = [], None, []
        with open(res.csv_path, "r", encoding="utf-8") as fh:
            raw = fh.read().splitlines()
        if not raw:
            return res
        header = raw[0]
        for line in raw[1:]:
            parts = line.split(",")
            if len(parts) >= 7 and parts[0] == "S":
                # c1 分量用统一应力，c2 取 20%（同 fake 的口径比例）
                factor = 1.0 if parts[5] == "c1" else 0.2
                parts[6] = "%.6f" % (stress * factor)
                line = ",".join(parts)
            rows.append(line)
        with open(res.csv_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(header + "\n")
            for line in rows:
                fh.write(line + "\n")
        return res


def _inp_total_thickness(inp_path):
    """从 .inp 的 `*SHELL SECTION, ... COMPOSITE` 块累加各层厚度（mm -> m）。"""
    total = 0.0
    in_section = False
    try:
        with open(inp_path, "r", encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("*SHELL SECTION"):
                    in_section = True
                    continue
                if s.startswith("*"):
                    in_section = False
                    continue
                if in_section and s:
                    head = s.split(",")[0].strip()
                    try:
                        total += float(head)
                    except ValueError:
                        pass
    except (IOError, OSError):
        return None
    if total <= 0.0:
        return None
    return total * 1e-3        # 平台铺层厚度口径是 mm


def _fresh(name):
    d = os.path.join(TMP, name)
    if os.path.isdir(d):
        try:
            shutil.rmtree(d)
        except OSError:
            pass
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def _fresh_db(name):
    d = _fresh(name)
    return db.connect(os.path.join(d, "m27.db"))


def _run(conn, tmp, sid, surrogate_policy=None, seed=0, budget=None,
         max_evals=None, abaqus=None, inject_hotspot=False, intent=None):
    """建会话 + 跑优化，返回 (session_id, summary)。"""
    catia = CountingCatiaDriver(os.path.join(tmp, "catparts"))
    session = optloop.create_session(
        conn, DV_SPEC, intent=intent or INTENT, budget=budget or BUDGET,
        name="M27 " + sid, session_id=sid, surrogate=surrogate_policy)
    summary = optloop.run_optimization(
        conn, session["session_id"], catia,
        options=None, work_dir=os.path.join(tmp, "work"),
        abaqus=abaqus or FakeAbaqusRunner(n_elems=6, n_plies=2,
                                          inject_hotspot=inject_hotspot),
        seed=seed, max_evals=max_evals, surrogate=surrogate_policy)
    return session["session_id"], summary


#: 跑一次优化 ≈ 6 秒/点（每点一个完整 Study）。所以"同预算对照"这类需要
#: 两次一致轨迹的断言，统一复用下面的共享会话，避免重复烧同样的算力。
_CACHE = {}


def _baseline_pair(conn, tmp):
    """一对**同预算同 seed** 的会话：默认（不传策略）与显式关闭。"""
    if "pair" not in _CACHE:
        sum_a = _run(conn, tmp, "OPT-M27-OFF-A", surrogate_policy=None,
                     seed=0, max_evals=6)[1]
        sum_b = _run(conn, tmp, "OPT-M27-OFF-B",
                     surrogate_policy={"enabled": False}, seed=0,
                     max_evals=6)[1]
        _CACHE["pair"] = (sum_a, sum_b)
    return _CACHE["pair"]


def _on_off_pair(conn, tmp):
    """
    一对同预算会话：关闭 vs 启用（**说谎代理**），被两组核心断言共用。

    两者都用 `ThicknessAwareRunner`：它让合格与否**真的随设计变化**
    （薄铺层应力高 -> 不可行，厚铺层合格）。否则所有点同判定，
    "代理篡改 feasible"这种变异就观察不到，断言会变成空转。
    """
    if "onoff" not in _CACHE:
        sum_off = _run(conn, tmp, "OPT-M27-V-OFF", surrogate_policy=None,
                       seed=0, max_evals=10,
                       abaqus=ThicknessAwareRunner(n_elems=6, n_plies=2))[1]
        liar = LyingSurrogate(flip=True)
        sum_on = _run(conn, tmp, "OPT-M27-V-ON",
                      surrogate_policy={"enabled": True, "prefetch": 2,
                                        "fit": liar},
                      seed=0, max_evals=10,
                      abaqus=ThicknessAwareRunner(n_elems=6, n_plies=2))[1]
        _CACHE["onoff"] = (sum_off, sum_on, liar)
    return _CACHE["onoff"]


def _trace(summary):
    """轨迹指纹：向量序列 + 分组（用于"默认关闭 == #25"的逐点对照）。"""
    return [(p["vector_key"], p["group_name"], p["source"])
            for p in summary["points"]]


def _classifications(summary):
    """每个设计点的合格判定（feasible）与目标值，按向量键索引。"""
    out = {}
    for p in summary["points"]:
        out[p["vector_key"]] = {
            "ok": p["ok"], "feasible": p["feasible"],
            "group": p["group_name"], "value": p["objective_value"],
        }
    return out


# ---------------------------------------------------------------------------
# 1) 默认关闭：与 #25 纯确定性路径逐点一致
# ---------------------------------------------------------------------------

def test_default_off_matches_baseline(fails):
    """1) 不传 surrogate（默认）与显式关闭，轨迹逐点一致。"""
    conn = _fresh_db("default_off")
    try:
        tmp = _fresh("default_off")
        sum_a, sum_b = _baseline_pair(conn, tmp)

        state_a = sum_a["surrogate"]
        state_b = sum_b["surrogate"]
        if state_a.get("enabled"):
            fails.append("默认（不传 surrogate）应视为关闭，实际 %r"
                         % (state_a.get("enabled"),))
        if state_a.get("status") != optloop.SURROGATE_OFF:
            fails.append("默认关闭时状态应为 off，实际 %r"
                         % (state_a.get("status"),))
        if state_a.get("status") != state_b.get("status"):
            fails.append("默认与显式关闭的快筛状态不一致")
        if state_a.get("n_prefetched"):
            fails.append("关闭时不应预取任何点，实际 %d"
                         % (state_a.get("n_prefetched"),))

        # ---- 轨迹逐点一致（同预算同 seed） ----
        trace_a, trace_b = _trace(sum_a), _trace(sum_b)
        if len(trace_a) != len(trace_b):
            fails.append("默认/显式关闭的点数不同: %d vs %d"
                         % (len(trace_a), len(trace_b)))
        for i, (ra, rb) in enumerate(zip(trace_a, trace_b)):
            if ra != rb:
                fails.append("默认/显式关闭第 %d 点轨迹不同: %r != %r"
                             % (i + 1, ra, rb))
                break

        # 关闭时所有点来源都不是 surrogate
        for p in sum_a["points"]:
            if p["source"] == optloop.SOURCE_SURROGATE:
                fails.append("关闭快筛时出现了代理来源的点: %r" % (p["vector_key"],))
                break
        if sum_a["surrogate"]["by_source"].get(optloop.SOURCE_SURROGATE):
            fails.append("关闭时的 by_source 不应有 surrogate 计数")

        # 文本摘要明确说明代理快筛状态（不可静默省略）
        text = optloop.summarize_text(sum_a)
        if "代理快筛" not in text:
            fails.append("文本摘要未提及代理快筛状态（不可静默省略）: %r" % (text,))

        # 开关也被记进会话意图快照（可追溯"当初有没有启用"）
        sess = optloop.get_session(conn, sum_a["session_id"])
        surro_intent = (sess["intent"] or {}).get("surrogate") or {}
        if surro_intent.get("enabled") is not False:
            fails.append("会话意图快照未记录快筛已关闭: %r" % (surro_intent,))
    finally:
        conn.close()


def test_off_equals_baseline_across_seeds(fails):
    """1b) 换一个 seed，关闭快筛仍与不传策略一致（不是碰巧一致）。"""
    conn = _fresh_db("seeds")
    try:
        tmp = _fresh("seeds")
        seed = 5
        s1 = _run(conn, tmp, "OPT-M27-SEED-A", surrogate_policy=None,
                  seed=seed, max_evals=4)[1]
        s2 = _run(conn, tmp, "OPT-M27-SEED-B",
                  surrogate_policy={"enabled": False}, seed=seed,
                  max_evals=4)[1]
        if _trace(s1) != _trace(s2):
            fails.append("seed=%d 时默认与显式关闭的轨迹不同" % seed)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2) 样本不足时不启用（并说明）
# ---------------------------------------------------------------------------

def test_waiting_when_not_enough_samples(fails):
    """2) 启用快筛但样本不够 -> 明确不启用并给出原因（不是静默跳过）。"""
    conn = _fresh_db("waiting")
    try:
        tmp = _fresh("waiting")
        variables = optloop.optimizer_variables(DV_SPEC)
        min_samples = surrogate.min_samples_for(variables)

        # max_evals=1：连攒样本的余量都没有，肯定到不了 min_samples
        summary = _run(
            conn, tmp, "OPT-M27-WAIT",
            surrogate_policy={"enabled": True, "prefetch": 2},
            seed=0, max_evals=1)[1]

        state = summary["surrogate"]
        if state.get("status") != optloop.SURROGATE_WAITING:
            fails.append("样本不足时状态应为 waiting，实际 %r（样本 %r，需 %r）"
                         % (state.get("status"), state.get("n_samples"),
                            state.get("min_samples")))
        if state.get("available"):
            fails.append("样本不足时不应宣称代理可用")
        reason = state.get("reason") or ""
        if not reason:
            fails.append("样本不足时必须说明原因（不静默）")
        elif "样本" not in reason:
            fails.append("样本不足的原因不可读: %r" % (reason,))
        if state.get("n_prefetched"):
            fails.append("样本不足时不应预取点，实际 %d" % (state.get("n_prefetched"),))
        if state.get("min_samples") != min_samples:
            fails.append("快筛状态里的 min_samples 应为 %d，实际 %r"
                         % (min_samples, state.get("min_samples")))
        text = optloop.summarize_text(summary)
        if "代理快筛" not in text:
            fails.append("文本摘要未提及快筛状态")
    finally:
        conn.close()


def test_policy_validation(fails):
    """2b) 快筛策略非法值早报（不跑到一半才炸）。"""
    good = optloop.normalize_surrogate_policy(None)
    if good.get("enabled") is not False:
        fails.append("默认策略必须 enabled=False（票27 验收项）")

    for bad in ({"prefetch": "many"}, {"max_mae_ratio": 0},
                {"max_rmse_ratio": -1}):
        try:
            optloop.normalize_surrogate_policy(bad)
            fails.append("非法快筛策略未被拒绝: %r" % (bad,))
        except optloop.OptimizationLoopError:
            pass

    try:
        optloop.normalize_surrogate_policy("on")
        fails.append("非 dict 快筛策略未被拒绝")
    except optloop.OptimizationLoopError:
        pass


# ---------------------------------------------------------------------------
# 3) 自动启用 + 预取点带 surrogate 来源
# ---------------------------------------------------------------------------

def test_auto_enable_and_source(fails):
    """3) 累积够样本后自动拟合启用；预取的点来源标为 surrogate。"""
    conn = _fresh_db("auto")
    try:
        tmp = _fresh("auto")
        variables = optloop.optimizer_variables(DV_SPEC)
        min_samples = surrogate.min_samples_for(variables)

        sid, summary = _run(
            conn, tmp, "OPT-M27-AUTO",
            surrogate_policy={"enabled": True, "prefetch": 2},
            seed=0, max_evals=10)

        state = summary["surrogate"]
        if not state.get("enabled"):
            fails.append("启用快筛时状态应为 enabled=True: %r" % (state,))
        if state.get("status") not in (optloop.SURROGATE_ACTIVE,
                                       optloop.SURROGATE_DEGRADED):
            fails.append("样本够后应进入 active 或 degraded，实际 %r（样本 %r，需 %r）"
                         % (state.get("status"), state.get("n_samples"),
                            min_samples))

        # 汇总的 by_source 能区分来源
        by_source = state.get("by_source") or {}
        if not by_source:
            fails.append("汇总缺少 by_source（无法区分代理建议与确定性搜索）")
        for p in summary["points"]:
            if p["source"] not in by_source:
                fails.append("点来源 %r 未出现在 by_source 里" % (p["source"],))
                break

        if state.get("status") == optloop.SURROGATE_ACTIVE:
            # 真启用了：应当有代理预取的点（否则"启用"是空话）
            if not state.get("n_prefetched"):
                fails.append("快筛 active 但没有预取任何点（启用形同虚设）")
            if not by_source.get(optloop.SOURCE_SURROGATE):
                fails.append("active 时 by_source 里应有 surrogate 来源的点")
            if not state.get("model_quality"):
                fails.append("active 时应带模型误差（quality）")
            text = optloop.summarize_text(summary)
            if "参与排序" not in text:
                fails.append("active 时文本摘要应说明参与排序: %r" % (text,))

        # 预取的点也是真的跑完了 Study（不是只做了预测）
        for p in summary["points"]:
            if p["source"] != optloop.SOURCE_SURROGATE:
                continue
            if not p["study_id"]:
                fails.append("代理预取的点没有关联 Study（应真的跑 Study）")
            for kind in ("parameter_set", "topology", "check_result"):
                if kind not in (p["artifacts"] or {}):
                    fails.append("代理预取的点缺产物 %r（说明没真跑全链）" % (kind,))
                    break

        # ---- 快筛状态随会话落库，可由独立查询接口读回 ----
        report = optloop.surrogate_report(conn, sid)
        if not report.get("enabled"):
            fails.append("落库的快筛状态丢了 enabled")
        if report.get("status") != state.get("status"):
            fails.append("独立查询的状态与汇总不一致: %r != %r"
                         % (report.get("status"), state.get("status")))
        if "by_source" not in report:
            fails.append("快筛报告缺 by_source")
        if not report.get("text"):
            fails.append("快筛报告缺可读摘要")

        # ---- 预算必须被共享遵守 ----
        # 快筛把闭环切成"攒样本 + 预取 + 正式搜索"几段，每段都会调 optimizer，
        # 而 optimize 的 max_evals 是**每次调用**的上限。若各段各自给满预算，
        # 实际评估次数就会超过用户设定的预算（多烧真实求解 = 真金白银）。
        # 这里直接对"总点数"断言上界。
        budget_max = BUDGET["max_evals"]
        if summary["n_points"] > budget_max:
            fails.append("实际评估点数 %d 超过预算 max_evals=%d（各段各自给满预算了）"
                         % (summary["n_points"], budget_max))
    finally:
        conn.close()


def test_prefetch_is_diversified(fails):
    """3b) 预取点做分散化：不该把有限的评估次数重复烧在同一小块区域。"""
    variables = optloop.optimizer_variables(DV_SPEC)

    # 造一批"排序靠前但彼此极近"的候选 + 一个远处的候选
    near = [{"vector": {"span": 5.0 + 0.001 * i, "n_plies": 8, "angle_0": 0,
                        "ply_thickness": 0.1},
             "key": "near-%d" % i, "predicted": float(i)} for i in range(5)]
    far = {"vector": {"span": 7.0, "n_plies": 10, "angle_0": 1,
                      "ply_thickness": 0.13},
           "key": "far", "predicted": 100.0}
    picked = optloop.diversify_candidates(near + [far], variables, 2)

    if len(picked) != 2:
        fails.append("分散化应挑出 2 个候选，实际 %d" % len(picked))
    else:
        keys = [p["key"] for p in picked]
        if "far" not in keys:
            fails.append("分散化没有把远处的候选选进来（挤成一团了）: %r" % (keys,))

    # limit 大于候选数时按原排序补齐，不报错
    few = optloop.diversify_candidates(near[:1], variables, 3)
    if len(few) != 1:
        fails.append("候选不足时分散化应返回现有候选，实际 %d" % len(few))

    # limit=0 返回空（预取开关为 0 时不预取）
    if optloop.diversify_candidates(near, variables, 0) != []:
        fails.append("limit=0 时应返回空列表")


def test_surrogate_report_persisted(fails):
    """3c) 关闭的会话也能查快筛状态（返回 off，不抛错）。"""
    conn = _fresh_db("report")
    try:
        tmp = _fresh("report")
        sid_off = _run(conn, tmp, "OPT-M27-REPORT-OFF", surrogate_policy=None,
                       seed=0, max_evals=2)[0]
        rep_off = optloop.surrogate_report(conn, sid_off)
        if rep_off.get("status") != optloop.SURROGATE_OFF:
            fails.append("关闭的会话查询状态应为 off，实际 %r"
                         % (rep_off.get("status"),))
        if not rep_off.get("text"):
            fails.append("关闭的会话查询也应给出可读摘要")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4) 误差不达标 / 拟合出错 -> 自动降级
# ---------------------------------------------------------------------------

def test_degrade_when_error_too_large(fails):
    """4) 误差不达标/拟合出错时自动降级为不排序，原因可见（不阻塞）。"""
    conn = _fresh_db("degrade")
    try:
        tmp = _fresh("degrade")

        # (a) 拟合成功但自称不可用 = 模拟"误差超标"
        liar = LyingSurrogate(claim_available=False)
        summary = _run(
            conn, tmp, "OPT-M27-DEGRADE",
            surrogate_policy={"enabled": True, "prefetch": 2, "fit": liar},
            seed=0, max_evals=10)[1]

        state = summary["surrogate"]
        if state.get("status") != optloop.SURROGATE_DEGRADED:
            fails.append("误差不达标时应降级（degraded），实际 %r"
                         % (state.get("status"),))
        if state.get("available"):
            fails.append("降级时不应宣称代理可用")
        if not state.get("reason"):
            fails.append("降级必须给出原因（不静默）")
        # 原因必须**带上代理自己说的具体原因**，而不是一句泛泛的"不可用"：
        # 只要求"非空"是空转断言（换成任何一句废话都能过）。
        if "自称不可用" not in (state.get("reason") or ""):
            fails.append("降级原因丢了具体信息（应原样带出代理给出的原因）: %r"
                         % (state.get("reason"),))
        if state.get("n_prefetched"):
            fails.append("降级时不应预取点，实际 %d" % (state.get("n_prefetched"),))
        if summary["status"] != optloop.SESSION_FINISHED:
            fails.append("降级后会话应正常结束，实际 %r" % (summary["status"],))
        if not summary["points"]:
            fails.append("降级后仍应有设计点")
        text = optloop.summarize_text(summary)
        if "降级" not in text:
            fails.append("文本摘要应说明已降级: %r" % (text,))
        for p in summary["points"]:
            if p["source"] == optloop.SOURCE_SURROGATE:
                fails.append("降级时不应出现代理预取点")
                break

        # (b) 拟合本身抛错也不得中断优化
        def _boom(*_args, **_kwargs):
            raise surrogate.SurrogateError("模拟拟合失败：设计矩阵奇异")

        summary2 = _run(
            conn, tmp, "OPT-M27-BOOM",
            surrogate_policy={"enabled": True, "prefetch": 2, "fit": _boom},
            seed=0, max_evals=10)[1]

        state2 = summary2["surrogate"]
        if state2.get("status") != optloop.SURROGATE_DEGRADED:
            fails.append("拟合抛错时应降级，实际 %r" % (state2.get("status"),))
        if "奇异" not in (state2.get("reason") or ""):
            fails.append("降级原因应带上拟合失败的原始信息: %r"
                         % (state2.get("reason"),))
        if summary2["status"] != optloop.SESSION_FINISHED:
            fails.append("拟合出错后会话仍应正常结束，实际 %r"
                         % (summary2["status"],))
        if not summary2["points"]:
            fails.append("拟合出错后仍应有设计点（搜索不能停）")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5) 【核心】代理绝不改变合格判定
# ---------------------------------------------------------------------------

def test_surrogate_never_changes_verdict(fails):
    """
    5) 用**说谎的代理**断言：代理只影响顺序，不影响任何点的合格判定。

    同一份设计向量空间、同预算同 seed，跑一次关闭 + 一次启用（说谎代理），
    然后对**两个会话共有**的设计向量逐一比对 ok / feasible / group / 目标值。
    """
    conn = _fresh_db("verdict")
    try:
        tmp = _fresh("verdict")
        sum_off, sum_on, liar = _on_off_pair(conn, tmp)

        if liar.calls == 0:
            fails.append("前置条件不成立：说谎代理从未被调用（样本没攒够？）")

        off_map = _classifications(sum_off)
        on_map = _classifications(sum_on)
        common = set(off_map) & set(on_map)
        if not common:
            fails.append("两次跑没有共同的设计点，无法对照判定")

        # ---- 先确认这批数据**本身有区分度** ----
        # 若所有点判定相同（全可行或全不可行），"代理篡改判定"就观察不到，
        # 下面那串逐点比对会全部空转。实测中这个缺口让变异存活过一轮，
        # 所以把"数据必须有区分度"本身也变成断言。
        verdicts = set((v["ok"], v["feasible"]) for v in off_map.values())
        if len(verdicts) < 2:
            fails.append("前置条件不成立：关闭会话里所有点的判定都相同 %r，"
                         "无法检验代理是否篡改判定（需要可行与不可行混合的数据）"
                         % (verdicts,))
        groups_seen = set(v["group"] for v in off_map.values())
        if not (optloop.GROUP_FEASIBLE in groups_seen
                or optloop.GROUP_INFEASIBLE in groups_seen):
            fails.append("关闭会话里既没有可行点也没有不可行点: %r"
                         % (groups_seen,))

        for key in sorted(common):
            a, b = off_map[key], on_map[key]
            if a["ok"] != b["ok"]:
                fails.append("代理改变了点的 ok 判定: %s %r != %r"
                             % (key, a["ok"], b["ok"]))
            if a["feasible"] != b["feasible"]:
                fails.append("代理改变了点的可行判定: %s %r != %r"
                             % (key, a["feasible"], b["feasible"]))
            if a["group"] != b["group"]:
                fails.append("代理改变了点的分组: %s %r != %r"
                             % (key, a["group"], b["group"]))
            if a["value"] is not None and b["value"] is not None:
                if abs(float(a["value"]) - float(b["value"])) > 1e-9:
                    fails.append("同一个设计向量的目标值不同: %s %r != %r"
                                 % (key, a["value"], b["value"]))

        # ---- 代理预取的点：判定必须与**它自己的校核产物**一致 ----
        #
        # 这一条是上面"共有向量逐点比对"的补充，也是本票唯一能抓住
        # "代理偷偷把某点的 feasible 改成 True"的断言：
        # 预取点是**只在启用会话里存在**的，逐点比对覆盖不到它们；
        # 而伪装的 feasible 与真实校核产物必然对不上。
        # 所以这里不信内存里的字段，直接读该点 Study 的 check_result 产物
        # 里 strength_check 写下的 all_pass，跟 feasible 核对。
        checked = 0
        for p in sum_on["points"]:
            check_id = (p["artifacts"] or {}).get("check_result")
            if check_id is None:
                continue
            row = conn.execute("SELECT params_json FROM artifacts WHERE id=?",
                               (check_id,)).fetchone()
            if row is None:
                continue
            params = json.loads(row["params_json"] or "{}")
            if "all_pass" not in params:
                continue
            actual = bool(params["all_pass"])
            checked += 1
            if p["feasible"] is not None and bool(p["feasible"]) != actual:
                fails.append(
                    "设计点 %d（来源 %s）的 feasible=%r 与它自己的校核产物 "
                    "all_pass=%r 不一致 —— 判定被代理污染了（ADR-0002）"
                    % (p["point_id"], p["source"], p["feasible"], actual))
            # 分组也必须跟着实测判定走
            expected_group = (optloop.GROUP_FEASIBLE if actual
                              else optloop.GROUP_INFEASIBLE)
            if p["ok"] is True and p["group_name"] != expected_group:
                fails.append(
                    "设计点 %d 的分组 %r 与实测 all_pass=%r 不符"
                    % (p["point_id"], p["group_name"], actual))
        if checked == 0:
            fails.append("没有任何设计点带可核对的校核产物（判定无法独立复核）")

        # ---- 关键前提：预取的点里必须有**实测不可行**的 ----
        #
        # 若代理预取到的点恰好全是可行点，那么"代理把 feasible 篡改成 True"
        # 就观察不到任何差别，上面那条核对会空转（实测中该变异因此存活过一轮）。
        # 故把"预取点含不可行点"本身也变成断言：它是上面那条断言的**有效性前提**。
        prefetched_verdicts = set()
        for p in sum_on["points"]:
            if p["source"] != optloop.SOURCE_SURROGATE:
                continue
            check_id = (p["artifacts"] or {}).get("check_result")
            if check_id is None:
                continue
            row = conn.execute("SELECT params_json FROM artifacts WHERE id=?",
                               (check_id,)).fetchone()
            if row is None:
                continue
            params = json.loads(row["params_json"] or "{}")
            if "all_pass" in params:
                prefetched_verdicts.add(bool(params["all_pass"]))
        if prefetched_verdicts and False not in prefetched_verdicts:
            fails.append(
                "前置条件不成立：代理预取的点全是实测可行点，"
                "无法检验'代理是否篡改判定'（需要预取点里含不可行点）")

        # 代理预取的点：来源标 surrogate，但判定字段仍来自实测校核
        for p in sum_on["points"]:
            if p["source"] != optloop.SOURCE_SURROGATE:
                continue
            ev = p["evaluation"] or {}
            if ev.get("ok") is None:
                fails.append("代理预取点缺评估结果（应来自实测）")
            cons = ev.get("constraints")
            if ev.get("ok") is True and not cons:
                fails.append("代理预取点跑通了却没有约束状态（判定不完整）")

        # 快筛状态里不得出现任何"合格判定"字段（ADR-0002 结构性保证）
        flat = json.dumps(sum_on["surrogate"], ensure_ascii=False)
        for token in surrogate.FORBIDDEN_VERDICT_KEYS:
            if '"%s"' % token in flat:
                fails.append("快筛状态里出现了禁用的判定字段 %r（ADR-0002）"
                             % (token,))
    finally:
        conn.close()


def test_best_is_always_measured_feasible(fails):
    """
    6) 启用快筛不改变"最终最优点必须是实测可行点"。

    即便说谎代理强烈推荐某个点，最优点也必须仍是**实测**最轻的可行点。
    """
    conn = _fresh_db("best")
    try:
        tmp = _fresh("best")
        sum_off, sum_on, _liar = _on_off_pair(conn, tmp)

        for tag, summary in (("关闭", sum_off), ("启用", sum_on)):
            best = summary["best"]
            if best is None:
                if summary["counts"][optloop.GROUP_FEASIBLE]:
                    fails.append("%s：有可行点却没选出最优" % (tag,))
                continue
            if best["group_name"] != optloop.GROUP_FEASIBLE:
                fails.append("%s：最优点不是可行点: %r"
                             % (tag, best["group_name"]))
            if best["objective_value"] is None:
                fails.append("%s：最优点没有目标值" % (tag,))
            # 最优点必须是库里真实存在的点（有 Study、有校核结果）
            if not best["study_id"]:
                fails.append("%s：最优点没有关联 Study" % (tag,))
            if "check_result" not in (best["artifacts"] or {}):
                fails.append("%s：最优点没有强度校核产物（不是实测的）" % (tag,))
            # 独立复算：可行点里目标值最小
            feasible = [p for p in summary["points"]
                        if p["group_name"] == optloop.GROUP_FEASIBLE
                        and p["objective_value"] is not None]
            if feasible:
                lowest = min(float(p["objective_value"]) for p in feasible)
                if abs(float(best["objective_value"]) - lowest) > 1e-9:
                    fails.append("%s：最优点不是实测最轻的可行点（%r != %r）"
                                 % (tag, best["objective_value"], lowest))

        # 两个会话的最优点都必须是可行的（代理没有把不可行点推成最优）
        for tag, summary in (("关闭", sum_off), ("启用", sum_on)):
            if summary["best"] and summary["best"]["feasible"] is not True:
                fails.append("%s：最优点的 feasible 不是 True" % (tag,))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6) Python 3.8 兼容 + 零第三方依赖
# ---------------------------------------------------------------------------

def test_py38(fails):
    """7) Python 3.8 兼容；core 仍不 import 第三方库。"""
    import ast
    import compat_py38

    targets = [
        os.path.join(CORE, "optimization_loop.py"),
        os.path.join(CORE, "surrogate.py"),
        os.path.join(HERE, "test_m27.py"),
    ]
    bad = compat_py38.check_paths(targets)
    if bad:
        fails.append("Python 3.8 兼容性检查未通过:\n%s"
                     % compat_py38.format_violations(bad))

    # 零第三方依赖：用 AST 查真实 import（黑名单，避免"追认现状"的伪检查）
    path = os.path.join(CORE, "optimization_loop.py")
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add((alias.name or "").split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            mods.add((node.module or "").split(".")[0])
    banned = set(["numpy", "scipy", "pandas", "matplotlib", "sklearn",
                  "torch", "tensorflow", "sympy", "numba"])
    hit = mods & banned
    if hit:
        fails.append("optimization_loop.py 引入了第三方依赖: %r" % (sorted(hit),))

    # 主切口未改动
    import run_study
    names = list(inspect.signature(run_study.run_study).parameters.keys())
    if names != ["study_id", "parameter_set", "template_id", "catia", "conn",
                 "options", "legacy"]:
        fails.append("run_study 签名被改动了: %r" % (names,))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    if not os.path.isdir(TMP):
        os.makedirs(TMP)

    fails = []
    print("1. 默认关闭：与 #25 纯确定性路径逐点一致 ...")
    test_default_off_matches_baseline(fails)
    print("1b. 换 seed 关闭仍一致 ...")
    test_off_equals_baseline_across_seeds(fails)
    print("2. 样本不足 -> 明确不启用并说明 ...")
    test_waiting_when_not_enough_samples(fails)
    print("2b. 快筛策略校验 ...")
    test_policy_validation(fails)
    print("3. 累积够样本自动启用 + 来源可区分 ...")
    test_auto_enable_and_source(fails)
    print("3b. 预取分散化 ...")
    test_prefetch_is_diversified(fails)
    print("3c. 快筛状态落库可复算 ...")
    test_surrogate_report_persisted(fails)
    print("4. 误差不达标/拟合出错 -> 自动降级且原因可见 ...")
    test_degrade_when_error_too_large(fails)
    print("5. 【核心】代理绝不改变合格判定（说谎代理） ...")
    test_surrogate_never_changes_verdict(fails)
    print("6. 最优点必是实测可行点 ...")
    test_best_is_always_measured_feasible(fails)
    print("7. Python 3.8 兼容 + 零第三方依赖 ...")
    test_py38(fails)

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M27 (票27) SELF-TEST: ALL PASSED "
          "(默认关闭一致 / 自动启用 / 降级可见 / 来源可区分 / "
          "代理不改合格判定 / 最优点实测可行 / 分散化 / py3.8)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
