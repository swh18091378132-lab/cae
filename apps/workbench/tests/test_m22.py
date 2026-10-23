# -*- coding: utf-8 -*-
"""
test_m22.py -- 票22 验收：设计向量声明与映射（含铺层轮次跳过数模再生）

验收点（对应 issue #22）：
  1. 设计向量可声明（**数据驱动**），含类型（continuous/discrete）、范围/候选值、
     类别（geometry/ply）；**新增变量只改声明，不改优化器**
  2. 向量 → (参数集, 铺层) 映射正确（几何变量与铺层变量混合的情形）
  3. 离散变量采样/吸附后仍是合法整数（不存在 6.37 这种肋数）
  4. 越出参数化包络时**明确报警**，不静默穿越（包络外应换模板）
  5. 纯铺层变动判定为"无需再生数模"；几何变动判定为"需要"
  6. `RunOptions.reuse_catpart_from` 生效：**CATIA 驱动调用次数为 0**，
     且数模产物与源 Study 同指纹
  7. 复用数模时血缘仍完整（拓扑/网格等仍能回溯到数模），并记"复用自某 Study"的边
  8. 不设 `reuse_catpart_from` 时行为与 P2 完全一致（零回归），签名不变
  9. Python 3.8 兼容（core/compat_py38.py），且核心模块零第三方依赖（不 import numpy）

设计要点（为什么这样切）：
  * 设计向量把「层数 + 每层角度 + 单层厚度」参数化：层数是一个离散变量，角度是**定长
    的角度槽**（第 k 层取 angles[k % len(angles)]），因此搜索空间是定长的、可去重可缓存，
    不必把不定长的铺层列表当数组搜索。
  * 包络守卫复用模板已声明的参数契约 + 跨参数包络（`template_registry.Template.validate`），
    不另造一套包络口径——否则两套口径迟早漂移。
  * `reuse_catpart_from` 的分工：**是否该复用**由 design_vector 判定（几何参数是否变化），
    **怎么复用**由 run_study 执行（跳过 regenerate、复制产物引用、补血缘边）。

运行 (在 apps/workbench 下): python tests/test_m22.py
"""

import os
import sys
import json
import shutil
import inspect
import random

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
sys.path.insert(0, CORE)
sys.path.insert(0, WORKBENCH)

import db
import study
import run_study
import design_vector
import template_registry
from run_study import RunOptions
from catia_driver import FakeCatiaDriver
from abaqus_runner import FakeAbaqusRunner


TMP_ROOT = os.path.join(WORKBENCH, ".tmp_test", "m22")

TEMPLATE_ID = "wingbox_v1"

# 设计向量声明：**纯数据**。几何变量进参数集，铺层变量经 ply_sequence 参数化。
DV_SPEC = {
    "id": "wingbox_dv_v1",
    "template_id": TEMPLATE_ID,
    "variables": [
        {"name": "span", "category": "geometry", "type": "continuous",
         "min": 5.0, "max": 40.0, "default": 5.0},
        {"name": "sweep_deg", "category": "geometry", "type": "continuous",
         "min": 0.0, "max": 60.0, "default": 10.0},
        {"name": "c_root", "category": "geometry", "type": "continuous",
         "min": 0.5, "max": 8.0, "default": 1.0},
        {"name": "c_tip", "category": "geometry", "type": "continuous",
         "min": 0.2, "max": 5.0, "default": 0.6},
        {"name": "n_ribs", "category": "geometry", "type": "discrete",
         "min": 2, "max": 40, "step": 1, "default": 3},
        {"name": "n_stringers", "category": "geometry", "type": "discrete",
         "min": 2, "max": 60, "step": 1, "default": 4},
        # --- 铺层：层数 + 角度档位 + 单层厚度 ---
        # 角度用**整数档位索引**（0..3）+ angle_table 换算成角度。这样离散候选值是纯整数，
        # 优化器（票23）能原生做 ±1 邻域搜索；语义上仍是 0/45/-45/90（spec 用户故事 10
        # 说的"角度档位"）。
        {"name": "n_plies", "category": "ply", "type": "discrete",
         "min": 2, "max": 16, "step": 2, "default": 8},
        {"name": "angle_0", "category": "ply", "type": "discrete",
         "candidates": [0, 1, 2, 3], "default": 0},
        {"name": "angle_1", "category": "ply", "type": "discrete",
         "candidates": [0, 1, 2, 3], "default": 1},
        {"name": "angle_2", "category": "ply", "type": "discrete",
         "candidates": [0, 1, 2, 3], "default": 2},
        {"name": "angle_3", "category": "ply", "type": "discrete",
         "candidates": [0, 1, 2, 3], "default": 3},
        {"name": "ply_thickness", "category": "ply", "type": "continuous",
         "min": 0.05, "max": 0.3, "default": 0.1},
    ],
    "ply_sequence": {
        "n_plies": "n_plies",
        "angles": ["angle_0", "angle_1", "angle_2", "angle_3"],
        "thickness": "ply_thickness",
        "material": "CFRP",
        "order": "cyclic",
        "angle_table": [0.0, 45.0, -45.0, 90.0],
    },
}

# 一条合法几何（与 P1/P2 测试同口径：3 肋 × 4 长桁 → 6 个自然格 → 6 个单元）
BASE_VECTOR = {
    "span": 5.0, "sweep_deg": 10.0, "c_root": 1.0, "c_tip": 0.6,
    "n_ribs": 3, "n_stringers": 4,
    "n_plies": 8,
    "angle_0": 0, "angle_1": 1, "angle_2": 2, "angle_3": 3,
    "ply_thickness": 0.1,
}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

