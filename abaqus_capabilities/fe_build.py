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
              load, bc, params):
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
    """
    span = float(params["span"])
    c_root = float(params["c_root"])

    lines = []
    add = lines.append

    add("*HEADING")
    add("CAE platform P1 - wing box")
    add("*PREPRINT, ECHO=NO, MODEL=NO, HISTORY=NO, CONTACT=NO")

    # ---- 节点 ----
    add("*NODE")
    for n in mesh_dict["nodes"]:
        # xi(0..1) -> 物理 X；y -> 物理 Y；壳位于平面
        x = float(n["xi"]) * c_root
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

    # ---- 分析步 ----
    stype = step.get("type", "static")
    add("*STEP, NAME=%s" % step.get("name", "Step-1"))
    if stype == "static":
        add("*STATIC")
        add("0.1, 1.0, 1e-05, 1.0")
    elif stype == "frequency":
        add("*FREQUENCY")
        add("%d, , , , ," % int(step.get("n_modes", 10)))
    elif stype == "buckle":
        add("*BUCKLE")
        add("%d, , , , ," % int(step.get("n_modes", 5)))
    else:
        add("*STATIC")
        add("0.1, 1.0, 1e-05, 1.0")

    # ---- 边界条件：固定根部（y = 0 节点） ----
    root_nodes = [n["id"] for n in mesh_dict["nodes"] if abs(float(n["y"])) < 1e-9]
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
        # 尖部（y = span）节点施加展向?  这里施加载荷方向的集中力
        tip_nodes = [n["id"] for n in mesh_dict["nodes"]
                     if abs(float(n["y"]) - span) < 1e-9]
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
