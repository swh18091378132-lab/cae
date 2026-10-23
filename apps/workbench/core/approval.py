# -*- coding: utf-8 -*-
"""
approval.py -- 强度校核报告的多角色审批流（票18）

背景（issue #18）：
  强度校核报告进入**多角色审批流程**。角色：设计 / 强度分析 / 审核 / 签发。
  报告状态机：分析中 → 待审核 → 通过 / 退回 → 已签发。

  **关键合规点**：重跑产生新版本报告时，审批状态必须重置为「待审核」——
  旧审批（尤其是「已签发」）绝不能继承到新内容上。所有状态变更记入
  L2 决策审计（decisions 表：谁、何时、什么决定、理由）。

本模块是**应用逻辑层**（纯标准库，不依赖 UI / CATIA / Abaqus），可独立单测。
数据落在 db.py 已建好的表上，本模块**不新建表、不改 schema**：

  * ``report_approvals``（study_id, artifact_id, version, status, updated_at）
    —— 一个 Study 的**每一版报告**各占一行，各自独立审批。
  * ``artifacts.approval_status`` —— 报告产物的当前审批状态缓存（便于 UI/查询）。
  * ``decisions``（P1 已有）—— L2 审计日志；审批记录的理由统一以
    ``报告 v<N>：...`` 开头，于是无需新列也能按版本检索审计链。

设计原则：
  1. **合法/非法转换明确区分**：非法转换抛 ``ApprovalError``，``.reason`` 是
     可读中文原因（例："未通过审核的报告不允许签发"），``.code`` 供程序判断。
  2. 审核通过/退回**必须能留意见**；退回**必须**写理由（否则无法指导设计修改），
     所有意见都进审计且可按版本查询。
  3. 签发 = 终态（最终版），此后任何审批动作都被拒绝。
  4. 每次状态变更只经由 ``_set_status`` 一个出口，状态表、artifact 缓存与
     审计日志同步更新，不可能出现"改了状态但没留痕"。

兼容 Python 3.8（Win7 目标机）。
"""

import datetime

try:
    from . import study as study_mod
except ImportError:  # 允许作为脚本/在 core 目录内直接运行
    import study as study_mod


# ---------------------------------------------------------------------------
# 角色（GLOSSARY 口径：设计 / 强度分析 / 审核 / 签发）
# ---------------------------------------------------------------------------

ROLE_DESIGN = "design"        # 设计：提出/修改设计；报告退回后由其处理
ROLE_ANALYSIS = "analysis"    # 强度分析：跑分析、出报告、提交审核
ROLE_REVIEWER = "reviewer"    # 审核：通过或退回，并填写意见
ROLE_SIGNER = "signer"        # 签发：把已通过审核的报告签发为最终版

ROLES = (ROLE_DESIGN, ROLE_ANALYSIS, ROLE_REVIEWER, ROLE_SIGNER)

ROLE_NAMES = {
    ROLE_DESIGN: "设计",
    ROLE_ANALYSIS: "强度分析",
    ROLE_REVIEWER: "审核",
    ROLE_SIGNER: "签发",
}


# ---------------------------------------------------------------------------
# 报告状态机
# ---------------------------------------------------------------------------

STATUS_ANALYZING = "analyzing"
STATUS_PENDING_REVIEW = "pending_review"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_SIGNED = "signed"

STATUSES = (STATUS_ANALYZING, STATUS_PENDING_REVIEW, STATUS_APPROVED,
            STATUS_REJECTED, STATUS_SIGNED)

STATUS_NAMES = {
    STATUS_ANALYZING: "分析中",
    STATUS_PENDING_REVIEW: "待审核",
    STATUS_APPROVED: "已通过审核",
    STATUS_REJECTED: "已退回",
    STATUS_SIGNED: "已签发（最终版）",
}