class CountingCatiaDriver(FakeCatiaDriver):
    """
    会计数的 fake 驱动 —— 票22 的核心可观测验收点就是「它一次都没被调用」。

    真机 CATIA 无许可证（ADR-0013），所以"有没有调用 CATIA"只能通过适配器计数观测；
    这也是唯一的诚实口径：run_study 里对 catia 的**唯一**接触点就是 regenerate()。
    """

    def __init__(self, out_dir, fail_on=None):
        FakeCatiaDriver.__init__(self, out_dir, fail_on=fail_on)
        self.calls = 0
        self.params_seen = []

    def regenerate(self, parameter_set, template):
        self.calls += 1
        self.params_seen.append(dict(parameter_set))
        return FakeCatiaDriver.regenerate(self, parameter_set, template)


def _fresh(name):
    d = os.path.join(TMP_ROOT, name)
    if os.path.isdir(d):
        shutil.rmtree(d)
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def _near(a, b, tol=1e-9):
    return abs(float(a) - float(b)) <= tol


def _spec():
    return design_vector.normalize_spec(DV_SPEC)


def _run_point(conn, tmp, sid, vector, catia, reuse_from=None,
               with_report=False, with_check=True, template_id=None):
    """按设计向量跑一个设计点（= 一个独立 Study）。"""
    spec = _spec()
    inputs = design_vector.to_inputs(spec, vector)
    opts = RunOptions(
        params_dir=os.path.join(tmp, sid, "params"),
        topo_dir=os.path.join(tmp, sid, "topo"),
        with_topology=True,
        with_fe_model=True,
        fe_dir=os.path.join(tmp, sid, "fe"),
        subdiv_per_bay=1,
        plies=inputs["plies"],
        abaqus=FakeAbaqusRunner(n_elems=6, n_plies=len(inputs["plies"] or []) or 2),
        with_solve=True, job_name="job1",
        solve_dir=os.path.join(tmp, sid, "solve"),
        with_check=with_check,
        check_dir=os.path.join(tmp, sid, "check"),
        rules={"criteria": ["tsai_wu"], "safety_factor": 1.0},
        with_report=with_report,
        report_dir=os.path.join(tmp, sid, "report"))
    if reuse_from:
        opts.set("reuse_catpart_from", reuse_from)
    study.create_study(sid, "M22 " + sid, conn=conn)
    return run_study.run_study(sid, inputs["params"],
                               template_id or inputs["template_id"],
                               catia, conn, opts)


# ---------------------------------------------------------------------------
# 1) 数据驱动声明
# ---------------------------------------------------------------------------

