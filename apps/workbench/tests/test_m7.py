# -*- coding: utf-8 -*-
"""
test_m7.py -- 票 07 验收：强度校核报告（.docx 模板灌数）

运行 (在 apps/workbench 下): python tests/test_m7.py
"""

import os
import sys
import shutil
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
sys.path.insert(0, CORE)

import db
import study
import run_study
import report_template
import report as report_mod
import hotspot_figure
from catia_driver import FakeCatiaDriver
from abaqus_runner import FakeAbaqusRunner


PARAMS = {"span": 5.0, "sweep_deg": 10.0, "c_root": 1.0, "c_tip": 0.6,
          "n_ribs": 3, "n_stringers": 4}


def _tmpdir(name):
    d = os.path.join(WORKBENCH, ".tmp_test", name)
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d)
    return d


def _doc_text(path):
    from docx import Document
    doc = Document(path)
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for row in t.rows:
            for c in row.cells:
                parts.append(c.text)
    return "\n".join(parts)


def test_template(fails):
    tmp = _tmpdir("m7tpl")
    path = os.path.join(tmp, "t.docx")
    report_template.build_standard_template(path)
    if not os.path.isfile(path):
        fails.append("template not generated")
        return

    from docx import Document
    try:
        doc = Document(path)
    except Exception as exc:
        fails.append("template not openable: %s" % exc)
        return

    text = _doc_text(path)
    missing = [p for p in report_template.PLACEHOLDERS if "{{%s}}" % p not in text
               and "{{ %s }}" % p not in text]
    if missing:
        fails.append("template missing placeholders: %s" % missing)


def test_render(fails):
    tmp = _tmpdir("m7render")
    tpl = os.path.join(tmp, "t.docx")
    report_template.build_standard_template(tpl)
    out = os.path.join(tmp, "r.docx")

    ctx = {
        "study_id": "STUDY-X",
        "template_id": "wingbox_v1",
        "generated_at": "2026-01-01 00:00:00",
        "summary": {"max_fi": "0.5", "min_ms": "1.0", "all_pass": "合格",
                    "n_elems": 6, "n_detail_rows": 36,
                    "n_bays_checked": 6, "n_bays_excluded": 0},
        "check": {"criteria": "tsai_wu", "safety_factor": "1.5"},
        "allowables": "Xt=1500 MPa",
        "param_table": {"span": 5.0, "n_ribs": 3},
        "hotspot_table": [
            {"elem": 5, "sp": 2, "criterion": "tsai_wu", "FI": 0.9, "MS": 0.11},
            {"elem": 3, "sp": 1, "criterion": "hashin", "FI": 0.7, "MS": 0.43},
        ],
        "invalid_bay_table": ["NB-r1-s0", "NB-r1-s1"],
        "hotspot_figure": None,
        "conclusion": "TEST-CONCLUSION",
    }

    rr = report_mod.render_report(ctx, tpl, out)
    if not os.path.isfile(out):
        fails.append("report not written")
        return

    text = _doc_text(out)
    # 文本占位符替换
    if "STUDY-X" not in text:
        fails.append("study_id not replaced")
    if "TEST-CONCLUSION" not in text:
        fails.append("conclusion not replaced")
    if "0.5" not in text:
        fails.append("summary.max_fi not replaced")
    if "合格" not in text:
        fails.append("all_pass not replaced")

    # 表格渲染
    from docx import Document
    doc = Document(out)
    table_sizes = [(len(t.rows), len(t.columns)) for t in doc.tables]
    # 期望：param_table(1+2 行) / hotspot_table(1+2) / invalid(1+2)
    if not any(r == 3 for (r, c) in table_sizes):
        fails.append("expected a 3-row table, got %s" % table_sizes)
    if len(doc.tables) < 3:
        fails.append("expected >=3 tables, got %d" % len(doc.tables))

    # 无未解析占位符
    if rr.get("unresolved"):
        fails.append("unresolved placeholders remain: %s" % rr["unresolved"])


