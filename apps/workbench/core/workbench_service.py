# -*- coding: utf-8 -*-
"""
workbench_service.py -- 工作台应用服务层（票 08）

UI 与 run_study 之间的**可测试中间层**：所有编排逻辑都在这，
Tkinter 视图只负责调它 + 显示。

这样设计的原因：UI 控件难以自动化测试，而服务层是纯 Python，
可以用与 m0-m7 相同的方式完整单测。

兼容 Python 3.8（Win7 目标机）。
"""

import os
import json
import datetime

try:
    from . import db as db_mod
    from . import study as study_mod
    from . import template_registry
    from . import run_study as rs_mod
    from . import report_template
    from . import approval
    from . import model_config as model_config_mod
    from . import model_rollup as rollup_mod
    from . import textfmt
    from .catia_driver import FakeCatiaDriver, RealCatiaDriver
    from .abaqus_runner import FakeAbaqusRunner, RealAbaqusRunner
except ImportError:
    import db as db_mod
    import study as study_mod
    import template_registry
    import run_study as rs_mod
    import report_template
    import approval
    import model_config as model_config_mod
    import model_rollup as rollup_mod
    import textfmt
    from catia_driver import FakeCatiaDriver, RealCatiaDriver
    from abaqus_runner import FakeAbaqusRunner, RealAbaqusRunner


class ServiceError(Exception):
    pass


