# -*- coding: utf-8 -*-
"""
test_m17.py -- 票17 验收：型号级汇总视图（多部件批量运行与结论汇总）

验收点（对应 issue #17）：
  1. 可对型号配置下多个部件一次性编排运行（fake 适配器确定性回归）
  2. 汇总视图给出每部件：最大 FI、最小 MS、合格判定、流程状态
  3. 汇总取各部件**最新版本**的校核结论（改参重跑后自动更新）
  4. 单部件失败不影响其他部件，失败原因在汇总中可见
  5. 汇总结果可被工作台服务层查询（供后续 UI 使用）
  6. Python 3.8 兼容

策略：只断言**外部可观察行为**——库里的产物/版本/审批状态、汇总视图的字段值、
运行结果里的成败与原因；不断言内部调用顺序。本文件用自己的 `ProbeRunner` 包住
真实运行器，只记录"哪些部件被跑过"，不改变运行语义。

运行 (在 apps/workbench 下): python tests/test_m17.py
"""

import os
import sys
import json
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
sys.path.insert(0, CORE)

import db
import study
import approval
import compat_py38
import model_config as mc
import model_rollup as mr
import workbench_service as ws
from catia_driver import FakeCatiaDriver
from abaqus_runner import FakeAbaqusRunner


# 全机总体参数：**常规运输机量级**（票16 默认分解规则的适用范围；
# 见 .notes/p2/HANDOFF.md 的"遗留风险"：别用极端小/大型号）
OVERALL = {
    "span": 34.0,
    "fuselage_length": 30.0,
    "tail_volume": 0.9,
    "fuselage_diameter": 3.2,
    "sweep_deg": 25.0,
}

PARTS = ("wing", "tail", "fuselage")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _fresh(name):
    d = os.path.join(WORKBENCH, ".tmp_test", "m17", name)
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d)
    return d


def _near(a, b, tol=1e-9):
    if a is None or b is None:
        return a is b
    return abs(float(a) - float(b)) <= tol


def _check_versions(conn, study_id):
    """某 Study 的 check_result 全部版本，(version, params_dict) 列表（升序）。"""
    rows = conn.execute(
        "SELECT version, params_json FROM artifacts WHERE study_id=? "
        "AND kind='check_result' ORDER BY version", (study_id,)).fetchall()
    return [(int(r["version"]), json.loads(r["params_json"] or "{}")) for r in rows]


def _row(rollup_dict, part_key):
    for r in rollup_dict["parts"]:
        if r["part_key"] == part_key:
            return r
    return None


class BoomAbaqus(object):
    """求解适配器：一律抛错（模拟该部件的 Abaqus 求解崩溃）。"""

    def solve(self, inp_path, job_name, work_dir, step_name=None, analysis=None):
        raise RuntimeError("模拟求解器崩溃：求解进程异常退出")

    def extract(self, odb_path, out_dir, step_name=None):
        raise RuntimeError("模拟求解器崩溃：无 ODB 可抽取")


# ---------------------------------------------------------------------------
# 1) 可对型号配置下多个部件一次性编排运行
# ---------------------------------------------------------------------------

