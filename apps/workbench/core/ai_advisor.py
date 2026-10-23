# -*- coding: utf-8 -*-
"""
ai_advisor.py -- L1 AI 辅助（解释 / 标注 / 建议）协议 + 确定性 Fake（票26）

## 为什么需要这个模块

ADR-0002 允许的 L1 辅助此前完全没有落地：报告里的"结论草稿"是写死的模板三分支
（`report._build_conclusion` 的 all_pass 判断），不是真的基于数据生成的解释；
危险部位图只画点，没有"为什么这里危险"的说明。本模块补上这一层。

## ADR-0002 的硬约束（本模块的全部设计出发点）

**强度判定（FI/MS 计算与合格判定）永不由 LLM 完成。**

因此本模块的每个返回对象都遵循同一条纪律：

  * `AdvisorNote` 只有 `draft_text` 是"生成的文字"，且恒带 `layer="L1"`、
    `draft=True` —— 它在报告里必须与确定性结论**视觉上分开**；
  * `facts` 字段只是**只读回显**（把确定性校核已经算好的数字抄过来便于对账），
    **绝不**作为任何判定的输入；
  * 本模块**不计算 FI/MS**、不引用许用值做任何比值判定 —— 那属于
    `strength_check`（L0）。这里只做：读已算好的数字 + 组织成人能读的说明。

## 三件套惯例（与 CatiaDriver / AbaqusRunner 一致）

  * `AiAdvisor`     —— 协议（三个方法）
  * `DeterministicAiAdvisor` —— fake：完全确定性、无网络、可回归。**CI 只用它。**
  * `RealAiAdvisor` —— 真机适配器位置，**本票不实现**（见类 docstring 说明原因）

## 降级路径（硬验收项）

适配器**缺失**（没传）或**不可用**（构造失败 / 调用抛异常）时，流程必须
"安静退化"为纯确定性输出：不抛异常、不中断报告生成，返回 `degraded=True`
且文本为确定性兜底内容。两个入口：

  * `FallbackAiAdvisor` —— 适配器缺失时的替身（degraded 恒为 True）
  * `SafeAiAdvisor`     —— 包住任意适配器，调用异常时降级（degraded 为 True）
  * `resolve_advisor(x)` —— 统一入口：None/鸭子类型不合规/已合规，分别给出上述对象

## 留痕（ADR-0002「可解释、可回退、留痕」）

每个 `AdvisorNote.trace` 记录：适配器标识、方法名、输入摘要（指纹）、是否降级。
另提供 `AiAdvisor.invocations` 调用流水，便于审计"这一版报告的解释是谁生成的"。

纯标准库。兼容 Python 3.8（Win7 目标机）。
"""

import hashlib
import json

try:
    from . import textfmt
except ImportError:  # 允许在 core 目录内直接运行
    import textfmt


# AI 层标识（报告里要显式标注，见 ADR-0002）
LAYER_L1 = "L1"
LAYER_L1_LABEL = "L1 辅助草稿（AI 生成，人可否决）"

# 适配器标识（留痕用）
ADVISOR_FAKE = "deterministic-fake/v1"
ADVISOR_FALLBACK = "fallback-deterministic/v1"
ADVISOR_SAFE_PREFIX = "safe:"
ADVISOR_REAL = "real-llm/unimplemented"

# 方法名（留痕与测试共用，避免各处硬编码字符串）
METHOD_EXPLAIN_CHECK = "explain_check"
METHOD_EXPLAIN_HOTSPOT = "explain_hotspot"
METHOD_SUGGEST_LAYUP = "suggest_layup"
METHODS = (METHOD_EXPLAIN_CHECK, METHOD_EXPLAIN_HOTSPOT, METHOD_SUGGEST_LAYUP)

# 降级原因的机器可读码
DEGRADE_MISSING = "advisor-missing"      # 调用方没提供适配器
DEGRADE_INVALID = "advisor-invalid"      # 提供的东西不满足协议
DEGRADE_ERROR = "advisor-error"          # 调用适配器时抛异常
DEGRADE_DISABLED = "advisor-disabled"    # 调用方显式关闭

# 兜底文本的前缀（报告里可据此识别"这不是 AI 生成的内容"）
FALLBACK_PREFIX = "（AI 解释不可用，以下为确定性兜底说明）"