class WorkbenchService(object):
    """
    工作台服务。持有数据库连接与工作目录。

    mode: "fake"（默认，无需 CATIA/Abaqus）或 "real"
    """

    def __init__(self, work_root, db_path=None, mode="fake"):
        self.work_root = work_root
        if not os.path.isdir(work_root):
            os.makedirs(work_root)
        self.db_path = db_path or os.path.join(work_root, "cae_platform.db")
        self.mode = mode
        self._conn = None

    # ---------------- 基础设施 ----------------

    @property
    def conn(self):
        if self._conn is None:
            self._conn = db_mod.connect(self.db_path)
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def _dir(self, study_id, sub):
        d = os.path.join(self.work_root, "studies", study_id, sub)
        if not os.path.isdir(d):
            os.makedirs(d)
        return d

    # ---------------- 模板与 Study ----------------

    def list_templates(self, templates_dir=None):
        return template_registry.list_templates(templates_dir)

    def get_template(self, template_id, templates_dir=None):
        return template_registry.load(template_id, templates_dir)

    def new_study_id(self, prefix="STUDY"):
        return "%s-%s" % (prefix, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))

    def start_study(self, name, study_id=None):
        sid = study_id or self.new_study_id()
        try:
            study_mod.create_study(sid, name, conn=self.conn)
        except Exception as exc:
            # 已存在则复用
            if "UNIQUE" not in str(exc).upper() and "PRIMARY" not in str(exc).upper():
                raise ServiceError("创建 Study 失败: %s" % exc)
        return sid

    def list_studies(self):
        rows = self.conn.execute(
            "SELECT s.id, s.name, s.created_at, "
            "(SELECT COUNT(*) FROM artifacts a WHERE a.study_id = s.id) AS n "
            "FROM studies s ORDER BY s.created_at").fetchall()
        return [{"id": r["id"], "name": r["name"],
                 "created_at": r["created_at"], "n_artifacts": r["n"]}
                for r in rows]

    # ---------------- 适配器 ----------------

    def _catia(self, study_id):
        if self.mode == "real":
            return RealCatiaDriver(self._dir(study_id, "catparts"))
        return FakeCatiaDriver(self._dir(study_id, "catparts"))

    def _abaqus(self):
        if self.mode == "real":
            return RealAbaqusRunner()
        return FakeAbaqusRunner(n_elems=6, n_plies=2)

    # ---------------- 主链 ----------------

    def run_chain(self, study_id, parameter_set, template_id,
                  plies=None, rules=None, load=None,
                  subdiv_per_bay=1, with_report=True, options=None):
        """
        跑完整 P1 链：参数 -> 数模 -> 拓扑 -> FE -> 求解 -> 抽取 -> 校核 -> 报告。
        返回 run_study 的结果 dict（含 artifacts / check / report）。

        options: RunOptions（票10）。服务层先按工作台目录约定填好各阶段目录与开关，
                 再用调用方传入的 options 覆盖——于是新增能力（分析类型、网格策略、
                 型号配置等）只需传 options，不必改本方法签名。
        """
        plies = plies or [{"angle": 0.0, "thickness": 0.1},
                          {"angle": 90.0, "thickness": 0.1}]
        rules = rules or {"criteria": ["tsai_wu", "hashin"]}

        # 各阶段目录与开关集中在 RunOptions.full_chain（加新阶段只改那一处）
        base = rs_mod.RunOptions.full_chain(
            lambda sub: self._dir(study_id, sub),
            plies=plies, rules=rules, load=load,
            subdiv_per_bay=subdiv_per_bay, with_report=with_report,
            abaqus=self._abaqus())

        # 运行开始前先把该 Study 标成「分析中」（票18 状态机起点 / spec 用户故事 26）。
        # 这样项目负责人在型号汇总里看到的是"分析中"，而不是"未提交审批"
        # ——后者会让人以为这份报告已经跑完且在等人处理。
        analysing_version = self._begin_analysis(study_id)

        try:
            result = rs_mod.run_study(
                study_id, parameter_set, template_id,
                self._catia(study_id), self.conn, base.overlay(options))
        except Exception as exc:
            # 运行失败：把「分析中」标记撤掉，避免留下一个永远停在分析中的幽灵版本
            self._abort_analysis(study_id, analysing_version, str(exc))
            raise ServiceError(str(exc))

        # 报告产出后登记审批（票18）：**新版本一律从 pending_review 开始**，
        # 绝不继承上一版的审批结果。不改 run_study 签名，由服务层后置登记。
        rep_id = (result.get("artifacts") or {}).get("report")
        if rep_id:
            st = self._register_report_approval(study_id, rep_id, result,
                                               analysing_version)
            if st is not None:
                result["approval"] = st
                # 回填审计用：报告版本号便于 UI 显示"待审核 v2"
                result["report"]["version"] = st["version"]
        else:
            # 没出报告（with_report=False）：分析已完成，撤掉「分析中」
            self._abort_analysis(study_id, analysing_version,
                                 "本次运行未生成报告（with_report=False）")
        return result

    def _begin_analysis(self, study_id):
        """
        登记"分析中"；返回占位版本号（报告尚未产出，故用下一版版本号）。

        返回 None 表示登记失败（不影响主流程，审批是管理维度信息）。
        """
        try:
            next_ver = self._next_report_version(study_id)
            row = self.conn.execute(
                "SELECT id FROM artifacts WHERE study_id=? AND kind='report' "
                "ORDER BY version DESC LIMIT 1", (study_id,)).fetchone()
            artifact_id = row["id"] if row is not None else None
            approval.ensure_registered(self.conn, study_id, artifact_id,
                                       next_ver, status=approval.STATUS_ANALYZING)
            return next_ver
        except Exception:
            return None

    def _abort_analysis(self, study_id, version, reason):
        """
        运行失败时删除「分析中」占位记录。

        必须真的删掉而不是改成别的状态：一个停在"分析中"的版本会让汇总视图
        长期显示错误的进度，而它对应的报告从来不存在。
        """
        if version is None:
            return
        try:
            self.conn.execute(
                "DELETE FROM report_approvals WHERE study_id=? AND version=? "
                "AND status=?", (study_id, version,
                                 approval.STATUS_ANALYZING))
            self.conn.commit()
        except Exception:
            pass

    def _next_report_version(self, study_id):
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM artifacts "
            "WHERE study_id=? AND kind='report'", (study_id,)).fetchone()
        v = int(row["v"]) + 1
        # 「分析中」占位已经用过该版本号时让位（同一 Study 连续两次运行）
        row2 = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM report_approvals "
            "WHERE study_id=?", (study_id,)).fetchone()
        return max(v, int(row2["v"]) + 1)

    def _register_report_approval(self, study_id, report_artifact_id, result=None,
                                  analysing_version=None):
        """把一版报告产物登记进审批流（新版本 -> pending_review）。"""
        row = self.conn.execute(
            "SELECT version FROM artifacts WHERE id=?",
            (report_artifact_id,)).fetchone()
        if row is None:
            return None
        version = int(row["version"])
        try:
            # 「分析中」占位若与真实报告版本同号，先删占位再登记真实状态，
            # 否则 ensure_registered 会幂等返回占位行，报告永远停在"分析中"。
            self.conn.execute(
                "DELETE FROM report_approvals WHERE study_id=? AND version=? "
                "AND status=?", (study_id, version,
                                 approval.STATUS_ANALYZING))
            self.conn.commit()
            return approval.ensure_registered(
                self.conn, study_id, report_artifact_id, version)
        except approval.ApprovalError as exc:
            # 审批登记失败不应吞掉报告本身：记进 warnings，服务层仍返回产物
            if result is not None:
                result.setdefault("warnings", []).append(
                    "报告审批状态登记失败: %s" % exc)
            return None

    # ---------------- 血缘 ----------------

    def get_lineage(self, study_id):
        """
        返回血缘结构（供 UI 树展示）：
        {
          "study_id":..., "nodes":[{id,kind,version,checksum,content_uri,parents,children}],
          "order":[ids in topological order], "decisions":[...]
        }
        """
        conn = self.conn
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE study_id=? ORDER BY id", (study_id,)).fetchall()
        if not rows:
            return {"study_id": study_id, "nodes": [], "order": [], "decisions": []}

        by_id = {}
        for r in rows:
            by_id[r["id"]] = {
                "id": r["id"], "kind": r["kind"], "version": r["version"],
                "checksum": r["checksum"], "content_uri": r["content_uri"],
                "status": r["status"], "created_at": r["created_at"],
                "parents": [], "children": [],
            }

        for e in conn.execute("SELECT parent_id, child_id FROM edges").fetchall():
            p, c = e["parent_id"], e["child_id"]
            if p in by_id and c in by_id:
                by_id[c]["parents"].append(p)
                by_id[p]["children"].append(c)

        # 拓扑排序
        indeg = dict((i, len(by_id[i]["parents"])) for i in by_id)
        ready = sorted([i for i in by_id if indeg[i] == 0])
        order = []
        while ready:
            n = ready.pop(0)
            order.append(n)
            for c in by_id[n]["children"]:
                indeg[c] -= 1
                if indeg[c] == 0:
                    ready.append(c)
            ready.sort()
        if len(order) != len(by_id):
            order = sorted(by_id)

        decisions = []
        for d in conn.execute(
                "SELECT * FROM decisions WHERE study_id=? ORDER BY id",
                (study_id,)).fetchall():
            decisions.append({"gate": d["gate"], "actor": d["actor"],
                              "decision": d["decision"],
                              "rationale": d["rationale"],
                              "created_at": d["created_at"]})

        return {"study_id": study_id,
                "nodes": [by_id[i] for i in order],
                "order": order,
                "decisions": decisions}

    def format_lineage_text(self, study_id):
        """把血缘渲染成可读文本（CLI 与 UI 共用）。"""
        lin = self.get_lineage(study_id)
        if not lin["nodes"]:
            return "(Study %s 无产物)" % study_id
        by_id = dict((n["id"], n) for n in lin["nodes"])
        depth = {}
        lines = ["Study %s 血缘 (%d 个产物):" % (study_id, len(lin["nodes"]))]
        for i in lin["order"]:
            n = by_id[i]
            d = 0
            for p in n["parents"]:
                d = max(d, depth.get(p, 0) + 1)
            depth[i] = d
            prefix = "  " * d + ("+- " if d else "")
            lines.append("%s%s#%d v%d  sha=%s  %s"
                         % (prefix, n["kind"], n["id"], n["version"],
                            n["checksum"][:12], n["content_uri"] or ""))
        if lin["decisions"]:
            lines.append("决策(L2):")
            for d in lin["decisions"]:
                lines.append("   [%s] %s by %s (%s)"
                             % (d["gate"], d["decision"], d["actor"],
                                d["rationale"] or ""))
        return "\n".join(lines)

    # ---------------- 报告 ----------------

    def get_report_path(self, study_id):
        """取该 Study 最新一版报告路径；没有返回 None。"""
        row = self.conn.execute(
            "SELECT content_uri FROM artifacts WHERE study_id=? AND kind='report' "
            "ORDER BY version DESC LIMIT 1", (study_id,)).fetchone()
        if row is None:
            return None
        p = row["content_uri"]
        return p if p and os.path.isfile(p) else None

    def open_report(self, study_id):
        """用系统默认程序打开报告（Windows）。返回路径。"""
        p = self.get_report_path(study_id)
        if not p:
            raise ServiceError("该 Study 尚无报告，请先运行完整链")
        if hasattr(os, "startfile"):
            os.startfile(p)  # noqa
        return p

    def ensure_report_template(self, path=None):
        """确保标准报告模板存在**且是当前版本**；返回路径。"""
        if path is None:
            path = os.path.join(self.work_root, "report_standard.docx")
        return report_template.ensure_standard_template(path)

    # ---------------- 报告审批流（票18） ----------------
    #
    # 服务层只做**薄封装**：真正的状态机与审计在 core/approval.py（纯 Python 可单测）。
    # UI 调这里，测试也可以直接调 approval，两层都能用。

    def get_approval(self, study_id, version=None):
        """取报告审批状态快照（默认最新版）；未登记返回 None。"""
        return approval.get_state(self.conn, study_id, version)

    def list_approval_versions(self, study_id):
        """该 Study 所有已登记审批的报告版本。"""
        return approval.list_versions(self.conn, study_id)

    def get_approval_history(self, study_id, version=None):
        """审批审计历史（L2 decisions：角色/动作/理由/时间）。"""
        return approval.history(self.conn, study_id, version)

    def pending_approvals(self, role):
        """按角色查询待处理清单（reviewer=待审 / signer=待签 / design,analysis=已退回）。"""
        return approval.pending_for_role(self.conn, role)

    def approve_report(self, study_id, opinion=None, version=None,
                       role=None, operator=None):
        """审核通过。失败抛 ServiceError（原因可读）。"""
        return self._approval_call(
            approval.approve, study_id, version,
            role=role or approval.ROLE_REVIEWER, opinion=opinion,
            operator=operator)

    def reject_report(self, study_id, opinion=None, version=None,
                      role=None, operator=None):
        """审核退回（必须填意见）。失败抛 ServiceError（原因可读）。"""
        return self._approval_call(
            approval.reject, study_id, version,
            role=role or approval.ROLE_REVIEWER, opinion=opinion,
            operator=operator)

    def sign_report(self, study_id, opinion=None, version=None,
                    role=None, operator=None):
        """签发（必须已通过审核）。失败抛 ServiceError（原因可读）。"""
        return self._approval_call(
            approval.sign, study_id, version,
            role=role or approval.ROLE_SIGNER, opinion=opinion,
            operator=operator)

    def _approval_call(self, fn, study_id, version, **kwargs):
        try:
            return fn(self.conn, study_id, version, **kwargs)
        except approval.ApprovalError as exc:
            raise ServiceError(str(exc))

    # ---------------- 型号级汇总视图（票17） ----------------
    #
    # 与审批流同样只做**薄封装**：批量编排与结论汇总的算法在 core/model_rollup.py
    # （纯 Python 可单测），UI（票19）只调这里。
    #
    # mode="real" 时批量运行走真机适配器；mode="fake" 时走 fake 适配器，
    # 于是"一键跑通全部部件"在测试机上也能确定性回归。

    def run_model_batch(self, model_id, **kwargs):
        """
        在型号配置下**一键跑通多个部件**（逐部件完整链：建模→拓扑→FE→求解→
        抽取→校核→报告），单部件失败不影响其他部件。

        返回 model_rollup.run_model_batch 的结果 dict（含逐部件结果 + 型号级汇总）。
        额外关键字参数原样透传（parts / options / part_options /
        params_overrides / stop_on_error）。
        """
        try:
            return rollup_mod.run_model_batch(self.conn, model_id,
                                              service=self, **kwargs)
        except Exception as exc:
            raise ServiceError(str(exc))

    def get_model_rollup(self, model_id):
        """
        取型号级汇总视图：每部件的最大 FI / 最小 MS / 合格判定 / 流程状态 /
        失败原因（结论取各部件**最新版本**的校核结果）。失败抛 ServiceError。
        """
        try:
            return rollup_mod.rollup(self.conn, model_id)
        except Exception as exc:
            raise ServiceError(str(exc))

    def get_part_rollup(self, model_id, part_key):
        """取型号配置下某个部件的汇总行（UI 局部刷新用）；无该部件返回 None。"""
        try:
            return rollup_mod.get_part_rollup(self.conn, model_id, part_key)
        except Exception as exc:
            raise ServiceError(str(exc))

    def format_model_rollup(self, model_id):
        """把型号级汇总渲染成可读文本（CLI / 日志 / UI 列表共用）。"""
        return rollup_mod.format_rollup_text(self.get_model_rollup(model_id))

    def list_models(self, limit=None):
        """列型号配置（供审批/汇总视图的型号下拉框使用）。"""
        try:
            return model_config_mod.list_model_configs(self.conn, limit=limit)
        except Exception as exc:
            raise ServiceError(str(exc))

    # =======================================================================
    # 审批工作台 / 型号汇总视图的应用逻辑（票19）
    #
    # 一张票的核心原则（票08 已有的模式，这里继续沿用）：
    #   **所有编排逻辑都在服务层**（纯 Python，可脱离 UI 单测），
    #   UI 只负责调这里 + 把返回值摆到控件上。
    #
    # 因此本段负责三件事：
    #   1. 把审批状态快照（票18 approval）+ 型号归属（票16 model_config）
    #      + 校核结论（P1/票06 check_result）**拼成 UI 一行所需的一整条事实**；
    #   2. 给出"这个角色现在能做什么"（可见动作），UI 据此显示/置灰按钮；
    #   3. 给出审核详情（血缘 + 校核明细摘要），审核人据此做通过/退回。
    #
    # 状态机、血缘、汇总算法都不在这里重写——只调用 approval / model_rollup /
    # model_config 的既有函数，保证"UI 看到的就是库里的事实"。
    # =======================================================================

    def get_check_detail(self, study_id, version=None, max_hotspots=10,
                         max_rows=20):
        """
        取某 Study 某版校核结论的**明细摘要**（审核人判断依据）。

        返回 dict（无校核产物时返回 None）::

            {"study_id", "version", "content_uri", "created_at",
             "summary": {...},          # 汇总口径原样（含 criteria / safety_factor）
             "max_fi", "min_ms", "all_pass",
             "n_detail_rows", "n_fail_rows", "n_bays_checked", "n_bays_excluded",
             "n_hotspots", "hotspots": [前 N 个危险部位],
             "invalid_bays": [...],     # 被排除出自然格级的格（ADR-0008）
             "bay_best_count": ...,
             "detail_rows_total": ..., "detail_rows_sample": [...],
             "text": "可读摘要"}
        """
        if version is None:
            row = self.conn.execute(
                "SELECT * FROM artifacts WHERE study_id=? AND "
                "kind='check_result' ORDER BY version DESC LIMIT 1",
                (study_id,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM artifacts WHERE study_id=? AND "
                "kind='check_result' AND version=?",
                (study_id, int(version))).fetchone()
        if row is None:
            return None

        params = _json_loads(row["params_json"], {}) or {}
        payload = None
        uri = row["content_uri"]
        if uri and os.path.isfile(uri):
            try:
                with open(uri, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except (IOError, OSError, ValueError):
                payload = None
        if not isinstance(payload, dict):
            payload = {}

        summary = dict(payload.get("summary") or {})
        for key in ("n_detail_rows", "n_elems", "n_bays_checked",
                    "n_bays_excluded", "max_fi", "min_ms", "all_pass",
                    "n_fail_rows", "criteria", "safety_factor",
                    "rules_checksum", "ms_threshold", "allowables"):
            if key not in summary and key in params:
                summary[key] = params[key]

        hotspots = list(payload.get("hotspots") or [])[:int(max_hotspots)]
        invalid_bays = list(payload.get("invalid_bays") or [])
        bay_best = payload.get("bay_best") or {}
        rows_sample = []
        csv_path = os.path.join(os.path.dirname(uri or ""), "check_detail.csv")
        if uri and os.path.isfile(csv_path):
            rows_sample = _read_csv_head(csv_path, int(max_rows))

        detail = {
            "study_id": study_id,
            "version": int(row["version"]),
            "content_uri": uri,
            "created_at": row["created_at"],
            "summary": summary,
            "max_fi": _as_float(summary.get("max_fi", params.get("max_fi"))),
            "min_ms": _as_float(summary.get("min_ms", params.get("min_ms"))),
            "all_pass": summary.get("all_pass", params.get("all_pass")),
            "n_detail_rows": summary.get("n_detail_rows"),
            "n_fail_rows": summary.get("n_fail_rows"),
            "n_bays_checked": summary.get("n_bays_checked"),
            "n_bays_excluded": summary.get("n_bays_excluded"),
            "criteria": list(summary.get("criteria") or []),
            "safety_factor": summary.get("safety_factor"),
            "n_hotspots": len(payload.get("hotspots") or []),
            "hotspots": hotspots,
            "invalid_bays": invalid_bays,
            "bay_best_count": len(bay_best),
            "detail_rows_total": summary.get("n_detail_rows"),
            "detail_rows_sample": rows_sample,
        }
        detail["text"] = format_check_detail_text(detail)
        return detail

    def format_check_detail_text(self, study_id, version=None):
        """把某版校核明细渲染成可读文本（UI 显示；无产物时给出明确提示）。"""
        detail = self.get_check_detail(study_id, version)
        if detail is None:
            return "Study %s 尚无校核结论（需先跑完整链）" % study_id
        return detail["text"]

    # ---------------- 按角色区分可见动作 ----------------

    def approval_actions_for_role(self, role):
        """
        该角色在审批工作台里**可以执行**的动作清单（UI 据此显示按钮）。

        审核 -> 通过 / 退回；签发 -> 签发；设计 / 强度分析 -> 只能查看
        （报告被退回后由它们改设计/重跑，审批动作不属于它们）。
        未知角色抛 ServiceError。
        """
        if role not in approval.ROLES:
            raise ServiceError("未知角色 %r（合法值：%s）"
                               % (role, ", ".join(approval.ROLES)))
        return list(ROLE_ACTIONS.get(role, ()))

    def pending_review_list(self):
        """审核人视角的待审列表（每项含报告版本、Study 名、部件/型号、校核结论）。"""
        return self._inbox(approval.ROLE_REVIEWER)

    def pending_sign_list(self):
        """
        签发人视角的待签列表。

        **只有已通过审核的报告才可能出现**（approval.ROLE_QUEUES 的
        signer 队列就是 approved）；这里再显式过滤一次，防止上游改动
        让未通过的进入签发入口。
        """
        rows = self._inbox(approval.ROLE_SIGNER)
        return [r for r in rows
                if r["status"] == approval.STATUS_APPROVED
                and r["state_can_sign"]]

    def rejected_list(self):
        """设计 / 强度分析视角：被退回、需要修改的报告（只能查看，无审批动作）。"""
        return self._inbox(approval.ROLE_DESIGN)

    def approval_inbox(self, role):
        """
        统一入口：按角色取待办列表（UI 一页刷完）。

        返回 [行]；行的 ``actions`` 是该角色在这一行上**可见可执行**的动作，
        ``can_approve`` / ``can_reject`` / ``can_sign`` 供按钮置灰判断。
        """
        return self._inbox(role)

    def can_sign_report(self, study_id, version=None):
        """
        签发入口的守卫：返回 ``(ok, reason)``。

        未通过审核的报告返回 ``(False, 可读中文原因)``——UI 据此**不给入口 / 置灰**；
        已通过审核返回 ``(True, "")``。报告未登记审批时也给可读原因。
        """
        st = self.get_approval(study_id, version)
        if st is None:
            return False, ("Study %s 的报告尚未登记审批，不能签发" % study_id)
        if st["status"] == approval.STATUS_APPROVED:
            return True, ""
        if st["status"] == approval.STATUS_SIGNED:
            return False, ("报告 v%d 已签发为最终版，不可重复签发"
                           % st["version"])
        return False, ("未通过审核的报告不允许签发：当前状态「%s」，"
                       "请先经审核通过" % st["status_name"])

    def assert_can_sign(self, study_id, version=None):
        """签发前置断言：不满足时抛 ServiceError（UI 与脚本共用同一判据）。"""
        ok, reason = self.can_sign_report(study_id, version)
        if not ok:
            raise ServiceError(reason)
        return True

    # ---------------- 审核详情：血缘 + 校核明细 ----------------

    def get_review_detail(self, study_id, version=None, role=None):
        """
        审核详情：某报告版本的**完整血缘 + 校核明细摘要 + 审批历史**。

        审核人据此判断是否可信、能否通过。返回::

            {"study_id", "study_name", "model_id", "model_name", "part_key",
             "template_id",
             "approval": <状态快照>, "version", "status", "status_name",
             "actions": [该角色可见动作],
             "lineage": <get_lineage 结果>,
             "lineage_text": ...,
             "check": <get_check_detail 结果 或 None>,
             "check_text": ...,
             "history": [审批审计],
             "report_path": ...,
             "can_sign": bool, "sign_blocked_reason": ... 或 None}

        role : 可选。给定时 ``actions`` 只保留该角色**可见**的动作（UI 按当前
               角色置灰按钮）；不给则按报告状态给出全部可执行动作。

        报告未登记审批时抛 ServiceError（明确告诉 UI 该 Study 没有可审的报告）。
        """
        st = self.get_approval(study_id, version)
        if st is None:
            raise ServiceError("Study %s 的报告尚未登记审批（没有可审/可签的报告版本）"
                               % study_id)
        brief = self._study_brief(study_id)
        lineage = self.get_lineage(study_id)
        check = self.get_check_detail(study_id)
        actions = self._actions_for(st)
        if role is not None:
            if role not in approval.ROLES:
                raise ServiceError("未知角色 %r（合法值：%s）"
                                   % (role, ", ".join(approval.ROLES)))
            allowed = ROLE_ACTIONS.get(role) or ()
            actions = [a for a in actions if a in allowed]
        ok_sign, sign_reason = self.can_sign_report(study_id, st["version"])
        can_sign = ok_sign and APPROVAL_ACTION_SIGN in actions
        if ok_sign and not can_sign:
            sign_reason = "角色「%s」无权签发（需要「%s」角色）" % (
                approval.role_name(role), approval.role_name(approval.ROLE_SIGNER))
        detail = {
            "study_id": study_id,
            "study_name": brief.get("study_name"),
            "study_created_at": brief.get("study_created_at"),
            "model_id": brief.get("model_id"),
            "model_name": brief.get("model_name"),
            "part_key": brief.get("part_key"),
            "template_id": brief.get("template_id"),
            "approval": st,
            "version": st["version"],
            "status": st["status"],
            "status_name": st["status_name"],
            "role": role,
            "actions": actions,
            "lineage": lineage,
            "lineage_text": self.format_lineage_text(study_id),
            "check": check,
            "check_text": (check["text"] if check else
                           "Study %s 尚无校核结论" % study_id),
            "history": self.get_approval_history(study_id, st["version"]),
            "report_path": self.get_report_path(study_id),
            "can_sign": can_sign,
            "sign_blocked_reason": None if can_sign else sign_reason,
        }
        detail["text"] = format_review_detail_text(detail)
        return detail

    def format_review_detail_text(self, study_id, version=None, role=None):
        """把审核详情渲染成可读文本（UI 显示；一次给全血缘 + 校核 + 历史）。"""
        return self.get_review_detail(study_id, version, role=role)["text"]

    # ---------------- 型号汇总视图：各部件报告的流程状态 ----------------

    def get_model_approval_overview(self, model_id):
        """
        型号汇总视图（项目负责人视角）：各部件报告处于**哪个流程环节**。

        返回::

            {"model_id", "model_name", "generated_at",
             "parts": [{"part_key", "template_id", "study_id", "report_version",
                        "approval_version", "status", "status_name",
                        "updated_at", "last_reason", "last_actor",
                        "last_gate", "last_at",
                        "can_review", "can_sign", "final",
                        "max_fi", "min_ms", "pass", "run_status", "run_status_name",
                        "error", "report_path"}],
             "totals": {"n_parts", "by_status", "by_status_name",
                        "n_pending_review", "n_approved", "n_signed",
                        "n_rejected", "n_analyzing", "n_none"},
             "text": 可读渲染}
        """
        view = self.get_model_rollup(model_id)      # 复用票17 的汇总（结论 + 流程状态）
        parts = []
        for row in view.get("parts") or []:
            st = row.get("approval")
            status = row.get("approval_status") or rollup_mod.APPROVAL_NONE
            status_name = row.get("approval_status_name") or \
                rollup_mod.APPROVAL_NONE_NAME
            item = {
                "part_key": row.get("part_key"),
                "template_id": row.get("template_id"),
                "study_id": row.get("study_id"),
                "report_version": (row.get("latest_study") or {}).get(
                    "report_version"),
                "approval_version": row.get("approval_version"),
                "approval_study_id": row.get("approval_study_id"),
                "status": status,
                "status_name": status_name,
                "updated_at": (st or {}).get("updated_at"),
                "last_reason": (st or {}).get("last_reason"),
                "last_actor": (st or {}).get("last_actor"),
                "last_gate": (st or {}).get("last_gate"),
                "last_at": (st or {}).get("last_at"),
                "final": bool((st or {}).get("final")),
                "can_review": bool((st or {}).get("can_review")),
                "can_sign": bool((st or {}).get("can_sign")),
                "max_fi": row.get("max_fi"),
                "min_ms": row.get("min_ms"),
                "pass": row.get("pass"),
                "run_status": row.get("run_status"),
                "run_status_name": row.get("run_status_name"),
                "error": row.get("error"),
                "report_path": self._report_path_of(row.get("approval_study_id")),
            }
            parts.append(item)

        by_status = dict(view.get("totals", {}).get("by_approval_status") or {})
        by_name = dict(view.get("totals", {}).get("by_approval_name") or {})
        totals = {
            "n_parts": len(parts),
            "by_status": by_status,
            "by_status_name": by_name,
            "n_pending_review": by_status.get(approval.STATUS_PENDING_REVIEW, 0),
            "n_approved": by_status.get(approval.STATUS_APPROVED, 0),
            "n_signed": by_status.get(approval.STATUS_SIGNED, 0),
            "n_rejected": by_status.get(approval.STATUS_REJECTED, 0),
            "n_analyzing": by_status.get(approval.STATUS_ANALYZING, 0),
            "n_none": by_status.get(rollup_mod.APPROVAL_NONE, 0),
            "n_pass": view.get("totals", {}).get("n_pass"),
            "n_fail": view.get("totals", {}).get("n_fail"),
            "all_pass": view.get("totals", {}).get("all_pass"),
        }
        return {
            "model_id": view.get("model_id"),
            "model_name": view.get("model_name"),
            "generated_at": view.get("generated_at"),
            "parts": parts,
            "totals": totals,
            "rollup": view,
            "text": format_model_approval_text(view.get("model_id"),
                                               view.get("model_name"),
                                               parts, totals),
        }

    def format_model_approval(self, model_id):
        """型号汇总视图的可读渲染（各部件报告流程状态）。"""
        return self.get_model_approval_overview(model_id)["text"]

    # ---------------- 内部：把事实拼成 UI 一行 ----------------

    def _study_brief(self, study_id):
        """Study 名 + 型号归属（studies.model_id / part_key 由票16 写入）。"""
        out = {"study_id": study_id, "study_name": None,
               "study_created_at": None, "model_id": None, "model_name": None,
               "part_key": None, "template_id": None}
        if not study_id:
            return out
        row = self.conn.execute("SELECT * FROM studies WHERE id=?",
                                (study_id,)).fetchone()
        if row is None:
            return out
        keys = row.keys()
        out["study_name"] = row["name"]
        out["study_created_at"] = row["created_at"]
        out["model_id"] = row["model_id"] if "model_id" in keys else None
        out["part_key"] = row["part_key"] if "part_key" in keys else None
        if out["model_id"]:
            cfg = self.conn.execute(
                "SELECT name FROM model_configs WHERE id=?",
                (out["model_id"],)).fetchone()
            if cfg is not None:
                out["model_name"] = cfg["name"]
            part = self.conn.execute(
                "SELECT template_id FROM model_config_parts "
                "WHERE model_id=? AND part_key=?",
                (out["model_id"], out["part_key"])).fetchone()
            if part is not None:
                out["template_id"] = part["template_id"]
        return out

    def _report_path_of(self, study_id):
        """某 Study 最新报告路径（打不开/不存在时返回 None，不让 UI 崩）。"""
        if not study_id:
            return None
        try:
            return self.get_report_path(study_id)
        except Exception:
            return None

    def _check_brief(self, study_id):
        """列表行里的校核结论摘要（只要几个数，不读明细 CSV）。"""
        row = self.conn.execute(
            "SELECT * FROM artifacts WHERE study_id=? AND kind='check_result' "
            "ORDER BY version DESC LIMIT 1", (study_id,)).fetchone()
        if row is None:
            return None
        params = _json_loads(row["params_json"], {}) or {}
        return {
            "check_version": int(row["version"]),
            "max_fi": _as_float(params.get("max_fi")),
            "min_ms": _as_float(params.get("min_ms")),
            "all_pass": params.get("all_pass"),
            "n_detail_rows": params.get("n_detail_rows"),
        }

    def _actions_for(self, st):
        """该状态 + 该角色下可执行的动作（供 UI 显示/置灰按钮）。"""
        acts = []
        if st.get("can_review"):
            acts.append(APPROVAL_ACTION_APPROVE)
            acts.append(APPROVAL_ACTION_REJECT)
        if st.get("can_sign"):
            acts.append(APPROVAL_ACTION_SIGN)
        return acts

    def _inbox_row(self, st, role):
        """把一条审批快照拼成 UI 一行（状态 + 归属 + 结论 + 可见动作）。"""
        brief = self._study_brief(st["study_id"])
        check = self._check_brief(st["study_id"])
        actions = self._actions_for(st)
        # 角色过滤：审核角色看不到签发动作，签发角色看不到审核动作
        allowed = ROLE_ACTIONS.get(role)
        if allowed is not None:
            actions = [a for a in actions if a in allowed]
        row = dict(st)
        row.update(brief)
        row["check"] = check
        row["actions"] = actions
        # 状态机口径（报告本身能不能签）与"该角色可见动作"是两件事，分别保留：
        row["state_can_sign"] = bool(st.get("can_sign"))
        row["state_can_review"] = bool(st.get("can_review"))
        row["can_approve"] = APPROVAL_ACTION_APPROVE in actions
        row["can_reject"] = APPROVAL_ACTION_REJECT in actions
        row["can_sign"] = APPROVAL_ACTION_SIGN in actions
        row["report_version"] = st.get("version")
        row["report_path"] = self._report_path_of(st["study_id"])
        row["role"] = role
        row["role_name"] = approval.role_name(role)
        ok, reason = self.can_sign_report(st["study_id"], st["version"])
        row["sign_blocked_reason"] = None if ok else reason
        row["label"] = inbox_label(row)
        return row

    def _inbox(self, role):
        """按角色取待办并逐条补全事实（未知角色由 approval 抛可读错误）。"""
        try:
            states = approval.pending_for_role(self.conn, role)
        except approval.ApprovalError as exc:
            raise ServiceError(str(exc))
        return [self._inbox_row(st, role) for st in states]


# ---------------------------------------------------------------------------
# 审批工作台用到的常量与纯函数（无 UI 依赖，可单测）
# ---------------------------------------------------------------------------

APPROVAL_ACTION_APPROVE = "approve"
APPROVAL_ACTION_REJECT = "reject"
APPROVAL_ACTION_SIGN = "sign"
APPROVAL_ACTION_VIEW = "view"

APPROVAL_ACTION_NAMES = {
    APPROVAL_ACTION_APPROVE: "审核通过",
    APPROVAL_ACTION_REJECT: "审核退回",
    APPROVAL_ACTION_SIGN: "签发",
    APPROVAL_ACTION_VIEW: "查看",
}

# 角色 -> 该角色**可见**的审批动作（票19 验收项：按角色区分可见操作）
ROLE_ACTIONS = {
    approval.ROLE_REVIEWER: (APPROVAL_ACTION_APPROVE, APPROVAL_ACTION_REJECT),
    approval.ROLE_SIGNER: (APPROVAL_ACTION_SIGN,),
    # 设计 / 强度分析只能查看：报告被退回后由它们改设计与重跑，审批权不在它们手里
    approval.ROLE_DESIGN: (),
    approval.ROLE_ANALYSIS: (),
}


def _json_loads(text, default=None):
    """宽松 JSON 解析（实现在 core/textfmt，与 model_rollup 共用一份）。"""
    return textfmt.json_loads(text, default)


def _as_float(value):
    """尽力转 float（None/不可转时原样返回 None，不编造数字）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_csv_head(path, n):
    """读 CSV 前 n 行（不含表头为第一列名；返回 [dict]）。"""
    import csv as _csv
    out = []
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            reader = _csv.DictReader(fh)
            for i, row in enumerate(reader):
                if i >= int(n):
                    break
                out.append(dict(row))
    except (IOError, OSError, UnicodeError):
        return []
    return out


def _fmt_num(value, fmt="%.4f"):
    """数值格式化（实现在 core/textfmt，与 model_rollup 共用一份）。"""
    return textfmt.fmt_num(value, fmt)


def _fmt_verdict(passed):
    """合格判定文案（实现在 core/textfmt）。"""
    return textfmt.fmt_verdict(passed)


def format_check_detail_text(detail):
    """校核明细摘要的可读渲染（审核详情页直接用）。"""
    if not detail:
        return "（无校核结论）"
    lines = ["校核结论 v%s（%s）" % (detail["version"],
                                   detail.get("created_at") or "-")]
    lines.append("  最大 FI %s / 最小 MS %s / 判定 %s"
                 % (_fmt_num(detail.get("max_fi")),
                    _fmt_num(detail.get("min_ms")),
                    _fmt_verdict(detail.get("all_pass"))))
    lines.append("  准则 %s，安全系数 %s，规则指纹 %s"
                 % (", ".join(detail.get("criteria") or []) or "-",
                    detail.get("safety_factor"),
                    str(detail["summary"].get("rules_checksum") or "-")[:12]))
    lines.append("  明细行 %s（不通过 %s），有效自然格 %s，排除格 %s（不连续格）"
                 % (detail.get("n_detail_rows"), detail.get("n_fail_rows"),
                    detail.get("n_bays_checked"), detail.get("n_bays_excluded")))
    hotspots = detail.get("hotspots") or []
    if hotspots:
        lines.append("  危险部位（前 %d）：" % len(hotspots))
        for h in hotspots:
            lines.append("    %-12s sp=%-3s %-9s FI=%s MS=%s"
                         % (h.get("elem"), h.get("sp"), h.get("criterion"),
                            _fmt_num(h.get("FI")), _fmt_num(h.get("MS"))))
    else:
        lines.append("  危险部位：无")
    if detail.get("invalid_bays"):
        lines.append("  不连续格（排除自然格级）：%s"
                     % ", ".join(str(b) for b in detail["invalid_bays"]))
    return "\n".join(lines)


def inbox_label(row):
    """
    待办列表的一行文字：「Study 名（型号·部件） · 报告 vN · 状态 · FI/MS」。

    Study 名里已经含部件键（批量运行命名 ``型号 / 部件（模板）``）时不再重复
    拼型号与部件，避免一行里同一信息出现两次。
    """
    head = row.get("study_name") or row.get("study_id") or ""
    model_name = row.get("model_name")
    part_key = row.get("part_key")
    if model_name and part_key and part_key not in head:
        head = "%s（%s·%s）" % (head, model_name, part_key)
    tail = " · 报告 v%s · %s" % (row.get("version"), row.get("status_name"))
    if row.get("check"):
        tail += " · FI %s / MS %s" % (_fmt_num(row["check"].get("max_fi")),
                                      _fmt_num(row["check"].get("min_ms")))
    return "%s%s" % (head, tail)


def format_review_detail_text(detail):
    """审核详情页的可读渲染：身份 + 状态 + 校核 + 血缘 + 审批历史。"""
    lines = []
    title = detail.get("study_name") or detail.get("study_id")
    lines.append("审核详情：%s" % title)
    lines.append("  Study %s" % detail.get("study_id"))
    if detail.get("model_id"):
        lines.append("  型号配置 %s  %s / 部件 %s（模板 %s）"
                     % (detail.get("model_id"), detail.get("model_name") or "",
                        detail.get("part_key") or "-",
                        detail.get("template_id") or "-"))
    lines.append("  报告 v%s  状态：%s"
                 % (detail.get("version"), detail.get("status_name")))
    if detail.get("report_path"):
        lines.append("  报告文件：%s" % detail["report_path"])
    lines.append("")
    lines.append("【校核明细】")
    lines.append(detail.get("check_text") or "（无校核结论）")
    lines.append("")
    lines.append("【血缘】")
    lines.append(detail.get("lineage_text") or "（无产物）")
    lines.append("")
    lines.append("【审批历史】")
    hist = detail.get("history") or []
    if not hist:
        lines.append("  （无）")
    for h in hist:
        lines.append("  [%s] %s → %s by %s（%s）"
                     % (h.get("created_at"), h.get("gate"),
                        h.get("decision"), h.get("actor"),
                        h.get("rationale") or ""))
    return "\n".join(lines)


def _gate_name(gate):
    """审批审计 gate 的中文名（用于把"最近动作"与"审核意见"分开说清）。"""
    return {
        approval.GATE_SUBMIT: "提交审核",
        approval.GATE_RERUN: "重跑登记",
        approval.GATE_REVIEW: "审核",
        approval.GATE_SIGN: "签发",
    }.get(gate, gate or "-")


def format_model_approval_text(model_id, model_name, parts, totals):
    """型号汇总视图的可读渲染（各部件报告流程状态）。"""
    lines = ["型号配置 %s  %s — 报告流程状态" % (model_id, model_name or "")]
    lines.append("  %-10s %-20s %10s %10s %8s  %s"
                 % ("部件", "模板", "最大FI", "最小MS", "判定", "流程状态"))
    for p in parts or []:
        status = p.get("status_name") or rollup_mod.APPROVAL_NONE_NAME
        ver = p.get("approval_version")
        if ver:
            status = "%s（报告 v%s）" % (status, ver)
        lines.append("  %-10s %-20s %10s %10s %8s  %s"
                     % (p.get("part_key"), p.get("template_id"),
                        _fmt_num(p.get("max_fi")), _fmt_num(p.get("min_ms")),
                        _fmt_verdict(p.get("pass")), status))
        # 最近一次审批动作（谁、什么动作、什么理由）——退回意见在这里可见
        if p.get("last_reason"):
            lines.append("      最近动作: %s%s — %s"
                         % (_gate_name(p.get("last_gate")) or "-",
                            ("·" + p["last_actor"]) if p.get("last_actor") else "",
                            p["last_reason"]))
        if p.get("run_status") == rollup_mod.RUN_FAILED and p.get("error"):
            lines.append("      ! 最近一次运行失败: %s" % p["error"])
    totals = totals or {}
    lines.append("  合计: 部件 %s；待审核 %s / 已通过 %s / 已签发 %s / "
                 "已退回 %s / 未提交 %s"
                 % (totals.get("n_parts"), totals.get("n_pending_review"),
                    totals.get("n_approved"), totals.get("n_signed"),
                    totals.get("n_rejected"), totals.get("n_none")))
    return "\n".join(lines)

