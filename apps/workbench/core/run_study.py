# -*- coding: utf-8 -*-
"""
run_study.py -- Study 编排器（唯一主测试切口）

已接入:
  M1(票02)：参数集 -> 参数契约/包络校验 -> CATIA 再生数模 -> 血缘
  M2(票03)：数模 -> 结构拓扑模型 -> 自然格 + 不连续格 -> 血缘
  M3(票04)：拓扑 -> 网格(与自然格对齐) -> 铺层(CAE 属性)+载荷/BC -> .inp -> 血缘
  M4(票05)：.inp -> Abaqus 求解(.odb) -> ODB 抽取(CSV/JSON+manifest) -> 血缘
  M5(票06)：抽取 + 规则 -> 单元x铺层x准则 FI/MS -> 自然格聚合 -> 危险部位 -> 血缘
后续票 07 将在此继续接入 报告生成，接口保持向后兼容。

兼容 Python 3.8。
"""

import os

try:
    from . import template_registry
    from . import topology
    from . import fe_model
    from . import strength_check
    from . import report as report_mod
    from . import hotspot_figure
    from . import report_template
    from .study import add_artifact, link
except ImportError:
    import template_registry
    import topology
    import fe_model
    import strength_check
    import report as report_mod
    import hotspot_figure
    import report_template
    from study import add_artifact, link


class StudyRunError(Exception):
    pass


# ---------------------------------------------------------------------------
# 运行选项（票10）：把 P1 时代膨胀到 31 个的位置参数收进一个带默认值的对象
# ---------------------------------------------------------------------------
#
# 设计约束（票10 验收）：
#   * run_study 的**签名不再随能力增长**——新增能力 = 往 RunOptions 加字段；
#   * 不传 options 即可跑通 P1 全链（每个字段都有合理默认值）；
#   * 旧的**关键字**调用形式仍然可用（向后兼容，P1 的 m0~m8 不改断言）。
#
# 字段分三组：必备流程（study/参数/模板/适配器/连接 仍是 run_study 的位置参数）、
# 落盘目录、各阶段开关与参数。