def test_declaration_is_data(fails):
    """声明是数据：类型/范围/类别可读；加变量只改声明，不改任何代码。"""
    spec = _spec()

    names = design_vector.variable_names(spec)
    for want in ("span", "n_ribs", "n_plies", "angle_0", "ply_thickness"):
        if want not in names:
            fails.append("声明里缺少变量 %r（现有 %r）" % (want, names))

    geo = design_vector.geometry_variable_names(spec)
    ply = design_vector.ply_variable_names(spec)
    if sorted(geo) != sorted(["span", "sweep_deg", "c_root", "c_tip",
                              "n_ribs", "n_stringers"]):
        fails.append("几何变量分类不符: %r" % (geo,))
    if sorted(ply) != sorted(["n_plies", "angle_0", "angle_1", "angle_2",
                              "angle_3", "ply_thickness"]):
        fails.append("铺层变量分类不符: %r" % (ply,))

    # 每个变量都带类型与类别（数据自描述）
    for name in names:
        var = design_vector.variable(spec, name)
        if var.get("type") not in (design_vector.TYPE_CONTINUOUS,
                                   design_vector.TYPE_DISCRETE):
            fails.append("变量 %s 的类型不合法: %r" % (name, var.get("type")))
        if var.get("category") not in (design_vector.CATEGORY_GEOMETRY,
                                       design_vector.CATEGORY_PLY):
            fails.append("变量 %s 的类别不合法: %r" % (name, var.get("category")))

    # 声明默认向量：未给全的向量由默认值补齐
    dv = design_vector.default_vector(spec)
    if dv["n_ribs"] != 3 or dv["n_plies"] != 8:
        fails.append("默认向量取值不符: n_ribs=%r n_plies=%r"
                     % (dv.get("n_ribs"), dv.get("n_plies")))
    partial = design_vector.validate_vector(spec, {"span": 7.0})
    if not _near(partial["span"], 7.0) or partial["n_ribs"] != 3:
        fails.append("validate_vector 未用默认值补齐缺省变量: %r" % partial)

    # ---- 核心：只加**声明**就能映射新变量，不碰任何代码 ----
    minimal = {
        "template_id": TEMPLATE_ID,
        "variables": [
            {"name": "span", "category": "geometry", "type": "continuous",
             "min": 5.0, "max": 40.0, "default": 5.0},
            {"name": "n_ribs", "category": "geometry", "type": "discrete",
             "min": 2, "max": 40, "step": 1, "default": 3},
        ],
    }
    s1 = design_vector.normalize_spec(minimal)
    d1 = design_vector.default_vector(s1)
    p1 = design_vector.to_params(s1, d1)
    if sorted(p1) != ["n_ribs", "span"]:
        fails.append("最小声明映射出的参数集不符: %r" % sorted(p1))

    # 改声明（数据）——加一个 c_tip 变量，映射结果随之变化
    extended = json.loads(json.dumps(minimal))
    extended["variables"].append(
        {"name": "c_tip", "category": "geometry", "type": "continuous",
         "min": 0.2, "max": 5.0, "default": 0.6})
    s2 = design_vector.normalize_spec(extended)
    d2 = design_vector.default_vector(s2)
    p2 = design_vector.to_params(s2, d2)
    if sorted(p2) != ["c_tip", "n_ribs", "span"]:
        fails.append("只加声明未能映射新变量: %r" % sorted(p2))
    if not _near(p2["c_tip"], 0.6):
        fails.append("新变量的默认值未生效: %r" % p2.get("c_tip"))

    # 变量名可与模板参数名不同（"param" 显式映射）
    renamed = json.loads(json.dumps(minimal))
    renamed["variables"].append(
        {"name": "rib_count", "category": "geometry", "type": "discrete",
         "min": 2, "max": 40, "step": 1, "default": 4, "param": "n_ribs"})
    renamed["variables"] = [v for v in renamed["variables"]
                            if v["name"] != "n_ribs"]
    s3 = design_vector.normalize_spec(renamed)
    p3 = design_vector.to_params(s3, design_vector.default_vector(s3))
    if p3.get("n_ribs") != 4:
        fails.append("param 显式映射未生效: %r" % p3)
    if "rib_count" in p3:
        fails.append("变量名不应直接进参数集（应经 param 映射）: %r" % sorted(p3))

    # 声明可从 JSON 文件加载（声明本身可落盘、可版本化）
    tmp = _fresh("spec")
    path = os.path.join(tmp, "dv.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(DV_SPEC, fh, ensure_ascii=False)
    from_file = design_vector.load_spec(path)
    if design_vector.variable_names(from_file) != names:
        fails.append("load_spec 从 JSON 文件加载结果与内存声明不一致")

    # 非法声明必须被拒绝（声明是数据 -> 数据错了要早报）
    bad_cases = []
    bad = json.loads(json.dumps(DV_SPEC))
    bad["variables"][0]["type"] = "bogus"
    bad_cases.append(("未知类型", bad))
    bad = json.loads(json.dumps(DV_SPEC))
    bad["variables"].append({"name": "span", "category": "geometry",
                             "type": "continuous", "min": 1.0, "max": 2.0})
    bad_cases.append(("重名变量", bad))
    bad = json.loads(json.dumps(DV_SPEC))
    bad["ply_sequence"]["angles"] = ["angle_0", "no_such_var"]
    bad_cases.append(("角度槽引用不存在的变量", bad))
    bad = json.loads(json.dumps(DV_SPEC))
    bad["variables"].append({"name": "orphan_ply", "category": "ply",
                             "type": "discrete", "candidates": [0, 90]})
    bad_cases.append(("铺层变量未被 ply_sequence 引用", bad))
    bad = json.loads(json.dumps(DV_SPEC))
    bad["variables"][0].pop("max")
    bad_cases.append(("连续变量缺 max", bad))
    # angle_table 与档位索引不符：表只有 2 档，候选却是 0..3
    bad = json.loads(json.dumps(DV_SPEC))
    bad["ply_sequence"]["angle_table"] = [0.0, 90.0]
    bad_cases.append(("角度档位索引超出角度表范围", bad))
    # angle_table 配了连续角度变量（档位索引只对离散变量有意义）
    bad = json.loads(json.dumps(DV_SPEC))
    bad["ply_sequence"]["angle_table"] = [0.0, 45.0, -45.0, 90.0]
    bad["variables"] = [dict(v, type="continuous", min=0.0, max=90.0) if
                        v["name"] == "angle_1" else v
                        for v in bad["variables"]]
    bad["variables"] = [dict((k, val) for k, val in v.items()
                            if k not in ("candidates",))
                        if v["name"] == "angle_1" else v
                        for v in bad["variables"]]
    bad_cases.append(("angle_table 配连续角度变量", bad))
    for why, data in bad_cases:
        try:
            design_vector.normalize_spec(data)
        except design_vector.DesignVectorError:
            continue
        fails.append("非法声明未被拒绝（%s）" % why)


# ---------------------------------------------------------------------------
# 2) 向量 → (参数集, 铺层) 混合映射
# ---------------------------------------------------------------------------

def test_mapping(fails):
    """几何变量进参数集；铺层变量按「层数 + 角度槽 + 厚度」展开成铺层列表。"""
    spec = _spec()
    dv = design_vector.validate_vector(spec, BASE_VECTOR)
    inputs = design_vector.to_inputs(spec, dv)

    params = inputs["params"]
    expected_params = {"span": 5.0, "sweep_deg": 10.0, "c_root": 1.0,
                       "c_tip": 0.6, "n_ribs": 3, "n_stringers": 4}
    if sorted(params) != sorted(expected_params):
        fails.append("参数集应只含几何变量: %r" % sorted(params))
    for k, v in expected_params.items():
        if not _near(params.get(k, float("nan")), v):
            fails.append("参数 %s 映射错误: %r != %r" % (k, params.get(k), v))

    # 铺层：8 层，角度按 4 个角度槽循环
    plies = inputs["plies"]
    if not isinstance(plies, list) or len(plies) != 8:
        fails.append("铺层应展开成 8 层，实际 %r"
                     % (len(plies) if isinstance(plies, list) else plies))
        return
    expect_angles = [0.0, 45.0, -45.0, 90.0, 0.0, 45.0, -45.0, 90.0]
    got_angles = [p["angle"] for p in plies]
    if not all(_near(a, b) for a, b in zip(got_angles, expect_angles)):
        fails.append("铺层角度展开错误: %r != %r" % (got_angles, expect_angles))
    for p in plies:
        if not _near(p["thickness"], 0.1):
            fails.append("单层厚度映射错误: %r" % p["thickness"])
            break
        if p.get("material") != "CFRP":
            fails.append("材料未从声明带入: %r" % p.get("material"))
            break

    # 层数变化 -> 铺层长度随之变化（参数化，不是把列表当数组）
    dv2 = dict(dv)
    dv2["n_plies"] = 4
    inputs2 = design_vector.to_inputs(spec, dv2)
    if len(inputs2["plies"]) != 4:
        fails.append("层数改为 4 后铺层长度应为 4，实际 %d" % len(inputs2["plies"]))
    if [p["angle"] for p in inputs2["plies"]] != [0.0, 45.0, -45.0, 90.0]:
        fails.append("层数改为 4 后角度展开错误: %r"
                     % [p["angle"] for p in inputs2["plies"]])

    # 厚度变化
    dv3 = dict(dv)
    dv3["ply_thickness"] = 0.2
    total = sum(p["thickness"] for p in design_vector.to_inputs(spec, dv3)["plies"])
    if not _near(total, 1.6):
        fails.append("总厚应为 8×0.2=1.6，实际 %r" % total)

    # 对称铺层（order=mirror）也支持，且仍是定长角度槽
    mirrored = json.loads(json.dumps(DV_SPEC))
    mirrored["ply_sequence"]["order"] = "mirror"
    sm = design_vector.normalize_spec(mirrored)
    pm = design_vector.to_inputs(sm, dv)["plies"]
    if [p["angle"] for p in pm] != [0.0, 45.0, -45.0, 90.0, 90.0, -45.0, 45.0, 0.0]:
        fails.append("mirror 铺层展开错误: %r" % [p["angle"] for p in pm])

    # 纯几何声明（无铺层）-> plies 为 None（由 run_study 沿用默认铺层）
    geo_only = {
        "template_id": TEMPLATE_ID,
        "variables": [{"name": "span", "category": "geometry",
                       "type": "continuous", "min": 5.0, "max": 40.0,
                       "default": 5.0}],
    }
    sg = design_vector.normalize_spec(geo_only)
    if design_vector.to_inputs(sg, design_vector.default_vector(sg))["plies"] is not None:
        fails.append("无铺层声明时 plies 应为 None（沿用默认铺层）")


# ---------------------------------------------------------------------------
# 3) 离散变量守住整数性
# ---------------------------------------------------------------------------

def test_discrete_integrality(fails):
    """离散变量怎么采样/吸附，落到设计点上都是合法取值（肋数不会出现 6.37）。"""
    spec = _spec()

    legal = design_vector.discrete_values(spec, "n_ribs")
    if legal[:3] != [2, 3, 4] or legal[-1] != 40:
        fails.append("n_ribs 的合法取值集合不符: %r" % (legal[:3] + legal[-1:],))
    if not all(isinstance(v, int) for v in legal):
        fails.append("n_ribs 的合法取值应为整数: %r" % (legal[:5],))

    # 层数按 step=2：2,4,...,16
    ply_legal = design_vector.discrete_values(spec, "n_plies")
    if ply_legal != [2, 4, 6, 8, 10, 12, 14, 16]:
        fails.append("n_plies 的合法取值集合不符: %r" % (ply_legal,))

    # 角度档位：显式候选值是**整数档位索引**，经 angle_table 换算成角度
    ang_legal = design_vector.discrete_values(spec, "angle_0")
    if ang_legal != [0, 1, 2, 3]:
        fails.append("angle_0 的候选档位不符: %r" % (ang_legal,))
    if not all(isinstance(v, int) for v in ang_legal):
        fails.append("角度档位应为整数（优化器离散候选值只接受整数）: %r" % (ang_legal,))
    if design_vector.ply_sequence(spec).get("angle_table") != [0.0, 45.0, -45.0, 90.0]:
        fails.append("angle_table 未保留: %r"
                     % (design_vector.ply_sequence(spec).get("angle_table"),))

    # 任意实数吸附后必落在合法集合里；整数型离散变量吸附后必为 int
    rng = random.Random(20261023)
    int_vars = ("n_ribs", "n_stringers", "n_plies")
    for name in int_vars:
        legal_set = set(design_vector.discrete_values(spec, name))
        for _ in range(400):
            raw = rng.uniform(-5.0, 50.0)
            got = design_vector.snap_value(spec, name, raw)
            if got not in legal_set:
                fails.append("%s 吸附结果 %r 不在合法集合里（原始 %r）"
                             % (name, got, raw))
                break
            if not isinstance(got, int):
                fails.append("%s 吸附结果不是整数: %r（原始 %r）"
                             % (name, got, raw))
                break
            if abs(float(got) - round(float(got))) > 1e-9:
                fails.append("%s 出现了非整数值: %r" % (name, got))
                break

    # 明确断言：不会出现 6.37 这类设计点
    if design_vector.snap_value(spec, "n_ribs", 6.37) == 6.37:
        fails.append("n_ribs 吸附后仍是 6.37（离散整数性失守）")
    if design_vector.snap_value(spec, "n_plies", 5.9) == 5.9:
        fails.append("n_plies 吸附后仍是 5.9（离散整数性失守）")

    # 整个向量一起吸附：全部离散变量都合法
    sampled = {}
    for name in design_vector.variable_names(spec):
        var = design_vector.variable(name=name, spec=spec)
        if var["type"] == design_vector.TYPE_DISCRETE:
            sampled[name] = rng.uniform(-3.0, 45.0)
        else:
            sampled[name] = rng.uniform(var["min"], var["max"])
    coerced = design_vector.coerce_vector(spec, sampled)
    for name in int_vars:
        if not isinstance(coerced[name], int):
            fails.append("coerce_vector 后 %s 不是整数: %r" % (name, coerced[name]))
    # 吸附后的向量必须能通过严格校验
    design_vector.validate_vector(spec, coerced)

    # 非法值（不在离散集合内）不得被静默接受
    try:
        design_vector.validate_vector(spec, dict(BASE_VECTOR, n_ribs=3.7))
        fails.append("n_ribs=3.7 未被拒绝（离散整数性失守）")
    except design_vector.DesignVectorError:
        pass


# ---------------------------------------------------------------------------
# 4) 包络守卫：明确报警，不静默穿越
# ---------------------------------------------------------------------------

def test_envelope_guard(fails):
    """越出参数化包络必须报警；包络外应换模板，不是靠搜索穿越。"""
    spec = _spec()
    tpl = template_registry.load(TEMPLATE_ID)

    # 合法点：包络通过
    ok = design_vector.envelope_report(
        spec, design_vector.validate_vector(spec, BASE_VECTOR), tpl)
    if not ok.get("ok"):
        fails.append("合法设计点被判为越出包络: %r" % ok.get("violations"))

    # 越包络 1：跨参数约束 span/(n_ribs-1) >= 0.3（span=5, n_ribs=20 -> 0.26）
    bad = dict(BASE_VECTOR)
    bad["n_ribs"] = 20
    rep = design_vector.envelope_report(
        spec, design_vector.validate_vector(spec, bad), tpl)
    if rep.get("ok"):
        fails.append("跨参数包络越界未被发现（span/(n_ribs-1) < 0.3）")
    msgs = " ".join(rep.get("messages") or [])
    if "包络" not in msgs:
        fails.append("包络报警文案未提到「包络」: %r" % msgs)
    if "换模板" not in msgs:
        fails.append("包络报警未提示「应换模板而不是靠搜索穿越」: %r" % msgs)

    # 越包络 2：c_tip < c_root
    bad2 = dict(BASE_VECTOR)
    bad2["c_root"] = 1.0
    bad2["c_tip"] = 2.5
    rep2 = design_vector.envelope_report(
        spec, design_vector.validate_vector(spec, bad2), tpl)
    if rep2.get("ok"):
        fails.append("c_tip < c_root 越界未被发现")

    # 严格模式：直接抛（拒绝静默穿越）
    try:
        design_vector.to_inputs(spec, bad, template=tpl, strict=True)
        fails.append("strict=True 时越包络未被拒绝")
    except design_vector.EnvelopeError:
        pass
    # 非严格模式：返回告警而不抛（供优化器把点标成"越包络"后跳过，而不是中断整次搜索）
    out = design_vector.to_inputs(spec, bad, template=tpl, strict=False)
    if not (out.get("warnings") or []):
        fails.append("strict=False 时越包络未产生告警")
    if "包络" not in " ".join(out.get("warnings") or []):
        fails.append("strict=False 的告警文案未提到包络: %r" % (out.get("warnings"),))
    if not out.get("params") or not out.get("plies"):
        fails.append("strict=False 时仍应返回可用的映射结果（供记录该点）")

    # 变量自身范围越界：结构性问题，直接拒绝（不是"越包络"而是"向量不合法"）
    try:
        design_vector.validate_vector(spec, dict(BASE_VECTOR, span=999.0))
        fails.append("变量超出声明范围未被拒绝")
    except design_vector.DesignVectorError:
        pass

    # 模板参数契约对不上（未知参数）也要被包络守卫捕获
    rogue = {
        "template_id": TEMPLATE_ID,
        "variables": [{"name": "no_such_param", "category": "geometry",
                       "type": "continuous", "min": 1.0, "max": 2.0,
                       "default": 1.5}],
    }
    sr = design_vector.normalize_spec(rogue)
    rep3 = design_vector.envelope_report(
        sr, design_vector.default_vector(sr), tpl)
    if rep3.get("ok"):
        fails.append("参数契约对不上（未知参数）未被包络守卫捕获")


# ---------------------------------------------------------------------------
# 5) 是否需要再生数模
# ---------------------------------------------------------------------------

def test_regeneration_decision(fails):
    """铺层变了、几何没变 -> 不需要再生数模；几何变了 -> 需要。"""
    spec = _spec()
    a = design_vector.validate_vector(spec, BASE_VECTOR)

    # 纯铺层变动：角度档位、层数、厚度各改一样
    for tag, patch in (("角度", {"angle_1": 0}),
                       ("层数", {"n_plies": 12}),
                       ("厚度", {"ply_thickness": 0.2})):
        b = dict(a)
        b.update(patch)
        b = design_vector.validate_vector(spec, b)
        d = design_vector.needs_regeneration(spec, a, b)
        if d["needs_regeneration"] is not False:
            fails.append("纯铺层变动（%s）应判定为无需再生数模，实际 %r"
                         % (tag, d))
        if d["changed_geometry"]:
            fails.append("纯铺层变动（%s）不应报告几何变量变化: %r"
                         % (tag, d["changed_geometry"]))
        if not d["changed_ply"]:
            fails.append("纯铺层变动（%s）未报告铺层变量变化" % tag)

    # 几何变动 -> 需要再生
    for tag, patch, expect_key in (
            ("展长", {"span": 6.0}, "span"),
            ("肋数", {"n_ribs": 4}, "n_ribs"),
            ("弦长", {"c_root": 1.2}, "c_root")):
        b = dict(a)
        b.update(patch)
        b = design_vector.validate_vector(spec, b)
        d = design_vector.needs_regeneration(spec, a, b)
        if d["needs_regeneration"] is not True:
            fails.append("几何变动（%s）应判定为需要再生数模，实际 %r" % (tag, d))
        if expect_key not in d["changed_params"]:
            fails.append("几何变动（%s）未在 changed_params 里点名 %r: %r"
                         % (tag, expect_key, d["changed_params"]))
        if not d["changed_geometry"]:
            fails.append("几何变动（%s）未报告几何变量变化" % tag)

    # 完全不变 -> 不需要再生
    d0 = design_vector.needs_regeneration(spec, a, dict(a))
    if d0["needs_regeneration"] is not False:
        fails.append("向量未变时应判定为无需再生，实际 %r" % d0)

    # 几何 + 铺层同时变 -> 需要再生（几何变了就得再生）
    both = dict(a)
    both.update({"span": 6.0, "n_plies": 10})
    d2 = design_vector.needs_regeneration(spec, a, both)
    if d2["needs_regeneration"] is not True:
        fails.append("几何+铺层同时变应判定为需要再生")
    if not d2["changed_ply"]:
        fails.append("几何+铺层同时变应同时报告铺层变化")


# ---------------------------------------------------------------------------
# 6) reuse_catpart_from：跳过 CATIA，复用数模与血缘
# ---------------------------------------------------------------------------

def test_reuse_catpart(conn, fails):
    """设了 reuse_catpart_from：CATIA 调用次数为 0，数模与源 Study 同指纹。"""
    tmp = _fresh("reuse")

    # 字段已加进 RunOptions（不改 run_study 签名）
    if not hasattr(RunOptions(), "reuse_catpart_from"):
        fails.append("RunOptions 缺 reuse_catpart_from 字段")
        return
    if RunOptions().reuse_catpart_from is not None:
        fails.append("reuse_catpart_from 默认应为 None（不设即 P2 行为）")

    # 源 Study：正常再生数模
    src_catia = CountingCatiaDriver(os.path.join(tmp, "cp_src"))
    src = _run_point(conn, tmp, "S-M22-src", BASE_VECTOR, src_catia)
    if src_catia.calls != 1:
        fails.append("源 Study 应调用 CATIA 1 次，实际 %d" % src_catia.calls)
    if "catpart_reuse" in src:
        fails.append("未设置复用时不应用 catpart_reuse 字段（零回归）")

    # 设计点 2：只改铺层（几何全未变）-> 复用源 Study 的数模
    ply_vector = dict(BASE_VECTOR)
    ply_vector.update({"n_plies": 12, "ply_thickness": 0.12, "angle_1": 0})
    spec = _spec()
    decision = design_vector.needs_regeneration(
        spec, design_vector.validate_vector(spec, BASE_VECTOR),
        design_vector.validate_vector(spec, ply_vector))
    if decision["needs_regeneration"] is not False:
        fails.append("前置条件：纯铺层变动应判定为无需再生，实际 %r" % decision)

    reuse_catia = CountingCatiaDriver(os.path.join(tmp, "cp_reuse"))
    reuse = _run_point(conn, tmp, "S-M22-reuse", ply_vector, reuse_catia,
                       reuse_from="S-M22-src")

    # ---- 核心验收点：CATIA 一次都没被调用 ----
    if reuse_catia.calls != 0:
        fails.append("复用数模时 CATIA 驱动被调用了 %d 次（应为 0）"
                     % reuse_catia.calls)
    if reuse_catia.params_seen:
        fails.append("复用数模时不应把参数送进 CATIA: %r" % reuse_catia.params_seen)

    # 数模产物与源 Study 同指纹（同一份文件、同一个内容指纹）
    if reuse.get("catpart_path") != src.get("catpart_path"):
        fails.append("复用后的数模路径与源 Study 不同: %r != %r"
                     % (reuse.get("catpart_path"), src.get("catpart_path")))
    if reuse.get("catpart_checksum") != src.get("catpart_checksum"):
        fails.append("复用后的数模指纹与源 Study 不同: %r != %r"
                     % (reuse.get("catpart_checksum"), src.get("catpart_checksum")))
    if not os.path.isfile(reuse.get("catpart_path") or ""):
        fails.append("复用后的数模文件不存在")

    # 产物里留痕：复用自哪个 Study / 哪个产物
    row = conn.execute("SELECT params_json FROM artifacts WHERE id=?",
                       (reuse["artifacts"]["catpart"],)).fetchone()
    meta = json.loads(row["params_json"])
    if meta.get("reused_from") != "S-M22-src":
        fails.append("数模产物未记录 reused_from: %r" % meta)
    if meta.get("reused_from_artifact") != src["artifacts"]["catpart"]:
        fails.append("数模产物未记录复用的源产物 id: %r" % meta)
    if meta.get("checksum") != src.get("catpart_checksum"):
        fails.append("数模产物记录的指纹与源不一致")

    if (reuse.get("catpart_reuse") or {}).get("from_study") != "S-M22-src":
        fails.append("result 未记录复用来源: %r" % reuse.get("catpart_reuse"))

    # ---- 血缘：记一条"复用自某 Study"的边 ----
    n = conn.execute(
        "SELECT COUNT(*) c FROM edges WHERE parent_id=? AND child_id=?",
        (src["artifacts"]["catpart"], reuse["artifacts"]["catpart"])).fetchone()["c"]
    if n != 1:
        fails.append("血缘缺少「复用自源 Study 数模」的边")

    # ---- 血缘仍完整：参数集 -> 数模 -> 拓扑 -> 网格 -> inp -> odb -> 抽取 -> 校核 ----
    arts = reuse["artifacts"]
    for parent, child in (("parameter_set", "catpart"), ("catpart", "topology"),
                          ("topology", "mesh"), ("mesh", "inp"), ("inp", "odb"),
                          ("odb", "extraction"), ("extraction", "check_result")):
        if parent not in arts or child not in arts:
            fails.append("复用链缺少产物 %s/%s" % (parent, child))
            continue
        n = conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE parent_id=? AND child_id=?",
            (arts[parent], arts[child])).fetchone()["c"]
        if n != 1:
            fails.append("复用链血缘边 %s->%s 缺失" % (parent, child))

    # 几何未变 -> 下游拓扑/网格与源 Study 一致（复用没有污染下游）
    if reuse["topology"]["n_total"] != src["topology"]["n_total"]:
        fails.append("复用后拓扑自然格数与源 Study 不一致: %r != %r"
                     % (reuse["topology"]["n_total"], src["topology"]["n_total"]))
    if reuse["fe"]["n_elems"] != src["fe"]["n_elems"]:
        fails.append("复用后单元数与源 Study 不一致: %r != %r"
                     % (reuse["fe"]["n_elems"], src["fe"]["n_elems"]))
    # 铺层确实变了（证明这是一次真正的"纯铺层轮次"）
    if reuse["fe"]["n_plies"] == src["fe"]["n_plies"]:
        fails.append("复用轮次的铺层数与源 Study 相同，未真正验证纯铺层变动")

    # 复用轮无需 CATIA 驱动也能跑（结构上保证"CATIA 一次都不会被调用"）
    none_run = _run_point(conn, tmp, "S-M22-nocatia", ply_vector, None,
                          reuse_from="S-M22-src")
    if none_run["catpart_path"] != src["catpart_path"]:
        fails.append("不传 CATIA 驱动时未能复用数模")