# 每段 AI 文字自带的标注前缀（报告层面再加一道标识，防止文字脱离上下文后
# 被误当成确定性结论——ADR-0002 要求 AI 输出必须可识别）。
L1_TEXT_PREFIX = "【L1 辅助草稿｜AI 生成，人可否决】"


# ---------------------------------------------------------------------------
# 返回对象
# ---------------------------------------------------------------------------

class AdvisorNote(object):
    """
    一次 L1 辅助输出的**唯一载体**。

    字段刻意把"生成的文字"与"确定性事实"分开放，且带上层级/草稿/留痕标记，
    使得调用方（报告渲染、UI）在类型层面就无法把 AI 文字当成强度结论用：

      kind         : 方法名（explain_check / explain_hotspot / suggest_layup）
      draft_text   : AI 生成的说明文字（**永远**是草稿，须人确认）
      facts        : 确定性事实的只读回显（数字来自 strength_check，本模块不重算）
      advisor_id   : 哪个适配器生成的（留痕）
      degraded     : True = 适配器不可用，draft_text 是确定性兜底内容
      degrade_reason: 降级原因码（未降级时为 None）
      layer        : 恒为 "L1"
      draft        : 恒为 True
      trace        : {method, advisor_id, input_digest, degraded, degrade_reason}
    """

    def __init__(self, kind, draft_text, facts=None, advisor_id="",
                 degraded=False, degrade_reason=None, trace=None):
        self.kind = kind
        self.draft_text = draft_text or ""
        self.facts = dict(facts or {})
        self.advisor_id = advisor_id
        self.degraded = bool(degraded)
        self.degrade_reason = degrade_reason
        self.layer = LAYER_L1
        self.draft = True
        self.trace = dict(trace or {})

    # -- 便于 UI / 报告直接消费 -------------------------------------------

    @property
    def label(self):
        """给人看的来源标注（报告里必须出现，验收项 6）。"""
        if self.degraded:
            return "L1 辅助（AI 不可用，确定性兜底）"
        return LAYER_L1_LABEL

    @property
    def is_ai_generated(self):
        """真正由 AI 生成的文字才为 True；兜底文本为 False（不算 AI 生成）。"""
        return not self.degraded

    def to_dict(self):
        return {
            "kind": self.kind,
            "draft_text": self.draft_text,
            "facts": dict(self.facts),
            "advisor_id": self.advisor_id,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "layer": self.layer,
            "draft": self.draft,
            "label": self.label,
            "trace": dict(self.trace),
        }

    def __repr__(self):
        return ("AdvisorNote(%s, advisor=%s, degraded=%r)"
                % (self.kind, self.advisor_id, self.degraded))


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------

