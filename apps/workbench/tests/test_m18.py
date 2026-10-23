# -*- coding: utf-8 -*-
"""
test_m18.py -- 票18 验收：多角色审批流（报告状态机 + L2 审计）

验收点（对应 issue #18）：
  1. 报告具备审批状态；状态机合法/非法转换被明确区分（非法转换被拒绝并给出原因）
  2. 审核可通过或退回，必须可填写意见；退回意见可被查询
  3. **未通过审核的报告不允许签发**
  4. 签发后报告标记为最终版，不可再改
  5. **重跑产生新报告版本时审批状态重置为待审核**（本文件有硬断言）
  6. 所有状态变更记入 L2 决策审计（角色、动作、理由、时间）
  7. 可按角色查询待处理列表（审核人的待审清单 / 签发人的待签清单）
  8. 服务层可独立单测，不依赖 UI
  9. Python 3.8 兼容

策略：**测服务层，不测 Tk 控件**（与 test_m8 同思路）。审批状态机在
core/approval.py 是纯 Python，可直接单测；workbench_service 只做薄封装。
「重跑重置」这一条必须走真实链路（run_chain 再跑一次）来验证，不能只测函数。

运行 (在 apps/workbench 下): python tests/test_m18.py
"""

import os
import sys
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
REPO = os.path.dirname(os.path.dirname(WORKBENCH))
sys.path.insert(0, CORE)
sys.path.insert(0, WORKBENCH)

import db
import study
import compat_py38
import approval
import workbench_service as ws


PARAMS = {"span": 5.0, "sweep_deg": 10.0, "c_root": 1.0, "c_tip": 0.6,
          "n_ribs": 3, "n_stringers": 4}

RULES = {"criteria": ["tsai_wu", "hashin"], "safety_factor": 1.0}


def _fresh(name):
    d = os.path.join(WORKBENCH, ".tmp_test", "m18", name)
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d)
    return d


def _expect_error(fails, fn, code=None, label=""):
    """
    断言 fn() 抛 ApprovalError（且 code 匹配）；返回异常供进一步断言原因文本。
    """
    try:
        fn()
    except approval.ApprovalError as exc:
        if code is not None and exc.code != code:
            fails.append("%s: 期望错误码 %r，实际 %r（%s）"
                         % (label, code, exc.code, exc.reason))
        if not exc.reason:
            fails.append("%s: 非法转换没有给出可读原因" % label)
        return exc
    fails.append("%s: 期望抛 ApprovalError，实际没有抛" % label)
    return None


# ---------------------------------------------------------------------------
# 1) 状态机：合法 / 非法转换
# ---------------------------------------------------------------------------

def test_state_machine(fails, conn, sid, art_id):
    """合法转换逐级成功；非法转换被拒且给出可读原因。"""
    # 状态机定义本身
    if approval.can_transition(approval.STATUS_ANALYZING,
                               approval.STATUS_PENDING_REVIEW)[0] is not True:
        fails.append("analyzing -> pending_review 应为合法转换")
    if approval.can_transition(approval.STATUS_ANALYZING,
                               approval.STATUS_SIGNED)[0] is not False:
        fails.append("analyzing -> signed 应为非法转换")

    # 起点：分析中
    st = approval.begin_analysis(conn, sid, art_id, 1,
                                 operator="李工")
    if st["status"] != approval.STATUS_ANALYZING:
        fails.append("begin_analysis 后状态应为 analyzing，实际 %r" % st["status"])

    # 分析中不能直接签发（未经过任何审核）
    exc = _expect_error(fails, lambda: approval.sign(conn, sid, 1),
                        code="illegal-transition",
                        label="analyzing -> signed")
    if exc is not None and "未通过审核" not in exc.reason:
        fails.append("analyzing->signed 的原因应点明'未通过审核'，实际: %s"
                     % exc.reason)

    # analyzing -> pending_review（合法）
    st = approval.submit_for_review(conn, sid, 1, operator="李工")
    if st["status"] != approval.STATUS_PENDING_REVIEW:
        fails.append("submit_for_review 后应为待审核，实际 %r" % st["status"])

    # 待审核 -> 通过（合法）
    st = approval.approve(conn, sid, 1, opinion="复核通过", operator="王审核")
    if st["status"] != approval.STATUS_APPROVED:
        fails.append("approve 后应为 approved，实际 %r" % st["status"])

    # 已通过审核不能再退回（旧状态已过）
    _expect_error(fails, lambda: approval.reject(conn, sid, 1, opinion="想反悔"),
                  code="illegal-transition", label="approved -> rejected")

    # 未知状态 / 未知角色
    ok, reason = approval.can_transition("bogus", "signed")
    if ok or "未知" not in reason:
        fails.append("未知状态应返回非法 + 可读原因，实际 ok=%r reason=%r"
                     % (ok, reason))

    st = approval.sign(conn, sid, 1, opinion="同意签发", operator="赵签发")
    if st["status"] != approval.STATUS_SIGNED:
        fails.append("sign 后应为 signed，实际 %r" % st["status"])
    if not st["final"] or st["editable"]:
        fails.append("签发后应标记为最终版且不可编辑")


