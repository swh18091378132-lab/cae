# -*- coding: utf-8 -*-
"""
test_m25.py -- 票25 验收：优化会话编排与持久化（每个设计点 = 独立 Study）

验收点（对应 issue #25 / ADR-0009 / ADR-0002 / ADR-0005）：
  1. 可创建优化会话并记录意图、预算、设计向量声明快照（**可从库里读回**）
  2. 每个设计点都是一个独立 Study：Study 数 = 评估点数，且互不覆盖
  3. 纯铺层变动的设计点**不产生新数模**，且 CATIA 驱动调用次数为 0
  4. 设计点与 Study 的关联可查，设计点的血缘能从会话**一跳回溯**
  5. 单点技术故障不中断整次优化，失败原因在汇总中可见
  6. 汇总给出目标值/约束/可行性/是否最优，且能按三类分组
  7. **优化方案未经人批准时不可被当作"采用方案"**（断言拒绝）
  8. 「维持原设计」是合法决策且被记录
  9. 数据库演进不破坏 P1/P2 既有数据（构造旧库断言仍可查）
 10. P2 审批行为零回归（既有审批语义不变 + 转换表未被改动）
 11. Python 3.8 兼容

设计要点（为什么这样切）：
  * **每个点真的跑一个独立 Study**：不为提速给编排层留"跳过 Study"的捷径 ——
    否则"Study 数 = 评估点数"在测试里永远成立、在生产里悄悄不成立。
    速度由 fake 适配器负责（FakeCatiaDriver + FakeAbaqusRunner）。
  * 故障隔离用一个**会在特定参数上抛错**的求解器注入，验证的是"一个点炸了，
    整次优化仍跑完、且原因可查"，而不是"异常被 except 吞掉"。
  * L2 门的断言只看**公开 API**：`adopted_point()` 为 None、`require_adopted_point()`
    抛错；不检查内部标记位。

运行 (在 apps/workbench 下): python tests/test_m25.py
"""

import os
import sys
import json
import shutil
import sqlite3
import inspect

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
sys.path.insert(0, CORE)
sys.path.insert(0, WORKBENCH)

import db
import study
import approval
import optimizer
import objectives
import design_vector
import optimization_loop as optloop
from run_study import RunOptions
from catia_driver import FakeCatiaDriver
from abaqus_runner import FakeAbaqusRunner


# 临时目录按 PID 隔离：run_tests.py 可能与单测并行执行，共用固定目录会撞文件锁
TMP_BASE = os.path.join(WORKBENCH, ".tmp_test", "m25")
TMP = os.path.join(TMP_BASE, "run_%d" % os.getpid())

TEMPLATE_ID = "wingbox_v1"

# 设计向量声明：几何变量 3 个 + 铺层变量（层数 + 角度档位 + 单层厚度）。
# 角度用**整数档位索引 + angle_table**，与 optimizer 的离散候选（整数）原生互通。
DV_SPEC = {
    "id": "m25_dv_v1",
    "template_id": TEMPLATE_ID,
    "variables": [
        {"name": "span", "category": "geometry", "type": "continuous",
         "min": 5.0, "max": 12.0, "default": 5.0},
        {"name": "sweep_deg", "category": "geometry", "type": "continuous",
         "min": 0.0, "max": 40.0, "default": 10.0},
        {"name": "c_root", "category": "geometry", "type": "continuous",
         "min": 0.6, "max": 1.6, "default": 1.0},
        {"name": "c_tip", "category": "geometry", "type": "continuous",
         "min": 0.3, "max": 0.9, "default": 0.6},
        {"name": "n_ribs", "category": "geometry", "type": "discrete",
         "min": 2, "max": 5, "step": 1, "default": 3},
        {"name": "n_stringers", "category": "geometry", "type": "discrete",
         "min": 3, "max": 5, "step": 1, "default": 4},
        {"name": "n_plies", "category": "ply", "type": "discrete",
         "min": 4, "max": 10, "step": 2, "default": 8},
        {"name": "angle_0", "category": "ply", "type": "discrete",
         "candidates": [0, 1, 2, 3], "default": 0},
        {"name": "angle_1", "category": "ply", "type": "discrete",
         "candidates": [0, 1, 2, 3], "default": 1},
        {"name": "ply_thickness", "category": "ply", "type": "continuous",
         "min": 0.08, "max": 0.2, "default": 0.1},
    ],
    "ply_sequence": {
        "n_plies": "n_plies",
        "angles": ["angle_0", "angle_1"],
        "thickness": "ply_thickness",
        "material": "CFRP",
        "order": "cyclic",
        "angle_table": [0.0, 45.0, -45.0, 90.0],
    },
}

BASE_VECTOR = design_vector.default_vector(design_vector.normalize_spec(DV_SPEC))

INTENT = {
    "objective": {"name": "mass", "direction": "minimize"},
    "constraints": [{"name": "strength", "require": "all_pass"}],
    "note": "M25 单测：强度合格前提下减重",
}

BUDGET = {"max_evals": 6, "max_rounds": 2, "improvement_threshold": 1e-6}

RULES = {"criteria": ["tsai_wu"], "safety_factor": 1.0}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

class CountingCatiaDriver(FakeCatiaDriver):
    """
    会计数的 fake 驱动 —— 票25「纯铺层点 CATIA 调用次数为 0」的可观测口径。

    真机 CATIA 无许可证（ADR-0013），所以"有没有调用 CATIA"只能靠适配器计数；
    这也是唯一诚实的位置：run_study 对 catia 的唯一接触点就是 regenerate()。
    """

    def __init__(self, out_dir, fail_on=None):
        FakeCatiaDriver.__init__(self, out_dir, fail_on=fail_on)
        self.calls = 0
        self.params_seen = []

    def regenerate(self, parameter_set, template):
        self.calls += 1
        self.params_seen.append(dict(parameter_set))
        return FakeCatiaDriver.regenerate(self, parameter_set, template)


class FlakyRunner(FakeAbaqusRunner):
    """
    会在指定单元数上求解失败的求解器（模拟"某设计点技术故障"）。

    为什么按 n_elems 触发：它由设计点参数决定，于是"故障"是**可复现且与设计
    相关**的，而不是随机炸 —— 这样测的是"故障隔离 + 原因可查"，不是"运气"。
    """

    def __init__(self, n_elems=6, n_plies=2, fail_on_n_elems=(), **kwargs):
        FakeAbaqusRunner.__init__(self, n_elems=n_elems, n_plies=n_plies,
                                  **kwargs)
        self.fail_on_n_elems = set(int(x) for x in fail_on_n_elems)
        self.solve_calls = 0

    def solve(self, inp_path, job_name, work_dir, step_name=None, analysis=None):
        self.solve_calls += 1
        n = _count_inp_elems(inp_path)
        if n in self.fail_on_n_elems:
            raise RuntimeError("模拟求解故障：%d 个单元的模型求解不收敛" % n)
        return FakeAbaqusRunner.solve(self, inp_path, job_name, work_dir,
                                      step_name=step_name, analysis=analysis)


