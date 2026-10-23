# -*- coding: utf-8 -*-
"""
template_registry.py -- 模板注册表：参数契约 + 参数化包络（M1）

对应 ADR-0004。术语见 GLOSSARY：参数契约 / 参数化包络。
兼容 Python 3.8，仅标准库。
"""

import os
import json

# 本文件位于 <repo>/apps/workbench/core/，data/templates 在 <repo>/data/templates
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_DEFAULT_DIR = os.path.join(_REPO_ROOT, "data", "templates")


class ParamError(ValueError):
    """参数不在契约/包络内。"""


class Template(object):
    def __init__(self, data, base_dir):
        self.data = data
        self.id = data["id"]
        self.name = data.get("name", self.id)
        self.parameters = data.get("parameters", [])
        self.envelope = data.get("envelope", {})
        self.template_file = data.get("template_file", "")
        # 部件族默认校核口径（P2 / spec 用户故事 18）：
        # "为不同部件族指定不同的默认准则与许用值，以便贴合各族材料与结构特点"
        # 形状与 strength_check.load_rules 的 overrides 一致，但优先级最低。
        self.checks = data.get("checks", {}) or {}
        self._base_dir = base_dir

    def names(self):
        return [p["name"] for p in self.parameters]

    def contract(self):
        return {p["name"]: p for p in self.parameters}

    def default_rules(self):
        """
        该部件族的默认校核规则覆盖项（可空 dict）。

        只包含模板**显式声明**的部分（准则、许用值、安全系数等），
        其余由 strength_check.DEFAULT_RULES 兜底。调用方传入的 rules 优先级更高。
        """
        out = {}
        for key in ("criteria", "allowables", "safety_factor", "ms_threshold",
                    "stress_source"):
            if key in self.checks:
                val = self.checks[key]
                out[key] = dict(val) if isinstance(val, dict) else val
        return out

    def full_path(self):
        if not self.template_file:
            return None
        return os.path.join(self._base_dir, os.path.basename(self.template_file))

    def validate(self, params):
        """
        校验参数是否满足参数契约与参数化包络。
        返回规范化后的参数字典；不通过抛 ParamError。
        """
        contract = self.contract()
        out = {}

        # 1) 未知参数
        unknown = [k for k in params if k not in contract]
        if unknown:
            raise ParamError("未知参数: %s（模板 %s 的参数契约只有 %s）"
                             % (", ".join(unknown), self.id, ", ".join(self.names())))

        # 2) 逐个类型/范围校验；缺省补默认
        for name, spec in contract.items():
            if name in params:
                val = params[name]
            else:
                if "default" in spec:
                    val = spec["default"]
                else:
                    raise ParamError("缺少必填参数: %s" % name)

            typ = spec.get("type", "float")
            if typ == "int":
                if isinstance(val, bool) or not isinstance(val, int):
                    raise ParamError("参数 %s 应为 int，实际 %r" % (name, val))
            elif typ == "float":
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    raise ParamError("参数 %s 应为数值，实际 %r" % (name, val))
                val = float(val)
            elif typ == "str":
                if not isinstance(val, str):
                    raise ParamError("参数 %s 应为 str" % name)
            if "min" in spec and val < spec["min"]:
                raise ParamError("参数 %s=%s 低于下限 %s" % (name, val, spec["min"]))
            if "max" in spec and val > spec["max"]:
                raise ParamError("参数 %s=%s 高于上限 %s" % (name, val, spec["max"]))
            out[name] = val

        # 3) 包络约束（跨参数）
        _SAFE = {"__builtins__": {}, "max": max, "min": min, "abs": abs, "round": round}
        for expr in self.envelope.get("constraints", []):
            try:
                ok = eval(expr, _SAFE, dict(out))  # 受控表达式
            except Exception as exc:
                raise ParamError("包络约束无法求值: %s (%s)" % (expr, exc))
            if not ok:
                raise ParamError("超出参数化包络: 约束不满足 [%s]" % expr)

        return out


def load(template_id, templates_dir=None):
    d = templates_dir or _DEFAULT_DIR
    path = os.path.join(d, template_id + ".json")
    if not os.path.isfile(path):
        raise FileNotFoundError("模板不存在: %s" % path)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return Template(data, d)


def list_templates(templates_dir=None):
    d = templates_dir or _DEFAULT_DIR
    if not os.path.isdir(d):
        return []
    return sorted(n[:-5] for n in os.listdir(d) if n.endswith(".json"))
