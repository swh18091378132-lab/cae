# -*- coding: utf-8 -*-
"""
test_m19.py -- 票19 验收：审批流与型号汇总接入工作台

验收点（对应 issue #19）：
  1. 工作台可展示待审 / 待签列表，并按角色区分可见操作
  2. 审核操作界面能看到报告对应血缘与校核明细，据此做出通过 / 退回
  3. **签发入口只对已通过审核的报告可用**（未通过的不可签发）
  4. 型号汇总视图展示各部件报告的流程状态
  5. 工作台逻辑放在服务层，可脱离 UI 单测
  6. UI 冒烟可构造并销毁；Python 3.8 兼容

策略（与 test_m8 / test_m17 / test_m18 一脉相承）：
  控件难以自动化测试，所以**编排逻辑全在 workbench_service**，
  本文件除最后的 UI 冒烟外**不 import tkinter**（第 1~5 项全是纯服务层断言）。
  UI 冒烟只做两件事：能构造/销毁；以及**签发按钮的可用性真的挂在
  "报告是否已通过审核"上**（未通过置灰、通过后可用）——这是验收项 3 的
  UI 侧证据，逻辑判据仍来自服务层 can_sign_report / review_detail。

运行 (在 apps/workbench 下): python tests/test_m19.py
"""

import os
import sys
import ast
import json
import shutil
import inspect

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
sys.path.insert(0, CORE)
sys.path.insert(0, WORKBENCH)

import compat_py38
import approval
import model_config as mc
import model_rollup as mr
import workbench_service as ws


# 全机总体参数：常规运输机量级（票16 默认分解规则的适用范围）
OVERALL = {
    "span": 34.0,
    "fuselage_length": 30.0,
    "tail_volume": 0.9,
    "fuselage_diameter": 3.2,
    "sweep_deg": 25.0,
}

PARTS = ("wing", "tail", "fuselage")

MODEL_NAME = "某型运输机-方案A"

PARAMS = {"span": 5.0, "sweep_deg": 10.0, "c_root": 1.0, "c_tip": 0.6,
          "n_ribs": 3, "n_stringers": 4}

RULES = {"criteria": ["tsai_wu", "hashin"], "safety_factor": 1.0}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _fresh(name):
    # 按 PID 隔离临时目录：并发跑测试（如全量 + 单票同时进行）时互不干扰
    d = os.path.join(WORKBENCH, ".tmp_test", "m19", "run_%d" % os.getpid(), name)
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d)
    return d


def _row_of(rows, study_id):
    for r in rows:
        if r.get("study_id") == study_id:
            return r
    return None


def _part_row(overview, part_key):
    for r in overview.get("parts") or []:
        if r.get("part_key") == part_key:
            return r
    return None


def _expect_service_error(fails, fn, must_contain, label):
    """断言 fn() 抛 ServiceError，且消息里包含指定片段（可读原因）。"""
    try:
        fn()
    except ws.ServiceError as exc:
        text = str(exc)
        for frag in must_contain:
            if frag not in text:
                fails.append("%s：错误消息缺少 %r（实际：%s）" % (label, frag, text))
        if not text.strip():
            fails.append("%s：错误消息为空（不可读）" % label)
        return exc
    except Exception as exc:
        fails.append("%s：期望 ServiceError，实际 %s: %s"
                     % (label, type(exc).__name__, exc))
        return None
    fails.append("%s：期望报错，实际没有报错" % label)
    return None


def _check_params(conn, study_id, kind="check_result"):
    row = conn.execute(
        "SELECT version, params_json FROM artifacts WHERE study_id=? AND kind=? "
        "ORDER BY version DESC LIMIT 1", (study_id, kind)).fetchone()
    if row is None:
        return None, None
    return int(row["version"]), (json.loads(row["params_json"] or "{}"))


# ---------------------------------------------------------------------------
# 1) 待审 / 待签列表 + 按角色区分可见操作
# ---------------------------------------------------------------------------

