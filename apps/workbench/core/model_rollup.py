# -*- coding: utf-8 -*-
"""
model_rollup.py -- 型号级汇总视图：多部件批量运行与结论汇总（票17）

背景（issue #17）：
    项目负责人在型号配置下**一键跑通全部部件**（机翼 / 尾翼 / 机身），并看到
    **型号级汇总视图**：每个部件的最新最大失效指数 FI、最小安全裕度 MS、是否合格、
    以及当前流程状态。某部件改参重跑后，汇总自动反映最新结论。

本模块提供（应用逻辑层，纯标准库，不依赖 UI / CATIA / Abaqus，可独立单测）：

    run_model_batch(conn, model_id, service=None, ...) -> dict
        对型号配置下的多个部件**一次性编排运行**：逐部件跑完整链
        （建模 -> 拓扑 -> FE -> 求解 -> 抽取 -> 校核 -> 报告），
        每个部件跑完自动 attach_study 关联到型号配置。
        **单个部件失败不影响其他部件**，失败原因记进 L2 审计并在汇总中可见。

    rollup(conn, model_id) -> dict
        型号级汇总视图：每个部件的
          * 最大 FI / 最小 MS / 合格判定（取该部件**最新版本**的校核结论）
          * 当前流程状态（分析中 / 待审核 / 已通过 / 已退回 / 已签发，
            由 core/approval.py 的 get_state 给出，本模块不复制状态机）
          * 本次运行的成败与失败原因
        以及型号级总计（最差 FI、最差 MS、是否整机合格、各流程状态计数）。

    format_rollup_text(rollup_result) -> str
        人类可读渲染（CLI / 日志 / UI 列表共用）。

## 设计要点

1. **结论取"最新"，不取"当前挂着的那个"**：
   每个部件在型号下可以有多个历史 Study（改参重跑 = 新 Study）。汇总里的
   FI / MS / 合格判定一律取该部件**最新的、真正产出了 check_result 的那次运行**，
   于是改参重跑后汇总自动更新；而一次失败的尝试不会把上一次的结论抹掉。

2. **失败尝试也算一次尝试**：
   失败的 Study 同样挂到型号上（它就是"最近一次尝试"的记录），汇总里这条尝试
   显示 ``run_status="failed"`` 与可读的 ``error``；而结论仍取最近一次**成功**产出
   check_result 的运行。失败原因同时写进 decisions 审计（gate=model_batch_run），
   可追溯、可在 UI 里展开。

3. **流程状态只查询、不实现**：
   报告审批状态机在 core/approval.py（票18）。本模块只调 ``approval.get_state``，
   没有审批记录时给出"未提交审批"。

4. **运行器可替换**：
   ``ServiceRunner`` 走工作台服务层（UI 的真实路径，fake/real 适配器由 service.mode 决定）；
   ``DirectRunner`` 直连 run_study（可注入 fake 适配器做确定性回归）；
   调用方也可以传任意实现了 ``run(study_id, params, template_id, options, part_key)``
   的对象来做失败注入。

兼容 Python 3.8（禁用 3.9+ 语法），仅标准库。
"""

import os
import json
import datetime

try:
    from . import model_config as mc
    from . import approval
    from . import study as study_mod
    from . import textfmt
    from .run_study import RunOptions, run_study
    from .catia_driver import FakeCatiaDriver
    from .abaqus_runner import FakeAbaqusRunner
except ImportError:  # 允许作为脚本/在 core 目录内直接运行
    import model_config as mc
    import approval
    import study as study_mod
    import textfmt
    from run_study import RunOptions, run_study
    from catia_driver import FakeCatiaDriver
    from abaqus_runner import FakeAbaqusRunner


class RollupError(Exception):
    """型号级批量运行 / 汇总查询的错误（参数不对、部件不存在等，消息可读）。"""

    pass


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 批量运行在 decisions 审计表里的 gate（L2：谁在什么时候跑了什么、成没成）
GATE_BATCH_RUN = "model_batch_run"
BATCH_ACTOR = "型号批量运行"

# 一次尝试的运行状态
RUN_OK = "ok"
RUN_FAILED = "failed"
RUN_NOT_RUN = "not_run"

RUN_STATUS_NAMES = {
    RUN_OK: "已运行",
    RUN_FAILED: "运行失败",
    RUN_NOT_RUN: "未运行",
}

