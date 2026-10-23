# -*- coding: utf-8 -*-
"""
optimization_loop.py -- 优化会话编排与持久化（票 25）

对应 ADR-0009（优化闭环：单一设计向量驱动双模型；**每轮产生新 Study，绝不覆盖**）、
ADR-0005（铺层是 CAE 属性 -> 纯铺层变动无需再生数模）、
ADR-0002（AI 三层：优化器是 L0 确定性搜索，**方案采用是 L2 人决策**）。
术语见 GLOSSARY：参数化包络 / 自然格 / 强度校核 / 可追溯性。

## 这一层补的是"从算法到平台"的那一步

`optimizer` 只会搜索，它注入的 `evaluate(向量)` 回调是空的；
`objectives` 只会归一，它不知道 Study 怎么跑。
本模块把那两者接到真实的 `run_study` 上，并把整个过程**落库**：

    设计向量 --design_vector--> (参数集, 铺层)
             --run_study-----> 一个**独立 Study**（含数模/拓扑/网格/odb/校核/报告）
             --objectives----> 统一评估结果（目标值 / 约束 / 可行性 / 故障）
             --本模块---------> 优化会话 + 设计点两表，可查询、可回溯

## 四件必须做对的事

### 1. 每个设计点 = 一个独立 Study，绝不覆盖

ADR-0009 的硬要求。每个点用新的 study_id（`<session>-p<序号>`），于是
artifact 版本号天然从 1 开始、互不覆盖；血缘不会被后来的优化轮次冲掉。
这是"事后能回答当初为什么选这个方案"的前提。

### 2. 纯铺层变动不碰 CATIA

`design_vector.needs_regeneration()` 判定"几何变没变"，没变就用
`RunOptions.reuse_catpart_from` 复用上一轮数模 —— 走 `run_study` 的复用分支，
**CATIA 驱动的 regenerate() 一次都不会被调用**。这是 ADR-0005 点名的成本优化，
也是本票最值得观测的验收点（调用次数是可数的）。

### 3. 技术故障 ≠ 目标很差

单点求解失败/参数越界/网格破戒都记成**故障点**（`objectives` 的三分组语义：
可行 / 不可行 / 故障），**记录原因后继续**跑下一个点。绝不因为一个点炸了
就中断整次优化，也绝不把故障当成"这个区域目标值很差"而绕开它。

### 4. 建议采用 ≠ 已采用（ADR-0002 的 L2 门）

优化结束只给出"建议采用的设计点"（`recommendation`）。**能不能采用由人决定**：
必须由人调用 `record_decision()` 记录 `adopt`（或 `keep_original` 维持原设计），
决定写进 P1 的 decisions 审计表。在有人批准之前，
`adopted_point()` 恒为 None、`require_adopted_point()` 直接抛错 ——
"未经批准就成了采用方案"在这套数据结构上做不到。

## 数据库演进

两张新表（`optimization_sessions` / `optimization_points`）加进 `db.SCHEMA`
（`CREATE TABLE IF NOT EXISTS`，打开旧库时幂等建出），**不改任何既有表结构**，
所以 P1/P2 的历史库打开后照常可用。这与 P2 的 `report_approvals` 是同一套做法。

## 依赖

只用标准库 + core 内既有模块（db/study/run_study/design_vector/objectives/
optimizer/approval）。**不 import numpy**（目标机 Win7 + py3.8）。
兼容 Python 3.8。
"""

import datetime
import json
import math
import os
import uuid

try:
    from . import approval
    from . import design_vector
    from . import objectives
    from . import optimizer
    from . import study as study_mod
    from . import surrogate as surrogate_mod
    from . import template_registry
    from . import textfmt
except ImportError:  # 允许在 core 目录内直接运行
    import approval
    import design_vector
    import objectives
    import optimizer
    import study as study_mod
    import surrogate as surrogate_mod
    import template_registry
    import textfmt


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 会话状态
SESSION_CREATED = "created"     # 已创建，尚未开始评估
SESSION_RUNNING = "running"     # 正在评估
SESSION_FINISHED = "finished"   # 评估结束（有三分组结论）
SESSION_FAILED = "failed"       # 编排本身失败（不是单点故障）

SESSION_STATUSES = (SESSION_CREATED, SESSION_RUNNING, SESSION_FINISHED,
                    SESSION_FAILED)

#: 目标（P3 只做减重；给 maximize 会明确报错而不是静默忽略）
OBJECTIVE_MINIMIZE = "minimize"
OBJECTIVE_MAXIMIZE = "maximize"

#: 约束声明的默认口径（与 objectives.constraints 的来源一致，不另造一套）
DEFAULT_CONSTRAINT = {
    "name": "strength",
    "source": objectives.CHECK_SUMMARY_SOURCE,
    "require": "all_pass",
    "note": "强度校核合格（FI/MS，ADR-0007 确定性判定）是唯一可行性依据",
}

#: 默认优化意图：要优化什么，是**记录在案的事实**
DEFAULT_INTENT = {
    "objective": {
        "name": "mass",
        "direction": OBJECTIVE_MINIMIZE,
        "unit": "kg",
        "note": "结构质量最小化（口径见 objectives.MASS_BASIS）",
    },
    "constraints": [dict(DEFAULT_CONSTRAINT)],
    "note": "P3 默认意图：在强度校核合格的前提下减重",
}

#: 设计点来源
SOURCE_BASELINE = "baseline"   # 会话起点：设计向量声明里的默认值
SOURCE_DOE = "doe"             # 优化器的 DOE 采样
SOURCE_LOCAL = "local"         # 优化器的邻域搜索
SOURCE_MANUAL = "manual"       # 调用方单独评估一个点
#: 票27：该点是**代理模型建议优先评估**的（只影响"先算哪个"，不影响合格判定）
SOURCE_SURROGATE = "surrogate"

#: 三分组名（与 optimizer.GROUP_NAMES 同一口径，不另造命名）
#
# 本模块对外用**名字**（"feasible"/"infeasible"/"failed"），optimizer 内部用
# **整数** 0/1/2。两套表示共存是有意的：DB 列存名字（可读、可查询、加组不改表），
# 优化器内部用整数（比较快、与 objectives.sort_key 的 group 位一致）。
# 但**必须有一个唯一换算处**，否则同一字段两种类型并存 —— 那正是"最优点标记
# 静默失效"的成因（0 != "feasible" 恒为真）。
GROUP_FEASIBLE = "feasible"
GROUP_INFEASIBLE = "infeasible"
GROUP_FAILED = "failed"

_GROUP_BY_INT = {
    optimizer.GROUP_FEASIBLE: GROUP_FEASIBLE,
    optimizer.GROUP_INFEASIBLE: GROUP_INFEASIBLE,
    optimizer.GROUP_FAILED: GROUP_FAILED,
}
_GROUP_TO_INT = dict((v, k) for k, v in _GROUP_BY_INT.items())


def group_name_of(group):
    """整数分组 -> 名字（未知一律归 failed，绝不静默丢点）。"""
    return _GROUP_BY_INT.get(group, GROUP_FAILED)


def _group_of_name(name):
    """名字 -> 整数分组（与 optimizer.GROUP_* 一致）；未知归 failed。"""
    return _GROUP_TO_INT.get(name, optimizer.GROUP_FAILED)

# ---------------------------------------------------------------------------
# 票27：代理模型快筛（可选，默认关闭）
# ---------------------------------------------------------------------------

#: 快筛状态 —— 汇总里据此区分"代理在起作用"与"确定性路径"
SURROGATE_OFF = "off"            # 未启用（默认）
SURROGATE_WAITING = "waiting"    # 已启用，但样本还不够，尚未拟合
SURROGATE_ACTIVE = "active"      # 拟合成功且误差达标 -> 参与排序
SURROGATE_DEGRADED = "degraded"  # 拟合过但不可用（样本不足/误差超标/矩阵奇异）-> 不排序

#: 默认快筛策略。**默认关闭**：数据不够或算力不足时闭环退化为纯确定性路径。
DEFAULT_SURROGATE_POLICY = {
    "enabled": False,      # 总开关（票27 验收项：默认关闭时与 #25 一致）
    "prefetch": 2,         # 每轮用代理挑几个点先算（只影响"先算哪个"）
    "max_mae_ratio": surrogate_mod.DEFAULT_MAX_MAE_RATIO,
    "max_rmse_ratio": surrogate_mod.DEFAULT_MAX_RMSE_RATIO,
}