class RunOptions(object):
    """
    一次 Study 运行的可选参数集合。所有字段都有默认值。

    用法::

        run_study(sid, params, "wingbox_v1", catia, conn)               # 默认
        run_study(sid, params, "wingbox_v1", catia, conn, RunOptions(with_solve=True, abaqus=fake))
        run_study(sid, params, "wingbox_v1", catia, conn, with_solve=True, abaqus=fake)  # 兼容旧写法

    扩展方式（后续票）：直接 `opts.analysis = AnalysisSpec(...)`，或
    `RunOptions(analysis=...)`；run_study 的签名不变。未知字段也不会丢，
    存在 `opts.extra` 里，用 `opts.get("名字")` 读。
    """

    DEFAULTS = {
        # --- 目录 ---
        "params_dir": None,
        "templates_dir": None,
        "topo_dir": None,
        # --- 拓扑（票03）---
        "holes": None,
        "stringer_runout": None,
        "partial_ribs": None,
        "with_topology": True,
        # --- FE 模型（票04）---
        "with_fe_model": False,
        "fe_dir": None,
        "subdiv_per_bay": 4,
        "plies": None,
        "material": None,
        "step": None,
        "load": None,
        # --- 求解与抽取（票05）---
        "abaqus": None,
        "with_solve": False,
        "job_name": "job1",
        "solve_dir": None,
        # P2（票14）：分析类型与其参数；None = 沿用 P1 的静力默认
        "analysis": None,
        # --- 强度校核（票06）---
        "with_check": False,
        "rules": None,
        "rules_path": None,
        "check_dir": None,
        # --- 报告（票07）---
        "with_report": False,
        "report_dir": None,
        "report_template_path": None,
        "report_out": None,
        "report_options": None,
        # --- P2 扩展位（先占位，由后续票填充语义）---
        "mesh_quality": None,      # 票15：网格质量门策略
        "model_config_id": None,   # 票16：型号配置引用
    }

    def __init__(self, **kwargs):
        self._explicit = set(kwargs.keys())
        for name, default in self.DEFAULTS.items():
            setattr(self, name, kwargs.pop(name, default))
        # 未知字段照存不丢（字典/列表等可变默认值无需深拷贝：调用方各自传入）
        self.extra = kwargs

    # -- 读写 --------------------------------------------------------------

    def get(self, name, default=None):
        """读一个字段；未声明字段回退到 extra。"""
        if name in self.DEFAULTS:
            return getattr(self, name, default)
        return self.extra.get(name, default)

    def set(self, name, value):
        """写一个字段；未声明字段进 extra（不需要改 run_study 签名）。"""
        self._explicit.add(name)
        if name in self.DEFAULTS:
            setattr(self, name, value)
        else:
            self.extra[name] = value
        return self

    def replace(self, **kwargs):
        """基于当前值派生一份新选项（服务层组合各阶段开关时用）。"""
        merged = dict((k, getattr(self, k)) for k in self.DEFAULTS)
        merged.update(self.extra)
        out = RunOptions(**merged)
        out._explicit = set(self._explicit)
        for k, v in kwargs.items():
            out.set(k, v)
        return out

    def overlay(self, other):
        """
        把另一个选项里**显式设置过**的字段盖到自己上面，返回新对象。

        与 replace(**other.to_dict()) 的区别：未显式设置（仍是默认值）的字段不会
        把当前的定制值冲掉——服务层先铺好目录/开关，再让调用方按需覆盖时正是要这个。
        """
        if other is None:
            return self.replace()
        if isinstance(other, dict):
            other = RunOptions(**other)
        if not isinstance(other, RunOptions):
            raise StudyRunError("overlay 需要 RunOptions 或 dict，实际 %r"
                                % type(other).__name__)
        out = self.replace()
        for name in other._explicit:
            out.set(name, other.get(name))
        return out

    def to_dict(self):
        d = dict((k, getattr(self, k)) for k in self.DEFAULTS)
        d.update(self.extra)
        return d

    def __repr__(self):
        on = [k for k in ("with_topology", "with_fe_model", "with_solve",
                          "with_check", "with_report") if getattr(self, k)]
        return "RunOptions(%s)" % ", ".join(on)

    # -- 归一化 ------------------------------------------------------------

    @classmethod
    def coerce(cls, options=None, legacy=None):
        """
        把 (options, legacy_kwargs) 归一成一个 RunOptions。

        options 可为 None / dict / RunOptions；legacy_kwargs 是老的**关键字**写法，
        显式传入的 legacy 值覆盖 options 里的同名字段。
        """
        if options is None:
            base = cls()
        elif isinstance(options, cls):
            base = options
        elif isinstance(options, dict):
            base = cls(**options)
        else:
            raise StudyRunError("options 必须是 RunOptions 或 dict，实际 %r"
                                % type(options).__name__)
        return base.replace(**(legacy or {}))


def _normalize_analysis(analysis):
    """
    把 options.analysis 归一成分析类型声明 dict（票14）。

    统一委托给 core/analysis.py —— 分析类型的唯一定义处（宿主侧）；
    Abaqus 侧 fe_build.py 只消费声明 dict，以保持 Python 2.7 可运行。
    """
    try:
        from . import analysis as analysis_mod
    except ImportError:
        import analysis as analysis_mod
    try:
        return analysis_mod.normalize(analysis)
    except Exception as exc:
        raise StudyRunError("分析类型无效: %s" % exc)