# 没有审批记录时的流程状态（不是 approval 模块的状态，是"还没有报告进流程"）
APPROVAL_NONE = "none"
APPROVAL_NONE_NAME = "未提交审批"

# 从 check_result 产物的 params_json 里取的汇总字段（run_study 写入的那一套）
_CHECK_PARAM_KEYS = (
    "max_fi", "min_ms", "all_pass", "n_detail_rows", "n_elems",
    "n_bays_checked", "n_bays_excluded", "n_fail_rows",
)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def _json_loads(text, default=None):
    """宽松 JSON 解析（实现在 core/textfmt，与服务层共用一份）。"""
    return textfmt.json_loads(text, default)


def _as_options(value):
    """None / dict / RunOptions -> RunOptions（非法类型给可读错误）。"""
    if value is None:
        return None
    if isinstance(value, RunOptions):
        return value
    if isinstance(value, dict):
        return RunOptions(**value)
    raise RollupError(
        "运行选项应为 RunOptions 或 dict，实际 %r" % type(value).__name__)


def _merged_options(base, extra):
    """
    合并两级运行选项：``extra`` 里**显式设置过**的字段盖到 ``base`` 上。

    用 RunOptions.overlay 而不是 replace：base 上服务层铺好的目录/开关不会被
    extra 的默认值冲掉，只有调用方真正关心的字段被覆盖（例：只换 abaqus 适配器）。
    """
    if base is None:
        return extra
    if extra is None:
        return base
    return base.overlay(extra)


def _repo_root():
    # 本文件在 <repo>/apps/workbench/core/，向上四层即仓库根
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))


def _default_work_root():
    """DirectRunner 未指定工作根时的默认目录（与 .gitignore 的 data/work/ 一致）。"""
    return os.path.join(_repo_root(), "data", "work")


def _part_name(cfg_name, part, suffix=None):
    """批量运行新建 Study 的标题：型号 / 部件（模板）[后缀]。"""
    base = "%s / %s（%s）" % (cfg_name, part["part_key"], part["template_id"])
    if suffix:
        return "%s %s" % (base, suffix)
    return base


# ---------------------------------------------------------------------------
# 运行器协议（可替换）
# ---------------------------------------------------------------------------

class ModelRunner(object):
    """
    型号级批量运行的运行器协议。

    实现者只需提供 ``run(study_id, params, template_id, options, part_key)``：
    跑一次完整链并返回 run_study 式的结果 dict（至少含 ``artifacts``）。
    抛异常即视为该部件失败（批量编排会捕获并继续跑其他部件）。
    """

    def run(self, study_id, params, template_id, options=None, part_key=None):
        raise NotImplementedError


class ServiceRunner(ModelRunner):
    """
    走工作台服务层（WorkbenchService.run_chain）。

    这是 UI 的真实路径：适配器（fake / real）由服务层按 mode 决定，
    报告审批登记也由服务层完成（票18）。
    """

    def __init__(self, service):
        if not hasattr(service, "run_chain"):
            raise RollupError(
                "service 需要提供 run_chain(...)（WorkbenchService）；实际 %r"
                % type(service).__name__)
        self.service = service

    def run(self, study_id, params, template_id, options=None, part_key=None):
        return self.service.run_chain(study_id, params, template_id,
                                      options=options)


