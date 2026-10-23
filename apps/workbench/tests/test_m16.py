# -*- coding: utf-8 -*-
"""
test_m16.py -- 票16 验收：型号配置（全机层级与参数分解）

验收点（对应 issue #16）：
  1. 可创建型号配置并记录其总体参数
  2. 给定全机总体参数，能自动分解出各部件参数集，且各参数集满足对应模板的参数契约
     （wingbox_v1 / tailbox_v1 / fuselage_section_v1 三个模板都真调 validate 断言通过）
  3. 各部件 Study 可关联到同一型号配置，父子层级可查询
  4. 分解规则可配置、可测试（给定总体参数断言分解结果；换规则断言结果随之改变）
  5. 数据库演进**不破坏 P1 既有数据**（只有 P1 表的旧库打开后既有 Study/产物仍可查，新表可用）
  6. 层级关系可被血缘查询使用（按型号配置汇总各部件 Study 及产物数）
  7. Python 3.8 兼容

策略：只断言**外部可观察行为**（库里的事实、分解出的参数集、层级查询结果、血缘汇总），
不依赖内部函数调用顺序。分解出的参数集不是"看起来对"，而是真的交给
template_registry.validate 与 run_study 跑一遍——契约不满足就不可能通过。

运行 (在 apps/workbench 下): python tests/test_m16.py
"""

import os
import sys
import json
import shutil
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)
CORE = os.path.join(WORKBENCH, "core")
sys.path.insert(0, CORE)

import db
import study
import template_registry
import compat_py38
import model_config as mc
from run_study import run_study, RunOptions
from catia_driver import FakeCatiaDriver


# 全机总体参数：翼展 span / 机身长 fuselage_length / 尾容量 tail_volume /
# 机身当量直径 fuselage_diameter / 机翼后掠角 sweep_deg（票16 原文点名的输入）
OVERALL = {
    "span": 34.0,
    "fuselage_length": 30.0,
    "tail_volume": 0.9,
    "fuselage_diameter": 3.2,
    "sweep_deg": 25.0,
}

# 三个部件族的模板（都已在 P2 交付；这里只用，不修改）
EXPECTED_TEMPLATES = {
    "wing": "wingbox_v1",
    "tail": "tailbox_v1",
    "fuselage": "fuselage_section_v1",
}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _fresh(name):
    d = os.path.join(WORKBENCH, ".tmp_test", "m16", name)
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d)
    return d


def _near(a, b, tol=1e-6):
    return abs(float(a) - float(b)) <= tol


def _expect_error(fails, fn, must_contain=(), label=""):
    """
    断言 fn() 抛 ModelConfigError；返回异常供进一步断言消息内容。
    must_contain: 消息里必须出现的片段（用于断言"说清是哪个部件哪个参数"）。
    """
    try:
        fn()
    except mc.ModelConfigError as exc:
        text = str(exc)
        if not text:
            fails.append("%s: 错误没有给出可读原因" % label)
        for frag in must_contain:
            if frag not in text:
                fails.append("%s: 错误消息里缺少 %r（实际：%s）" % (label, frag, text))
        return exc
    except Exception as exc:
        fails.append("%s: 期望 ModelConfigError，实际抛了 %s: %s"
                     % (label, type(exc).__name__, exc))
        return None
    fails.append("%s: 期望抛 ModelConfigError，实际没有抛" % label)
    return None


def _study_count(conn, model_id):
    """型号配置下已关联的 Study 数（含历史版本）。"""
    row = conn.execute("SELECT COUNT(*) c FROM studies WHERE model_id=?",
                       (model_id,)).fetchone()
    return int(row["c"])


# ---------------------------------------------------------------------------
# 1) 创建型号配置并记录总体参数
# ---------------------------------------------------------------------------

