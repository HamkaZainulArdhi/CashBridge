"""Tamper-evident audit log: append-only SHA-256 hash chain.

Each entry stores ``prev_hash`` and ``hash``, where
``hash = sha256(prev_hash | ts | actor | action | entity | payload)``.
``verify_chain`` recomputes every hash, so any edit or deletion breaks the chain
and points at the first broken entry.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

GENESIS_HASH = "0" * 64


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_hash(prev_hash: str, ts: str, actor: str, action: str,
                  entity_type: Optional[str], entity_id: Optional[str],
                  payload_json: str) -> str:
    body = "|".join([prev_hash, ts, actor, action,
                     entity_type or "", entity_id or "", payload_json])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def append(conn, actor: str, action: str, entity_type: Optional[str] = None,
           entity_id: Optional[str] = None, payload: Optional[dict] = None) -> dict:
    """Append one entry to the chain. Returns the stored entry."""
    ts = _utc_now()
    payload_json = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
    last = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = last["hash"] if last else GENESIS_HASH
    entry_hash = _compute_hash(prev_hash, ts, actor, action, entity_type,
                               entity_id, payload_json)
    cur = conn.execute(
        """INSERT INTO audit_log (ts, actor, action, entity_type, entity_id, payload, prev_hash, hash)
           VALUES (?,?,?,?,?,?,?,?)""",
        (ts, actor, action, entity_type, entity_id, payload_json, prev_hash, entry_hash),
    )
    conn.commit()
    return {"id": cur.lastrowid, "ts": ts, "actor": actor, "action": action,
            "prev_hash": prev_hash, "hash": entry_hash}


def verify_chain(conn) -> dict:
    """Recompute the whole chain. Returns {ok, entries, broken_at?}."""
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    prev_hash = GENESIS_HASH
    for row in rows:
        expected = _compute_hash(prev_hash, row["ts"], row["actor"], row["action"],
                                 row["entity_type"], row["entity_id"], row["payload"])
        if row["prev_hash"] != prev_hash or row["hash"] != expected:
            return {"ok": False, "entries": len(rows), "broken_at": row["id"],
                    "detail": f"Entry {row['id']} does not match its hash chain."}
        prev_hash = row["hash"]
    return {"ok": True, "entries": len(rows), "broken_at": None}


def recent(conn, limit: int = 50) -> list:
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