# ---------------------------------------------------------------------------
# 2) 终态不可再改（signed 是终态）
# ---------------------------------------------------------------------------

def test_final_immutable(fails, conn, sid):
    """签发后：再签发 / 再审核 / 再退回 / 再提交 全部被拒。"""
    for label, fn in (
            ("再签发", lambda: approval.sign(conn, sid, 1)),
            ("再审核通过", lambda: approval.approve(conn, sid, 1, opinion="x")),
            ("再退回", lambda: approval.reject(conn, sid, 1, opinion="x")),
            ("再提交审核", lambda: approval.submit_for_review(conn, sid, 1))):
        exc = _expect_error(fails, fn, code="illegal-transition",
                            label="signed 终态-%s" % label)
        if exc is not None and "最终版" not in exc.reason:
            fails.append("signed 终态-%s 的原因应点明'最终版'，实际: %s"
                         % (label, exc.reason))

    # 状态确实没被改动
    st = approval.require_state(conn, sid, 1)
    if st["status"] != approval.STATUS_SIGNED:
        fails.append("终态被非法动作篡改：%r" % st["status"])

    # 终态守卫
    exc = _expect_error(fails, lambda: approval.ensure_editable(conn, sid, 1),
                        code="final-immutable", label="ensure_editable")
    if exc is not None and "不可再修改" not in exc.reason:
        fails.append("ensure_editable 原因应说明不可再修改，实际: %s" % exc.reason)
    if approval.is_signed(conn, sid, 1) is not True:
        fails.append("is_signed 应为 True")


# ---------------------------------------------------------------------------
# 3) 退回 + 意见可查询
# ---------------------------------------------------------------------------

