# -*- coding: utf-8 -*-
"""
test_m0.py -- M0 自测（不依赖 CATIA/Abaqus，纯标准库）

运行:
    python tests/test_m0.py
退出码 0 = 全部通过。
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(os.path.dirname(HERE), "core")
sys.path.insert(0, CORE)

import db
import study


def main():
    tmp = os.path.join(os.path.dirname(HERE), ".tmp_test")
    if not os.path.isdir(tmp):
        os.makedirs(tmp)
    dbpath = os.path.join(tmp, "t.db")
    if os.path.isfile(dbpath):
        os.remove(dbpath)
    conn = db.connect(dbpath)

    fails = []

    # 1) 建 Study
    study.create_study("S1", "测试", conn=conn)

    # 2) 追加同 kind 两次 -> 版本递增、内容不可变
    a1 = study.add_artifact("S1", "mesh", content_uri="m1.inp", params={"e": 100}, conn=conn)
    a2 = study.add_artifact("S1", "mesh", content_uri="m2.inp", params={"e": 200}, conn=conn)
    v1 = conn.execute("SELECT version FROM artifacts WHERE id=?", (a1,)).fetchone()["version"]
    v2 = conn.execute("SELECT version FROM artifacts WHERE id=?", (a2,)).fetchone()["version"]
    if (v1, v2) != (1, 2):
        fails.append("version should be 1,2 got %s,%s" % (v1, v2))

    # 3) 不可变：尝试 UPDATE 内容字段应抛错
    try:
        conn.execute("UPDATE artifacts SET checksum='x' WHERE id=?", (a1,))
        conn.commit()
        fails.append("immutability trigger did NOT fire")
    except Exception:
        pass  # 期望

    # 4) status 允许变更（不应抛错）
    try:
        conn.execute("UPDATE artifacts SET status='warn' WHERE id=?", (a1,))
        conn.commit()
    except Exception as e:
        fails.append("status update should be allowed: %s" % e)

    # 5) 血缘边
    b = study.add_artifact("S1", "odb", content_uri="o.odb", conn=conn)
    study.link(a1, b, conn=conn)
    study.link(a2, b, conn=conn)
    n = conn.execute("SELECT COUNT(*) c FROM edges WHERE child_id=?", (b,)).fetchone()["c"]
    if n != 2:
        fails.append("expected 2 parents, got %d" % n)

    # 6) 决策日志
    study.add_decision("S1", "report_sign", "chief", "approve", "ok", conn=conn)
    d = conn.execute("SELECT COUNT(*) c FROM decisions WHERE study_id='S1'").fetchone()["c"]
    if d != 1:
        fails.append("expected 1 decision, got %d" % d)

    # 7) 指纹稳定性：同参数同 uri 得同 sha
    x = study.add_artifact("S1", "report", content_uri="r.docx", params={"t": 1}, conn=conn)
    y = study.add_artifact("S1", "report", content_uri="r.docx", params={"t": 1}, conn=conn)
    sx = conn.execute("SELECT checksum FROM artifacts WHERE id=?", (x,)).fetchone()["checksum"]
    sy = conn.execute("SELECT checksum FROM artifacts WHERE id=?", (y,)).fetchone()["checksum"]
    if sx != sy:
        fails.append("checksum not deterministic for identical content")

    conn.close()

    if fails:
        print("FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("M0 SELF-TEST: ALL PASSED (%s)" % dbpath)
    return 0


if __name__ == "__main__":
    sys.exit(main())
