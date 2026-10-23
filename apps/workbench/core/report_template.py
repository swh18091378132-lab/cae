# -*- coding: utf-8 -*-
"""
report_template.py -- 生成标准 .docx 报告模板（票 07）

用户之后可替换 `data/templates/report_standard.docx` 为自有规范格式，
只要保留同样的 `{{占位符}}` 即可。

占位符清单见 PLACEHOLDERS。
兼容 Python 3.8。
"""

import os

try:
    from docx import Document
    from docx.shared import Pt
except ImportError:  # pragma: no cover
    Document = None
    Pt = None


# 全部占位符（模板生成器与渲染器共用，便于一致性测试）
# 语法：{{key}}；点号表示命名空间（{{param.span}} / {{model.n_elems}}）
PLACEHOLDERS = [
    "study_id",
    "study_name",
    "generated_at",
    "template_id",
    # 逐参数命名空间
    "param.span",
    "param.sweep_deg",
    "param.c_root",
    "param.c_tip",
    "param.n_ribs",
    "param.n_stringers",
    # 模型信息命名空间
    "model.n_bays_total",
    "model.n_bays_valid",
    "model.n_bays_invalid",
    "model.n_elems",
    "model.n_nodes",
    "model.n_plies",
    "model.total_thickness",
    # 校核依据
    "check.criteria",
    "check.safety_factor",
    "check.ms_threshold",
    "check.allowables",
    # 汇总
    "summary.max_fi",
    "summary.min_ms",
    "summary.all_pass",
    "summary.n_elems",
    "summary.n_detail_rows",
    "summary.n_bays_checked",
    "summary.n_bays_excluded",
    # 分析类型（票14）
    "analysis.kind",
    "analysis.label",
    "analysis.detail",
    # 网格质量（票15 / spec 用户故事 35）
    "mesh_quality.summary",
    "mesh_quality.warnings",
    "mesh_quality.n_warn_bays",
    "mesh_quality.n_refined_bays",
    "mesh_quality.worst_jacobian",
    "mesh_quality.max_aspect",
    "mesh_quality.max_warp",
    "mesh_quality.criteria",
    # 结构化
    "param_table",
    "detail_table",
    "hotspot_table",
    "invalid_bay_table",
    "mesh_quality_table",
    "artifact_table",
    "hotspot_figure",
    "conclusion",
]

# 标准模板版本。**新增占位符时必须递增**，否则已经存在的
# data/templates/report_standard.docx（旧版）不会被重建，
# 新章节就永远不会出现在报告里（用户看到的是老模板）。
TEMPLATE_VERSION = 2

# 标记段落：写进 docx 正文，用于识别这份模板是哪一版
VERSION_MARKER = "模板版本：v%d" % TEMPLATE_VERSION


def template_needs_rebuild(path):
    """
    判断已有的标准模板是否该重建。

    * 文件不存在 -> 需要建
    * 文件里找不到当前版本标记 -> 是旧版，需要重建
    * 版本标记匹配 -> 沿用（用户可能已把它替换成自有规范格式，
      有正确标记就说明是基于当前版本改的，不该覆盖）
    """
    if not os.path.isfile(path):
        return True
    if Document is None:
        return False
    try:
        doc = Document(path)
    except Exception:
        return True
    for para in doc.paragraphs:
        if VERSION_MARKER in (para.text or ""):
            return False
    return True


def ensure_standard_template(path):
    """
    确保标准模板存在**且是当前版本**；返回实际可用的路径。

    这是 run_study 应该调用的入口（而不是"文件不存在才建"）：
    否则 P2 新增的网格质量/分析类型章节在已有安装上永远不生效。
    """
    if template_needs_rebuild(path):
        build_standard_template(path)
    return path