class DirectRunner(ModelRunner):
    """
    直连 run_study（不经服务层），用于注入 fake 适配器做确定性回归。

    adapters: {"catia": CatiaDriver 或 factory, "abaqus": AbaqusRunner 或 factory}
        * factory 形如 ``lambda study_id: FakeAbaqusRunner()``（每次运行可新建）
        * 缺省 catia = FakeCatiaDriver（按 Study 目录落桩 CATPart）
        * 缺省 abaqus = FakeAbaqusRunner(n_elems=6, n_plies=2)
    报告产出后会登记审批（与 ServiceRunner 行为一致，票18 的重跑重置语义不变）。
    """

    def __init__(self, work_root=None, adapters=None, conn=None,
                 templates_dir=None):
        self.work_root = work_root or _default_work_root()
        self.adapters = adapters or {}
        self.conn = conn
        self.templates_dir = templates_dir

    # -- 目录 --------------------------------------------------------------

    def _dir(self, study_id, sub):
        d = os.path.join(self.work_root, "studies", study_id, sub)
        if not os.path.isdir(d):
            os.makedirs(d)
        return d

    # -- 适配器 ------------------------------------------------------------

    def _adapter(self, name, study_id, default_fn):
        val = (self.adapters or {}).get(name)
        if val is None:
            return default_fn()
        if callable(val) and not self._looks_like_adapter(val, name):
            return val(study_id)
        return val

    @staticmethod
    def _looks_like_adapter(obj, name):
        """
        已经是适配器实例，还是"按 Study 造适配器"的工厂？

        适配器实例有协议方法（catia.regenerate / abaqus.solve）；
        类本身虽然也有同名属性，但当实例用会漏传 self，故类一律按工厂处理。
        """
        if isinstance(obj, type):
            return False
        if name == "catia":
            return hasattr(obj, "regenerate")
        return hasattr(obj, "solve")

    # -- 主流程 ------------------------------------------------------------

    def run(self, study_id, params, template_id, options=None, part_key=None):
        catia = self._adapter(
            "catia", study_id,
            lambda: FakeCatiaDriver(self._dir(study_id, "catparts")))
        abaqus = self._adapter(
            "abaqus", study_id,
            lambda: FakeAbaqusRunner(n_elems=6, n_plies=2))

        # 各阶段目录与开关集中在 RunOptions.full_chain（加新阶段只改那一处）
        base = RunOptions.full_chain(
            lambda sub: self._dir(study_id, sub),
            templates_dir=self.templates_dir,
            subdiv_per_bay=1,
            abaqus=abaqus)

        result = run_study(study_id, params, template_id, catia,
                           self.conn, options=base.overlay(options))
        self._register_approval(study_id, result)
        return result

    def _register_approval(self, study_id, result):
        """报告产出后登记审批（新版本一律从 pending_review 开始，票18）。"""
        if self.conn is None:
            return
        rep_id = (result.get("artifacts") or {}).get("report")
        if not rep_id:
            return
        row = self.conn.execute(
            "SELECT version FROM artifacts WHERE id=?", (rep_id,)).fetchone()
        if row is None:
            return
        try:
            st = approval.ensure_registered(
                self.conn, study_id, rep_id, int(row["version"]))
        except approval.ApprovalError as exc:
            result.setdefault("warnings", []).append(
                "报告审批状态登记失败: %s" % exc)
            return
        result["approval"] = st
        if isinstance(result.get("report"), dict):
            result["report"]["version"] = st["version"]


# ---------------------------------------------------------------------------
# 批量运行
# ---------------------------------------------------------------------------

def _select_parts(all_parts, wanted):
    """按调用方给的部件键挑选部件（保持型号配置里的声明顺序）。"""
    keys = [p["part_key"] for p in all_parts]
    if wanted is None:
        return list(all_parts)
    if isinstance(wanted, str):
        wanted = [wanted]
    try:
        want = [str(k) for k in wanted]
    except TypeError:
        raise RollupError(
            "parts 应为部件键列表，实际 %r" % (wanted,))
    unknown = [k for k in want if k not in keys]
    if unknown:
        raise RollupError(
            "型号配置下没有部件 %s（现有部件：%s）"
            % (", ".join(unknown), ", ".join(keys) or "(无)"))
    if not want:
        return list(all_parts)
    out = []
    seen = set()
    for key in want:          # 按型号声明顺序输出，去重
        for p in all_parts:
            if p["part_key"] == key and key not in seen:
                seen.add(key)
                out.append(p)
                break
    return out


def _record_run(conn, study_id, ok, reason):
    """把一次批量运行的结果写进 L2 审计（decisions）。失败也不吞掉。"""
    try:
        study_mod.add_decision(
            study_id, GATE_BATCH_RUN, BATCH_ACTOR,
            RUN_OK if ok else RUN_FAILED, reason, conn=conn)
        return True
    except Exception:
        # 审计写不进去不能连累批量运行本身（汇总仍可从产物推断成败）
        return False


