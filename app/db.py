"""持久化层：SQLite（单文件，Docker 命名卷挂载，重启后数据保留）。

写操作统一走 service 层的串行化事务（全局写锁 + IMMEDIATE 事务 + busy_timeout），
保证并行退役办理时，最后一份副本始终留在在用盘上。
"""
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "/data/archive.db")

# 进程内写锁：FastAPI 线程池下先在应用层串行化，再由 SQLite 单写者模型兜底
write_lock = threading.RLock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx():
    """写事务：持有进程锁，立即获取 SQLite 写锁（BEGIN IMMEDIATE）。

    多进程（多 worker / 多容器）情况下由 busy_timeout 等待写锁，
    锁等待冲突抛 OperationalError 时由 service 层重试。
    """
    with write_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS device_models (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS disks (
    id         INTEGER PRIMARY KEY,
    model_id   INTEGER NOT NULL REFERENCES device_models(id),
    disk_no    TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('active', 'retired')),
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    retired_at TEXT,
    UNIQUE (model_id, disk_no)
);

CREATE TABLE IF NOT EXISTS manifest_entries (
    id         INTEGER PRIMARY KEY,
    disk_id    INTEGER NOT NULL REFERENCES disks(id),
    test_no    TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (disk_id, test_no, sha256)
);

CREATE TABLE IF NOT EXISTS retirement_records (
    id          INTEGER PRIMARY KEY,
    model_id    INTEGER NOT NULL REFERENCES device_models(id),
    disk_ids    TEXT NOT NULL,           -- JSON 数组，保留退役时的盘号顺序
    disk_nos    TEXT NOT NULL,           -- JSON 数组（盘号冗余，便于拔盘线下核对）
    result      TEXT NOT NULL CHECK (result IN ('retired', 'rejected')),
    missing     TEXT NOT NULL,           -- JSON：缺副本的 [{test_no, sha256, on_disk}]
    entry_count INTEGER NOT NULL,        -- 本次核对的报文条目数
    detail      TEXT NOT NULL,           -- 文本说明
    created_at  TEXT NOT NULL
);
"""


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