def _count_inp_elems(inp_path):
    """数 .inp 里的单元行（FAKE 求解器据此决定是否"故障"）。"""
    try:
        with open(inp_path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except (IOError, OSError):
        return 0
    n = 0
    in_elem = False
    for line in lines:
        s = line.strip()
        if s.startswith("*ELEMENT,"):
            in_elem = True
            continue
        if s.startswith("*"):
            in_elem = False
            continue
        if in_elem and s:
            n += 1
    return n


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
    path = os.path.join(d, "m25.db")
    return db.connect(path), path


def _options(tmp, sid):
    """一个设计点的标准全链选项（各阶段目录逐点独立）。"""
    def dir_for(sub):
        return os.path.join(tmp, sid, sub)
    return RunOptions.full_chain(dir_for, with_report=False,
                                 abaqus=None).replace(rules=RULES)


def _run_session(conn, tmp, sid, vector=None, budget=None, catia=None,
                 abaqus=None, seed=0, intent=None, max_evals=None,
                 inject_hotspot=False):
    """
    建会话 + 跑优化，返回 (session_id, summary, catia)。

    `inject_hotspot=True` 让 fake 求解器在最后一层制造高应力热点 —— 此时
    **每个点都会不可行**（FI 超 1），用于验证「不可行 / 可行」两类分组；
    默认 False（应力很小）用来得到真实可行点，验证最优点与采用门。
    """
    catia = catia or CountingCatiaDriver(os.path.join(tmp, "catparts"))
    session = optloop.create_session(
        conn, DV_SPEC, intent=intent or INTENT, budget=budget or BUDGET,
        name="M25 " + sid, session_id=sid)
    summary = optloop.run_optimization(
        conn, session["session_id"], catia,
        options=None, work_dir=os.path.join(tmp, "work"),
        abaqus=abaqus or FakeAbaqusRunner(n_elems=6, n_plies=2,
                                          inject_hotspot=inject_hotspot),
        seed=seed, max_evals=max_evals)
    return session["session_id"], summary, catia


# ---------------------------------------------------------------------------
# 1) 会话：意图 / 预算 / 声明快照可落库可读回
# ---------------------------------------------------------------------------

def test_session_persisted(fails):
    """1) 创建会话后，意图、预算、设计向量声明快照都能从库里读回（可追溯）。"""
    conn, _path = _fresh_db("session")
    try:
        sid = "OPT-M25-S1"
        created = optloop.create_session(
            conn, DV_SPEC, intent=INTENT, budget=BUDGET,
            name="减重优化", note="首轮", session_id=sid)

        if created["session_id"] != sid:
            fails.append("会话 id 不符: %r" % (created["session_id"],))
        if created["template_id"] != TEMPLATE_ID:
            fails.append("会话未记录模板族: %r" % (created["template_id"],))
        if created["status"] != optloop.SESSION_CREATED:
            fails.append("新会话状态应为 created，实际 %r" % (created["status"],))

        # 重新从库里读（**不是**复用内存对象）：意图/预算/声明快照都在
        back = optloop.get_session(conn, sid)
        if back["intent"]["objective"]["name"] != "mass":
            fails.append("意图目标未落库: %r" % (back["intent"],))
        if back["intent"]["objective"]["direction"] != "minimize":
            fails.append("意图方向未落库: %r" % (back["intent"],))
        if not back["intent"]["constraints"]:
            fails.append("意图约束未落库: %r" % (back["intent"],))
        if back["intent"]["note"] != INTENT["note"]:
            fails.append("意图备注未落库: %r" % (back["intent"].get("note"),))

        # 预算三元组
        for key, want in BUDGET.items():
            if back["budget"].get(key) != want:
                fails.append("预算 %s 未落库: %r != %r"
                             % (key, back["budget"].get(key), want))

        # 设计向量**声明快照**：变量名/类型/默认值都能读回
        spec = back["design_vector"]
        names = [v["name"] for v in spec.get("variables") or []]
        for want in ("span", "n_ribs", "n_plies", "angle_0", "ply_thickness"):
            if want not in names:
                fails.append("声明快照缺少变量 %r（现有 %r）" % (want, names))
        if spec.get("ply_sequence", {}).get("angle_table") != [0.0, 45.0, -45.0, 90.0]:
            fails.append("声明快照丢失 angle_table: %r"
                         % (spec.get("ply_sequence"),))

        # 快照是**副本**：外部改了声明文件/对象，会话记录不跟着变
        mutated = json.loads(json.dumps(DV_SPEC))
        mutated["variables"][0]["default"] = 11.0
        again = optloop.get_session(conn, sid)
        if again["design_vector"]["variables"][0]["default"] != 5.0:
            fails.append("会话里的声明快照被外部改动影响了（应当是快照）")

        # 会话不可覆盖
        try:
            optloop.create_session(conn, DV_SPEC, session_id=sid)
            fails.append("同一会话 id 重复创建未被拒绝（会话不应可覆盖）")
        except optloop.OptimizationLoopError as exc:
            if exc.code != "study-exists":
                fails.append("重复创建会话的错误码应为 study-exists，实际 %r"
                             % exc.code)

        # 意图校验：非法方向/空约束必须早报
        for bad in ({"objective": {"name": "mass", "direction": "up"}},
                    {"constraints": []}):
            try:
                optloop.normalize_intent(bad)
                fails.append("非法意图未被拒绝: %r" % (bad,))
            except optloop.OptimizationLoopError:
                pass
    finally:
        conn.close()


def test_optimizer_variables_mapping(fails):
    """1b) 声明 -> 优化器变量：离散变量候选值是整数（优化器可 ±1 邻域搜索）。"""
    variables = optloop.optimizer_variables(DV_SPEC)
    by_name = dict((v["name"], v) for v in variables)

    for name in ("span", "sweep_deg", "c_root", "c_tip", "n_ribs",
                 "n_stringers", "n_plies", "angle_0", "angle_1",
                 "ply_thickness"):
        if name not in by_name:
            fails.append("优化器变量缺少 %r" % (name,))
    # 几何类连续变量
    if by_name["span"]["type"] != "continuous":
        fails.append("span 应为连续变量: %r" % (by_name["span"],))
    # 离散变量：候选值全整数
    for name in ("n_ribs", "n_plies", "angle_0"):
        spec_var = by_name[name]
        if spec_var["type"] != "discrete":
            fails.append("%s 应为离散变量" % (name,))
        cands = optimizer._discrete_values(spec_var)
        for c in cands:
            if not isinstance(c, int):
                fails.append("%s 的离散候选 %r 不是整数（优化器只做整数邻域）"
                             % (name, c))
    if optimizer._discrete_values(by_name["angle_0"]) != [0, 1, 2, 3]:
        fails.append("角度档位候选应为 [0,1,2,3]，实际 %r"
                     % (optimizer._discrete_values(by_name["angle_0"]),))

    # 类别带出来了（用于分组统计）
    if by_name["span"]["category"] != "geometry":
        fails.append("span 类别应为 geometry")
    if by_name["n_plies"]["category"] != "ply":
        fails.append("n_plies 类别应为 ply")


# ---------------------------------------------------------------------------
# 2) 每个设计点 = 独立 Study
# ---------------------------------------------------------------------------

def test_each_point_is_independent_study(fails):
    """2) Study 数 = 评估点数，且互不覆盖（各自从 v1 起、产物不共用）。"""
    conn, _path = _fresh_db("studies")
    try:
        sid, summary, _catia = _run_session(
            conn, os.path.join(TMP, "studies"),
            "OPT-M25-S2",
            abaqus=FakeAbaqusRunner(n_elems=6, n_plies=2),
            max_evals=4)

        points = optloop.list_points(conn, sid)
        if len(points) < 2:
            fails.append("会话至少应评估 2 个设计点，实际 %d" % len(points))

        study_ids = [p["study_id"] for p in points]
        if len(set(study_ids)) != len(study_ids):
            fails.append("不同设计点复用了同一个 Study（ADR-0009 被破坏）: %r"
                         % (study_ids,))
        if any(not x for x in study_ids):
            fails.append("有设计点没有关联 Study: %r" % (study_ids,))

        # 每个点一个 Study 行 —— Study 数严格等于评估点数
        for s in study_ids:
            row = conn.execute("SELECT id FROM studies WHERE id=?", (s,)).fetchone()
            if row is None:
                fails.append("设计点的 Study %r 不存在于 studies 表" % (s,))

        # 互不覆盖：每个点的 parameter_set 都是 v1（各自独立版本序列）
        for p in points:
            arts = p["artifacts"]
            if "parameter_set" not in arts:
                fails.append("设计点 %d 的 Study 缺参数集产物" % p["point_id"])
            row = conn.execute(
                "SELECT version FROM artifacts WHERE id=?",
                (arts.get("parameter_set"),)).fetchone()
            if row is not None and int(row["version"]) != 1:
                fails.append("设计点 %d 的参数集应是 v1（独立 Study），实际 v%d"
                             % (p["point_id"], row["version"]))
            # 拓扑/网格也各有一份 -> 说明真的跑了完整的建模链
            for kind in ("topology", "mesh", "inp", "check_result"):
                if kind not in arts:
                    fails.append("设计点 %d 的 Study 缺产物 %r（现有 %r）"
                                 % (p["point_id"], kind, sorted(arts)))

        # 每个点各自的中间参数集文件路径不同（不互相覆盖）
        paths = []
        for p in points:
            row = conn.execute("SELECT content_uri FROM artifacts WHERE id=?",
                               (p["artifacts"].get("parameter_set"),)).fetchone()
            if row is not None and row["content_uri"]:
                paths.append(os.path.normcase(os.path.abspath(row["content_uri"])))
        if len(set(paths)) != len(paths):
            fails.append("多个设计点的参数集写到了同一个文件（会互相覆盖）: %r"
                         % (paths,))

        if summary["counts"][optloop.GROUP_FEASIBLE] + \
                summary["counts"][optloop.GROUP_INFEASIBLE] + \
                summary["counts"][optloop.GROUP_FAILED] != summary["n_points"]:
            fails.append("三分组之和不等于设计点总数")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3) 纯铺层变动：不产生新数模、CATIA 调用 0 次