def build_standard_template(path):
    """生成一版标准强度校核报告模板（含全部占位符）。"""
    if Document is None:
        raise RuntimeError("python-docx 未安装，无法生成模板")

    doc = Document()

    doc.add_heading("强度校核报告", level=0)

    p = doc.add_paragraph()
    p.add_run("分析任务(Study)：").bold = True
    p.add_run("{{study_id}}")
    p.add_run("    名称：")
    p.add_run("{{study_name}}")

    p = doc.add_paragraph()
    p.add_run("模板：").bold = True
    p.add_run("{{template_id}}")
    p.add_run("    生成时间：")
    p.add_run("{{generated_at}}")

    # 1 参数与模型
    doc.add_heading("1  参数与模型", level=1)
    doc.add_paragraph("{{param_table}}")
    p = doc.add_paragraph()
    p.add_run("展长 span = ").bold = True
    p.add_run("{{param.span}}")
    p.add_run(" m    后掠角 = ")
    p.add_run("{{param.sweep_deg}}")
    p.add_run(" deg")
    p = doc.add_paragraph()
    p.add_run("根弦长 = ").bold = True
    p.add_run("{{param.c_root}}")
    p.add_run(" m    尖弦长 = ")
    p.add_run("{{param.c_tip}}")
    p.add_run(" m    肋数 = ")
    p.add_run("{{param.n_ribs}}")
    p.add_run("    长桁数 = ")
    p.add_run("{{param.n_stringers}}")
    p = doc.add_paragraph()
    p.add_run("自然格：候选 ").bold = True
    p.add_run("{{model.n_bays_total}}")
    p.add_run("，有效 ")
    p.add_run("{{model.n_bays_valid}}")
    p.add_run("，不连续 ")
    p.add_run("{{model.n_bays_invalid}}")
    p = doc.add_paragraph()
    p.add_run("网格：节点 ").bold = True
    p.add_run("{{model.n_nodes}}")
    p.add_run("，单元 ")
    p.add_run("{{model.n_elems}}")
    p.add_run("    铺层数 ")
    p.add_run("{{model.n_plies}}")
    p.add_run("，总厚 ")
    p.add_run("{{model.total_thickness}}")

    # 2 校核依据
    doc.add_heading("2  校核依据", level=1)
    p = doc.add_paragraph()
    p.add_run("分析类型：").bold = True
    p.add_run("{{analysis.label}}")
    p.add_run("（").bold = True
    p.add_run("{{analysis.kind}}")
    p.add_run("）    说明：")
    p.add_run("{{analysis.detail}}")
    p = doc.add_paragraph()
    p.add_run("失效准则：").bold = True
    p.add_run("{{check.criteria}}")
    p = doc.add_paragraph()
    p.add_run("安全系数：").bold = True
    p.add_run("{{check.safety_factor}}")
    p.add_run("    MS 阈值：")
    p.add_run("{{check.ms_threshold}}")
    p = doc.add_paragraph()
    p.add_run("材料许用值：").bold = True
    p.add_run("{{check.allowables}}")

    # 3 校核结果汇总
    doc.add_heading("3  校核结果汇总", level=1)
    p = doc.add_paragraph()
    p.add_run("单元数：").bold = True
    p.add_run("{{summary.n_elems}}")
    p.add_run("    明细行数：")
    p.add_run("{{summary.n_detail_rows}}")
    p = doc.add_paragraph()
    p.add_run("校核自然格数：").bold = True
    p.add_run("{{summary.n_bays_checked}}")
    p.add_run("    排除的不连续格数：")
    p.add_run("{{summary.n_bays_excluded}}")
    p = doc.add_paragraph()
    p.add_run("最大失效指数 FI：").bold = True
    p.add_run("{{summary.max_fi}}")
    p = doc.add_paragraph()
    p.add_run("最小安全裕度 MS：").bold = True
    p.add_run("{{summary.min_ms}}")
    p = doc.add_paragraph()
    p.add_run("总体判定：").bold = True
    p.add_run("{{summary.all_pass}}")

    doc.add_heading("3.1  校核明细表（单元 × 铺层 × 准则）", level=2)
    doc.add_paragraph("{{detail_table}}")

    # 4 危险部位
    doc.add_heading("4  危险部位", level=1)
    doc.add_paragraph("{{hotspot_table}}")

    doc.add_heading("4.1  不连续格（已排除出自然格级校核）", level=2)
    doc.add_paragraph("{{invalid_bay_table}}")

    # 5 危险部位图
    doc.add_heading("5  危险部位图", level=1)
    doc.add_paragraph("{{hotspot_figure}}")

    # 5.1 网格质量（票15 / spec 用户故事 35：审核人须知道结论的适用边界）
    doc.add_heading("5.1  网格质量与结论适用边界", level=2)
    p = doc.add_paragraph()
    p.add_run("质量门结论：").bold = True
    p.add_run("{{mesh_quality.summary}}")
    p = doc.add_paragraph()
    p.add_run("判定阈值：").bold = True
    p.add_run("{{mesh_quality.criteria}}")
    p = doc.add_paragraph()
    p.add_run("未达标自然格数：").bold = True
    p.add_run("{{mesh_quality.n_warn_bays}}")
    p.add_run("    已自动加密格数：")
    p.add_run("{{mesh_quality.n_refined_bays}}")
    p = doc.add_paragraph()
    p.add_run("最坏指标：畸变度 ").bold = True
    p.add_run("{{mesh_quality.worst_jacobian}}")
    p.add_run("，长宽比 ")
    p.add_run("{{mesh_quality.max_aspect}}")
    p.add_run("，翘曲度 ")
    p.add_run("{{mesh_quality.max_warp}}")
    p = doc.add_paragraph()
    p.add_run("质量警告：").bold = True
    p.add_run("{{mesh_quality.warnings}}")
    doc.add_paragraph("{{mesh_quality_table}}")

    # 6 结论
    doc.add_heading("6  结论与建议", level=1)
    doc.add_paragraph("{{conclusion}}")

    # 附录 可追溯性
    doc.add_heading("附录  产物与指纹（可追溯性）", level=1)
    doc.add_paragraph("{{artifact_table}}")

    doc.add_paragraph()
    p = doc.add_paragraph()
    p.add_run("说明：本报告结论文字为 AI 辅助草稿（L1），须经人工审核签发（L2）；"
              "FI/MS 的计算与判定为确定性算法（L0），不含 AI 参与。").italic = True

    # 版本标记（供 template_needs_rebuild 识别；新增占位符时必须递增 TEMPLATE_VERSION）
    p = doc.add_paragraph()
    run = p.add_run(VERSION_MARKER)
    try:
        run.font.size = Pt(7)
    except Exception:
        pass

    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    doc.save(path)
    return path


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    target = os.path.join(repo, "data", "templates", "report_standard.docx")
    print(build_standard_template(target))