def normalize_surrogate_policy(policy=None):
    """
    归一化快筛策略；None / 空 dict 得到**默认关闭**的策略。

    非法值早报（而不是跑到一半才发现 prefetch 是字符串）。
    """
    out = dict(DEFAULT_SURROGATE_POLICY)
    if policy:
        if not isinstance(policy, dict):
            raise OptimizationLoopError(
                "快筛策略 surrogate 应为 dict，实际 %r"
                % type(policy).__name__, code="invalid-intent")
        out.update(policy)

    out["enabled"] = bool(out.get("enabled", False))
    try:
        prefetch = int(out.get("prefetch", 2))
    except (TypeError, ValueError):
        raise OptimizationLoopError("surrogate.prefetch 必须是整数，实际 %r"
                                    % (out.get("prefetch"),),
                                    code="invalid-intent")
    out["prefetch"] = max(0, prefetch)
    for key in ("max_mae_ratio", "max_rmse_ratio"):
        try:
            value = float(out[key])
        except (TypeError, ValueError):
            raise OptimizationLoopError("surrogate.%s 必须是数值，实际 %r"
                                        % (key, out.get(key)),
                                        code="invalid-intent")
        if value <= 0.0:
            raise OptimizationLoopError("surrogate.%s 必须为正数，实际 %r"
                                        % (key, value), code="invalid-intent")
        out[key] = value
    return out


def public_surrogate_policy(policy):
    """
    只保留可 JSON 序列化的策略键（用于落库快照）。

    策略里允许注入 `fit`（测试用替身/上层自定义拟合器）——那是运行期对象，
    不能进数据库。快照里去掉它，保证"当初的策略"是可读、可复算的数据。
    """
    out = {}
    for key, value in (policy or {}).items():
        if callable(value):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
    return out




class OptimizationLoopError(ValueError):
    """
    编排层错误（会话不存在 / 声明不合法 / 未获批就取采用方案 / 单点无法评估）。

    `.code` 供程序判断：
      invalid-session   会话不存在或已关闭
      invalid-intent    优化意图声明不合法
      invalid-spec      设计向量声明不合法
      no-points         会话还没有任何设计点
      not-adopted       **未经人批准**，不能当作采用方案
      decision-invalid  采用决策不合法（未知决策/无权角色）
      study-exists      设计点的 Study 已存在（绝不覆盖）
    """

    def __init__(self, reason, code="invalid-session"):
        ValueError.__init__(self, reason)
        self.reason = reason
        self.code = code

    def __str__(self):
        return self.reason


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(text, default=None):
    return textfmt.json_loads(text, default)


def new_session_id(prefix="OPT"):
    """生成一个稳定可读的会话 id（时间 + 随机后缀，可重复调用不撞）。"""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return "%s-%s-%s" % (prefix, stamp, uuid.uuid4().hex[:4])


# ---------------------------------------------------------------------------
# 优化意图（"当初想优化什么"）
# ---------------------------------------------------------------------------

def normalize_intent(intent=None):
    """
    归一化优化意图声明：目标 + 约束 + 备注。

    intent 可为 None（用 `DEFAULT_INTENT`）或部分覆盖的 dict::

        {"objective": {"name": "mass", "direction": "minimize"},
         "constraints": [{"name": "strength", "require": "all_pass"}],
         "note": "..."}

    意图是**记录在案的事实**，所以要早校验：方向写错、目标不是 mass、
    约束没写判定依据，都在这里报错，而不是等跑完一堆 Study 才发现。
    """
    out = {
        "objective": dict(DEFAULT_INTENT["objective"]),
        "constraints": [dict(c) for c in DEFAULT_INTENT["constraints"]],
        "note": DEFAULT_INTENT["note"],
    }
    if intent is None:
        return out
    if not isinstance(intent, dict):
        raise OptimizationLoopError(
            "优化意图 intent 应为 dict，实际 %r" % type(intent).__name__,
            code="invalid-intent")

    obj = intent.get("objective")
    if obj is not None:
        if not isinstance(obj, dict):
            raise OptimizationLoopError("intent.objective 应为 dict",
                                        code="invalid-intent")
        merged = dict(out["objective"])
        merged.update(obj)
        direction = str(merged.get("direction") or "").lower()
        if direction not in (OBJECTIVE_MINIMIZE, OBJECTIVE_MAXIMIZE):
            raise OptimizationLoopError(
                "intent.objective.direction 只能是 minimize / maximize，实际 %r"
                % (merged.get("direction"),), code="invalid-intent")
        if direction == OBJECTIVE_MAXIMIZE:
            raise OptimizationLoopError(
                "P3 的目标方向只支持 minimize（减重）；要最大化请另立目标口径，"
                "不要在优化器里翻转比较方向", code="invalid-intent")
        if not merged.get("name"):
            raise OptimizationLoopError("intent.objective 缺少 name",
                                        code="invalid-intent")
        out["objective"] = merged

    cons = intent.get("constraints")
    if cons is not None:
        if not isinstance(cons, (list, tuple)) or not cons:
            raise OptimizationLoopError(
                "intent.constraints 应为非空列表（没有约束的优化是危险的："
                "会挑出一个强度不合格的最轻方案）", code="invalid-intent")
        normalized = []
        for i, item in enumerate(cons):
            if not isinstance(item, dict):
                raise OptimizationLoopError(
                    "intent.constraints[%d] 应为 dict，实际 %r"
                    % (i, type(item).__name__), code="invalid-intent")
            if not item.get("name"):
                raise OptimizationLoopError(
                    "intent.constraints[%d] 缺少 name" % i, code="invalid-intent")
            merged = dict(DEFAULT_CONSTRAINT)
            merged.update(item)
            normalized.append(merged)
        out["constraints"] = normalized

    if "note" in intent:
        out["note"] = intent["note"]
    return out


# ---------------------------------------------------------------------------
# 会话：创建 / 读取
# ---------------------------------------------------------------------------

_SESSION_COLUMNS = ("id, name, template_id, intent_json, budget_json, "
                    "design_vector_json, status, result_json, adoption_json, "
                    "note, created_at, updated_at")


def _session_from_row(row):
    """DB 行 -> 会话 dict（JSON 字段解析出来，便于直接读）。"""
    if row is None:
        return None
    return {
        "session_id": row["id"],
        "name": row["name"],
        "template_id": row["template_id"],
        "intent": _loads(row["intent_json"], {}) or {},
        "budget": _loads(row["budget_json"], {}) or {},
        "design_vector": _loads(row["design_vector_json"], {}) or {},
        "status": row["status"],
        "result": _loads(row["result_json"], None),
        "adoption": _loads(row["adoption_json"], None),
        "note": row["note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_session(conn, design_vector_spec, template_id=None, intent=None,
                   budget=None, name=None, note=None, session_id=None,
                   surrogate=None):
    """
    创建优化会话：意图 + 收敛配置（预算三元组）+ 设计向量声明快照 **全部落库**。

    设计向量声明在创建时就**快照**存下（不是引用外部文件）：优化跑完后
    声明文件改了、删了，都还能回答"当初声明的搜索空间长什么样"。
    这与 artifact 不可变是同一个道理。

    conn                 : 已打开的数据库连接
    design_vector_spec   : 设计向量声明（dict 或已归一化），见 design_vector 模块
    template_id          : 模板族；None 时取声明里的 template_id
    intent               : 优化意图（见 `normalize_intent`）；None = 默认减重意图
    budget               : 预算三元组 {max_evals, max_rounds, improvement_threshold}
    session_id           : 可选，显式指定（测试/复现用）；None 则自动生成
    surrogate            : 可选，快筛策略（票27）；**None = 默认关闭**。
                           建会话时把开关记进意图快照，于是"当初是否启用快筛"
                           也是记录在案的事实。

    返回会话 dict（与 `get_session` 同形）。
    """
    try:
        spec = design_vector.normalize_spec(design_vector_spec)
    except design_vector.DesignVectorError as exc:
        raise OptimizationLoopError("设计向量声明不合法: %s" % exc,
                                    code="invalid-spec")

    tpl_id = template_id or spec.get("template_id")
    if not tpl_id:
        raise OptimizationLoopError(
            "缺少 template_id：设计向量声明里没有，参数里也没给（无法确定部件族）",
            code="invalid-spec")

    normalized_intent = normalize_intent(intent)
    # 票27：快筛开关记进意图快照（可追溯"当初有没有启用代理快筛"）
    normalized_intent["surrogate"] = public_surrogate_policy(
        normalize_surrogate_policy(surrogate))

    try:
        bud = optimizer.normalize_budget(budget)
    except optimizer.OptimizerError as exc:
        raise OptimizationLoopError("收敛配置（预算）不合法: %s" % exc,
                                    code="invalid-intent")

    sid = session_id or new_session_id()
    now = _now()
    existing = conn.execute("SELECT id FROM optimization_sessions WHERE id=?",
                            (sid,)).fetchone()
    if existing is not None:
        raise OptimizationLoopError(
            "优化会话 %r 已存在（会话不可覆盖）" % (sid,), code="study-exists")

    conn.execute(
        "INSERT INTO optimization_sessions(id, name, template_id, intent_json, "
        "budget_json, design_vector_json, status, result_json, adoption_json, "
        "note, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, name or sid, tpl_id, _dumps(normalized_intent), _dumps(bud),
         _dumps(spec), SESSION_CREATED, None, None, note, now, now))
    conn.commit()
    return get_session(conn, sid)