def test_reject_with_opinion(fails, conn, sid):
    """审核退回必须填意见；意见可被查询；退回后可重新提交审核。"""
    art2 = study.add_artifact(sid, "report", content_uri="report:v2",
                              params={"tag": "v2"}, conn=conn)
    approval.register_report(conn, sid, art2, 2)
    st = approval.get_state(conn, sid, 2)
    if st["status"] != approval.STATUS_PENDING_REVIEW:
        fails.append("登记后应为 pending_review，实际 %r" % st["status"])

    # 意见为空 -> 拒绝（必须可填写意见的强制面）
    _expect_error(fails, lambda: approval.reject(conn, sid, 2, opinion=""),
                  code="missing-opinion", label="空意见退回")
    _expect_error(fails, lambda: approval.reject(conn, sid, 2, opinion="   "),
                  code="missing-opinion", label="空白意见退回")

    OPINION = "危险部位 NB-r1-s0 的 MS=0.82 不足，需增加铺层后重算"
    st = approval.reject(conn, sid, 2, opinion=OPINION, operator="王审核")
    if st["status"] != approval.STATUS_REJECTED:
        fails.append("reject 后应为 rejected，实际 %r" % st["status"])
    if OPINION not in (st["last_reason"] or ""):
        fails.append("退回意见无法从状态快照查询到：%r" % st["last_reason"])

    # 通过审计历史查询退回意见（含角色/时间）
    hist = approval.history(conn, sid, 2)
    rejected = [h for h in hist if h["decision"] == approval.STATUS_REJECTED]
    if not rejected:
        fails.append("审计历史里没有退回记录：%r" % hist)
    else:
        h = rejected[-1]
        if OPINION not in (h["rationale"] or ""):
            fails.append("审计里的退回意见缺失：%r" % h["rationale"])
        if "审核" not in (h["actor"] or ""):
            fails.append("审计里的退回角色不正确：%r" % h["actor"])
        if not h["created_at"]:
            fails.append("审计里的退回时间缺失")

    # 退回 -> 重新提交审核（合法：设计/分析已修改）
    st = approval.submit_for_review(conn, sid, 2, operator="李工")
    if st["status"] != approval.STATUS_PENDING_REVIEW:
        fails.append("退回后重新提交应为 pending_review，实际 %r" % st["status"])

    # 重新提交后可以再通过
    st = approval.approve(conn, sid, 2, opinion="补铺层后满足要求", operator="王审核")
    if st["status"] != approval.STATUS_APPROVED:
        fails.append("重新提交后 approve 应为 approved，实际 %r" % st["status"])
    return art2


# ---------------------------------------------------------------------------
# 4) 角色权限
# ---------------------------------------------------------------------------

def test_role_permissions(fails, conn, sid):
    """审核动作只能由审核角色做，签发只能由签发角色做。"""
    art3 = study.add_artifact(sid, "report", content_uri="report:v3",
                              params={"tag": "v3"}, conn=conn)
    approval.register_report(conn, sid, art3, 3)

    _expect_error(fails,
                  lambda: approval.sign(conn, sid, 3, role=approval.ROLE_ANALYSIS),
                  code="invalid-role", label="分析角色签发")
    _expect_error(fails,
                  lambda: approval.approve(conn, sid, 3, role=approval.ROLE_ANALYSIS),
                  code="invalid-role", label="分析角色审核通过")
    _expect_error(fails,
                  lambda: approval.approve(conn, sid, 3, role="bogus"),
                  code="invalid-role", label="未知角色审核")
    _expect_error(fails,
                  lambda: approval.pending_for_role(conn, "bogus"),
                  code="invalid-role", label="未知角色查待办")

    # 角色正确时成功
    st = approval.approve(conn, sid, 3, role=approval.ROLE_REVIEWER,
                          opinion="通过")
    if st["status"] != approval.STATUS_APPROVED:
        fails.append("审核角色的 approve 应成功")
    st = approval.sign(conn, sid, 3, role=approval.ROLE_SIGNER, opinion="签发")
    if st["status"] != approval.STATUS_SIGNED:
        fails.append("签发角色的 sign 应成功")
    return art3


# ---------------------------------------------------------------------------
# 5) 重跑 -> 新版本重置为 pending_review（硬验收项，走真实链路）
# ---------------------------------------------------------------------------