# ---------------------------------------------------------------------------

def test_ply_only_point_skips_catia(fails):
    """3) 纯铺层变动的设计点复用数模：CATIA 调用次数为 0，且不产生新数模文件。"""
    conn, _path = _fresh_db("plyonly")
    try:
        tmp = os.path.join(TMP, "plyonly")
        catia = CountingCatiaDriver(os.path.join(tmp, "catparts"))
        session = optloop.create_session(
            conn, DV_SPEC, intent=INTENT, budget=BUDGET,
            name="纯铺层", session_id="OPT-M25-S3")

        # 第 1 点：基线（几何+铺层都是默认值）-> 必须真的再生数模
        p1 = optloop.evaluate_point(
            conn, session["session_id"], BASE_VECTOR, catia,
            work_dir=os.path.join(tmp, "work"),
            abaqus=FakeAbaqusRunner(n_elems=6, n_plies=2),
            options=_options(tmp, "P1"), source=optloop.SOURCE_BASELINE)
        if catia.calls != 1:
            fails.append("基线点应调用 CATIA 1 次，实际 %d" % catia.calls)

        # 第 2 点：**只改铺层**（几何全未变）-> 复用数模，CATIA 一次都不许被调用
        ply_vector = dict(BASE_VECTOR)
        ply_vector.update({"n_plies": 10, "ply_thickness": 0.12, "angle_0": 2})

        spec = design_vector.normalize_spec(DV_SPEC)
        decision = design_vector.needs_regeneration(
            spec, design_vector.validate_vector(spec, BASE_VECTOR),
            design_vector.validate_vector(spec, ply_vector))
        if decision["needs_regeneration"] is not False:
            fails.append("前置条件：纯铺层变动应判定为无需再生，实际 %r" % decision)

        before = catia.calls
        p2 = optloop.evaluate_point(
            conn, session["session_id"], ply_vector, catia,
            work_dir=os.path.join(tmp, "work"),
            abaqus=FakeAbaqusRunner(n_elems=6, n_plies=2),
            options=_options(tmp, "P2"), source=optloop.SOURCE_DOE)

        # ---- 核心验收点：CATIA 调用次数为 0 ----
        if catia.calls != before:
            fails.append("纯铺层点调用了 CATIA %d 次（应为 0 次）"
                         % (catia.calls - before))
        if p2["reused_from"] != p1["study_id"]:
            fails.append("纯铺层点应记录复用自 %r，实际 %r"
                         % (p1["study_id"], p2["reused_from"]))

        # 不产生新数模文件：数模产物的 content_uri 与源 Study 相同
        c1 = conn.execute("SELECT content_uri, checksum FROM artifacts WHERE id=?",
                          (p1["artifacts"]["catpart"],)).fetchone()
        c2 = conn.execute("SELECT content_uri, checksum FROM artifacts WHERE id=?",
                          (p2["artifacts"]["catpart"],)).fetchone()
        if c1 is not None and c2 is not None:
            if os.path.normcase(c1["content_uri"]) != os.path.normcase(c2["content_uri"]):
                fails.append("纯铺层点产生了新数模文件（应复用同一份）: %r != %r"
                             % (c1["content_uri"], c2["content_uri"]))

        # 但铺层确实变了（否则"复用"就没意义）：inp 里的层数应不同
        def _nplies(point):
            row = conn.execute("SELECT params_json FROM artifacts WHERE id=?",
                               (point["artifacts"]["inp"],)).fetchone()
            return json.loads(row["params_json"]).get("n_plies")
        if _nplies(p1) == _nplies(p2):
            fails.append("纯铺层点的层数未变（%r），说明铺层没有真的传下去"
                         % (_nplies(p1),))

        # 两个点都是"跑通"的（复用不影响评估可用性）
        if p2["ok"] is not True:
            fails.append("复用数模的点评估失败: %r" % (p2.get("error"),))

        # 几何变动时必须重新再生（复用不能变成"用旧几何偷懒"）
        geo_vector = dict(BASE_VECTOR)
        geo_vector.update({"n_ribs": 5})
        before2 = catia.calls
        optloop.evaluate_point(
            conn, session["session_id"], geo_vector, catia,
            work_dir=os.path.join(tmp, "work"),
            abaqus=FakeAbaqusRunner(n_elems=6, n_plies=2),
            options=_options(tmp, "P3"), source=optloop.SOURCE_DOE)
        if catia.calls != before2 + 1:
            fails.append("几何变动的点应重新再生数模（CATIA +1），实际 +%d"
                         % (catia.calls - before2))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4) 设计点 <-> Study 关联；血缘从会话一跳回溯
