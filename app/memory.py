"""记忆层：SQLite 单文件，存对话历史、长期事实、去重标记。

QQ 和 B 站共用同一个库，但用 platform 字段隔离，互不串味。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            config.ensure_dirs()
            _conn = sqlite3.connect(str(config.MEMORY_PATH), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    channel  TEXT NOT NULL,
                    user_id  TEXT DEFAULT '',
                    name     TEXT DEFAULT '',
                    role     TEXT NOT NULL,
                    content  TEXT NOT NULL,
                    ts       REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_msg_chan ON messages(platform, channel, id);

                CREATE TABLE IF NOT EXISTS facts (
                    platform TEXT NOT NULL,
                    channel  TEXT NOT NULL,
                    k        TEXT NOT NULL,
                    v        TEXT NOT NULL,
                    ts       REAL NOT NULL,
                    PRIMARY KEY (platform, channel, k)
                );

                CREATE TABLE IF NOT EXISTS marks (
                    platform TEXT NOT NULL,
                    k        TEXT NOT NULL,
                    ts       REAL NOT NULL,
                    PRIMARY KEY (platform, k)
                );

                CREATE TABLE IF NOT EXISTS daycount (
                    day   TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    n     INTEGER NOT NULL,
                    PRIMARY KEY (day, scope)
                );

                CREATE TABLE IF NOT EXISTS actions (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    kind     TEXT NOT NULL,
                    target   TEXT NOT NULL,
                    content  TEXT NOT NULL,
                    ok       INTEGER NOT NULL DEFAULT 1,
                    ts       REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_act ON actions(id DESC);
                """
            )
            _conn.commit()
        return _conn


# --------------------------------------------------------------------- 对话
def append(
    platform: str,
    channel: str,
    role: str,
    content: str,
    user_id: str = "",
    name: str = "",
) -> None:
    with _lock:
        db = _db()
        db.execute(
            "INSERT INTO messages(platform, channel, user_id, name, role, content, ts)"
            " VALUES(?,?,?,?,?,?,?)",
            (platform, channel, user_id, name, role, content, time.time()),
        )
        # 每个会话只留最近 400 条，防止库无限长大
        db.execute(
            "DELETE FROM messages WHERE platform=? AND channel=? AND id NOT IN"
            " (SELECT id FROM messages WHERE platform=? AND channel=? ORDER BY id DESC LIMIT 400)",
            (platform, channel, platform, channel),
        )
        db.commit()


def history(platform: str, channel: str, limit: int = 8) -> List[Dict[str, str]]:
    """取最近 limit 轮（user/assistant 各算一条）。"""
    with _lock:
        rows = _db().execute(
            "SELECT role, content, name FROM messages WHERE platform=? AND channel=?"
            " ORDER BY id DESC LIMIT ?",
            (platform, channel, limit * 2),
        ).fetchall()
    out = []
    for r in reversed(rows):
        item = {"role": r["role"], "content": r["content"]}
        if r["role"] == "user" and r["name"]:
            item["content"] = f"{r['name']}：{r['content']}"
        out.append(item)
    return out


def clear(platform: str, channel: str) -> None:
    with _lock:
        db = _db()
        db.execute("DELETE FROM messages WHERE platform=? AND channel=?", (platform, channel))
        db.commit()


# --------------------------------------------------------------------- 事实
def remember(platform: str, channel: str, key: str, value: str) -> None:
    with _lock:
        db = _db()
        db.execute(
            "INSERT INTO facts(platform, channel, k, v, ts) VALUES(?,?,?,?,?)"
            " ON CONFLICT(platform, channel, k) DO UPDATE SET v=excluded.v, ts=excluded.ts",
            (platform, channel, key, value, time.time()),
        )
        db.commit()


def facts(platform: str, channel: str, limit: int = 12) -> Dict[str, str]:
    with _lock:
        rows = _db().execute(
            "SELECT k, v FROM facts WHERE platform=? AND channel=? ORDER BY ts DESC LIMIT ?",
            (platform, channel, limit),
        ).fetchall()
    return {r["k"]: r["v"] for r in rows}