def test_batch_run(fails, conn, svc, tmp):
    """一键跑通三个部件（fake 适配器）：全链产物齐全、自动关联、结论可查。"""
    mid = mc.create_model_config("某型运输机-方案A", OVERALL, note="票17 自测",
                                 conn=conn)

    # 1) 运行前汇总：三个部件都"未运行"，没有结论（不编造数字）
    before = mr.rollup(conn, mid)
    if [r["part_key"] for r in before["parts"]] != list(PARTS):
        fails.append("运行前汇总的部件不对：%r"
                     % [r["part_key"] for r in before["parts"]])
    for row in before["parts"]:
        if row["run_status"] != mr.RUN_NOT_RUN:
            fails.append("运行前部件 %r 的 run_status 应为 not_run，实际 %r"
                         % (row["part_key"], row["run_status"]))
        if row["max_fi"] is not None or row["min_ms"] is not None:
            fails.append("运行前部件 %r 不应有 FI/MS 结论" % row["part_key"])
        if row["approval_status"] != mr.APPROVAL_NONE:
            fails.append("运行前部件 %r 不应有审批状态" % row["part_key"])
    if before["totals"]["all_pass"] is not False:
        fails.append("运行前整机判定不应为合格")

    # 2) 一次性编排运行全部部件（走服务层 = UI 的真实路径，fake 适配器）
    out = svc.run_model_batch(mid)
    if not out.get("ok"):
        fails.append("批量运行应全部成功，实际失败 %r 个：%r"
                     % (out.get("n_failed"),
                        [(r["part_key"], r["error"]) for r in out["parts"]]))
    if out["n_ok"] != 3 or out["n_failed"] != 0:
        fails.append("批量运行统计不对：n_ok=%r n_failed=%r"
                     % (out.get("n_ok"), out.get("n_failed")))
    if [r["part_key"] for r in out["parts"]] != list(PARTS):
        fails.append("批量运行未按型号部件顺序跑：%r"
                     % [r["part_key"] for r in out["parts"]])

    # 3) 每个部件都跑了**完整链**（建模→拓扑→FE→求解→抽取→校核→报告）
    need = ("parameter_set", "catpart", "topology", "mesh", "inp", "odb",
            "extraction", "check_rule", "check_result", "report")
    for rec in out["parts"]:
        key = rec["part_key"]
        if not rec["ok"]:
            continue
        for kind in need:
            if not (rec["artifacts"] or {}).get(kind):
                fails.append("部件 %r 缺少 %r 产物（跑的不是完整链）：%r"
                             % (key, kind, sorted((rec["artifacts"] or {}))))
        if rec["check"] is None or rec["check"].get("max_fi") is None:
            fails.append("部件 %r 的运行结果里没有校核汇总" % key)
        if not rec["study_id"]:
            fails.append("部件 %r 未产生 Study" % key)

    # 4) 每个部件跑完自动关联到型号配置（两处事实都写了）
    for rec in out["parts"]:
        key, sid = rec["part_key"], rec["study_id"]
        part = mc.get_part(conn, mid, key)
        if part["study_id"] != sid:
            fails.append("部件 %r 未自动关联到本次运行的 Study %r（实际 %r）"
                         % (key, sid, part["study_id"]))
        row = conn.execute("SELECT model_id, part_key FROM studies WHERE id=?",
                           (sid,)).fetchone()
        if row is None or row["model_id"] != mid or row["part_key"] != key:
            fails.append("Study %r 上的型号归属列不对" % sid)

    # 5) 批量运行在 L2 审计留痕（gate=model_batch_run，成/败都有记录）
    for rec in out["parts"]:
        dec = conn.execute(
            "SELECT * FROM decisions WHERE study_id=? AND gate=?",
            (rec["study_id"], mr.GATE_BATCH_RUN)).fetchone()
        if dec is None:
            fails.append("部件 %r 的批量运行未留 L2 审计" % rec["part_key"])
        elif dec["decision"] != mr.RUN_OK:
            fails.append("成功运行的审计 decision 不对：%r" % dec["decision"])

    # 6) 批量运行结果自带最新汇总（UI 拿到一次返回值就能刷新整页）
    if (out.get("rollup") or {}).get("model_id") != mid:
        fails.append("批量运行结果里没有带上型号级汇总")
    return mid


# ---------------------------------------------------------------------------
# 3) 流程状态覆盖：分析中 / 待审核 / 已通过 / 已退回 / 已签发
# ---------------------------------------------------------------------------