def test_rerun_resets_approval(fails, work_root):
    """
    在真实 run_chain 链路上验证：
      v1 走到 signed（最终版）-> 重跑产生 v2 -> v2 必须是 pending_review，
      旧审批（尤其 signed）绝不继承。
    """
    svc = ws.WorkbenchService(work_root, mode="fake")
    try:
        sid = svc.start_study("重跑重置审批")
        r1 = svc.run_chain(sid, PARAMS, "wingbox_v1", rules=RULES)
        rep1 = (r1.get("artifacts") or {}).get("report")
        if not rep1:
            fails.append("rerun: 第一次运行没有 report 产物")
            return
        st1 = svc.get_approval(sid)
        if st1 is None:
            fails.append("rerun: 服务层未在报告产出后登记审批")
            return
        if st1["version"] != 1 or st1["status"] != approval.STATUS_PENDING_REVIEW:
            fails.append("rerun: v1 初始应为 pending_review，实际 %r"
                         % (st1,))

        # v1 一路走到签发（最终版）
        svc.approve_report(sid, opinion="v1 审核通过", operator="王审核")
        svc.sign_report(sid, opinion="v1 签发", operator="赵签发")
        if svc.get_approval(sid)["status"] != approval.STATUS_SIGNED:
            fails.append("rerun: v1 应为 signed")
        if svc.get_approval(sid)["final"] is not True:
            fails.append("rerun: v1 应为最终版")

        # 重跑 -> 新报告版本
        r2 = svc.run_chain(sid, PARAMS, "wingbox_v1",
                           rules={"criteria": ["tsai_wu"],
                                  "safety_factor": 1.2})
        rep2 = (r2.get("artifacts") or {}).get("report")
        if not rep2 or rep2 == rep1:
            fails.append("rerun: 重跑未产生新的 report 产物")
            return

        st2 = svc.get_approval(sid)          # 最新版
        if st2 is None:
            fails.append("rerun: v2 未登记审批")
            return
        if st2["version"] != 2:
            fails.append("rerun: 最新登记版本应为 2，实际 %r" % st2["version"])
        if st2["status"] != approval.STATUS_PENDING_REVIEW:
            fails.append("rerun: **新版本审批状态必须重置为 pending_review**，"
                         "实际 %r" % st2["status"])
        if st2["final"]:
            fails.append("rerun: 新版本不应继承旧版的'最终版'标记")
        if st2["last_reason"] and "v1" in str(st2["last_reason"]) and \
                "不继承" not in str(st2["last_reason"]):
            fails.append("rerun: 新版本审计理由出现旧版内容继承：%r"
                         % st2["last_reason"])

        # v1 的历史状态仍在（历史不可篡改）
        if svc.get_approval(sid, 1)["status"] != approval.STATUS_SIGNED:
            fails.append("rerun: v1 的签发记录被覆盖了")

        # 新版本必须重新审核才可签发（旧审批不能当新审批用）
        try:
            svc.sign_report(sid, opinion="想直接签发 v2")
            fails.append("rerun: 未审核的新版本 v2 竟然可以签发")
        except ws.ServiceError as exc:
            if "未通过审核" not in str(exc):
                fails.append("rerun: v2 直接签发被拒但原因不明确：%s" % exc)

        # 审计里应有 approval_rerun 记录
        hist = svc.get_approval_history(sid)
        rerun = [h for h in hist if h["gate"] == approval.GATE_RERUN]
        if not rerun:
            fails.append("rerun: 审计里缺少 approval_rerun 记录（现有 %r）"
                         % [h["gate"] for h in hist])
        else:
            if "重置" not in (rerun[-1]["rationale"] or ""):
                fails.append("rerun: 重跑审计理由应说明状态重置：%r"
                             % rerun[-1]["rationale"])
            if not rerun[-1]["created_at"]:
                fails.append("rerun: 重跑审计缺少时间")

        # 同一版本重复登记必须被拒（不能把已有审批抹掉）
        try:
            approval.register_report(svc.conn, sid, rep2, 2)
            fails.append("rerun: 同版本重复登记未被拒绝")
        except approval.ApprovalError as exc:
            if exc.code != "already-registered":
                fails.append("rerun: 重复登记错误码应为 already-registered，实际 %s"
                             % exc.code)
    finally:
        svc.close()


# ---------------------------------------------------------------------------
# 6) L2 审计完整性
# ---------------------------------------------------------------------------

