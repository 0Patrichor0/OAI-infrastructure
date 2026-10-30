"""意图账本(sqlite)+ 审计流水(JSONL)。

账本记录"每个意图要做什么、做了什么、验证结果、快照在哪",
审计流水是 append-only 的,用来回放与追责。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List

HOME = os.environ.get("INTENT_HOME", "/home/ubuntu/intent-driven")
DB_PATH = os.path.join(HOME, "ledger.db")
AUDIT_PATH = os.path.join(HOME, "audit", "audit.jsonl")

SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  action TEXT NOT NULL,
  state TEXT NOT NULL,
  source TEXT,
  intent_json TEXT NOT NULL,
  plan_json TEXT,
  snapshot_json TEXT,
  exec_json TEXT,
  verify_json TEXT,
  error TEXT
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class Ledger:
    def __init__(self, path: str = DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- 账本 ----
    def create(self, intent: Dict[str, Any], plan: List[str], source: str) -> str:
        iid = time.strftime("%y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        n = _now()
        self.conn.execute(
            "INSERT INTO intents (id,created_at,updated_at,action,state,source,intent_json,plan_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (iid, n, n, intent.get("action", "?"), "planned", source,
             json.dumps(intent, ensure_ascii=False), json.dumps(plan, ensure_ascii=False)))
        self.conn.commit()
        return iid

    def update(self, iid: str, **kw) -> None:
        cols, vals = ["updated_at"], [_now()]
        for k, v in kw.items():
            cols.append(k)
            vals.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
        vals.append(iid)
        self.conn.execute(f"UPDATE intents SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?", vals)
        self.conn.commit()

    def get(self, iid: str) -> Dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM intents WHERE id=?", (iid,)).fetchone()
        return dict(r) if r else None

    def list(self, limit: int = 20) -> List[Dict[str, Any]]:
        rs = self.conn.execute("SELECT id,created_at,action,state,source,intent_json FROM intents "
                               "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rs:
            d = dict(r)
            intent = json.loads(d.pop("intent_json"))
            d["imsi"] = (intent.get("target") or {}).get("imsi")
            d["raw_text"] = intent.get("raw_text")
            out.append(d)
        return out

    # ---- 审计 ----
    def audit(self, phase: str, iid: str | None, payload: Dict[str, Any]) -> None:
        rec = {"ts": _now(), "phase": phase, "intent_id": iid, **payload}
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def audit_tail(self, n: int = 10) -> List[Dict[str, Any]]:
        if not os.path.exists(AUDIT_PATH):
            return []
        with open(AUDIT_PATH, encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