def test_approval_states(fails, conn, svc, model_id):
    """汇总里的"当前流程状态"就是审批状态机的现状（不复制、不猜测）。"""
    fus = _row(mr.rollup(conn, model_id), "fuselage")
    sid = fus["conclusion_study_id"]

    def _status_now():
        row = _row(mr.rollup(conn, model_id), "fuselage")
        return row["approval_status"], row["approval_status_name"]

    # 1) 初始：报告产出即待审核（服务层登记，票18）
    status, name = _status_now()
    if status != approval.STATUS_PENDING_REVIEW:
        fails.append("机身报告初始状态应为待审核，实际 %r" % (status,))
    if name != approval.STATUS_NAMES[approval.STATUS_PENDING_REVIEW]:
        fails.append("待审核的中文名不对：%r" % (name,))

    # 2) 退回（必须填意见）-> 汇总是"已退回"
    approval.reject(conn, sid, opinion="蒙皮开口边缘应力偏高，请重新布置开口位置")
    status, name = _status_now()
    if status != approval.STATUS_REJECTED:
        fails.append("退回后汇总状态应为已退回，实际 %r" % (status,))
    if name != approval.STATUS_NAMES[approval.STATUS_REJECTED]:
        fails.append("已退回的中文名不对：%r" % (name,))
    fus_row = _row(mr.rollup(conn, model_id), "fuselage")
    if fus_row["approval"] is None:
        fails.append("汇总里没有审批快照（UI 无法显示退回意见）")
    elif "开口位置" not in (fus_row["approval"].get("last_reason") or ""):
        fails.append("汇总的审批快照里查不到退回意见：%r"
                     % (fus_row["approval"].get("last_reason"),))

    # 3) 重新提交 -> 待审核；审核通过 -> 已通过审核
    approval.submit_for_review(conn, sid, rationale="已调整开口位置，重新提交")
    approval.approve(conn, sid, opinion="同意")
    status, name = _status_now()
    if status != approval.STATUS_APPROVED:
        fails.append("通过后汇总状态应为已通过审核，实际 %r" % (status,))
    if name != approval.STATUS_NAMES[approval.STATUS_APPROVED]:
        fails.append("已通过审核的中文名不对：%r" % (name,))

    # 4) 签发 -> 已签发（最终版）
    approval.sign(conn, sid, opinion="批准发布")
    status, name = _status_now()
    if status != approval.STATUS_SIGNED:
        fails.append("签发后汇总状态应为已签发，实际 %r" % (status,))
    if name != approval.STATUS_NAMES[approval.STATUS_SIGNED]:
        fails.append("已签发的中文名不对：%r" % (name,))
    if not (_row(mr.rollup(conn, model_id), "fuselage")["approval"] or {}).get("final"):
        fails.append("汇总的审批快照未标记为最终版")

    # 5) 型号总计的流程状态计数跟着走（已签发 1 个，待审核 2 个）
    totals = mr.rollup(conn, model_id)["totals"]
    if totals["by_approval_status"].get(approval.STATUS_SIGNED) != 1:
        fails.append("型号总计未统计到 1 个已签发：%r"
                     % (totals["by_approval_status"],))
    if totals["by_approval_status"].get(approval.STATUS_PENDING_REVIEW) != 2:
        fails.append("型号总计未统计到 2 个待审核：%r"
                     % (totals["by_approval_status"],))
    if "已签发" not in mr.format_rollup_text(mr.rollup(conn, model_id)):
        fails.append("汇总文本未显示已签发状态")

    # 6) 硬合规点：已签发的旧版绝不继承到重跑出的新版本上
    part = mc.get_part(conn, model_id, "fuselage")
    svc.run_chain(sid, part["params"], part["template_id"])
    status, _name = _status_now()
    if status != approval.STATUS_PENDING_REVIEW:
        fails.append("重跑产生新版报告后汇总应重置为待审核（已签发的旧版绝不继承），"
                     "实际 %r" % (status,))
    row = _row(mr.rollup(conn, model_id), "fuselage")
    if row["approval_version"] != 2:
        fails.append("重跑后流程状态应针对报告 v2，实际 v%r"
                     % (row["approval_version"],))


# ---------------------------------------------------------------------------
# 2) 汇总视图给出每部件：最大 FI、最小 MS、合格判定、流程状态
# ---------------------------------------------------------------------------