# ---------------------------------------------------------------------------

def test_lineage_from_session(fails):
    """4) 从会话 id 一次查询就能到每个点的全部产物（含 .inp/odb/校核结果）。"""
    conn, _path = _fresh_db("lineage")
    try:
        sid, _summary, _catia = _run_session(
            conn, os.path.join(TMP, "lineage"), "OPT-M25-S4", max_evals=3)

        lin = optloop.lineage(conn, sid)
        if lin["n_points"] != len(optloop.list_points(conn, sid)):
            fails.append("血缘里的点数与设计点数不一致")
        if not lin["points"]:
            fails.append("血缘为空")
            return

        for item in lin["points"]:
            if not item["study_id"]:
                fails.append("血缘条目缺 study_id: %r" % (item,))
                continue
            # 一跳：会话 -> 点 -> 产物链，链上要有完整建模/求解/校核产物
            kinds = set(a["kind"] for a in item["chain"])
            for want in ("parameter_set", "catpart", "topology", "mesh", "inp",
                         "odb", "extraction", "check_result"):
                if want not in kinds:
                    fails.append("设计点 %d 的血缘链缺 %r（现有 %r）"
                                 % (item["seq"], want, sorted(kinds)))
            # 血缘边真的连起来了（不是只按 study_id 查）
            mesh_id = item["artifacts"].get("mesh")
            inp_id = item["artifacts"].get("inp")
            if mesh_id and inp_id:
                n = conn.execute(
                    "SELECT COUNT(*) c FROM edges WHERE parent_id=? AND child_id=?",
                    (mesh_id, inp_id)).fetchone()["c"]
                if n != 1:
                    fails.append("设计点 %d 的 mesh->inp 血缘边缺失" % item["seq"])

        # 会话元信息也一并带出（意图/预算/声明快照）
        s = lin["session"]
        for key in ("session_id", "template_id", "intent", "budget",
                    "design_vector"):
            if key not in s:
                fails.append("血缘里的会话元信息缺 %r" % (key,))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5) 单点技术故障隔离
# ---------------------------------------------------------------------------

def test_failure_isolation(fails):
    """5) 单点求解故障不中断整次优化：其余点照跑，故障原因在汇总中可见。"""
    conn, _path = _fresh_db("failure")
    try:
        tmp = os.path.join(TMP, "failure")
        catia = CountingCatiaDriver(os.path.join(tmp, "catparts"))

        # 让"6 单元"的模型求解失败：基线恰好 6 单元（3 肋 × 4 长桁），
        # 于是必然至少有一个故障点；其余点仍须照跑
        flaky = FlakyRunner(n_elems=6, n_plies=2, fail_on_n_elems=(6,))
        sid, summary, _c = _run_session(
            conn, tmp, "OPT-M25-S5", catia=catia, abaqus=flaky, max_evals=5,
            inject_hotspot=False)

        counts = summary["counts"]
        if counts[optloop.GROUP_FAILED] < 1:
            fails.append("注入求解故障后应有评估失败点，实际三组为 %r" % (counts,))
        if summary["n_points"] <= counts[optloop.GROUP_FAILED]:
            fails.append("有故障点时其余设计点仍应被评估（总数 %d，故障 %d）"
                         % (summary["n_points"], counts[optloop.GROUP_FAILED]))

        # 故障原因在汇总中可见（kind + message）
        reasons = summary["failed_reasons"]
        if not reasons:
            fails.append("故障点没有记录失败原因")
        else:
            r0 = reasons[0]
            if not r0.get("message"):
                fails.append("失败原因缺 message: %r" % (r0,))
            if not r0.get("kind"):
                fails.append("失败原因缺 kind（技术故障分类）: %r" % (r0,))

        # 故障点的数据结构：ok=False / feasible=None / 目标值为 None
        for p in summary["groups"][optloop.GROUP_FAILED]:
            if p["ok"] is not False:
                fails.append("故障点的 ok 应为 False，实际 %r" % (p["ok"],))
            if p["feasible"] is not None:
                fails.append("故障点的 feasible 应为 None（不能当成'设计不可行'），"
                             "实际 %r" % (p["feasible"],))
            if p["objective_value"] is not None:
                fails.append("故障点的目标值应为 None，实际 %r"
                             % (p["objective_value"],))

        # 故障点绝不参与"最优"：最优只能是可行点
        best = summary["best"]
        if best is not None and best["group_name"] != optloop.GROUP_FEASIBLE:
            fails.append("最优设计点不是可行点: %r" % (best["group_name"],))
        if best is not None and best["objective_value"] is None:
            fails.append("最优设计点的目标值为 None")

        # 优化仍正常收尾（状态 finished）
        if summary["status"] != optloop.SESSION_FINISHED:
            fails.append("有故障点时会话仍应正常结束，实际状态 %r"
                         % (summary["status"],))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6) 汇总：目标值/约束/可行性/是否最优 + 三类分组