def get_session(conn, session_id):
    """读一个会话（含意图/预算/声明快照）；不存在抛 `OptimizationLoopError`。"""
    row = conn.execute(
        "SELECT %s FROM optimization_sessions WHERE id=?" % _SESSION_COLUMNS,
        (session_id,)).fetchone()
    if row is None:
        raise OptimizationLoopError("优化会话 %r 不存在" % (session_id,))
    return _session_from_row(row)


def find_session(conn, session_id):
    """读一个会话；不存在返回 None（用于探测，不抛）。"""
    row = conn.execute(
        "SELECT %s FROM optimization_sessions WHERE id=?" % _SESSION_COLUMNS,
        (session_id,)).fetchone()
    return _session_from_row(row)


def list_sessions(conn):
    """全部优化会话（按创建时间升序）。"""
    rows = conn.execute(
        "SELECT %s FROM optimization_sessions ORDER BY created_at, id"
        % _SESSION_COLUMNS).fetchall()
    return [_session_from_row(r) for r in rows]


def _set_session_status(conn, session_id, status, result=None, adoption=None):
    """更新会话状态（以及可选的结论/采用快照）。"""
    sets = ["status=?", "updated_at=?"]
    args = [status, _now()]
    if result is not None:
        sets.append("result_json=?")
        args.append(_dumps(result))
    if adoption is not None:
        sets.append("adoption_json=?")
        args.append(_dumps(adoption))
    args.append(session_id)
    conn.execute("UPDATE optimization_sessions SET %s WHERE id=?"
                 % ", ".join(sets), tuple(args))
    conn.commit()


# ---------------------------------------------------------------------------
# 声明 -> 优化器变量（数据驱动，不解释具体变量名）
# ---------------------------------------------------------------------------

def optimizer_variables(design_vector_spec):
    """
    设计向量声明 -> 优化器变量声明（`optimizer.normalize_variables` 的口径）。

    两套声明只差形状，这里做**唯一一次**翻译，避免两处各写一份映射而漂移。
    离散变量的候选值必须是整数（角度用 `angle_table` 档位索引），因为优化器
    要为离散变量做 ±1 的邻域搜索 —— 见票22 的"整数档位 + 角度表"设计。
    """
    spec = design_vector._as_spec(design_vector_spec)
    out = []
    for var in spec["variables"]:
        item = {
            "name": var["name"],
            "type": var["type"],
            "category": var["category"],
            "min": var.get("min", 0.0),
            "max": var.get("max", 1.0),
        }
        if var["type"] == design_vector.TYPE_DISCRETE:
            cands = var.get("candidates")
            if cands is not None:
                if not all(isinstance(c, int) and not isinstance(c, bool)
                           for c in cands):
                    raise OptimizationLoopError(
                        "离散变量 %r 的候选值必须是整数（优化器只对整数做 ±1 邻域"
                        "搜索）；角度请用 ply_sequence.angle_table 的档位索引，"
                        "实际候选 %r" % (var["name"], cands), code="invalid-spec")
                item["candidates"] = list(cands)
            else:
                step = var.get("step", 1)
                if not (isinstance(step, int) and not isinstance(step, bool)):
                    raise OptimizationLoopError(
                        "离散变量 %r 的 step 必须是整数，实际 %r"
                        % (var["name"], step), code="invalid-spec")
                item["step"] = step
                item["min"] = int(var["min"])
                item["max"] = int(var["max"])
        out.append(item)
    return optimizer.normalize_variables(out)


# ---------------------------------------------------------------------------
# 设计点：持久化
# ---------------------------------------------------------------------------

_POINT_COLUMNS = ("id, session_id, seq, vector_json, vector_key, study_id, "
                  "group_name, ok, feasible, objective_value, evaluation_json, "
                  "error_json, source, reused_from, created_at")


def _artifacts_of_study(conn, study_id):
    """一个 Study 的产物（kind -> id）；供"设计点 -> 血缘"一跳查询用。"""
    if not study_id:
        return {}
    rows = conn.execute(
        "SELECT id, kind FROM artifacts WHERE study_id=? ORDER BY id",
        (study_id,)).fetchall()
    out = {}
    for r in rows:
        out[r["kind"]] = r["id"]
    return out


def _point_from_row(conn, row, best_key=None):
    """DB 行 -> 设计点 dict（含完整评估结果与产物关联）。"""
    if row is None:
        return None
    evaluation = _loads(row["evaluation_json"], None) or {}
    point = {
        "point_id": row["id"],
        "session_id": row["session_id"],
        "seq": row["seq"],
        "vector": _loads(row["vector_json"], {}) or {},
        "vector_key": row["vector_key"],
        "study_id": row["study_id"],
        # group 必须是**整数** 0/1/2（与 optimizer.GROUP_* / objectives.sort_key 一致）。
        # 这里曾误用 GROUP_NAMES.get(...) 取到字符串 "feasible"，于是同一字段在
        # 循环内部是 int、从库读回是 str —— 任何 `p["group"] == GROUP_FEASIBLE`
        # 恒为 False（0 != "feasible"），"是否最优点"会静默判错。
        "group": _group_of_name(row["group_name"]),
        "group_name": row["group_name"],
        "ok": bool(row["ok"]),
        "feasible": (None if row["feasible"] is None else bool(row["feasible"])),
        "objective_value": row["objective_value"],
        "constraints": evaluation.get("constraints"),
        "error": _loads(row["error_json"], None),
        "source": row["source"],
        "reused_from": row["reused_from"],
        "evaluation": evaluation,
        "artifacts": _artifacts_of_study(conn, row["study_id"]),
        "created_at": row["created_at"],
        "is_best": (best_key is not None and row["vector_key"] == best_key),
    }
    return point


def _insert_point(conn, session_id, seq, vector, evaluation, study_id,
                  source, reused_from=None):
    """
    写入一个设计点（**唯一写入路径**：摘要列与完整评估结果在这里一起落）。

    摘要列（group_name/ok/feasible/objective_value）是为 SQL 分组查询准备的
    冗余；`evaluation_json` 才是权威。两者在同一个函数里写，不可能漂移。
    """
    key = optimizer.vector_key(vector)
    group = objective_group(evaluation)
    err = evaluation.get("error")
    cur = conn.execute(
        "INSERT INTO optimization_points(session_id, seq, vector_json, "
        "vector_key, study_id, group_name, ok, feasible, objective_value, "
        "evaluation_json, error_json, source, reused_from, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, int(seq), _dumps(vector), key, study_id,
         group_name_of(group),
         1 if evaluation.get("ok") is True else 0,
         (None if evaluation.get("feasible") is None
          else (1 if evaluation.get("feasible") else 0)),
         (evaluation.get("objective") or {}).get("value"),
         _dumps(evaluation), _dumps(err) if err else None,
         source, reused_from, _now()))
    conn.commit()
    return int(cur.lastrowid)


def _next_seq(conn, session_id):
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS s FROM optimization_points "
        "WHERE session_id=?", (session_id,)).fetchone()
    return int(row["s"]) + 1