def test_rollup_fields(fails, conn, model_id):
    """汇总每部件的 FI / MS / 合格判定 / 流程状态都来自真实产物，可交叉核对。"""
    view = mr.rollup(conn, model_id)
    if view["model_id"] != model_id:
        fails.append("汇总的 model_id 不对")
    if len(view["parts"]) != 3:
        fails.append("汇总的部件数应为 3，实际 %d" % len(view["parts"]))

    for row in view["parts"]:
        key = row["part_key"]
        # 1) 每部件四项必有：最大 FI / 最小 MS / 合格判定 / 流程状态
        for field in ("max_fi", "min_ms", "pass", "approval_status",
                      "approval_status_name"):
            if field not in row:
                fails.append("部件 %r 的汇总缺少字段 %r" % (key, field))
        if row["max_fi"] is None or row["min_ms"] is None:
            fails.append("部件 %r 没有 FI/MS 结论" % key)
            continue
        if not isinstance(row["max_fi"], float):
            fails.append("部件 %r 的最大 FI 不是数值：%r" % (key, row["max_fi"]))
        if not isinstance(row["pass"], bool):
            fails.append("部件 %r 的合格判定不是布尔：%r" % (key, row["pass"]))
        # 2) 与数据库里的 check_result 产物逐字段一致（汇总不是另算一套）
        params = _check_versions(conn, row["conclusion_study_id"])[-1][1]
        if not _near(row["max_fi"], params["max_fi"]):
            fails.append("部件 %r 的最大 FI 与产物不一致：%r != %r"
                         % (key, row["max_fi"], params["max_fi"]))
        if not _near(row["min_ms"], params["min_ms"]):
            fails.append("部件 %r 的最小 MS 与产物不一致：%r != %r"
                         % (key, row["min_ms"], params["min_ms"]))
        if row["pass"] != bool(params["all_pass"]):
            fails.append("部件 %r 的合格判定与产物不一致：%r != %r"
                         % (key, row["pass"], params["all_pass"]))
        # 3) 流程状态来自 approval.get_state（票18 的状态机，不是自己编的状态）
        st = approval.get_state(conn, row["conclusion_study_id"])
        if st is None:
            fails.append("部件 %r 的报告未登记审批（应有 pending_review）" % key)
        elif row["approval_status"] != st["status"]:
            fails.append("部件 %r 的流程状态与审批模块不一致：%r != %r"
                         % (key, row["approval_status"], st["status"]))
        elif row["approval_status_name"] != st["status_name"]:
            fails.append("部件 %r 的流程状态中文名不对：%r"
                         % (key, row["approval_status_name"]))
        if row["approval_status"] != approval.STATUS_PENDING_REVIEW:
            fails.append("部件 %r 首次产报告后应为待审核，实际 %r"
                         % (key, row["approval_status"]))

    # 4) 型号级总计：整机判定取最弱环节（最差 FI / 最差 MS）
    totals = view["totals"]
    if totals["n_parts"] != 3 or totals["n_with_conclusion"] != 3:
        fails.append("型号总计的部件数/有结论数不对：%r" % (totals,))
    expect_fi = max(r["max_fi"] for r in view["parts"])
    expect_ms = min(r["min_ms"] for r in view["parts"])
    if not _near(totals["max_fi"], expect_fi):
        fails.append("型号级最大 FI 应取各部件最差：%r != %r"
                     % (totals["max_fi"], expect_fi))
    if not _near(totals["min_ms"], expect_ms):
        fails.append("型号级最小 MS 应取各部件最差：%r != %r"
                     % (totals["min_ms"], expect_ms))
    if totals["all_pass"] != all(r["pass"] for r in view["parts"]):
        fails.append("型号级合格判定与各部件不一致")
    if totals["by_approval_status"].get(approval.STATUS_PENDING_REVIEW) != 3:
        fails.append("型号总计的流程状态计数不对：%r"
                     % (totals["by_approval_status"],))

    # 5) 汇总可渲染成可读文本（UI 列表直接可用）
    text = mr.format_rollup_text(view)
    for key in PARTS:
        if key not in text:
            fails.append("汇总文本里没有部件 %r" % key)
    if "最大FI" not in text:
        fails.append("汇总文本缺少表头")


# ---------------------------------------------------------------------------
# 3) 汇总取最新版本：同 Study 重跑产生新版本 -> 汇总自动更新
# ---------------------------------------------------------------------------

def test_latest_version_in_study(fails, conn, svc, model_id):
    """在同一个 Study 里改规则重跑 -> check_result 出新版本 -> 汇总取新版本。"""
    wing = mc.get_part(conn, model_id, "wing")
    sid = wing["study_id"]
    v_before = _check_versions(conn, sid)
    if len(v_before) != 1:
        fails.append("机翼 Study 应已有 1 版校核结论，实际 %r" % (len(v_before),))
        return
    row_before = _row(mr.rollup(conn, model_id), "wing")

    # 改安全系数重跑（同一 Study -> 新版本产物，模拟"改参重跑"）
    svc.run_chain(sid, wing["params"], wing["template_id"],
                  rules={"criteria": ["tsai_wu", "hashin"],
                         "safety_factor": 2.0})
    v_after = _check_versions(conn, sid)
    if len(v_after) != 2:
        fails.append("同 Study 重跑后应产生 2 版校核结论，实际 %r"
                     % (len(v_after),))
        return

    row_after = _row(mr.rollup(conn, model_id), "wing")
    v1, v2 = v_after[0][1], v_after[1][1]

    # 1) 汇总取的是**最新版本**（v2），不是 v1
    if row_after["conclusion"]["check_version"] != 2:
        fails.append("汇总未取最新版校核结论：%r"
                     % (row_after["conclusion"]["check_version"],))
    if not _near(row_after["max_fi"], v2["max_fi"]):
        fails.append("汇总的最大 FI 应等于 v2：%r != %r"
                     % (row_after["max_fi"], v2["max_fi"]))
    if not _near(row_after["min_ms"], v2["min_ms"]):
        fails.append("汇总的最小 MS 应等于 v2：%r != %r"
                     % (row_after["min_ms"], v2["min_ms"]))
    # 2) 而且确实**变了**（不是恰好相同——否则"取最新"无从验证）
    if _near(v2["max_fi"], v1["max_fi"]) and _near(v2["min_ms"], v1["min_ms"]):
        fails.append("重跑后校核结论没有变化，无法验证'取最新版本'")
    if _near(row_after["max_fi"], row_before["max_fi"]):
        fails.append("改规则重跑后汇总的最大 FI 未更新：%r"
                     % (row_after["max_fi"],))
    # 3) 流程状态跟着新版本走（新版本报告 = pending_review v2，不继承旧审批）
    if row_after["approval_version"] != 2:
        fails.append("流程状态应针对报告 v2，实际 v%r" % (row_after["approval_version"],))
    if row_after["approval_status"] != approval.STATUS_PENDING_REVIEW:
        fails.append("重跑后新版本报告应重置为待审核，实际 %r"
                     % (row_after["approval_status"],))
    # 4) 其它部件不受影响
    for key in ("tail", "fuselage"):
        row = _row(mr.rollup(conn, model_id), key)
        if row["conclusion"]["check_version"] != 1:
            fails.append("未重跑的部件 %r 的结论版本被改了：%r"
                         % (key, row["conclusion"]["check_version"]))