def _make_runner(service, runner, adapters, work_root, conn, templates_dir,
                 part_key):
    if runner is not None:
        if not hasattr(runner, "run"):
            raise RollupError(
                "runner 需要提供 run(study_id, params, template_id, options, "
                "part_key)；实际 %r" % type(runner).__name__)
        return runner
    if service is not None:
        return ServiceRunner(service)
    return DirectRunner(work_root=work_root,
                        adapters=_adapters_for(adapters, part_key),
                        conn=conn, templates_dir=templates_dir)


def _adapters_for(adapters, part_key):
    """
    解析适配器声明，支持三种写法：

        {"catia": ..., "abaqus": ...}                     —— 所有部件共用
        {"default": {...}, "<part_key>": {...}}            —— 按部件覆盖
        None                                              —— 用 DirectRunner 的 fake 默认
    """
    if not adapters:
        return {}
    if not isinstance(adapters, dict):
        raise RollupError(
            "adapters 应为 dict（{'catia':..., 'abaqus':...}），实际 %r"
            % type(adapters).__name__)
    if "catia" in adapters or "abaqus" in adapters:
        return dict(adapters)
    merged = dict(adapters.get("default") or {})
    per = adapters.get(part_key)
    if isinstance(per, dict):
        merged.update(per)
    return merged


def run_model_batch(conn, model_id, service=None, parts=None, options=None,
                    part_options=None, params_overrides=None, runner=None,
                    adapters=None, work_root=None, stop_on_error=False,
                    templates_dir=None):
    """
    对型号配置下的多个部件**一次性编排运行**（型号级"一键跑通"）。

    必需:
        conn       : 打开的数据库连接
        model_id   : 型号配置 id

    运行器（三选一，优先级 runner > service > adapters/DirectRunner）:
        service        : WorkbenchService 实例（UI 真实路径；fake/real 由 mode 定）
        runner         : 任意实现了 run(study_id, params, template_id,
                         options, part_key) 的对象（做失败注入很方便）
        adapters       : {"catia":..., "abaqus":...}，也可按部件覆盖
                         （{"default": {...}, "wing": {...}}）；不传则用 fake 适配器

    其它:
        parts          : 只跑这些部件键（None = 型号下全部部件）
        options        : 所有部件共用的 RunOptions / dict
        part_options   : {部件键: RunOptions/dict}，盖在 options 之上
        params_overrides: {部件键: {参数名: 值}}，临时改参（改参重跑；
                         非法值会让该部件失败，其它部件照跑）
        work_root      : DirectRunner 的工作目录（默认 <repo>/data/work）
        stop_on_error  : True = 遇首个失败就停（默认 False：单点失败不影响其他部件）
        templates_dir  : 模板目录（默认 data/templates）

    返回::

        {"model_id": ..., "model_name": ..., "started_at": ..., "finished_at": ...,
         "elapsed_sec": ..., "ok": <是否全部成功>,
         "parts": [{"part_key", "template_id", "params", "ok", "study_id",
                    "error", "error_type", "elapsed_sec", "artifacts",
                    "check", "report_version", "warnings", "params_overridden"}],
         "n_ok": ..., "n_failed": ...,
         "rollup": <运行结束时的型号级汇总视图（rollup 的返回）>}

    每个部件都新建一个 Study（改参重跑 = 新 Study，血缘不断），跑完（无论成败）
    都关联到型号配置的该部件上——失败的那次就是"最近一次尝试"的记录。
    """
    cfg = mc.get_model_config(conn, model_id)   # 不存在则抛 ModelConfigError
    all_parts = mc.list_parts(conn, model_id)
    if not all_parts:
        raise RollupError("型号配置 %r 下没有任何部件，无法批量运行" % (model_id,))
    selected = _select_parts(all_parts, parts)

    base_options = _as_options(options)
    per_part_options = part_options or {}
    if not isinstance(per_part_options, dict):
        raise RollupError("part_options 应为 {部件键: 选项} 的 dict，实际 %r"
                          % type(per_part_options).__name__)
    overrides = params_overrides or {}
    if not isinstance(overrides, dict):
        raise RollupError("params_overrides 应为 {部件键: {参数: 值}} 的 dict，"
                          "实际 %r" % type(overrides).__name__)

    started = _now()
    t0 = datetime.datetime.now()
    records = []

    for part in selected:
        key = part["part_key"]
        opts = _merged_options(base_options, _as_options(per_part_options.get(key)))

        params = dict(part["params"] or {})
        mine = overrides.get(key) or {}
        if mine:
            params.update(mine)

        rec = {
            "part_key": key,
            "template_id": part["template_id"],
            "params": params,
            "params_overridden": bool(mine),
            "ok": False,
            "study_id": None,
            "error": None,
            "error_type": None,
            "artifacts": {},
            "check": None,
            "report_version": None,
            "warnings": [],
            "elapsed_sec": 0.0,
        }

        pt0 = datetime.datetime.now()
        study_id = None
        try:
            # 1) 新 Study 并挂到型号配置（改参重跑 = 新 Study，血缘不断）
            study_id = mc.create_part_study(
                conn, model_id, key, name=_part_name(cfg["name"], part))
            rec["study_id"] = study_id

            # 2) 跑完整链
            runner_obj = _make_runner(service, runner, adapters, work_root,
                                      conn, templates_dir, key)
            out = runner_obj.run(study_id, params, part["template_id"],
                                 options=opts, part_key=key)

            rec["ok"] = True
            rec["artifacts"] = dict(out.get("artifacts") or {})
            rec["warnings"] = list(out.get("warnings") or [])
            rec["check"] = _check_summary_of_result(out)
            rec["report_version"] = _report_version(conn, study_id,
                                                    rec["artifacts"])
            _record_run(conn, study_id, True,
                        "型号级批量运行完成（模板 %s）" % part["template_id"])
        except Exception as exc:
            rec["ok"] = False
            rec["error_type"] = type(exc).__name__
            rec["error"] = str(exc) or type(exc).__name__
            # 脏事务回滚：已落库的产物（各自 commit）保留，半截插入丢弃
            try:
                conn.rollback()
            except Exception:
                pass
            if study_id:
                _record_run(conn, study_id, False,
                            "型号级批量运行失败：%s" % rec["error"])
        finally:
            rec["elapsed_sec"] = round(
                (datetime.datetime.now() - pt0).total_seconds(), 3)
        records.append(rec)
        if not rec["ok"] and stop_on_error:
            break

    finished = _now()
    elapsed = round((datetime.datetime.now() - t0).total_seconds(), 3)
    n_ok = len([r for r in records if r["ok"]])
    n_failed = len([r for r in records if not r["ok"]])

    return {
        "model_id": model_id,
        "model_name": cfg["name"],
        "started_at": started,
        "finished_at": finished,
        "elapsed_sec": elapsed,
        "ok": n_failed == 0,
        "parts": records,
        "n_ok": n_ok,
        "n_failed": n_failed,
        "rollup": rollup(conn, model_id),
    }