# 合法转换表：状态 -> 允许到达的状态集合
#   analyzing      -> pending_review           （报告产出 / 主动提交审核）
#   pending_review -> approved / rejected      （审核人的两个决定）
#   approved       -> signed                   （只有通过审核才可签发）
#   rejected       -> pending_review           （设计/分析修改后重新提交）
#   signed         -> （终态，无出边：最终版不可再改）
TRANSITIONS = {
    STATUS_ANALYZING: (STATUS_PENDING_REVIEW,),
    STATUS_PENDING_REVIEW: (STATUS_APPROVED, STATUS_REJECTED),
    STATUS_APPROVED: (STATUS_SIGNED,),
    STATUS_REJECTED: (STATUS_PENDING_REVIEW,),
    STATUS_SIGNED: (),
}

# 审计 gate 名（decisions.gate）
GATE_SUBMIT = "approval_submit"    # 首次登记 / 提交审核
GATE_RERUN = "approval_rerun"      # 重跑产生新版本 -> 状态重置
GATE_REVIEW = "approval_review"    # 审核（通过 / 退回）
GATE_SIGN = "approval_sign"        # 签发

AUDIT_GATES = (GATE_SUBMIT, GATE_RERUN, GATE_REVIEW, GATE_SIGN)

# 动作 -> (需要哪个角色, 中文动作名)
_ACTION_RULES = {
    GATE_REVIEW: (ROLE_REVIEWER, "审核"),
    GATE_SIGN: (ROLE_SIGNER, "签发"),
    GATE_SUBMIT: (None, "提交审核"),
    GATE_RERUN: (None, "重跑登记"),
}

# 各角色"待处理"清单对应的状态
ROLE_QUEUES = {
    ROLE_REVIEWER: (STATUS_PENDING_REVIEW,),   # 审核人的待审清单
    ROLE_SIGNER: (STATUS_APPROVED,),           # 签发人的待签清单
    ROLE_DESIGN: (STATUS_REJECTED,),           # 被退回、需要设计处理
    ROLE_ANALYSIS: (STATUS_REJECTED,),         # 被退回、需要分析处理
}

# 审批审计记录的理由前缀（便于按版本检索，见 history()）
AUDIT_PREFIX = "报告 v%d："


class ApprovalError(Exception):
    """
    审批流错误。除消息文本外还带机器可读的 ``code`` 与可读的 ``reason``。

    code 取值：
      illegal-transition   非法状态转换
      invalid-status       未知状态
      invalid-role         角色不合法 / 无权执行该动作
      missing-opinion      退回未填写意见
      not-registered       该报告版本尚未登记审批
      already-registered   同一版本重复登记
      final-immutable      最终版不可修改
    """

    def __init__(self, reason, code="illegal-transition"):
        Exception.__init__(self, reason)
        self.reason = reason
        self.code = code

    def __str__(self):
        return self.reason


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def status_name(status):
    return STATUS_NAMES.get(status, status)


def role_name(role):
    return ROLE_NAMES.get(role, role)


def is_final(status):
    """已签发 = 最终版，不可再改。"""
    return status == STATUS_SIGNED


def _check_status(status):
    if status not in STATUSES:
        raise ApprovalError("未知的审批状态 %r（合法值：%s）"
                            % (status, ", ".join(STATUSES)),
                            code="invalid-status")


def _check_role(role, gate):
    """角色合法性 + 该角色是否有权执行这个动作。"""
    if role not in ROLES:
        raise ApprovalError("未知角色 %r（合法值：%s）"
                            % (role, ", ".join(ROLES)), code="invalid-role")
    required, action = _ACTION_RULES.get(gate, (None, gate))
    if required is not None and role != required:
        raise ApprovalError("角色「%s」无权执行「%s」动作（需要「%s」角色）"
                            % (role_name(role), action, role_name(required)),
                            code="invalid-role")


def _actor_text(role, operator=None):
    """审计里的 actor 字段：``审核:张三``（无具体人时就是角色名）。"""
    if operator:
        return "%s:%s" % (role_name(role), operator)
    return role_name(role)


