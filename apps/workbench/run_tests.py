# -*- coding: utf-8 -*-
"""
run_tests.py -- 统一测试运行器（发现并运行 tests/ 下所有 test_*.py）

用法 (在 apps/workbench 下):
    python run_tests.py
每个测试文件暴露 main() -> int(0 通过)。
"""

import os
import sys
import glob
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(HERE, "tests")


def _load(path):
    name = "t_" + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    files = sorted(glob.glob(os.path.join(TESTS, "test_*.py")))
    if not files:
        print("no tests found")
        return 2
    failed = 0
    for f in files:
        print("=== %s ===" % os.path.basename(f))
        mod = _load(f)
        rc = mod.main()
        if rc != 0:
            failed += 1
    print("\n%d/%d test files passed" % (len(files) - failed, len(files)))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