def test_create_model_config(fails, conn):
    """型号配置落库：总体参数、规则快照、部件参数集都可查回。"""
    mid = mc.create_model_config("某型运输机-方案A", OVERALL, note="票16 自测",
                                 conn=conn)
    if not mid:
        fails.append("create_model_config 未返回 model_id")
        return None

    cfg = mc.get_model_config(conn, mid)
    if cfg["name"] != "某型运输机-方案A":
        fails.append("型号配置名不对：%r" % cfg["name"])
    # 1) 总体参数被完整记录（逐字段等值）
    for key, val in OVERALL.items():
        if key not in cfg["overall"]:
            fails.append("型号配置未记录总体参数 %r" % key)
        elif not _near(cfg["overall"][key], val):
            fails.append("总体参数 %r 记录值不对：%r != %r"
                         % (key, cfg["overall"][key], val))
    if cfg["note"] != "票16 自测":
        fails.append("备注未记录：%r" % cfg["note"])

    # 2) 分解规则快照落库（可追溯 / 可重放）
    rule = cfg["rule"]
    if not isinstance(rule, dict) or not rule.get("parts"):
        fails.append("rule_json 未落库或结构不对：%r" % (rule,))
    else:
        if set(rule["parts"]) != set(EXPECTED_TEMPLATES):
            fails.append("规则快照的部件不对：%r" % sorted(rule["parts"]))
        if "version" not in rule:
            fails.append("规则快照缺少 version（无法判断口径版本）")

    # 3) 原始 rule_json 是 JSON 文本、可独立解析（不依赖 Python 对象）
    row = conn.execute("SELECT overall_json, rule_json FROM model_configs WHERE id=?",
                       (mid,)).fetchone()
    if json.loads(row["overall_json"])["span"] != OVERALL["span"]:
        fails.append("overall_json 落库内容不对")
    if not isinstance(json.loads(row["rule_json"]), dict):
        fails.append("rule_json 落库内容不是 JSON 对象")

    # 4) 部件行落库：三个部件各一行，带模板与参数集
    parts = mc.list_parts(conn, mid)
    if [p["part_key"] for p in parts] != list(EXPECTED_TEMPLATES):
        fails.append("部件行顺序/键不对：%r" % [p["part_key"] for p in parts])
    for p in parts:
        if p["template_id"] != EXPECTED_TEMPLATES.get(p["part_key"]):
            fails.append("部件 %r 的模板不对：%r" % (p["part_key"], p["template_id"]))
        if not p["params"]:
            fails.append("部件 %r 没有落参数集" % p["part_key"])
        if p["study_id"] is not None:
            fails.append("新建型号配置的部件不应预挂 Study")

    # 5) 列表可查
    listed = mc.list_model_configs(conn)
    if not any(m["model_id"] == mid for m in listed):
        fails.append("list_model_configs 未返回新建的型号配置")
    hit = [m for m in listed if m["model_id"] == mid][0]
    if hit["n_parts"] != 3:
        fails.append("型号配置的部件数统计不对：%r" % hit["n_parts"])

    # 6) 重复 id 被拒（不静默覆盖既有型号）
    _expect_error(fails,
                  lambda: mc.create_model_config("重复", OVERALL, model_id=mid, conn=conn),
                  must_contain=("已存在",), label="重复 model_id")

    # 7) 不存在的型号配置被明确拒绝
    _expect_error(fails, lambda: mc.get_model_config(conn, "MODEL-NOT-EXIST"),
                  must_contain=("不存在",), label="查不存在的型号配置")
    return mid


# ---------------------------------------------------------------------------
# 2) 分解 + 三模板参数契约
# ---------------------------------------------------------------------------

def test_decompose_contract(fails, conn, model_id):
    """给定总体参数自动分解；三个部件参数集逐项通过模板参数契约。"""
    parts = mc.decompose_parts(OVERALL)
    by_key = dict((p["part_key"], p) for p in parts)
    if set(by_key) != set(EXPECTED_TEMPLATES):
        fails.append("分解出的部件不对：%r" % sorted(by_key))
        return

    # 1) 三个部件都用对模板，且模板文件真的存在可加载（不是编出来的 id）
    for key, tid in EXPECTED_TEMPLATES.items():
        if by_key[key]["template_id"] != tid:
            fails.append("部件 %r 应使用模板 %r，实际 %r"
                         % (key, tid, by_key[key]["template_id"]))
        try:
            template_registry.load(tid)
        except (IOError, OSError) as exc:
            fails.append("模板 %r 无法加载：%s" % (tid, exc))

    # 2) **逐个部件真调 validate**（这是票16 的硬验收项）
    for key, tid in EXPECTED_TEMPLATES.items():
        tpl = template_registry.load(tid)
        params = by_key[key]["params"]
        try:
            normalized = tpl.validate(params)
        except template_registry.ParamError as exc:
            fails.append("部件 %r 的参数集不满足模板 %s 契约：%s"
                         % (key, tid, exc))
            continue
        # 分解结果不含未知参数（否则 validate 已抛错，这里再兜一层）
        unknown = [k for k in params if k not in tpl.names()]
        if unknown:
            fails.append("部件 %r 的参数集含模板 %s 不认识的参数：%r"
                         % (key, tid, unknown))
        # 缺省补齐后与分解结果一致（说明分解没漏必填参数）
        if set(normalized) != set(tpl.names()):
            fails.append("部件 %r 规范化后参数不全：缺 %r"
                         % (key, sorted(set(tpl.names()) - set(normalized))))

    # 3) 分解结果落库的与纯算法算出的一致（同输入同口径）
    stored = dict((p["part_key"], p["params"]) for p in mc.list_parts(conn, model_id))
    for key in EXPECTED_TEMPLATES:
        if stored.get(key) != by_key[key]["params"]:
            fails.append("部件 %r 的落库参数集与分解结果不一致" % key)

    # 4) 分解口径可断言（不是"随便给了个数"）：
    #    机翼 span = 半展长 = 翼展/2
    wing = by_key["wing"]["params"]
    if not _near(wing["span"], OVERALL["span"] / 2.0):
        fails.append("机翼 span 应取半展长 %.3f，实际 %r"
                     % (OVERALL["span"] / 2.0, wing["span"]))
    #    c_tip < c_root（模板包络的硬约束，分解必须自洽）
    if not wing["c_tip"] < wing["c_root"]:
        fails.append("机翼弦长锥度不自洽：c_tip=%r >= c_root=%r"
                     % (wing["c_tip"], wing["c_root"]))
    #    尾翼由尾容量反推面积：V_h = S_h · l_t / (S_w · c_w)，故 S_h 必须与之一致
    tail = by_key["tail"]["params"]
    wing_mean_chord = OVERALL["span"] / 8.0
    wing_area = OVERALL["span"] * wing_mean_chord
    tail_arm = 0.55 * OVERALL["fuselage_length"]
    tail_area = OVERALL["tail_volume"] * wing_area * wing_mean_chord / tail_arm
    area_from_params = (tail["span"] * 2.0) * (tail["c_root"] + tail["c_tip"]) / 2.0
    if not _near(area_from_params, tail_area, tol=1e-6):
        fails.append("尾翼面积未按尾容量反推：参数推出 %.6f，规则要求 %.6f"
                     % (area_from_params, tail_area))
    #    机身筒段长度 = 机身长/2，半径 = 直径/2
    fus = by_key["fuselage"]["params"]
    if not _near(fus["length"], OVERALL["fuselage_length"] / 2.0):
        fails.append("机身筒段长度不对：%r" % fus["length"])
    if not _near(fus["radius"], OVERALL["fuselage_diameter"] / 2.0):
        fails.append("机身筒段半径不对：%r" % fus["radius"])

    # 5) decompose() 的扁平结果与 decompose_parts 一致
    flat = mc.decompose(OVERALL)
    if set(flat) != set(by_key):
        fails.append("decompose 与 decompose_parts 的部件集合不一致")
    for key in flat:
        if flat[key] != by_key[key]["params"]:
            fails.append("decompose 与 decompose_parts 对部件 %r 的参数不一致" % key)

    # 6) 可解释性：explain 给出中间量（追溯"为什么是这个参数"）
    exp = mc.explain(OVERALL)
    for name in ("wing_semispan", "tail_area", "fuselage_radius"):
        if name not in exp["vars"]:
            fails.append("explain 缺少中间量 %r（分解过程不可追溯）" % name)

    # 7) 规则不是对某一组数值硬编码：换一组常规运输机总体参数同样分解成功
    other = {"span": 20.0, "fuselage_length": 20.0, "tail_volume": 0.9,
             "fuselage_diameter": 2.4, "sweep_deg": 20.0}
    try:
        d_other = mc.decompose(other)
    except mc.ModelConfigError as exc:
        fails.append("换一组总体参数后分解失败（规则应通用而非硬编码）：%s" % exc)
    else:
        if _near(d_other["wing"]["span"], by_key["wing"]["params"]["span"]):
            fails.append("不同翼展的型号分解出了相同机翼展长（疑似硬编码）")
        for key, tid in EXPECTED_TEMPLATES.items():
            try:
                template_registry.load(tid).validate(d_other[key])
            except template_registry.ParamError as exc:
                fails.append("第二组总体参数下部件 %r 不满足契约：%s" % (key, exc))

    # 8) 超出模板参数化包络时**明确拒绝**（不静默给越界值、不偷偷夹紧）
    huge = dict(OVERALL)
    huge["span"] = 60.0
    _expect_error(fails, lambda: mc.decompose(huge),
                  must_contain=("wing",), label="超出包络的翼展")