def _illegal_reason(current, target):
    """生成**可读**的非法转换原因（验收项：非法转换被拒绝并给出原因）。"""
    if current == STATUS_SIGNED:
        return ("报告已签发为最终版，不可再变更审批状态（%s → %s 被拒绝）"
                % (status_name(current), status_name(target)))
    if target == STATUS_SIGNED and current != STATUS_APPROVED:
        return ("未通过审核的报告不允许签发：当前状态「%s」，请先经审核通过"
                % status_name(current))
    if target == STATUS_APPROVED and current != STATUS_PENDING_REVIEW:
        return ("只有「待审核」状态的报告可以通过审核：当前状态「%s」"
                % status_name(current))
    if target == STATUS_REJECTED and current != STATUS_PENDING_REVIEW:
        return ("只有「待审核」状态的报告可以退回：当前状态「%s」"
                % status_name(current))
    if target == STATUS_PENDING_REVIEW and current not in (
            STATUS_ANALYZING, STATUS_REJECTED):
        return ("报告当前状态「%s」不能重新提交审核"
                % status_name(current))
    return ("非法审批状态转换：%s → %s（合法后继状态：%s）"
            % (status_name(current), status_name(target),
               ", ".join(status_name(s) for s in TRANSITIONS.get(current, ()))
               or "无（终态）"))


def can_transition(current, target):
    """
    判断转换是否合法，返回 ``(ok, reason)``；非法时 reason 是可读原因。

    这是状态机的**唯一判据**，所有动作函数都经它做校验。
    """
    try:
        _check_status(current)
        _check_status(target)
    except ApprovalError as exc:
        return False, exc.reason
    if current == target:
        return False, ("报告已处于「%s」状态，无需重复操作"
                       % status_name(current))
    if target in TRANSITIONS.get(current, ()):
        return True, ""
    return False, _illegal_reason(current, target)


def _require_transition(current, target):
    ok, reason = can_transition(current, target)
    if not ok:
        raise ApprovalError(reason)
    return target


# ---------------------------------------------------------------------------
# 数据库访问（report_approvals + artifacts.approval_status + decisions）
# ---------------------------------------------------------------------------

_COLUMNS = "id, study_id, artifact_id, version, status, updated_at"


def _fetch(conn, study_id, version):
    if version is None:
        return conn.execute(
            "SELECT %s FROM report_approvals WHERE study_id=? "
            "ORDER BY version DESC LIMIT 1" % _COLUMNS,
            (study_id,)).fetchone()
    return conn.execute(
        "SELECT %s FROM report_approvals WHERE study_id=? AND version=?"
        % _COLUMNS, (study_id, int(version))).fetchone()


def _require_row(conn, study_id, version):
    row = _fetch(conn, study_id, version)
    if row is None:
        if version is None:
            raise ApprovalError("Study %s 尚无登记审批的报告版本" % study_id,
                                code="not-registered")
        raise ApprovalError("Study %s 的报告版本 v%s 尚未登记审批"
                            % (study_id, version), code="not-registered")
    return row


def _audit(conn, study_id, version, gate, role, decision, note,
           operator=None):
    """写一条 L2 审计（复用 P1 的 decisions 表）：角色、动作、理由、时间。"""
    rationale = AUDIT_PREFIX % int(version) + note
    study_mod.add_decision(study_id, gate, _actor_text(role, operator),
                           decision, rationale, conn=conn)
    return rationale


def _sync_cache(conn, artifact_id, status):
    """同步 artifacts.approval_status 缓存（表里没有该 artifact 时静默跳过）。"""
    if not artifact_id:
        return
    conn.execute("UPDATE artifacts SET approval_status=? WHERE id=?",
                 (status, artifact_id))


def _strip_prefix(text):
    """从审计理由里剥掉 ``报告 v<N>：`` 前缀，返回可读意见正文。"""
    if not text:
        return text
    idx = text.find("：")
    if text.startswith("报告 v") and idx > 0:
        return text[idx + 1:]
    return text


def _state_of(conn, row_id):
    row = conn.execute(
        "SELECT %s FROM report_approvals WHERE id=?" % _COLUMNS,
        (row_id,)).fetchone()
    return _state_dict(conn, row)


def _last_decision(conn, study_id, version):
    """该版本最近一次审批动作（审计表里带版本前缀的记录）。"""
    rows = conn.execute(
        "SELECT * FROM decisions WHERE study_id=? AND gate LIKE 'approval%%' "
        "ORDER BY id DESC", (study_id,)).fetchall()
    prefix = AUDIT_PREFIX % int(version)
    for r in rows:
        if (r["rationale"] or "").startswith(prefix):
            return r
    return None