def test_reuse_guards(conn, fails):
    """复用是"有条件"的：几何变了、源 Study 不存在，都必须明确拒绝而不是静默错配。"""
    tmp = _fresh("reuse_guard")

    src_catia = CountingCatiaDriver(os.path.join(tmp, "cp"))
    _run_point(conn, tmp, "S-M22-g-src", BASE_VECTOR, src_catia)

    # 几何变了还复用 -> 拒绝（否则血缘会指向一个与拓扑不符的数模）
    changed = dict(BASE_VECTOR)
    changed["span"] = 6.0
    try:
        _run_point(conn, tmp, "S-M22-g-bad", changed,
                   CountingCatiaDriver(os.path.join(tmp, "cp2")),
                   reuse_from="S-M22-g-src")
        fails.append("几何参数变化时仍允许复用数模（会产出与拓扑不符的数模）")
    except run_study.StudyRunError as exc:
        if "复用" not in str(exc) and "几何" not in str(exc):
            fails.append("几何变化复用的拒绝原因不可读: %s" % exc)
    # 拒绝时不落 catpart 产物（不留半成品）
    n = conn.execute("SELECT COUNT(*) c FROM artifacts WHERE study_id=? "
                     "AND kind='catpart'", ("S-M22-g-bad",)).fetchone()["c"]
    if n != 0:
        fails.append("拒绝复用后仍落了 %d 个数模产物" % n)

    # 源 Study 不存在 -> 明确报错
    try:
        _run_point(conn, tmp, "S-M22-g-missing", BASE_VECTOR,
                   CountingCatiaDriver(os.path.join(tmp, "cp3")),
                   reuse_from="NO-SUCH-STUDY")
        fails.append("源 Study 不存在时未报错")
    except run_study.StudyRunError:
        pass

    # 源 Study 是自身 -> 拒绝（否则血缘 DAG 上出现自环）
    try:
        _run_point(conn, tmp, "S-M22-g-self", BASE_VECTOR,
                   CountingCatiaDriver(os.path.join(tmp, "cp4")),
                   reuse_from="S-M22-g-self")
        fails.append("复用自身未被拒绝（会在血缘 DAG 上造自环）")
    except run_study.StudyRunError:
        pass

    # 源 Study 存在但没跑过 CATIA（无数模产物）-> 明确报错，不是空指针
    study.create_study("S-M22-g-empty", "M22 空 Study", conn=conn)
    try:
        _run_point(conn, tmp, "S-M22-g-nocp", BASE_VECTOR,
                   CountingCatiaDriver(os.path.join(tmp, "cp5")),
                   reuse_from="S-M22-g-empty")
        fails.append("源 Study 没有数模产物时未报错")
    except run_study.StudyRunError as exc:
        if "数模" not in str(exc):
            fails.append("无数模产物的报错文案不可读: %s" % exc)

    # 源 Study 用不同模板 -> 拒绝（不同模板族的数模不可互换）
    try:
        _run_point(conn, tmp, "S-M22-g-tpl", BASE_VECTOR,
                   CountingCatiaDriver(os.path.join(tmp, "cp6")),
                   reuse_from="S-M22-g-src",
                   template_id="fuselage_section_v1")
        fails.append("跨模板族复用数模未被拒绝")
    except Exception as exc:
        # 参数契约本身也会先把机身参数拒掉；两种拒绝都可接受，
        # 但必须**是拒绝**而不是产出错误血缘
        if not isinstance(exc, (run_study.StudyRunError,
                                template_registry.ParamError)):
            fails.append("跨模板复用的拒绝类型不符: %r" % type(exc).__name__)