# ---------------------------------------------------------------------------

def test_summary_and_groups(fails):
    """6) 汇总给出目标值/约束/可行性/是否最优；三组覆盖全部点且互斥。

    两个会话各跑一次：
      * 无热点 -> 存在**可行点**（验证最优点、约束数值、目标值）
      * 注入热点 -> 存在**不可行点**（验证"跑通但违反约束"的形态，
        且不可行点仍有可信目标值）
    """
    conn, _path = _fresh_db("summary")
    try:
        sid, summary, _catia = _run_session(
            conn, os.path.join(TMP, "summary"), "OPT-M25-S6", max_evals=3,
            inject_hotspot=False)

        sid_bad, summary_bad, _c2 = _run_session(
            conn, os.path.join(TMP, "summary_bad"), "OPT-M25-S6B", max_evals=2,
            inject_hotspot=True)

        points = summary["points"]
        if not points:
            fails.append("汇总里没有设计点")
            return
        if not summary["groups"][optloop.GROUP_FEASIBLE]:
            fails.append("无热点时应至少有一个可行点（前置条件不成立）")
        if summary["groups"][optloop.GROUP_FEASIBLE]:
            _check_group_invariants(summary, fails, cases=("feasible", True))
        if summary_bad["groups"][optloop.GROUP_INFEASIBLE]:
            _check_group_invariants(summary_bad, fails,
                                    cases=("infeasible", False))
        else:
            fails.append("注入热点后应至少有一个不可行点")

        # groups() 与 summary["groups"] 一致（同一口径的另一个入口）
        for key in (optloop.GROUP_FEASIBLE, optloop.GROUP_INFEASIBLE,
                    optloop.GROUP_FAILED):
            grouped = optloop.groups(conn, sid)
            if len(grouped.get(key) or []) != summary["counts"][key]:
                fails.append("groups()[%r] 与汇总计数不一致" % (key,))

        # 汇总可复算：隔一会再查，结论相同（不是靠内存拼的）
        again = optloop.summarize(conn, sid)
        if again["counts"] != summary["counts"]:
            fails.append("重新查询的汇总与首次不一致（结论未从库重算）")

        # 文本摘要能读（人类可读的结论）
        text = optloop.summarize_text(summary)
        if "设计点" not in text or "建议采用" not in text:
            fails.append("文本摘要内容不完整: %r" % (text,))
    finally:
        conn.close()


def _check_group_invariants(summary, fails, cases=("feasible", True)):
    """两类分组点各自的形态不变量（可行点全项齐全；不可行点跑通但违约束）。"""
    kind, expect_feasible = cases
    points = summary["points"]
    feasible = [p for p in points if p["group_name"] == optloop.GROUP_FEASIBLE]
    infeasible = [p for p in points if p["group_name"] == optloop.GROUP_INFEASIBLE]
    failed = [p for p in points if p["group_name"] == optloop.GROUP_FAILED]

    # 三组互斥且覆盖全部
    if len(feasible) + len(infeasible) + len(failed) != len(points):
        fails.append("三类分组不覆盖全部设计点")
    ids = set()
    for group in (feasible, infeasible, failed):
        for p in group:
            if p["point_id"] in ids:
                fails.append("设计点 %r 出现在多个分组里" % (p["point_id"],))
            ids.add(p["point_id"])

    # counts 与分组一致
    for key, group in ((optloop.GROUP_FEASIBLE, feasible),
                       (optloop.GROUP_INFEASIBLE, infeasible),
                       (optloop.GROUP_FAILED, failed)):
        if summary["counts"][key] != len(group):
            fails.append("counts[%r]=%d 与分组长度 %d 不一致"
                         % (key, summary["counts"][key], len(group)))

    # 可行点：目标值 + 约束状态 + 约束数值都齐
    for p in feasible:
        if p["objective_value"] is None:
            fails.append("可行点 %d 缺目标值" % p["point_id"])
        cons = p.get("constraints") or {}
        if cons.get("feasible") is not True:
            fails.append("可行点 %d 的约束状态应为 True: %r"
                         % (p["point_id"], cons))
        if not (cons.get("values") or {}):
            fails.append("可行点 %d 的约束缺具体数值（max_fi/min_ms）"
                         % p["point_id"])

    # 不可行点：跑通了但违反约束
    for p in infeasible:
        if p["ok"] is not True or p["feasible"] is not False:
            fails.append("不可行点 %d 形态不对: ok=%r feasible=%r"
                         % (p["point_id"], p["ok"], p["feasible"]))
        if p["objective_value"] is None:
            fails.append("不可行点 %d 的目标值应可信（非 None），实际 None"
                         % (p["point_id"],))

    # "是否最优"恰好标在一个可行点上
    marked = [p for p in points if p["is_best"]]
    if feasible:
        if len(marked) != 1:
            fails.append("应恰有一个点被标为最优，实际 %d 个" % len(marked))
        elif marked[0]["group_name"] != optloop.GROUP_FEASIBLE:
            fails.append("被标为最优的点不是可行点")
        else:
            best_value = min(p["objective_value"] for p in feasible)
            if abs(float(marked[0]["objective_value"]) - float(best_value)) > 1e-9:
                fails.append("最优点的目标值 %r 不是可行点里的最小值 %r"
                             % (marked[0]["objective_value"], best_value))
    elif marked:
        fails.append("没有可行点却标了最优")


# ---------------------------------------------------------------------------
# 7) L2 门：未批准不可采用
# ---------------------------------------------------------------------------

