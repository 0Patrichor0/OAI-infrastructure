"""意图结果持久化:把签约数据的"期望状态"落成可重放的 SQL。

问题:`start-all` 的 L3 会重建 mysql(匿名卷 + `database/oai_db2.sql` 初始化),
任何意图改过的签约都被种子数据覆盖。这里做两件事:

1. 账本侧:每次成功的意图把目标 IMSI 的当前 auth/sm 行 dump 下来,存到 `state/intent_state.json`
2. 环境侧:渲染成 `database/zz_intent_state.sql`(幂等:先按 ueid 删,再插),
   并挂到 mysql 的 init 目录(文件名 zz_ 开头,在 oai_db.sql 之后执行),
   同时由 `intent-state-apply.timer` 在 mysql 起来后兜底应用
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Dict, List

from .storage import atomic_text

from . import db
from .actions import AUTH_T, SM_T

HOME = os.environ.get("INTENT_HOME", "/home/ubuntu/intent-driven")
STATE = os.path.join(HOME, "state", "intent_state.json")
CN5G = "/home/ubuntu/nwdaf-rel17/oai-cn5g-fed/docker-compose"
RENDERED = os.path.join(CN5G, "database", "zz_intent_state.sql")
APPLIED = os.path.join(HOME, "state", "applied.sha")

HEADER = """-- 由 intentctl 自动生成,勿手改;意图结果持久化用
-- 幂等:每个 IMSI 先删后插,和 database/oai_db2.sql 一起被执行
SET NAMES utf8mb4;
"""


def _load() -> Dict[str, dict]:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save(data: Dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    atomic_text(STATE, json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))


def capture(imsi: str) -> bool:
    """记录该 IMSI 当前的签约/auth 状态;若库里已经没有了则删除记录。"""
    data = _load()
    auth = db.dump_rows(AUTH_T, f"ueid='{imsi}'")
    sm = db.dump_rows(SM_T, f"ueid='{imsi}'")
    if not auth.strip() and not sm.strip():
        data.pop(imsi, None)
        _save(data)
        render()
        return False
    # 半成品（有鉴权、没签约）说明这个 IMSI 根本建不起 PDU 会话，
    # 把它固化进持久层只会在每次重建后复制一个用不了的用户。
    if not sm.strip():
        print(f"[persist] {imsi} 缺 SessionManagementSubscriptionData，跳过持久化")
        data.pop(imsi, None)
        _save(data)
        render()
        return False
    data[imsi] = {"auth": auth, "sm": sm}
    _save(data)
    render()
    return True


def forget(imsi: str) -> None:
    data = _load()
    data.pop(imsi, None)
    _save(data)
    render()


def render() -> str:
    """生成 init 用的 SQL 文件,返回其 sha256。"""
    import hashlib
    data = _load()
    parts: List[str] = [HEADER]
    for imsi in sorted(data):
        row = data[imsi]
        variants = ",".join(db.quote(v) for v in db.imsi_variants(imsi))
        parts.append(f"\n-- {imsi}")
        parts.append(f"DELETE FROM {AUTH_T} WHERE ueid IN ({variants});")
        parts.append(f"DELETE FROM {SM_T} WHERE ueid IN ({variants});")
        if (row.get("auth") or "").strip():
            parts.append(row["auth"].strip())
        if (row.get("sm") or "").strip():
            parts.append(row["sm"].strip())
    text = "\n".join(parts) + "\n"
    os.makedirs(os.path.dirname(RENDERED), exist_ok=True)
    atomic_text(RENDERED, text, mode=0o644)
    digest = hashlib.sha256(text.encode()).hexdigest()
    atomic_text(RENDERED + ".sha", digest, mode=0o644)
    return digest


def apply_now() -> str:
    """立刻把渲染好的 SQL 应用到运行中的 mysql,并记录已应用 hash。"""
    digest = render()
    p = subprocess.run(db.BASE, stdin=open(RENDERED, "rb"),
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).decode(errors="replace")[:400])
    try:
        with open(APPLIED, "w", encoding="utf-8") as f:
            f.write(digest)
    except PermissionError:
        # 别的进程(root/systemd/UI)可能以 root 写过这个文件,退回 sudo
        subprocess.run(["sudo", "tee", APPLIED], input=digest.encode(),
                       capture_output=True)
    return digest


def state() -> Dict[str, dict]:
    return _load()