def test_no_regression(conn, fails):
    """不设 reuse_catpart_from 时行为与 P2 完全一致：CATIA 照常调用、血缘照常。"""
    tmp = _fresh("regression")

    # 签名不变（票10 的契约）
    names = list(inspect.signature(run_study.run_study).parameters.keys())
    expected = ["study_id", "parameter_set", "template_id", "catia", "conn",
                "options", "legacy"]
    if names != expected:
        fails.append("run_study 签名应保持 %r，实际 %r" % (expected, names))

    catia = CountingCatiaDriver(os.path.join(tmp, "cp"))
    res = _run_point(conn, tmp, "S-M22-base", BASE_VECTOR, catia)

    if catia.calls != 1:
        fails.append("未设置复用时 CATIA 应被调用 1 次，实际 %d" % catia.calls)
    if "catpart_reuse" in res:
        fails.append("未设置复用时不应出现 catpart_reuse 字段")
    for kind in ("parameter_set", "catpart", "topology", "mesh", "inp",
                 "odb", "extraction", "check_rule", "check_result"):
        if kind not in (res.get("artifacts") or {}):
            fails.append("零回归：缺少产物 %s" % kind)
    # 数模产物 params 里不应带复用痕迹
    row = conn.execute("SELECT params_json FROM artifacts WHERE id=?",
                       (res["artifacts"]["catpart"],)).fetchone()
    if "reused_from" in (row["params_json"] or ""):
        fails.append("未设置复用时数模产物却带 reused_from")

    # 与复用轮的拓扑/网格一致（同一几何 -> 同一份下游结构）
    reuse_catia = CountingCatiaDriver(os.path.join(tmp, "cp_r"))
    reuse = _run_point(conn, tmp, "S-M22-base-r", dict(BASE_VECTOR, n_plies=10),
                       reuse_catia, reuse_from="S-M22-base")
    if reuse["topology"] != res["topology"]:
        fails.append("纯铺层轮次的拓扑应与几何轮次一致: %r != %r"
                     % (reuse["topology"], res["topology"]))