# ---------------------------------------------------------------------------
# 4) 改参重跑（新 Study）-> 汇总自动反映最新结论
# ---------------------------------------------------------------------------

def test_latest_across_studies(fails, conn, model_id, tmp):
    """只重跑机翼且换了适配器口径 -> 新 Study 的结论成为汇总里的最新结论。"""
    before = mr.rollup(conn, model_id)
    wing_before = _row(before, "wing")
    tail_before = _row(before, "tail")
    old_study = wing_before["study_id"]

    # 用"无热点"的求解适配器重跑机翼：结论显然不同（合格 vs 不合格）
    work = os.path.join(tmp, "direct")
    out = mr.run_model_batch(
        conn, model_id, parts=["wing"], work_root=work,
        adapters={"abaqus": FakeAbaqusRunner(n_elems=6, n_plies=2,
                                             inject_hotspot=False)})
    if out["n_ok"] != 1 or out["n_failed"] != 0:
        fails.append("单部件重跑应成功：%r"
                     % [(r["part_key"], r["error"]) for r in out["parts"]])
        return

    new_study = out["parts"][0]["study_id"]
    if new_study == old_study:
        fails.append("改参重跑应新建 Study（改参重跑 = 新 Study，血缘不断）")

    after = mr.rollup(conn, model_id)
    wing = _row(after, "wing")

    # 1) 部件的当前 Study 指向最新一次运行
    if wing["study_id"] != new_study:
        fails.append("重跑后部件未指向新 Study：%r != %r"
                     % (wing["study_id"], new_study))
    if mc.get_part(conn, model_id, "wing")["study_id"] != new_study:
        fails.append("重跑后型号配置的部件未自动改挂新 Study")
    if wing["n_studies"] != 2:
        fails.append("机翼在型号下应有 2 个 Study（历史 + 最新），实际 %r"
                     % (wing["n_studies"],))

    # 2) 结论自动换成最新那次（新版结论显然不同）
    if wing["conclusion_study_id"] != new_study:
        fails.append("汇总的结论未取最新 Study：%r != %r"
                     % (wing["conclusion_study_id"], new_study))
    if _near(wing["max_fi"], wing_before["max_fi"]):
        fails.append("重跑后汇总的最大 FI 未更新：%r" % (wing["max_fi"],))
    if wing["max_fi"] >= wing_before["max_fi"]:
        fails.append("换'无热点'口径后最大 FI 应变小：%r -> %r"
                     % (wing_before["max_fi"], wing["max_fi"]))
    if wing["pass"] is not True:
        fails.append("无热点口径下机翼应判合格，实际 pass=%r" % (wing["pass"],))
    if wing["approval_status"] != approval.STATUS_PENDING_REVIEW:
        fails.append("新 Study 的报告应为待审核，实际 %r" % (wing["approval_status"],))

    # 3) 未重跑的部件结论原样不动
    tail = _row(after, "tail")
    if tail["conclusion_study_id"] != tail_before["conclusion_study_id"]:
        fails.append("未重跑的尾翼结论 Study 被改了")
    if not _near(tail["max_fi"], tail_before["max_fi"]):
        fails.append("未重跑的尾翼 FI 被改了：%r -> %r"
                     % (tail_before["max_fi"], tail["max_fi"]))

    # 4) 型号总计随最新结论更新（整机最差 FI 现在来自别处）
    if _near(after["totals"]["max_fi"], before["totals"]["max_fi"]):
        fails.append("型号级最差 FI 未随部件结论更新")
    return new_study


