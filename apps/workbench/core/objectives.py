# -*- coding: utf-8 -*-
"""
objectives.py -- 优化目标与约束口径（票 21）

对应 ADR-0009（优化闭环）。术语见 GLOSSARY：自然格 / 层合板 / 安全裕度 / 强度校核。

## 为什么需要这一层

P3 的优化闭环要「最小化质量、满足 MS ≥ 0」，但在此之前**全平台没有任何地方
计算质量**。目标函数不存在，后面所有搜索都在优化一个不存在的量。本模块把
「目标」与「约束」的口径固定下来：

  * `mass(...)`   —— 结构质量（可手算对照、带口径声明）
  * `constraints(...)` —— 从句柄既有强度校核结果里**提取**约束状态
  * `evaluate(...)`    —— 把一次运行归一成统一的评估结果

## 三条硬口径

### 1. 物理尺度必须换算，不许把参数空间 0..1 当米

自然格的 y 已经是物理长度（m），但 xi 是 0..1 的无量纲量。面积必须走
`TopologyModel.extents()`：

    物理弦向长度 = (xi1 - xi0) * xi_scale
    面积         = (y1 - y0) * 物理弦向长度

`xi_scale` 由拓扑层按部件族给出（翼面类 = 根弦长；筒段类 = 展开周长 2πr），
所以本模块**部件族无关**。把它当 1 会让质量系统性偏小 —— 这正是"减重 30%"
最常见的来源。

### 2. 只给一个数不说包含什么是不可接受的

质量结果必带 `components`（如 `["skin"]`）与 `excluded`。平台目前没有加筋
截面定义，所以**默认只算蒙皮**；加筋作为可选附加项，仅在调用方按模板声明的
截面假设显式给出时才计入，并如实列进 `components`。分母不明的减重百分比
没有工程意义。

### 3. 技术故障 ≠ 设计不可行

优化器若把"求解失败"读成"目标很差"，就会主动避开某些参数区域 —— 那是错的。
两者在数据结构上必须可分：

    ok=True,  feasible=True   跑通且满足约束
    ok=True,  feasible=False  跑通但违反约束（设计不可行）
    ok=False, error=...       技术故障（求解失败 / 网格破戒 / 参数越界）

`feasible` 在 `ok=False` 时恒为 `None`（不是 False），避免调用方误当成"不可行"。

## 纯算法

不依赖 CATIA / Abaqus / DB / 第三方库；只吃 `TopologyModel`（或它的
`to_dict()`）与校核汇总 dict，便于秒级单测与离线批量评估。
兼容 Python 3.8。
"""

# 平台口径：铺层厚度以 mm 计（见 abaqus_capabilities/fe_build.py 的单层默认 0.125），
# 几何与密度用米 / kg/m3。这里集中换算，避免各处各写一个 0.001。
MM_TO_M = 1e-3

#: 默认密度（kg/m3）。CFRP 典型值，与 fe_build.default_material 的 rho 一致。
DEFAULT_DENSITY = 1600.0

#: 质量口径名（写进结果，供报告与优化日志引用）
MASS_BASIS = "skin+declared_stiffeners"

_OPTS_TOL = 1e-12


class ObjectiveError(ValueError):
    """目标/约束口径无法计算（输入对象不对、单位不可判等）。"""


# ---------------------------------------------------------------------------
# 质量
# ---------------------------------------------------------------------------

def _extents(model):
    """取拓扑层物理尺度；也可以接受已经 to_dict() 过的模型。"""
    if hasattr(model, "extents"):
        return model.extents()
    if isinstance(model, dict):
        ext = model.get("extents")
        if isinstance(ext, dict):
            return ext
    raise ObjectiveError(
        "model 必须是 topology.TopologyModel（或其 to_dict()，需含 extents）")


def _bays(model):
    """取自然格序列；元素需有 y0/y1/xi0/xi1/id。"""
    bays = getattr(model, "bays", None)
    if bays is None and isinstance(model, dict):
        bays = model.get("bays")
    if not bays:
        raise ObjectiveError("model 里没有自然格（bays 为空），无法计算质量")
    return bays