def test_not_adopted_until_approved(fails):
    """7) 优化方案的采用必须经人批准：未批准时取采用方案必须被拒绝。"""
    conn, _path = _fresh_db("adopt")
    try:
        sid, summary, _catia = _run_session(
            conn, os.path.join(TMP, "adopt"), "OPT-M25-S7", max_evals=3)

        # ---- 优化结束：只给出"建议"，不是"已采用" ----
        rec = summary["recommendation"]
        if not rec.get("requires_approval"):
            fails.append("建议采用点必须声明 requires_approval=True: %r" % (rec,))
        if rec.get("approved"):
            fails.append("建议点在未经批准时不应标记 approved")

        state = optloop.get_adoption(conn, sid)
        if state.get("decided"):
            fails.append("还没人批准就把会话标成已决策: %r" % (state,))

        # 未批准 -> 取不到采用方案
        if optloop.adopted_point(conn, sid) is not None:
            fails.append("未经批准却取到了采用方案（ADR-0002 被绕过）")
        try:
            optloop.require_adopted_point(conn, sid)
            fails.append("未经批准时 require_adopted_point 未抛错")
        except optloop.OptimizationLoopError as exc:
            if exc.code != "not-adopted":
                fails.append("未批准的错误码应为 not-adopted，实际 %r" % exc.code)

        # 非签发角色不能决定采用
        best_pid = (summary["best"] or {}).get("point_id")
        if best_pid is None:
            fails.append("本应找到可行点（前置条件不成立，检查参数）")
            return
        for role in (approval.ROLE_DESIGN, approval.ROLE_ANALYSIS,
                     approval.ROLE_REVIEWER):
            try:
                optloop.record_decision(conn, sid, approval.DECISION_ADOPT,
                                        role=role, operator="张三")
                fails.append("角色 %r 不应有权决定采用优化方案" % (role,))
            except optloop.OptimizationLoopError:
                pass

        # 不可行的点不能被"采用"
        infeasible = summary["groups"][optloop.GROUP_INFEASIBLE]
        if infeasible:
            try:
                optloop.record_decision(
                    conn, sid, approval.DECISION_ADOPT,
                    role=approval.ROLE_SIGNER,
                    point_id=infeasible[0]["point_id"])
                fails.append("不可行的设计点被允许采用（强度不合格的方案不能采用）")
            except optloop.OptimizationLoopError:
                pass

        # 签发角色批准采用 -> 才能取到
        state = optloop.record_decision(
            conn, sid, approval.DECISION_ADOPT, role=approval.ROLE_SIGNER,
            operator="李四", rationale="满足减重目标，同意采用")
        if not state.get("decided"):
            fails.append("批准后会话仍未标记已决策: %r" % (state,))

        adopted = optloop.adopted_point(conn, sid)
        if adopted is None:
            fails.append("批准后仍取不到采用方案")
        else:
            if adopted["point_id"] != best_pid:
                fails.append("采用的应是建议的最优点 %r，实际 %r"
                             % (best_pid, adopted["point_id"]))
            if not objectives.is_feasible(adopted["evaluation"]):
                fails.append("被采用的设计点不是可行点")

        required = optloop.require_adopted_point(conn, sid)
        if required["point_id"] != best_pid:
            fails.append("require_adopted_point 返回的点与批准的不一致")

        # 决策进 L2 审计（角色/动作/理由/时间）
        audit = approval.optimize_decisions(conn, session_id=sid)
        if not audit:
            fails.append("采用决策未写进 L2 审计")
        else:
            a0 = audit[-1]
            if a0["decision"] != approval.DECISION_ADOPT:
                fails.append("审计里的决策不对: %r" % (a0,))
            if "签发" not in (a0["actor"] or ""):
                fails.append("审计里的角色不对（应为签发）: %r" % (a0,))
            if not a0["created_at"] or not a0["rationale"]:
                fails.append("审计缺时间或理由: %r" % (a0,))
            if sid not in (a0["rationale"] or ""):
                fails.append("审计理由里应能看出是哪个会话: %r" % (a0,))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 8) 维持原设计是合法决策且被记录
# ---------------------------------------------------------------------------

def test_keep_original_is_valid(fails):
    """8) 「维持原设计」是合法决策：被记录、可查，且不会冒充"采用了新方案"。"""
    conn, _path = _fresh_db("keep")
    try:
        sid, summary, _catia = _run_session(
            conn, os.path.join(TMP, "keep"), "OPT-M25-S8", max_evals=3)

        state = optloop.record_decision(
            conn, sid, approval.DECISION_KEEP_ORIGINAL,
            role=approval.ROLE_SIGNER, operator="王五",
            rationale="优化收益有限，本期维持原设计")

        if state.get("decision") != approval.DECISION_KEEP_ORIGINAL:
            fails.append("维持原设计的决策未记录正确: %r" % (state,))
        if not state.get("decided"):
            fails.append("维持原设计应视为已决策（不是『没决定』）")

        # 维持原设计 -> 没有"采用的新方案"
        if optloop.adopted_point(conn, sid) is not None:
            fails.append("维持原设计时不应有采用的新方案")
        try:
            optloop.require_adopted_point(conn, sid)
            fails.append("维持原设计时 require_adopted_point 未抛错")
        except optloop.OptimizationLoopError as exc:
            if "维持原设计" not in (exc.reason or ""):
                fails.append("拒绝理由应说明是维持原设计，实际 %r" % exc.reason)

        # 汇总里能看出这个决定（而不是"尚未批准"）
        again = optloop.summarize(conn, sid)
        adoption = again.get("adoption") or {}
        if adoption.get("decision") != approval.DECISION_KEEP_ORIGINAL:
            fails.append("汇总里看不到维持原设计的决定: %r" % (adoption,))
        text = optloop.summarize_text(again)
        if "维持原设计" not in text:
            fails.append("文本摘要未体现维持原设计: %r" % (text,))

        # 审计留痕
        audit = approval.optimize_decisions(conn, session_id=sid)
        if not any(a["decision"] == approval.DECISION_KEEP_ORIGINAL for a in audit):
            fails.append("维持原设计未进 L2 审计: %r" % (audit,))

        # 未知决策被拒绝
        try:
            optloop.record_decision(conn, sid, "maybe", role=approval.ROLE_SIGNER)
            fails.append("未知决策未被拒绝")
        except optloop.OptimizationLoopError:
            pass
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 9) DB 演进不破坏 P1/P2 既有数据
# ---------------------------------------------------------------------------

P1_ONLY_SCHEMA = """
CREATE TABLE studies (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, note TEXT
);
CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id TEXT NOT NULL REFERENCES studies(id),
    kind TEXT NOT NULL, version INTEGER NOT NULL, checksum TEXT NOT NULL,
    content_uri TEXT, params_json TEXT, status TEXT NOT NULL DEFAULT 'ok',
    created_at TEXT NOT NULL, UNIQUE(study_id, kind, version)
);
CREATE TABLE edges (
    parent_id INTEGER NOT NULL, child_id INTEGER NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, study_id TEXT NOT NULL,
    gate TEXT NOT NULL, actor TEXT NOT NULL, decision TEXT NOT NULL,
    rationale TEXT, created_at TEXT NOT NULL
);
"""