# ---------------------------------------------------------------------------
# 从产物里读结论
# ---------------------------------------------------------------------------

def _check_summary_of_result(out):
    """从一次 run_study 结果里取校核汇总（没跑校核时返回 None）。"""
    check = (out or {}).get("check") or {}
    summary = check.get("summary")
    if not summary:
        return None
    picked = {}
    for k in _CHECK_PARAM_KEYS:
        if k in summary:
            picked[k] = summary[k]
    if not picked:
        return None
    picked["criteria"] = list(summary.get("criteria") or [])
    picked["safety_factor"] = summary.get("safety_factor")
    picked["rules_checksum"] = summary.get("rules_checksum")
    picked["n_hotspots"] = len(check.get("hotspots") or [])
    return picked


def _report_version(conn, study_id, artifacts):
    """报告产物版本号（审批状态按版本登记；没写 artifacts 时查库兜底）。"""
    rep_id = (artifacts or {}).get("report")
    if not rep_id:
        row = conn.execute(
            "SELECT version FROM artifacts WHERE study_id=? AND kind='report' "
            "ORDER BY version DESC LIMIT 1", (study_id,)).fetchone()
        return int(row["version"]) if row is not None else None
    row = conn.execute("SELECT version FROM artifacts WHERE id=?",
                       (rep_id,)).fetchone()
    return int(row["version"]) if row is not None else None