def test_audit_trail(fails, conn, sid):
    """所有状态变更都在 decisions 表留痕：角色、动作、理由、时间。"""
    hist = approval.history(conn, sid)
    if len(hist) < 6:
        fails.append("审计记录过少（%d 条）：%r" % (len(hist), hist))

    gates = set(h["gate"] for h in hist)
    for g in (approval.GATE_SUBMIT, approval.GATE_REVIEW, approval.GATE_SIGN):
        if g not in gates:
            fails.append("审计缺少 gate %r（现有 %r）" % (g, sorted(gates)))
    for g in gates:
        if not str(g).startswith("approval"):
            fails.append("审批审计 gate 命名不合约定：%r" % g)

    for h in hist:
        if not h["actor"]:
            fails.append("审计记录缺少角色/经办人：%r" % h)
        if not h["decision"]:
            fails.append("审计记录缺少动作：%r" % h)
        if not h["created_at"]:
            fails.append("审计记录缺少时间：%r" % h)
        if not h["rationale"]:
            fails.append("审计记录缺少理由：%r" % h)

    # 直接查 decisions 表（确认落在 P1 既有表上，不是另建表）
    rows = conn.execute(
        "SELECT gate, actor, decision, rationale, created_at FROM decisions "
        "WHERE study_id=? AND gate LIKE 'approval%' ORDER BY id",
        (sid,)).fetchall()
    if len(rows) != len(hist):
        fails.append("decisions 表里的审批记录数（%d）与 history（%d）不一致"
                     % (len(rows), len(hist)))

    # 角色确实是中文角色名（可读审计）
    actors = " ".join(h["actor"] for h in hist)
    for name in ("审核", "签发"):
        if name not in actors:
            fails.append("审计 actor 里缺少「%s」角色：%r" % (name, actors))


# ---------------------------------------------------------------------------
# 7) 按角色查询待处理列表
# ---------------------------------------------------------------------------

def test_role_queues(fails, work_root):
    """审核人的待审清单、签发人的待签清单。"""
    svc = ws.WorkbenchService(work_root, mode="fake2")
    try:
        # 三个 Study 分别处于：待审核 / 已通过待签发 / 已签发
        sids = []
        for tag in ("A", "B", "C"):
            sid = svc.start_study("待办-%s" % tag, study_id="S-M18-Q-" + tag)
            svc.run_chain(sid, PARAMS, "wingbox_v1", rules=RULES)
            sids.append(sid)

        # A: 保持待审核；B: 审核通过（进入待签发）；C: 通过并签发
        svc.approve_report(sids[1], opinion="B 通过")
        svc.approve_report(sids[2], opinion="C 通过")
        svc.sign_report(sids[2], opinion="C 签发")

        rev = svc.pending_approvals(approval.ROLE_REVIEWER)
        rev_ids = set(r["study_id"] for r in rev)
        if "S-M18-Q-A" not in rev_ids:
            fails.append("审核待审清单缺少 A（现有 %r）" % rev_ids)
        for s in ("S-M18-Q-B", "S-M18-Q-C"):
            if s in rev_ids:
                fails.append("审核待审清单不应包含已审的 %s" % s)
        for r in rev:
            if r["status"] != approval.STATUS_PENDING_REVIEW:
                fails.append("审核待审清单里出现了状态 %r" % r["status"])

        sig = svc.pending_approvals(approval.ROLE_SIGNER)
        sig_ids = set(r["study_id"] for r in sig)
        if "S-M18-Q-B" not in sig_ids:
            fails.append("签发待签清单缺少 B（现有 %r）" % sig_ids)
        if "S-M18-Q-C" in sig_ids:
            fails.append("签发待签清单不应包含已签发的 C")
        if "S-M18-Q-A" in sig_ids:
            fails.append("签发待签清单不应包含未通过审核的 A（未通过审核不允许签发）")

        # 设计/分析：被退回的清单
        sid_d = svc.start_study("待办-D", study_id="S-M18-Q-D")
        svc.run_chain(sid_d, PARAMS, "wingbox_v1", rules=RULES)
        svc.reject_report(sid_d, opinion="MS 不足，请修设计")
        for role in (approval.ROLE_DESIGN, approval.ROLE_ANALYSIS):
            q = svc.pending_approvals(role)
            ids = set(r["study_id"] for r in q)
            if "S-M18-Q-D" not in ids:
                fails.append("「%s」待办清单缺少被退回的 D（现有 %r）"
                             % (role, ids))

        # 便捷函数
        if not any(r["study_id"] == "S-M18-Q-A"
                   for r in approval.pending_reviews(svc.conn)):
            fails.append("pending_reviews 未返回 A")
        if not any(r["study_id"] == "S-M18-Q-B"
                   for r in approval.pending_signs(svc.conn)):
            fails.append("pending_signs 未返回 B")
    finally:
        svc.close()


