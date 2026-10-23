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
    from .study import add_artifact, link
except ImportError:
    import template_registry
    import topology
    import fe_model
    import strength_check
    from study import add_artifact, link


class StudyRunError(Exception):
    pass


def run_study(study_id, parameter_set, template_id, catia, conn,
              params_dir=None, templates_dir=None, topo_dir=None,
              holes=None, stringer_runout=None, partial_ribs=None,
              with_topology=True, with_fe_model=False, fe_dir=None,
              subdiv_per_bay=4, plies=None, material=None,
              step=None, load=None,
              abaqus=None, with_solve=False, job_name="job1", solve_dir=None,
              with_check=False, rules=None, rules_path=None, check_dir=None):
    """
    执行一次 Study。返回 dict 汇总。

    参数:
        study_id      : 已存在的 Study id
        parameter_set : 用户输入参数 dict
        template_id   : 模板 id（见 data/templates）
        catia         : CatiaDriver 实例（真机 RealCatiaDriver / 测试 FakeCatiaDriver）
        conn          : 打开的数据库连接
        topo_dir      : 拓扑 JSON 落盘目录（None 则用内联标识）
        holes / stringer_runout / partial_ribs : 拓扑缺陷输入（见 topology 模块）
        with_topology : 是否生成结构拓扑与自然格（票 03）
    """
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
        "template": tpl.id,
        "parameters": params,
        "artifacts": {"parameter_set": ps_id, "catpart": cat_id},
        "catpart_path": catpart.path,
        "catpart_checksum": catpart.checksum,
    }

    # 4) 结构拓扑模型 -> 自然格 + 不连续格（票 03）
    if with_topology:
        model = topology.build_topology(
            tpl.id, params, holes=holes,
            stringer_runout=stringer_runout, partial_ribs=partial_ribs)

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

    # 5) FE 模型构建 -> 网格 + 铺层 + 载荷/BC -> .inp（票 04）
    if with_fe_model:
        if not with_topology:
            raise StudyRunError("构建 FE 模型需要先启用 with_topology")
        try:
            fe = fe_model.build_fe_model(
                model, fe_dir or (os.path.join(params_dir, "fe") if params_dir else "fe"),
                subdiv_per_bay=subdiv_per_bay, plies=plies,
                material=material, step=step, load=load)
        except Exception as exc:
            raise StudyRunError("FE 模型构建失败: %s" % exc)

        mesh_id = add_artifact(
            study_id, "mesh", content_uri=fe.inp_path,
            params={"n_nodes": fe.mesh_dict["n_nodes"],
                    "n_elems": fe.mesh_dict["n_elems"],
                    "subdiv_per_bay": subdiv_per_bay,
                    "n_bays": len(model.bays)},
            conn=conn)
        link(result["artifacts"]["topology"], mesh_id, conn=conn)

        inp_id = add_artifact(
            study_id, "inp", content_uri=fe.inp_path,
            params={"step": fe.step.get("name"),
                    "step_type": fe.step.get("type"),
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

    # 6) Abaqus 求解 + 结果抽取（票 05）
    if with_solve:
        if not with_fe_model:
            raise StudyRunError("求解需要先启用 with_fe_model")
        if abaqus is None:
            raise StudyRunError("求解需要提供 abaqus runner（Real/Fake）")

        sdir = solve_dir or (os.path.join(params_dir, "solve") if params_dir else "solve")
        if not os.path.isdir(sdir):
            os.makedirs(sdir)

        try:
            sol = abaqus.solve(fe.inp_path, job_name, sdir, step_name=None)
        except Exception as exc:
            raise StudyRunError("Abaqus 求解失败: %s" % exc)
        if sol.status != "COMPLETED":
            raise StudyRunError("Abaqus 求解未完成(status=%s)\n%s"
                                % (sol.status, sol.log_tail))

        odb_id = add_artifact(
            study_id, "odb", content_uri=sol.odb_path,
            params={"job": job_name, "status": sol.status},
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
        }

    return result
