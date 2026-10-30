"""oai_db 访问层:全部走 `docker exec mysql mysql`(容器内 3306 未对外暴露)。"""
from __future__ import annotations

import pathlib
import re
import shlex
import subprocess
from typing import Iterable, Sequence

BASE = ["sudo", "docker", "exec", "-i", "mysql", "sh", "-c",
        'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" --batch --raw -N oai_db']
DUMP = ["sudo", "docker", "exec", "mysql", "sh", "-c"]

ERRORS: list[str] = []


class DBError(RuntimeError):
    pass


def _clean(text: str) -> str:
    """去掉 mysql 客户端的告警噪声（"Using a password on the command line" 之类），
    只留真正的错误行；全被过滤掉时退回原文。"""
    keep = [ln for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("mysql: [Warning]")]
    return ("\n".join(keep) or text.strip())[:600]


def _run(cmd: Sequence[str], stdin: str | None = None, timeout: int = 120) -> str:
    p = subprocess.run(cmd, input=(stdin.encode() if stdin is not None else None),
                       capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise DBError(_clean(p.stderr.decode(errors="replace") or p.stdout.decode(errors="replace")))
    return p.stdout.decode(errors="replace")


def sql(statement: str) -> str:
    """执行 SQL,返回原始 TSV 输出(无表头)。"""
    return _run(BASE, statement)


def rows(statement: str) -> list[list[str]]:
    out = sql(statement)
    return [ln.split("\t") for ln in out.splitlines() if ln]


def scalar(statement: str, default: str | None = None) -> str | None:
    r = rows(statement)
    return r[0][0] if r and r[0] else default


def json_one(statement: str) -> dict | None:
    import json
    v = scalar(statement)
    if v in (None, "", "NULL"):
        return None
    return json.loads(v)


def quote(v: str) -> str:
    """SQL 字符串字面量(输入只应来自校验过的 IMSI/数值)。"""
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def dump_rows(table: str, where: str) -> str:
    """mysqldump 单表 + 条件,产出可回放的 INSERT 语句。"""
    cmd = (f'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --no-create-info --skip-add-locks --skip-comments '
           f"--skip-extended-insert --compact oai_db {table} --where={shlex.quote(where)}")
    return _run(DUMP + [cmd], timeout=180)


def restore(sql_text: str) -> None:
    """执行 dump 出来的 SQL(用于回滚)。"""
    if sql_text.strip():
        _run(BASE, sql_text)


def table_columns(table: str) -> list[str]:
    return [r[0] for r in rows(f"SHOW COLUMNS FROM {table}")]


def exists(table: str, where: str) -> bool:
    return scalar(f"SELECT COUNT(*) FROM {table} WHERE {where}", "0") not in ("0", None)


def imsi_variants(imsi: str) -> list[str]:
    """库里 ueid 有时带 imsi- 前缀,有时纯数字。"""
    return [imsi, f"imsi-{imsi}"]


def resolve_ueid(imsi: str, tables: Iterable[str]) -> str | None:
    for t in tables:
        for v in imsi_variants(imsi):
            if exists(t, f"ueid={quote(v)}"):
                return v
    return None


UE_CONF = "/home/ubuntu/oai-ran-conf/ue.conf"


def active_ue_imsi() -> str | None:
    """当前活动 UE 的 IMSI。

    真值是 ue.conf 里的 `imsi = "..."` —— 进程实际用的就是它。
    以前只读 oran-ue.service 的 Description，但 `switch_ue` 换 UE 时只改 ue.conf、
    不会同步 Description，结果 active_ue_imsi() 会一直返回旧 IMSI：
    "对活动 UE 操作" 的动作（set_qos / sla_guard / reduce_latency）就会改错订户。
    所以先读 ue.conf，读不到再退回 Description。
    """
    try:
        in_conf = pathlib.Path(UE_CONF).read_text(encoding="utf-8", errors="replace")
    except OSError:
        in_conf = ""
    m = re.search(r'^\s*imsi\s*=\s*"(\d{15})"', in_conf, re.M)
    if m:
        return m.group(1)
    try:
        p = subprocess.run(["systemctl", "cat", "oran-ue.service"], capture_output=True, timeout=15)
        m2 = re.search(r"IMSI\s*(\d{15})", p.stdout.decode(errors="replace"))
        return m2.group(1) if m2 else None
    except Exception:
        return None