def objective_group(evaluation):
    """
    设计点分组（0 可行 / 1 不可行 / 2 技术故障）—— 直接复用 objectives.sort_key。

    **不在这里另定判定**：可行与否由 strength_check 的 all_pass 决定
    （ADR-0007），本模块只搬运结论。
    """
    return objectives.sort_key(evaluation or {})[0]


def _best_key_of(points):
    """当前最优可行点的 vector_key（无可行点 -> None）。与 optimizer._pick_best 同口径。"""
    best = None
    best_sort = None
    for p in points:
        if not objectives.is_feasible(p.get("evaluation")):
            continue
        sk = objectives.sort_key(p["evaluation"])
        if best_sort is None or sk < best_sort:
            best_sort, best = sk, p["vector_key"]
    return best


def list_points(conn, session_id):
    """
    会话内全部设计点（按评估顺序），每个点带 `is_best` 与产物关联。

    `is_best` 是**每次查询时重算**的，不是落库的标记位 —— 否则后来导入
    历史点、或续跑新增更优点，库里就会留下一个永远不更新的"最优"。
    """
    rows = conn.execute(
        "SELECT %s FROM optimization_points WHERE session_id=? ORDER BY seq, id"
        % _POINT_COLUMNS, (session_id,)).fetchall()
    points = [_point_from_row(conn, r) for r in rows]
    best_key = _best_key_of(points)
    for p in points:
        p["is_best"] = (best_key is not None and p["vector_key"] == best_key)
    return points


def groups(conn, session_id):
    """
    按「可行 / 不可行 / 评估失败」三组返回设计点（票25 验收项）。

    分组口径与 `optimizer` 完全一致（都走 `objectives.sort_key`），
    于是"优化器认为的最优"和"汇总里标的最优"不可能是两个不同的点。
    """
    out = {GROUP_FEASIBLE: [], GROUP_INFEASIBLE: [], GROUP_FAILED: []}
    for p in list_points(conn, session_id):
        out.setdefault(p["group_name"], []).append(p)
    return out


# ---------------------------------------------------------------------------
# 单点评估：设计向量 -> 独立 Study -> 评估结果
# ---------------------------------------------------------------------------

def _require_driver(catia):
    """
    校验 CATIA 驱动可用（**按协议方法**判断，不是 `callable()`）。

    驱动是一个对象（`CatiaDriver` 协议），它本身不可调用 —— 可调用的是
    `regenerate()`。用错判据会让真实驱动一律被拒，或让坏驱动溜过去。
    """
    if catia is None or not callable(getattr(catia, "regenerate", None)):
        raise OptimizationLoopError(
            "缺少可用的 CATIA 驱动：优化闭环要跑真实 Study，必须提供实现了 "
            "regenerate() 的驱动（测试用 FakeCatiaDriver）")


def _run_options_class():
    """延迟导入 RunOptions（run_study 会 import 很多模块，避免入口处循环 import）。"""
    try:
        from .run_study import RunOptions
    except ImportError:
        from run_study import RunOptions
    return RunOptions


def default_point_options(work_dir, study_id, plies=None):
    """
    一个设计点的标准全链选项：**独立目录** + 全阶段开关 + 不生成报告。

    报告按点生成会显著拖慢一次优化（每个点一份 docx + 图），而优化的目的
    是搜索；选定方案后再对它单独跑一次带报告的全链即可。
    """
    RunOptions = _run_options_class()

    def dir_for(sub):
        return os.path.join(work_dir, study_id, sub)

    opts = RunOptions.full_chain(dir_for, plies=plies or None,
                                 with_report=False)
    return opts


def _point_options(base_options, work_dir, study_id, plies, reuse_from):
    """
    该设计点专用的 RunOptions：独立目录 + 本次铺层 + （可能）复用数模。

    **不改 run_study 签名**：所有差异都通过 RunOptions 字段表达。
    目录必须逐点独立，否则多个设计点的产物会互相覆盖（ADR-0009）。
    """
    if base_options is None:
        base = default_point_options(work_dir, study_id, plies)
    else:
        base = base_options

    def dir_for(sub):
        return os.path.join(work_dir, study_id, sub)

    return base.replace(
        params_dir=dir_for("params"),
        topo_dir=dir_for("topo"),
        fe_dir=dir_for("fe"),
        solve_dir=dir_for("solve"),
        check_dir=dir_for("check"),
        report_dir=dir_for("report"),
        plies=plies,
        reuse_catpart_from=reuse_from)


def _reuse_candidate(conn, session, spec, vector):
    """
    判断这个设计点能否复用上一轮数模，能则返回源 study_id。

    分工（票22）：**是否该复用**由 `design_vector.needs_regeneration` 判定
    （只看几何变量变没变）；**复用时会不会出错**由 run_study 自己再校验一次
    参数集一致性（它拿不到声明，只能看参数集）。两道守卫都在，不会出现
    "数模是旧几何、拓扑是新几何"的错配血缘。
    """
    rows = conn.execute(
        "SELECT %s FROM optimization_points WHERE session_id=? AND study_id "
        "IS NOT NULL AND group_name != ? ORDER BY seq DESC"
        % _POINT_COLUMNS,
        (session["session_id"], GROUP_FAILED)).fetchall()
    if not rows:
        return None, None
    prev = _point_from_row(conn, rows[0])
    prev_vector = prev.get("vector") or {}
    try:
        decision = design_vector.needs_regeneration(spec, vector, prev_vector)
    except design_vector.DesignVectorError:
        return None, None
    if decision["needs_regeneration"]:
        return None, decision
    return prev["study_id"], decision


def evaluate_point(conn, session_id, vector, catia, options=None, work_dir=None,
                   study_id=None, source=SOURCE_MANUAL, abaqus=None,
                   template=None):
    """
    评估**一个设计点**：映射 -> 跑独立 Study -> 归一评估 -> 落库。

    这是"设计点 = 独立 Study"的落地点，也是失败隔离的边界：
    任何异常都在这里被收成 `objectives.evaluate(error=...)` 的故障点并落库，
    **不向上抛**（除非是编排配置错误，那种跑下去也没意义）。

    conn        : 数据库连接
    session_id  : 会话 id
    vector      : 设计向量（会经 design_vector.coerce_vector 归一）
    catia       : CatiaDriver（复用分支下**一次都不会被调用**）
    options     : 基础 RunOptions（含 abaqus/rules 等）；None 时用默认全链
    work_dir    : 各点产物的根目录
    study_id    : 可选，显式指定该点的 Study id（默认按序号生成）
    source      : 该点从哪来（baseline/doe/local/manual）
    abaqus      : 可选，覆盖 options.abaqus
    template    : 可选，已加载的模板（省一次读盘）

    返回设计点 dict（`list_points` 的元素）。
    """
    _require_driver(catia)
    session = get_session(conn, session_id)
    spec = design_vector._as_spec(session["design_vector"])
    tpl_id = session["template_id"]

    if work_dir is None:
        work_dir = default_work_dir()

    normalized = design_vector.coerce_vector(spec, vector)
    sid = study_id or "%s-p%03d" % (session_id, _next_seq(conn, session_id))

    # ---- 该点的独立 Study：先建行，于是"评估过的点都有 Study"始终成立 ----
    exists = conn.execute("SELECT id FROM studies WHERE id=?", (sid,)).fetchone()
    if exists is not None:
        raise OptimizationLoopError(
            "设计点的 Study %r 已存在（优化绝不覆盖既有 Study，ADR-0009）"
            % (sid,), code="study-exists")
    study_mod.create_study(sid, "%s / %s" % (session["name"], sid),
                           note="优化会话 %s 的设计点" % session_id, conn=conn)

    if template is None:
        template = template_registry.load(
            tpl_id, (options.templates_dir if options is not None else None))

    try:
        inputs = design_vector.to_inputs(spec, normalized, template=template)
    except design_vector.DesignVectorError as exc:
        # 越包络等声明层错误：记成故障点继续，不中断整次优化
        evaluation = objectives.evaluate(
            error=exc, study_id=sid, design=normalized)
        return _persist_point(conn, session_id, normalized, evaluation, sid,
                              source, None)

    reuse_from, _decision = _reuse_candidate(conn, session, spec, normalized)

    try:
        try:
            from . import run_study as run_study_mod
        except ImportError:
            import run_study as run_study_mod

        opts = _point_options(options, work_dir, sid, inputs["plies"],
                              reuse_from)
        if abaqus is not None:
            opts = opts.replace(abaqus=abaqus)
        result = run_study_mod.run_study(sid, inputs["params"], tpl_id,
                                         catia, conn, opts)
        evaluation = objectives.evaluate(result=result, study_id=sid,
                                         design=normalized)
        reused_from = None
        if result.get("catpart_reuse"):
            reused_from = result["catpart_reuse"].get("from_study")
    except Exception as exc:   # 失败隔离：单点故障不中断整次优化
        evaluation = objectives.evaluate(error=exc, study_id=sid,
                                         design=normalized)
        reused_from = None

    return _persist_point(conn, session_id, normalized, evaluation, sid,
                          source, reused_from)


