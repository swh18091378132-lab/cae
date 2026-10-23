# -*- coding: utf-8 -*-
"""
analysis.py -- 分析类型声明（票14）

P1 只有静力/模态/屈曲三类**线性**分析。真实复材结构还要面对**接触、非线性、
冲击/后屈曲（Explicit）**，这些在 Abaqus 侧是完全不同的建模与提交方式。

本模块是分析类型的**宿主侧唯一定义**：run_study / workbench_service / UI 都从这里取
`ANALYSIS_KINDS` 与 `default_analysis()`，避免同一份类型清单散落多处。Abaqus 侧
（abaqus_capabilities/fe_build.py）只消费**声明 dict**，不 import 本模块——
那样它才能继续在 Abaqus 内置 Python 2.7 里运行（ADR-0012）。

因此本文件里每个字段都必须满足两条：
  1. 有明确语义，且 **fe_build 真的会把它写进 .inp**（"声明了但没人消费"= 假能力）；
  2. 只放原始类型（str/int/float/list/dict），能被 JSON 序列化后原样交给 Abaqus 侧。

## 声明形状

    {"kind": "static"|"frequency"|"buckle"|"contact"|"nonlinear"|"explicit",
     "step_name": "Static-1",       # 写进 *STEP, NAME=...（fe_build 直接读这个键）
     ... 该类型的专属参数 ...}

各类型的默认参数见 `default_analysis()`；`normalize()` 负责补齐默认值并校验。

## 关键契约（票14 验收）

**不同分析类型的抽取结果格式必须一致**（同字段同口径），因此强度校核逻辑无需按
分析类型分支。这条由 `AbaqusRunner.extract()` 的返回结构保证：无论哪种类型，
都产出 element × section_point × field 的同一套 CSV + manifest（ADR-0006）；
分析类型只出现在 manifest 的 `analysis_kind` 元信息里，不进数据列。

兼容 Python 3.8（宿主侧）：不用 3.9+ 语法。
"""

# 支持的分析类型（顺序即 UI 展示顺序）
ANALYSIS_KINDS = ("static", "frequency", "buckle", "contact", "nonlinear",
                  "explicit")

# 人类可读名称（UI 与报告用）
ANALYSIS_LABELS = {
    "static": "静力分析",
    "frequency": "模态分析",
    "buckle": "屈曲分析",
    "contact": "接触分析",
    "nonlinear": "非线性分析",
    "explicit": "显式动力学（Explicit）",
}

# 哪些类型是「线性」的（P1 只有这三种；perturbation/线性静力步）
LINEAR_KINDS = ("static", "frequency", "buckle")

# 允许的壳单元类型（铺层按 *SHELL SECTION 定义，故只能是壳族；ADR-0005）。
#
# **只列网格生成器真的能产出的类型**：meshgen.add_quad 一律产出 4 节点四边形，
# fe_build 也固定写 4 节点连接行。允许 S8R/S8R5（8 节点二次壳）会生成
# `*ELEMENT, TYPE=S8R` + 4 节点记录 —— 那是 Abaqus 直接拒收的 .inp。
# 声明了却写不出合法 .inp 的能力，比没有这个能力更糟（用户以为跑的是二次单元）。
# 真要支持二次壳，得先让 meshgen 产出 8 节点单元，届时再往这里加。
SHELL_ELEMENT_TYPES = ("S4R", "S4R5")

# 壳单元的面标签：SPOS=正法向面，SNEG=负法向面（Abaqus 壳单元 5/6 号面）。
# 接触对默认用这两个面——壳与壳的接触几乎总是这两个面，而不是边界面 1/2。
CONTACT_FACE_LABELS = ("SPOS", "SNEG", "S1", "S2", "S3", "S4", "S5", "S6")

# 材料非线性用的 Hashin 损伤起始许用值（MPa）。
# 与 strength_check 的默认许用值同源但**另有用途**：这里是写进 .inp 的
# *DAMAGE INITIATION 参数，校核用的是宿主侧判定规则，两者不共用同一份配置。
DEFAULT_DAMAGE_ALLOWABLES = {
    "Xt": 1500.0,   # 纵向拉伸强度
    "Xc": 1200.0,   # 纵向压缩强度
    "Yt": 50.0,     # 横向拉伸强度
    "Yc": 200.0,    # 横向压缩强度
    "Sl": 70.0,     # 纵向剪切强度
    "St": 70.0,     # 横向剪切强度
}

# 各分析类型的默认 *STEP 名称（写进 .inp，便于 ODB 里按类型辨认分析步）
_KIND_STEP_NAMES = {
    "static": "Static-1",
    "frequency": "Frequency-1",
    "buckle": "Buckle-1",
    "contact": "Contact-1",
    "nonlinear": "Nonlinear-1",
    "explicit": "Explicit-1",
}


class AnalysisError(ValueError):
    """分析类型声明不合法。"""


