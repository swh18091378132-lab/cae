# -*- coding: utf-8 -*-
"""
fe_build.py -- 生成 Abaqus 输入文件 (.inp)：铺层(CAE 属性) + 载荷 + 边界

原则（ADR-0005）：复合材料铺层作为 **FE 截面/layup 属性**定义，不进入 CATIA 几何。
ADR-0010：网格已按自然格划分，每个单元唯一归属一个自然格；单元集合按自然格分组。

**本文件将在 Abaqus 内置 Python 2.7.15 中执行**（ADR-0012 实测版本）：
  - 禁止 f-string / yield from / nonlocal / 变量与参数注解 / 解包扩展
  - 只用 % 或 .format() 做字符串格式化
  - 通过 abaqus_capabilities/compat_check.py 静态检查

坐标系：参数空间 y(展向) / xi(弦向 0..1)。这里把 xi 线性映射到物理弦向坐标 X。

票14：分析类型只影响**分析步块**与**接触/材料非线性定义**（见文件末尾），
网格 / 材料 / 铺层 / 载荷 / 边界完全复用，结果抽取口径也不变（ADR-0006）。
"""

import math


# ---------------------------------------------------------------------------
# 铺层（ laminate ）定义：作为截面属性
# ---------------------------------------------------------------------------

def make_laminate(plies):
    """
    plies: [{'angle': float, 'thickness': float, 'material': str}]
    返回规范化的 layup 描述（供写 *SHELL SECTION / *ELASTIC 等使用）。
    """
    total = 0.0
    norm = []
    for p in plies:
        t = float(p["thickness"])
        total += t
        norm.append({
            "angle": float(p["angle"]),
            "thickness": t,
            "material": p.get("material", "CFRP"),
        })
    return {"plies": norm, "n_plies": len(norm), "total_thickness": total}


def default_quasi_isotropic(n_plies=8, thickness=0.125, material="CFRP"):
    """
    默认准各向同性铺层 [0/45/-45/90]_s，仅当用户未提供铺层时使用。
    """
    base = [0.0, 45.0, -45.0, 90.0]
    angles = []
    half = n_plies // 2
    k = 0
    while len(angles) < half:
        angles.append(base[k % 4])
        k += 1
    angles = angles + [-a for a in reversed(angles)]
    plies = [{"angle": a, "thickness": thickness, "material": material}
             for a in angles[:n_plies]]
    return make_laminate(plies)


# ---------------------------------------------------------------------------
# .inp 写出
# ---------------------------------------------------------------------------

def _fmt(v):
    return "%.9g" % float(v)


