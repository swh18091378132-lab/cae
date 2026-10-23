# -*- coding: utf-8 -*-
"""
compat_py38.py -- 宿主侧 Python 3.8 兼容性静态检查（P2 通用工具）

目标机是 **Windows 7 + CPython 3.8.10**（见 requirements.txt 与 research_win7_toolchain.md），
所以 apps/workbench 下的代码**不得使用 3.9+ 语法**。本模块把这件事变成可自动检查的
断言，避免后续每张票各写一份粗糙的字符串匹配。

检查两件事：

1. **语法层**：用 `ast.parse(..., feature_version=(3, 8))` 让 CPython 自己按 3.8 语法
   规则解析；解析失败即为违规。
2. **语义层**（3.8 能解析但跑不起来的用法）：
   - 内建类型直接下标做注解 `list[int]` / `dict[str, int]` / `tuple[...]` / `set[...]`
     （3.9 前必须 `typing.List[...]`）；
   - `str.removeprefix` / `str.removesuffix`（3.9+）；
   - `zoneinfo` / `graphlib` / `math.lcm` / `functools.cache`（3.9+ 标准库）；
   - `dict | dict` 合并运算符（3.9+）。

纯标准库。可在宿主 Python 3.8+ 上运行。
"""

import ast
import os

# 3.9+ 才有的标准库名字（仅匹配 import 语句里的模块名，不做全局字符串匹配）
NEW_STDLIB_MODULES = ("zoneinfo", "graphlib")

# 3.9+ 才有的函数/方法名
NEW_ATTRS = ("removeprefix", "removesuffix")

# 3.9+ 才有的 math/functools 成员
NEW_MEMBERS = {"math": ("lcm", "nextafter_"), "functools": ("cache",)}

# 内建类型下标做注解（3.9 起合法）
BUILTIN_GENERICS = ("list", "dict", "tuple", "set", "frozenset", "type")


class Violation(object):
    def __init__(self, path, lineno, rule, detail):
        self.path = path
        self.lineno = lineno
        self.rule = rule
        self.detail = detail

    def __str__(self):
        return "%s:%d [%s] %s" % (self.path, self.lineno, self.rule, self.detail)


def _annotation_violations(path, tree):
    """注解里用了内建泛型 `list[int]` 之类 -> 3.8 无法求值。"""
    out = []
    ann_nodes = []

    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            ann_nodes.append(node.annotation)
        elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
            ann_nodes.append(node.annotation)
        elif isinstance(node, ast.FunctionDef) and node.returns is not None:
            ann_nodes.append(node.returns)

    for ann in ann_nodes:
        for sub in ast.walk(ann):
            if isinstance(sub, ast.Subscript):
                value = sub.value
                if isinstance(value, ast.Name) and value.id in BUILTIN_GENERICS:
                    out.append(Violation(
                        path, getattr(sub, "lineno", 0), "builtin-generics",
                        "Python 3.8 不支持 %s[...] 注解，用 typing.%s[...]"
                        % (value.id, value.id.capitalize())))
    return out


def _name_violations(path, tree):
    """3.9+ 的模块/函数/属性用法。"""
    out = []
    for node in ast.walk(tree):
        # import zoneinfo / from graphlib import ...
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = (alias.name or "").split(".")[0]
                if root in NEW_STDLIB_MODULES:
                    out.append(Violation(path, node.lineno, "new-stdlib",
                                         "%s 是 3.9+ 标准库" % root))
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in NEW_STDLIB_MODULES:
                out.append(Violation(path, node.lineno, "new-stdlib",
                                     "%s 是 3.9+ 标准库" % root))

        # x.removeprefix(...) / functools.cache / math.lcm
        elif isinstance(node, ast.Attribute):
            if node.attr in NEW_ATTRS:
                out.append(Violation(path, getattr(node, "lineno", 0), "new-attr",
                                     "%s 是 3.9+ 方法" % node.attr))
            if isinstance(node.value, ast.Name):
                mod = node.value.id
                if node.attr in NEW_MEMBERS.get(mod, ()):
                    out.append(Violation(path, getattr(node, "lineno", 0),
                                         "new-member",
                                         "%s.%s 是 3.9+" % (mod, node.attr)))

        # a | b 作为字典合并（3.9+）：BinOp(BitOr) 且两侧看起来是 dict 字面量/变量
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            left, right = node.left, node.right
            if isinstance(left, ast.Dict) or isinstance(right, ast.Dict):
                out.append(Violation(path, getattr(node, "lineno", 0),
                                     "dict-merge",
                                     "a | b 合并字典是 3.9+，用 update()/dict(...)"))
    return out


def check_source(path, source):
    """检查一段源码，返回 [Violation]。"""
    out = []
    try:
        tree = ast.parse(source, filename=path, feature_version=(3, 8))
    except SyntaxError as exc:
        out.append(Violation(path, exc.lineno or 0, "syntax-3.8",
                             "按 Python 3.8 语法无法解析: %s" % exc.msg))
        return out
    except TypeError:
        # 老解释器不支持 feature_version 参数；退化为普通解析
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            out.append(Violation(path, exc.lineno or 0, "syntax", str(exc)))
            return out

    out.extend(_annotation_violations(path, tree))
    out.extend(_name_violations(path, tree))
    return out


def check_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        return check_source(path, fh.read())


def iter_py_files(root):
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".tmp_test")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def check_paths(paths):
    """检查多个文件/目录，返回 [Violation]。"""
    out = []
    for p in paths:
        for f in iter_py_files(p):
            out.extend(check_file(f))
    return out


def format_violations(violations):
    return "\n".join("  - " + str(v) for v in violations)