def _bay_field(bay, name):
    """自然格字段读取：同时支持对象属性与 dict 键（to_dict() 的形态）。"""
    if isinstance(bay, dict):
        return bay[name]
    return getattr(bay, name)


def _thickness_m(laminate):
    """
    层合板总厚（米）。

    平台铺层厚度口径是 **mm**（`fe_build` 的默认单层 0.125），所以这里乘 1e-3。
    也允许显式传单位 `laminate["thickness_unit"]`（"mm"/"m"）覆盖默认。
    """
    if laminate is None:
        raise ObjectiveError("缺少层合板信息（laminate），无法计算质量")

    if "total_thickness" in laminate:
        raw = float(laminate["total_thickness"])
    else:
        plies = laminate.get("plies") or []
        raw = sum(float(p.get("thickness", 0.0)) for p in plies)
    if raw <= 0.0:
        raise ObjectiveError("层合板总厚必须为正，实际 %r" % raw)

    unit = str(laminate.get("thickness_unit") or "mm").lower()
    if unit == "m":
        return raw
    if unit == "mm":
        return raw * MM_TO_M
    raise ObjectiveError("层合板厚度单位无法识别: %r（应为 'mm' 或 'm'）" % unit)


def _density(value):
    if value is None:
        return DEFAULT_DENSITY
    try:
        rho = float(value)
    except (TypeError, ValueError):
        raise ObjectiveError("密度必须是数值，实际 %r" % (value,))
    if rho <= 0.0:
        raise ObjectiveError("密度必须为正，实际 %r" % rho)
    return rho


def _stiffener_mass(model, laminate, density, assumption):
    """
    按模板声明的截面假设折算加筋质量。

    assumption 形如::

        {"area_per_bay": 1.2e-3,   # m2：每个自然格分到的加筋截面积
         "basis": "per_bay"}       # 默认 per_bay

    或按单位长度给（"per_length"）：加筋截面积 × 该格沿 y 的物理长度。
    """
    if not isinstance(assumption, dict):
        raise ObjectiveError("加筋截面假设应为 dict，实际 %r"
                             % (assumption,))
    basis = str(assumption.get("basis") or "per_bay").lower()
    if basis == "per_length":
        area = float(assumption["area"])
    elif basis == "per_bay":
        area = float(assumption["area_per_bay"])
    else:
        raise ObjectiveError("加筋截面假设 basis 无法识别: %r" % basis)
    if area <= 0.0:
        raise ObjectiveError("加筋截面积必须为正，实际 %r" % area)

    stiffness_mass = 0.0
    for bay in _bays(model):
        y_len = float(_bay_field(bay, "y1")) - float(_bay_field(bay, "y0"))
        if basis == "per_bay":
            stiffness_mass += area * density
        else:
            stiffness_mass += area * y_len * density
    return stiffness_mass, area, basis