def test_unknown_placeholder(fails):
    tmp = _tmpdir("m7unknown")
    tpl = os.path.join(tmp, "t.docx")
    report_template.build_standard_template(tpl)

    # 往模板里塞一个未知占位符
    from docx import Document
    doc = Document(tpl)
    doc.add_paragraph("未知字段：{{not.a.real.key}}")
    doc.save(tpl)

    out = os.path.join(tmp, "r.docx")
    ctx = report_mod.build_report_context(
        {"study_id": "S", "parameters": {}, "check": {"summary": {}}})
    rr = report_mod.render_report(ctx, tpl, out)

    if not any("not.a.real.key" in w for w in rr.get("warnings") or []):
        fails.append("unknown placeholder not warned: %s" % rr.get("warnings"))
    # 保留原样
    text = _doc_text(out)
    if "{{not.a.real.key}}" not in text:
        fails.append("unknown placeholder should be preserved verbatim")


def test_figure(fails):
    tmp = _tmpdir("m7fig")
    # 用 fake 全链拿到 result
    res, conn = _full_chain(tmp, "fig")
    conn.close()
    png = os.path.join(tmp, "hot.png")
    hotspot_figure.draw_hotspot_figure(res, png)
    if not os.path.isfile(png):
        fails.append("figure not written")
    else:
        if os.path.getsize(png) < 1000:
            fails.append("figure suspiciously small: %d" % os.path.getsize(png))


def _full_chain(tmp, tag, with_report=True):
    conn = db.connect(os.path.join(tmp, "db.sqlite"))
    study.create_study("S-" + tag, "M7 " + tag, conn=conn)
    res = run_study.run_study(
        "S-" + tag, PARAMS, "wingbox_v1",
        FakeCatiaDriver(os.path.join(tmp, "catparts")), conn,
        params_dir=os.path.join(tmp, "params"),
        topo_dir=os.path.join(tmp, "topo"),
        with_fe_model=True, fe_dir=os.path.join(tmp, "fe"),
        subdiv_per_bay=1,
        plies=[{"angle": 0.0, "thickness": 0.1},
               {"angle": 90.0, "thickness": 0.1}],
        abaqus=FakeAbaqusRunner(n_elems=6, n_plies=2),
        with_solve=True, job_name="job1",
        solve_dir=os.path.join(tmp, "solve"),
        with_check=True, check_dir=os.path.join(tmp, "check"),
        rules={"criteria": ["tsai_wu"]},
        with_report=with_report,
        report_dir=os.path.join(tmp, "report"))
    return res, conn


def test_full_chain_report(fails):
    tmp = _tmpdir("m7chain")
    res, conn = _full_chain(tmp, "chain")

    arts = res["artifacts"]
    if "report" not in arts:
        fails.append("report artifact missing")
    else:
        # 血缘 check_result -> report
        n = conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE parent_id=? AND child_id=?",
            (arts["check_result"], arts["report"])).fetchone()["c"]
        if n != 1:
            fails.append("lineage check_result->report missing")

    rep = res.get("report") or {}
    path = rep.get("path")
    if not path or not os.path.isfile(path):
        fails.append("report file missing: %r" % path)
    else:
        if os.path.getsize(path) < 5000:
            fails.append("report too small: %d" % os.path.getsize(path))
        # 能重新打开
        from docx import Document
        try:
            Document(path)
        except Exception as exc:
            fails.append("report not openable: %s" % exc)
        # 含图片
        with zipfile.ZipFile(path) as z:
            imgs = [n for n in z.namelist() if n.startswith("word/media/")]
        if not imgs:
            fails.append("report contains no images")
        # 关键内容
        text = _doc_text(path)
        if "强度校核报告" not in text:
            fails.append("report missing title")
        if "S-chain" not in text:
            fails.append("report missing study id")
        if rep.get("unresolved"):
            fails.append("report has unresolved placeholders: %s"
                         % rep["unresolved"])

    conn.close()


def main():
    fails = []
    print("1. template ...")
    test_template(fails)
    print("2. render ...")
    test_render(fails)
    print("3. unknown placeholder ...")
    test_unknown_placeholder(fails)
    print("4. chain + report ...")
    test_full_chain_report(fails)
    print("5. figure ...")
    test_figure(fails)

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M7 (票房07) SELF-TEST: ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