# ---------------------------------------------------------------------------
# 5) 单部件失败不影响其他部件，失败原因在汇总中可见
# ---------------------------------------------------------------------------

def test_failure_isolation(fails, conn, model_id, tmp):
    """一个部件失败（非法参数 / 适配器抛错），其他部件照跑完，原因可见。"""
    before = mr.rollup(conn, model_id)
    wing_before = _row(before, "wing")
    fus_before = _row(before, "fuselage")

    # ---- 5.1 非法参数：机翼照跑成功，机身因参数越界失败（同一批次内） ----
    out = mr.run_model_batch(
        conn, model_id, parts=["wing", "fuselage"], work_root=os.path.join(tmp, "fail1"),
        params_overrides={"fuselage": {"n_frames": 1}})   # 模板下限 2
    recs = dict((r["part_key"], r) for r in out["parts"])
    if not recs["wing"]["ok"]:
        fails.append("机身参数非法不应影响机翼：机翼也失败了（%r）"
                     % (recs["wing"]["error"],))
    if recs["fuselage"]["ok"]:
        fails.append("机身框数低于下限应失败，实际成功")
    if not recs["fuselage"]["error"]:
        fails.append("失败部件的运行结果没有失败原因")
    elif "n_frames" not in recs["fuselage"]["error"]:
        fails.append("失败原因未说清是哪个参数：%r" % (recs["fuselage"]["error"],))
    if out["n_ok"] != 1 or out["n_failed"] != 1:
        fails.append("批量运行统计不对：n_ok=%r n_failed=%r"
                     % (out["n_ok"], out["n_failed"]))

    # 失败原因在汇总视图里可见，且上次成功的结论没被抹掉
    fus = _row(mr.rollup(conn, model_id), "fuselage")
    if fus["run_status"] != mr.RUN_FAILED:
        fails.append("汇总未标出机身最近一次运行失败：%r" % (fus["run_status"],))
    if not fus["error"] or "n_frames" not in fus["error"]:
        fails.append("汇总里看不到机身的失败原因：%r" % (fus["error"],))
    if fus["run_status_name"] != mr.RUN_STATUS_NAMES[mr.RUN_FAILED]:
        fails.append("汇总的失败状态中文名不对：%r" % (fus["run_status_name"],))
    if fus["max_fi"] is None:
        fails.append("一次失败的尝试不应抹掉上一次成功的校核结论")
    if fus["conclusion_study_id"] != fus_before["conclusion_study_id"]:
        fails.append("失败尝试后汇总结论 Study 变成了失败那次：%r"
                     % (fus["conclusion_study_id"],))
    # 失败尝试同样留在型号血缘里（它就是"最近一次尝试"的记录）
    if fus["study_id"] and fus["study_id"] == fus_before["study_id"]:
        fails.append("失败尝试应新建 Study 记录，不应复用上次成功的 Study")
    # 失败也写 L2 审计
    if fus["study_id"]:
        dec = conn.execute(
            "SELECT * FROM decisions WHERE study_id=? AND gate=?",
            (fus["study_id"], mr.GATE_BATCH_RUN)).fetchone()
        if dec is None or dec["decision"] != mr.RUN_FAILED:
            fails.append("失败的批量运行未在 L2 审计留痕")
    # 机翼在这次批量里成功 -> 汇总显示可运行、有结论
    wing = _row(mr.rollup(conn, model_id), "wing")
    if wing["run_status"] != mr.RUN_OK:
        fails.append("同批次成功的机翼在汇总里应为已运行，实际 %r"
                     % (wing["run_status"],))
    if wing["max_fi"] is None:
        fails.append("同批次成功的机翼没有结论")

    # ---- 5.2 适配器抛错：尾翼的 CATIA 再生失败，机翼照常成功 ----
    work2 = os.path.join(tmp, "fail2")
    out2 = mr.run_model_batch(
        conn, model_id, parts=["wing", "tail"], work_root=work2,
        adapters={
            "default": {},
            "wing": {"abaqus": FakeAbaqusRunner(n_elems=6, n_plies=2)},
            "tail": {"catia": lambda sid: FakeCatiaDriver(
                os.path.join(work2, "catparts", "tail"), fail_on=["n_ribs"])},
        })
    recs2 = dict((r["part_key"], r) for r in out2["parts"])
    if not recs2["wing"]["ok"]:
        fails.append("尾翼 CATIA 失败不应影响机翼：机翼也失败了（%r）"
                     % (recs2["wing"]["error"],))
    if recs2["tail"]["ok"]:
        fails.append("尾翼 CATIA 抛错应判失败，实际成功")
    if "CATIA" not in (recs2["tail"]["error"] or ""):
        fails.append("尾翼的失败原因未说明是 CATIA 失败：%r"
                     % (recs2["tail"]["error"],))
    tail = _row(mr.rollup(conn, model_id), "tail")
    if tail["run_status"] != mr.RUN_FAILED:
        fails.append("汇总未标出尾翼最近一次运行失败")
    if "CATIA" not in (tail["error"] or ""):
        fails.append("汇总里看不到尾翼的 CATIA 失败原因：%r" % (tail["error"],))
    # 可读文本同样把失败与原因摆出来（UI 列表/日志直接可读）
    text = mr.format_rollup_text(mr.rollup(conn, model_id))
    if "最近一次运行失败" not in text:
        fails.append("汇总文本未标出最近一次运行失败")
    if "CATIA" not in text:
        fails.append("汇总文本未显示失败原因")

    # ---- 5.3 求解适配器抛错：整条链失败但原因可读，其它部件不受影响 ----
    work3 = os.path.join(tmp, "fail3")
    out3 = mr.run_model_batch(
        conn, model_id, parts=["tail"], work_root=work3,
        adapters={"abaqus": BoomAbaqus()})
    rec3 = out3["parts"][0]
    if rec3["ok"]:
        fails.append("求解适配器抛错应判失败，实际成功")
    if "求解" not in (rec3["error"] or ""):
        fails.append("求解失败的根因没说清：%r" % (rec3["error"],))
    view3 = mr.rollup(conn, model_id)
    if _row(view3, "tail")["conclusion"] is None:
        fails.append("求解失败不应抹掉尾翼上次成功的结论")
    # 其它部件（机翼/机身）的结论一直在，不受任何失败影响
    for key in ("wing", "fuselage"):
        row = _row(view3, key)
        if row["max_fi"] is None:
            fails.append("其它部件 %r 的结论被失败连累了" % key)

    # ---- 5.4 stop_on_error=True 时遇首个失败就停（默认 False 才是不互相影响） ----
    work4 = os.path.join(tmp, "fail4")
    out4 = mr.run_model_batch(
        conn, model_id, work_root=work4, stop_on_error=True,
        adapters={"abaqus": BoomAbaqus()})
    if len(out4["parts"]) != 1:
        fails.append("stop_on_error=True 应在首个失败后停止，实际跑了 %d 个部件"
                     % len(out4["parts"]))
    if out4["ok"] or out4["n_failed"] != 1:
        fails.append("stop_on_error=True 的首个部件应失败：%r" % (out4,))
    # 默认（stop_on_error=False）则三个部件全都会被尝试
    if len(mr.run_model_batch(conn, model_id,
                              work_root=os.path.join(tmp, "fail5"),
                              adapters={"abaqus": BoomAbaqus()})["parts"]) != 3:
        fails.append("默认应尝试全部部件（单点失败不影响其他部件）")