def default_analysis(kind="static"):
    """
    返回一个分析类型声明 dict，缺失参数已补齐。

    未知类型抛 AnalysisError——不静默降级成静力，否则用户以为在跑 Explicit。
    """
    kind = kind or "static"
    if kind not in ANALYSIS_KINDS:
        raise AnalysisError("未知分析类型 %r（可选: %s）"
                            % (kind, ", ".join(ANALYSIS_KINDS)))
    base = {"kind": kind, "step_name": _KIND_STEP_NAMES[kind]}

    if kind == "frequency":
        # 模态：取前 n_modes 阶
        base["n_modes"] = 10
    elif kind == "buckle":
        # 屈曲：取前 n_modes 个屈曲因子
        base["n_modes"] = 5
    elif kind == "contact":
        # 接触：接触对列表 + 几何非线性
        #   每个接触对：{'master': elset 名, 'slave': elset 名,
        #                'friction': 摩擦系数, 'master_face': 面标签, 'slave_face': 面标签}
        base["contacts"] = []
        base["nlgeom"] = True
        base["initial_increment"] = 0.1
        base["max_increments"] = 100
    elif kind == "nonlinear":
        # 非线性：材料非线性 / 几何非线性 / 两者
        #   nlgeom 由 nonlinearity **派生**（不单独声明，避免"说 material 却开 NLGEOM"）：
        #     material -> 几何线性；geometry/both -> 几何非线性
        base["nonlinearity"] = "material"    # material | geometry | both
        base["nlgeom"] = False
        base["max_increments"] = 100
        base["initial_increment"] = 0.1
        # 材料非线性 = 复合材料 Hashin 损伤起始（*DAMAGE INITIATION）
        base["damage"] = dict(DEFAULT_DAMAGE_ALLOWABLES)
    elif kind == "explicit":
        # Explicit：时间步 + 质量缩放
        base["time_period"] = 0.01
        base["mass_scaling"] = 1.0
        base["n_field_outputs"] = 20       # *OUTPUT, FIELD, NUMBER INTERVAL=n
        base["element_type"] = "S4R"       # 显式壳单元（与 P1 一致）
    return base


def normalize(spec):
    """
    把用户输入归一并校验。

    接受:
      * None / ""            -> None（沿用 P1 默认，不启用分析类型扩展）
      * "contact"            -> 该类型的默认声明
      * {"kind": "explicit"} -> 补默认后的声明
    未知类型抛 AnalysisError。
    """
    if spec is None or spec == "":
        return None
    if isinstance(spec, str):
        return default_analysis(spec)
    if not isinstance(spec, dict):
        raise AnalysisError("analysis 必须是字符串或 dict，实际 %r"
                            % type(spec).__name__)

    out = dict(spec)
    kind = out.get("kind") or out.get("type") or "static"
    if kind not in ANALYSIS_KINDS:
        raise AnalysisError("未知分析类型 %r（可选: %s）"
                            % (kind, ", ".join(ANALYSIS_KINDS)))
    out["kind"] = kind
    for k, v in default_analysis(kind).items():
        if k not in out or out[k] is None:
            out[k] = v

    _validate(out)
    return out


def _validate(spec):
    kind = spec["kind"]

    # *STEP 名称：Abaqus 要求同一输入文件内唯一且非空
    name = spec.get("step_name")
    if not isinstance(name, str) or not name.strip():
        raise AnalysisError("%s.step_name 必须是非空字符串，实际 %r"
                            % (kind, name))

    if kind == "contact":
        _validate_contacts(spec)
        _validate_increments(spec, "contact")

    elif kind == "nonlinear":
        nl = spec.get("nonlinearity", "material")
        if nl not in ("material", "geometry", "both"):
            raise AnalysisError(
                "nonlinear.nonlinearity 必须是 material/geometry/both，实际 %r" % nl)
        # 声明了几何非线性就必须真的开 NLGEOM（反之亦然），避免"声明与 .inp 不符"
        spec["nlgeom"] = nl in ("geometry", "both")
        if nl in ("material", "both"):
            _validate_damage(spec.get("damage"))
        else:
            # 只声明几何非线性时不带材料损伤：声明里也不该留着，否则
            # "声明 dict"与"实际写出的 .inp"会不一致（假能力）
            spec.pop("damage", None)
        _validate_increments(spec, "nonlinear")

    elif kind == "explicit":
        try:
            tp = float(spec.get("time_period", 0.01))
        except (TypeError, ValueError):
            raise AnalysisError("explicit.time_period 必须是数值")
        if tp <= 0:
            raise AnalysisError("explicit.time_period 必须 > 0")
        try:
            if float(spec.get("mass_scaling", 1.0)) <= 0:
                raise AnalysisError("explicit.mass_scaling 必须 > 0")
        except (TypeError, ValueError):
            raise AnalysisError("explicit.mass_scaling 必须是数值")
        try:
            if int(spec.get("n_field_outputs", 20)) <= 0:
                raise AnalysisError("explicit.n_field_outputs 必须 > 0")
        except (TypeError, ValueError):
            raise AnalysisError("explicit.n_field_outputs 必须是正整数")
        et = str(spec.get("element_type", "S4R")).upper()
        if et not in SHELL_ELEMENT_TYPES:
            raise AnalysisError(
                "explicit.element_type 必须是壳单元 %s（铺层要求壳族，ADR-0005），"
                "实际 %r" % ("/".join(SHELL_ELEMENT_TYPES), et))
        spec["element_type"] = et

    elif kind in ("frequency", "buckle"):
        try:
            if int(spec.get("n_modes", 10)) <= 0:
                raise AnalysisError("%s.n_modes 必须 > 0" % kind)
        except (TypeError, ValueError):
            raise AnalysisError("%s.n_modes 必须是正整数" % kind)


