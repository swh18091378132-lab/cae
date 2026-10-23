# -*- coding: utf-8 -*-
"""
fe_build.py -- 生成 Abaqus 输入文件 (.inp)：铺层(CAE 属性) + 载荷 + 边界

原则（ADR-0005）：复合材料铺层作为 **FE 截面/layup 属性**定义，不进入 CATIA 几何。
ADR-0010：网格已按自然格划分，每个单元唯一归属一个自然格；单元集合按自然格分组。

**本文件将在 Abaqus 内置 Python 2.6.2 中执行**：
  - 禁止 f-string / dict 推导 / set 推导 / yield from / 非 local / 变量注解
  - 只用 % 或 .format() 做字符串格式化
  - 通过 abaqus_capabilities/compat_check.py 静态检查

坐标系：参数空间 y(展向) / xi(弦向 0..1)。这里把 xi 线性映射到物理弦向坐标 X。
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

    # ---- 单元（S4R 壳，复合材料常用） ----
    add("*ELEMENT, TYPE=S4R, ELSET=ALL")
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

    # ---- 复合材料壳截面（铺层 = CAE 属性，ADR-0005） ----
    add("*SHELL SECTION, ELSET=ALL, COMPOSITE, OFFSET=0.")
    for p in laminate["plies"]:
        add("%s, 3, %s, %s"
            % (_fmt(p["thickness"]), material.get("name", "CFRP"), _fmt(p["angle"])))

    # ---- 接触定义（票14）：写在 *STEP 之前（模型数据区） ----
    for ln in _contact_block(analysis):
        add(ln)

    # ---- 分析步 ----
    # analysis（票14）优先于 step：不同分析类型在 Abaqus 侧是完全不同的分析步写法。
    stype, step_block = _step_block(step, analysis, model_dict)
    add("*STEP, NAME=%s" % step.get("name", "Step-1"))
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
# 本文件在 Abaqus 内置 Python 2.7 执行，故只用 2.7 语法。
# ---------------------------------------------------------------------------

ANALYSIS_KINDS = ("static", "frequency", "buckle", "contact", "nonlinear",
                  "explicit")


def default_analysis(kind="static"):
    """返回一个分析类型声明 dict（缺省参数齐备，便于 fake/real 同签名）。"""
    kind = kind or "static"
    base = {"kind": kind}
    if kind == "frequency":
        base["n_modes"] = 10
    elif kind == "buckle":
        base["n_modes"] = 5
        base["load_factor_span"] = 2.0
    elif kind == "contact":
        # 接触对：每个 {'master': elset, 'slave': elset, 'friction': mu}
        base["contacts"] = []
        base["nlgeom"] = True
    elif kind == "nonlinear":
        base["nlgeom"] = True
        base["nonlinearity"] = "material"   # material | geometry | both
        base["max_increments"] = 100
    elif kind == "explicit":
        base["time_period"] = 0.01
        base["mass_scaling"] = 1.0
        base["n_increments_hint"] = 1000
    return base


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

    if kind == "frequency":
        n = int((analysis or step).get("n_modes", 10))
        return kind, ["*FREQUENCY", "%d, , , , ," % n]
    if kind == "buckle":
        n = int((analysis or step).get("n_modes", 5))
        return kind, ["*BUCKLE", "%d, , , , ," % n]
    if kind == "explicit":
        a = analysis or {}
        period = a.get("time_period", 0.01)
        lines = ["*DYNAMIC, EXPLICIT"]
        lines.append("%s, %s" % (_fmt(period), _fmt(period)))
        # 质量缩放：给一个稳定时间增量缩放因子，显式分析常用手法
        if a.get("mass_scaling"):
            lines.insert(1, "*FIXED MASS SCALING, FACTOR=%s, TYPE=UNIFORM"
                         % _fmt(a.get("mass_scaling")))
        return kind, lines
    if kind == "contact":
        # 接触分析：静力步 + 几何非线性，接触定义在 *STEP 之前由 _contact_block 写
        return kind, ["*STATIC", "0.1, 1.0, 1e-05, 1.0", "*NLGEOM"]
    if kind == "nonlinear":
        a = analysis or {}
        lines = ["*STATIC"]
        max_inc = int(a.get("max_increments", 100))
        lines.append("0.1, 1.0, 1e-05, 1.0, , %d" % max_inc)
        if a.get("nlgeom") or a.get("nonlinearity") in ("geometry", "both"):
            lines.append("*NLGEOM")
        return kind, lines
    return "static", ["*STATIC", "0.1, 1.0, 1e-05, 1.0"]


def _contact_block(analysis):
    """
    产出接触定义行（*SURFACE / *CONTACT PAIR / *SURFACE INTERACTION）。

    接触对写在 *STEP 之前（模型数据），这是 Abaqus 的合法位置。
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
        lines.append("*SURFACE INTERACTION, NAME=INT-%d" % i)
        lines.append("*FRICTION")
        lines.append("%s, 0." % _fmt(mu))
        lines.append("*SURFACE, TYPE=ELEMENT, NAME=SURF-M%d" % i)
        lines.append("%s, S%d" % (master, int(cp.get("master_face", 1))))
        lines.append("*SURFACE, TYPE=ELEMENT, NAME=SURF-S%d" % i)
        lines.append("%s, S%d" % (slave, int(cp.get("slave_face", 2))))
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
