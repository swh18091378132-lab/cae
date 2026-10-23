# -*- coding: utf-8 -*-
"""
run_study.py -- Study 编排器（唯一主测试切口，M1 版本）

M1 范围：参数集 -> (参数契约/包络校验) -> CATIA 再生 -> 记录血缘。
后续 M2-M6 将在此逐步接入 拓扑/网格/求解/抽取/校核/报告，接口保持向后兼容。

兼容 Python 3.8。
"""

import os

try:
    from . import template_registry
    from .study import add_artifact, link
except ImportError:
    import template_registry
    from study import add_artifact, link


class StudyRunError(Exception):
    pass


def run_study(study_id, parameter_set, template_id, catia, conn,
              params_dir=None, templates_dir=None):
    """
    执行一次 Study（M1 片段）。返回 dict 汇总。

    参数:
        study_id      : 已存在的 Study id
        parameter_set : 用户输入参数 dict
        template_id   : 模板 id（见 data/templates）
        catia         : CatiaDriver 实例（真机 RealCatiaDriver / 测试 FakeCatiaDriver）
        conn          : 打开的数据库连接
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

    return {
        "study_id": study_id,
        "template": tpl.id,
        "parameters": params,
        "artifacts": {"parameter_set": ps_id, "catpart": cat_id},
        "catpart_path": catpart.path,
        "catpart_checksum": catpart.checksum,
    }
