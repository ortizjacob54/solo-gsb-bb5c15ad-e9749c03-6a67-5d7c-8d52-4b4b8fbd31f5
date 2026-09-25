"""SQLite 持久化层（标准库 sqlite3，随应用启动自动建表）。"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

# 数据库文件路径，可通过环境变量 DATABASE_PATH 覆盖（Docker 中指向挂载卷）
DATABASE_PATH = os.environ.get("DATABASE_PATH", os.path.join("data", "app.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calculations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL,
    capacity_a  INTEGER NOT NULL,
    capacity_b  INTEGER NOT NULL,
    capacity_c  INTEGER NOT NULL,
    feasible    INTEGER NOT NULL,
    imbalance   INTEGER,
    load_a      INTEGER,
    load_b      INTEGER,
    load_c      INTEGER
);

CREATE TABLE IF NOT EXISTS devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    calculation_id  INTEGER NOT NULL REFERENCES calculations(id) ON DELETE CASCADE,
    device_code     TEXT    NOT NULL,
    power           INTEGER NOT NULL,
    allowed_phases  TEXT    NOT NULL,  -- JSON 数组，如 ["A","B"]
    assigned_phase  TEXT               -- 无可行方案时为 NULL
);

CREATE INDEX IF NOT EXISTS idx_devices_calculation_id
    ON devices (calculation_id);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """初始化数据库：创建目录并建表（幂等，可重复执行）。"""
    directory = os.path.dirname(DATABASE_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def insert_calculation(request: Any, result: Any) -> int:
    """保存一次计算的输入与结果，返回记录 id。

    :param request: schemas.CalculationRequest
    :param result: solver.SolveResult
    """
    created_at = datetime.now(timezone.utc).isoformat()
    loads = result.loads or {}
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO calculations
                (created_at, capacity_a, capacity_b, capacity_c,
                 feasible, imbalance, load_a, load_b, load_c)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                request.capacities.A,
                request.capacities.B,
                request.capacities.C,
                int(result.feasible),
                result.imbalance,
                loads.get("A"),
                loads.get("B"),
                loads.get("C"),
            ),
        )
        calculation_id = cursor.lastrowid
        assignment = result.assignment or {}
        for device in sorted(request.devices, key=lambda d: d.id):
            conn.execute(
                """
                INSERT INTO devices
                    (calculation_id, device_code, power, allowed_phases, assigned_phase)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    calculation_id,
                    device.id,
                    device.power,
                    json.dumps(device.allowed_phases, ensure_ascii=False),
                    assignment.get(device.id),
                ),
            )
    return calculation_id


def _summary_from_row(row: sqlite3.Row) -> dict:
    feasible = bool(row["feasible"])
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "capacities": {
            "A": row["capacity_a"],
            "B": row["capacity_b"],
            "C": row["capacity_c"],
        },
        "feasible": feasible,
        "imbalance": row["imbalance"] if feasible else None,
        "loads": (
            {"A": row["load_a"], "B": row["load_b"], "C": row["load_c"]}
            if feasible
            else None
        ),
    }


def list_calculations(limit: int = 100, offset: int = 0) -> list[dict]:
    """按创建时间倒序返回计算记录摘要。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM calculations ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [_summary_from_row(row) for row in rows]


def get_calculation(calculation_id: int) -> Optional[dict]:
    """返回单条计算记录详情（含设备输入与分配结果），不存在时返回 None。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM calculations WHERE id = ?", (calculation_id,)
        ).fetchone()
        if row is None:
            return None
        device_rows = conn.execute(
            "SELECT * FROM devices WHERE calculation_id = ? ORDER BY device_code",
            (calculation_id,),
        ).fetchall()

    detail = _summary_from_row(row)
    detail["devices"] = [
        {
            "device_id": d["device_code"],
            "power": d["power"],
            "allowed_phases": json.loads(d["allowed_phases"]),
            "assigned_phase": d["assigned_phase"],
        }
        for d in device_rows
    ]
    return detail