def _state_dict(conn, row):
    """报告审批状态的对外快照（dict，便于 UI/CLI 直接消费）。"""
    if row is None:
        return None
    status = row["status"]
    version = int(row["version"])
    last = _last_decision(conn, row["study_id"], version)
    return {
        "study_id": row["study_id"],
        "artifact_id": row["artifact_id"],
        "version": version,
        "status": status,
        "status_name": status_name(status),
        "updated_at": row["updated_at"],
        "final": is_final(status),
        # 当前状态还能做什么（UI 可据此置灰按钮，无需复制状态机）
        "can_review": status == STATUS_PENDING_REVIEW,
        "can_sign": status == STATUS_APPROVED,
        "can_resubmit": status == STATUS_REJECTED,
        "editable": not is_final(status),
        # 最近一次审批动作（退回意见可在此查询）
        "last_reason": _strip_prefix(last["rationale"]) if last is not None else None,
        "last_actor": last["actor"] if last is not None else None,
        "last_gate": last["gate"] if last is not None else None,
        "last_at": last["created_at"] if last is not None else None,
    }


def _set_status(conn, row, target, gate, role, note, operator=None):
    """
    **所有状态变更的唯一出口**：先校验角色权限与状态转换，再改状态表 +
    同步 artifact 缓存 + 写 L2 审计 + 提交。

    顺序上先查角色（谁在做、有没有权限），再查状态机（现在能不能做）——
    两者同时不满足时报"无权"，这是更根本的错误。
    """
    current = row["status"]
    _check_role(role, gate)
    _require_transition(current, target)

    version = int(row["version"])
    now = _now()
    conn.execute(
        "UPDATE report_approvals SET status=?, updated_at=? WHERE id=?",
        (target, now, row["id"]))
    _sync_cache(conn, row["artifact_id"], target)
    conn.commit()

    _audit(conn, row["study_id"], version, gate, role, target, note,
           operator=operator)
    return _state_of(conn, row["id"])


# ---------------------------------------------------------------------------
# 登记 / 重跑重置（硬验收项：新版本绝不继承旧审批）
# ---------------------------------------------------------------------------

def register_report(conn, study_id, artifact_id, version,
                    status=STATUS_PENDING_REVIEW,
                    role=ROLE_ANALYSIS, operator=None, rationale=None):
    """
    登记一版报告进入审批流；返回该版本的审批状态快照。

    这是「报告产出 / 重跑产生新版本」的唯一入口：无论上一版是 approved 还是
    signed，新版本一律从给定 status（默认 pending_review）重新开始，
    旧审批**不继承**。

    同一 (study_id, version) 重复登记 -> ``ApprovalError(code="already-registered")``
    （同一版本 = 同一份内容，重登记不能把已有审批抹掉；要重跑请产生新版本）。
    """
    _check_status(status)
    version = int(version)

    existing = _fetch(conn, study_id, version)
    if existing is not None:
        raise ApprovalError(
            "Study %s 的报告版本 v%d 已登记审批（当前状态：%s），不能重复登记"
            % (study_id, version, status_name(existing["status"])),
            code="already-registered")

    prev = _fetch(conn, study_id, None)   # 本版之前的最新一版
    if prev is not None and int(prev["version"]) >= version:
        prev = conn.execute(
            "SELECT %s FROM report_approvals WHERE study_id=? AND version<? "
            "ORDER BY version DESC LIMIT 1" % _COLUMNS,
            (study_id, version)).fetchone()

    if prev is None:
        gate = GATE_SUBMIT
        note = rationale or "报告产出，登记审批"
    else:
        gate = GATE_RERUN
        note = rationale or (
            "重跑产生新报告版本 v%d：审批状态重置为「%s」，不继承上一版 v%d 的「%s」"
            % (version, status_name(status), int(prev["version"]),
               status_name(prev["status"])))

    now = _now()
    cur = conn.execute(
        "INSERT INTO report_approvals(study_id, artifact_id, version, status, "
        "updated_at) VALUES (?,?,?,?,?)",
        (study_id, artifact_id, version, status, now))
    row_id = int(cur.lastrowid)
    _sync_cache(conn, artifact_id, status)
    conn.commit()

    _audit(conn, study_id, version, gate, role, status, note, operator=operator)
    return _state_of(conn, row_id)