def write_inp(path, mesh_dict, model_dict, laminate, material, step,
              load, bc, params, extents=None, analysis=None):
    """
    写出 Abaqus .inp。

    mesh_dict  : meshgen.Mesh.to_dict()
    model_dict : topology.TopologyModel.to_dict()
    laminate   : make_laminate() 的返回
    material   : {'E1','E2','nu12','G12','G13','G23','rho'}
    step       : {'name':'Static','type':'static'} / 'frequency' / 'buckle'
    load       : {'type':'pressure'|'tip_force','magnitude':float}
    bc         : {'fix_bay': 'root'} 等
    params     : 参数集（用于坐标缩放）
    extents    : topology.TopologyModel.extents() —— 参数空间包围盒与物理换算。
                 **部件族无关**：翼面族的 y=展向、筒段族的 y=轴向，xi 一律乘
                 xi_scale 换成物理长度。None 时回退到 P1 的 span/c_root 口径。
    analysis   : 分析类型声明（票14）；None 则按 step 决定分析步。
                 声明里被消费的键：kind / step_name / n_modes / time_period /
                 mass_scaling / n_field_outputs / element_type / nlgeom /
                 nonlinearity / damage / contacts / max_increments / initial_increment。
                 **分析类型清单与默认值只在 core/analysis.py 定义**，这里不复制。

    坐标系：参数空间 y(纵向) / xi(横向 0..1)。xi 经 xi_scale 线性映射到物理 X，
    y 直接用物理值作 Y。这样翼面类（展向×弦向）与筒段类（轴向×周向展开）
    共用同一套坐标约定，Abaqus 侧无需区分部件族。
    """
    ext = extents or {}
    if "y_min" in ext:
        y_min = float(ext["y_min"])
        y_max = float(ext["y_max"])
        xi_min = float(ext["xi_min"])
        xi_max = float(ext["xi_max"])
        xi_scale = float(ext.get("xi_scale", 1.0))
    else:
        # P1 兼容回退：y 从 0 到 span，xi 乘根弦长
        y_min, y_max = 0.0, float(params["span"])
        xi_min, xi_max = 0.0, 1.0
        xi_scale = float(params["c_root"])

    span = y_max           # 命名沿用 P1：y 方向总长
    c_root = xi_scale       # 命名沿用 P1：xi 方向的物理尺度

    lines = []
    add = lines.append

    add("*HEADING")
    add("CAE platform - %s" % str(model_dict.get("template_id", "part")))
    add("*PREPRINT, ECHO=NO, MODEL=NO, HISTORY=NO, CONTACT=NO")

    # ---- 节点 ----
    add("*NODE")
    for n in mesh_dict["nodes"]:
        # xi(参数空间) -> 物理 X；y -> 物理 Y；壳位于平面
        x = (float(n["xi"]) - xi_min) * c_root
        y = float(n["y"])
        add("%d, %s, %s, 0." % (n["id"], _fmt(x), _fmt(y)))

    # ---- 单元（壳族；Explicit 可用 S4R 之外的显式壳，见 analysis.element_type） ----
    etype = _element_type(analysis)
    add("*ELEMENT, TYPE=%s, ELSET=ALL" % etype)
    for e in mesh_dict["elems"]:
        n = e["nodes"]
        add("%d, %d, %d, %d, %d" % (e["id"], n[0], n[1], n[2], n[3]))

    # ---- 按自然格分单元集合（便于逐格校核与结果提取） ----
    by_bay = {}
    for e in mesh_dict["elems"]:
        by_bay.setdefault(e["bay_id"], []).append(e["id"])
    for bay_id in sorted(by_bay):
        els = by_bay[bay_id]
        add("*ELSET, ELSET=%s" % _safe(bay_id))
        for i in range(0, len(els), 16):
            add(", ".join(str(x) for x in els[i:i + 16]))

    # ---- 材料 ----
    add("*MATERIAL, NAME=%s" % material.get("name", "CFRP"))
    add("*ELASTIC, TYPE=LAMINA")
    add("%s, %s, %s, %s, %s, %s, %s, %s, %s"
        % (_fmt(material["E1"]), _fmt(material["E2"]), _fmt(material["nu12"]),
           _fmt(material["G12"]), _fmt(material["G13"]), _fmt(material["G23"]),
           "0.", "0.", "0."))
    add("*DENSITY")
    add("%s," % _fmt(material.get("rho", 1600.0)))

    # ---- 材料非线性（票14）：复合材料 Hashin 损伤起始，写在 *MATERIAL 之后 ----
    for ln in _damage_block(analysis):
        add(ln)

    # ---- 复合材料壳截面（铺层 = CAE 属性，ADR-0005） ----
    add("*SHELL SECTION, ELSET=ALL, COMPOSITE, OFFSET=0.")
    for p in laminate["plies"]:
        add("%s, 3, %s, %s"
            % (_fmt(p["thickness"]), material.get("name", "CFRP"), _fmt(p["angle"])))

    # ---- 接触定义（票14）：写在 *STEP 之前（模型数据区） ----
    for ln in _contact_block(analysis):
        add(ln)

    # ---- 分析步 ----
    # analysis（票14）优先于 step：不同分析类型在 Abaqus 侧是完全不同的分析步写法,
    # 分析步名也随类型走（Explicit-1 / Frequency-1 ...），便于 ODB 里按类型辨认。
    stype, step_block = _step_block(step, analysis, model_dict)
    add("*STEP, NAME=%s%s" % (_step_name(step, analysis),
                              _step_params(analysis)))
    for ln in step_block:
        add(ln)

    # ---- 边界条件：固定根部（y = y_min 节点） ----
    root_nodes = [n["id"] for n in mesh_dict["nodes"]
                  if abs(float(n["y"]) - y_min) < 1e-9]
    if root_nodes:
        add("*BOUNDARY")
        for nid in root_nodes:
            add("%d, 1, 6, 0." % nid)

    # ---- 载荷 ----
    ltype = load.get("type", "pressure")
    mag = float(load.get("magnitude", 0.0))
    if ltype == "pressure":
        # 面压：S4R 壳单元用 P（P4 是实体单元面压，对 S4R 非法）
        add("*DLOAD")
        add("ALL, P, %s" % _fmt(mag))
    elif ltype == "tip_force":
        # 尖部（y = y_max）节点施加载荷方向的集中力
        tip_nodes = [n["id"] for n in mesh_dict["nodes"]
                     if abs(float(n["y"]) - y_max) < 1e-9]
        if tip_nodes:
            add("*CLOAD")
            for nid in tip_nodes:
                add("%d, 3, %s" % (nid, _fmt(mag / float(len(tip_nodes)))))

    # ---- 输出请求：显式动力学要指定场输出间隔，其余保持 P1 口径 ----
    n_intervals = int((analysis or {}).get("n_field_outputs", 0) or 0)
    if n_intervals > 0:
        add("*OUTPUT, FIELD, NUMBER INTERVAL=%d" % n_intervals)
    else:
        add("*OUTPUT, FIELD")
    add("*ELEMENT OUTPUT")
    add("S, E")
    add("*NODE OUTPUT")
    add("U, RF")
    add("*END STEP")

    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def _safe(name):
    """Abaqus 集合名：去掉不合法字符（保留字母数字下划线连字符）。"""
    out = []
    for ch in str(name):
        if ch.isalnum() or ch in "_-":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