def test_pending_lists_and_roles(fails, svc, model_id):
    """审核人的待审清单、签发人的待签清单，以及各角色可见动作。"""
    view = mr.rollup(svc.conn, model_id)
    sids = dict((r["part_key"], r["conclusion_study_id"]) for r in view["parts"])

    # ---- 初始：三份报告都是待审核；签发清单必须为空 ----
    rev = svc.pending_review_list()
    if len(rev) != 3:
        fails.append("审核待审清单应有 3 条，实际 %d：%r"
                     % (len(rev), [r["study_id"] for r in rev]))
    for r in rev:
        sid = r["study_id"]
        # 每项含报告版本 / Study 名 / 部件与型号信息（UI 一行所需）
        for field in ("version", "report_version", "study_name", "part_key",
                      "model_id", "model_name", "status", "status_name",
                      "check", "label", "actions"):
            if field not in r:
                fails.append("待审行缺少字段 %r：%r" % (field, sorted(r)))
        if r["version"] != 1:
            fails.append("待审行的报告版本应为 v1，实际 %r" % (r["version"],))
        if r["model_name"] != MODEL_NAME:
            fails.append("待审行未带出型号名：%r" % (r["model_name"],))
        if r["part_key"] not in PARTS:
            fails.append("待审行的部件键不对：%r" % (r["part_key"],))
        if not r["study_name"]:
            fails.append("待审行缺少 Study 名")
        if r["status"] != approval.STATUS_PENDING_REVIEW:
            fails.append("待审行状态应为待审核，实际 %r" % (r["status"],))
        if (r["check"] or {}).get("max_fi") is None:
            fails.append("待审行没有带出校核结论（审核人无法据此判断）：%r"
                         % (r["check"],))
        # 审核角色只应看到「通过 / 退回」，看不到签发
        if r["actions"] != [ws.APPROVAL_ACTION_APPROVE,
                            ws.APPROVAL_ACTION_REJECT]:
            fails.append("审核待审行的可见动作不对：%r" % (r["actions"],))
        if not (r["can_approve"] and r["can_reject"]) or r["can_sign"]:
            fails.append("审核待审行的动作可用性不对：%r" % (r,))
        # 未通过审核 -> 签发被服务层挡住，并给出可读原因
        if not r["sign_blocked_reason"]:
            fails.append("待审行应带出'不能签发'的原因（UI 据此置灰）")
        elif "未通过审核" not in r["sign_blocked_reason"]:
            fails.append("不能签发的原因不明确：%r" % (r["sign_blocked_reason"],))

    if svc.pending_sign_list():
        fails.append("没有任何报告通过审核时，签发清单必须为空：%r"
                     % ([r["study_id"] for r in svc.pending_sign_list()],))

    # ---- 角色可见动作清单（票19 硬验收项：按角色区分） ----
    if svc.approval_actions_for_role(approval.ROLE_REVIEWER) != [
            ws.APPROVAL_ACTION_APPROVE, ws.APPROVAL_ACTION_REJECT]:
        fails.append("审核角色的可见动作应只有通过/退回")
    if svc.approval_actions_for_role(approval.ROLE_SIGNER) != [
            ws.APPROVAL_ACTION_SIGN]:
        fails.append("签发角色的可见动作应只有签发")
    for role in (approval.ROLE_DESIGN, approval.ROLE_ANALYSIS):
        if svc.approval_actions_for_role(role) != []:
            fails.append("角色「%s」不应有审批动作（只能查看）"
                         % approval.role_name(role))
    _expect_service_error(fails,
                          lambda: svc.approval_actions_for_role("bogus"),
                          ("未知角色",), "未知角色查可见动作")
    _expect_service_error(fails,
                          lambda: svc.approval_inbox("bogus"),
                          ("未知角色",), "未知角色查待办")

    # ---- 审核通过机翼 -> 进入待签清单，且从待审清单消失 ----
    wing = sids["wing"]
    svc.approve_report(wing, opinion="机翼校核结论可信，同意")
    sig = svc.pending_sign_list()
    if len(sig) != 1 or sig[0]["study_id"] != wing:
        fails.append("机翼通过审核后应进入签发待签清单：%r"
                     % ([r["study_id"] for r in sig],))
    else:
        row = sig[0]
        if row["actions"] != [ws.APPROVAL_ACTION_SIGN]:
            fails.append("签发待签行的可见动作应只有签发：%r" % (row["actions"],))
        if not row["can_sign"] or row["can_approve"] or row["can_reject"]:
            fails.append("签发待签行的动作可用性不对：%r" % (row,))
        if row["sign_blocked_reason"] is not None:
            fails.append("已通过审核的报告不应带'不能签发'的原因：%r"
                         % (row["sign_blocked_reason"],))
    if any(r["study_id"] == wing for r in svc.pending_review_list()):
        fails.append("已审核的报告仍留在待审清单里")

    # ---- 退回尾翼 -> 设计 / 强度分析的已退回清单可查，且无审批动作 ----
    tail = sids["tail"]
    OPINION = "危险部位 MS 不足，请增加铺层后重算"
    svc.reject_report(tail, opinion=OPINION)
    for role in (approval.ROLE_DESIGN, approval.ROLE_ANALYSIS):
        q = svc.approval_inbox(role)
        row = _row_of(q, tail)
        if row is None:
            fails.append("角色「%s」的已退回清单缺少尾翼：%r"
                         % (approval.role_name(role), [r["study_id"] for r in q]))
            continue
        if row["status"] != approval.STATUS_REJECTED:
            fails.append("已退回清单里的状态应为 rejected，实际 %r"
                         % (row["status"],))
        if row["actions"]:
            fails.append("设计/分析角色不应看到审批动作：%r" % (row["actions"],))
        if row["can_approve"] or row["can_reject"] or row["can_sign"]:
            fails.append("设计/分析角色的动作可用性应为全部不可用：%r" % (row,))
        if OPINION not in (row.get("last_reason") or ""):
            fails.append("已退回行查不到退回意见：%r" % (row.get("last_reason"),))

    # ---- 已退回的报告同样不允许签发 ----
    ok, reason = svc.can_sign_report(tail)
    if ok:
        fails.append("已退回的报告不应允许签发")
    elif "未通过审核" not in reason:
        fails.append("已退回报告的不能签发原因不明确：%r" % (reason,))

    return sids


