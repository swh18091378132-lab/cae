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

-- ===========================================================================
-- P2 新增（票16 型号配置 / 票18 审批流）
--
-- 迁移策略：**只新增表与新增列，不改 P1 既有结构**。所有 P2 新列都能为 NULL
-- 或有默认值，因此 P1 的历史库/历史 Study 打开后照常可用（票16 验收项）。
-- ===========================================================================

-- 型号配置：全机总体参数 → 各部件参数集 的层级根（票16）
CREATE TABLE IF NOT EXISTS model_configs (
    id           TEXT PRIMARY KEY,        -- 例：MODEL-20261023-120000
    name         TEXT NOT NULL,
    overall_json TEXT NOT NULL,           -- 全机总体参数（JSON）
    rule_json    TEXT,                    -- 分解规则快照（JSON，可追溯）
    note         TEXT,
    created_at   TEXT NOT NULL
);

-- 型号配置的部件分解结果：一个型号配置下有多个部件（票16）
--   parent 维度表达「全机 → 部件」；部件本身可再分（未来多级）
CREATE TABLE IF NOT EXISTS model_config_parts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id     TEXT NOT NULL REFERENCES model_configs(id),
    part_key     TEXT NOT NULL,           -- 部件键：wing / tail / fuselage
    template_id  TEXT NOT NULL,           -- 该部件所用模板族
    params_json  TEXT NOT NULL,           -- 分解出的参数集（JSON）
    study_id     TEXT REFERENCES studies(id),  -- 关联的 Study（可后补）
    parent_part  TEXT,                    -- 上级部件键（NULL = 直挂全机）
    created_at   TEXT NOT NULL,
    UNIQUE(model_id, part_key)
);

-- 报告审批状态（票18）：一个 Study 的**每一版报告**各自独立审批
--   状态机：analyzing → pending_review → approved / rejected → signed
--   关键合规点：重跑产生新报告版本时，新行状态必须重置为 pending_review，
--   旧审批绝不继承到新内容上（由 approval 模块保证，本表只存事实）。
CREATE TABLE IF NOT EXISTS report_approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id    TEXT NOT NULL REFERENCES studies(id),
    artifact_id INTEGER REFERENCES artifacts(id),  -- 对应 report 产物
    version     INTEGER NOT NULL,        -- 报告版本
    status      TEXT NOT NULL,           -- analyzing/pending_review/approved/rejected/signed
    updated_at  TEXT NOT NULL,
    UNIQUE(study_id, version)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_study ON artifacts(study_id, kind);
CREATE INDEX IF NOT EXISTS idx_decisions_study ON decisions(study_id);
CREATE INDEX IF NOT EXISTS idx_mc_parts_model ON model_config_parts(model_id);
CREATE INDEX IF NOT EXISTS idx_approval_study ON report_approvals(study_id, version);
CREATE INDEX IF NOT EXISTS idx_approval_status ON report_approvals(status);

-- ===========================================================================
-- P3 新增（票25 优化会话与设计点）
--
-- 迁移策略与 P2 一致：**只新增表，不改既有表结构**。两张表都用
-- CREATE TABLE IF NOT EXISTS，旧库打开时幂等建出，P1/P2 历史数据不受影响。
-- ===========================================================================

-- 优化会话（票25）：意图 / 预算 / 设计向量声明快照 —— "当初想优化什么"落库
CREATE TABLE IF NOT EXISTS optimization_sessions (
    id                 TEXT PRIMARY KEY,      -- 例：OPT-20261023-120000-a1b2
    name               TEXT NOT NULL,
    template_id        TEXT NOT NULL,         -- 部件族（决定用哪套参数契约）
    intent_json        TEXT NOT NULL,         -- 优化意图：目标 + 约束声明（JSON）
    budget_json        TEXT NOT NULL,         -- 收敛配置：预算三元组（JSON）
    design_vector_json TEXT NOT NULL,         -- 设计向量声明**快照**（JSON）
    status             TEXT NOT NULL,         -- created/running/finished/failed
    result_json        TEXT,                  -- 结算快照（停止原因/评估次数）
    adoption_json      TEXT,                  -- L2 采用决策快照（NULL = 未批准）
    note               TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

-- 设计点（票25）：一个优化会话内的某个设计向量 + 它那一次独立 Study 的结论
--   一个点 = 一个独立 Study（ADR-0009：每轮产生新 Study，绝不覆盖）
CREATE TABLE IF NOT EXISTS optimization_points (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL REFERENCES optimization_sessions(id),
    seq              INTEGER NOT NULL,        -- 会话内评估顺序（从 1 起）
    vector_json      TEXT NOT NULL,           -- 设计向量（JSON）
    vector_key       TEXT NOT NULL,           -- 去重键（与 optimizer.vector_key 同口径）
    study_id         TEXT REFERENCES studies(id),  -- 该点对应的独立 Study
    group_name       TEXT NOT NULL,           -- feasible/infeasible/failed（三分组）
    ok               INTEGER NOT NULL,        -- 技术层面是否跑通
    feasible         INTEGER,                 -- 是否满足约束（NULL = 技术故障）
    objective_value  REAL,                    -- 目标值（技术故障时为 NULL）
    evaluation_json  TEXT NOT NULL,           -- objectives.evaluate 的完整结果（权威）
    error_json       TEXT,                    -- 技术故障原因（kind + message）
    source           TEXT,                    -- baseline/doe/local/manual
    reused_from      TEXT,                    -- 复用了哪个 Study 的数模（ADR-0005）
    created_at       TEXT NOT NULL,
    UNIQUE(session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_opt_points_session
    ON optimization_points(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_opt_points_group
    ON optimization_points(session_id, group_name);
CREATE INDEX IF NOT EXISTS idx_opt_points_study
    ON optimization_points(study_id);
"""

# P1 库升级补丁（票16「P1 既有数据可平滑升级」）：
# 老库里的表没有 P2 新列；这里用 ALTER TABLE ADD COLUMN 幂等补齐。
#   (table, column, type)
MIGRATIONS = [
    ("studies", "model_id", "TEXT"),          # 该 Study 属于哪个型号配置（票16）
    ("studies", "part_key", "TEXT"),          # 该 Study 是型号里的哪个部件（票16）
    ("artifacts", "approval_status", "TEXT"),  # 报告产物的审批状态缓存（票18）
]

# 不可变守卫：禁止修改已存在 artifact 的内容字段（只允许 status 变更）
IMMUTABLE_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_artifacts_no_update
BEFORE UPDATE OF study_id, kind, version, checksum, content_uri, params_json, created_at
ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifact is immutable: append a new version instead');
END;
"""


def _columns(conn, table):
    try:
        rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    except Exception:
        return set()
    return set(r[1] for r in rows)


def _migrate(conn):
    """
    幂等补齐 P2 新增列（P1 老库平滑升级，票16 验收项）。

    ALTER TABLE ADD COLUMN 在 SQLite 上不会重写既有行，新列取 NULL —— 于是
    P1 的历史 Study / artifact 打开后依然可读可用。
    """
    for table, column, coltype in MIGRATIONS:
        cols = _columns(conn, table)
        if not cols:
            continue          # 表不存在（理论上不会：SCHEMA 已建）
        if column in cols:
            continue
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s"
                     % (table, column, coltype))
    conn.commit()


def connect(db_path=None):
    path = db_path or DEFAULT_DB_PATH
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executescript(IMMUTABLE_TRIGGERS)
    conn.commit()
    return conn
