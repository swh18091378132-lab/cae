# -*- coding: utf-8 -*-
"""
compat_check.py -- Abaqus 6.13 (Python 2.6.2) 兼容性静态检查

ADR-0003：Abaqus 6.13 内置 Python 是 2.6.2，不是 2.7/3.x。
2.6.2 下会**直接语法/运行失败**的写法必须被拦下。

本模块是**纯标准库**，可在宿主 Python (3.8) 上运行，用于检查
abaqus_capabilities/ 下将在 Abaqus 内部执行的脚本。

检查项（保守但高价值）：
  1. 语法层面：用 py2 风格大括号 print 检测（2.6 无 print() 函数，但 print(...) 单值可用，
     故只禁 f-string 与 dict/set 推导等 2.7+ 特性）
  2. f-string                      -> 2.6 语法错误
  3. 带关键字的 dict 推导/集合推导  -> 2.6 不支持
  4. `yield from`                  -> 2.6 不支持
  5. `nonlocal`                    -> 2.6 不支持
  6. `{}` 形式的 set 字面量在 2.6 可用，不检查
  7. `str.format` 的自动编号 `{}` 在 2.6 可用，不检查
  8. `import` 现代库（numpy 高版本 API 等）无法静态判断，此处仅标注

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

    # --- f-string (JoinedStr / FormattedValue) ---
    def visit_JoinedStr(self, node):
        self._add(node, "f-string", "Python 2.6.2 不支持 f-string，用 % 或 .format()")
        self.generic_visit(node)

    # --- yield from ---
    def visit_YieldFrom(self, node):
        self._add(node, "yield-from", "Python 2.6.2 不支持 yield from")
        self.generic_visit(node)

    # --- nonlocal ---
    def visit_Nonlocal(self, node):
        self._add(node, "nonlocal", "Python 2.6.2 不支持 nonlocal")
        self.generic_visit(node)

    # --- 推导式中带 if 的异步/复杂形式不做；重点拦 dict/set 推导 ---
    def visit_DictComp(self, node):
        self._add(node, "dict-comprehension", "Python 2.6.2 不支持 dict 推导，用 dict((k,v) for ...)")
        self.generic_visit(node)

    def visit_SetComp(self, node):
        self._add(node, "set-comprehension", "Python 2.6.2 不支持集合推导，用 set(... for ...)")
        self.generic_visit(node)

    # --- 带注解的函数/变量 (3.x only) ---
    def visit_AnnAssign(self, node):
        self._add(node, "annotation", "Python 2.6.2 不支持变量注解")
        self.generic_visit(node)

    def visit_arguments(self, node):
        for a in list(getattr(node, "args", [])):
            if getattr(a, "annotation", None) is not None:
                self._add(a, "annotation", "Python 2.6.2 不支持参数注解")
        self.generic_visit(node)

    # --- print 函数形式：2.6 可用 print('x')（单值括号）; 但 print(a, b) 会当 tuple 打印
    #     这里不强制，只在多参数时提示（需人工判断）
    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Name) and f.id == "print" and len(node.args) > 1:
            self._add(node, "print-multiarg",
                      "2.6 的 print(a, b) 会打印 tuple，建议用 %% 拼接")
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
        print("Python 2.6.2 COMPAT: %d violation(s) in %d file(s)"
              % (len(all_v), n_files))
        for v in all_v:
            print("  " + str(v))
        return 1

    print("Python 2.6.2 COMPAT: OK (%d file(s) checked)" % n_files)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
