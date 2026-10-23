# -*- coding: utf-8 -*-
"""
analysis.py -- 分析类型声明（票14）

P1 只有静力/模态/屈曲三类**线性**分析。真实复材结构还要面对**接触、非线性、
冲击/后屈曲（Explicit）**，这些在 Abaqus 侧是完全不同的建模与提交方式。

本模块是分析类型的**宿主侧唯一定义**：run_study / workbench_service / UI 都从这里取
`ANALYSIS_KINDS` 与 `default_analysis()`，避免同一份类型清单散落多处。Abaqus 侧
（abaqus_capabilities/fe_build.py）只消费**声明 dict**，不 import 本模块——
那样它才能继续在 Abaqus 内置 Python 2.7 里运行（ADR-0012）。

## 声明形状

    {"kind": "static"|"frequency"|"buckle"|"contact"|"nonlinear"|"explicit",
     ... 该类型的专属参数 ...}

各类型的默认参数见 `default_analysis()`。

## 关键契约（票14 验收）

**不同分析类型的抽取结果格式必须一致**（同字段同口径），因此强度校核逻辑无需按
分析类型分支。这条由 `AbaqusRunner.extract()` 的返回结构保证：无论哪种类型，
都产出 element × section_point × field 的同一套 CSV + manifest（ADR-0006）。
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

# 哪些类型是「线性」的（P1 只有这三种）
LINEAR_KINDS = ("static", "frequency", "buckle")


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
    base = {"kind": kind}

    if kind == "frequency":
        # 模态：取前 n_modes 阶
        base["n_modes"] = 10
    elif kind == "buckle":
        # 屈曲：取前 n_modes 个屈曲因子
        base["n_modes"] = 5
    elif kind == "contact":
        # 接触：接触对列表 + 几何非线性
        #   每个接触对：{'master': elset 名, 'slave': elset 名,
        #                'friction': 摩擦系数, 'master_face': 面号, 'slave_face': 面号}
        base["contacts"] = []
        base["nlgeom"] = True
        base["allowed_state_file"] = True
    elif kind == "nonlinear":
        # 非线性：材料非线性 / 几何非线性 / 两者
        base["nlgeom"] = True
        base["nonlinearity"] = "material"    # material | geometry | both
        base["max_increments"] = 100
        base["initial_increment"] = 0.1
    elif kind == "explicit":
        # Explicit：时间步 + 质量缩放
        base["time_period"] = 0.01
        base["mass_scaling"] = 1.0
        base["n_increments_hint"] = 1000
        base["element_type"] = "S4R"          # 显式壳单元（与 P1 一致）
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
        out.setdefault(k, v)

    _validate(out)
    return out


def _validate(spec):
    kind = spec["kind"]
    if kind == "contact":
        contacts = spec.get("contacts") or []
        if not isinstance(contacts, list):
            raise AnalysisError("contact.contacts 必须是列表")
        for i, cp in enumerate(contacts):
            if not isinstance(cp, dict):
                raise AnalysisError("contact.contacts[%d] 必须是 dict" % i)
            if not cp.get("master") or not cp.get("slave"):
                raise AnalysisError(
                    "contact.contacts[%d] 必须给 master 与 slave 单元集合名" % i)
            mu = cp.get("friction", 0.2)
            try:
                mu = float(mu)
            except (TypeError, ValueError):
                raise AnalysisError("contact.contacts[%d].friction 必须是数值" % i)
            if mu < 0:
                raise AnalysisError("contact.contacts[%d].friction 不能为负" % i)
    elif kind == "nonlinear":
        nl = spec.get("nonlinearity", "material")
        if nl not in ("material", "geometry", "both"):
            raise AnalysisError(
                "nonlinear.nonlinearity 必须是 material/geometry/both，实际 %r" % nl)
        try:
            if int(spec.get("max_increments", 100)) <= 0:
                raise AnalysisError("nonlinear.max_increments 必须 > 0")
        except (TypeError, ValueError):
            raise AnalysisError("nonlinear.max_increments 必须是正整数")
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
    elif kind in ("frequency", "buckle"):
        try:
            if int(spec.get("n_modes", 10)) <= 0:
                raise AnalysisError("%s.n_modes 必须 > 0" % kind)
        except (TypeError, ValueError):
            raise AnalysisError("%s.n_modes 必须是正整数" % kind)


def is_linear(spec):
    return (spec or {}).get("kind", "static") in LINEAR_KINDS


def label(spec_or_kind):
    kind = spec_or_kind if isinstance(spec_or_kind, str) else \
        (spec_or_kind or {}).get("kind", "static")
    return ANALYSIS_LABELS.get(kind, kind)


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
        txt += "（%s 非线性）" % spec.get("nonlinearity", "material")
    elif kind == "explicit":
        txt += "（时间步 %gs，质量缩放 %g）" % (
            float(spec.get("time_period", 0.01)),
            float(spec.get("mass_scaling", 1.0)))
    return txt