def _summary_from_artifact(row):
    """
    把一行 check_result 产物解析成汇总 dict。

    params_json 里存的就是 run_study 从 chk["summary"] 抄下来的那些字段，
    因此不必打开 JSON 文件即可出汇总；文件可读时再补 criteria / 危险部位数
    （best-effort，读不到不影响汇总）。
    """
    params = _json_loads(row["params_json"], {}) or {}
    out = {
        "study_id": row["study_id"],
        "check_version": int(row["version"]),
        "checked_at": row["created_at"],
    }
    for k in _CHECK_PARAM_KEYS:
        out[k] = params.get(k)
    out["criteria"] = None
    out["safety_factor"] = None
    out["n_hotspots"] = None

    uri = row["content_uri"]
    if uri and os.path.isfile(uri):
        try:
            with open(uri, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (IOError, OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            summary = payload.get("summary") or {}
            for k in _CHECK_PARAM_KEYS:
                if out.get(k) is None and k in summary:
                    out[k] = summary[k]
            out["criteria"] = summary.get("criteria")
            out["safety_factor"] = summary.get("safety_factor")
            out["n_hotspots"] = len(payload.get("hotspots") or [])
    # 归一：max_fi / min_ms 是数值（None 保持 None，表示没结论）
    for k in ("max_fi", "min_ms"):
        if out.get(k) is not None:
            try:
                out[k] = float(out[k])
            except (TypeError, ValueError):
                pass
    return out


# ---------------------------------------------------------------------------
# 汇总视图
# ---------------------------------------------------------------------------

def _studies_by_part(conn, model_id):
    """
    型号配置下各部件的历史 Study，按**插入顺序**（rowid）升序。

    不按 created_at 排：created_at 只到秒，同一秒内建的两个 Study 无法定序；
    rowid 是插入顺序，天然是"谁后建的"。返回 {part_key: [行, ...]}。
    """
    out = {}
    rows = conn.execute(
        "SELECT rowid AS _rid, * FROM studies WHERE model_id=? ORDER BY _rid",
        (model_id,)).fetchall()
    for row in rows:
        keys = row.keys()
        part_key = row["part_key"] if "part_key" in keys else None
        out.setdefault(part_key, []).append(row)
    return out


def _latest_artifact(conn, study_id, kind):
    """某 Study 某类产物的最新版本（没有返回 None）。"""
    return conn.execute(
        "SELECT * FROM artifacts WHERE study_id=? AND kind=? "
        "ORDER BY version DESC LIMIT 1", (study_id, kind)).fetchone()


def _latest_batch_decision(conn, study_id):
    """最近一次批量运行在该 Study 上留下的审计记录（没有返回 None）。"""
    return conn.execute(
        "SELECT * FROM decisions WHERE study_id=? AND gate=? "
        "ORDER BY id DESC LIMIT 1", (study_id, GATE_BATCH_RUN)).fetchone()


def _artifact_kinds(conn, study_id):
    counts = {}
    for row in conn.execute(
            "SELECT kind, COUNT(*) c FROM artifacts WHERE study_id=? "
            "GROUP BY kind", (study_id,)).fetchall():
        counts[row["kind"]] = int(row["c"])
    return counts


def _approval_of(conn, study_id):
    """某 Study 最新一版报告的审批状态快照（未登记 -> 未提交审批）。"""
    if not study_id:
        return None, APPROVAL_NONE, APPROVAL_NONE_NAME, None
    try:
        st = approval.get_state(conn, study_id)
    except approval.ApprovalError:
        st = None
    if st is None:
        return None, APPROVAL_NONE, APPROVAL_NONE_NAME, None
    return (st, st["status"], st.get("status_name"),
            int(st.get("version") or 0))


def _part_row(conn, part, studies):
    """
    单个部件的汇总行（rollup 的核心）。

    studies: 该部件在型号下的全部历史 Study 行（插入顺序升序）
    """
    key = part["part_key"]

    # --- 最近一次尝试：型号配置上挂着的当前 Study（批量运行会把它指向最新那次）
    latest_row = None
    if part["study_id"]:
        for row in studies:
            if row["id"] == part["study_id"]:
                latest_row = row
                break
    if latest_row is None and studies:
        latest_row = studies[-1]

    # --- 结论：该部件**最新一次真正产出 check_result** 的运行
    conc_row = None
    conc_artifact = None
    for row in reversed(studies):
        art = _latest_artifact(conn, row["id"], "check_result")
        if art is not None:
            conc_row = row
            conc_artifact = art
            break

    # --- 最近一次尝试的成败与原因（L2 审计优先，产物推断兜底）
    run_status = RUN_NOT_RUN
    error = None
    decision = _latest_batch_decision(conn, latest_row["id"]) \
        if latest_row is not None else None
    if decision is not None:
        if decision["decision"] == RUN_OK:
            run_status = RUN_OK
        else:
            run_status = RUN_FAILED
            error = decision["rationale"]
    elif latest_row is not None:
        run_status = (RUN_OK if _latest_artifact(conn, latest_row["id"],
                                                 "check_result") is not None
                      else RUN_NOT_RUN)

    conclusion = _summary_from_artifact(conc_artifact) if conc_artifact else None

    latest_study = None
    if latest_row is not None:
        kinds = _artifact_kinds(conn, latest_row["id"])
        latest_study = {
            "study_id": latest_row["id"],
            "name": latest_row["name"],
            "created_at": latest_row["created_at"],
            "n_artifacts": sum(kinds.values()),
            "by_kind": kinds,
            "has_check": "check_result" in kinds,
            "has_report": "report" in kinds,
            "check_version": None,
            "report_version": None,
            "run_status": run_status,
            "error": error,
        }
        check_art = _latest_artifact(conn, latest_row["id"], "check_result")
        if check_art is not None:
            latest_study["check_version"] = int(check_art["version"])
        rep_art = _latest_artifact(conn, latest_row["id"], "report")
        if rep_art is not None:
            latest_study["report_version"] = int(rep_art["version"])

    # --- 流程状态取"正在被审的那份报告" = 结论所在 Study 的报告
    approval_study = conc_row["id"] if conc_row is not None else (
        latest_row["id"] if latest_row is not None else None)
    st, status, status_name, version = _approval_of(conn, approval_study)

    max_fi = conclusion.get("max_fi") if conclusion else None
    min_ms = conclusion.get("min_ms") if conclusion else None
    passed = conclusion.get("all_pass") if conclusion else None

    return {
        "part_key": key,
        "template_id": part["template_id"],
        "parent_part": part["parent_part"],
        "params": dict(part["params"] or {}),
        # 当前挂着的 Study（= 最近一次尝试）
        "study_id": latest_row["id"] if latest_row is not None else None,
        "n_studies": len(studies),
        "latest_study": latest_study,
        # 最新一版校核结论（改参重跑后自动更新）
        "conclusion": conclusion,
        "conclusion_study_id": conc_row["id"] if conc_row is not None else None,
        "max_fi": max_fi,
        "min_ms": min_ms,
        "pass": passed,
        "n_fail_rows": conclusion.get("n_fail_rows") if conclusion else None,
        # 当前流程状态（approval.get_state）
        "approval": st,
        "approval_status": status,
        "approval_status_name": status_name,
        "approval_version": version,
        "approval_study_id": approval_study,
        # 最近一次运行的成败与失败原因
        "run_status": run_status,
        "run_status_name": RUN_STATUS_NAMES.get(run_status, run_status),
        "ok": run_status == RUN_OK,
        "error": error,
    }


def _worst(values, pick):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return pick(vals)


def _totals(rows):
    """型号级总计：整机最差 FI / 最差 MS、合格判定、流程状态计数、产物统计。"""
    by_status = {}
    for r in rows:
        by_status[r["approval_status"]] = \
            by_status.get(r["approval_status"], 0) + 1

    with_conc = [r for r in rows if r["conclusion"] is not None]
    n_pass = len([r for r in rows if r.get("pass") is True])
    n_fail = len([r for r in rows if r.get("pass") is False])
    all_pass = (len(with_conc) == len(rows) and len(rows) > 0
                and n_pass == len(rows))

    return {
        "n_parts": len(rows),
        "n_ok": len([r for r in rows if r["run_status"] == RUN_OK]),
        "n_failed": len([r for r in rows if r["run_status"] == RUN_FAILED]),
        "n_not_run": len([r for r in rows if r["run_status"] == RUN_NOT_RUN]),
        "n_with_conclusion": len(with_conc),
        "n_pass": n_pass,
        "n_fail": n_fail,
        "all_pass": all_pass,
        "complete": len(with_conc) == len(rows) and len(rows) > 0,
        # 型号级口径：取各部件结论里**最差**的那一个（型号整体由最弱环节决定）
        "max_fi": _worst([r["max_fi"] for r in rows], max),
        "min_ms": _worst([r["min_ms"] for r in rows], min),
        "by_approval_status": by_status,
        "by_approval_name": dict(
            (k, approval.status_name(k) if k != APPROVAL_NONE
             else APPROVAL_NONE_NAME) for k in by_status),
    }


def rollup(conn, model_id):
    """
    型号级汇总视图：每个部件的最新 FI / MS / 合格判定 / 流程状态 / 失败原因。

    返回::

        {"model_id", "model_name", "overall", "note", "created_at",
         "generated_at",
         "parts": [部件行, ...],        # 按型号配置的部件声明顺序
         "totals": {...}}

    部件行：见 ``_part_row``（part_key / max_fi / min_ms / pass / approval_status /
    approval_status_name / run_status / error / conclusion / latest_study ...）。

    **结论取最新**：改参重跑（新 Study）后，这里自动反映新结论；一次失败的尝试
    不会抹掉上一次成功的结论（失败信息在 run_status / error 里）。
    """
    cfg = mc.get_model_config(conn, model_id)
    parts = mc.list_parts(conn, model_id)
    by_part = _studies_by_part(conn, model_id)

    rows = []
    for part in parts:
        rows.append(_part_row(conn, part, by_part.get(part["part_key"]) or []))

    return {
        "model_id": cfg["model_id"],
        "model_name": cfg["name"],
        "overall": cfg["overall"],
        "note": cfg["note"],
        "created_at": cfg["created_at"],
        "generated_at": _now(),
        "parts": rows,
        "totals": _totals(rows),
    }


def get_part_rollup(conn, model_id, part_key):
    """取某个部件的汇总行（UI 单行刷新用）；没有该部件返回 None。"""
    for row in rollup(conn, model_id)["parts"]:
        if row["part_key"] == part_key:
            return row
    return None


# ---------------------------------------------------------------------------
# 人类可读渲染
# ---------------------------------------------------------------------------

def _fmt_num(value, fmt="%.4f"):
    """数值格式化（实现在 core/textfmt，与服务层共用一份）。"""
    return textfmt.fmt_num(value, fmt)


def _fmt_verdict(passed):
    """合格判定文案（实现在 core/textfmt）。"""
    return textfmt.fmt_verdict(passed)


def format_rollup_text(result):
    """把汇总视图渲染成可读文本（CLI / 日志 / UI 列表共用）。"""
    lines = ["型号配置 %s  %s" % (result.get("model_id"),
                                  result.get("model_name") or "")]
    overall = result.get("overall") or {}
    if overall:
        lines.append("  总体参数: " + ", ".join(
            "%s=%s" % (k, overall[k]) for k in sorted(overall)))
    lines.append("  %-10s %-20s %10s %10s %8s  %s"
                 % ("部件", "模板", "最大FI", "最小MS", "判定", "流程状态"))
    for row in result.get("parts") or []:
        status = row.get("approval_status_name") or APPROVAL_NONE_NAME
        # 流程状态与"最近一次运行成败"是**两条正交的事实**，不能互相覆盖：
        # 报告已签发之后又跑失败一次，签发状态依然有效（旧结论仍是被批准的结论）。
        if row.get("run_status") == RUN_FAILED:
            status = "%s（最近一次运行失败）" % status
        lines.append("  %-10s %-20s %10s %10s %8s  %s"
                     % (row["part_key"], row["template_id"],
                        _fmt_num(row.get("max_fi")),
                        _fmt_num(row.get("min_ms")),
                        _fmt_verdict(row.get("pass")), status))
        if row.get("error"):
            lines.append("      ! 失败原因: %s" % row["error"])
    totals = result.get("totals") or {}
    lines.append("  合计: 部件 %s / 已运行 %s / 失败 %s / 有结论 %s；"
                 "整机最差 FI %s，最差 MS %s，%s"
                 % (totals.get("n_parts"), totals.get("n_ok"),
                    totals.get("n_failed"), totals.get("n_with_conclusion"),
                    _fmt_num(totals.get("max_fi")),
                    _fmt_num(totals.get("min_ms")),
                    _fmt_verdict(totals.get("all_pass"))))
    return "\n".join(lines)