# ---------------------------------------------------------------------------
# 3) 分解规则可配置 / 可测试
# ---------------------------------------------------------------------------

def test_rule_configurable(fails, conn):
    """换规则 -> 分解结果随之改变；坏公式/坏参数集给出明确错误。"""
    base = mc.decompose(OVERALL)

    # 1) 定制规则：只动"锥度 / 肋距 / 框距"这几个口径，看分解结果跟着变
    rule = mc.default_rule()
    rule["name"] = "紧凑肋距方案规则"
    rule["vars"]["wing_taper"] = 0.4          # 锥度变小 -> 根弦变大、尖弦变小
    rule["vars"]["wing_rib_pitch"] = 1.2      # 肋距变大 -> 肋数变少
    rule["vars"]["fuselage_frame_pitch"] = 2.5  # 框距变大 -> 框数变少
    rule["vars"]["tail_rib_pitch"] = 0.6      # 尾翼肋距变小 -> 尾翼肋数变多
    custom = mc.decompose(OVERALL, rule=rule)

    # 锥度变小 -> 根弦长变大（展弦比未动，故平均弦长不变）
    if not custom["wing"]["c_root"] > base["wing"]["c_root"]:
        fails.append("换锥度规则后 c_root 应变大：%r -> %r"
                     % (base["wing"]["c_root"], custom["wing"]["c_root"]))
    if not custom["wing"]["c_tip"] < base["wing"]["c_tip"]:
        fails.append("换锥度规则后 c_tip 应变小：%r -> %r"
                     % (base["wing"]["c_tip"], custom["wing"]["c_tip"]))
    # 肋距变大 -> 肋数变少；框距变大 -> 框数变少
    if not custom["wing"]["n_ribs"] < base["wing"]["n_ribs"]:
        fails.append("换肋距规则后 n_ribs 应变少：%r -> %r"
                     % (base["wing"]["n_ribs"], custom["wing"]["n_ribs"]))
    if not custom["fuselage"]["n_frames"] < base["fuselage"]["n_frames"]:
        fails.append("换框距规则后 n_frames 应变少：%r -> %r"
                     % (base["fuselage"]["n_frames"], custom["fuselage"]["n_frames"]))
    # 尾翼肋距变小 -> 尾翼肋数变多（尾翼展长由尾容量定，不受肋距影响）
    if not custom["tail"]["n_ribs"] > base["tail"]["n_ribs"]:
        fails.append("换尾翼肋距规则后 n_ribs 应变多：%r -> %r"
                     % (base["tail"]["n_ribs"], custom["tail"]["n_ribs"]))
    if not _near(custom["tail"]["span"], base["tail"]["span"]):
        fails.append("未改尾容量的规则不应改变尾翼展长：%r -> %r"
                     % (base["tail"]["span"], custom["tail"]["span"]))
    # 换规则后参数集仍必须满足契约
    for key, tid in EXPECTED_TEMPLATES.items():
        try:
            template_registry.load(tid).validate(custom[key])
        except template_registry.ParamError as exc:
            fails.append("定制规则下部件 %r 参数集不满足契约：%s" % (key, exc))

    # 2) 换规则不影响模块内默认规则（规则是纯数据、互不污染）
    again = mc.decompose(OVERALL)
    if again != base:
        fails.append("传定制规则后默认规则被污染了（分解结果变了）")

    # 3) 规则可只覆盖部分部件：去掉机身，只剩两个部件
    rule2 = mc.default_rule()
    del rule2["parts"]["fuselage"]
    two = mc.decompose(OVERALL, rule=rule2)
    if set(two) != set(("wing", "tail")):
        fails.append("规则删掉机身部件后仍出现机身：%r" % sorted(two))

    # 4) 规则快照是**拷贝**：创建后外部改 dict 不影响已落库的型号配置
    rule3 = mc.default_rule()
    rule3["vars"]["wing_rib_pitch"] = 1.5
    mid = mc.create_model_config("规则快照独立性", OVERALL, rule=rule3, conn=conn)
    rule3["vars"]["wing_rib_pitch"] = 99.0          # 事后篡改
    snap = mc.get_model_config(conn, mid)["rule"]
    if snap["vars"]["wing_rib_pitch"] != 1.5:
        fails.append("rule_json 未做快照拷贝（事后改 dict 影响了已落库规则）：%r"
                     % snap["vars"]["wing_rib_pitch"])

    # 5) 规则引用不存在的总体参数 -> 明确报出缺哪个名字
    bad = mc.default_rule()
    bad["parts"]["wing"]["params"]["span"] = "wing_span_typo / 2.0"
    _expect_error(fails, lambda: mc.decompose(OVERALL, rule=bad),
                  must_contain=("wing_span_typo",), label="引用未提供的名字")
    _expect_error(fails, lambda: mc.create_model_config("坏规则", OVERALL, rule=bad,
                                                        conn=conn),
                  must_contain=("wing_span_typo",), label="坏规则建型号配置")

    # 6) 分解结果不满足参数契约 -> 说清哪个部件、哪个参数
    bad2 = mc.default_rule()
    bad2["parts"]["wing"]["params"]["span"] = "span * 4.0"   # 136m > 模板上限 40m
    _expect_error(fails, lambda: mc.decompose(OVERALL, rule=bad2),
                  must_contain=("wing", "span"), label="机翼 span 超上限")
    bad3 = mc.default_rule()
    bad3["parts"]["fuselage"]["params"]["n_frames"] = 1       # 模板下限 2
    _expect_error(fails, lambda: mc.decompose(OVERALL, rule=bad3),
                  must_contain=("fuselage", "n_frames"), label="机身框数低于下限")
    bad4 = mc.default_rule()
    bad4["parts"]["wing"]["params"]["n_ribs"] = "wing_n_ribs + 0.5"  # int 契约
    _expect_error(fails, lambda: mc.decompose(OVERALL, rule=bad4),
                  must_contain=("wing", "n_ribs"), label="肋数不是整数")

    # 7) 规则形状非法 -> 可读错误（不静默吞掉）
    _expect_error(fails, lambda: mc.decompose(OVERALL, rule={"vars": {}}),
                  must_contain=("parts",), label="规则缺 parts")
    _expect_error(fails,
                  lambda: mc.decompose(OVERALL,
                                       rule={"parts": {"wing": {"params": {}}}}),
                  must_contain=("template_id",), label="部件缺 template_id")

    # 8) 总体参数非法/缺失 -> 可读错误
    _expect_error(fails, lambda: mc.decompose({}),
                  must_contain=("不能为空",), label="总体参数为空")
    _expect_error(fails, lambda: mc.decompose({"span": "34"}),
                  must_contain=("span",), label="总体参数非数值")
    _expect_error(fails,
                  lambda: mc.decompose({"span": 34.0, "fuselage_length": 30.0}),
                  must_contain=("tail_volume",), label="总体参数缺失")

    # 9) 坏规则不落库（先算后写：不留半成品型号配置）
    n_before = len(mc.list_model_configs(conn))
    try:
        mc.create_model_config("不该落地", OVERALL, rule=bad2, conn=conn)
    except mc.ModelConfigError:
        pass
    if len(mc.list_model_configs(conn)) != n_before:
        fails.append("分解失败的型号配置仍被落库了（应无半成品）")