def reset_for_new_version(conn, study_id, artifact_id, version,
                          role=ROLE_ANALYSIS, operator=None, rationale=None):
    """
    重跑产生新报告版本 -> 重置为「待审核」（``register_report`` 的语义化别名）。

    显式暴露这个名字，是因为这是票18 的**关键合规点**：旧审批绝不继承到新内容上。
    """
    return register_report(conn, study_id, artifact_id, version,
                           status=STATUS_PENDING_REVIEW, role=role,
                           operator=operator, rationale=rationale)


def ensure_registered(conn, study_id, artifact_id, version, role=ROLE_ANALYSIS,
                      status=STATUS_PENDING_REVIEW):
    """
    幂等登记：已有该版本则原样返回，否则按 `status` 登记（默认「待审核」）。

    给服务层用——同一版本重复调用不会把已有审批状态冲掉（同一版本 = 同一内容），
    而**新版本**（重跑）一定走 ``register_report`` 的 pending_review 重置路径。

    `status=STATUS_ANALYZING` 用于"分析已开始、报告尚未产出"的登记：
    这样 spec 用户故事 26（"看到每个 Study 当前处于流程的哪个环节"）里的
    「分析中」是**真的会出现**的状态，而不是一个永远到不了的状态。
    """
    row = _fetch(conn, study_id, version)
    if row is not None:
        return _state_dict(conn, row)
    return register_report(conn, study_id, artifact_id, version, role=role,
                           status=status)


# ---------------------------------------------------------------------------
# 状态查询
# ---------------------------------------------------------------------------

def get_state(conn, study_id, version=None):
    """取某版报告（默认最新版）的审批状态快照；未登记返回 None。"""
    return _state_dict(conn, _fetch(conn, study_id, version))


def get_status(conn, study_id, version=None):
    """取状态字符串（快捷方式）；未登记返回 None。"""
    row = _fetch(conn, study_id, version)
    return row["status"] if row is not None else None


def require_state(conn, study_id, version=None):
    """取状态快照，未登记则抛 ApprovalError。"""
    return _state_dict(conn, _require_row(conn, study_id, version))


def list_versions(conn, study_id):
    """该 Study 所有已登记的报告版本（按版本号升序）。"""
    rows = conn.execute(
        "SELECT %s FROM report_approvals WHERE study_id=? ORDER BY version"
        % _COLUMNS, (study_id,)).fetchall()
    return [_state_dict(conn, r) for r in rows]


def history(conn, study_id, version=None):
    """
    审批审计历史（decisions 表中 gate 以 ``approval`` 开头的记录，按时间升序）。

    每条含：gate（动作类别）、actor（角色[:经办人]）、decision（状态）、
    rationale（理由，已带 ``报告 v<N>：`` 前缀以便按版本检索）、created_at（时间）。
    version 给定时只返回该版本的记录。
    """
    rows = conn.execute(
        "SELECT * FROM decisions WHERE study_id=? AND gate LIKE 'approval%' "
        "ORDER BY id", (study_id,)).fetchall()
    out = []
    for r in rows:
        out.append({"gate": r["gate"], "actor": r["actor"],
                    "decision": r["decision"], "rationale": r["rationale"],
                    "created_at": r["created_at"]})
    if version is None:
        return out
    prefix = AUDIT_PREFIX % int(version)
    return [h for h in out if (h["rationale"] or "").startswith(prefix)]


def pending_for_role(conn, role):
    """
    按角色查询待处理清单。

    审核人 -> 待审核（pending_review）；签发人 -> 待签发（approved）；
    设计 / 强度分析 -> 被退回（rejected）。返回状态快照列表（按更新时间升序）。
    """
    if role not in ROLE_QUEUES:
        raise ApprovalError("未知角色 %r（合法值：%s）"
                            % (role, ", ".join(ROLES)), code="invalid-role")
    wanted = ROLE_QUEUES[role]
    marks = ",".join("?" * len(wanted))
    rows = conn.execute(
        "SELECT %s FROM report_approvals WHERE status IN (%s) "
        "ORDER BY updated_at, study_id, version" % (_COLUMNS, marks),
        tuple(wanted)).fetchall()
    return [_state_dict(conn, r) for r in rows]


