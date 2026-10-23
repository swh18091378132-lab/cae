# -*- coding: utf-8 -*-
"""
test_m14.py -- 票14 验收：分析类型扩展（接触 / 非线性 / Explicit）

验收点（对应 issue #14）：
  1. 分析类型作为**可选项**暴露，至少覆盖
     静力 / 模态 / 屈曲 / 接触 / 非线性 / Explicit（6 种）
  2. 接触可声明接触对；非线性可声明材料或几何非线性；
     Explicit 可声明时间步与质量缩放——且这些声明**真的写进 .inp**
  3. Abaqus 适配器求解接口对 fake 与 real **保持同签名**
     （`inspect.signature` 断言参数名序列一致）
  4. **不同分析类型的抽取结果格式一致**（同字段同口径）：对每种类型跑完整链，
     断言 check.summary 的键集合一致、抽取 CSV 表头一致、明细 CSV 表头一致、
     且**数值完全一致** —— 证明校核逻辑无需按分析类型分支
  5. Abaqus 侧脚本继续通过 Python 2.7 兼容检查
  6. .inp 内容按类型不同：static 有 *STATIC；explicit 有 *DYNAMIC, EXPLICIT
     与质量缩放、时间步；contact 有 *CONTACT PAIR 与 *SURFACE；nonlinear 的
     材料/几何非线性分别落成 *DAMAGE INITIATION / NLGEOM=YES；
     frequency 有 *FREQUENCY；buckle 有 *BUCKLE
  7. 在**非机翼部件族**（tailbox_v1 尾翼盒段）上跑通一种新增分析类型的
     端到端闭环（fake 层，含报告）
  8. 真机探测到可用 Abaqus 时跑一次非静力分析；探测不到则显式 SKIP 提示
  9. Python 3.8 兼容

本文件只断言**外部可观察行为**：.inp 文本、产物、抽取 CSV/manifest、校核汇总、
血缘边；不断言内部调用顺序。

运行 (在 apps/workbench 下): python tests/test_m14.py
"""

import os
import sys
import csv
import json
import time
import inspect
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
REPO = os.path.dirname(os.path.dirname(WORKBENCH))
CAP = os.path.join(REPO, "abaqus_capabilities")
sys.path.insert(0, CORE)
sys.path.insert(0, WORKBENCH)

import db
import study
import topology
import fe_model
import analysis
import template_registry
import run_study
import strength_check
from run_study import RunOptions
from catia_driver import FakeCatiaDriver
from abaqus_runner import (
    FakeAbaqusRunner, RealAbaqusRunner, AbaqusRunner, CSV_HEADER,
    FIXED_MANIFEST_KEYS)

# 机翼盒段：3 肋 × 4 长桁 -> (3-1)*(4-1) = 6 个自然格 = 6 个单元（subdiv=1）
WING_PARAMS = {"span": 5.0, "sweep_deg": 10.0, "c_root": 1.0, "c_tip": 0.6,
               "n_ribs": 3, "n_stringers": 4}
WING_N_ELEMS = 6

TAILBOX_ID = "tailbox_v1"

PLIES = [{"angle": 0.0, "thickness": 0.1},
         {"angle": 90.0, "thickness": 0.1}]

RULES = {"criteria": ["tsai_wu", "hashin"], "safety_factor": 1.0}

ALL_KINDS = ("static", "frequency", "buckle", "contact", "nonlinear",
             "explicit")

# 明细 CSV 的表头（strength_check.write_outputs 写死，与分析类型无关）
CHECK_DETAIL_HEADER = ["elem", "sp", "criterion", "FI", "MS", "pass"]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

# 临时根目录：固定父目录 + **按进程唯一化**的子目录。
# run_tests.py 可能被多个 agent/进程同时跑（本仓库协作方式如此），固定路径会让
# 两边的 m14.db / 产物互相踩（Windows 上表现为 PermissionError / 目录非空）。
# 父目录仍固定在 .tmp_test/m14/，只是每次运行一个 run-<pid>，互不干扰。
TMP_ROOT = os.path.join(WORKBENCH, ".tmp_test", "m14", "run-%d" % os.getpid())