def test_old_db_migration(fails):
    """9) 打开 P1/P2 老库：新表建出、旧数据仍可查、既有血缘/决策不变。"""
    conn, path = _fresh_db("olddb")
    conn.close()

    # 1) 手工造一个 P1 老库并写入现场数据
    try:
        os.remove(path)
    except OSError:
        pass
    raw = sqlite3.connect(path)
    try:
        raw.executescript(P1_ONLY_SCHEMA)
        raw.execute("INSERT INTO studies(id, name, created_at, note) VALUES "
                    "('S-P1-OLD','P1 机翼','2026-01-01T00:00:00','历史')")
        raw.execute("INSERT INTO artifacts(study_id, kind, version, checksum, "
                    "content_uri, params_json, status, created_at) VALUES "
                    "('S-P1-OLD','parameter_set',1,'sha-ps','params:p1','{}','ok',"
                    "'2026-01-01T00:00:01')")
        raw.execute("INSERT INTO artifacts(study_id, kind, version, checksum, "
                    "content_uri, params_json, status, created_at) VALUES "
                    "('S-P1-OLD','report',1,'sha-rp','report:p1','{}','ok',"
                    "'2026-01-01T00:00:09')")
        raw.execute("INSERT INTO edges(parent_id, child_id) VALUES (1, 2)")
        raw.execute("INSERT INTO decisions(study_id, gate, actor, decision, "
                    "rationale, created_at) VALUES ('S-P1-OLD','rule_confirm',"
                    "'张三','通过','历史决策','2026-01-01T00:00:10')")
        raw.commit()
    finally:
        raw.close()

    # 2) 用 db.connect() 打开（= 现场升级动作）不能报错、不能丢数据
    conn = db.connect(path)
    try:
        tables = set(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        for t in ("studies", "artifacts", "edges", "decisions",
                  "report_approvals", "optimization_sessions",
                  "optimization_points"):
            if t not in tables:
                fails.append("老库升级后缺少表 %r" % (t,))

        # 既有数据仍可查
        row = conn.execute("SELECT * FROM studies WHERE id='S-P1-OLD'").fetchone()
        if row is None or row["name"] != "P1 机翼":
            fails.append("P1 既有 Study 升级后查不到或内容变了")
        arts = conn.execute("SELECT kind FROM artifacts WHERE study_id='S-P1-OLD' "
                            "ORDER BY id").fetchall()
        if [a["kind"] for a in arts] != ["parameter_set", "report"]:
            fails.append("P1 既有产物升级后不全: %r" % ([a["kind"] for a in arts],))
        n_edges = conn.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
        if n_edges != 1:
            fails.append("P1 既有血缘边升级后丢失（%d 条）" % n_edges)
        decs = conn.execute("SELECT * FROM decisions WHERE study_id='S-P1-OLD'"
                            ).fetchall()
        if len(decs) != 1 or decs[0]["gate"] != "rule_confirm":
            fails.append("P1 既有决策升级后丢失/变了: %r"
                         % ([dict(d) for d in decs],))

        # 3) 新能力在老库上可用：建会话、读回
        sess = optloop.create_session(
            conn, DV_SPEC, intent=INTENT, budget=BUDGET,
            name="老库上的优化", session_id="OPT-M25-OLD")
        if optloop.get_session(conn, sess["session_id"])["name"] != "老库上的优化":
            fails.append("老库上升级出的会话表不可用")

        # 4) 迁移幂等：再次 connect 不报错、不重复
        conn.close()
        conn = db.connect(path)
        n1 = conn.execute("SELECT COUNT(*) c FROM optimization_sessions"
                          ).fetchone()["c"]
        if n1 != 1:
            fails.append("重复打开老库后会话数变了（迁移不幂等）: %d" % n1)
        if conn.execute("SELECT COUNT(*) c FROM studies").fetchone()["c"] != 1:
            fails.append("重复打开老库后 Study 数量变了")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 10) P2 审批行为零回归
# ---------------------------------------------------------------------------