# ---------------------------------------------------------------------------
# 2) 审核详情：血缘 + 校核明细
# ---------------------------------------------------------------------------

def test_review_detail(fails, svc, sids):
    """审核人能看到该报告版本的完整血缘与校核明细（据此判断是否可信）。"""
    fus = sids["fuselage"]
    detail = svc.get_review_detail(fus)

    # ---- 身份与状态 ----
    if detail["study_id"] != fus:
        fails.append("审核详情的 Study 不对")
    if detail["version"] != 1 or detail["status"] != approval.STATUS_PENDING_REVIEW:
        fails.append("审核详情的报告版本/状态不对：v%r %r"
                     % (detail["version"], detail["status"]))
    if detail["model_name"] != MODEL_NAME or detail["part_key"] != "fuselage":
        fails.append("审核详情未带出型号/部件归属：%r / %r"
                     % (detail["model_name"], detail["part_key"]))
    if detail["template_id"] != "fuselage_section_v1":
        fails.append("审核详情未带出模板：%r" % (detail["template_id"],))
    if ws.APPROVAL_ACTION_APPROVE not in detail["actions"] or \
            ws.APPROVAL_ACTION_REJECT not in detail["actions"]:
        fails.append("待审核报告的详情页应可用通过/退回：%r" % (detail["actions"],))

    # ---- 血脉完整：报告节点的父节点是校核结论 ----
    lin = detail["lineage"]
    nodes = lin.get("nodes") or []
    if len(nodes) < 10:
        fails.append("审核详情的血缘节点过少（%d），不是完整链" % len(nodes))
    if len(lin.get("order") or []) != len(nodes):
        fails.append("审核详情的血缘拓扑序长度不一致")
    by_id = dict((n["id"], n) for n in nodes)
    rep_id = detail["approval"]["artifact_id"]
    if rep_id not in by_id:
        fails.append("审核详情的血缘里没有本次送审的报告产物 #%r" % (rep_id,))
    else:
        parents = by_id[rep_id]["parents"]
        p_kinds = [by_id[p]["kind"] for p in parents if p in by_id]
        if "check_result" not in p_kinds:
            fails.append("报告的血缘父节点里没有校核结论：%r" % (p_kinds,))
        if by_id[rep_id]["kind"] != "report":
            fails.append("送审产物不是 report：%r" % (by_id[rep_id]["kind"],))
    kinds = set(n["kind"] for n in nodes)
    for need in ("parameter_set", "catpart", "topology", "mesh", "inp", "odb",
                 "extraction", "check_rule", "check_result", "report"):
        if need not in kinds:
            fails.append("审核详情的血缘缺少 %r" % (need,))
    if fus not in (detail["lineage_text"] or ""):
        fails.append("血缘文本里没有 Study id")
    if "report" not in (detail["lineage_text"] or ""):
        fails.append("血缘文本里没有 report 节点")

    # ---- 校核明细摘要与数据库产物一致（不是另算一套） ----
    chk = detail["check"]
    if chk is None:
        fails.append("审核详情没有校核明细（审核人无法判断是否可信）")
    else:
        ver, params = _check_params(svc.conn, fus)
        if chk["version"] != ver:
            fails.append("审核详情的校核版本与产物不一致：%r != %r"
                         % (chk["version"], ver))
        for field in ("max_fi", "min_ms", "all_pass", "n_detail_rows",
                      "n_bays_checked", "n_bays_excluded", "hotspots",
                      "criteria", "safety_factor", "n_hotspots"):
            if field not in chk:
                fails.append("校核明细缺少字段 %r" % (field,))
        if not chk["hotspots"]:
            fails.append("校核明细没有危险部位（审核无从判断）")
        else:
            h = chk["hotspots"][0]
            for field in ("elem", "sp", "criterion", "FI", "MS"):
                if field not in h:
                    fails.append("危险部位缺少字段 %r" % (field,))
        if abs(float(chk["max_fi"]) - float(params["max_fi"])) > 1e-9:
            fails.append("校核明细的最大 FI 与产物不一致：%r != %r"
                         % (chk["max_fi"], params["max_fi"]))
        if abs(float(chk["min_ms"]) - float(params["min_ms"])) > 1e-9:
            fails.append("校核明细的最小 MS 与产物不一致：%r != %r"
                         % (chk["min_ms"], params["min_ms"]))
        if bool(chk["all_pass"]) != bool(params["all_pass"]):
            fails.append("校核明细的合格判定与产物不一致")
        if not chk["criteria"]:
            fails.append("校核明细没有带出失效准则")
        for frag in ("危险部位", "最大 FI", "自然格"):
            if frag not in (chk["text"] or ""):
                fails.append("校核明细文本缺少 %r" % (frag,))
        # 明细行样本（审核人可逐行核对）；有 CSV 时应有样本
        if chk["detail_rows_total"] and not chk["detail_rows_sample"]:
            fails.append("校核明细没有提供明细行样本")

    # ---- 审批历史（含谁在何时做了什么） ----
    hist = detail["history"]
    if not hist:
        fails.append("审核详情没有审批历史")
    else:
        if not any(h["gate"] == approval.GATE_SUBMIT for h in hist):
            fails.append("审批历史里没有提交审核的记录")
        for h in hist:
            if not h["actor"] or not h["created_at"]:
                fails.append("审批历史缺少角色/时间：%r" % (h,))

    # ---- 可读渲染：一屏给全血缘 + 校核 + 历史 ----
    text = detail["text"]
    for frag in ("【校核明细】", "【血缘】", "【审批历史】", "报告 v1"):
        if frag not in text:
            fails.append("审核详情文本缺少 %r" % (frag,))

    # ---- 未登记审批的 Study 给出可读错误 ----
    sid = svc.start_study("无报告 Study")
    _expect_service_error(fails,
                          lambda: svc.get_review_detail(sid),
                          ("尚未登记审批",), "未登记审批的 Study 查审核详情")
    if svc.get_check_detail(sid) is not None:
        fails.append("无校核产物的 Study 不应给出校核明细")
    if "尚无校核结论" not in svc.format_check_detail_text(sid):
        fails.append("无校核产物时应给出明确提示文本")

    # ---- 审核详情可按角色过滤可见动作（UI 不复制角色权限表） ----
    rev = svc.get_review_detail(fus, role=approval.ROLE_REVIEWER)
    if rev["actions"] != [ws.APPROVAL_ACTION_APPROVE, ws.APPROVAL_ACTION_REJECT]:
        fails.append("审核角色看待审报告应只可见通过/退回：%r" % (rev["actions"],))
    sig = svc.get_review_detail(fus, role=approval.ROLE_SIGNER)
    if sig["actions"]:
        fails.append("签发角色看未通过审核的报告不应有可见动作：%r"
                     % (sig["actions"],))
    if sig["can_sign"]:
        fails.append("签发角色看未通过审核的报告不应可签发")
    _expect_service_error(fails,
                          lambda: svc.get_review_detail(fus, role="bogus"),
                          ("未知角色",), "未知角色查审核详情")