# ---------------------------------------------------------------------------
# 4) 部件 Study 关联 + 父子层级查询
# ---------------------------------------------------------------------------

def test_attach_and_tree(fails, conn):
    """三个部件 Study 关联到同一型号配置；层级可查（含嵌套子部件）。"""
    mid = mc.create_model_config("层级测试", OVERALL, conn=conn)
    study.create_study("S-M16-W", "机翼 Study", conn=conn)
    study.create_study("S-M16-T", "尾翼 Study", conn=conn)
    study.create_study("S-M16-F", "机身 Study", conn=conn)

    # 关联前：层级里没有 Study
    tree0 = mc.get_model_tree(conn, mid)
    if tree0["study_ids"]:
        fails.append("关联前层级已有 Study：%r" % tree0["study_ids"])

    # 1) 三个部件依次关联 -> 同一型号配置
    for key, sid in (("wing", "S-M16-W"), ("tail", "S-M16-T"),
                     ("fuselage", "S-M16-F")):
        mc.attach_study(conn, mid, key, sid)

    # 两处事实都写了：model_config_parts.study_id 与 studies.model_id/part_key
    for key, sid in (("wing", "S-M16-W"), ("tail", "S-M16-T"),
                     ("fuselage", "S-M16-F")):
        part = mc.get_part(conn, mid, key)
        if part["study_id"] != sid:
            fails.append("部件 %r 未关联到 Study %r（实际 %r）"
                         % (key, sid, part["study_id"]))
        row = conn.execute("SELECT model_id, part_key FROM studies WHERE id=?",
                           (sid,)).fetchone()
        if row["model_id"] != mid or row["part_key"] != key:
            fails.append("Study %r 上的归属列不对：model_id=%r part_key=%r"
                         % (sid, row["model_id"], row["part_key"]))

    # 2) 层级树：全机 -> 部件 -> Study
    tree = mc.get_model_tree(conn, mid)
    if tree["model_id"] != mid:
        fails.append("层级树 model_id 不对")
    if len(tree["parts"]) != 3:
        fails.append("层级树顶层部件数应为 3，实际 %d" % len(tree["parts"]))
    if _study_count(conn, mid) != 3:
        fails.append("型号配置下应关联 3 个 Study，实际 %d" % _study_count(conn, mid))
    if sorted(tree["study_ids"]) != ["S-M16-F", "S-M16-T", "S-M16-W"]:
        fails.append("层级树的 study_ids 不对：%r" % tree["study_ids"])
    for node in tree["parts"]:
        leaf = node.get("study")
        if not leaf or not leaf.get("study_id"):
            fails.append("部件 %r 的层级里没有 Study 叶子" % node["part_key"])
            continue
        if leaf["study_id"] != node["study_id"]:
            fails.append("部件 %r 的 Study 叶子不对" % node["part_key"])
        # 部件节点带当前参数集（UI 直接可显示，不必再查表）
        if not node["params"]:
            fails.append("部件 %r 的层级节点没有参数集" % node["part_key"])
        if leaf["n_artifacts"] != 0:
            fails.append("新 Study 的产物数应为 0，实际 %r" % leaf["n_artifacts"])

    # 3) 按部件键查节点 / 查上级
    node = mc.get_part_node(conn, mid, "wing")
    if node is None or node["study_id"] != "S-M16-W":
        fails.append("get_part_node 未取到机翼节点")
    for node in tree["parts"]:
        if node["parent_part"] is not None:
            fails.append("直挂全机的部件 parent_part 应为 None：%r" % node["part_key"])

    # 4) 改参重跑后重挂新 Study：部件指向新 Study，旧 Study 仍是型号历史（血缘不断）
    study.create_study("S-M16-W2", "机翼 Study v2", conn=conn)
    mc.attach_study(conn, mid, "wing", "S-M16-W2")
    tree2 = mc.get_model_tree(conn, mid)
    wing = mc.get_part_node(conn, mid, "wing")
    if wing["study_id"] != "S-M16-W2":
        fails.append("重挂后部件未指向新 Study：%r" % wing["study_id"])
    hist_ids = [s["study_id"] for s in wing["studies"]]
    if "S-M16-W" not in hist_ids or "S-M16-W2" not in hist_ids:
        fails.append("重挂后部件的 Study 历史不全：%r" % hist_ids)
    if "S-M16-W2" not in tree2["study_ids"]:
        fails.append("重挂后层级树未反映新 Study")

    # 5) 解除关联
    mc.detach_study(conn, mid, "tail")
    if mc.get_part(conn, mid, "tail")["study_id"] is not None:
        fails.append("detach_study 后部件仍关联 Study")
    row = conn.execute("SELECT model_id FROM studies WHERE id='S-M16-T'").fetchone()
    if row["model_id"] is not None:
        fails.append("detach_study 后 Study 仍挂着型号配置")

    # 6) 嵌套层级（parent_part）：机身 -> 后机身筒段
    rule = mc.default_rule()
    aft = json.loads(json.dumps(rule["parts"]["fuselage"]))
    aft["parent_part"] = "fuselage"
    aft["note"] = "后机身筒段（嵌在前机身之下）"
    aft["params"] = dict(aft["params"])
    aft["params"]["length"] = "fuselage_half_length * 0.8"
    rule["parts"]["fuselage_aft"] = aft
    mid2 = mc.create_model_config("嵌套层级", OVERALL, rule=rule, conn=conn)
    tree3 = mc.get_model_tree(conn, mid2)
    if len(tree3["parts"]) != 3:
        fails.append("嵌套规则下顶层部件数应仍为 3，实际 %d" % len(tree3["parts"]))
    if len(tree3["all_parts"]) != 4:
        fails.append("嵌套规则下全部部件数应为 4，实际 %d" % len(tree3["all_parts"]))
    kids = mc.get_part_children(conn, mid2, "fuselage")
    if [k["part_key"] for k in kids] != ["fuselage_aft"]:
        fails.append("get_part_children 未取到后机身子部件：%r"
                     % [k["part_key"] for k in kids])
    flat_keys = [n["part_key"] for n in mc.iter_parts(tree3)]
    if "fuselage_aft" not in flat_keys:
        fails.append("iter_parts 未遍历到嵌套子部件：%r" % flat_keys)
    # 嵌套子部件的参数集同样满足模板契约
    try:
        template_registry.load(EXPECTED_TEMPLATES["fuselage"]).validate(
            mc.get_part(conn, mid2, "fuselage_aft")["params"])
    except template_registry.ParamError as exc:
        fails.append("嵌套子部件参数集不满足契约：%s" % exc)

    # 7) 关联的错误情况都可读
    _expect_error(fails, lambda: mc.attach_study(conn, "MODEL-X", "wing", "S-M16-W"),
                  must_contain=("不存在",), label="关联到不存在的型号配置")
    _expect_error(fails, lambda: mc.attach_study(conn, mid, "bogus", "S-M16-W"),
                  must_contain=("bogus",), label="关联不存在的部件")
    _expect_error(fails, lambda: mc.attach_study(conn, mid, "wing", "S-NOT-EXIST"),
                  must_contain=("不存在",), label="关联不存在的 Study")
    # 跨型号抢占同一个 Study 被拒
    _expect_error(fails, lambda: mc.attach_study(conn, mid2, "wing", "S-M16-W2"),
                  must_contain=("S-M16-W2",), label="跨型号抢占 Study")
    return mid


