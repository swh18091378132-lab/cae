# -*- coding: utf-8 -*-
"""
fe_model.py -- 编排器侧的 FE 模型构建（票 04）

把 meshgen(纯算法) + abaqus_capabilities.fe_build(写 .inp) 串起来，
交由 run_study 调用，产出 .inp 并记血缘（拓扑 -> 网格 -> inp）。

注意：本模块运行在**宿主 Python (3.8)**，但它写出的 .inp 供 Abaqus 使用；
fe_build 本身保持 2.6.2 语法子集，以便将来直接在 `abaqus python` 内复用。

兼容 Python 3.8（不使用 3.9+ 语法）。
"""

import os
import sys

try:
    from . import meshgen
except ImportError:
    import meshgen


class FeModel(object):
    def __init__(self, mesh, mesh_dict, inp_path, laminate, step, load):
        self.mesh = mesh
        self.mesh_dict = mesh_dict
        self.inp_path = inp_path
        self.laminate = laminate
        self.step = step
        self.load = load


def _import_fe_build():
    """abaqus_capabilities 不在包路径内，按仓库根定位后导入。"""
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    cap_dir = os.path.join(repo_root, "abaqus_capabilities")
    if cap_dir not in sys.path:
        sys.path.insert(0, cap_dir)
    import fe_build
    return fe_build


def build_fe_model(topology_model, out_dir, subdiv_per_bay=4,
                   plies=None, material=None, step=None, load=None,
                   analysis=None, mesh_quality=None):
    """
    由结构拓扑模型构建 FE 模型并写出 .inp。

    topology_model : topology.TopologyModel
    out_dir        : .inp 落盘目录
    plies          : [{'angle','thickness','material'}]  ；None 则默认准各向同性
    material       : 材料参数；None 则默认 CFRP
    step           : {'name','type'}；默认静力
    load           : {'type','magnitude'}；默认面压
    analysis       : 分析类型声明（票14）；None 则沿用静力默认
    mesh_quality   : 网格质量门策略（票15）；None 则不启用

    返回 FeModel
    """
    fb = _import_fe_build()

    mesh = meshgen.build_mesh(topology_model, subdiv_per_bay=subdiv_per_bay)
    ok, problems = meshgen.verify_no_straddle(topology_model, mesh)
    if not ok:
        raise ValueError("网格与自然格未对齐(ADR-0010): %s" % problems[:3])

    if plies:
        laminate = fb.make_laminate(plies)
    else:
        laminate = fb.default_quasi_isotropic()
    mat = material or fb.default_material()
    st = step or {"name": "Static-1", "type": "static"}
    ld = load or {"type": "pressure", "magnitude": 0.05}

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    inp_path = os.path.join(out_dir, "model.inp")

    fb.write_inp(inp_path, mesh.to_dict(), topology_model.to_dict(),
                 laminate, mat, st, ld, {}, topology_model.params,
                 topology_model.extents(), analysis)

    return FeModel(mesh, mesh.to_dict(), inp_path, laminate, st, ld)