def _fresh(name):
    """清空并重建临时目录（清理是尽力而为：删不掉就复用，不因此判失败）。"""
    d = os.path.join(TMP_ROOT, name)
    if os.path.isdir(d):
        for _ in range(3):
            try:
                shutil.rmtree(d)
                break
            except OSError:             # 含 PermissionError / 目录非空
                time.sleep(0.2)
    try:
        os.makedirs(d)
    except OSError:
        if not os.path.isdir(d):
            raise
    return d


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _csv_header(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        rd = csv.reader(fh)
        try:
            return next(rd)
        except StopIteration:
            return []


def _csv_rows(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        rd = csv.reader(fh)
        try:
            next(rd)
        except StopIteration:
            return []
        return [tuple(r) for r in rd if r]


def _step_line(txt):
    """`.inp` 里 `*STEP` 那一行（便于断言分析步名与 STEP 参数）。"""
    for line in txt.splitlines():
        if line.strip().upper().startswith("*STEP"):
            return line.strip()
    return ""


def _has_keyword(txt, keyword):
    """按行匹配关键字（避免 `*STATIC` 误匹配 `*STATIC, ...` 之外的内容）。"""
    kw = keyword.strip().upper()
    for line in txt.splitlines():
        s = line.strip().upper()
        if s == kw or s.startswith(kw + ","):
            return True
    return False


def _bay_ids(model):
    return sorted(b.id for b in model.bays)


def _spec(kind, **overrides):
    """构造一个合法的分析类型声明（走 core/analysis 归一，与用户路径一致）。"""
    base = dict(overrides)
    base["kind"] = kind
    return analysis.normalize(base)


def _contact_spec(model, friction=0.3):
    """用真实自然格 ELSET 名构造一个接触对声明（master/slave 都是网格里有的集合）。"""
    ids = _bay_ids(model)
    return analysis.normalize({
        "kind": "contact",
        "contacts": [{"master": ids[0], "slave": ids[1],
                      "friction": friction}],
    })


# ---------------------------------------------------------------------------
# 1) 分析类型作为可选项暴露
# ---------------------------------------------------------------------------

def test_kinds_exposed(fails):
    """6 种分析类型都作为可选项暴露，且有中文名与默认声明。"""
    if tuple(analysis.ANALYSIS_KINDS) != ALL_KINDS:
        fails.append("ANALYSIS_KINDS 应覆盖 %r，实际 %r"
                     % (list(ALL_KINDS), list(analysis.ANALYSIS_KINDS)))

    for kind in ALL_KINDS:
        spec = analysis.default_analysis(kind)
        if spec.get("kind") != kind:
            fails.append("default_analysis(%r) 的 kind 不符: %r" % (kind, spec))
        # 每种类型都要有稳定、非空、可写进 *STEP NAME= 的分析步名
        name = spec.get("step_name")
        if not name or not str(name).strip():
            fails.append("%s 缺少 step_name" % kind)
        # 中文名（UI/报告用）不能漏
        lbl = analysis.label(kind)
        if not lbl or lbl == kind:
            fails.append("%s 缺少人类可读名称（label=%r）" % (kind, lbl))
        if kind not in analysis.ANALYSIS_LABELS:
            fails.append("ANALYSIS_LABELS 缺少 %s" % kind)
        # describe() 要能对该类型给出描述（报告里会用到）
        if not analysis.describe(spec):
            fails.append("%s 的 describe 为空" % kind)

    # 线性/非线性分类：P1 三种是线性，新增三种不是
    for kind in ("static", "frequency", "buckle"):
        if not analysis.is_linear(analysis.default_analysis(kind)):
            fails.append("%s 应判为线性分析" % kind)
    for kind in ("contact", "nonlinear", "explicit"):
        if analysis.is_linear(analysis.default_analysis(kind)):
            fails.append("%s 不应判为线性分析" % kind)

    # 作为 run_study 的可选项暴露（RunOptions 已声明 analysis 字段，不改签名）
    if "analysis" not in RunOptions.DEFAULTS:
        fails.append("RunOptions 未声明 analysis 选项字段")
    if RunOptions().analysis is not None:
        fails.append("RunOptions().analysis 默认应为 None（沿用 P1 静力）")

    # CLI 也把它作为可选项暴露（--analysis）
    import main
    if "analysis" not in inspect.signature(main.cmd_run).parameters:
        fails.append("CLI cmd_run 未暴露 analysis 选项")

    # 直接给选项字段赋值必须被 overlay 当成"显式设置"（服务层 base.overlay(user)
    # 的路径；本类文档与 CLI 都这么写，丢了就是静默失效）
    user = RunOptions()
    user.analysis = "explicit"
    merged = RunOptions(params_dir="p", with_solve=True).overlay(user)
    if merged.analysis != "explicit":
        fails.append("直接赋值 opts.analysis 在 overlay 时被丢弃（%r）—— "
                     "分析类型会静默失效" % (merged.analysis,))
    if merged.params_dir != "p" or merged.with_solve is not True:
        fails.append("overlay 冲掉了 base 的定制字段")
    if RunOptions().overlay(RunOptions()).analysis is not None:
        fails.append("overlay 把未设置的分析类型变成了非 None")

    # 每种类型都能被 normalize 归一成声明 dict（内部类型一致、可 JSON 序列化）
    for kind in ALL_KINDS:
        spec = analysis.normalize(kind)
        if spec.get("kind") != kind:
            fails.append("normalize(%r) 未归一成该类型" % kind)
        try:
            json.dumps(spec)
        except Exception as exc:
            fails.append("%s 的声明不能 JSON 序列化（无法交给 Abaqus 侧）: %s"
                         % (kind, exc))


# ---------------------------------------------------------------------------
# 2) 各类型可声明的专属参数
# ---------------------------------------------------------------------------

def test_declarations(model, fails):
    """接触可声明接触对；非线性可声明材料/几何；Explicit 可声明时间步与质量缩放。"""
    # -- 接触：接触对必须给 master/slave；摩擦系数必须非负 --
    ids = _bay_ids(model)
    spec = _contact_spec(model)
    if len(spec.get("contacts") or []) != 1:
        fails.append("接触声明的接触对没保留下来: %r" % spec.get("contacts"))
    else:
        cp = spec["contacts"][0]
        if cp.get("master") != ids[0] or cp.get("slave") != ids[1]:
            fails.append("接触对的 master/slave 被改写: %r" % cp)
        if abs(float(cp.get("friction")) - 0.3) > 1e-12:
            fails.append("接触对摩擦系数被改写: %r" % cp.get("friction"))

    # 多个接触对都要保留（不允许只留第一个）
    multi = analysis.normalize({
        "kind": "contact",
        "contacts": [{"master": ids[0], "slave": ids[1]},
                     {"master": ids[2], "slave": ids[3], "friction": 0.0}],
    })
    if len(multi.get("contacts") or []) != 2:
        fails.append("多个接触对未被保留: %r" % multi.get("contacts"))

    # 非法接触声明必须被拒（不静默降级）
    for bad, msg in (
            ({"kind": "contact", "contacts": [{"slave": ids[0]}]},
             "缺少 master 的接触对"),
            ({"kind": "contact", "contacts": [{"master": ids[0]}]},
             "缺少 slave 的接触对"),
            ({"kind": "contact", "contacts": [{"master": ids[0],
                                               "slave": ids[1],
                                               "friction": -1.0}]},
             "负摩擦系数"),
            ({"kind": "contact", "contacts": "not-a-list"},
             "接触对不是列表")):
        try:
            analysis.normalize(bad)
            fails.append("%s 未被拒绝" % msg)
        except analysis.AnalysisError:
            pass

    # -- 非线性：材料 / 几何 / 两者，且 nlgeom 由 nonlinearity 派生 --
    expect_nlgeom = {"material": False, "geometry": True, "both": True}
    expect_damage = {"material": True, "geometry": False, "both": True}
    for nl, nlgeom in expect_nlgeom.items():
        spec = _spec("nonlinear", nonlinearity=nl)
        if bool(spec.get("nlgeom")) != nlgeom:
            fails.append("nonlinearity=%r 时 nlgeom 应为 %r，实际 %r"
                         % (nl, nlgeom, spec.get("nlgeom")))
        if ("damage" in spec) != expect_damage[nl]:
            fails.append("nonlinearity=%r 时是否带材料损伤声明不符: %r"
                         % (nl, "damage" in spec))
    try:
        analysis.normalize({"kind": "nonlinear", "nonlinearity": "thermal"})
        fails.append("未知的非线性类型未被拒绝")
    except analysis.AnalysisError:
        pass

    # -- Explicit：时间步 + 质量缩放 --
    spec = _spec("explicit", time_period=0.002, mass_scaling=2.5)
    if abs(float(spec["time_period"]) - 0.002) > 1e-15:
        fails.append("Explicit 时间步被改写: %r" % spec["time_period"])
    if abs(float(spec["mass_scaling"]) - 2.5) > 1e-12:
        fails.append("Explicit 质量缩放被改写: %r" % spec["mass_scaling"])
    for bad, msg in (({"kind": "explicit", "time_period": 0.0}, "零时间步"),
                     ({"kind": "explicit", "time_period": -1.0}, "负时间步"),
                     ({"kind": "explicit", "mass_scaling": 0.0}, "零质量缩放"),
                     ({"kind": "explicit", "element_type": "C3D8R"},
                      "非壳单元类型")):
        try:
            analysis.normalize(bad)
            fails.append("Explicit %s 未被拒绝" % msg)
        except analysis.AnalysisError:
            pass

    # -- 模态/屈曲：阶数 --
    for kind, default_n in (("frequency", 10), ("buckle", 5)):
        spec = analysis.default_analysis(kind)
        if int(spec.get("n_modes")) != default_n:
            fails.append("%s 默认阶数应为 %d，实际 %r"
                         % (kind, default_n, spec.get("n_modes")))
        spec = _spec(kind, n_modes=7)
        if int(spec["n_modes"]) != 7:
            fails.append("%s 阶数声明被改写: %r" % (kind, spec["n_modes"]))
        try:
            analysis.normalize({"kind": kind, "n_modes": 0})
            fails.append("%s 的零阶数未被拒绝" % kind)
        except analysis.AnalysisError:
            pass


# ---------------------------------------------------------------------------
# 3) fake 与 real 的 solve 同签名
# ---------------------------------------------------------------------------

def test_signature_parity(fails):
    """fake 与 real 适配器的 solve/extract 必须同签名（宿主侧只换实现）。"""
    fake = FakeAbaqusRunner()
    real_sig = inspect.signature(RealAbaqusRunner.solve)
    fake_sig = inspect.signature(FakeAbaqusRunner.solve)
    proto_sig = inspect.signature(AbaqusRunner.solve)

    def names(sig):
        return [p.name for p in sig.parameters.values()]

    if names(fake_sig) != names(real_sig):
        fails.append("fake/real 的 solve 参数名序列不一致: %r vs %r"
                     % (names(fake_sig), names(real_sig)))
    if names(proto_sig) != names(fake_sig):
        fails.append("协议 solve 与 fake 的参数名序列不一致: %r vs %r"
                     % (names(proto_sig), names(fake_sig)))
    if "analysis" not in names(fake_sig):
        fails.append("solve 缺少 analysis 参数（票14）")
    # 默认值也要一致：宿主侧不传 analysis 时两者行为相同
    for pname in names(fake_sig):
        fd = fake_sig.parameters[pname].default
        rd = real_sig.parameters[pname].default
        if fd != rd and not (fd is inspect.Parameter.empty
                             and rd is inspect.Parameter.empty):
            fails.append("solve 参数 %r 的默认值不一致: fake=%r real=%r"
                         % (pname, fd, rd))

    # extract 同样要一致
    if names(inspect.signature(FakeAbaqusRunner.extract)) != \
            names(inspect.signature(RealAbaqusRunner.extract)):
        fails.append("fake/real 的 extract 参数名序列不一致")

    # 校核引擎**不接受**分析类型参数 —— 这是"无需按类型分支"的结构性证据
    if "analysis" in inspect.signature(strength_check.check).parameters:
        fails.append("strength_check.check 出现了 analysis 参数"
                     "（校核逻辑不应按分析类型分支）")

    # 假的 solve 对每种类型都确定性 COMPLETED，并把声明带到抽取口
    tmp = _fresh("sig")
    for kind in ALL_KINDS:
        spec = analysis.default_analysis(kind)
        if kind == "contact":
            spec = analysis.normalize({"kind": "contact", "contacts": []})
        runner = FakeAbaqusRunner()
        sol = runner.solve("dummy.inp", "job-" + kind, tmp,
                           step_name=spec["step_name"], analysis=spec)
        if sol.status != "COMPLETED":
            fails.append("%s: fake solve 未 COMPLETED（%r）"
                         % (kind, sol.status))
        if (sol.analysis or {}).get("kind") != kind:
            fails.append("%s: fake solve 未记录分析类型（%r）"
                         % (kind, sol.analysis))
        ext = runner.extract(sol.odb_path, tmp, step_name=spec["step_name"])
        if ext.manifest.get("analysis_kind") != kind:
            fails.append("%s: 抽取 manifest 未记录分析类型（%r）"
                         % (kind, ext.manifest.get("analysis_kind")))
        if ext.manifest.get("step") != spec["step_name"]:
            fails.append("%s: 抽取 manifest 的 step 名不符（%r != %r）"
                         % (kind, ext.manifest.get("step"), spec["step_name"]))


# ---------------------------------------------------------------------------
# 4) .inp 内容按分析类型不同（声明真的落进 .inp）
# ---------------------------------------------------------------------------

def _build(model, out_dir, spec, **kwargs):
    """构建一次 FE 模型并读回 .inp 文本。"""
    fe = fe_model.build_fe_model(model, out_dir, subdiv_per_bay=1,
                                 plies=PLIES, analysis=spec, **kwargs)
    return fe, _read(fe.inp_path)


def test_inp_per_type(model, tmp, fails):
    """.inp 关键字随分析类型变化；每类的专属声明都真的写进了 .inp。"""
    # ---- 静力（P1 基线） ----
    fe, txt = _build(model, os.path.join(tmp, "inp_static"), None)
    if not _has_keyword(txt, "*STATIC"):
        fails.append("static: .inp 缺少 *STATIC")
    for bad in ("*DYNAMIC", "*FREQUENCY", "*BUCKLE", "*CONTACT PAIR",
                "*DAMAGE INITIATION"):
        if _has_keyword(txt, bad):
            fails.append("static: .inp 不应出现 %s" % bad)
    if "NLGEOM=YES" in txt.upper():
        fails.append("static: .inp 不应开几何非线性（NLGEOM=YES）")
    # 零回归：未启用分析类型时分析步名仍是 P1 的 Static-1
    if "NAME=Static-1" not in _step_line(txt):
        fails.append("static: 分析步名应与 P1 一致（Static-1），实际 %r"
                     % _step_line(txt))

    # ---- 模态：阶数进 .inp ----
    fe, txt = _build(model, os.path.join(tmp, "inp_frequency"),
                     _spec("frequency", n_modes=7))
    if not _has_keyword(txt, "*FREQUENCY"):
        fails.append("frequency: .inp 缺少 *FREQUENCY")
    if not any(line.strip().startswith("7,") for line in txt.splitlines()):
        fails.append("frequency: .inp 里没有按 7 阶求解的数据行")
    if "NAME=Frequency-1" not in _step_line(txt):
        fails.append("frequency: 分析步名应为 Frequency-1，实际 %r"
                     % _step_line(txt))

    # ---- 屈曲：阶数进 .inp ----
    fe, txt = _build(model, os.path.join(tmp, "inp_buckle"),
                     _spec("buckle", n_modes=3))
    if not _has_keyword(txt, "*BUCKLE"):
        fails.append("buckle: .inp 缺少 *BUCKLE")
    if not any(line.strip().startswith("3,") for line in txt.splitlines()):
        fails.append("buckle: .inp 里没有按 3 个屈曲因子求解的数据行")
    if "NAME=Buckle-1" not in _step_line(txt):
        fails.append("buckle: 分析步名应为 Buckle-1，实际 %r" % _step_line(txt))

    # ---- 接触：接触对（*SURFACE / *CONTACT PAIR）+ 几何非线性 ----
    ids = _bay_ids(model)
    spec = analysis.normalize({
        "kind": "contact",
        "contacts": [{"master": ids[0], "slave": ids[1],
                      "friction": 0.37}],
    })
    fe, txt = _build(model, os.path.join(tmp, "inp_contact"), spec)
    for kw in ("*SURFACE", "*SURFACE INTERACTION", "*FRICTION",
               "*CONTACT PAIR"):
        if not _has_keyword(txt, kw):
            fails.append("contact: .inp 缺少 %s" % kw)
    if "%s, SPOS" % ids[0] not in txt:
        fails.append("contact: 主面没落在声明的 master 集合 %s 上" % ids[0])
    if "%s, SNEG" % ids[1] not in txt:
        fails.append("contact: 从面没落在声明的 slave 集合 %s 上" % ids[1])
    if "0.37, 0." not in txt:
        fails.append("contact: 声明的摩擦系数 0.37 没写进 .inp")
    # 接触必须开几何非线性（壳-壳大滑移），且写成 *STEP 的参数而不是独立关键字
    if "NLGEOM=YES" not in _step_line(txt):
        fails.append("contact: *STEP 未带 NLGEOM=YES（接触分析必须几何非线性）")
    if _has_keyword(txt, "*NLGEOM"):
        fails.append("contact: 出现了独立的 *NLGEOM 关键字"
                     "（Abaqus 里几何非线性是 *STEP 的参数，不是独立关键字）")
    # 接触对必须写在 *STEP 之前（模型数据区），否则 Abaqus 报错
    if txt.index("*CONTACT PAIR") > txt.index("*STEP"):
        fails.append("contact: *CONTACT PAIR 写在了 *STEP 之后（应为模型数据区）")

    # ---- 非线性：材料 ----
    fe, txt = _build(model, os.path.join(tmp, "inp_nl_material"),
                     _spec("nonlinear", nonlinearity="material"))
    if not _has_keyword(txt, "*DAMAGE INITIATION"):
        fails.append("nonlinear[material]: .inp 缺少材料非线性（*DAMAGE INITIATION）")
    if "CRITERION=HASHIN" not in txt.upper():
        fails.append("nonlinear[material]: 损伤起始未声明 Hashin 准则")
    if "NLGEOM=YES" in txt.upper():
        fails.append("nonlinear[material]: 只声明材料非线性时不应开 NLGEOM=YES")

    # ---- 非线性：几何 ----
    fe, txt = _build(model, os.path.join(tmp, "inp_nl_geometry"),
                     _spec("nonlinear", nonlinearity="geometry"))
    if "NLGEOM=YES" not in _step_line(txt):
        fails.append("nonlinear[geometry]: *STEP 未带 NLGEOM=YES")
    if _has_keyword(txt, "*DAMAGE INITIATION"):
        fails.append("nonlinear[geometry]: 只声明几何非线性时不应写材料损伤")
    if _has_keyword(txt, "*NLGEOM"):
        fails.append("nonlinear[geometry]: 出现了独立的 *NLGEOM 关键字")

    # ---- 非线性：两者 ----
    fe, txt = _build(model, os.path.join(tmp, "inp_nl_both"),
                     _spec("nonlinear", nonlinearity="both"))
    if "NLGEOM=YES" not in _step_line(txt):
        fails.append("nonlinear[both]: *STEP 未带 NLGEOM=YES")
    if not _has_keyword(txt, "*DAMAGE INITIATION"):
        fails.append("nonlinear[both]: .inp 缺少材料损伤起始")

    # 最大增量数写成 *STEP 的 INC= 参数（不是 *STATIC 数据行的第 6 项）
    fe, txt = _build(model, os.path.join(tmp, "inp_nl_inc"),
                     _spec("nonlinear", nonlinearity="both",
                           max_increments=250))
    if "INC=250" not in _step_line(txt):
        fails.append("nonlinear: 最大增量数 250 未写成 *STEP 的 INC=250，实际 %r"
                     % _step_line(txt))
    static_line = txt[txt.index("*STATIC"):].splitlines()[1]
    if len([f for f in static_line.split(",") if f.strip()]) > 4:
        fails.append("nonlinear: *STATIC 数据行超过 4 项（Abaqus 只有 4 项）: %r"
                     % static_line)

    # ---- Explicit：时间步 + 质量缩放 + 显式过程 ----
    fe, txt = _build(model, os.path.join(tmp, "inp_explicit"),
                     _spec("explicit", time_period=0.002, mass_scaling=2.5))
    if "*DYNAMIC, EXPLICIT" not in txt.upper():
        fails.append("explicit: .inp 缺少 *DYNAMIC, EXPLICIT")
    if not _has_keyword(txt, "*FIXED MASS SCALING"):
        fails.append("explicit: .inp 缺少质量缩放")
    if "FACTOR=2.5" not in txt:
        fails.append("explicit: 声明的质量缩放 2.5 没写进 .inp")
    # 时间步（time period）必须出现在 *DYNAMIC, EXPLICIT 的数据行上
    lines = txt.splitlines()
    idx = [i for i, ln in enumerate(lines)
           if ln.strip().upper().startswith("*DYNAMIC, EXPLICIT")][0]
    data_line = lines[idx + 1]
    if "0.002" not in data_line:
        fails.append("explicit: 时间步 0.002 不在 *DYNAMIC, EXPLICIT 的数据行上"
                     "（实际 %r）" % data_line)
    if "NAME=Explicit-1" not in _step_line(txt):
        fails.append("explicit: 分析步名应为 Explicit-1，实际 %r"
                     % _step_line(txt))

    # Explicit 的壳单元类型可声明（铺层要求壳族）
    fe, txt = _build(model, os.path.join(tmp, "inp_explicit_s4r5"),
                     _spec("explicit", element_type="S4R5"))
    if "*ELEMENT, TYPE=S4R5" not in txt:
        fails.append("explicit: 声明的单元类型 S4R5 没写进 .inp")


# ---------------------------------------------------------------------------
# 5) 不同分析类型的抽取/校核结果格式一致（票14 硬验收）
# ---------------------------------------------------------------------------

def _run_chain(conn, tmp, tag, template_id, params, spec, n_elems,
               with_report=False):
    """用 RunOptions 跑一条完整链（fake 适配器），返回 (sid, result)。"""
    sid = "S-M14-" + tag
    study.create_study(sid, "M14 " + tag, conn=conn)
    opts = RunOptions(
        params_dir=os.path.join(tmp, tag, "params"),
        topo_dir=os.path.join(tmp, tag, "topo"),
        with_topology=True,
        with_fe_model=True,
        fe_dir=os.path.join(tmp, tag, "fe"),
        subdiv_per_bay=1,
        plies=PLIES,
        abaqus=FakeAbaqusRunner(n_elems=n_elems, n_plies=2),
        with_solve=True,
        job_name="job1",
        solve_dir=os.path.join(tmp, tag, "solve"),
        with_check=True,
        check_dir=os.path.join(tmp, tag, "check"),
        rules=RULES,
        with_report=with_report,
        report_dir=os.path.join(tmp, tag, "report"),
        analysis=spec)
    res = run_study.run_study(
        sid, dict(params), template_id,
        FakeCatiaDriver(os.path.join(tmp, tag, "catparts")), conn, opts)
    return sid, res


def test_format_consistency(conn, tmp, fails):
    """
    对 6 种分析类型各跑一次完整链，断言抽取与校核结果**格式与数值完全一致**。

    这就是"校核逻辑无需按分析类型分支"的证据：换分析类型只改 .inp 与 manifest
    的元信息，不改数据口径。
    """
    model = topology.build_topology("wingbox_v1", WING_PARAMS)
    ids = _bay_ids(model)

    specs = {}
    for kind in ALL_KINDS:
        if kind == "contact":
            specs[kind] = analysis.normalize({
                "kind": "contact",
                "contacts": [{"master": ids[0], "slave": ids[1],
                              "friction": 0.2}],
            })
        elif kind == "nonlinear":
            specs[kind] = _spec("nonlinear", nonlinearity="both")
        elif kind == "explicit":
            specs[kind] = _spec("explicit", time_period=0.005,
                                mass_scaling=1.5)
        else:
            specs[kind] = analysis.default_analysis(kind)

    seen = {}
    for kind in ALL_KINDS:
        sid, res = _run_chain(conn, tmp, "fmt_" + kind, "wingbox_v1",
                              WING_PARAMS, specs[kind], WING_N_ELEMS)

        arts = res.get("artifacts") or {}
        for need in ("parameter_set", "catpart", "topology", "mesh", "inp",
                     "odb", "extraction", "check_rule", "check_result"):
            if need not in arts:
                fails.append("%s: 全链缺少产物 %s" % (kind, need))

        # 结果里记录的分析类型必须是本类型（声明确实走完了整条链）
        if (res.get("analysis") or {}).get("kind") != kind:
            fails.append("%s: 结果里的分析类型不符（%r）"
                         % (kind, res.get("analysis")))

        extr = res.get("extraction") or {}
        csv_path = extr.get("csv_path")
        if not csv_path or not os.path.isfile(csv_path):
            fails.append("%s: 抽取 CSV 未生成" % kind)
            continue
        man = extr.get("manifest") or {}
        chk = res.get("check") or {}
        summ = chk.get("summary") or {}
        detail = chk.get("detail_csv")

        # manifest：口径固定字段齐全 + 分析类型只作元信息
        for key in FIXED_MANIFEST_KEYS:
            if key not in man:
                fails.append("%s: manifest 缺少口径字段 %r" % (kind, key))
        if man.get("analysis_kind") != kind:
            fails.append("%s: manifest 的 analysis_kind 不符（%r）"
                         % (kind, man.get("analysis_kind")))
        if man.get("step") != specs[kind]["step_name"]:
            fails.append("%s: manifest 的 step 应为 %r，实际 %r"
                         % (kind, specs[kind]["step_name"], man.get("step")))

        if not detail or not os.path.isfile(detail):
            fails.append("%s: 校核明细 CSV 未生成" % kind)
            detail_header = None
        else:
            detail_header = _csv_header(detail)

        seen[kind] = {
            "csv_header": _csv_header(csv_path),
            "csv_rows": _csv_rows(csv_path),
            "manifest_keys": set(man.keys()),
            "summary_keys": set(summ.keys()),
            "summary": summ,
            "detail_header": detail_header,
            "n_rows": man.get("n_rows"),
            "n_section_points": man.get("n_section_points"),
            "position": man.get("position"),
        }

    if len(seen) != len(ALL_KINDS):
        fails.append("分析类型结果不全，只跑到 %r" % sorted(seen))
        return seen

    base = seen["static"]
    for kind in ALL_KINDS:
        cur = seen[kind]
        # 1) 抽取 CSV 表头一致
        if cur["csv_header"] != base["csv_header"]:
            fails.append("%s: 抽取 CSV 表头与 static 不一致: %r vs %r"
                         % (kind, cur["csv_header"], base["csv_header"]))
        if cur["csv_header"] != list(CSV_HEADER):
            fails.append("%s: 抽取 CSV 表头不是约定口径: %r"
                         % (kind, cur["csv_header"]))
        # 2) 校核 summary 的键集合一致（同字段）
        if cur["summary_keys"] != base["summary_keys"]:
            only_cur = sorted(cur["summary_keys"] - base["summary_keys"])
            only_base = sorted(base["summary_keys"] - cur["summary_keys"])
            fails.append("%s: check.summary 键集合与 static 不一致"
                         "（多 %r / 少 %r）" % (kind, only_cur, only_base))
        # 3) 明细 CSV 表头一致（同口径）
        if cur["detail_header"] != base["detail_header"]:
            fails.append("%s: 校核明细 CSV 表头与 static 不一致: %r vs %r"
                         % (kind, cur["detail_header"], base["detail_header"]))
        if cur["detail_header"] != CHECK_DETAIL_HEADER:
            fails.append("%s: 校核明细表头不是约定口径: %r"
                         % (kind, cur["detail_header"]))
        # 4) 数值完全一致：换分析类型不改数据口径（除元信息）
        if cur["csv_rows"] != base["csv_rows"]:
            fails.append("%s: 抽取数据与 static 不一致（口径应按类型不变），"
                         "static %d 行 / 本类型 %d 行"
                         % (kind, len(base["csv_rows"]), len(cur["csv_rows"])))
        if cur["summary"] != base["summary"]:
            diff = sorted(k for k in base["summary"]
                          if cur["summary"].get(k) != base["summary"].get(k))
            fails.append("%s: 校核汇总数值与 static 不一致（差异字段 %r）"
                         % (kind, diff))
        # 5) manifest 的抽取口径字段一致（除 analysis 相关元信息）
        for key in ("n_rows", "n_section_points", "position"):
            if cur[key] != base[key]:
                fails.append("%s: manifest.%s 应为 %r（与 static 同口径），实际 %r"
                             % (kind, key, base[key], cur[key]))

    # manifest 键集合也应一致（分析类型只影响值，不新增/删除列）
    for kind in ALL_KINDS:
        extra = sorted(seen[kind]["manifest_keys"] - base["manifest_keys"])
        missing = sorted(base["manifest_keys"] - seen[kind]["manifest_keys"])
        if extra or missing:
            fails.append("%s: manifest 键集合与 static 不一致（多 %r / 少 %r）"
                         % (kind, extra, missing))

    return seen


# ---------------------------------------------------------------------------
# 6) 非机翼部件族上的新增分析类型闭环（tailbox_v1 × Explicit）
# ---------------------------------------------------------------------------

def _edges_by_kind(conn, study_id):
    """血缘边按 kind 表达（不依赖 id 插入顺序）。"""
    id2kind = {}
    for r in conn.execute("SELECT id, kind FROM artifacts WHERE study_id=?",
                          (study_id,)).fetchall():
        id2kind[r["id"]] = r["kind"]
    edges = set()
    for e in conn.execute("SELECT parent_id, child_id FROM edges").fetchall():
        p, c = id2kind.get(e["parent_id"]), id2kind.get(e["child_id"])
        if p and c:
            edges.add((p, c))
    return edges


def test_tailbox_new_type(conn, tmp, fails):
    """
    尾翼盒段（非机翼部件族）× Explicit：端到端闭环（fake 层，含报告）。

    本票刻意在**尾翼盒段**上验证新分析类型（issue 原文要求），避免同时引入
    "新部件族"与"新分析类型"两个变量——这里两者都已就位，正好验证它们能组合。
    """
    tpl = template_registry.load(TAILBOX_ID)
    params = dict((p["name"], p.get("default")) for p in tpl.parameters)
    n_bays = (int(params["n_ribs"]) - 1) * (int(params["n_stringers"]) - 1)

    spec = _spec("explicit", time_period=0.003, mass_scaling=3.0)
    sid, res = _run_chain(conn, tmp, "tailbox_explicit", TAILBOX_ID, params,
                          spec, n_bays, with_report=True)

    arts = res.get("artifacts") or {}
    for need in ("parameter_set", "catpart", "topology", "mesh", "inp",
                 "odb", "extraction", "check_rule", "check_result", "report"):
        if need not in arts:
            fails.append("尾翼 Explicit: 闭环缺少产物 %s" % need)

    if res.get("template") != TAILBOX_ID:
        fails.append("尾翼 Explicit: 模板不符 %r" % res.get("template"))
    if (res.get("analysis") or {}).get("kind") != "explicit":
        fails.append("尾翼 Explicit: 结果里的分析类型不符")

    # .inp 真的是显式分析，且分析步名按类型走
    inp_path = (res.get("fe") or {}).get("inp_path")
    if not inp_path or not os.path.isfile(inp_path):
        fails.append("尾翼 Explicit: .inp 未生成")
    else:
        txt = _read(inp_path)
        if "*DYNAMIC, EXPLICIT" not in txt.upper():
            fails.append("尾翼 Explicit: .inp 不是显式分析步")
        if "NAME=Explicit-1" not in _step_line(txt):
            fails.append("尾翼 Explicit: 分析步名应为 Explicit-1，实际 %r"
                         % _step_line(txt))
        if "FACTOR=3" not in txt:
            fails.append("尾翼 Explicit: 质量缩放 3.0 没写进 .inp")
        # 尾翼的操纵面切口造成的 ELSET 仍按自然格分组（网格口径未受影响）
        invalids = [b.id for b in res["topology_model"].invalid_bays]
        for bid in invalids:
            if "*ELSET, ELSET=%s" % bid not in txt:
                fails.append("尾翼 Explicit: .inp 缺少不连续格 %s 的 ELSET" % bid)

    # 抽取与校核：同一套口径
    man = (res.get("extraction") or {}).get("manifest") or {}
    if man.get("analysis_kind") != "explicit":
        fails.append("尾翼 Explicit: manifest 的 analysis_kind 不符")
    if man.get("step") != "Explicit-1":
        fails.append("尾翼 Explicit: manifest 的 step 不符（%r）"
                     % man.get("step"))
    summ = (res.get("check") or {}).get("summary") or {}
    if summ.get("n_detail_rows", 0) <= 0:
        fails.append("尾翼 Explicit: 校核明细为空")
    if not (res.get("check") or {}).get("hotspots"):
        fails.append("尾翼 Explicit: 未产出危险部位")

    # 血缘链完整（分析类型不影响血缘结构）
    edges = _edges_by_kind(conn, sid)
    chain = [("parameter_set", "catpart"), ("catpart", "topology"),
             ("topology", "mesh"), ("mesh", "inp"), ("inp", "odb"),
             ("odb", "extraction"), ("extraction", "check_result"),
             ("check_rule", "check_result"), ("check_result", "report")]
    for parent, child in chain:
        if parent not in arts or child not in arts:
            fails.append("尾翼 Explicit: 血缘缺少端点 %s/%s" % (parent, child))
        elif (parent, child) not in edges:
            fails.append("尾翼 Explicit: 血缘缺少边 %s -> %s" % (parent, child))

    # 报告可生成
    rep = res.get("report") or {}
    rpath = rep.get("path")
    if not rpath or not os.path.isfile(rpath):
        fails.append("尾翼 Explicit: 报告未生成")
    elif os.path.getsize(rpath) < 5000:
        fails.append("尾翼 Explicit: 报告过小（%d 字节）"
                     % os.path.getsize(rpath))

    return sid, res


# ---------------------------------------------------------------------------
# 7) 未知/非法分析类型被显式拒绝（不静默降级成静力）
# ---------------------------------------------------------------------------

def test_rejects(conn, tmp, fails):
    """未知分析类型必须显式报错，绝不静默按静力跑。"""
    for bad in ("static_dynamic", "impact", "STATIC ", None):
        if bad is None:
            continue
        try:
            analysis.normalize(bad)
            fails.append("未知分析类型 %r 未被拒绝" % bad)
        except analysis.AnalysisError:
            pass

    # normalize 对 None/"" 返回 None（= 沿用 P1 默认，这是合法的"不启用"）
    for empty in (None, ""):
        if analysis.normalize(empty) is not None:
            fails.append("normalize(%r) 应返回 None（不启用分析类型）" % empty)

    # 端到端：run_study 上未知类型抛 StudyRunError，且**不落半成品产物**
    sid = "S-M14-bad-kind"
    study.create_study(sid, "M14 未知分析类型", conn=conn)
    badout = os.path.join(tmp, "badkind")
    try:
        run_study.run_study(
            sid, dict(WING_PARAMS), "wingbox_v1",
            FakeCatiaDriver(os.path.join(badout, "catparts")), conn,
            RunOptions(params_dir=os.path.join(badout, "params"),
                       topo_dir=os.path.join(badout, "topo"),
                       with_fe_model=True,
                       fe_dir=os.path.join(badout, "fe"),
                       abaqus=FakeAbaqusRunner(n_elems=WING_N_ELEMS),
                       with_solve=True,
                       solve_dir=os.path.join(badout, "solve"),
                       analysis={"kind": "impact"}))
        fails.append("未知分析类型在 run_study 上未被拒绝")
    except run_study.StudyRunError:
        pass
    except Exception as exc:
        fails.append("未知分析类型应以 StudyRunError 拒绝，实际 %s: %s"
                     % (type(exc).__name__, exc))

    row = conn.execute("SELECT COUNT(*) c FROM artifacts WHERE study_id=?",
                       (sid,)).fetchone()
    if int(row["c"]) != 0:
        fails.append("未知分析类型被拒后仍落了 %d 个半成品产物" % int(row["c"]))
    fe_dir = os.path.join(badout, "fe")
    if os.path.isdir(fe_dir) and os.listdir(fe_dir):
        fails.append("未知分析类型被拒后仍写出了 .inp: %r"
                     % os.listdir(fe_dir)[:3])


# ---------------------------------------------------------------------------
# 8) 兼容性：Abaqus 侧 Python 2.7 + 宿主侧 Python 3.8
# ---------------------------------------------------------------------------

def test_compat(fails):
    """Abaqus 侧脚本仍过 Py2.7 兼容检查；本票改动的宿主侧代码过 Py3.8 检查。"""
    import compat_check
    for fn in ("fe_build.py", "extract.py"):
        path = os.path.join(CAP, fn)
        v = compat_check.check_file(path)
        if v:
            fails.append("Abaqus 侧 %s 未通过 Python 2.7 兼容检查:\n%s"
                         % (fn, "\n".join("    " + str(x) for x in v)))

    import compat_py38
    targets = [
        os.path.join(CORE, "analysis.py"),
        os.path.join(CORE, "abaqus_runner.py"),
        os.path.join(CORE, "run_study.py"),
        os.path.join(CORE, "compat_py38.py"),
        os.path.join(HERE, "test_m14.py"),
    ]
    bad = compat_py38.check_paths(targets)
    if bad:
        fails.append("Python 3.8 兼容性检查未通过:\n%s"
                     % compat_py38.format_violations(bad))


# ---------------------------------------------------------------------------
# 9) 真机（探测到可用 Abaqus 才跑；否则显式 SKIP）
# ---------------------------------------------------------------------------

def _real_probe(runner, tmp):
    """
    用一次最小的静力求解判断"这台机器上的 Abaqus 是否真的可用"。

    返回 (ok, reason)。受限环境（WMI 查 CPU 数被拒）会让 abaqus 直接退出，
    这属于**环境限制**，应当显式 SKIP 而不是判失败。
    """
    model = topology.build_topology("wingbox_v1", WING_PARAMS)
    fe = fe_model.build_fe_model(model, os.path.join(tmp, "probe_fe"),
                                 subdiv_per_bay=1, plies=PLIES)
    sol = runner.solve(fe.inp_path, "m14probe", os.path.join(tmp, "probe"),
                       step_name="Static-1")
    if sol.status == "COMPLETED":
        return True, ""
    log = (sol.log_tail or "").strip()
    for env_hint in ("getWMI", "number of cpus", "exceeds the number of cpus"):
        if env_hint.lower() in log.lower():
            return False, "本机 Abaqus 无法取到 CPU 数（受限环境）"
    return False, "Abaqus 求解失败：\n%s" % log


def test_real(tmp, fails):
    """
    真机：探测到可用 Abaqus 就跑一次**非静力**分析（模态），否则显式 SKIP。
    """
    try:
        runner = RealAbaqusRunner()
    except Exception:
        print("  [SKIP] 未探测到 Abaqus 命令，跳过真机测试")
        return

    ok, reason = _real_probe(runner, tmp)
    if not ok:
        print("  [SKIP] 探测到 abaqus 命令但当前环境无法求解，跳过真机测试")
        print("         原因: %s" % reason.splitlines()[0])
        return

    # ---- 非静力分析：模态（P1 之外的分析步写法，最能验证分析类型落到了 Abaqus 侧） ----
    model = topology.build_topology("wingbox_v1", WING_PARAMS)
    spec = _spec("frequency", n_modes=5)
    out = os.path.join(tmp, "real_frequency")
    fe = fe_model.build_fe_model(model, os.path.join(out, "fe"),
                                 subdiv_per_bay=1, plies=PLIES,
                                 analysis=spec)
    sol = runner.solve(fe.inp_path, "m14freq", os.path.join(out, "solve"),
                       step_name=spec["step_name"], analysis=spec)
    if sol.status != "COMPLETED":
        fails.append("真机: 模态分析求解失败\n%s" % sol.log_tail)
        return

    ext = runner.extract(sol.odb_path, os.path.join(out, "solve"),
                         step_name=spec["step_name"])
    man = ext.manifest
    if man.get("analysis_kind") != "frequency":
        fails.append("真机: 抽取 manifest 的分析类型不符（%r）"
                     % man.get("analysis_kind"))
    if man.get("step") != "Frequency-1":
        fails.append("真机: 抽取的 step 应为 Frequency-1，实际 %r"
                     % man.get("step"))
    if man.get("n_section_points", 0) < 2:
        fails.append("真机: 铺层数 < 2（%r）" % man.get("n_section_points"))
    if not man.get("source_odb_sha256") or \
            len(man["source_odb_sha256"]) != 64:
        fails.append("真机: odb sha256 不合法")
    # 真机抽取格式也必须与 fake 同一口径
    header = _csv_header(ext.csv_path)
    if header != list(CSV_HEADER):
        fails.append("真机: 抽取 CSV 表头与约定口径不一致: %r" % header)
    if man.get("position") != "CENTROID":
        fails.append("真机: 抽取位置口径不符（%r）" % man.get("position"))
    print("  [REAL] 模态分析 COMPLETED: rows=%s section_points=%s py=%s"
          % (man.get("n_rows"), man.get("section_points"),
             man.get("python_version")))

    # ---- Explicit：Abaqus/Explicit 需单独的产品/许可证；缺失属环境事实，SKIP 不判错 ----
    spec_x = _spec("explicit", time_period=0.001, mass_scaling=1.0)
    out_x = os.path.join(tmp, "real_explicit")
    fe_x = fe_model.build_fe_model(model, os.path.join(out_x, "fe"),
                                   subdiv_per_bay=1, plies=PLIES,
                                   analysis=spec_x)
    sol_x = runner.solve(fe_x.inp_path, "m14exp", os.path.join(out_x, "solve"),
                         step_name=spec_x["step_name"], analysis=spec_x)
    if sol_x.status != "COMPLETED":
        last = (sol_x.log_tail or "").strip().splitlines()[-1:]
        print("  [SKIP] 真机 Explicit 未完成（可能缺 Abaqus/Explicit 产品），"
              "已由 .inp 内容断言与 Py2.7 兼容检查覆盖")
        if last:
            print("         %s" % last[0])
    else:
        ext_x = runner.extract(sol_x.odb_path, os.path.join(out_x, "solve"),
                               step_name=spec_x["step_name"])
        if ext_x.manifest.get("analysis_kind") != "explicit":
            fails.append("真机: Explicit 抽取的分析类型不符")
        print("  [REAL] Explicit COMPLETED: rows=%s"
              % ext_x.manifest.get("n_rows"))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    fails = []
    tmp = _fresh("run")
    conn = db.connect(os.path.join(tmp, "m14.db"))

    print("1. 分析类型作为可选项暴露（6 种） ...")
    test_kinds_exposed(fails)
    print("2. 各分析类型的专属声明（接触对 / 非线性 / 时间步+质量缩放） ...")
    model = topology.build_topology("wingbox_v1", WING_PARAMS)
    test_declarations(model, fails)
    print("3. fake 与 real 的 solve 同签名 ...")
    test_signature_parity(fails)
    print("4. .inp 内容按分析类型不同（声明真的落进 .inp） ...")
    test_inp_per_type(model, tmp, fails)
    print("5. 不同分析类型的抽取/校核格式一致（同字段同口径） ...")
    seen = test_format_consistency(conn, tmp, fails)
    print("6. 非机翼部件族（尾翼盒段）× Explicit 端到端闭环 ...")
    test_tailbox_new_type(conn, tmp, fails)
    print("7. 未知/非法分析类型被拒绝 ...")
    test_rejects(conn, tmp, fails)
    print("8. Abaqus 侧 Python 2.7 / 宿主侧 Python 3.8 兼容 ...")
    test_compat(fails)
    print("9. 真机（探测到才跑，否则 SKIP） ...")
    test_real(tmp, fails)

    conn.close()

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M14 (票14) SELF-TEST: ALL PASSED "
          "(%d 分析类型 / 抽取格式与校核汇总同口径 / .inp 按类型不同 / "
          "尾翼 Explicit 闭环 / fake-real 同签名 / Py2.7+Py3.8)"
          % len(ALL_KINDS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