def test_p2_approval_zero_regression(fails):
    """10) 新增 optimize gate 不改报告状态机：转换表与既有语义逐项不变。"""
    # 转换表原样（P2 的语义：analyzing→pending→approved/rejected→signed）
    expected = {
        approval.STATUS_ANALYZING: (approval.STATUS_PENDING_REVIEW,),
        approval.STATUS_PENDING_REVIEW: (approval.STATUS_APPROVED,
                                         approval.STATUS_REJECTED),
        approval.STATUS_APPROVED: (approval.STATUS_SIGNED,),
        approval.STATUS_REJECTED: (approval.STATUS_PENDING_REVIEW,),
        approval.STATUS_SIGNED: (),
    }
    if dict((k, tuple(v)) for k, v in approval.TRANSITIONS.items()) != expected:
        fails.append("报告审批转换表被改动了（P2 零回归被破坏）: %r"
                     % (approval.TRANSITIONS,))

    # 既有 gate 常量与审计 gate 清单不变（新增的 optimize gate 不挤进 AUDIT_GATES）
    for name, value in (("GATE_SUBMIT", "approval_submit"),
                        ("GATE_RERUN", "approval_rerun"),
                        ("GATE_REVIEW", "approval_review"),
                        ("GATE_SIGN", "approval_sign")):
        if getattr(approval, name) != value:
            fails.append("%s 的值被改动: %r" % (name, getattr(approval, name)))
    for g in ("approval_submit", "approval_rerun", "approval_review",
              "approval_sign"):
        if g not in approval.AUDIT_GATES:
            fails.append("AUDIT_GATES 丢了既有 gate %r" % (g,))
    if approval.GATE_OPTIMIZE_DECISION in approval.AUDIT_GATES:
        fails.append("新增的 optimize gate 不应混进报告的 AUDIT_GATES（会改变既有语义）")

    # 角色授权规则不变：审核只能审核、签发只能签发
    if approval._ACTION_RULES[approval.GATE_REVIEW][0] != approval.ROLE_REVIEWER:
        fails.append("审核动作的角色要求被改动")
    if approval._ACTION_RULES[approval.GATE_SIGN][0] != approval.ROLE_SIGNER:
        fails.append("签发动作的角色要求被改动")

    # 端到端仍按 P2 语义工作：未通过审核不允许签发；退回必须填意见
    conn, _path = _fresh_db("approval")
    try:
        study.create_study("S-M25-AP", "审批零回归", conn=conn)
        st = approval.register_report(conn, "S-M25-AP", artifact_id=None, version=1)
        if st["status"] != approval.STATUS_PENDING_REVIEW:
            fails.append("登记报告的状态应为待审核，实际 %r" % (st["status"],))
        try:
            approval.sign(conn, "S-M25-AP", 1)
            fails.append("未通过审核的报告被签发了（P2 语义被破坏）")
        except approval.ApprovalError:
            pass
        try:
            approval.reject(conn, "S-M25-AP", 1)
            fails.append("退回未填意见未被拒绝（P2 语义被破坏）")
        except approval.ApprovalError as exc:
            if exc.code != "missing-opinion":
                fails.append("退回缺意见的错误码应为 missing-opinion，实际 %r"
                             % exc.code)
        st = approval.reject(conn, "S-M25-AP", 1, opinion="应力超标，请加厚蒙皮")
        if st["status"] != approval.STATUS_REJECTED:
            fails.append("退回后状态应为 rejected，实际 %r" % (st["status"],))
        approval.submit_for_review(conn, "S-M25-AP", 1)
        approval.approve(conn, "S-M25-AP", 1, opinion="已复核")
        st = approval.sign(conn, "S-M25-AP", 1)
        if not st["final"]:
            fails.append("签发后应为最终版")

        # 优化采用决策的审计不干扰报告审批历史
        n_hist_before = len(approval.history(conn, "S-M25-AP"))
        approval.record_optimize_decision(
            conn, "S-M25-AP", approval.DECISION_KEEP_ORIGINAL,
            role=approval.ROLE_SIGNER, operator="赵六",
            rationale="维持原设计", session_id="OPT-X", point_id=1)
        n_hist_after = len(approval.history(conn, "S-M25-AP"))
        if n_hist_after != n_hist_before + 1:
            fails.append("优化决策应进同一条审计时间线（+1 条），实际 %d -> %d"
                         % (n_hist_before, n_hist_after))
        # 但报告审批版本状态不受影响
        if approval.get_status(conn, "S-M25-AP", 1) != approval.STATUS_SIGNED:
            fails.append("写优化决策后报告审批状态被改了")

        # 未知决策/非法角色被拒
        try:
            approval.record_optimize_decision(
                conn, "S-M25-AP", "bogus", role=approval.ROLE_SIGNER)
            fails.append("未知优化决策未被拒绝")
        except approval.ApprovalError as exc:
            if exc.code != "invalid-decision":
                fails.append("未知决策的错误码应为 invalid-decision，实际 %r"
                             % exc.code)
        try:
            approval.record_optimize_decision(
                conn, "S-M25-AP", approval.DECISION_ADOPT,
                role=approval.ROLE_REVIEWER)
            fails.append("审核角色不应有权决定采用优化方案")
        except approval.ApprovalError as exc:
            if exc.code != "invalid-role":
                fails.append("无权角色的错误码应为 invalid-role，实际 %r" % exc.code)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 11) Python 3.8 兼容 + 零第三方依赖
# ---------------------------------------------------------------------------

def test_py38(fails):
    """11) Python 3.8 兼容；core 仍不 import 第三方库（不引入 numpy）。"""
    import compat_py38

    targets = [
        os.path.join(CORE, "optimization_loop.py"),
        os.path.join(CORE, "approval.py"),
        os.path.join(CORE, "db.py"),
        os.path.join(HERE, "test_m25.py"),
    ]
    bad = compat_py38.check_paths(targets)
    if bad:
        fails.append("Python 3.8 兼容性检查未通过:\n%s"
                     % compat_py38.format_violations(bad))

    # 零第三方依赖：本模块只 import 标准库与 core 内模块。
    # 用 AST 查真实的 import 语句 —— 字符串匹配会把文档里"不 import numpy"这句话
    # 本身判成违规（那正是首轮实现的误报）。
    #
    # 判定方式是**黑名单**（禁止第三方科学与数值库），不是白名单：
    # 白名单会在每次合法新增一个标准库/core 模块时误报（票27 加 surrogate 就撞过），
    # 于是有人会把新名字塞进白名单——那这条检查就变成了"追认现状"，形同虚设。
    import ast
    path = os.path.join(CORE, "optimization_loop.py")
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    third_party = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                third_party.add((alias.name or "").split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:      # 相对 import = core 内模块
                continue
            third_party.add((node.module or "").split(".")[0])
    banned = set(["numpy", "scipy", "pandas", "matplotlib", "sklearn",
                  "torch", "tensorflow", "sympy", "numba"])
    hit = third_party & banned
    if hit:
        fails.append("optimization_loop.py 引入了第三方依赖: %r" % (sorted(hit),))

    # 主切口未被改动（不改 run_study 签名 / RunOptions 加字段的方式）
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
    print("1. 会话意图/预算/声明快照落库可读回 ...")
    test_session_persisted(fails)
    print("1b. 声明 -> 优化器变量映射（整数候选） ...")
    test_optimizer_variables_mapping(fails)
    print("2. 每个设计点 = 独立 Study（数量相等、互不覆盖） ...")
    test_each_point_is_independent_study(fails)
    print("3. 纯铺层点跳过数模再生（CATIA 调用 0 次） ...")
    test_ply_only_point_skips_catia(fails)
    print("4. 设计点/Study 关联 + 会话一跳血缘 ...")
    test_lineage_from_session(fails)
    print("5. 单点技术故障隔离 ...")
    test_failure_isolation(fails)
    print("6. 汇总（目标/约束/可行/最优）+ 三类分组 ...")
    test_summary_and_groups(fails)
    print("7. L2 门：未批准不可采用 ...")
    test_not_adopted_until_approved(fails)
    print("8. 维持原设计是合法决策 ...")
    test_keep_original_is_valid(fails)
    print("9. 旧库迁移不破坏既有数据 ...")
    test_old_db_migration(fails)
    print("10. P2 审批行为零回归 ...")
    test_p2_approval_zero_regression(fails)
    print("11. Python 3.8 兼容 + 零第三方依赖 ...")
    test_py38(fails)

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M25 (票25) SELF-TEST: ALL PASSED "
          "(会话落库 / 每点独立 Study / 纯铺层零 CATIA / 血缘一跳 / "
          "故障隔离 / 三分组 / 未批准不可采用 / 维持原设计 / 旧库迁移 / "
          "P2 审批零回归 / py3.8)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