class AiAdvisor(object):
    """
    协议：真机与 fake 都实现这三个方法，**同签名**（与 CatiaDriver 惯例一致）。

    契约（所有实现都必须遵守）：
      * 只做解释/标注/建议，**不得**返回或改写任何 FI/MS/合格判定；
      * 返回值恒为 `AdvisorNote`，`layer` 恒为 L1、`draft` 恒为 True；
      * 不得因内部故障抛给调用方——要么自己降级，要么由 `SafeAiAdvisor` 兜住。
    """

    #: 适配器标识（写进留痕）；子类必须覆盖
    advisor_id = "abstract"

    #: 调用流水（留痕）：每成功/降级返回一条，元素为 trace dict
    invocations = None

    def explain_check(self, check, study_name=None):
        """解释整份校核结论（基于 check.summary 与危险部位）。返回 AdvisorNote。"""
        raise NotImplementedError

    def explain_hotspot(self, hotspot, context=None):
        """解释单个危险部位"为什么这里危险"（FI/MS、铺层、自然格归属）。"""
        raise NotImplementedError

    def suggest_layup(self, hotspot, context=None):
        """给出铺层改进建议草案（基于应力分量与失效准则）。"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 确定性 Fake（CI 只用它）
# ---------------------------------------------------------------------------

def _num(value, fmt="%.4g"):
    """确定性数字格式化（None -> "—"）。用 textfmt，全项目口径一致。"""
    if value is None:
        return "—"
    return textfmt.fmt_num(value, fmt)


def _digest(payload):
    """输入摘要（留痕用）：对规范化的 JSON 取 sha256 前 16 位。"""
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          default=str)
    except (TypeError, ValueError):
        text = repr(payload)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _ply_summary(context):
    """
    铺层摘要：角度 -> 总厚/层数（确定性，按角度升序）。

    只读 `context['plies']`；缺失时返回空表（调用方文案自行兜底）。
    """
    per_angle = {}
    for ply in (context or {}).get("plies") or []:
        try:
            ang = float(ply.get("angle", 0.0))
        except (TypeError, ValueError):
            ang = 0.0
        try:
            thk = float(ply.get("thickness", 0.0))
        except (TypeError, ValueError):
            thk = 0.0
        rec = per_angle.setdefault(ang, {"n": 0, "thickness": 0.0})
        rec["n"] += 1
        rec["thickness"] += thk
    return [(a, per_angle[a]["n"], per_angle[a]["thickness"])
            for a in sorted(per_angle)]


def _dominant_stress(context):
    """
    主控应力分量（按**相对占比**判断，不与许用值相除）。

    注意：这里刻意不做 `应力/许用值` 的比值——那是失效指数（FI）的定义，
    属于 strength_check 的 L0 职责（ADR-0002）。本函数只回答
    "三个分量里谁占得最多"，用于决定建议方向，不产生任何判定。
    """
    stress = (context or {}).get("stress") or {}
    parts = []
    for key in ("s11", "s22", "s12"):
        try:
            parts.append((key, abs(float(stress.get(key, 0.0)))))
        except (TypeError, ValueError):
            parts.append((key, 0.0))
    total = sum(v for _, v in parts)
    if total <= 0.0:
        return None, 0.0
    key, val = max(parts, key=lambda kv: kv[1])
    return key, val / total


class DeterministicAiAdvisor(AiAdvisor):
    """
    确定性 Fake 适配器（**CI 只用它**）。

    确定性怎么保证（验收项 1/2）：
      * 不联网、不读时钟、不用随机数、不依赖进程内可变状态；
      * 所有文本只由输入数据经纯函数格式化而来；
      * 因此"同一输入连续两次调用"必然逐字节相同（测试直接断言 to_dict 相等）。

    它**不是**在装 AI：它的价值是让 L1 解释这条链路在无网络环境下
    完整、可回归、可审计地跑通，并为将来接入真 LLM 提供同签名的对照基线。
    """

    advisor_id = ADVISOR_FAKE

    def __init__(self):
        self.invocations = []

    # -- 内部 --------------------------------------------------------------

    def _note(self, method, draft_text, facts, payload, degraded=False,
              degrade_reason=None):
        trace = {
            "method": method,
            "advisor_id": self.advisor_id,
            "input_digest": _digest(payload),
            "degraded": bool(degraded),
            "degrade_reason": degrade_reason,
        }
        note = AdvisorNote(method, draft_text, facts=facts,
                           advisor_id=self.advisor_id, degraded=degraded,
                           degrade_reason=degrade_reason, trace=trace)
        if self.invocations is not None:
            self.invocations.append(trace)
        return note

    # -- 方法 --------------------------------------------------------------

    def explain_check(self, check, study_name=None):
        """基于 check.summary 与危险部位，解释校核结论（不改写任何判定）。"""
        chk = check or {}
        summ = chk.get("summary") or {}
        hotspots = chk.get("hotspots") or []

        all_pass = summ.get("all_pass")
        max_fi = summ.get("max_fi")
        min_ms = summ.get("min_ms")
        crit = summ.get("criteria") or []
        n_fail = summ.get("n_fail_rows")
        n_elems = summ.get("n_elems")
        n_rows = summ.get("n_detail_rows")
        n_bays = summ.get("n_bays_checked")
        n_excl = summ.get("n_bays_excluded")
        sf = summ.get("safety_factor")

        # 判定句只复述确定性结论，不改写、不升级
        if all_pass is True:
            verdict = "确定性校核判定为合格"
        elif all_pass is False:
            verdict = "确定性校核判定为不合格"
        else:
            verdict = "确定性校核未给出判定（无有效结果）"

        lines = []
        lines.append("一、结论解读")
        lines.append(
            "%s：最大失效指数 FI=%s，最小安全裕度 MS=%s，判定准则 %s，"
            "安全系数 %s。"
            % (verdict, _num(max_fi), _num(min_ms),
               "、".join(crit) if crit else "（未指定）", _num(sf)))
        lines.append(
            "覆盖范围：%s 个单元、%s 条单元×铺层×准则明细；"
            "参与自然格级校核 %s 格，因不连续被排除 %s 格；"
            "不通过明细 %s 条。"
            % (_num(n_elems, "%d"), _num(n_rows, "%d"), _num(n_bays, "%d"),
               _num(n_excl, "%d"), _num(n_fail, "%d")))

        if hotspots:
            lines.append("二、危险部位")
            for i, h in enumerate(hotspots[:3], 1):
                lines.append(
                    "%d) 单元 %s 第 %s 铺层（%s）：FI=%s，MS=%s。"
                    % (i, h.get("elem"), h.get("sp"), h.get("criterion"),
                       _num(h.get("FI")), _num(h.get("MS"))))
            if len(hotspots) > 3:
                lines.append("（其余 %d 处见第 4 章危险部位表）"
                             % (len(hotspots) - 3))
            lines.append("解释侧重点：优先核对 FI 最高的部位是否与几何不连续"
                         "（长桁终止/开口/部分肋）重叠。")
        else:
            lines.append("二、危险部位：本次结果未产生危险部位条目。")

        lines.append("三、阅读建议")
        if all_pass is False:
            lines.append("报告判定为不合格，审阅时应逐条核对该批次不通过明细的"
                         "单元与自然格归属，确认是否需要局部加强后重跑。")
        elif all_pass is True:
            lines.append("报告判定为合格，审阅时可关注最接近判据的部位"
                         "（FI 最大/MS 最小者）与其网格质量是否达标。")
        else:
            lines.append("无有效判定时，请先排查求解与抽取环节是否成功。")

        facts = {
            "all_pass": all_pass,
            "max_fi": max_fi,
            "min_ms": min_ms,
            "criteria": list(crit),
            "safety_factor": sf,
            "n_fail_rows": n_fail,
            "n_hotspots": len(hotspots),
        }
        payload = {"method": METHOD_EXPLAIN_CHECK, "summary": summ,
                   "hotspots": hotspots, "study_name": study_name}
        return self._note(METHOD_EXPLAIN_CHECK, "\n".join(lines), facts, payload)

    def explain_hotspot(self, hotspot, context=None):
        """解释单个危险部位"为什么这里危险"（FI/MS + 铺层 + 自然格归属）。"""
        h = hotspot or {}
        ctx = context or {}
        bay = ctx.get("bay_id") or "（未归属自然格）"
        plies = _ply_summary(ctx)
        n_plies = ctx.get("n_plies")
        total_thk = ctx.get("total_thickness")
        stress = ctx.get("stress") or {}

        elem = h.get("elem")
        sp = h.get("sp")
        crit = h.get("criterion")
        fi = h.get("FI")
        ms = h.get("MS")

        try:
            fi_f = float(fi) if fi is not None else None
        except (TypeError, ValueError):
            fi_f = None
        try:
            ms_f = float(ms) if ms is not None else None
        except (TypeError, ValueError):
            ms_f = None

        lines = []
        lines.append("部位：单元 %s 第 %s 铺层，自然格 %s。" % (elem, sp, bay))

        # 为什么会"这里"：把 FI/MS 译成人能读的严重程度（不重新判定）
        if fi_f is not None and fi_f >= 1.0:
            sev = ("该处 FI=%s ≥ 1，确定性校核已判定此处失效"
                   % _num(fi_f))
        elif fi_f is not None:
            sev = ("该处 FI=%s < 1，确定性校核判定此处未失效，但为全模型最不利部位"
                   % _num(fi_f))
        else:
            sev = "该处未给出 FI"
        if ms_f is not None:
            sev += "；MS=%s（%s）" % (
                _num(ms_f), "≤ 0，无安全裕度" if ms_f <= 0 else "> 0，尚有余量")
        lines.append(sev + "。")

        # 铺层信息（"为什么是这一层"）
        if plies:
            desc = "、".join("%g°×%d 层（总厚 %s）" % (ang, n, _num(thk))
                            for ang, n, thk in plies)
            lines.append("铺层构成：%s；共 %s 层，总厚 %s。"
                         % (desc, _num(n_plies, "%d"), _num(total_thk)))
            target = _find_angle(plies, sp)
            if target is not None:
                lines.append("失效发生在第 %s 铺层，对应 %g° 方向，"
                             "该方向承载最不利。" % (sp, target))
        else:
            lines.append("铺层构成：上下文未提供铺层信息，无法判断失效所在层的铺向。")

        # 应力侧写（相对占比，不产生判定）
        dom, share = _dominant_stress(ctx)
        if dom:
            lines.append(
                "该处应力分量 s11=%s、s22=%s、s12=%s MPa；主控分量为 %s"
                "（占比约 %.0f%%）。"
                % (_num(stress.get("s11")), _num(stress.get("s22")),
                   _num(stress.get("s12")), dom, share * 100.0))
        else:
            lines.append("该处应力分量上下文缺失，无法判断主控分量。")

        lines.append("判据：%s。以上解释由 L1 辅助生成，"
                     "合格与否以确定性校核的 FI/MS 为准。" % (crit or "（未指定）"))

        facts = {"elem": elem, "sp": sp, "criterion": crit, "FI": fi, "MS": ms,
                 "bay_id": bay}
        payload = {"method": METHOD_EXPLAIN_HOTSPOT, "hotspot": h,
                   "bay_id": ctx.get("bay_id"), "plies": ctx.get("plies"),
                   "stress": stress}
        return self._note(METHOD_EXPLAIN_HOTSPOT, "\n".join(lines), facts, payload)

    def suggest_layup(self, hotspot, context=None):
        """铺层改进建议草案（基于应力分量占比与失效准则）。"""
        h = hotspot or {}
        ctx = context or {}
        crit = h.get("criterion") or "（未指定准则）"
        plies = _ply_summary(ctx)
        dom, share = _dominant_stress(ctx)

        lines = []
        lines.append("建议草案（针对 %s 准则下单元 %s 第 %s 铺层的失效）："
                     % (crit, h.get("elem"), h.get("sp")))

        if dom is None:
            lines.append("由于缺少应力分量上下文，无法给出有方向性的铺层建议；"
                         "建议先补充该部位的应力提取结果。")
        else:
            if dom == "s11":
                action = ("以 0° 铺层承担主控的轴向应力：优先增加 0° 铺层"
                          "（或提高其厚度比例），使轴向刚度与强度同步提高")
            elif dom == "s22":
                action = ("以 90° 铺层承担主控的横向应力：优先增加 90° 铺层，"
                          "并检查横向基体主导失效（基体拉伸/压缩）")
            else:
                action = ("以 ±45° 铺层承担主控的剪切应力：优先增加 ±45° 铺层，"
                          "提高剪切承载与层间协调能力")
            lines.append("1) %s。主控分量 %s 占比约 %.0f%%。"
                         % (action, dom, share * 100.0))

        if plies:
            desc = "、".join("%g°×%d 层" % (ang, n) for ang, n, _ in plies)
            lines.append("2) 当前铺层为 %s；建议在同角度上增层而非改向，"
                         "避免引入新的层间不连续。" % desc)
        else:
            lines.append("2) 当前铺层上下文缺失，建议先确认铺层定义再定方案。")

        if "hashin" in [str(c).lower() for c in [crit]]:
            lines.append("3) Hashin 判据下失效常由基体拉伸/压缩控制："
                         "增层时同时关注横向与剪切分量，必要时考虑层间增韧。")
        elif "tsai_wu" in str(crit).lower():
            lines.append("3) Tsai-Wu 判据为二次耦合判据：单纯加厚可能改变交互项"
                         "的相对权重，建议增层后重新校核确认。")
        else:
            lines.append("3) 更换准则或增层后都应重新校核，不要以本草案代替校核。")

        lines.append("注意：本建议为 L1 辅助草稿，须由强度分析人员确认；"
                     "采纳后必须重跑强度校核（FI/MS 由确定性引擎给出），"
                     "AI 不参与任何合格判定。")

        facts = {"elem": h.get("elem"), "sp": h.get("sp"),
                 "criterion": crit, "dominant_stress": dom,
                 "dominant_share": round(share, 4)}
        payload = {"method": METHOD_SUGGEST_LAYUP, "hotspot": h,
                   "plies": ctx.get("plies"), "stress": ctx.get("stress")}
        return self._note(METHOD_SUGGEST_LAYUP, "\n".join(lines), facts, payload)


def _find_angle(plies, sp):
    """由 section point 序号猜该铺层的角度（1-based；越界返回 None）。"""
    try:
        idx = int(sp)
    except (TypeError, ValueError):
        return None
    if idx is None or idx < 1 or idx > len(plies):
        return None
    return plies[idx - 1][0]


# ---------------------------------------------------------------------------
# 降级：适配器缺失 / 不可用
# ---------------------------------------------------------------------------

class FallbackAiAdvisor(AiAdvisor):
    """
    适配器**缺失**时的替身：三个方法都返回确定性兜底说明，`degraded=True`。

    它的存在使"没有 AI"不再是一条特殊分支——调用方永远拿到同样结构的
    `AdvisorNote`，报告照常生成，只是文本换成不含任何 AI 参与的兜底内容
    （验收项 3，且这条路径本身可测）。
    """

    advisor_id = ADVISOR_FALLBACK

    def __init__(self, reason=DEGRADE_MISSING):
        self.reason = reason
        self.invocations = []

    def _note(self, method, text, facts, payload):
        trace = {
            "method": method,
            "advisor_id": self.advisor_id,
            "input_digest": _digest(payload),
            "degraded": True,
            "degrade_reason": self.reason,
        }
        note = AdvisorNote(method, text, facts=facts,
                           advisor_id=self.advisor_id, degraded=True,
                           degrade_reason=self.reason, trace=trace)
        if self.invocations is not None:
            self.invocations.append(trace)
        return note

    def explain_check(self, check, study_name=None):
        summ = (check or {}).get("summary") or {}
        all_pass = summ.get("all_pass")
        verdict = ("合格" if all_pass is True
                   else ("不合格" if all_pass is False else "未知"))
        text = "\n".join([
            FALLBACK_PREFIX,
            "确定性校核判定：%s；最大失效指数 FI=%s，最小安全裕度 MS=%s。"
            % (verdict, _num(summ.get("max_fi")), _num(summ.get("min_ms"))),
            "本次未启用 AI 解释，以上为纯确定性口径（FI/MS 由校核引擎计算）。",
        ])
        return self._note(METHOD_EXPLAIN_CHECK, text,
                          {"all_pass": all_pass,
                           "max_fi": summ.get("max_fi"),
                           "min_ms": summ.get("min_ms")},
                          {"summary": summ, "study_name": study_name})

    def explain_hotspot(self, hotspot, context=None):
        h = hotspot or {}
        bay = (context or {}).get("bay_id") or "（未归属自然格）"
        text = "\n".join([
            FALLBACK_PREFIX,
            "危险部位：单元 %s 第 %s 铺层，自然格 %s，准则 %s，"
            "FI=%s，MS=%s。"
            % (h.get("elem"), h.get("sp"), bay, h.get("criterion"),
               _num(h.get("FI")), _num(h.get("MS"))),
            "本次未启用 AI 解释，此处的失效原因需人工判读（见危险部位表与校核明细）。",
        ])
        return self._note(METHOD_EXPLAIN_HOTSPOT, text,
                          {"elem": h.get("elem"), "sp": h.get("sp"),
                           "criterion": h.get("criterion"),
                           "FI": h.get("FI"), "MS": h.get("MS")},
                          {"hotspot": h, "bay_id": bay})

    def suggest_layup(self, hotspot, context=None):
        h = hotspot or {}
        text = "\n".join([
            FALLBACK_PREFIX,
            "铺层改进需由强度分析人员根据 %s 准则下的失效模式人工确定。"
            % (h.get("criterion") or "（未指定）"),
            "本次未启用 AI 建议，请以确定性校核的不通过明细为依据制定方案，"
            "修改后重新校核。",
        ])
        return self._note(METHOD_SUGGEST_LAYUP, text,
                          {"elem": h.get("elem"), "criterion": h.get("criterion")},
                          {"hotspot": h})


class SafeAiAdvisor(AiAdvisor):
    """
    包住任意适配器：调用抛异常时**安静降级**为确定性兜底（degraded=True）。

    "不可用"包括：适配器内部报错、依赖缺失（如真机 LLM 库/网络不可达）、
    返回了不合规对象。任何一种都不允许把异常甩到报告生成路径上——
    AI 是 L1 辅助，它坏掉不能让 L0 的结论出不了报告。
    """

    def __init__(self, inner):
        self.inner = inner
        inner_id = getattr(inner, "advisor_id", None) or type(inner).__name__
        self.advisor_id = ADVISOR_SAFE_PREFIX + str(inner_id)
        self.invocations = []
        self._fallback = FallbackAiAdvisor(DEGRADE_ERROR)

    def _wrap(self, method, *args, **kwargs):
        try:
            note = getattr(self.inner, method)(*args, **kwargs)
        except Exception as exc:
            note = self._fallback_note(method, args, kwargs, str(exc))
            return note
        if not isinstance(note, AdvisorNote):
            note = self._fallback_note(
                method, args, kwargs,
                "适配器返回了非 AdvisorNote 对象（%r）" % type(note).__name__)
            return note
        # 留痕：正常路径也记一条
        trace = dict(note.trace or {})
        trace.setdefault("method", method)
        trace.setdefault("advisor_id", self.advisor_id)
        if self.invocations is not None:
            self.invocations.append(trace)
        return note

    def _fallback_note(self, method, args, kwargs, reason):
        note = getattr(self._fallback, method)(*args, **kwargs)
        note.degraded = True
        note.degrade_reason = DEGRADE_ERROR
        note.trace = dict(note.trace or {})
        note.trace["advisor_id"] = self.advisor_id
        note.trace["degraded"] = True
        note.trace["degrade_reason"] = DEGRADE_ERROR
        note.trace["error"] = reason
        if self.invocations is not None:
            self.invocations.append(dict(note.trace))
        return note

    def explain_check(self, check, study_name=None):
        return self._wrap(METHOD_EXPLAIN_CHECK, check, study_name=study_name)

    def explain_hotspot(self, hotspot, context=None):
        return self._wrap(METHOD_EXPLAIN_HOTSPOT, hotspot, context=context)

    def suggest_layup(self, hotspot, context=None):
        return self._wrap(METHOD_SUGGEST_LAYUP, hotspot, context=context)


def _is_advisor(obj):
    """鸭子类型检查：三个方法都在即视为符合协议。"""
    return all(callable(getattr(obj, m, None)) for m in METHODS)


def resolve_advisor(advisor=None):
    """
    统一入口：把调用方给的东西归一成一个**保证可用**的适配器。

    - None / 不可用/ 显式关闭  -> `FallbackAiAdvisor`（degraded 恒 True）
    - 合规适配器               -> 包一层 `SafeAiAdvisor`（异常时降级）

    报告渲染与服务层都走这里，于是"AI 挂了"永远只表现为 degraded=True，
    而不是异常或空报告（验收项 3/6）。
    """
    if advisor is None:
        return FallbackAiAdvisor(DEGRADE_MISSING)
    if isinstance(advisor, bool) and advisor is False:
        return FallbackAiAdvisor(DEGRADE_DISABLED)
    if isinstance(advisor, SafeAiAdvisor):
        return advisor
    if isinstance(advisor, FallbackAiAdvisor):
        return advisor
    if not _is_advisor(advisor):
        return FallbackAiAdvisor(DEGRADE_INVALID)
    return SafeAiAdvisor(advisor)


# ---------------------------------------------------------------------------
# 真机适配器（本票不实现）
# ---------------------------------------------------------------------------

class RealAiAdvisor(AiAdvisor):
    """
    真机 LLM 适配器位置。**本票刻意不实现**。

    原因（票26 原文）：
      1. 目标机（Win7 + py3.8、可能离线）能否联网尚未确认；
      2. CI 不应依赖网络——依赖网络的测试本质上不可回归；
      3. 接入真实 LLM 还牵涉密钥管理、内容审计与「输出不得影响判定」的
         运行时校验，属于独立的一次决策，不在本票范围内。

    保留位置的意义：`resolve_advisor` / 报告 / 服务层已经按"任意 AiAdvisor
    实现都能接"来写，将来只要在这里实现三个方法即可接入，无需改动调用方。
    """

    advisor_id = ADVISOR_REAL

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "RealAiAdvisor 未实现（票26 有意留空）：目标机联网能力未确认，"
            "且 CI 不得依赖网络。请先实现 DeterministicAiAdvisor 的对照基线，"
            "并完成密钥/审计方案评审后再接入真实 LLM。")

    def explain_check(self, check, study_name=None):
        raise NotImplementedError("RealAiAdvisor.explain_check 未实现")

    def explain_hotspot(self, hotspot, context=None):
        raise NotImplementedError("RealAiAdvisor.explain_hotspot 未实现")

    def suggest_layup(self, hotspot, context=None):
        raise NotImplementedError("RealAiAdvisor.suggest_layup 未实现")