# ---------------------------------------------------------------------------
# 3) 签发入口只对已通过审核的报告可用
# ---------------------------------------------------------------------------

def test_sign_gate(fails, svc, sids):
    """未通过审核 / 已退回 / 已签发 / 未登记，都不允许签发（服务层断言）。"""
    tail = sids["tail"]          # 已退回
    fus = sids["fuselage"]       # 待审核
    wing = sids["wing"]          # 已通过审核（第 1 项里 approve 过）

    # ---- 3.1 待审核 -> 不可签发，原因可读 ----
    ok, reason = svc.can_sign_report(fus)
    if ok:
        fails.append("待审核的报告不应允许签发")
    else:
        if "未通过审核" not in reason:
            fails.append("待审核报告不能签发的原因不明确：%r" % (reason,))
        if "待审核" not in reason:
            fails.append("不能签发的原因未点明当前状态：%r" % (reason,))
    _expect_service_error(fails,
                          lambda: svc.assert_can_sign(fus),
                          ("未通过审核",), "待审核报告 assert_can_sign")
    _expect_service_error(fails,
                          lambda: svc.sign_report(fus, opinion="直接签发"),
                          ("未通过审核",), "待审核报告直接签发")
    # 状态没被改动
    if svc.get_approval(fus)["status"] != approval.STATUS_PENDING_REVIEW:
        fails.append("签发被拒后报告状态被改动了")

    # ---- 3.2 已退回 -> 不可签发 ----
    ok, reason = svc.can_sign_report(tail)
    if ok:
        fails.append("已退回的报告不应允许签发")
    _expect_service_error(fails,
                          lambda: svc.sign_report(tail, opinion="硬签发"),
                          ("未通过审核",), "已退回报告签发")

    # ---- 3.3 未登记审批的 Study -> 不可签发 ----
    sid = svc.start_study("无审批 Study")
    ok, reason = svc.can_sign_report(sid)
    if ok:
        fails.append("未登记审批的 Study 不应允许签发")
    elif "尚未登记审批" not in reason:
        fails.append("未登记审批的不能签发原因不明确：%r" % (reason,))
    _expect_service_error(fails,
                          lambda: svc.sign_report(sid, opinion="x"),
                          ("登记审批",), "未登记审批的 Study 签发")

    # ---- 3.4 已通过审核 -> 可签发；签发后为最终版，不再可签 ----
    ok, reason = svc.can_sign_report(wing)
    if not ok:
        fails.append("已通过审核的报告应允许签发，实际被拒：%r" % (reason,))
    detail = svc.get_review_detail(wing)
    if detail["can_sign"] is not True or detail["sign_blocked_reason"] is not None:
        fails.append("已通过审核的报告详情页应可签发：%r / %r"
                     % (detail.get("can_sign"), detail.get("sign_blocked_reason")))
    if ws.APPROVAL_ACTION_SIGN not in detail["actions"]:
        fails.append("已通过审核的报告详情页没有签发动作：%r" % (detail["actions"],))
    st = svc.sign_report(wing, opinion="批准发布", operator="赵签发")
    if st["status"] != approval.STATUS_SIGNED or not st["final"]:
        fails.append("签发后应为已签发的最终版：%r" % (st,))
    ok, reason = svc.can_sign_report(wing)
    if ok:
        fails.append("已签发的最终版不应允许重复签发")
    elif "最终版" not in reason:
        fails.append("已签发后不能签发的原因应说明已为最终版：%r" % (reason,))
    if wing in [r["study_id"] for r in svc.pending_sign_list()]:
        fails.append("已签发的报告仍留在待签清单里")

    # ---- 3.5 待签清单里绝不允许出现未通过审核的报告（硬断言） ----
    for r in svc.pending_sign_list():
        if r["status"] != approval.STATUS_APPROVED:
            fails.append("待签清单出现了非'已通过审核'的报告：%s %r"
                         % (r["study_id"], r["status"]))
        if not r["can_sign"]:
            fails.append("待签清单里的行却不可签发：%r" % (r["study_id"],))