# ---------------------------------------------------------------------------
# 分析类型（票14）
#
# 设计：分析类型只影响**分析步块**与**接触/非线性定义**；网格、材料、铺层、
# 载荷/边界完全复用。分析类型也不改变结果抽取格式（ADR-0006）——
# extract.py 对每种类型都吐同一套 element×ply×field CSV。
#
# 本文件在 Abaqus 内置 Python 2.7 执行，故只用 2.7 语法。
# **分析类型的清单与默认值只在 core/analysis.py 定义**（宿主侧唯一权威），
# 这里只消费传进来的声明 dict，不复制一份"影子定义"以免两处漂移。
#
# Abaqus 关键字要点（票14 修正，此前实现有 4 处会产生非法 .inp）：
#   * `*NLGEOM` **不是独立关键字**——几何非线性是 `*STEP` 的参数
#     （`*STEP, NAME=..., NLGEOM=YES`）。单写 `*NLGEOM` 会被 Abaqus 判为
#     未知关键字直接报错。
#   * `*STATIC` 数据行只有 4 项：(初始增量, 步长, 最小增量, 最大增量)。
#     最大增量数属于 `*STEP` 的 `INC` 参数，不是数据行第 6 项。
#   * `*FIXED MASS SCALING, TYPE=UNIFORM` 必须同时给 `DT`；只给质量缩放因子
#     时应写 `*FIXED MASS SCALING, FACTOR=<x>`（整步按因子缩放质量）。
#   * `*DYNAMIC, EXPLICIT` 走自动增量时，数据行第 1 项应为**空**
#     （空=让 Abaqus 估算稳定增量），第 2 项才是 time period。
# ---------------------------------------------------------------------------