def mass(model, laminate, density=None, stiffeners=None, include=None):
    """
    结构质量（kg）：自然格蒙皮面积 × 层合板总厚 × 密度求和。

    model      : topology.TopologyModel（或其 to_dict()；需含 bays 与 extents）
    laminate   : {'total_thickness'} 或 {'plies': [...]}（厚度口径 mm，见 `_thickness_m`）
    density    : kg/m3；None = `DEFAULT_DENSITY`
    stiffeners : 可选，模板声明的加筋截面假设 dict（见 `_stiffener_mass`）。
                 **不给则只算蒙皮**，并在结果的 `excluded` 里如实标注。
    include    : 可选，显式要计入的分量名列表（默认按"声明了什么算什么"推导）。

    返回 dict：
      value        : 总质量 kg
      unit         : "kg"
      basis        : 口径名（`MASS_BASIS`）
      components   : 实际计入的分量名列表（如 ["skin"] / ["skin","stiffeners"]）
      breakdown    : {分量名: {mass, area?, ...}}（各分量质量之和 == value）
      excluded     : [{component, reason}] 未计入的部件及原因
      skin         : {area, thickness, density, mass, n_bays, xi_scale}
      skin_by_bay  : [{bay_id, area, mass}] 逐格明细（可核查口径）
      units        : {length, thickness, density, mass}
      density      : kg/m3
    """
    ext = _extents(model)
    bays = _bays(model)
    xi_scale = float(ext.get("xi_scale", 1.0))
    t_m = _thickness_m(laminate)
    rho = _density(density)

    skin_by_bay = []
    total_area = 0.0
    for bay in bays:
        y0 = float(_bay_field(bay, "y0"))
        y1 = float(_bay_field(bay, "y1"))
        xi0 = float(_bay_field(bay, "xi0"))
        xi1 = float(_bay_field(bay, "xi1"))
        area = abs(y1 - y0) * abs(xi1 - xi0) * xi_scale
        if area <= 0.0:
            raise ObjectiveError(
                "自然格 %r 面积非正（y %.6f..%.6f, xi %.6f..%.6f, xi_scale=%r）"
                % (_bay_field(bay, "id"), y0, y1, xi0, xi1, xi_scale))
        bay_mass = area * t_m * rho
        total_area += area
        skin_by_bay.append({
            "bay_id": _bay_field(bay, "id"),
            "area": area,
            "thickness": t_m,
            "mass": bay_mass,
        })

    skin_mass = total_area * t_m * rho

    breakdown = {
        "skin": {
            "mass": skin_mass,
            "area": total_area,
            "thickness": t_m,
            "density": rho,
            "n_bays": len(bays),
        }
    }
    components = ["skin"]
    excluded = [{
        "component": "stiffeners",
        "reason": "未声明加筋截面假设（模板无加筋截面定义）：只算蒙皮",
    }]

    if stiffeners:
        stiff_mass, stiff_area, stiff_basis = _stiffener_mass(
            model, laminate, rho, stiffeners)
        breakdown["stiffeners"] = {
            "mass": stiff_mass,
            "area_per_bay": stiff_area,
            "basis": stiff_basis,
            "assumption": dict(stiffeners),
        }
        components.append("stiffeners")
        excluded = [e for e in excluded if e["component"] != "stiffeners"]

    if include:
        wanted = set(include)
        unknown = wanted - set(breakdown.keys())
        if unknown:
            raise ObjectiveError(
                "include 里要求计入尚未声明的分量 %r（已声明 %r）"
                % (sorted(unknown), sorted(breakdown.keys())))
        # 只要被 include 的分量（默认 = 全部已声明的）
        breakdown = dict((k, v) for k, v in breakdown.items() if k in wanted)
        components = [k for k in components if k in wanted]

    total = sum(v["mass"] for v in breakdown.values())

    return {
        "value": total,
        "unit": "kg",
        "basis": MASS_BASIS,
        "components": components,
        "breakdown": breakdown,
        "excluded": excluded,
        "skin": {
            "area": total_area,
            "thickness": t_m,
            "density": rho,
            "mass": skin_mass,
            "n_bays": len(bays),
            "xi_scale": xi_scale,
        },
        "skin_by_bay": skin_by_bay,
        "density": rho,
        "units": {
            "length": str(ext.get("y_unit") or "m"),
            "thickness": "m",
            "density": "kg/m3",
            "mass": "kg",
        },
    }


# ---------------------------------------------------------------------------
# 约束（复用既有强度校核结论）
# ---------------------------------------------------------------------------

#: 约束来源标识（写进结果，优化日志/报告引用同一串）
CHECK_SUMMARY_SOURCE = "strength_check.check.summary"

#: 判定只认这三个字段；`all_pass` 是**唯一**的判定依据（不自行重算）
_CONSTRAINT_FIELDS = ("max_fi", "min_ms", "all_pass", "n_bays_excluded")


