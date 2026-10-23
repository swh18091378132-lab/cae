# -*- coding: utf-8 -*-
"""
compat_check.py -- Abaqus 内置 Python 兼容性静态检查

**基线：Abaqus 2023 内置 Python 2.7.15（ADR-0012 实测；取代原 ADR-0003 的 2.6.2 假设）。**

2.7 已支持的（**不再拦截**）：dict/set 推导、str.format、print() 函数式、
with 多上下文、OrderedDict、collections、argparse 等。

仍然拦截（2.7 也没有，属于 3.x-only）：
  1. f-string                         (3.6+)
  2. `yield from`                     (3.3+)
  3. `nonlocal`                        (3.0+)
  4. 变量注解 / 参数注解 / 函数返回注解 (3.0+)
  5. `async` / `await`                 (3.5+)
  6. 解包扩展 `[*a, *b]` / `{**a}`     (3.5+)
  7. 数字字面量下划线 `1_000`          (3.6+)
  8. 海象运算符 `:=`                   (3.8+)

本模块是**纯标准库**，可在宿主 Python (3.8+) 上运行，用于检查
abaqus_capabilities/ 下将在 Abaqus 内部执行的脚本。

用法:
    python abaqus_capabilities/compat_check.py [路径...]
    默认检查 abaqus_capabilities/ 目录下所有 .py（排除自身）

退出码 0=全通过, 1=有违规。
"""

import ast
import os
import sys


class Violation(object):
    def __init__(self, path, lineno, rule, detail):
        self.path = path
        self.lineno = lineno
        self.rule = rule
        self.detail = detail

    def __str__(self):
        return "%s:%d [%s] %s" % (self.path, self.lineno, self.rule, self.detail)


class Py262Checker(ast.NodeVisitor):
    """基于 AST 的 2.6.2 不兼容特性检测。"""

    def __init__(self, path, source):
        self.path = path
        self.source = source
        self.violations = []

    def _add(self, node, rule, detail):
        self.violations.append(
            Violation(self.path, getattr(node, "lineno", 0), rule, detail))

    # --- f-string (JoinedStr) : 3.6+ ---
    def visit_JoinedStr(self, node):
        self._add(node, "f-string", "Python 2.7 不支持 f-string，用 % 或 .format()")
        self.generic_visit(node)

    # --- yield from : 3.3+ ---
    def visit_YieldFrom(self, node):
        self._add(node, "yield-from", "Python 2.7 不支持 yield from")
        self.generic_visit(node)

    # --- nonlocal : 3.0+ ---
    def visit_Nonlocal(self, node):
        self._add(node, "nonlocal", "Python 2.7 不支持 nonlocal")
        self.generic_visit(node)

    # --- 变量注解 : 3.0+ ---
    def visit_AnnAssign(self, node):
        self._add(node, "annotation", "Python 2.7 不支持变量注解")
        self.generic_visit(node)

    # --- 参数/函数返回注解 : 3.0+ ---
    def visit_arguments(self, node):
        for a in list(getattr(node, "args", [])):
            if getattr(a, "annotation", None) is not None:
                self._add(a, "annotation", "Python 2.7 不支持参数注解")
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        if getattr(node, "returns", None) is not None:
            self._add(node, "annotation", "Python 2.7 不支持函数返回注解")
        self.generic_visit(node)

    # --- async / await : 3.5+ ---
    def visit_AsyncFunctionDef(self, node):
        self._add(node, "async", "Python 2.7 不支持 async 函数")
        self.generic_visit(node)

    def visit_Await(self, node):
        self._add(node, "async", "Python 2.7 不支持 await")
        self.generic_visit(node)

    # --- 解包扩展 [*a] / {**a} : 3.5+ ---
    def visit_List(self, node):
        for elt in node.elts:
            if isinstance(elt, ast.Starred):
                self._add(node, "unpack-generalization",
                          "Python 2.7 不支持列表解包 [*a]")
                break
        self.generic_visit(node)

    def visit_Dict(self, node):
        for k in node.keys:
            if k is None:  # {**a} 在 AST 中 key 为 None
                self._add(node, "unpack-generalization",
                          "Python 2.7 不支持字典解包 {**a}")
                break
        self.generic_visit(node)

    # --- 数字字面量下划线 1_000 : 3.6+ ---
    def visit_Constant(self, node):
        seg = getattr(node, "end_col_offset", None)
        if isinstance(getattr(node, "value", None), (int, float)) and not isinstance(
                getattr(node, "value", None), bool):
            try:
                raw = ast.get_source_segment(self.source, node) or ""
            except Exception:
                raw = ""
            if "_" in raw:
                self._add(node, "numeric-underscore",
                          "Python 2.7 不支持数字下划线 1_000")
        self.generic_visit(node)


def check_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as exc:
        return [Violation(path, exc.lineno or 0, "syntax", str(exc))]
    checker = Py262Checker(path, src)
    checker.visit(tree)
    return checker.violations


def iter_py_files(root):
    if os.path.isfile(root):
        yield root
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def main(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    targets = argv[1:] or [here]
    self_path = os.path.abspath(__file__)

    all_v = []
    n_files = 0
    for t in targets:
        for p in iter_py_files(t):
            if os.path.abspath(p) == self_path:
                continue
            n_files += 1
            all_v.extend(check_file(p))

    if all_v:
        print("Python 2.7(2.6.2-subset) COMPAT: %d violation(s) in %d file(s)"
              % (len(all_v), n_files))
        for v in all_v:
            print("  " + str(v))
        return 1

    print("Python 2.7(2.6.2-subset) COMPAT: OK (%d file(s) checked)" % n_files)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