# ---------------------------------------------------------------------------
# 6) 汇总结果可被工作台服务层查询
# ---------------------------------------------------------------------------

def test_service_layer(fails, conn, svc, model_id):
    """服务层暴露薄封装：整机汇总 / 单部件汇总 / 可读文本；错误可读。"""
    view = svc.get_model_rollup(model_id)
    if view["model_id"] != model_id:
        fails.append("服务层 get_model_rollup 的 model_id 不对")
    if [r["part_key"] for r in view["parts"]] != list(PARTS):
        fails.append("服务层汇总的部件不全：%r"
                     % [r["part_key"] for r in view["parts"]])
    # 服务层与算法层给的是同一份事实（不会各算一套）
    direct = mr.rollup(conn, model_id)
    for a, b in zip(view["parts"], direct["parts"]):
        for field in ("part_key", "max_fi", "min_ms", "pass",
                      "approval_status", "run_status", "error"):
            if a.get(field) != b.get(field):
                fails.append("服务层与算法层对部件 %r 的 %r 不一致：%r != %r"
                             % (a["part_key"], field, a.get(field), b.get(field)))

    one = svc.get_part_rollup(model_id, "tail")
    if one is None or one["part_key"] != "tail":
        fails.append("服务层 get_part_rollup 未取到尾翼")
    if svc.get_part_rollup(model_id, "bogus") is not None:
        fails.append("不存在的部件应返回 None")

    text = svc.format_model_rollup(model_id)
    if "型号配置" not in text or "最大FI" not in text:
        fails.append("服务层的可读汇总文本格式不对")

    # 型号级汇总也能直接用于 UI：每部件一行，四项关键值齐全
    for row in view["parts"]:
        for field in ("max_fi", "min_ms", "pass", "approval_status_name"):
            if field not in row:
                fails.append("服务层汇总行缺少 UI 需要的字段 %r" % field)

    # 错误情况都可读（ServiceError 而不是底层裸异常）
    try:
        svc.get_model_rollup("MODEL-NOT-EXIST")
    except ws.ServiceError as exc:
        if "不存在" not in str(exc):
            fails.append("查不存在型号的错误信息不可读：%r" % (str(exc),))
    except Exception as exc:
        fails.append("查不存在型号应抛 ServiceError，实际 %s: %s"
                     % (type(exc).__name__, exc))
    else:
        fails.append("查不存在的型号配置应报错，实际没有报错")


