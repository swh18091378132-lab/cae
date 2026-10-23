# -*- coding: utf-8 -*-
"""
main.py -- 本地工作台 CLI 骨架（M0）

用法（在 E:\\cae\\apps\\workbench 下）:
    python main.py demo        # 建一个演示 Study，串起 M1..M6 的产物链，打印血缘
    python main.py list        # 列出所有 Study
    python main.py lineage <study_id>

注意: P1 目标机为 Win7 + Python 3.8。本骨架只做数据/血缘层，不触发 CATIA/Abaqus。
"""

import os
import sys

# 让 core 可作为包导入
HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(HERE, "core")
if CORE not in sys.path:
    sys.path.insert(0, CORE)

from db import connect
from study import (create_study, add_artifact, link, add_decision,
                   print_lineage, list_studies)


def cmd_demo():
    sid = "STUDY-DEMO-001"
    conn = connect()
    existing = conn.execute(
        "SELECT COUNT(*) c FROM artifacts WHERE study_id=?", (sid,)).fetchone()["c"]
    if existing:
        print("(demo Study 已存在，直接打印血缘；如需重建请删除 data/cae_platform.db)")
        print_lineage(sid, conn=conn)
        conn.close()
        return
    try:
        create_study(sid, "机翼盒段 P1 演示", note="M0 骨架演示，无真实几何", conn=conn)
    except Exception:
        pass  # 已存在则复用

    # 串一条最小血缘链：参数集 -> 数模 -> 网格 -> inp -> odb -> 抽取 -> 校核结果 -> 报告
    p = add_artifact(sid, "parameter_set", content_uri="params:demo-v1",
                     params={"span": 12.0, "sweep_deg": 25.0, "ribs": 6, "stringers": 7})
    catpart = add_artifact(sid, "catpart", content_uri="catpart:wingbox_demo.CATPart",
                          params={"template": "wingbox_v1"})
    link(p, catpart)
    topo = add_artifact(sid, "topology", content_uri="topology:wingbox_demo.json",
                        params={"bays": 30})
    link(catpart, topo)
    mesh = add_artifact(sid, "mesh", content_uri="mesh:wingbox_demo.inp",
                        params={"elems": 960, "n_y": 40, "n_xi": 24})
    link(topo, mesh)
    inp = add_artifact(sid, "inp", content_uri="inp:wingbox_demo_static.inp",
                       params={"step": "static"})
    link(mesh, inp)
    odb = add_artifact(sid, "odb", content_uri="odb:wingbox_demo_static.odb",
                       params={"analysis": "S4R", "python": "2.6.2"})
    link(inp, odb)
    extr = add_artifact(sid, "extraction", content_uri="extraction:wingbox_demo.csv",
                        params={"fields": ["S", "U"], "section_points": 8})
    link(odb, extr)
    rule = add_artifact(sid, "check_rule", content_uri="rule:default_criteria.yaml",
                        params={"criteria": ["Tsai-Wu", "Hashin"]})
    res = add_artifact(sid, "check_result", content_uri="result:wingbox_demo_check.csv",
                       params={"min_ms": 0.12, "max_fi": 0.88})
    link(extr, res)
    link(rule, res)
    rep = add_artifact(sid, "report", content_uri="report:wingbox_demo.docx",
                       params={"template": "strength_report_v1"})
    link(res, rep)

    add_decision(sid, "rule_confirm", "engineer", "accept",
                 "确认默认 Tsai-Wu+Hashin 准则")
    add_decision(sid, "report_sign", "chief", "approve", "签发强度校核报告")

    print_lineage(sid)


def cmd_list():
    list_studies()


def cmd_lineage(sid):
    print_lineage(sid)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "demo":
        cmd_demo()
    elif args[0] == "list":
        cmd_list()
    elif args[0] == "lineage" and len(args) >= 2:
        cmd_lineage(args[1])
    else:
        print(__doc__)
        sys.exit(2)