# 壳单元的面标签：SPOS/SNEG 是壳的正/负法向面。
# 壳-壳接触几乎总是在这两个面上，而不是 1..6 里的边界面。
DEFAULT_MASTER_FACE = "SPOS"
DEFAULT_SLAVE_FACE = "SNEG"

_DEFAULT_SHELL_ELEMENT = "S4R"


def _element_type(analysis):
    """壳单元类型：默认 S4R；Explicit 可声明其他显式壳（铺层要求壳族）。"""
    et = str((analysis or {}).get("element_type") or _DEFAULT_SHELL_ELEMENT)
    return et.upper()


def _nlgeom(analysis):
    """
    是否要几何非线性。

    判定口径与 core/analysis.py 一致：显式 nlgeom 为真，或者 nonlinearity
    声明了 geometry/both。返回 True 时写进 `*STEP, NLGEOM=YES`。
    """
    a = analysis or {}
    if a.get("nlgeom"):
        return True
    return a.get("nonlinearity") in ("geometry", "both")


def _step_name(step, analysis):
    """
    分析步名。

    analysis 提供了 step_name 就用它（每个分析类型一个稳定名字，便于 ODB 定位）；
    未启用分析类型时沿用 P1 的 step['name']（零回归）。
    """
    if analysis:
        name = analysis.get("step_name")
        if name:
            return name
    return (step or {}).get("name", "Step-1")


def _step_params(analysis):
    """`*STEP` 关键字上的附加参数（目前只有 NLGEOM 与 INC）。"""
    parts = []
    if _nlgeom(analysis):
        parts.append("NLGEOM=YES")
    a = analysis or {}
    kind = a.get("kind") or ""
    if kind in ("nonlinear", "contact"):
        # 最大增量数是 *STEP, INC= 的参数（*STATIC 数据行里没有这一项）
        max_inc = int(a.get("max_increments", 100) or 100)
        if max_inc > 0:
            parts.append("INC=%d" % max_inc)
    if not parts:
        return ""
    return ", " + ", ".join(parts)


def _static_data_line(analysis, default_init=0.1):
    """`*STATIC` 数据行：初始增量, 步长, 最小增量, 最大增量（仅 4 项）。"""
    a = analysis or {}
    init = float(a.get("initial_increment", default_init) or default_init)
    return "%s, 1.0, 1e-05, 1.0" % _fmt(init)


def _step_block(step, analysis, model_dict):
    """
    产出 (分析步类型, [分析步关键字行])。

    analysis 为 None 时按 step['type'] 走 P1 行为（零回归）。
    """
    step = step or {}
    if analysis:
        kind = analysis.get("kind", "static")
    else:
        kind = step.get("type", "static")
    a = analysis or {}

    if kind == "frequency":
        n = int((analysis or step).get("n_modes", 10))
        # *FREQUENCY 数据行：特征值个数, 最小频率, 最大频率
        return kind, ["*FREQUENCY", "%d, ," % n]
    if kind == "buckle":
        n = int((analysis or step).get("n_modes", 5))
        # *BUCKLE 数据行：特征值个数, 精度, 最大迭代次数
        return kind, ["*BUCKLE", "%d, , ," % n]
    if kind == "explicit":
        period = float(a.get("time_period", 0.01))
        # *STEP 之后必须紧跟过程关键字（*DYNAMIC），再是它的数据行；
        # 质量缩放是步级历史数据，写在数据行**之后**。
        lines = ["*DYNAMIC, EXPLICIT"]
        # 自动增量：第 1 项空着（让 Abaqus 估稳定增量），第 2 项是 time period
        lines.append(", %s" % _fmt(period))
        # 质量缩放：整步一次按因子缩放质量。FACTOR=1 即不缩放（合法且显式留痕）。
        lines.append("*FIXED MASS SCALING, FACTOR=%s"
                     % _fmt(float(a.get("mass_scaling", 1.0))))
        return kind, lines
    if kind == "contact":
        # 接触分析：静力步（NLGEOM 由 _step_params 写在 *STEP 上）；
        # 接触定义在 *STEP 之前由 _contact_block 写。
        return kind, ["*STATIC", _static_data_line(a)]
    if kind == "nonlinear":
        return kind, ["*STATIC", _static_data_line(a)]
    # 静态默认：与 P1 完全一致（零回归）
    return "static", ["*STATIC", "0.1, 1.0, 1e-05, 1.0"]


