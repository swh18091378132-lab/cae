# -*- coding: utf-8 -*-
"""
study.py -- Study / 血缘 / 决策 的领域操作（M0）

不依赖 CATIA / Abaqus，可独立自测。
兼容 Python 3.8。
"""

import os
import json
import hashlib
import datetime

try:
    from .db import connect
except ImportError:  # 允许作为脚本/在 core 目录内直接运行
    from db import connect


def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def create_study(study_id, name, note=None, conn=None):
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            "INSERT INTO studies(id, name, created_at, note) VALUES (?,?,?,?)",
            (study_id, name, _now(), note))
        conn.commit()
        return study_id
    finally:
        if own:
            conn.close()


def _next_version(conn, study_id, kind):
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM artifacts "
        "WHERE study_id=? AND kind=?", (study_id, kind)).fetchone()
    return int(row["v"]) + 1


def add_artifact(study_id, kind, content_uri=None, params=None, status="ok", conn=None):
    """追加一个不可变 artifact 版本；返回 artifact id。"""
    own = conn is None
    conn = conn or connect()
    try:
        params_json = json.dumps(params or {}, ensure_ascii=False, sort_keys=True)
        # 指纹 = content_uri + 参数 + 内容文件(若存在)
        payload = content_uri + "|" + params_json
        if content_uri and os.path.isfile(content_uri):
            with open(content_uri, "rb") as fh:
                payload += "|" + hashlib.sha256(fh.read()).hexdigest()
        checksum = _sha256(payload)

        version = _next_version(conn, study_id, kind)
        cur = conn.execute(
            "INSERT INTO artifacts(study_id, kind, version, checksum, content_uri, "
            "params_json, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (study_id, kind, version, checksum, content_uri, params_json, status, _now()))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        if own:
            conn.close()


def link(parent_id, child_id, conn=None):
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO edges(parent_id, child_id) VALUES (?,?)",
            (parent_id, child_id))
        conn.commit()
    finally:
        if own:
            conn.close()


def add_decision(study_id, gate, actor, decision, rationale=None, conn=None):
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            "INSERT INTO decisions(study_id, gate, actor, decision, rationale, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (study_id, gate, actor, decision, rationale, _now()))
        conn.commit()
    finally:
        if own:
            conn.close()


def _artifact_rows(conn, study_id):
    return conn.execute(
        "SELECT * FROM artifacts WHERE study_id=? ORDER BY id", (study_id,)).fetchall()


def print_lineage(study_id, conn=None):
    """按拓扑顺序打印血缘 DAG。"""
    own = conn is None
    conn = conn or connect()
    try:
        rows = _artifact_rows(conn, study_id)
        if not rows:
            print("(study %s 无 artifact)" % study_id)
            return
        by_id = {r["id"]: r for r in rows}
        # 建立 child -> parents 与 parent -> children
        parents = {r["id"]: [] for r in rows}
        children = {r["id"]: [] for r in rows}
        for e in conn.execute(
                "SELECT parent_id, child_id FROM edges").fetchall():
            if e["parent_id"] in by_id and e["child_id"] in by_id:
                parents[e["child_id"]].append(e["parent_id"])
                children[e["parent_id"]].append(e["child_id"])

        # 简单拓扑排序（Kahn）
        indeg = {i: len(parents[i]) for i in by_id}
        ready = sorted([i for i in by_id if indeg[i] == 0])
        order = []
        while ready:
            n = ready.pop(0)
            order.append(n)
            for c in children[n]:
                indeg[c] -= 1
                if indeg[c] == 0:
                    ready.append(c)
            ready.sort()
        if len(order) != len(by_id):
            order = sorted(by_id)  # 兜底

        print("Study %s lineage DAG (%d artifacts):" % (study_id, len(rows)))
        indent = {}
        for i in order:
            depth = 0
            for p in parents[i]:
                depth = max(depth, indent.get(p, 0) + 1)
            indent[i] = depth
            r = by_id[i]
            arrow = "  " * depth + ("+- " if depth else "")
            print("%s%s#%d  v%d  %-14s  sha=%s  %s"
                  % (arrow, r["kind"], r["id"], r["version"],
                     r["kind"], r["checksum"][:12],
                     r["content_uri"] or ""))

        # 决策
        decs = conn.execute(
            "SELECT * FROM decisions WHERE study_id=? ORDER BY id", (study_id,)).fetchall()
        if decs:
            print("Decisions (L2):")
            for d in decs:
                print("   [%s] %s by %s -> %s (%s)"
                      % (d["gate"], d["decision"], d["actor"], d["rationale"] or "", d["created_at"]))
    finally:
        if own:
            conn.close()


def list_studies(conn=None):
    own = conn is None
    conn = conn or connect()
    try:
        rows = conn.execute("SELECT * FROM studies ORDER BY created_at").fetchall()
        if not rows:
            print("(无 Study)")
        for r in rows:
            n = conn.execute("SELECT COUNT(*) c FROM artifacts WHERE study_id=?",
                             (r["id"],)).fetchone()["c"]
            print("%-20s %-30s artifacts=%d  %s"
                  % (r["id"], r["name"], n, r["created_at"]))
    finally:
        if own:
            conn.close()
