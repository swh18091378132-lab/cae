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
import datetime

try:
    from . import db as db_mod
    from . import study as study_mod
    from . import template_registry
    from . import run_study as rs_mod
    from . import report_template
    from .catia_driver import FakeCatiaDriver, RealCatiaDriver
    from .abaqus_runner import FakeAbaqusRunner, RealAbaqusRunner
except ImportError:
    import db as db_mod
    import study as study_mod
    import template_registry
    import run_study as rs_mod
    import report_template
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

        base = rs_mod.RunOptions(
            params_dir=self._dir(study_id, "params"),
            topo_dir=self._dir(study_id, "topo"),
            with_topology=True,
            with_fe_model=True,
            fe_dir=self._dir(study_id, "fe"),
            subdiv_per_bay=subdiv_per_bay,
            plies=plies,
            load=load,
            abaqus=self._abaqus(),
            with_solve=True,
            job_name="job1",
            solve_dir=self._dir(study_id, "solve"),
            with_check=True,
            check_dir=self._dir(study_id, "check"),
            rules=rules,
            with_report=with_report,
            report_dir=self._dir(study_id, "report"))

        try:
            result = rs_mod.run_study(
                study_id, parameter_set, template_id,
                self._catia(study_id), self.conn, base.overlay(options))
        except Exception as exc:
            raise ServiceError(str(exc))
        return result

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
        """确保标准报告模板存在；返回路径。"""
        if path is None:
            path = os.path.join(self.work_root, "report_standard.docx")
        if not os.path.isfile(path):
            report_template.build_standard_template(path)
        return path
