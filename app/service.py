"""业务层：盘片清单登记、退役校验（全有或全无）、并行串行化。

核心不变量：退役成功后，每一份（测次编号, 摘要）报文仍至少有一份副本
留在「所选退役范围之外的在用盘」上。同一测次的不同摘要分别核对。
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from dataclasses import dataclass, field

from db import now, tx

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
HEADER = {"测次编号", "摘要", "test_no", "sha256", "sha-256", "digest"}


class ServiceError(Exception):
    """输入或状态错误，status 为建议的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass
class ParsedCSV:
    rows: list[tuple[str, str]]               # [(test_no, sha256), ...]
    duplicates: list[tuple[str, str]] = field(default_factory=list)  # 重复（自动去重）


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


def parse_manifest_csv(text: str) -> ParsedCSV:
    """解析清单 CSV：每行「测次编号, SHA-256」。

    - 允许带表头（首行两列均为已知表头名时自动跳过）；
    - 严格两列；测次编号非空；摘要必须是 64 位十六进制；
    - 同一 (测次, 摘要) 重复行自动去重并记录；
    - 同一测次不同摘要保留为不同条目（分别核对）。
    """
    if text is None or not text.strip():
        raise ServiceError("CSV 内容为空")
    try:
        reader = csv.reader(io.StringIO(text))
        raw = [row for row in reader]
    except csv.Error as e:
        raise ServiceError(f"CSV 解析失败：{e}")

    # 跳过全空行
    rows = [r for r in raw if any(cell.strip() for cell in r)]
    if not rows:
        raise ServiceError("CSV 内容为空")

    if len(rows[0]) == 2 and rows[0][0].strip().lower() in HEADER \
            and rows[0][1].strip().lower() in HEADER:
        rows = rows[1:]
    if not rows:
        raise ServiceError("CSV 只有表头，没有数据行")

    seen: set[tuple[str, str]] = set()
    parsed: list[tuple[str, str]] = []
    duplicates: list[tuple[str, str]] = []
    for i, row in enumerate(rows, start=1):
        if len(row) != 2:
            raise ServiceError(f"第 {i} 行列数为 {len(row)}，每行必须恰好两列：测次编号,SHA-256")
        test_no = row[0].strip()
        digest = row[1].strip().lower()
        if not test_no:
            raise ServiceError(f"第 {i} 行测次编号为空")
        if not SHA256_RE.match(digest):
            raise ServiceError(f"第 {i} 行摘要不是合法的 64 位十六进制 SHA-256：{digest[:32]}…")
        key = (test_no, digest)
        if key in seen:
            duplicates.append(key)
            continue
        seen.add(key)
        parsed.append(key)
    return ParsedCSV(rows=parsed, duplicates=duplicates)


# ---------------- 型号 ----------------

def create_model(conn: sqlite3.Connection, name: str) -> int:
    name = (name or "").strip()
    if not valid_name(name):
        raise ServiceError("型号名只能由字母数字及 _ . - 组成，长度 1~64")
    cur = conn.execute("SELECT id FROM device_models WHERE name=?", (name,))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO device_models(name, created_at) VALUES(?,?)",
                       (name, now()))
    return cur.lastrowid


def get_model(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM device_models WHERE name=?", (name,)).fetchone()
    if row is None:
        raise ServiceError(f"型号 {name!r} 不存在", status=404)
    return row


# ---------------- 盘片登记 ----------------

def register_disk(model_name: str, disk_no: str, csv_text: str, note: str = "") -> dict:
    """按盘号登记 CSV 清单。已登记的盘号不能覆盖（含已退役盘）。"""
    disk_no = (disk_no or "").strip()
    if not valid_name(disk_no):
        raise ServiceError("盘号只能由字母数字及 _ . - 组成，长度 1~64")
    parsed = parse_manifest_csv(csv_text)

    with tx() as conn:
        model = get_model(conn, model_name)
        existing = conn.execute(
            "SELECT id, status FROM disks WHERE model_id=? AND disk_no=?",
            (model["id"], disk_no)).fetchone()
        if existing is not None:
            raise ServiceError(
                f"盘号 {disk_no!r} 已登记（状态：{existing['status']}），不能覆盖；"
                "复制到新盘后请用新盘号补登", status=409)
        ts = now()
        cur = conn.execute(
            "INSERT INTO disks(model_id, disk_no, status, note, created_at) "
            "VALUES(?,?, 'active', ?,?)",
            (model["id"], disk_no, (note or "").strip(), ts))
        disk_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO manifest_entries(disk_id, test_no, sha256, created_at) "
            "VALUES(?,?,?,?)",
            [(disk_id, t, d, ts) for t, d in parsed.rows])
        return {"disk_id": disk_id, "disk_no": disk_no,
                "entry_count": len(parsed.rows),
                "duplicates": parsed.duplicates}


# ---------------- 退役 ----------------