# ---------------------------------------------------------------------------
# 8) 快照字段（UI 可直接消费）
# ---------------------------------------------------------------------------

def test_state_snapshot(fails, conn, sid):
    """状态快照字段完整，UI 无需复制状态机。"""
    st = approval.get_state(conn, sid, 1)
    if st is None:
        fails.append("get_state 返回 None")
        return
    for key in ("study_id", "artifact_id", "version", "status", "status_name",
                "updated_at", "final", "editable", "can_review", "can_sign",
                "can_resubmit", "last_reason", "last_actor", "last_gate"):
        if key not in st:
            fails.append("状态快照缺少字段 %r" % key)
    if st["status_name"] != "已签发（最终版）":
        fails.append("签发的状态中文名不对：%r" % st["status_name"])
    if st["can_review"] or st["can_sign"] or st["can_resubmit"]:
        fails.append("最终版快照不应还有可执行动作：%r" % st)
    # 未登记的版本
    if approval.get_state(conn, sid, 99) is not None:
        fails.append("未登记版本 get_state 应返回 None")
    try:
        approval.require_state(conn, sid, 99)
        fails.append("未登记版本 require_state 应抛错")
    except approval.ApprovalError as exc:
        if exc.code != "not-registered":
            fails.append("未登记版本错误码应为 not-registered，实际 %s" % exc.code)

    # 版本清单
    versions = [v["version"] for v in approval.list_versions(conn, sid)]
    if versions != sorted(versions) or 1 not in versions:
        fails.append("list_versions 结果异常：%r" % versions)


# ---------------------------------------------------------------------------
# 9) P1 老库兼容（approval 不建表、迁移幂等）
# ---------------------------------------------------------------------------

def test_db_compat(fails, tmp):
    """P1 老库升级后审批表可用，且 P1 既有数据仍可查。"""
    old = os.path.join(tmp, "p1_old.db")
    conn = db.connect(old)
    try:
        tpls = set(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        for t in ("report_approvals", "decisions", "artifacts", "studies",
                  "model_configs", "model_config_parts"):
            if t not in tpls:
                fails.append("老库升级后缺少表 %r" % t)

        cols = [r[1] for r in conn.execute("PRAGMA table_info(artifacts)")]
        if "approval_status" not in cols:
            fails.append("artifacts 缺少 approval_status 列")

        # 幂等：再连一次不应报错
        conn.close()
        conn = db.connect(old)
        study.create_study("S-M18-OLD", "P1 老库 Study", conn=conn)
        aid = study.add_artifact("S-M18-OLD", "report", content_uri="r:1",
                                 params={}, conn=conn)
        st = approval.register_report(conn, "S-M18-OLD", aid, 1)
        if st["status"] != approval.STATUS_PENDING_REVIEW:
            fails.append("老库上审批登记失败")
        # artifact 缓存列同步了
        row = conn.execute("SELECT approval_status FROM artifacts WHERE id=?",
                           (aid,)).fetchone()
        if row["approval_status"] != approval.STATUS_PENDING_REVIEW:
            fails.append("artifacts.approval_status 缓存未同步：%r"
                         % row["approval_status"])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 10) 服务层错误是可读的 ServiceError（UI 直接显示）