def _damage_block(analysis):
    """
    材料非线性（票14）：复合材料 Hashin 损伤起始。

    产出的关键字紧跟 `*ELASTIC`/`*DENSITY`（同属一个 `*MATERIAL`），
    只有 `nonlinearity` 声明了 material/both 时才写——这样"材料非线性"
    真的进了 .inp，而不是只在宿主侧的一个布尔标记。
    """
    a = analysis or {}
    if a.get("kind") != "nonlinear":
        return []
    if a.get("nonlinearity") not in ("material", "both"):
        return []
    damage = a.get("damage")
    if not damage:
        return []

    def g(key):
        return _fmt(float(damage.get(key, 0.0)))

    lines = ["*DAMAGE INITIATION, CRITERION=HASHIN, ALPHA=1."]
    # 数据行：Xt, Xc, Yt, Yc, Sl, St（纵向/横向拉压强度 + 纵横剪切强度）
    lines.append("%s, %s, %s, %s, %s, %s"
                 % (g("Xt"), g("Xc"), g("Yt"), g("Yc"), g("Sl"), g("St")))
    return lines


def _face_label(value, default):
    """接触面标签：整数面号 -> 'S<n>'；已有字母标签原样大写。"""
    if value is None or value == "":
        return default
    txt = str(value).strip().upper()
    try:
        return "S%d" % int(txt)
    except ValueError:
        return txt


def _contact_block(analysis):
    """
    产出接触定义行（*SURFACE / *CONTACT PAIR / *SURFACE INTERACTION）。

    接触对写在 *STEP 之前（模型数据区），这是 Abaqus 的合法位置。
    `analysis["contacts"]` 里声明的每个接触对都在这里落成 .inp 关键字，
    于是"声明了接触对"与"模型里真的有接触"是同一件事。
    """
    if not analysis:
        return []
    pairs = analysis.get("contacts") or []
    if not pairs:
        return []

    lines = []
    for i, cp in enumerate(pairs, 1):
        master = _safe(cp.get("master", ""))
        slave = _safe(cp.get("slave", ""))
        if not master or not slave:
            continue
        mu = float(cp.get("friction", 0.2))
        mface = _face_label(cp.get("master_face"), DEFAULT_MASTER_FACE)
        sface = _face_label(cp.get("slave_face"), DEFAULT_SLAVE_FACE)
        lines.append("*SURFACE INTERACTION, NAME=INT-%d" % i)
        lines.append("*FRICTION")
        # 摩擦数据行：摩擦系数, 滑移速度（0 = 静摩擦/无速度相关）
        lines.append("%s, 0." % _fmt(mu))
        lines.append("*SURFACE, TYPE=ELEMENT, NAME=SURF-M%d" % i)
        lines.append("%s, %s" % (master, mface))
        lines.append("*SURFACE, TYPE=ELEMENT, NAME=SURF-S%d" % i)
        lines.append("%s, %s" % (slave, sface))
        lines.append("*CONTACT PAIR, INTERACTION=INT-%d, TYPE=SURFACE TO SURFACE"
                     % i)
        lines.append("SURF-S%d, SURF-M%d" % (i, i))
    return lines


def default_material():
    return {
        "name": "CFRP",
        "E1": 135000.0,     # MPa
        "E2": 10000.0,
        "nu12": 0.3,
        "G12": 5000.0,
        "G13": 5000.0,
        "G23": 3800.0,
        "rho": 1600.0,
    }