def _retire_once(conn: sqlite3.Connection, model_id: int, disk_nos: list[str]) -> dict:
    """在已持有的事务内执行一次退役尝试（全部 SQL 与校验）。"""
    if not disk_nos:
        raise ServiceError("至少选择一块盘")
    # 去重但保留顺序
    seen: set[str] = set()
    ordered = [d for d in disk_nos if not (d in seen or seen.add(d))]

    placeholders = ",".join("?" * len(ordered))
    disks = conn.execute(
        f"SELECT id, disk_no, status FROM disks WHERE model_id=? AND disk_no IN ({placeholders}) "
        f"ORDER BY id",
        [model_id, *ordered]).fetchall()
    found = {d["disk_no"] for d in disks}
    missing_disks = [d for d in ordered if d not in found]
    if missing_disks:
        raise ServiceError(f"盘号不存在：{', '.join(missing_disks)}", status=404)
    not_active = [d["disk_no"] for d in disks if d["status"] != "active"]
    if not_active:
        raise ServiceError(f"以下盘不是在用状态，不能退役：{', '.join(not_active)}", status=409)

    target_ids = [d["id"] for d in disks]
    id_ph = ",".join("?" * len(target_ids))

    # 所选盘上的全部报文条目（同测次不同摘要分别列出）
    selected_entries = conn.execute(
        f"SELECT DISTINCT test_no, sha256 FROM manifest_entries "
        f"WHERE disk_id IN ({id_ph}) ORDER BY test_no, sha256",
        target_ids).fetchall()

    # 范围之外的在用盘：同型号、active、且不在所选集合内
    outside = conn.execute(
        f"SELECT me.test_no, me.sha256, d.disk_no FROM manifest_entries me "
        f"JOIN disks d ON d.id = me.disk_id "
        f"WHERE d.model_id=? AND d.status='active' AND d.id NOT IN ({id_ph})",
        [model_id, *target_ids]).fetchall()
    outside_keys = {(r["test_no"], r["sha256"]) for r in outside}

    missing: list[dict] = []
    for e in selected_entries:
        key = (e["test_no"], e["sha256"])
        if key not in outside_keys:
            on_disks = [r["disk_no"] for r in conn.execute(
                f"SELECT DISTINCT d.disk_no FROM manifest_entries me "
                f"JOIN disks d ON d.id = me.disk_id "
                f"WHERE me.test_no=? AND me.sha256=? AND d.id IN ({id_ph}) "
                f"ORDER BY d.disk_no",
                (e["test_no"], e["sha256"], *target_ids)).fetchall()]
            missing.append({"test_no": e["test_no"], "sha256": e["sha256"],
                            "on_disk": on_disks})

    ts = now()
    if missing:
        # 整次退役不生效：只留拒绝登记，盘状态与清单均不变
        conn.execute(
            "INSERT INTO retirement_records(model_id, disk_ids, disk_nos, result, missing, "
            "entry_count, detail, created_at) VALUES(?,?,?, 'rejected', ?,?,?,?)",
            (model_id, json.dumps(target_ids), json.dumps(ordered),
             json.dumps(missing, ensure_ascii=False), len(selected_entries),
             f"缺 {len(missing)} 份副本，整次退役未生效", ts))
        return {"ok": False, "disk_nos": ordered, "missing": missing,
                "entry_count": len(selected_entries), "created_at": ts}

    conn.execute(
        f"UPDATE disks SET status='retired', retired_at=? "
        f"WHERE id IN ({id_ph})", [ts, *target_ids])
    conn.execute(
        "INSERT INTO retirement_records(model_id, disk_ids, disk_nos, result, missing, "
        "entry_count, detail, created_at) VALUES(?,?,?, 'retired', '[]', ?,?,?)",
        (model_id, json.dumps(target_ids), json.dumps(ordered),
         len(selected_entries),
         f"{len(ordered)} 块盘、{len(selected_entries)} 份报文全部有范围外在用盘副本，已退役",
         ts))
    return {"ok": True, "disk_nos": ordered, "missing": [],
            "entry_count": len(selected_entries), "created_at": ts}


def retire_disks(model_name: str, disk_nos: list[str]) -> dict:
    """退役入口：串行化 + 写锁冲突重试，保证并行办理的安全。"""
    with tx() as conn:
        model = get_model(conn, model_name)
        return _retire_once(conn, model["id"], list(disk_nos))


# ---------------- 查询 ----------------

def list_models(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.*, "
        "SUM(CASE WHEN d.status='active' THEN 1 ELSE 0 END) AS active_disks, "
        "SUM(CASE WHEN d.status='retired' THEN 1 ELSE 0 END) AS retired_disks "
        "FROM device_models m LEFT JOIN disks d ON d.model_id=m.id "
        "GROUP BY m.id ORDER BY m.name").fetchall()


def model_detail(conn: sqlite3.Connection, name: str) -> dict:
    model = get_model(conn, name)
    disks = conn.execute(
        "SELECT * FROM disks WHERE model_id=? ORDER BY id", (model["id"],)).fetchall()
    out_disks = []
    for d in disks:
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM manifest_entries WHERE disk_id=?",
            (d["id"],)).fetchone()["c"]
        out_disks.append({**dict(d), "entry_count": cnt})
    records = conn.execute(
        "SELECT * FROM retirement_records WHERE model_id=? ORDER BY id DESC LIMIT 100",
        (model["id"],)).fetchall()
    return {"model": dict(model), "disks": out_disks,
            "records": [dict(r) for r in records]}
