# -*- coding: utf-8 -*-
"""
db.py -- SQLite 连接与 schema（M0）

设计要点（见 ADR-0006 / 设计共识 §4）:
  * 追溯单位 = Study
  * Artifact 不可变：同一 (study_id, kind) 只增版本，永不覆盖
  * 血缘用 edges(parent->child) 的 DAG 表达
  * L2 人类决策单独记 decisions 审计日志

兼容 Python 3.8（禁用 3.9+ 语法），仅用标准库 sqlite3。
"""

import os
import sqlite3

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "cae_platform.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS studies (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    note       TEXT
);

-- 一次 Study 内的各类产物，按 kind 独立版本号递增，不可变
CREATE TABLE IF NOT EXISTS artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id    TEXT NOT NULL REFERENCES studies(id),
    kind        TEXT NOT NULL,            -- parameter_set / catpart / mesh / inp / odb /
                                          -- extraction / check_rule / check_result / report / topology
    version     INTEGER NOT NULL,
    checksum    TEXT NOT NULL,            -- sha256 内容指纹
    content_uri TEXT,                     -- 文件相对路径或内联标识
    params_json TEXT,                     -- 关键口径/参数(JSON)
    status      TEXT NOT NULL DEFAULT 'ok',
    created_at  TEXT NOT NULL,
    UNIQUE(study_id, kind, version)
);

-- 血缘边：parent -> child（DAG）
CREATE TABLE IF NOT EXISTS edges (
    parent_id INTEGER NOT NULL REFERENCES artifacts(id),
    child_id  INTEGER NOT NULL REFERENCES artifacts(id),
    PRIMARY KEY (parent_id, child_id)
);

-- L2 人类决策审计
CREATE TABLE IF NOT EXISTS decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id   TEXT NOT NULL REFERENCES studies(id),
    gate       TEXT NOT NULL,             -- 例如 design_select / rule_confirm / report_sign
    actor      TEXT NOT NULL,
    decision   TEXT NOT NULL,
    rationale  TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_study ON artifacts(study_id, kind);
CREATE INDEX IF NOT EXISTS idx_decisions_study ON decisions(study_id);
"""

# 不可变守卫：禁止修改已存在 artifact 的内容字段（只允许 status 变更）
IMMUTABLE_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_artifacts_no_update
BEFORE UPDATE OF study_id, kind, version, checksum, content_uri, params_json, created_at
ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifact is immutable: append a new version instead');
END;
"""


def connect(db_path=None):
    path = db_path or DEFAULT_DB_PATH
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executescript(IMMUTABLE_TRIGGERS)
    conn.commit()
    return conn