# ---------------------------------------------------------------------------
# 5) 层级关系可被血缘查询使用
# ---------------------------------------------------------------------------

def test_lineage_usage(fails, tmp):
    """分解出的参数集真跑通各部件 Study；型号级血缘查询可跨部件追溯。"""
    work = os.path.join(tmp, "lineage")
    if not os.path.isdir(work):
        os.makedirs(work)
    conn = db.connect(os.path.join(work, "lineage.db"))
    try:
        mid = mc.create_model_config("血缘测试", OVERALL, conn=conn)
        params_by_part = dict((p["part_key"], p["params"])
                              for p in mc.list_parts(conn, mid))

        # 1) 每个部件：建 Study -> 用**分解出的参数集**跑 run_study -> 关联到型号配置
        study_ids = {}
        for key, tid in EXPECTED_TEMPLATES.items():
            sid = mc.create_part_study(conn, mid, key)
            study_ids[key] = sid
            # 走主切口 RunOptions，并带上型号配置引用（票10 预留字段，票17 会消费）
            opts = RunOptions(with_topology=True, model_config_id=mid)
            out = run_study(sid, params_by_part[key], tid,
                            FakeCatiaDriver(os.path.join(work, "catparts", key)),
                            conn, options=opts)
            if out["template"] != tid:
                fails.append("部件 %r 跑出来的模板不对：%r" % (key, out["template"]))
            if out["artifacts"].get("parameter_set") is None:
                fails.append("部件 %r 未产生 parameter_set 产物" % key)
            if out["artifacts"].get("topology") is None:
                fails.append("部件 %r 未产生 topology 产物" % key)
        if len(set(study_ids.values())) != 3:
            fails.append("三个部件应各有一个独立 Study：%r" % study_ids)

        # 型号配置引用经 RunOptions 传递不丢（票16 打通 RunOptions.model_config_id）
        if RunOptions(model_config_id=mid).get("model_config_id") != mid:
            fails.append("RunOptions 未保留 model_config_id")

        # 2) 型号级血缘汇总：各部件 Study 及产物数
        lin = mc.model_lineage(conn, mid)
        if lin["model_id"] != mid:
            fails.append("model_lineage 的 model_id 不对")
        if lin["totals"]["n_parts"] != 3:
            fails.append("型号血缘汇总的部件数不对：%r" % lin["totals"]["n_parts"])
        if lin["totals"]["n_parts_with_study"] != 3:
            fails.append("型号血缘汇总的已关联部件数不对：%r"
                         % lin["totals"]["n_parts_with_study"])
        if lin["totals"]["n_studies"] != 3:
            fails.append("型号血缘汇总的 Study 数不对：%r"
                         % lin["totals"]["n_studies"])
        if lin["totals"]["n_artifacts"] < 9:
            fails.append("型号血缘汇总的产物数偏少：%r"
                         % lin["totals"]["n_artifacts"])
        for rec in lin["parts"]:
            key = rec["part_key"]
            if rec["study_id"] != study_ids[key]:
                fails.append("型号血缘汇总里部件 %r 的 Study 不对" % key)
            if rec["n_artifacts"] < 3:
                fails.append("部件 %r 的产物数偏少：%r" % (key, rec["n_artifacts"]))
            for kind in ("parameter_set", "catpart", "topology"):
                if kind not in rec["by_kind"]:
                    fails.append("部件 %r 的血缘里缺少 %r 产物（实际 %r）"
                                 % (key, kind, sorted(rec["by_kind"])))
        # 各部件产物数之和 == 汇总（汇总不是拍脑袋的数）
        if sum(p["n_artifacts"] for p in lin["parts"]) != lin["totals"]["n_artifacts"]:
            fails.append("型号血缘汇总的产物总数与各部件之和不一致")

        # 3) 型号下全部产物行（带 part_key）——跨部件追溯的直接入口
        rows = mc.model_artifact_rows(conn, mid)
        if not rows:
            fails.append("model_artifact_rows 返回空")
        keys = set(r["part_key"] for r in rows)
        if keys != set(EXPECTED_TEMPLATES):
            fails.append("型号产物行未覆盖三个部件：%r" % sorted(keys))
        kinds = set(r["kind"] for r in rows)
        if not {"parameter_set", "catpart", "topology"} <= kinds:
            fails.append("型号产物行的产物类型不全：%r" % sorted(kinds))

        # 4) 反查：某 Study 属于哪个型号的哪个部件
        info = mc.resolve_model_of_study(conn, study_ids["tail"])
        if not info or info["model_id"] != mid or info["part_key"] != "tail":
            fails.append("resolve_model_of_study 结果不对：%r" % (info,))
        # 不属于任何型号的 P1 扁平 Study -> None，且不出现在型号血缘里
        study.create_study("S-M16-FLAT", "P1 扁平 Study", conn=conn)
        study.add_artifact("S-M16-FLAT", "parameter_set", content_uri="params:flat",
                           conn=conn)
        if mc.resolve_model_of_study(conn, "S-M16-FLAT") is not None:
            fails.append("扁平 Study 不应归属任何型号配置")
        if "S-M16-FLAT" in [s["study_id"] for s in mc.studies_of_model(conn, mid)]:
            fails.append("扁平 Study 不应出现在型号血缘里")

        # 5) 层级树里的 Study 叶子带产物数（血缘汇总量与树一致）
        tree = mc.get_model_tree(conn, mid)
        for node in tree["parts"]:
            lin_rec = [p for p in lin["parts"] if p["part_key"] == node["part_key"]][0]
            if node["study"]["n_artifacts"] != lin_rec["n_artifacts"]:
                fails.append("部件 %r 的层级树产物数与血缘汇总不一致" % node["part_key"])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6) 数据库演进不破坏 P1 既有数据