# --------------------------------------------------------------------- 去重
def mark(platform: str, key: str) -> None:
    with _lock:
        db = _db()
        db.execute("INSERT OR REPLACE INTO marks(platform, k, ts) VALUES(?,?,?)", (platform, key, time.time()))
        db.commit()


def seen(platform: str, key: str) -> bool:
    with _lock:
        row = _db().execute("SELECT 1 FROM marks WHERE platform=? AND k=?", (platform, key)).fetchone()
    return row is not None


# --------------------------------------------------------------------- 配额
def today(scope: str) -> str:
    return time.strftime("%Y-%m-%d")


def used_today(scope: str) -> int:
    with _lock:
        row = _db().execute(
            "SELECT n FROM daycount WHERE day=? AND scope=?", (today(scope), scope)
        ).fetchone()
    return int(row["n"]) if row else 0


def bump_today(scope: str, delta: int = 1) -> int:
    with _lock:
        db = _db()
        day = today(scope)
        db.execute(
            "INSERT INTO daycount(day, scope, n) VALUES(?,?,?)"
            " ON CONFLICT(day, scope) DO UPDATE SET n = n + ?",
            (day, scope, delta, delta),
        )
        db.commit()
        row = db.execute("SELECT n FROM daycount WHERE day=? AND scope=?", (day, scope)).fetchone()
    return int(row["n"]) if row else 0


# --------------------------------------------------------------------- 行为流水
def record_action(
    platform: str, kind: str, target: str, content: str, ok: bool = True
) -> None:
    """记一条"我实际干了什么"。这是历史页的数据来源，和对话记忆分开。"""
    with _lock:
        db = _db()
        db.execute(
            "INSERT INTO actions(platform, kind, target, content, ok, ts) VALUES(?,?,?,?,?,?)",
            (platform, kind, target, content, 1 if ok else 0, time.time()),
        )
        db.commit()


def actions(limit: int = 200, platform: str = "", kind: str = "", offset: int = 0) -> List[Dict[str, Any]]:
    """按平台 / 类型 / 偏移查行为流水。两个平台的数据在库里是分开存的。"""
    sql = "SELECT * FROM actions WHERE 1=1"
    args: List[Any] = []
    if platform:
        sql += " AND platform=?"
        args.append(platform)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args.extend([limit, offset])
    with _lock:
        rows = _db().execute(sql, args).fetchall()
    return [
        {
            "id": r["id"],
            "platform": r["platform"],
            "kind": r["kind"],
            "target": r["target"],
            "content": r["content"],
            "ok": bool(r["ok"]),
            "ts": r["ts"],
        }
        for r in rows
    ]


def action_stats(platform: str = "") -> Dict[str, Any]:
    """给历史页用的统计：总数、今日、按类型分布、最近一次。"""
    where, args = ("WHERE platform=?", (platform,)) if platform else ("", ())
    with _lock:
        db = _db()
        row = db.execute(f"SELECT COUNT(*) c, MAX(ts) t FROM actions {where}", args).fetchone()
        today = db.execute(
            f"SELECT COUNT(*) c FROM actions {where}{' AND' if where else ' WHERE'}"
            " date(ts,'unixepoch','localtime')=date('now','localtime')",
            args,
        ).fetchone()
        kinds = db.execute(
            f"SELECT kind, COUNT(*) c FROM actions {where} GROUP BY kind ORDER BY c DESC",
            args,
        ).fetchall()
    return {
        "total": int(row["c"] or 0),
        "today": int(today["c"] or 0),
        "last_ts": float(row["t"] or 0),
        "kinds": {r["kind"]: r["c"] for r in kinds},
    }


# --------------------------------------------------------------------- 统计
def stats() -> Dict[str, Any]:
    with _lock:
        db = _db()
        total = db.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
        by_plat = db.execute(
            "SELECT platform, COUNT(*) c FROM messages GROUP BY platform"
        ).fetchall()
        marks = db.execute("SELECT COUNT(*) c FROM marks").fetchone()["c"]
    return {
        "messages": total,
        "by_platform": {r["platform"]: r["c"] for r in by_plat},
        "marks": marks,
        "today_qq": used_today("qq:reply"),
        "today_bili": used_today("bili:comment"),
    }