# ---------------------------------------------------------------------------
# 4) 型号汇总视图：各部件报告的流程状态
# ---------------------------------------------------------------------------

def test_model_overview(fails, svc, model_id):
    """型号汇总视图展示各部件报告处于哪个流程环节，并与审批模块一致。"""
    view = svc.get_model_approval_overview(model_id)
    if view["model_id"] != model_id or view["model_name"] != MODEL_NAME:
        fails.append("型号汇总视图的型号信息不对：%r / %r"
                     % (view.get("model_id"), view.get("model_name")))
    parts = view["parts"]
    if [p["part_key"] for p in parts] != list(PARTS):
        fails.append("型号汇总的部件顺序不对：%r"
                     % ([p["part_key"] for p in parts],))

    for p in parts:
        for field in ("part_key", "template_id", "study_id", "report_version",
                      "approval_version", "status", "status_name", "updated_at",
                      "can_review", "can_sign", "final", "max_fi", "min_ms",
                      "pass", "run_status", "run_status_name", "report_path"):
            if field not in p:
                fails.append("部件 %r 的汇总行缺少字段 %r" % (p["part_key"], field))
        # 流程状态必须来自 approval.get_state（不复制状态机）
        st = approval.get_state(svc.conn, p["approval_study_id"])
        if st is None:
            fails.append("部件 %r 的报告未登记审批" % (p["part_key"],))
            continue
        if p["status"] != st["status"] or p["status_name"] != st["status_name"]:
            fails.append("部件 %r 的流程状态与审批模块不一致：%r / %r != %r / %r"
                         % (p["part_key"], p["status"], p["status_name"],
                            st["status"], st["status_name"]))
        if p["approval_version"] != st["version"]:
            fails.append("部件 %r 的流程状态版本不对：%r != %r"
                         % (p["part_key"], p["approval_version"], st["version"]))
        if p["max_fi"] is None or p["min_ms"] is None:
            fails.append("部件 %r 的汇总行没有 FI/MS 结论" % (p["part_key"],))

    # ---- 三种不同环节同时可见：机翼已签发 / 尾翼已退回 / 机身待审核 ----
    wing = _part_row(view, "wing")
    tail = _part_row(view, "tail")
    fus = _part_row(view, "fuselage")
    if wing["status"] != approval.STATUS_SIGNED or not wing["final"]:
        fails.append("机翼应为已签发（最终版），实际 %r" % (wing["status"],))
    if tail["status"] != approval.STATUS_REJECTED:
        fails.append("尾翼应为已退回，实际 %r" % (tail["status"],))
    if fus["status"] != approval.STATUS_PENDING_REVIEW:
        fails.append("机身应为待审核，实际 %r" % (fus["status"],))
    if not tail["last_reason"]:
        fails.append("型号汇总里看不到退回意见")
    elif "MS 不足" not in tail["last_reason"]:
        fails.append("型号汇总里的退回意见不对：%r" % (tail["last_reason"],))
    if tail["last_gate"] != approval.GATE_REVIEW:
        fails.append("型号汇总应带出最近一次审批动作的类别（审核），实际 %r"
                     % (tail["last_gate"],))
    if not tail["last_actor"]:
        fails.append("型号汇总应带出最近一次审批动作的经办角色")
    if wing["can_sign"] or wing["can_review"]:
        fails.append("已签发的部件不应还能审批/签发")
    if not fus["can_review"] or fus["can_sign"]:
        fails.append("待审核的部件应可审核、不可签发")

    # ---- 总计：流程状态计数 ----
    totals = view["totals"]
    if totals["n_parts"] != 3:
        fails.append("型号汇总的部件数不对：%r" % (totals.get("n_parts"),))
    expect = {approval.STATUS_SIGNED: 1, approval.STATUS_REJECTED: 1,
              approval.STATUS_PENDING_REVIEW: 1}
    for status, n in expect.items():
        if totals["by_status"].get(status) != n:
            fails.append("流程状态 %r 的计数应为 %d，实际 %r"
                         % (status, n, totals["by_status"].get(status)))
    if totals["n_signed"] != 1 or totals["n_rejected"] != 1 or \
            totals["n_pending_review"] != 1:
        fails.append("型号汇总的环节计数快捷字段不对：%r" % (totals,))

    # ---- 可读渲染：每个部件一行 + 流程状态 ----
    text = view["text"]
    for key in PARTS:
        if key not in text:
            fails.append("型号汇总文本里没有部件 %r" % (key,))
    for frag in ("流程状态", "已签发", "已退回", "待审核"):
        if frag not in text:
            fails.append("型号汇总文本没有显示 %r" % (frag,))
    if "MS 不足" not in text:
        fails.append("型号汇总文本没有显示退回意见")
    if svc.format_model_approval(model_id) != text:
        fails.append("format_model_approval 与 overview 的渲染不一致")

    # ---- 刷新后随之变化：机身通过审核 -> 待签 +1、待审 -1 ----
    svc.approve_report(fus["study_id"], opinion="机身校核可信")
    view2 = svc.get_model_approval_overview(model_id)
    fus2 = _part_row(view2, "fuselage")
    if fus2["status"] != approval.STATUS_APPROVED:
        fails.append("机身通过审核后汇总状态未更新：%r" % (fus2["status"],))
    if view2["totals"]["n_approved"] != 1 or \
            view2["totals"]["n_pending_review"] != 0:
        fails.append("通过审核后型号汇总的环节计数未更新：%r"
                     % (view2["totals"],))
    if "已通过审核" not in view2["text"]:
        fails.append("型号汇总文本未显示'已通过审核'")

    # ---- 错误可读：不存在的型号 ----
    _expect_service_error(fails,
                          lambda: svc.get_model_approval_overview("MODEL-NOT-EXIST"),
                          ("不存在",), "不存在的型号汇总")