# ---------------------------------------------------------------------------

# 只含 P1 表的旧库 DDL（照抄 P1 的 SCHEMA：studies/artifacts/edges/decisions，
# 刻意**不含** P2 的 model_configs / model_config_parts / report_approvals，
# 也刻意不给 studies 加 model_id/part_key 列、不给 artifacts 加 approval_status 列）
P1_ONLY_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE studies (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    note       TEXT
);

CREATE TABLE artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id    TEXT NOT NULL REFERENCES studies(id),
    kind        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    checksum    TEXT NOT NULL,
    content_uri TEXT,
    params_json TEXT,
    status      TEXT NOT NULL DEFAULT 'ok',
    created_at  TEXT NOT NULL,
    UNIQUE(study_id, kind, version)
);

CREATE TABLE edges (
    parent_id INTEGER NOT NULL REFERENCES artifacts(id),
    child_id  INTEGER NOT NULL REFERENCES artifacts(id),
    PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id   TEXT NOT NULL REFERENCES studies(id),
    gate       TEXT NOT NULL,
    actor      TEXT NOT NULL,
    decision   TEXT NOT NULL,
    rationale  TEXT,
    created_at TEXT NOT NULL
);
"""


def test_p1_old_db(fails, tmp):
    """P1 老库打开后：既有 Study/产物/血缘/决策仍可查，新增型号配置能力可用。"""
    old = os.path.join(tmp, "p1_old.db")
    if os.path.exists(old):
        os.remove(old)

    # 1) 手工造一个 P1 老库并写入历史数据（模拟升级前的现场数据）
    raw = sqlite3.connect(old)
    try:
        raw.executescript(P1_ONLY_SCHEMA)
        raw.execute("INSERT INTO studies(id, name, created_at, note) "
                    "VALUES ('S-P1-WING','P1 机翼盒段 Study','2026-01-01T00:00:00','历史')")
        raw.execute("INSERT INTO artifacts(study_id, kind, version, checksum, "
                    "content_uri, params_json, status, created_at) "
                    "VALUES ('S-P1-WING','parameter_set',1,'sha-ps','params:p1','{}','ok',"
                    "'2026-01-01T00:00:01')")
        raw.execute("INSERT INTO artifacts(study_id, kind, version, checksum, "
                    "content_uri, params_json, status, created_at) "
                    "VALUES ('S-P1-WING','report',1,'sha-rp','report:p1','{}','ok',"
                    "'2026-01-01T00:00:09')")
        raw.execute("INSERT INTO edges(parent_id, child_id) VALUES (1, 2)")
        raw.execute("INSERT INTO decisions(study_id, gate, actor, decision, "
                    "rationale, created_at) VALUES ('S-P1-WING','rule_confirm',"
                    "'张三','通过','历史决策','2026-01-01T00:00:10')")
        raw.commit()
    finally:
        raw.close()

    # 2) 用 db.connect() 打开（= 现场升级动作），不能报错、不能丢数据
    conn = db.connect(old)
    try:
        # 新表建出来了
        tables = set(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        for t in ("studies", "artifacts", "edges", "decisions",
                  "model_configs", "model_config_parts"):
            if t not in tables:
                fails.append("P1 老库升级后缺少表 %r" % t)
        # 新列补上了
        for table, column in (("studies", "model_id"), ("studies", "part_key"),
                              ("artifacts", "approval_status")):
            cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
            if column not in cols:
                fails.append("P1 老库升级后 %s 缺少列 %r" % (table, column))

        # 既有数据仍可查（Study / 产物 / 血缘 / 决策）
        row = conn.execute("SELECT * FROM studies WHERE id='S-P1-WING'").fetchone()
        if row is None or row["name"] != "P1 机翼盒段 Study":
            fails.append("P1 既有 Study 升级后查不到或内容变了")
        if row is not None and row["model_id"] is not None:
            fails.append("P1 既有 Study 的 model_id 应为 NULL（不该被硬塞归属）")
        arts = conn.execute("SELECT kind, checksum FROM artifacts "
                            "WHERE study_id='S-P1-WING' ORDER BY id").fetchall()
        if [a["kind"] for a in arts] != ["parameter_set", "report"]:
            fails.append("P1 既有产物升级后不全：%r" % [a["kind"] for a in arts])
        edge = conn.execute("SELECT * FROM edges").fetchall()
        if len(edge) != 1:
            fails.append("P1 既有血缘边升级后丢失：%r" % len(edge))
        dec = conn.execute("SELECT * FROM decisions WHERE study_id='S-P1-WING'").fetchall()
        if len(dec) != 1 or dec[0]["actor"] != "张三":
            fails.append("P1 既有决策记录升级后丢失/变了")
        # 不可变守卫生效（P1 行为不变）
        try:
            conn.execute("UPDATE artifacts SET checksum='x' WHERE id=1")
            fails.append("P1 的 artifact 不可变守卫在升级后失效了")
        except sqlite3.IntegrityError:
            pass
        conn.commit()

        # 3) 新表可用：在同一个老库里建型号配置，并把**P1 既有 Study**关联进来
        mid = mc.create_model_config("老库上的新型号", OVERALL, conn=conn)
        mc.attach_study(conn, mid, "wing", "S-P1-WING")
        tree = mc.get_model_tree(conn, mid)
        wing = mc.get_part_node(conn, mid, "wing")
        if wing is None or wing["study_id"] != "S-P1-WING":
            fails.append("P1 既有 Study 无法关联到型号配置的部件")
        if wing["study"] is None or wing["study"]["n_artifacts"] != 2:
            fails.append("关联后层级树里 P1 既有 Study 的产物数不对")
        lin = mc.model_lineage(conn, mid)
        if lin["totals"]["n_artifacts"] != 2:
            fails.append("型号血缘汇总未纳入 P1 既有产物：%r"
                         % lin["totals"]["n_artifacts"])
        if "report" not in lin["totals"]["by_kind"]:
            fails.append("型号血缘汇总按类型统计里缺少 P1 的 report 产物")

        # 4) 再连一次（升级幂等）；历史数据与型号配置都还在
        conn.close()
        conn = db.connect(old)
        if conn.execute("SELECT COUNT(*) c FROM studies").fetchone()["c"] != 1:
            fails.append("二次打开后 Study 数变了")
        if conn.execute("SELECT COUNT(*) c FROM model_configs").fetchone()["c"] != 1:
            fails.append("二次打开后型号配置丢了")
        if mc.get_part_node(conn, mid, "wing")["study_id"] != "S-P1-WING":
            fails.append("二次打开后部件与 Study 的关联丢了")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7) Python 3.8 兼容
# ---------------------------------------------------------------------------

def test_py38(fails):
    """本票新增/改动的文件必须 Python 3.8 兼容。"""
    paths = [os.path.join(CORE, "model_config.py"),
             os.path.join(CORE, "db.py"),
             os.path.abspath(__file__)]
    violations = compat_py38.check_paths(paths)
    if violations:
        fails.append("Python 3.8 不兼容：\n" + compat_py38.format_violations(violations))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    fails = []
    tmp = _fresh("case")

    conn = db.connect(os.path.join(tmp, "model_config.db"))
    try:
        print("1. 创建型号配置并记录总体参数 ...")
        mid = test_create_model_config(fails, conn)
        if mid:
            print("2. 分解 + 三模板参数契约 ...")
            test_decompose_contract(fails, conn, mid)
        else:
            fails.append("型号配置创建失败，跳过分解断言")
        print("3. 分解规则可配置/可测试 ...")
        test_rule_configurable(fails, conn)
    finally:
        conn.close()

    print("4. 部件 Study 关联 + 父子层级查询 ...")
    conn2 = db.connect(os.path.join(tmp, "hierarchy.db"))
    try:
        test_attach_and_tree(fails, conn2)
    finally:
        conn2.close()

    print("5. 层级关系可被血缘查询使用 ...")
    test_lineage_usage(fails, tmp)
    print("6. 数据库演进不破坏 P1 既有数据 ...")
    test_p1_old_db(fails, tmp)
    print("7. Python 3.8 兼容 ...")
    test_py38(fails)

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M16 (票16) SELF-TEST: ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
