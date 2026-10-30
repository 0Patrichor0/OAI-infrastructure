#!/usr/bin/env python3
"""intent-state-apply: 把意图持久层渲染出的 SQL 应用到运行中的 mysql。

为什么需要它：compose 的 init 脚本**只在数据目录为空时**执行，
容器重启（卷还在）不会重跑；而 L3 全量重建会新建匿名卷、只用种子 SQL 初始化。
所以除了挂 init 脚本，还必须有这个定时兜底。

幂等：hash 没变就跳过；SQL 本身是「先按 ueid 删、再插」，可重复执行。
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.environ.get("INTENT_HOME", "/home/ubuntu/intent-driven"))

from intentlib import persist, persist_cfg, operation  # noqa: E402


def mysql_ready() -> bool:
    p = subprocess.run(["docker", "inspect", "-f", "{{.State.Health.Status}}", "mysql"],
                       capture_output=True, text=True)
    return p.returncode == 0 and p.stdout.strip() == "healthy"


def _mysql_started_epoch() -> int:
    p = subprocess.run(["docker", "inspect", "-f", "{{.State.StartedAt}}", "mysql"],
                       capture_output=True, text=True)
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(p.stdout.strip().replace("Z", "+00:00")).timestamp())
    except Exception:  # noqa: BLE001
        return 0


def _missing_imsis() -> list[str]:
    """持久层里记录、但库里已经没有的 IMSI。

    只看 hash 会漏掉最关键的场景:L3 全量重建后 zz 文件没变、applied.sha 还在,
    但 mysql 已用种子重init,意图结果全丢。
    """
    from intentlib import db
    from intentlib.actions import AUTH_T, SM_T

    imsis = sorted(persist.state().keys())
    if not imsis:
        return []
    in_list = ",".join(db.quote(i) for i in imsis)
    missing: list[str] = []
    for tbl in (AUTH_T, SM_T):
        try:
            found = {r[0] for r in db.rows(f"SELECT ueid FROM {tbl} WHERE ueid IN ({in_list});")}
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"无法核对 {tbl} 的持久化签约: {exc}") from exc
        missing += [i for i in imsis if i not in found]
    return sorted(set(missing))


def main() -> int:
    hold = os.path.join(os.environ.get("INTENT_HOME", "/home/ubuntu/intent-driven"),
                        "state", "reconcile.hold")
    if os.path.exists(hold):
        print("intentctl 正在执行意图(reconcile.hold),本轮跳过")
        return 0
    digest = persist.render()
    try:
        with open(persist.APPLIED, encoding="utf-8") as f:
            applied = f.read().strip()
    except OSError:
        applied = ""

    reason = ""
    if digest != applied:
        reason = "文件变化"
    else:
        try:
            amtime = os.path.getmtime(persist.APPLIED)
        except OSError:
            amtime = 0
        if _mysql_started_epoch() > amtime:
            reason = "mysql 容器重建过（可能被种子重置）"
        else:
            missing = _missing_imsis()
            if missing:
                reason = f"库里缺记录: {', '.join(missing)}"

    if not reason:
        print(f"unchanged ({digest[:12]})")
        return 0
    if not os.path.getsize(persist.RENDERED) > len(persist.HEADER):
        print("渲染文件里没有 IMSI，跳过")
        return 0
    if not mysql_ready():
        print("mysql 未就绪，等下一轮")
        return 0
    try:
        persist.apply_now()
    except Exception as exc:  # noqa: BLE001
        print(f"apply failed: {exc}")
        return 1
    print(f"applied {digest[:12]}（原因: {reason}）")
    return 0


def apply_config() -> int:
    """把 compose env / 辅助容器按回意图记录的期望值（与数据库层独立）。"""
    st = persist_cfg.state()
    if not any(st.get(k) for k in ("compose_env", "containers", "files", "core_slices", "ue_nodes")):
        return 0
    try:
        changes = persist_cfg.apply(verbose=True)
        if any(any(term in row["action"] for term in ("失败", "文件不存在", "找不到目标")) for row in changes):
            return 1
        if any("需重启" in row["action"] or "需要重建" in row["action"] for row in changes):
            print("期望配置已同步，但部分运行态仍需重启或重建后生效")
            return 2
    except Exception as exc:  # noqa: BLE001
        print(f"config apply failed: {exc}")
        return 1
    return 0


def run(wait=False):
    try:
        with operation.exclusive(wait=wait):
            if operation.legacy_hold_active():
                print("意图执行中，本轮签约和配置同步均跳过")
                return 1 if wait else 0
            rc = main()
            return rc if rc else apply_config()
    except operation.BusyError as exc:
        print(str(exc))
        return 1 if wait else 0
    except Exception as exc:
        print(f"同步失败: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(run(wait="--wait" in sys.argv))