# ---------------------------------------------------------------------------
# 7) 与优化器的接缝：声明可以被直接喂给 optimizer（票23/票25 的契约）
# ---------------------------------------------------------------------------

def test_optimizer_interop(fails):
    """
    设计向量声明必须能**原样**喂给优化器，中间不需要再写一层适配。

    这是本票真正的下游接缝：票25 要把 `spec["variables"]` 直接传给
    `optimizer.optimize(variables, evaluate)`。若两边对声明字段的解读不一致，
    就会出现"优化器采样的点 design_vector 不认"这种难查的错配。
    """
    try:
        import optimizer
    except ImportError:
        print("  [SKIP] 无 optimizer（票23 未合入）")
        return

    spec = _spec()
    try:
        nv = optimizer.normalize_variables(spec["variables"])
    except Exception as exc:
        fails.append("设计向量声明无法直接喂给优化器: %s: %s"
                     % (type(exc).__name__, exc))
        return
    if [v["name"] for v in nv] != design_vector.variable_names(spec):
        fails.append("优化器归一后的变量顺序/名字与声明不一致: %r"
                     % ([v["name"] for v in nv],))
    # 优化器眼里的离散取值域必须与 design_vector 的合法集合一致
    for name in ("n_ribs", "n_plies"):
        want = design_vector.discrete_values(spec, name)
        got = [v for v in nv if v["name"] == name][0]
        if "candidates" in got:
            cand = list(got["candidates"])
        else:
            lo, hi, step = got["min"], got["max"], got["step"]
            cand = []
            k = 0
            while lo + k * step <= hi + 1e-9:
                cand.append(lo + k * step)
                k += 1
        if len(cand) != len(want) or not all(
                _near(a, b) for a, b in zip(cand, want)):
            fails.append("优化器与 design_vector 对 %s 的离散取值域不一致:"
                         "\n    优化器 %r\n    design_vector %r"
                         % (name, cand, want))

    # 优化器采样出的向量必须能通过 design_vector 的严格校验
    import random
    rng = random.Random(7)
    for i in range(30):
        sampled = {}
        for var in spec["variables"]:
            if var["type"] == design_vector.TYPE_DISCRETE:
                legal = design_vector.discrete_values(spec, var["name"])
                sampled[var["name"]] = legal[rng.randrange(len(legal))]
            else:
                sampled[var["name"]] = rng.uniform(var["min"], var["max"])
        try:
            design_vector.validate_vector(spec, sampled)
        except design_vector.DesignVectorError as exc:
            fails.append("优化器风格采样出的设计点未被 design_vector 接受: %s"
                         % exc)
            break