def pending_reviews(conn):
    """审核人的待审清单。"""
    return pending_for_role(conn, ROLE_REVIEWER)


def pending_signs(conn):
    """签发人的待签清单。"""
    return pending_for_role(conn, ROLE_SIGNER)


# ---------------------------------------------------------------------------
# 动作：分析中 -> 待审核 -> 通过 / 退回 -> 签发
# ---------------------------------------------------------------------------

def begin_analysis(conn, study_id, artifact_id, version,
                   role=ROLE_ANALYSIS, operator=None, rationale=None):
    """登记一版报告为「分析中」（状态机起点）。"""
    return register_report(conn, study_id, artifact_id, version,
                           status=STATUS_ANALYZING, role=role,
                           operator=operator, rationale=rationale)


def submit_for_review(conn, study_id, version=None,
                      role=ROLE_ANALYSIS, operator=None, rationale=None):
    """分析完成 -> 提交审核（analyzing / rejected -> pending_review）。"""
    row = _require_row(conn, study_id, version)
    return _set_status(conn, row, STATUS_PENDING_REVIEW, GATE_SUBMIT, role,
                       rationale or "分析完成，提交审核", operator=operator)


def approve(conn, study_id, version=None, role=ROLE_REVIEWER,
            opinion=None, operator=None):
    """
    审核通过（pending_review -> approved）。意见可选，但会进审计。

    只有「审核」角色能执行（``role`` 传错 -> ApprovalError code=invalid-role）。
    """
    row = _require_row(conn, study_id, version)
    return _set_status(conn, row, STATUS_APPROVED, GATE_REVIEW, role,
                       opinion or "审核通过", operator=operator)


def reject(conn, study_id, version=None, role=ROLE_REVIEWER,
           opinion=None, operator=None):
    """
    审核退回（pending_review -> rejected）。**必须填写退回意见**。

    退回意见写入 L2 审计，并可通过 ``get_state(...)["last_reason"]`` 或
    ``history(conn, study_id, version)`` 查询。
    """
    text = (opinion or "").strip()
    if not text:
        raise ApprovalError("退回报告必须填写退回意见（说明问题与修改要求），"
                            "否则设计/分析无法据此修改", code="missing-opinion")
    row = _require_row(conn, study_id, version)
    return _set_status(conn, row, STATUS_REJECTED, GATE_REVIEW, role,
                       "审核退回：" + text, operator=operator)


def sign(conn, study_id, version=None, role=ROLE_SIGNER,
         opinion=None, operator=None):
    """
    签发（approved -> signed）。签发后为最终版，不可再改。

    **未通过审核的报告不允许签发**（票18 硬性验收项），非法时抛 ApprovalError。
    """
    row = _require_row(conn, study_id, version)
    return _set_status(conn, row, STATUS_SIGNED, GATE_SIGN, role,
                       opinion or "签发：报告为最终版", operator=operator)


# ---------------------------------------------------------------------------
# 终态守卫
# ---------------------------------------------------------------------------

def ensure_editable(conn, study_id, version=None):
    """
    校验某版报告是否仍可修改（未签发）；已签发则抛 ``ApprovalError``。

    任何"要改这份已签发报告"的调用方都应先过这一关——最终版内容与审批
    都不允许再改。
    """
    row = _require_row(conn, study_id, version)
    if is_final(row["status"]):
        raise ApprovalError(
            "报告 v%d 已签发为最终版，不可再修改（如需变更请产生新版本报告）"
            % int(row["version"]), code="final-immutable")
    return True


def is_signed(conn, study_id, version=None):
    """该版本是否已签发（最终版）。未登记时返回 False。"""
    row = _fetch(conn, study_id, version)
    return bool(row is not None and is_final(row["status"]))