def test_batch_errors(fails, conn, model_id):
    """批量运行的输入错误要可读，且不产生半成品。"""
    def _expect(fn, must_contain, label):
        try:
            fn()
        except (mr.RollupError, mc.ModelConfigError) as exc:
            text = str(exc)
            for frag in must_contain:
                if frag not in text:
                    fails.append("%s：错误消息缺少 %r（实际：%s）"
                                 % (label, frag, text))
        except Exception as exc:
            fails.append("%s：期望 RollupError/ModelConfigError，实际 %s: %s"
                         % (label, type(exc).__name__, exc))
        else:
            fails.append("%s：期望报错，实际没有报" % label)

    n_before = len(mc.list_model_configs(conn))
    sids_before = conn.execute(
        "SELECT COUNT(*) c FROM studies WHERE model_id=?",
        (model_id,)).fetchone()["c"]
    _expect(lambda: mr.run_model_batch(conn, "MODEL-NOT-EXIST"),
            ("不存在",), "不存在的型号配置")
    _expect(lambda: mr.run_model_batch(conn, model_id, parts=["bogus"]),
            ("bogus",), "不存在的部件键")
    if len(mc.list_model_configs(conn)) != n_before:
        fails.append("批量运行报错后型号配置数量变了")
    sids_after = conn.execute(
        "SELECT COUNT(*) c FROM studies WHERE model_id=?",
        (model_id,)).fetchone()["c"]
    if sids_after != sids_before:
        fails.append("批量运行在参数校验失败前就建了 Study（应无半成品）")


# ---------------------------------------------------------------------------
# 7) Python 3.8 兼容
# ---------------------------------------------------------------------------

def test_py38(fails):
    """本票新增/改动的文件必须 Python 3.8 兼容。"""
    paths = [os.path.join(CORE, "model_rollup.py"),
             os.path.join(CORE, "workbench_service.py"),
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

    svc = ws.WorkbenchService(os.path.join(tmp, "svc"))
    conn = svc.conn
    try:
        print("1. 多部件一次性编排运行（完整链 + 自动关联）...")
        model_id = test_batch_run(fails, conn, svc, tmp)

        print("2. 汇总每部件的最大 FI / 最小 MS / 合格判定 / 流程状态 ...")
        test_rollup_fields(fails, conn, model_id)

        print("3. 汇总取最新版本（同 Study 重跑产生新版本）...")
        test_latest_version_in_study(fails, conn, svc, model_id)

        print("4. 改参重跑（新 Study）后汇总自动更新 ...")
        test_latest_across_studies(fails, conn, model_id, tmp)

        print("5. 单部件失败不影响其他部件，失败原因可见 ...")
        test_failure_isolation(fails, conn, model_id, tmp)

        print("6. 汇总可被工作台服务层查询 ...")
        test_service_layer(fails, conn, svc, model_id)

        print("7. 流程状态覆盖：分析中/待审核/已通过/已退回/已签发 ...")
        test_approval_states(fails, conn, svc, model_id)

        print("8. 批量运行的输入错误可读、不产生半成品 ...")
        test_batch_errors(fails, conn, model_id)
    finally:
        conn.close()
        svc.close()

    print("9. Python 3.8 兼容 ...")
    test_py38(fails)

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M17 (票17) SELF-TEST: ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