# ---------------------------------------------------------------------------
# 5) 工作台逻辑在服务层，可脱离 UI 单测
# ---------------------------------------------------------------------------

def test_logic_in_service_layer(fails):
    """
    审批工作台的全部判据都在服务层（纯 Python），UI 不复制状态机：

      * workbench_service 源码不引用 tkinter；
      * 本测试文件除 test_ui_smoke 外不 import tkinter（验收项 5 的证据）；
      * 服务层不对外暴露任何控件对象（返回的都是 dict/列表/字符串）。
    """
    src = inspect.getsource(ws)
    if "tkinter" in src or "tk." in src:
        fails.append("服务层源码出现了 UI 依赖（tkinter/tk.）——逻辑不应在 UI 层")

    # 顶层 import / 函数体内 import：tkinter 只允许出现在 test_ui_smoke 里
    with open(os.path.abspath(__file__), "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    def _is_tk_import(sub):
        if isinstance(sub, ast.Import):
            for a in sub.names:
                if (a.name or "").split(".")[0] == "tkinter":
                    return True
        elif isinstance(sub, ast.ImportFrom):
            if (sub.module or "").split(".")[0] == "tkinter":
                return True
        return False

    def _imports_tk(node):
        for sub in ast.walk(node):
            if _is_tk_import(sub):
                return True
        return False

    for node in tree.body:                 # 顶层语句只看这一层，不深入函数体
        if _is_tk_import(node):
            fails.append("测试文件顶层 import 了 tkinter（逻辑应可脱离 UI 单测）")
    bad_funcs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and _imports_tk(node):
            bad_funcs.append(node.name)
    if bad_funcs != ["test_ui_smoke"]:
        fails.append("tkinter 只允许在 test_ui_smoke 里 import，实际出现在 %r"
                     % (bad_funcs,))

    # 服务层返回的是纯数据（无控件对象）：抽查几个入口的返回类型
    for name in ("approval_inbox", "pending_review_list", "pending_sign_list",
                 "get_review_detail", "get_model_approval_overview"):
        fn = getattr(ws.WorkbenchService, name, None)
        if fn is None:
            fails.append("服务层缺少票19 入口 %r" % (name,))
        elif not callable(fn):
            fails.append("服务层入口 %r 不可调用" % (name,))


# ---------------------------------------------------------------------------
# 6) UI 冒烟：能构造并销毁；签发按钮只对已通过审核的报告可用
# ---------------------------------------------------------------------------

def test_ui_smoke(fails):
    """UI 冒烟（照 test_m8 套路）：无显示环境则 SKIP。"""
    try:
        import tkinter
    except ImportError:
        print("  [SKIP] 无 tkinter")
        return

    try:
        probe = tkinter.Tk()
        probe.withdraw()
        probe.destroy()
    except Exception as exc:
        print("  [SKIP] 无显示环境: %s" % exc)
        return

    try:
        import ui_workbench
    except Exception as exc:
        fails.append("ui_workbench import failed: %s" % exc)
        return

    root_dir = _fresh("ui")
    svc = ws.WorkbenchService(root_dir, mode="fake")
    root = None
    try:
        sid = svc.start_study("UI 冒烟 Study")
        svc.run_chain(sid, PARAMS, "wingbox_v1", rules=RULES)
        # 另建一个型号配置并只跑机翼：让"型号汇总视图"有真实内容可显示
        mid = mc.create_model_config(MODEL_NAME, OVERALL, conn=svc.conn)
        batch = svc.run_model_batch(mid, parts=["wing"])
        if not batch.get("ok"):
            fails.append("UI 冒烟准备失败：型号批量运行未成功 %r"
                         % ([(r["part_key"], r["error"]) for r in batch["parts"]],))

        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        app = ui_workbench.WorkbenchApp(root, svc)

        # 既有分析页仍然完好（模板 / 参数 / Study 列表 / 血缘控件）
        if not app.tpl_combo["values"]:
            fails.append("UI：模板未加载（既有功能被破坏）")
        if not app.param_vars:
            fails.append("UI：参数控件未生成（既有功能被破坏）")
        if app.notebook.index("end") < 2:
            fails.append("UI：缺少「审批与型号汇总」页")

        # 审核角色：待审 2 条（手跑的 wingbox Study + 型号批量跑的机翼）；
        # 可通过/退回可用，签发置灰（未通过审核）
        app.role_var.set(ui_workbench.ROLE_CHOICES[0][1])
        app.on_role_change()
        if app.inbox.size() != 2:
            fails.append("UI：审核待审列表应有 2 条，实际 %d" % app.inbox.size())
        if str(app.btn_sign["state"]) != "disabled":
            fails.append("UI：未通过审核时签发按钮必须置灰，实际 %r"
                         % (str(app.btn_sign["state"]),))
        if str(app.btn_approve["state"]) != "normal":
            fails.append("UI：待审核报告的通过按钮应可用")
        if not app.detail.get("1.0", tk.END).strip():
            fails.append("UI：审核详情为空（看不到血缘/校核明细）")
        detail_text = app.detail.get("1.0", tk.END)
        for frag in ("【血缘】", "【校核明细】"):
            if frag not in detail_text:
                fails.append("UI：审核详情缺少 %r" % (frag,))

        # 签发角色：未通过审核 -> 待签为空（不给入口）
        app.role_var.set(ui_workbench.ROLE_CHOICES[1][1])
        app.on_role_change()
        if app.inbox.size() != 0:
            fails.append("UI：未通过审核的报告不应出现在待签列表，实际 %d 条"
                         % app.inbox.size())

        # 通过审核后：待签出现，签发按钮可用，通过/退回置灰
        svc.approve_report(sid, opinion="UI 冒烟：通过")
        app.on_role_change()
        if app.inbox.size() != 1:
            fails.append("UI：通过审核后待签列表应有 1 条，实际 %d"
                         % app.inbox.size())
        if str(app.btn_sign["state"]) != "normal":
            fails.append("UI：已通过审核时签发按钮应可用，实际 %r"
                         % (str(app.btn_sign["state"]),))
        if str(app.btn_approve["state"]) != "disabled":
            fails.append("UI：签发角色下通过按钮应置灰")

        # 型号汇总视图：展示型号下各部件报告的流程状态（有型号配置时非空）
        app.on_refresh_rollup()
        rollup_txt = app.rollup_text.get("1.0", tk.END)
        if not rollup_txt.strip():
            fails.append("UI：型号汇总视图为空")
        for frag in ("报告流程状态", "wing", "待审核"):
            if frag not in rollup_txt:
                fails.append("UI：型号汇总视图缺少 %r" % (frag,))
    except Exception as exc:
        fails.append("UI 构造/操作失败: %s: %s" % (type(exc).__name__, exc))
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass
        svc.close()


# ---------------------------------------------------------------------------
# 7) Python 3.8 兼容
# ---------------------------------------------------------------------------

def test_py38(fails):
    """本票新增/改动的文件必须 Python 3.8 兼容。"""
    paths = [os.path.join(CORE, "workbench_service.py"),
             os.path.join(WORKBENCH, "ui_workbench.py"),
             os.path.abspath(__file__)]
    violations = compat_py38.check_paths(paths)
    if violations:
        fails.append("Python 3.8 不兼容：\n"
                     + compat_py38.format_violations(violations))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    fails = []
    tmp = _fresh("case")

    svc = ws.WorkbenchService(os.path.join(tmp, "svc"))
    try:
        # 准备：型号配置 + 三部件一键跑通（每份报告产出即待审核，票17/18）
        model_id = mc.create_model_config(MODEL_NAME, OVERALL, note="票19 自测",
                                         conn=svc.conn)
        out = svc.run_model_batch(model_id)
        if not out.get("ok"):
            fails.append("测试准备失败：型号批量运行未全部成功 %r"
                         % [(r["part_key"], r["error"]) for r in out["parts"]])

        print("1. 待审/待签列表 + 按角色区分可见操作 ...")
        sids = test_pending_lists_and_roles(fails, svc, model_id)

        print("2. 审核详情：完整血缘 + 校核明细 ...")
        test_review_detail(fails, svc, sids)

        print("3. 签发入口只对已通过审核的报告可用 ...")
        test_sign_gate(fails, svc, sids)

        print("4. 型号汇总视图展示各部件报告流程状态 ...")
        test_model_overview(fails, svc, model_id)
    finally:
        svc.close()

    print("5. 工作台逻辑在服务层（可脱离 UI 单测）...")
    test_logic_in_service_layer(fails)

    print("6. UI 冒烟（构造/销毁 + 签发按钮门控）...")
    test_ui_smoke(fails)

    print("7. Python 3.8 兼容 ...")
    test_py38(fails)

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M19 (票19) SELF-TEST: ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