# ---------------------------------------------------------------------------
# 8) Python 3.8 兼容 + 零第三方依赖
# ---------------------------------------------------------------------------

def test_py38(fails):
    """目标机 Win7 + CPython 3.8：禁用 3.9+ 语法；core 至今零第三方依赖。"""
    import compat_py38
    targets = [
        os.path.join(CORE, "design_vector.py"),
        os.path.join(CORE, "run_study.py"),
        os.path.join(HERE, "test_m22.py"),
    ]
    bad = compat_py38.check_paths(targets)
    if bad:
        fails.append("Python 3.8 兼容性检查未通过:\n%s"
                     % compat_py38.format_violations(bad))

    # 设计向量是纯算法：不得引入 numpy 等第三方依赖（核心层至今零依赖）
    with open(os.path.join(CORE, "design_vector.py"), "r", encoding="utf-8") as fh:
        src = fh.read()
    for banned in ("import numpy", "from numpy"):
        if banned in src:
            fails.append("design_vector.py 引入了第三方依赖: %r" % banned)


# ---------------------------------------------------------------------------

def main():
    fails = []
    if not os.path.isdir(TMP_ROOT):
        os.makedirs(TMP_ROOT)
    tmp = _fresh("db")
    conn = db.connect(os.path.join(tmp, "m22.sqlite"))

    print("1. 设计向量声明是数据（加变量不改代码） ...")
    test_declaration_is_data(fails)
    print("2. 向量 -> (参数集, 铺层) 混合映射 ...")
    test_mapping(fails)
    print("3. 离散变量守住整数性 ...")
    test_discrete_integrality(fails)
    print("4. 包络守卫（明确报警，不静默穿越） ...")
    test_envelope_guard(fails)
    print("5. 是否需要再生数模的判定 ...")
    test_regeneration_decision(fails)
    print("6. reuse_catpart_from：CATIA 调用次数为 0 + 血缘完整 ...")
    test_reuse_catpart(conn, fails)
    print("7. 复用的拒绝条件（几何变化 / 源不存在） ...")
    test_reuse_guards(conn, fails)
    print("8. 不设字段时零回归（签名不变） ...")
    test_no_regression(conn, fails)
    print("9. 与优化器声明的接缝（票23/25 下游契约） ...")
    test_optimizer_interop(fails)
    print("10. Python 3.8 兼容 + 零第三方依赖 ...")
    test_py38(fails)

    conn.close()

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M22 (票22) SELF-TEST: ALL PASSED "
          "(数据驱动声明 / 混合映射 / 离散整数 / 包络报警 / 再生判定 / "
          "复用数模 CATIA=0 / 零回归 / py3.8)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