def default_work_dir():
    """各设计点产物的默认根目录（与 P2 一致：应用目录下的 data/work_optimization）。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "data", "work_optimization")


def _persist_point(conn, session_id, vector, evaluation, study_id, source,
                   reused_from):
    """把一个已评估的点写库并返回公开形态（唯一写入口，见 `_insert_point`）。"""
    seq = _next_seq(conn, session_id)
    point_id = _insert_point(conn, session_id, seq, vector, evaluation,
                             study_id, source, reused_from)
    row = conn.execute(
        "SELECT %s FROM optimization_points WHERE id=?" % _POINT_COLUMNS,
        (point_id,)).fetchone()
    return _point_from_row(conn, row)


# ---------------------------------------------------------------------------
# 整次优化：接到 optimizer 上
# ---------------------------------------------------------------------------

def run_optimization(conn, session_id, catia, options=None, work_dir=None,
                     abaqus=None, seed=0, budget=None, max_evals=None,
                     surrogate=None):
    """
    跑一次完整优化：基线点 -> (可选)代理快筛预取 -> DOE -> 邻域/步长收缩 -> 汇总。

    基线点先评估（设计向量声明里的默认值 = 当前设计）：它既是搜索起点，
    也是「维持原设计」这个决策的对照基准。之后交给 `optimizer.optimize`
    注入回调 —— 优化器对此毫无感知，仍只认 `evaluate(向量)`。

    **每个设计点都真的跑一个独立 Study**（ADR-0009）。没提供替身评估的捷径，
    因为"Study 数 == 评估点数"是本票的验收项，捷径会让它在测试里永远成立、
    在生产里悄悄不成立。速度由 fake 适配器负责，不靠绕过 Study。

    conn / session_id : 会话
    catia             : CatiaDriver
    options           : 基础 RunOptions（含 abaqus/rules 等）；None 用默认全链
    work_dir          : 各点产物根目录
    abaqus            : 可选，覆盖求解器
    seed              : 随机种子（同种子同结果）
    budget            : 覆盖会话里记录的预算（默认用会话记录的）
    max_evals         : 可选，覆盖 max_evals（快速冒烟用）
    surrogate         : 可选，快筛策略（票27）。**默认 None = 关闭**，
                        此时轨迹与纯确定性路径完全一致。
                        形如 {"enabled": True, "prefetch": 2, ...}

    返回 dict（与 `summarize` 同形）：会话、停止原因、三分组、最优、建议采用点。
    """
    session = get_session(conn, session_id)
    spec = design_vector._as_spec(session["design_vector"])
    variables = optimizer_variables(spec)

    bud = dict(session["budget"])
    if budget:
        bud.update(budget)
    if max_evals is not None:
        bud["max_evals"] = int(max_evals)
    bud = optimizer.normalize_budget(bud)

    if work_dir is None:
        work_dir = default_work_dir()

    # 票27：快筛策略归一（默认关闭）。关闭时下面所有快筛分支都不执行。
    surro_policy = normalize_surrogate_policy(surrogate)

    _set_session_status(conn, session_id, SESSION_RUNNING)

    def evaluate_vector(vector, source):
        """跑一个独立 Study 并落库（快筛预取与优化器回调共用这一个入口）。"""
        return evaluate_point(conn, session_id, vector, catia, options=options,
                              work_dir=work_dir, abaqus=abaqus, source=source)

    # ---- 1) 基线点：先评估"当前设计"，它是搜索起点与维持原设计的对照 ----
    baseline_vector = design_vector.default_vector(spec)
    if _find_point(conn, session_id, optimizer.vector_key(baseline_vector)) is None:
        evaluate_vector(baseline_vector, SOURCE_BASELINE)

    # ---- 2) 票27：代理模型快筛（只影响"先算哪个"） ----
    #
    # 顺序说明：**先让确定性优化器跑一段攒样本，再拟合 -> 预取**。
    # 为什么不是"一开始就拟合"：刚起步时只有基线点，任何代理都无从拟合；
    # 而 #23 的优化器是"DOE 起步 + 邻域搜索"，它自己就会产出前几个样本。
    # 于是这里把闭环切成两段：
    #   (a) 先跑一小段确定性搜索，攒够 min_samples；
    #   (b) 拟合成功且误差达标 -> 用代理挑接下来的 prefetch 个点先算；
    #   (c) 再用这些点作为 history 续跑优化器（它不会重算已有的点）。
    # 这样"累积 -> 拟合 -> 排序"三步都真实发生，#25 的确定性路径只是其一特例。
    #
    # **预算必须全局算**：`optimizer.optimize` 的 max_evals 是**每次调用**的上限，
    # 而这里会调用它两次（攒样本 + 正式搜索）。若各自给满预算，总评估次数就会
    # 翻倍、"同预算对照"也就失去意义。故每次都只给**剩余额度**。
    surro_state = _surrogate_off_state()
    n_prefetched = 0
    opt_result = None
    eval_budget = [int(bud["max_evals"])]        # 剩余可评估次数（可变）

    def _spent():
        return len(list_points(conn, session_id))

    if surro_policy["enabled"]:
        variables_min = surrogate_mod.min_samples_for(variables)
        # (a) 攒样本：目标刚好够拟合（不多烧预算），再留出余量给正式搜索
        usable_now = len(surrogate_mod.usable_points(
            _history_records(conn, session_id))[0])
        need = max(1, variables_min - usable_now)
        spare = max(0, eval_budget[0] - _spent() - 1)
        warm_evals = max(1, min(need, spare))
        try:
            optimizer.optimize(
                variables,
                lambda v: evaluate_vector(v, SOURCE_DOE)["evaluation"],
                budget={"max_evals": warm_evals,
                        "max_rounds": max(1, min(2, bud["max_rounds"])),
                        "improvement_threshold": bud["improvement_threshold"]},
                seed=seed, history=_history_records(conn, session_id))
        except Exception:
            pass            # 攒样本阶段失败不该拖垮整次优化（下面还会再跑）

        # (b) 拟合 -> 误差达标才启用排序
        surro_state, model = _fit_surrogate(conn, session_id, variables,
                                            surro_policy)
        if model is not None:
            # (c) 用代理挑几个点先算（只影响顺序）
            n_prefetched = _prefetch_by_surrogate(
                conn, session_id, variables, model, surro_policy, bud,
                evaluate_vector, seed=seed)
            surro_state = _surrogate_state(
                surro_policy, SURROGATE_ACTIVE, model=model,
                n_prefetched=n_prefetched)

    # ---- 3) 交给优化器（它只认注入的 evaluate 回调），只给剩余预算 ----
    remaining = max(1, eval_budget[0] - _spent())
    main_budget = dict(bud)
    main_budget["max_evals"] = remaining
    history = _history_records(conn, session_id)

    def _callback(vector):
        """优化器每要一个点，就真的跑一个独立 Study，再把归一结果还给它。"""
        return evaluate_vector(vector, SOURCE_DOE)["evaluation"]

    try:
        opt_result = optimizer.optimize(variables, _callback, budget=main_budget,
                                        seed=seed, history=history)
    except Exception as exc:
        _set_session_status(
            conn, session_id, SESSION_FAILED,
            result={"error": str(exc), "surrogate": surro_state})
        raise OptimizationLoopError("优化编排失败: %s" % exc, code="run-failed")

    # ---- 4) 结算：先把状态与结论快照落定，再返回汇总 ----
    # 顺序要紧：`summarize` 从库里重算，若先汇总后落状态，返回的 status 会停在
    # "running"（测试据此判断会话是否正常收尾）。
    _set_session_status(conn, session_id, SESSION_FINISHED,
                        result={
                            "stop_reason": opt_result.get("stop_reason"),
                            "stop_text": opt_result.get("stop_text"),
                            "n_evals": opt_result.get("n_evals"),
                            "n_cached": opt_result.get("n_cached"),
                            "n_rounds": opt_result.get("n_rounds"),
                            "budget": opt_result.get("budget"),
                            "best_key": (opt_result.get("best") or {}).get("key"),
                            # 票27：快筛状态随会话落库，汇总/重启后仍可查
                            "surrogate": surro_state,
                            "surrogate_n_prefetched": n_prefetched,
                            "finished_at": _now(),
                        })
    return summarize(conn, session_id, optimizer_result=opt_result)


def _find_point(conn, session_id, vector_key):
    row = conn.execute(
        "SELECT %s FROM optimization_points WHERE session_id=? AND vector_key=?"
        % _POINT_COLUMNS, (session_id, vector_key)).fetchone()
    return _point_from_row(conn, row) if row is not None else None


def _history_records(conn, session_id):
    """已落库的点 -> 优化器可消费的 history 记录（续跑/去重用）。"""
    out = []
    for p in list_points(conn, session_id):
        rec = optimizer.point_record(p["vector"], p["evaluation"],
                                     source=p["source"])
        rec["study_id"] = p["study_id"]
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# 票27：代理模型快筛（只影响"先算哪个"）
# ---------------------------------------------------------------------------

def _surrogate_off_state():
    """未启用快筛时的状态（汇总里据此说明"走的是纯确定性路径"）。"""
    return {
        "enabled": False,
        "status": SURROGATE_OFF,
        "available": False,
        "reason": "未启用代理模型快筛（默认关闭）：本轮为纯确定性 DOE + 邻域搜索",
        "n_samples": 0,
        "min_samples": 0,
        "n_prefetched": 0,
        "model_quality": None,
        "thresholds": None,
        "source": surrogate_mod.SOURCE_SURROGATE,
    }


def _surrogate_state(policy, status, model=None, reason=None, n_prefetched=0):
    """构造一份快筛状态快照（写进会话结果、汇总里可见）。"""
    model = model or {}
    return {
        "enabled": True,
        "status": status,
        "available": bool(model.get("available")),
        "degraded": bool(model.get("degraded", status == SURROGATE_DEGRADED)),
        "reason": reason or model.get("reason"),
        "n_samples": model.get("n_samples", 0),
        "min_samples": model.get("min_samples", 0),
        "n_prefetched": int(n_prefetched),
        "model_quality": model.get("quality"),
        "thresholds": model.get("thresholds"),
        # 只留可序列化的策略键（`fit` 是运行期对象，不进快照）
        "policy": public_surrogate_policy(policy),
        # 来源标记：所有代理相关结论都带它，汇总据此与实测区分
        "source": surrogate_mod.SOURCE_SURROGATE,
    }


def _normalized_distance(a, b, variables):
    """两个设计向量在**归一化设计空间**里的欧氏距离（各变量按范围缩放）。"""
    total = 0.0
    for spec in variables:
        name = spec["name"]
        if name not in a or name not in b:
            continue
        if spec.get("candidates"):
            cands = [float(c) for c in spec["candidates"]]
            span = max(cands) - min(cands)
        else:
            span = float(spec["max"]) - float(spec["min"])
        if span <= 0:
            span = 1.0
        d = (float(a[name]) - float(b[name])) / span
        total += d * d
    return math.sqrt(total)


def diversify_candidates(ranked, variables, limit, min_distance=0.35):
    """
    从代理排序里挑 `limit` 个**彼此分散**的候选（贪心最远点式）。

    为什么需要：`surrogate.suggest()` 的 `next_batch` 只是取排前 3 个，
    没有去重/分散。若前几名在设计空间里挤成一团（很常见：连续变量只差一点），
    真实评估就等于在同一小块区域重复烧算力，信息量极低。
    这里按排序优先、但跳过与已选点太近的候选，让有限的评估次数覆盖更宽。

    **只调整"先算哪些"，不改变任何合格判定** —— 选出来的点仍要各自跑完
    完整 Study 与强度校核。
    """
    picked = []
    for item in ranked or []:
        if len(picked) >= limit:
            break
        vector = item.get("vector") or {}
        if any(_normalized_distance(vector, p["vector"], variables) < min_distance
               for p in picked):
            continue
        picked.append(item)
    # 分散过滤太严导致没选够时，按原排序补齐（宁可少分散也别浪费预算）
    if len(picked) < limit:
        chosen = set(id(x) for x in picked)
        for item in ranked or []:
            if len(picked) >= limit:
                break
            if id(item) not in chosen:
                picked.append(item)
    return picked[:limit]


def _fit_surrogate(conn, session_id, variables, policy):
    """
    在**当前已累积的已评估点**上尝试拟合代理模型。

    返回 (state dict, model dict)。样本不足/拟合出错都归一成 degraded，
    **不抛异常**（快筛坏了不能拖垮整次优化）。
    """
    records = _history_records(conn, session_id)
    usable, n_excluded = surrogate_mod.usable_points(records)
    min_samples = surrogate_mod.min_samples_for(variables)

    if len(usable) < min_samples:
        return _surrogate_state(
            policy, SURROGATE_WAITING,
            model={"n_samples": len(usable), "min_samples": min_samples} ,
            reason="代理模型尚未启用：可用样本 %d 个，拟合至少需要 %d 个"
                   "（技术故障点已排除 %d 个）"
                   % (len(usable), min_samples, n_excluded)), None

    # 允许替换拟合实现（测试注入"说谎的代理"用；生产走 surrogate.fit）
    fit_fn = policy.get("fit") or surrogate_mod.fit
    try:
        model = fit_fn(records, variables,
                       max_mae_ratio=policy["max_mae_ratio"],
                       max_rmse_ratio=policy["max_rmse_ratio"])
    except Exception as exc:
        # 拟合本身出错：降级为不排序，原因原样带出（不静默、不阻塞）
        return _surrogate_state(
            policy, SURROGATE_DEGRADED,
            model={"n_samples": len(usable), "min_samples": min_samples},
            reason="代理模型拟合失败，已降级为不排序：%s" % (exc,)), None

    if not model.get("available"):
        # 误差不达标 / 数据不可用：自动降级为不排序，原因**原样**带出来
        return _surrogate_state(
            policy, SURROGATE_DEGRADED, model=model,
            reason=model.get("reason") or "代理模型不可用，已降级为不排序"), None

    return _surrogate_state(policy, SURROGATE_ACTIVE, model=model), model


def _prefetch_by_surrogate(conn, session_id, variables, model, policy,
                           budget, evaluate_vector, seed=0):
    """
    用**已可用的**代理模型挑几个点先算（票27 的核心动作）。

    ADR-0002 边界：这里只决定"先算哪些"。每个被挑中的点仍要跑完整 Study +
    强度校核，合格与否只由实际校核结果决定。代理的预测值仅用于排序，
    绝不写进任何 `feasible` 字段。

    返回实际预取的点数。
    """
    want = int(policy.get("prefetch", 0))
    if want <= 0:
        return 0

    points = list_points(conn, session_id)
    remaining = int(budget.get("max_evals", 0)) - len(points)
    if remaining <= 0:
        return 0

    known = set(p["vector_key"] for p in points)
    pool_size = max(want * 6, 12)
    pool = []
    for vector in optimizer.doe_sample(variables, n_samples=pool_size,
                                       seed=seed):
        key = optimizer.vector_key(vector)
        if key in known:
            continue
        known.add(key)
        pool.append({"vector": vector, "key": key})

    suggestion = surrogate_mod.suggest(model, pool)
    if not suggestion.get("available"):
        return 0

    limit = max(0, min(want, remaining))
    chosen = diversify_candidates(suggestion.get("ranking") or [], variables,
                                  limit)

    n_prefetched = 0
    for item in chosen:
        evaluate_vector(item["vector"], SOURCE_SURROGATE)
        n_prefetched += 1
    return n_prefetched





def _surrogate_state_of(session):
    """从会话结果快照里取快筛状态；没有记录 = 未启用。"""
    result = session.get("result") or {}
    state = result.get("surrogate")
    if not isinstance(state, dict) or not state:
        return _surrogate_off_state()
    return state


def surrogate_report(conn, session_id):
    """
    查询会话的代理模型快筛状态（汇总口径，可复算）。

    返回 dict：
      enabled / status   : 是否启用、状态（off/waiting/active/degraded）
      available          : 模型这一轮是否真的参与排序
      reason             : 未启用或降级的原因（**不静默**）
      n_samples / min_samples / n_prefetched
      model_quality / thresholds : 误差与阈值（可用时）
      by_source          : {来源: 点数} —— 区分「代理建议」与「确定性搜索」
    """
    session = get_session(conn, session_id)
    state = dict(_surrogate_state_of(session))
    points = list_points(conn, session_id)

    by_source = {}
    for p in points:
        by_source[p["source"]] = by_source.get(p["source"], 0) + 1
    state["by_source"] = by_source
    state["n_prefetched"] = by_source.get(SOURCE_SURROGATE, 0)
    state["n_points"] = len(points)
    state["text"] = summarize_surrogate(state)
    return state


def summarize_surrogate(state):
    """人类可读一行摘要（日志 / 报告 / UI 共用）。"""
    if not state or not state.get("enabled"):
        return "代理快筛: 未启用（纯确定性搜索）"
    status = state.get("status")
    if status == SURROGATE_ACTIVE:
        q = state.get("model_quality") or {}
        return ("代理快筛: 已启用并参与排序（样本 %d 个，LOO MAE 占目标跨度 %.1f%%，"
                "预取 %d 个点优先评估）——只影响评估顺序，合格判定仍以强度校核为准"
                % (state.get("n_samples", 0), (q.get("mae_ratio") or 0.0) * 100.0,
                   state.get("n_prefetched", 0)))
    if status == SURROGATE_DEGRADED:
        return "代理快筛: 已降级为不排序（%s）" % (state.get("reason") or "原因未说明")
    if status == SURROGATE_WAITING:
        return "代理快筛: 等待样本（%s）" % (state.get("reason") or "样本不足")
    return "代理快筛: 未启用（纯确定性搜索）"



def summarize(conn, session_id, optimizer_result=None):
    """
    汇总会话：各点目标值 / 约束 / 可行性 / 失败原因 / 是否最优 + 三组分组。

    结论**从库里重算**（不是从内存结果拼），所以跑完、重启、隔天再查，
    得到的都是同一份结论。

    optimizer_result : 可选，优化器原始返回（补充 stop_reason 等元信息）
    """
    session = get_session(conn, session_id)
    points = list_points(conn, session_id)
    grouped = {GROUP_FEASIBLE: [], GROUP_INFEASIBLE: [], GROUP_FAILED: []}
    for p in points:
        grouped.setdefault(p["group_name"], []).append(p)

    best = None
    for p in points:
        if p["is_best"]:
            best = p
            break

    failed_reasons = []
    for p in grouped.get(GROUP_FAILED, []):
        err = p.get("error") or {}
        failed_reasons.append({
            "point_id": p["point_id"],
            "seq": p["seq"],
            "vector_key": p["vector_key"],
            "kind": err.get("kind"),
            "message": err.get("message"),
        })

    result = {
        "session_id": session_id,
        "name": session["name"],
        "template_id": session["template_id"],
        "status": session["status"],
        "intent": session["intent"],
        "budget": session["budget"],
        "n_points": len(points),
        "counts": {
            GROUP_FEASIBLE: len(grouped.get(GROUP_FEASIBLE, [])),
            GROUP_INFEASIBLE: len(grouped.get(GROUP_INFEASIBLE, [])),
            GROUP_FAILED: len(grouped.get(GROUP_FAILED, [])),
        },
        "groups": grouped,
        "best": best,
        "points": points,
        "failed_reasons": failed_reasons,
        "recommendation": None,
        "adoption": session.get("adoption"),
        "adopted_point_id": None,
    }
    if optimizer_result:
        for key in ("stop_reason", "stop_text", "n_evals", "n_cached",
                    "n_rounds", "seed"):
            if key in optimizer_result:
                result[key] = optimizer_result[key]

    # 票27：快筛状态 + 来源区分（代理建议 vs 确定性搜索）
    surro_state = dict(_surrogate_state_of(session))
    by_source = {}
    for p in points:
        by_source[p["source"]] = by_source.get(p["source"], 0) + 1
    surro_state["by_source"] = by_source
    surro_state["n_prefetched"] = by_source.get(SOURCE_SURROGATE, 0)
    surro_state["n_points"] = len(points)
    surro_state["text"] = summarize_surrogate(surro_state)
    result["surrogate"] = surro_state

    # 建议采用点：只有"有可行点"时才给建议；没可行点就明确说给不出
    result["recommendation"] = recommendation_from(points, session)
    adopted = _adoption_from(points, session)
    if adopted.get("decision") == approval.DECISION_ADOPT:
        result["adopted_point_id"] = adopted.get("point_id")
    return result


def recommendation_from(points, session):
    """
    建议采用的设计点（**建议**，不是决定）。

    这是 ADR-0002 的边界：L0 优化器可以算出"哪一点最轻且合格"，
    但它只能**建议**。采用与否是 L2 人的决定（见 `record_decision`）。
    """
    best = None
    for p in points:
        if p.get("is_best"):
            best = p
            break
    if best is None:
        return {
            "point_id": None,
            "basis": "no_feasible",
            "text": "未找到满足约束的可行设计：无可建议采用的方案"
                    "（维持原设计是当前唯一稳妥选择）",
            "requires_approval": True,
            "approved": False,
        }
    return {
        "point_id": best["point_id"],
        "study_id": best["study_id"],
        "vector": best["vector"],
        "vector_key": best["vector_key"],
        "objective_value": best["objective_value"],
        "basis": "min_objective_among_feasible",
        "text": "建议采用设计点 #%d（质量 %s kg，可行）"
                % (best["point_id"], best["objective_value"]),
        "requires_approval": True,
        "approved": False,
    }


def recommendation(conn, session_id):
    """会话的建议采用点（纯查询，可复算）。"""
    session = get_session(conn, session_id)
    return recommendation_from(list_points(conn, session_id), session)


# ---------------------------------------------------------------------------
# L2 门：采用与否由人决定
# ---------------------------------------------------------------------------

def record_decision(conn, session_id, decision, role=approval.ROLE_SIGNER,
                    operator=None, rationale=None, point_id=None):
    """
    记录 L2 决策：**采用**某个设计点，或**维持原设计**。

    为什么必须由人显式调用：ADR-0002 规定方案采用属 L2。优化器只能建议，
    平台不能把"最优解"自动当成"采用的方案"—— 那等于让搜索算法替工程师签字。
    于是在这套数据结构里，没有这一步就取不到采用方案（`adopted_point` 为 None，
    `require_adopted_point` 抛错）。

    decision  : approval.DECISION_ADOPT（采用）| approval.DECISION_KEEP_ORIGINAL（维持原设计）
    role      : 决策角色（"采用"必须由签发角色做出，见 approval 模块）
    point_id  : 可选，明确采用哪个点；None 则采用"建议点"（维持原设计时忽略）

    返回采用状态快照（与 `get_adoption` 同形）。
    """
    session = get_session(conn, session_id)
    points = list_points(conn, session_id)
    if not points:
        raise OptimizationLoopError(
            "会话 %r 还没有任何设计点，无法决策（先跑 run_optimization）"
            % (session_id,), code="no-points")

    # L2 决策是**终态**：一旦定案（采用 / 维持原设计）就不再改。
    # 这条守卫必须放在核心层，不能只靠服务层拦 —— 否则任何直接调本模块的路径
    # （脚本、未来 UI、CLI）都能把已签的"采用 A"改成"维持原设计"，
    # 审计里留下两条互相矛盾的记录，而最终结论取决于谁最后写入。
    _adoption = session.get("adoption") or {}
    if _adoption.get("decided"):
        raise OptimizationLoopError(
            "会话 %r 的采用决策已定案（%s），不能重复决策"
            % (session_id,
               approval.decision_name(_adoption.get("decision"))),
            code="already-decided")

    rec = recommendation_from(points, session)
    target = None
    if decision == approval.DECISION_ADOPT:
        pid = point_id if point_id is not None else rec.get("point_id")
        if pid is None:
            raise OptimizationLoopError(
                "没有可采用的可行设计点（未找到满足约束的方案）；"
                "此时只能选择「维持原设计」", code="not-adopted")
        target = None
        for p in points:
            if p["point_id"] == pid:
                target = p
                break
        if target is None:
            raise OptimizationLoopError(
                "设计点 %r 不在会话 %r 里" % (pid, session_id),
                code="invalid-session")
        if not objectives.is_feasible(target["evaluation"]):
            raise OptimizationLoopError(
                "设计点 #%s 不满足约束（不可行或评估失败），不能作为采用方案"
                % (pid,), code="not-adopted")
    elif decision == approval.DECISION_KEEP_ORIGINAL:
        # 维持原设计 = 采用会话起点（设计向量默认值）作为最终答案，也是有内容的决定
        for p in points:
            if p["source"] == SOURCE_BASELINE:
                target = p
                break
        if target is None:
            target = points[0]
    else:
        raise OptimizationLoopError(
            "未知的采用决策 %r（合法值：%s）"
            % (decision, ", ".join(approval.OPTIMIZE_DECISIONS)),
            code="decision-invalid")

    audit_study = target["study_id"] or _audit_study_fallback(conn, session_id)
    try:
        approval.record_optimize_decision(
            conn, audit_study, decision, role=role, operator=operator,
            rationale=rationale, session_id=session_id,
            point_id=target["point_id"])
    except approval.ApprovalError as exc:
        raise OptimizationLoopError("采用决策被拒绝: %s" % exc.reason,
                                    code="decision-invalid")

    state = {
        "session_id": session_id,
        "decision": decision,
        "decided": True,
        "point_id": target["point_id"],
        "study_id": target["study_id"],
        "actor": approval._actor_text(role, operator),
        "rationale": rationale,
        "at": _now(),
        "recommended_point_id": rec.get("point_id"),
        "accepted_recommendation": (target["point_id"] == rec.get("point_id")),
        "audit_study_id": audit_study,
    }
    _set_session_status(conn, session_id, session["status"], adoption=state)
    return state


def _audit_study_fallback(conn, session_id):
    """决策要落在某个 Study 上（decisions.study_id 有外键）；找不到就报错不硬塞。"""
    raise OptimizationLoopError(
        "会话 %r 的设计点都没有关联 Study，无法写 L2 审计" % (session_id,),
        code="not-adopted")


def _adoption_from(points, session):
    """从会话快照读出采用状态（并补齐"未经批准"的形态）。"""
    adoption = session.get("adoption")
    if not adoption or not adoption.get("decided"):
        return {
            "decided": False,
            "decision": None,
            "point_id": None,
            "text": "优化方案尚未经人批准：不能作为采用方案",
            "recommended_point_id": (
                recommendation_from(points, session).get("point_id")),
        }
    return adoption


def get_adoption(conn, session_id):
    """会话的采用状态（未批准时 decided=False，point_id=None）。"""
    session = get_session(conn, session_id)
    return _adoption_from(list_points(conn, session_id), session)


def adopted_point(conn, session_id):
    """
    **已批准**采用的方案；未经批准返回 None。

    这是"未经人批准不可被当作采用方案"在 API 上的落点：调用方拿不到
    未批准的点，就没法把它当答案。
    """
    state = get_adoption(conn, session_id)
    if not state.get("decided") or state.get("decision") != approval.DECISION_ADOPT:
        return None
    for p in list_points(conn, session_id):
        if p["point_id"] == state.get("point_id"):
            return p
    return None


def require_adopted_point(conn, session_id):
    """
    取已批准的采用方案；**未批准直接抛错**。

    给"要把优化结论写进报告/下发生产"的调用方用：必须过这一关，
    否则就是让优化器的建议冒充人的决定。
    """
    state = get_adoption(conn, session_id)
    if not state.get("decided"):
        raise OptimizationLoopError(
            "优化方案未经人批准，不能作为采用方案（请先由有权限的角色调用 "
            "record_decision 记录 adopt 或 keep_original）", code="not-adopted")
    if state.get("decision") == approval.DECISION_KEEP_ORIGINAL:
        raise OptimizationLoopError(
            "本会话的决策是「维持原设计」，没有采用新的优化方案",
            code="not-adopted")
    point = adopted_point(conn, session_id)
    if point is None:
        raise OptimizationLoopError(
            "采用决策指向的设计点 %r 已不可查（会话数据不完整）"
            % (state.get("point_id"),), code="not-adopted")
    return point


# ---------------------------------------------------------------------------
# 血缘：从会话一跳查到每个设计点的全部产物
# ---------------------------------------------------------------------------

def artifact_chain(conn, study_id):
    """一个 Study 的完整产物链（按插入顺序）。"""
    if not study_id:
        return []
    rows = conn.execute(
        "SELECT id, kind, version, checksum, content_uri, created_at "
        "FROM artifacts WHERE study_id=? ORDER BY id", (study_id,)).fetchall()
    return [{"id": r["id"], "kind": r["kind"], "version": r["version"],
             "checksum": r["checksum"], "content_uri": r["content_uri"],
             "created_at": r["created_at"]} for r in rows]


def lineage(conn, session_id):
    """
    会话 -> 每个设计点 -> 其 Study 的产物链（**一跳回溯**）。

    返回的每个点都带 `study_id` 与 `artifacts`（kind -> id），于是从会话 id
    出发，一次查询就能到"这一版方案的 .inp / odb / 校核结果 / 报告"。
    """
    session = get_session(conn, session_id)
    out = []
    for p in list_points(conn, session_id):
        out.append({
            "point_id": p["point_id"],
            "seq": p["seq"],
            "study_id": p["study_id"],
            "vector_key": p["vector_key"],
            "group_name": p["group_name"],
            "is_best": p["is_best"],
            "objective_value": p["objective_value"],
            "reused_from": p["reused_from"],
            "artifacts": p["artifacts"],
            "chain": artifact_chain(conn, p["study_id"]),
        })
    return {
        "session": {
            "session_id": session["session_id"],
            "name": session["name"],
            "template_id": session["template_id"],
            "status": session["status"],
            "intent": session["intent"],
            "budget": session["budget"],
            "design_vector": session["design_vector"],
            "created_at": session["created_at"],
        },
        "n_points": len(out),
        "points": out,
    }


# ---------------------------------------------------------------------------
# 文本摘要（日志 / CLI / 报告共用）
# ---------------------------------------------------------------------------

def summarize_text(summary):
    """人类可读的多行摘要（面向日志与报告附录）。"""
    if not summary:
        return "优化: 无结果"
    counts = summary.get("counts") or {}
    lines = [
        "优化会话 %s（%s）" % (summary.get("session_id"),
                              summary.get("status")),
        "设计点 %d：可行 %d / 不可行 %d / 评估失败 %d"
        % (summary.get("n_points", 0), counts.get(GROUP_FEASIBLE, 0),
           counts.get(GROUP_INFEASIBLE, 0), counts.get(GROUP_FAILED, 0)),
    ]
    if summary.get("stop_text"):
        lines.append("停止原因：%s" % summary["stop_text"])
    best = summary.get("best")
    if best:
        lines.append("最优可行点：设计点 #%d，质量 %s kg"
                     % (best["point_id"], textfmt.fmt_num(best["objective_value"])))
    else:
        lines.append("最优可行点：无（未找到满足约束的设计）")
    rec = summary.get("recommendation") or {}
    lines.append("建议采用：%s" % (rec.get("text") or "-"))
    # 票27：快筛在不在起作用，是总结论的一部分（不可静默省略）
    surro = summary.get("surrogate") or {}
    lines.append(surro.get("text") or summarize_surrogate(surro))
    by_source = surro.get("by_source") or {}
    if by_source:
        lines.append("点来源：%s" % "，".join(
            "%s %d" % (k, v) for k, v in sorted(by_source.items())))
    adoption = summary.get("adoption") or summary.get("adoption_text")
    if isinstance(adoption, dict):
        if adoption.get("decided"):
            lines.append("采用决策：%s（%s，%s）"
                         % (approval.decision_name(adoption.get("decision")),
                            adoption.get("actor"), adoption.get("at")))
        else:
            lines.append("采用决策：尚未批准（优化方案不可当作已采用）")
    else:
        lines.append("采用决策：尚未批准（优化方案不可当作已采用）")
    return "\n".join(lines)