def constraints(check_summary):
    """
    从既有强度校核结果提取约束状态（**不另起一套判定**）。

    check_summary: `strength_check.check()["summary"]`（或其 JSON 化形态）。

    判定口径以 `all_pass` 为唯一依据 —— 那是 strength_check 用 FI/MS 与许用值
    算出来的确定性命中（ADR-0007）。本函数**只提取、不重算**：即使数值看起来
    矛盾（all_pass=True 但 min_ms<0），也沿用既有结论并发警告，绝不覆盖。
    优化器的职责是搜索，不是重新定义合格。

    返回 dict：
      feasible : bool（= all_pass）
      source   : 约束来源标识
      values   : {max_fi, min_ms, n_bays_excluded, ...}  原样带出
      verdict  : 原始判定相关字段 {all_pass, n_fail_rows, criteria, ...}
      warnings : [str] 数值与判定矛盾时的提示（不改变 feasible）
    """
    if not isinstance(check_summary, dict):
        raise ObjectiveError("check_summary 必须是 dict（strength_check 的 summary）")
    for key in ("all_pass",):
        if key not in check_summary:
            raise ObjectiveError(
                "check_summary 缺少 %r —— 没有跑强度校核就不能得到约束状态"
                "（可用 evaluate(error=...) 表达技术故障）" % key)
    if check_summary.get("all_pass") is None:
        raise ObjectiveError("check_summary.all_pass 为 None：校核无结论")

    feasible = bool(check_summary["all_pass"])

    values = {}
    for key in _CONSTRAINT_FIELDS:
        if key in check_summary:
            values[key] = check_summary[key]
    # 补充可追溯字段（有就带）
    for key in ("n_bays_checked", "n_elems", "n_detail_rows", "n_fail_rows",
                "ms_threshold", "safety_factor"):
        if key in check_summary:
            values[key] = check_summary[key]

    verdict = {
        "all_pass": check_summary.get("all_pass"),
        "n_fail_rows": check_summary.get("n_fail_rows"),
        "criteria": list(check_summary.get("criteria") or []),
        "rules_checksum": check_summary.get("rules_checksum"),
    }

    warnings = []
    max_fi = check_summary.get("max_fi")
    min_ms = check_summary.get("min_ms")
    try:
        if max_fi is not None and float(max_fi) >= 1.0 and feasible:
            warnings.append(
                "check.summary 自相矛盾：all_pass=True 但 max_fi=%r >= 1；"
                "沿用既有判定（不重算），请核查规则/许用值" % (max_fi,))
        if min_ms is not None and float(min_ms) < 0.0 and feasible:
            warnings.append(
                "check.summary 自相矛盾：all_pass=True 但 min_ms=%r < 0；"
                "沿用既有判定（不重算），请核查 ms_threshold" % (min_ms,))
        if max_fi is None and min_ms is None:
            warnings.append("check.summary 既无 max_fi 也无 min_ms：约束状态仅依据 all_pass")
    except (TypeError, ValueError):
        warnings.append("check.summary 的 max_fi/min_ms 不是数值，无法交叉核对")

    return {
        "feasible": feasible,
        "source": CHECK_SUMMARY_SOURCE,
        "values": values,
        "verdict": verdict,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 技术故障分类（与"设计不可行"严格分开）
# ---------------------------------------------------------------------------

class ErrorKind(object):
    """
    技术故障的分类。优化器可据此决定"重试 / 跳过 / 报告"，
    而不是把故障当成目标值很差的点。纯常量容器（不需要实例化）。
    """

    PARAM_ERROR = "param_error"          # 参数越界 / 未知参数（抛 ParamError 时）
    MESH_VIOLATION = "mesh_violation"    # 网格破戒（ADR-0010）/ 界面悬挂节点
    NO_CHECK = "no_check"                # 没有校核结果（没跑到那一步）
    NO_MASS = "no_mass"                  # 算不出质量（缺层合板/尺度信息）
    RUN_FAILED = "run_failed"            # 求解/抽取/建模失败
    UNKNOWN = "unknown"                  # 兜底：绝不能丢


#: 异常类名 -> 分类（按 core 里既有的错误类型名匹配，不 import 它们）
_ERROR_KIND_BY_NAME = {
    "ParamError": ErrorKind.PARAM_ERROR,
    "TemplateError": ErrorKind.PARAM_ERROR,
    "MeshQualityError": ErrorKind.MESH_VIOLATION,
    "TopologyError": ErrorKind.MESH_VIOLATION,
    "FeBuildError": ErrorKind.MESH_VIOLATION,
    "StudyRunError": ErrorKind.RUN_FAILED,
    "RuleError": ErrorKind.RUN_FAILED,
    "ServiceError": ErrorKind.RUN_FAILED,
    "RollupError": ErrorKind.RUN_FAILED,
    "SolutionError": ErrorKind.RUN_FAILED,
}


def classify_error(exc):
    """
    把一个异常（或字符串）归类成 `ErrorKind`。

    按**类名**匹配而不是 import 各层模块：本模块必须能独立单测、不依赖
    CATIA/Abaqus/DB，也不能因为某层重命名而 import 失败。
    """
    if isinstance(exc, str):
        return ErrorKind.RUN_FAILED
    kind = _ERROR_KIND_BY_NAME.get(type(exc).__name__)
    if kind is not None:
        return kind
    # 参数类错误常以 ValueError 派生但类名不带 ParamError（如 Template.validate 内部）
    msg = str(exc) or ""
    if "未知参数" in msg or "超出参数化包络" in msg or "参数契约" in msg:
        return ErrorKind.PARAM_ERROR
    if "跨自然格单元" in msg or "ADR-0010" in msg or "悬挂节点" in msg:
        return ErrorKind.MESH_VIOLATION
    return ErrorKind.UNKNOWN


def _error_payload(exc):
    """把异常整理成可 JSON 序列化的故障描述（kind + 类型 + 可读原因）。"""
    if exc is None:
        return None
    if isinstance(exc, dict):
        payload = dict(exc)
        payload.setdefault("kind", payload.get("kind") or ErrorKind.UNKNOWN)
        payload.setdefault("message", payload.get("message") or str(payload))
        return payload

    kind = classify_error(exc)
    message = str(exc) or type(exc).__name__
    payload = {
        "kind": kind,
        "type": type(exc).__name__,
        "message": message,
    }
    # 参数错误带出参数名，便于优化器区分"某变量越界"与"整体失败"
    name = getattr(exc, "param", None) or getattr(exc, "param_name", None)
    if name:
        payload["param"] = name
    return payload


# ---------------------------------------------------------------------------
# 评估结果归一
# ---------------------------------------------------------------------------

#: 目标方向（P3 只做减重）
OBJECTIVE_DIRECTION = "minimize"


def _objective_block(mass_result):
    """目标块：value/unit/direction/components 一起给，避免"只有一个数"。"""
    if mass_result is None:
        return {"name": "mass", "direction": OBJECTIVE_DIRECTION,
                "value": None, "unit": None, "components": [],
                "declared": False, "basis": None}
    if isinstance(mass_result, (int, float)):
        # 调用方直接给了一个数（优化器批量评估图省事）：**不得替他编口径** ——
        # 我们并不知道这个数包含什么，所以 components 留空并标 declared=False。
        # 只有走 mass() 才算"有口径声明"。
        return {"name": "mass", "direction": OBJECTIVE_DIRECTION,
                "value": float(mass_result), "unit": "kg",
                "components": [], "declared": False, "basis": None,
                "note": "调用方直接给数值，未声明质量口径（components 未知）"}
    return {
        "name": "mass",
        "direction": OBJECTIVE_DIRECTION,
        "value": mass_result.get("value"),
        "unit": mass_result.get("unit", "kg"),
        "components": list(mass_result.get("components") or []),
        "declared": bool(mass_result.get("components")),
        "basis": mass_result.get("basis"),
        "breakdown": mass_result.get("breakdown"),
        "excluded": mass_result.get("excluded") or [],
    }


def _mass_from_result(result, density=None, stiffeners=None):
    """
    从 `run_study` 的返回里就地算质量（调用方不必自己拆 topology/laminate）。

    需要 `topology_model`（或 `topology_data`）与 `fe_model.laminate`。
    拿不到就返回 None —— 由 evaluate 判为 `no_mass` 技术故障。
    """
    if not isinstance(result, dict):
        return None
    model = result.get("topology_model")
    if model is None:
        model = result.get("topology_data")
    if model is None:
        return None

    fe = result.get("fe_model")
    laminate = getattr(fe, "laminate", None) if fe is not None else None
    if laminate is None:
        laminate = result.get("laminate")
    if not laminate:
        return None

    try:
        return mass(model, laminate, density=density, stiffeners=stiffeners)
    except ObjectiveError:
        return None


def evaluate(result=None, check_summary=None, mass_result=None, mass_kg=None,
             error=None, study_id=None, design=None, extra=None,
             density=None, stiffeners=None):
    """
    把一次运行归一成统一的**评估结果**（优化器的唯一输入形态）。

    三种调用方式（都归一成同一个 dict）：
      1. `evaluate(result=run_study(...) 的返回)`  —— 最常用：一次运行的完整结果
         此时目标值会**自动**由 `topology_model` + `fe_model.laminate` 算出
      2. `evaluate(check_summary=..., mass_kg=...)` —— 只要已知结论与目标值
      3. `evaluate(error=<异常>)` —— 技术故障

    三类结果（**必须可区分**，否则优化器会把故障读成"设计很差"）：

        ok=True,  feasible=True   跑通且满足约束
        ok=True,  feasible=False  跑通但违反约束（设计不可行，目标值可信）
        ok=False, feasible=None   技术故障（error 非空，**目标值为 None**）

    返回 dict：
      ok        : bool          技术层面是否跑通
      feasible  : bool / None   设计是否满足约束；ok=False 时**恒为 None**
      error     : None / {kind, type, message}
      objective : {name, direction, value, unit, components, basis, ...}
      constraints : constraints() 的结果（故障时为 None）
      mass      : 质量明细（故障时为 None）
      study_id / template / design / extra : 追溯字段

    注意 `feasible` 在故障时是 `None` 而**不是 False** —— 这个区别就是
    "不要把技术故障当成设计不可行"在数据结构上的落点。
    """
    # ---- 1) 技术故障优先：只要给了 error，就是故障，不再看别的 ----
    if error is not None:
        return _failure(_error_payload(error), study_id=study_id,
                        design=design, extra=extra,
                        template=(result or {}).get("template")
                        if isinstance(result, dict) else None)

    template = None
    if isinstance(result, dict):
        study_id = study_id or result.get("study_id")
        template = result.get("template")
        if design is None and result.get("parameters"):
            design = result.get("parameters")
        if check_summary is None:
            chk = result.get("check") or {}
            check_summary = chk.get("summary")

        # 网格破戒（ADR-0010）属技术故障：结论不可信，不能作为"设计不可行"
        mq = result.get("mesh_quality") or {}
        n_cross = mq.get("n_cross_bay_elems") or 0
        n_hang = mq.get("n_hanging_nodes") or 0
        if n_cross or n_hang:
            return _failure(
                {"kind": ErrorKind.MESH_VIOLATION, "type": "MeshViolation",
                 "message": "网格破戒：跨自然格单元 %d 个、悬挂节点 %d 个"
                            "（ADR-0010 不成立，校核结论不可交付）"
                            % (n_cross, n_hang),
                 "n_cross_bay_elems": n_cross, "n_hanging_nodes": n_hang},
                study_id=study_id, design=design, template=template,
                extra=extra)

        # 建模阶段就失败（run_study 抛过异常但我们只拿到结果）时无 check
        if not check_summary:
            return _failure(
                {"kind": ErrorKind.NO_CHECK, "type": "NoCheckResult",
                 "message": "结果里没有校核汇总（check.summary）：本次运行未跑到"
                            "强度校核，不能作为设计可行性的证据"},
                study_id=study_id, design=design, template=template,
                extra=extra)

    # ---- 2) 约束（复用既有校核结论） ----
    try:
        cons = constraints(check_summary)
    except ObjectiveError as exc:
        return _failure(_error_payload(exc), study_id=study_id, design=design,
                        template=template, extra=extra)

    # ---- 3) 目标（质量） ----
    mass_info = mass_result
    if mass_info is None and mass_kg is not None:
        mass_info = float(mass_kg)
    if mass_info is None and isinstance(result, dict):
        if result.get("mass"):
            mass_info = result.get("mass")
        else:
            mass_info = _mass_from_result(result, density=density,
                                          stiffeners=stiffeners)

    if mass_info is None:
        # 跑通了校核但算不出目标值：这是"目标口径缺失"，属技术故障
        return _failure(
            {"kind": ErrorKind.NO_MASS, "type": "NoMassResult",
             "message": "没有质量结果（mass_result / mass_kg 均为空，且无法从"
                        "结果的 topology_model + fe_model.laminate 算出）："
                        "优化器没有可比的目标值"},
            study_id=study_id, design=design, template=template,
            extra=extra, constraints_info=cons)

    objective = _objective_block(mass_info)
    if objective["value"] is None:
        return _failure(
            {"kind": ErrorKind.NO_MASS, "type": "NoMassValue",
             "message": "质量结果里没有 value 字段"},
            study_id=study_id, design=design, template=template,
            extra=extra, constraints_info=cons)

    return {
        "ok": True,
        "feasible": cons["feasible"],
        "error": None,
        "objective": objective,
        "constraints": cons,
        "mass": mass_info if isinstance(mass_info, dict) else None,
        "study_id": study_id,
        "template": template,
        "design": design,
        "extra": dict(extra or {}),
    }


def _failure(payload, study_id=None, design=None, template=None, extra=None,
             constraints_info=None):
    """统一的故障形态：ok=False / feasible=None / objective.value=None。"""
    return {
        "ok": False,
        # 恒为 None：绝不与"设计不可行"(False) 混淆
        "feasible": None,
        "error": payload,
        "objective": {"name": "mass", "direction": OBJECTIVE_DIRECTION,
                      "value": None, "unit": None, "components": [],
                      "basis": None},
        "constraints": constraints_info,
        "mass": None,
        "study_id": study_id,
        "template": template,
        "design": design,
        "extra": dict(extra or {}),
    }


# ---------------------------------------------------------------------------
# 便捷查询（优化器/日志共用，避免各写一份判定）
# ---------------------------------------------------------------------------

def is_feasible(ev):
    """该评估点能否参与"最优"比较（跑通且满足约束）。"""
    return bool(ev) and ev.get("ok") is True and ev.get("feasible") is True


def is_technical_failure(ev):
    """是否技术故障（ok=False）—— 优化器应单独归类，不参与目标比较。"""
    return bool(ev) and ev.get("ok") is False


def sort_key(ev):
    """
    排序键：可行点按目标值升序；不可行/故障点排在后面。

    返回 (group, value)：group=0 可行、1 跑通但不可行、2 技术故障。
    故障点的 value 用 +inf，绝不会被选成"最优"。
    """
    if not ev or ev.get("ok") is False:
        return (2, float("inf"))
    value = (ev.get("objective") or {}).get("value")
    if value is None:
        return (2, float("inf"))
    if ev.get("feasible") is True:
        return (0, float(value))
    return (1, float(value))


def summarize(ev):
    """人类可读一行摘要（日志/报告/UI 共用）。"""
    if not ev:
        return "评估: 无结果"
    if ev.get("ok") is False:
        err = ev.get("error") or {}
        return "评估: 技术故障[%s] %s" % (err.get("kind"), err.get("message"))
    obj = ev.get("objective") or {}
    cons = ev.get("constraints") or {}
    values = cons.get("values") or {}
    return ("评估: 质量 %s kg（%s）, %s, max_fi=%s, min_ms=%s"
            % (obj.get("value"),
               "+".join(obj.get("components") or []) or "未声明口径",
               "可行" if ev.get("feasible") else "不可行",
               values.get("max_fi"), values.get("min_ms")))