def _mesh_quality_artifact_params(fe):
    """网格质量门（票15）要写进 mesh 产物 params 的字段；未启用则为空。"""
    q = getattr(fe, "mesh_quality", None) or {}
    if not q:
        return {}
    return {
        "mesh_quality_enabled": True,
        "mesh_quality_n_warn_bays": len(q.get("warn_bays") or []),
        "mesh_quality_n_refined_bays": len(q.get("refined_bays") or []),
        "mesh_quality_worst_jacobian": q.get("worst", {}).get("jacobian"),
        "mesh_quality_max_aspect": q.get("worst", {}).get("aspect_ratio"),
        "mesh_quality_max_warp": q.get("worst", {}).get("warp"),
    }


def _study_name(study_id, conn):
    """取 Study 名称；查不到则退回 id（报告标题栏用）。"""
    try:
        row = conn.execute("SELECT name FROM studies WHERE id=?",
                           (study_id,)).fetchone()
        return row["name"] if row is not None else study_id
    except Exception:
        return study_id


def run_study(study_id, parameter_set, template_id, catia, conn,
              options=None, **legacy):
    """
    执行一次 Study。返回 dict 汇总。

    **必需参数（就这 5 个）**:
        study_id      : 已存在的 Study id
        parameter_set : 用户输入参数 dict
        template_id   : 模板 id（见 data/templates）
        catia         : CatiaDriver 实例（真机 RealCatiaDriver / 测试 FakeCatiaDriver）
        conn          : 打开的数据库连接

    **选项参数**:
        options       : RunOptions 实例或 dict；也可用旧的**关键字**形式直接传
                        （params_dir=... / with_solve=True / ...），二者可混用，
                        关键字优先。见 RunOptions 的字段表。

    新增能力时往 RunOptions 加字段即可，本签名保持不变。
    """
    opts = RunOptions.coerce(options, legacy)

    params_dir = opts.params_dir
    templates_dir = opts.templates_dir
    topo_dir = opts.topo_dir
    holes = opts.holes
    stringer_runout = opts.stringer_runout
    partial_ribs = opts.partial_ribs
    with_topology = opts.with_topology
    with_fe_model = opts.with_fe_model
    fe_dir = opts.fe_dir
    subdiv_per_bay = opts.subdiv_per_bay
    plies = opts.plies
    material = opts.material
    step = opts.step
    load = opts.load
    abaqus = opts.abaqus
    with_solve = opts.with_solve
    job_name = opts.job_name
    solve_dir = opts.solve_dir
    with_check = opts.with_check
    rules = opts.rules
    rules_path = opts.rules_path
    check_dir = opts.check_dir
    with_report = opts.with_report
    report_dir = opts.report_dir
    report_template_path = opts.report_template_path
    report_out = opts.report_out

    tpl = template_registry.load(template_id, templates_dir)

    # 1) 参数契约 + 参数化包络校验（不通过直接抛，不产生任何产物）
    params = tpl.validate(parameter_set)  # 抛 ParamError

    # 2) 参数集产物
    params_uri = None
    if params_dir:
        os.makedirs(params_dir, exist_ok=True)
        params_uri = os.path.join(params_dir, "parameter_set.json")
        import json
        with open(params_uri, "w", encoding="utf-8") as fh:
            json.dump(params, fh, ensure_ascii=False, sort_keys=True, indent=2)
    else:
        params_uri = "params:%s" % study_id

    ps_id = add_artifact(study_id, "parameter_set", content_uri=params_uri,
                         params=params, conn=conn)

    # 3) CATIA 再生参数化数模
    try:
        catpart = catia.regenerate(params, tpl)
    except Exception as exc:
        raise StudyRunError("CATIA 再生失败: %s" % exc)

    cat_id = add_artifact(study_id, "catpart", content_uri=catpart.path,
                          params={"template": tpl.id, "checksum": catpart.checksum},
                          conn=conn)
    link(ps_id, cat_id, conn=conn)

    result = {
        "study_id": study_id,
        "study_name": _study_name(study_id, conn),
        "template": tpl.id,
        "parameters": params,
        "artifacts": {"parameter_set": ps_id, "catpart": cat_id},
        "catpart_path": catpart.path,
        "catpart_checksum": catpart.checksum,
    }

    # 4) 结构拓扑模型 -> 自然格 + 不连续格（票 03；票 11 起按模板声明分派布置规则）
    if with_topology:
        model = topology.build_topology(
            tpl.id, params, holes=holes,
            stringer_runout=stringer_runout, partial_ribs=partial_ribs,
            template=tpl)

        if topo_dir:
            os.makedirs(topo_dir, exist_ok=True)
            topo_uri = os.path.join(topo_dir, "topology.json")
            with open(topo_uri, "w", encoding="utf-8") as fh:
                fh.write(model.to_json())
        else:
            topo_uri = "topology:%s" % study_id

        topo_id = add_artifact(
            study_id, "topology", content_uri=topo_uri,
            params={"n_bays_total": len(model.bays),
                    "n_bays_valid": len(model.valid_bays),
                    "n_bays_invalid": len(model.invalid_bays)},
            conn=conn)
        link(cat_id, topo_id, conn=conn)

        result["artifacts"]["topology"] = topo_id
        result["topology"] = {
            "n_total": len(model.bays),
            "n_valid": len(model.valid_bays),
            "n_invalid": len(model.invalid_bays),
            "invalid_ids": [b.id for b in model.invalid_bays],
        }
        result["topology_model"] = model

    # 5) FE 模型构建 -> 网格 + 铺层 + 载荷/BC -> .inp（票 04；票14 分析类型 / 票15 质量门）
    if with_fe_model:
        if not with_topology:
            raise StudyRunError("构建 FE 模型需要先启用 with_topology")
        analysis_spec = _normalize_analysis(opts.analysis)
        try:
            fe = fe_model.build_fe_model(
                model, fe_dir or (os.path.join(params_dir, "fe") if params_dir else "fe"),
                subdiv_per_bay=subdiv_per_bay, plies=plies,
                material=material, step=step, load=load,
                analysis=analysis_spec, mesh_quality=opts.mesh_quality)
        except Exception as exc:
            raise StudyRunError("FE 模型构建失败: %s" % exc)

        mesh_id = add_artifact(
            study_id, "mesh", content_uri=fe.inp_path,
            params={"n_nodes": fe.mesh_dict["n_nodes"],
                    "n_elems": fe.mesh_dict["n_elems"],
                    "subdiv_per_bay": subdiv_per_bay,
                    "n_bays": len(model.bays),
                    **_mesh_quality_artifact_params(fe)},
            conn=conn)
        link(result["artifacts"]["topology"], mesh_id, conn=conn)

        inp_id = add_artifact(
            study_id, "inp", content_uri=fe.inp_path,
            params={"step": fe.step.get("name"),
                    "step_type": fe.step.get("type"),
                    "analysis_kind": (analysis_spec or {}).get("kind", "static"),
                    "n_plies": fe.laminate["n_plies"],
                    "total_thickness": fe.laminate["total_thickness"],
                    "load": fe.load},
            conn=conn)
        link(mesh_id, inp_id, conn=conn)

        result["artifacts"]["mesh"] = mesh_id
        result["artifacts"]["inp"] = inp_id
        result["fe"] = {
            "n_nodes": fe.mesh_dict["n_nodes"],
            "n_elems": fe.mesh_dict["n_elems"],
            "n_plies": fe.laminate["n_plies"],
            "total_thickness": fe.laminate["total_thickness"],
            "inp_path": fe.inp_path,
        }
        result["fe_model"] = fe
        result["analysis"] = analysis_spec
        # 网格质量报告（票15）：无质量门时为 None
        q = getattr(fe, "mesh_quality", None)
        if q:
            result["mesh_quality"] = q
            if q.get("warnings"):
                result.setdefault("warnings", []).extend(q["warnings"])

    # 6) Abaqus 求解 + 结果抽取（票 05；票14 分析类型透传到适配器）
    if with_solve:
        if not with_fe_model:
            raise StudyRunError("求解需要先启用 with_fe_model")
        if abaqus is None:
            raise StudyRunError("求解需要提供 abaqus runner（Real/Fake）")

        sdir = solve_dir or (os.path.join(params_dir, "solve") if params_dir else "solve")
        if not os.path.isdir(sdir):
            os.makedirs(sdir)

        analysis_spec = result.get("analysis") or _normalize_analysis(opts.analysis)
        solve_kwargs = {}
        if analysis_spec:
            solve_kwargs["analysis"] = analysis_spec
        try:
            sol = abaqus.solve(fe.inp_path, job_name, sdir, step_name=None,
                               **solve_kwargs)
        except TypeError:
            # 兼容只实现 P1 签名的第三方 runner（协议向后兼容）
            sol = abaqus.solve(fe.inp_path, job_name, sdir, step_name=None)
        except Exception as exc:
            raise StudyRunError("Abaqus 求解失败: %s" % exc)
        if sol.status != "COMPLETED":
            raise StudyRunError("Abaqus 求解未完成(status=%s)\n%s"
                                % (sol.status, sol.log_tail))

        odb_id = add_artifact(
            study_id, "odb", content_uri=sol.odb_path,
            params={"job": job_name, "status": sol.status,
                    "analysis_kind": (analysis_spec or {}).get("kind", "static")},
            conn=conn)
        link(result["artifacts"]["inp"], odb_id, conn=conn)

        try:
            ext = abaqus.extract(sol.odb_path, sdir)
        except Exception as exc:
            raise StudyRunError("结果抽取失败: %s" % exc)

        ext_id = add_artifact(
            study_id, "extraction", content_uri=ext.csv_path,
            params={"n_rows": ext.manifest.get("n_rows"),
                    "n_section_points": ext.manifest.get("n_section_points"),
                    "section_points": ext.manifest.get("section_points"),
                    "position": ext.manifest.get("position"),
                    "source_odb_sha256": ext.manifest.get("source_odb_sha256"),
                    "abaqus_version": ext.manifest.get("abaqus_version"),
                    "python_version": ext.manifest.get("python_version")},
            conn=conn)
        link(odb_id, ext_id, conn=conn)

        result["artifacts"]["odb"] = odb_id
        result["artifacts"]["extraction"] = ext_id
        result["solve"] = {"job": job_name, "status": sol.status,
                           "odb_path": sol.odb_path}
        result["extraction"] = {
            "csv_path": ext.csv_path,
            "manifest_path": ext.manifest_path,
            "manifest": ext.manifest,
        }

    # 7) 强度校核（票 06）
    if with_check:
        if not with_solve:
            raise StudyRunError("强度校核需要先启用 with_solve")

        try:
            rule_set = strength_check.load_rules(rules_path, rules)
        except Exception as exc:
            raise StudyRunError("校核规则无效: %s" % exc)

        cdir = check_dir or (os.path.join(params_dir, "check") if params_dir else "check")
        if not os.path.isdir(cdir):
            os.makedirs(cdir)

        # 规则本身作为一个版本化产物（改规则 = 新版本）
        rules_uri = os.path.join(cdir, "rules.json")
        import json as _json
        with open(rules_uri, "w", encoding="utf-8") as fh:
            _json.dump(rule_set, fh, ensure_ascii=False, indent=2, sort_keys=True)
        rule_id = add_artifact(
            study_id, "check_rule", content_uri=rules_uri,
            params={"criteria": rule_set["criteria"],
                    "safety_factor": rule_set["safety_factor"],
                    "rules_checksum": strength_check.rules_checksum(rule_set)},
            conn=conn)

        # 单元 -> 自然格 归属（来自网格：单元按格划分，故可直接推）
        bay_of_elem = {}
        try:
            mesh = result["fe_model"].mesh
            for e in mesh.elems:
                bay_of_elem[e["id"]] = e["bay_id"]
        except Exception:
            bay_of_elem = None

        stress = strength_check.read_extraction(result["extraction"]["csv_path"], rule_set)
        chk = strength_check.check(stress, rule_set, model, bay_of_elem)
        csv_out, json_out = strength_check.write_outputs(chk, cdir)

        res_id = add_artifact(
            study_id, "check_result", content_uri=json_out,
            params={"n_detail_rows": chk["summary"]["n_detail_rows"],
                    "n_elems": chk["summary"]["n_elems"],
                    "n_bays_checked": chk["summary"]["n_bays_checked"],
                    "n_bays_excluded": chk["summary"]["n_bays_excluded"],
                    "max_fi": chk["summary"]["max_fi"],
                    "min_ms": chk["summary"]["min_ms"],
                    "all_pass": chk["summary"]["all_pass"]},
            conn=conn)
        link(result["artifacts"]["extraction"], res_id, conn=conn)
        link(rule_id, res_id, conn=conn)

        result["artifacts"]["check_rule"] = rule_id
        result["artifacts"]["check_result"] = res_id
        result["check"] = {
            "detail_csv": csv_out,
            "summary_json": json_out,
            "summary": chk["summary"],
            "hotspots": chk["hotspots"],
            "invalid_bays": chk["invalid_bays"],
            # 明细行供报告"校核明细表"使用（票 07）
            "detail_rows": chk["detail_rows"],
        }

    # 8) 强度校核报告（票 07）
    if with_report:
        if not with_check:
            raise StudyRunError("生成报告需要先启用 with_check")

        rdir = report_dir or (os.path.join(params_dir, "report") if params_dir else "report")
        if not os.path.isdir(rdir):
            os.makedirs(rdir)

        # 模板：未指定则用标准模板（不存在则生成）
        tpl_path = report_template_path
        if not tpl_path:
            repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))))
            tpl_path = os.path.join(repo_root, "data", "templates",
                                    "report_standard.docx")
            if not os.path.isfile(tpl_path):
                report_template.build_standard_template(tpl_path)

        # 危险部位图
        fig_path = os.path.join(rdir, "hotspot.png")
        fig_warnings = []
        try:
            hotspot_figure.draw_hotspot_figure(result, fig_path)
            result["hotspot_figure"] = fig_path
        except Exception as exc:
            fig_warnings.append("危险部位图生成失败: %s" % exc)
            result["hotspot_figure"] = None

        # 先把已有产物的版本/指纹收进 result，供报告"可追溯性"附录表使用
        meta = result.setdefault("artifact_meta", {})
        for kind, aid in list(result["artifacts"].items()):
            row = conn.execute(
                "SELECT version, checksum, content_uri FROM artifacts WHERE id=?",
                (aid,)).fetchone()
            if row is not None:
                meta[kind] = {"version": row["version"],
                              "checksum": row["checksum"],
                              "uri": row["content_uri"] or ""}

        # 渲染
        ctx = report_mod.build_report_context(result)
        out_path = report_out or os.path.join(rdir, "strength_report.docx")
        try:
            rr = report_mod.render_report(study_id, ctx, tpl_path, out_path,
                                          report_options=opts.report_options)
        except Exception as exc:
            raise StudyRunError("报告渲染失败: %s" % exc)

        warnings = list(fig_warnings) + list(rr.get("warnings") or [])
        rep_id = add_artifact(
            study_id, "report", content_uri=out_path,
            params={"template": os.path.basename(tpl_path),
                    "n_hotspots": len(result["check"]["hotspots"]),
                    "n_unresolved": len(rr.get("unresolved") or []),
                    "warnings": warnings},
            conn=conn)
        link(result["artifacts"]["check_result"], rep_id, conn=conn)

        result["artifacts"]["report"] = rep_id
        result["report"] = {
            "path": out_path,
            "figure": result.get("hotspot_figure"),
            "template": tpl_path,
            "warnings": warnings,
            "unresolved": rr.get("unresolved") or [],
        }

    return result