def _validate_increments(spec, kind):
    try:
        if int(spec.get("max_increments", 100)) <= 0:
            raise AnalysisError("%s.max_increments 必须 > 0" % kind)
    except (TypeError, ValueError):
        raise AnalysisError("%s.max_increments 必须是正整数" % kind)
    try:
        init = float(spec.get("initial_increment", 0.1))
    except (TypeError, ValueError):
        raise AnalysisError("%s.initial_increment 必须是数值" % kind)
    if not (0.0 < init <= 1.0):
        raise AnalysisError("%s.initial_increment 必须在 (0, 1] 内，实际 %r"
                            % (kind, init))


def _validate_contacts(spec):
    contacts = spec.get("contacts") or []
    if not isinstance(contacts, list):
        raise AnalysisError("contact.contacts 必须是列表")
    for i, cp in enumerate(contacts):
        if not isinstance(cp, dict):
            raise AnalysisError("contact.contacts[%d] 必须是 dict" % i)
        for key in ("master", "slave"):
            if not cp.get(key):
                raise AnalysisError(
                    "contact.contacts[%d] 必须给 master 与 slave 单元集合名" % i)
        mu = cp.get("friction", 0.2)
        try:
            mu = float(mu)
        except (TypeError, ValueError):
            raise AnalysisError("contact.contacts[%d].friction 必须是数值" % i)
        if mu < 0:
            raise AnalysisError("contact.contacts[%d].friction 不能为负" % i)
        for key in ("master_face", "slave_face"):
            if key in cp and cp[key] not in (None, ""):
                _validate_face(cp[key], i, key)


def _validate_face(value, idx, key):
    """接触面：1..6 或 SPOS/SNEG（壳单元的正/负法向面）。"""
    txt = str(value).strip().upper()
    if txt in CONTACT_FACE_LABELS:
        return
    try:
        num = int(txt)
    except (TypeError, ValueError):
        raise AnalysisError(
            "contact.contacts[%d].%s 必须是面号 1..6 或 %s，实际 %r"
            % (idx, key, "/".join(CONTACT_FACE_LABELS), value))
    if not (1 <= num <= 6):
        raise AnalysisError("contact.contacts[%d].%s 面号必须在 1..6，实际 %r"
                            % (idx, key, value))


def _validate_damage(damage):
    if damage is None:
        raise AnalysisError("nonlinear.damage 不能为 None（材料非线性需要许用值）")
    if not isinstance(damage, dict):
        raise AnalysisError("nonlinear.damage 必须是 dict")
    for key in ("Xt", "Xc", "Yt", "Yc", "Sl", "St"):
        if key not in damage:
            raise AnalysisError("nonlinear.damage 缺少 %s" % key)
        try:
            if float(damage[key]) <= 0:
                raise AnalysisError("nonlinear.damage.%s 必须 > 0" % key)
        except (TypeError, ValueError):
            raise AnalysisError("nonlinear.damage.%s 必须是数值" % key)


def is_linear(spec):
    return (spec or {}).get("kind", "static") in LINEAR_KINDS


def label(spec_or_kind):
    kind = spec_or_kind if isinstance(spec_or_kind, str) else \
        (spec_or_kind or {}).get("kind", "static")
    return ANALYSIS_LABELS.get(kind, kind)


def step_name(spec_or_kind):
    """该分析类型在 .inp 里的分析步名称（未声明则给该类型的默认名）。"""
    if isinstance(spec_or_kind, str):
        return _KIND_STEP_NAMES.get(spec_or_kind, "Step-1")
    spec = spec_or_kind or {}
    return spec.get("step_name") or \
        _KIND_STEP_NAMES.get(spec.get("kind", "static"), "Step-1")


def describe(spec):
    """一行人类可读描述（报告与日志用）。"""
    if not spec:
        return "静力分析（P1 默认）"
    kind = spec.get("kind", "static")
    txt = label(spec)
    if kind == "frequency":
        txt += "（前 %d 阶）" % int(spec.get("n_modes", 10))
    elif kind == "buckle":
        txt += "（前 %d 个屈曲因子）" % int(spec.get("n_modes", 5))
    elif kind == "contact":
        txt += "（%d 个接触对）" % len(spec.get("contacts") or [])
    elif kind == "nonlinear":
        txt += "（%s 非线性，最多 %d 增量）" % (
            spec.get("nonlinearity", "material"),
            int(spec.get("max_increments", 100)))
    elif kind == "explicit":
        txt += "（时间步 %gs，质量缩放 %g）" % (
            float(spec.get("time_period", 0.01)),
            float(spec.get("mass_scaling", 1.0)))
    return txt