# ---------------------------------------------------------------------------

def test_service_errors(fails, work_root):
    """服务层把审批错误转成可读 ServiceError；无报告时有明确提示。"""
    svc = ws.WorkbenchService(work_root, mode="fake3")
    try:
        sid = svc.start_study("服务层错误", study_id="S-M18-ERR")
        try:
            svc.get_approval_history(sid)  # 空 Study：返回空列表，不抛
        except Exception as exc:
            fails.append("空 Study 查审批历史不应抛错：%s" % exc)

        # 无报告版本 -> 审核报可读错误
        try:
            svc.approve_report(sid, opinion="x")
            fails.append("无报告版本时 approve 应失败")
        except ws.ServiceError as exc:
            if "尚未登记" not in str(exc) and "尚无" not in str(exc):
                fails.append("无报告版本时错误原因不明确：%s" % exc)

        # 有报告但未通过审核 -> 签发被拒，原因可读
        svc.run_chain(sid, PARAMS, "wingbox_v1", rules=RULES)
        try:
            svc.sign_report(sid, opinion="直接签发")
            fails.append("未通过审核的报告不应能签发（服务层）")
        except ws.ServiceError as exc:
            if "未通过审核" not in str(exc):
                fails.append("未通过审核的签发被拒但原因不明确：%s" % exc)

        # 退回不填意见 -> 可读错误
        try:
            svc.reject_report(sid, opinion="")
            fails.append("退回不填意见不应成功（服务层）")
        except ws.ServiceError as exc:
            if "意见" not in str(exc):
                fails.append("退回缺意见的错误原因不明确：%s" % exc)
    finally:
        svc.close()


# ---------------------------------------------------------------------------
# 11) Python 3.8 兼容
# ---------------------------------------------------------------------------

def test_py38(fails):
    """本票新增/改动的文件必须 Python 3.8 兼容。"""
    paths = [os.path.join(CORE, "approval.py"),
             os.path.join(CORE, "workbench_service.py"),
             os.path.join(CORE, "db.py"),
             os.path.abspath(__file__)]
    violations = compat_py38.check_paths(paths)
    if violations:
        fails.append("Python 3.8 不兼容：\n" + compat_py38.format_violations(violations))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    fails = []
    tmp = _fresh("case")

    # 纯审批状态机部分：一个干净库 + 一个 Study
    conn = db.connect(os.path.join(tmp, "approval.db"))
    sid = "S-M18-SM"
    try:
        study.create_study(sid, "审批状态机", conn=conn)
        art1 = study.add_artifact(sid, "report", content_uri="report:v1",
                                  params={"tag": "v1"}, conn=conn)

        print("1. 状态机合法/非法转换 ...")
        test_state_machine(fails, conn, sid, art1)
        print("2. 签发后终态不可改 ...")
        test_final_immutable(fails, conn, sid)
        print("3. 退回 + 意见可查询 ...")
        test_reject_with_opinion(fails, conn, sid)
        print("4. 角色权限 ...")
        test_role_permissions(fails, conn, sid)
        print("5. 状态快照字段 ...")
        test_state_snapshot(fails, conn, sid)
        print("6. L2 审计完整性 ...")
        test_audit_trail(fails, conn, sid)
    finally:
        conn.close()

    print("7. 重跑 -> 新版本重置为 pending_review ...")
    test_rerun_resets_approval(fails, os.path.join(tmp, "rerun"))
    print("8. 按角色查询待处理列表 ...")
    test_role_queues(fails, os.path.join(tmp, "queues"))
    print("9. P1 老库兼容 ...")
    test_db_compat(fails, tmp)
    print("10. 服务层错误可读 ...")
    test_service_errors(fails, os.path.join(tmp, "errors"))
    print("11. Python 3.8 兼容 ...")
    test_py38(fails)

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M18 (票18) SELF-TEST: ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
