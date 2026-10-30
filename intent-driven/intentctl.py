#!/usr/bin/env python3
"""intentctl —— 意图驱动 CLI

  intentctl parse "<自然语言>"            只解析,不执行(打印结构化意图,便于审计)
  intentctl apply --nl "<自然语言>"       解析 → 计划 → 执行 → 验证
  intentctl apply --file intent.json
  intentctl list / show <id> / verify <id> / rollback <id> / replay <id>

示例:
  ./intentctl parse "给新用户 208950000000099 开通,5QI 9,上下行各 50Mbps"
  ./intentctl apply --nl "把 208950000000031 的 5QI 改成 9,上下行各 200Mbps" --yes
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from intentlib import actions, db, nl, ue          # noqa: E402
from intentlib import actions_more                   # noqa: E402,F401  注册第二批动作
from intentlib import actions_slice                  # noqa: E402,F401  注册切片/UE 动作
from intentlib import actions_prb                    # 注册切片 PRB E2 控制
from intentlib import actions_ran                    # 注册 E2 DU 控制
from intentlib import actions_ue                     # noqa: E402,F401  注册 add_ue（新增 UE 节点）
from intentlib import persist, operation, preflight, effects  # noqa: E402

HOLD = os.path.join(os.environ.get("INTENT_HOME", "/home/ubuntu/intent-driven"),
                    "state", "reconcile.hold")


def _hold_reconcile(on: bool) -> None:
    """执行意图时挂住持久层对账,避免它在执行中途把旧签约写回来(实测竞态)。"""
    try:
        if on:
            os.makedirs(os.path.dirname(HOLD), exist_ok=True)
            open(HOLD, "w").write(str(os.getpid()))
        elif os.path.exists(HOLD):
            os.remove(HOLD)
    except OSError:
        pass
from intentlib import persist_cfg                    # noqa: E402
from intentlib.ledger import Ledger                  # noqa: E402

C = {
    "ok": "\033[32m", "bad": "\033[31m", "warn": "\033[33m",
    "dim": "\033[2m", "b": "\033[1m", "off": "\033[0m",
}


def c(tag: str, s: str) -> str:
    return f"{C.get(tag, '')}{s}{C['off']}"


def _print_intent(intent: dict) -> None:
    p = intent.get("parser", {})
    print(c("b", "结构化意图"))
    print(f"  action      : {intent['action']}")
    print(f"  target.imsi : {(intent.get('target') or {}).get('imsi')}")
    print(f"  params      : {json.dumps(intent.get('params', {}), ensure_ascii=False)}")
    print(f"  constraints : {json.dumps(intent.get('constraints', {}), ensure_ascii=False)}")
    print(f"  parser      : {p.get('type')} / {p.get('model')} / 置信度 {p.get('confidence')}")
    if intent.get("rationale"):
        print(f"  依据        : {intent['rationale']}")
    for w in intent.get("warnings") or []:
        print(c("warn", f"  ⚠ {w}"))
    if intent.get("raw_text"):
        print(c("dim", f"  原文        : {intent['raw_text']}"))


def _print_plan(steps: list[str]) -> None:
    print(c("b", "执行计划"))
    for i, s in enumerate(steps, 1):
        print(f"  {i}. {s}")


def _print_checks(checks: list[dict]) -> bool:
    print(c("b", "验证结果"))
    allok = True
    for ch in checks:
        mark = c("ok", "✓") if ch.get("ok") else c("bad", "✗")
        allok &= bool(ch.get("ok"))
        detail = f"  {c('dim', ch['detail'])}" if ch.get("detail") else ""
        print(f"  {mark} {ch['check']}{detail}")
    return allok



PERSIST_ACTIONS = {"add_subscriber", "set_qos", "provision_service", "reduce_latency",
                   "sla_guard", "activate_slice", "switch_ue", "add_ue"}


def _persist_snapshot(intent: dict) -> None:
    """把受影响 IMSI 的当前签约写进 state/intent_state.json,并渲染成 init 用 SQL。"""
    imsi = (intent.get("target") or {}).get("imsi")
    if not imsi or intent.get("action") not in PERSIST_ACTIONS:
        return
    try:
        kept = persist.capture(imsi)
        print(c("dim", f"已记录 {imsi} 的期望状态到意图持久层({'有' if kept else '已清除'})"))
    except Exception as e:  # noqa: BLE001
        raise actions.IntentError(f"运行态可能已改变，但保存签约期望状态失败: {e}") from e


def _persist_config(intent: dict, iid: str | None = None) -> None:
    """记录这条动作改过的非签约类配置（compose env / 辅助容器），供重建后按回。"""
    act = actions.get(intent.get("action"))
    items = act.config_footprint(intent) if act else []
    if not items:
        return
    for it in items:
        if not it.get("value") and it.get("kind") == "compose_env":
            continue
        if it["kind"] == "compose_env":
            persist_cfg.record_env(it["key"], it["value"], it.get("service", ""), iid)
            print(c("dim", f"已记录配置期望值 {it['key']}={it['value']}"))
        elif it["kind"] == "container":
            ok = persist_cfg.record_container(it["name"], iid)
            print(c("dim", f"已记录容器规格 {it['name']}" if ok else f"容器 {it['name']} 不存在，未记录"))
        elif it["kind"] == "ue_node":
            persist_cfg.record_ue_node(it["index"], it["imsi"], it["sst"], it["sd"], iid)
            print(c("dim", f"已记录 UE 节点 #{it['index']}（{it['imsi']}）"))
        elif it["kind"] == "core_slice":
            persist_cfg.record_core_slice(it["sst"], it["sd"], iid)
            print(c("dim", f"已记录 core 切片 {it['sst']}/{it['sd']}"))
        elif it["kind"] == "file_key":
            ok = persist_cfg.record_file(it["file"], it["pattern"], iid,
                                         it.get("label", ""), it.get("service", ""))
            print(c("dim", f"已记录 {it.get('label') or it['file']}" if ok else
                    f"{it.get('label') or it['file']} 找不到目标，未记录"))


def _drop_config(intent: dict, iid: str | None = None) -> None:
    """回滚后放弃这些配置期望值（值已被恢复成旧值）。"""
    act = actions.get(intent.get("action"))
    for it in (act.config_footprint(intent) if act else []):
        if it["kind"] == "compose_env":
            persist_cfg.forget_env(it["key"])
        elif it["kind"] == "container":
            persist_cfg.forget_container(it["name"])
        elif it["kind"] == "file_key":
            persist_cfg.forget_file(it["file"], it["pattern"])
        elif it["kind"] == "core_slice":
            persist_cfg.forget_core_slice(it["sst"], it["sd"])
        elif it["kind"] == "ue_node":
            persist_cfg.forget_ue_node(it["index"])


def cmd_persist(args) -> int:
    if getattr(args, "record_id", None):
        rec = Ledger().get(args.record_id)
        if not rec:
            raise SystemExit("没有这个意图")
        _persist_config(json.loads(rec["intent_json"]), args.record_id)
    st = persist.state()
    print(c("b", f"已记录的 IMSI({len(st)} 个)"))
    for imsi in sorted(st):
        row = st[imsi]
        print(f"  {imsi}: auth {'✓' if row.get('auth', '').strip() else '✗'} / "
              f"sm {'✓' if row.get('sm', '').strip() else '✗'}")
    digest = persist.render()
    print(c("dim", f"渲染: {persist.RENDERED}  sha256={digest[:12]}"))

    cfg = persist_cfg.state()
    envs, conts = cfg.get("compose_env", {}), cfg.get("containers", {})
    print(c("b", f"配置期望值({len(envs)} 项 env / {len(conts)} 个容器)"))
    for k, v in sorted(envs.items()):
        cur = persist_cfg.compose_env_get(k)
        mark = "✓" if cur == v["value"] else f"✗ 当前 {cur}"
        print(f"  {k}={v['value']} ({v.get('service') or '-'})  {mark}")
    for name in sorted(conts):
        print(f"  容器 {name}: {'存在' if persist_cfg._container_exists(name) else '缺失（apply 会重建）'}")
    files = cfg.get("files", {})
    if files:
        print(c("b", f"配置文件期望值({len(files)} 项)"))
        for rec in sorted(files.values(), key=lambda r: r.get("label", "")):
            try:
                m = re.search(rec["pattern"], open(rec["file"], encoding="utf-8", errors="replace").read())
                cur = m.group(1) if m else None
            except OSError:
                cur = None
            mark = "✓" if cur == rec["value"] else f"✗ 当前 {cur}"
            print(f"  {rec['label']} = {rec['value']}  {mark}")
    if args.apply:
        print(c("ok", f"已应用到 mysql: {persist.apply_now()[:12]}"))
    if getattr(args, "apply_config", False):
        for d in persist_cfg.apply(verbose=False):
            print(c("dim", f"  {d['item']}: {d['action']}"))
    return 0


def cmd_parse(args) -> int:
    intent = nl.parse(args.text, use_llm=not args.no_llm)
    _print_intent(intent)
    if args.json:
        print(json.dumps(intent, ensure_ascii=False, indent=2))
    return 0


def _load_intent(args) -> tuple[dict, str]:
    if args.nl:
        return nl.parse(args.nl, use_llm=not args.no_llm), "nl"
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            return json.load(f), "file"
    if args.id:
        led = Ledger()
        rec = led.get(args.id)
        if not rec:
            raise SystemExit(f"账本里没有意图 {args.id}")
        return json.loads(rec["intent_json"]), "ledger"
    raise SystemExit("需要 --nl / --file / --id 之一")


def cmd_apply(args) -> int:
    intent, source = _load_intent(args)
    _print_intent(intent)
    expected = None
    if getattr(args, 'preflight', None):
        with open(args.preflight, encoding='utf-8') as stream:
            expected = json.load(stream)
    preview = preflight.enforce(intent, expected)
    intent = preview['intent']
    act = actions.get(intent["action"])
    plan = preview['plan']
    print()
    _print_plan(plan)

    led = Ledger()
    iid = led.create(intent, plan, source)
    led.audit("parsed", iid, {"intent": intent, "plan": plan})
    print(c("dim", f"\n意图 ID: {iid}"))

    if args.dry_run:
        print(c("warn", "\n--dry-run:未执行任何变更"))
        led.update(iid, state="planned")
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("非交互环境必须加 --yes")
        ans = input(f"\n执行上述 {len(plan)} 步变更? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消")
            led.update(iid, state="cancelled")
            return 1

    observation = {'preflight':{k:v for k,v in preview.items() if k not in ('ran_state','intent')},
                   'before':None,'after':None}
    res = {}
    try:
        _hold_reconcile(True)
        observation['before'] = effects.safe_capture(intent, preview.get('ran_state'))
        if intent['action'] in ('set_ran_dl_mcs','set_ran_slice_prb'):
            # The global operation lock remains held; use the just-read DU state.
            before = observation['before'].get('ran') or preview.get('ran_state')
            snap = act.snapshot(intent,before)
        else:
            snap = act.snapshot(intent)
        snap["_intent_config_before"] = persist_cfg.state()
        led.update(iid, state="executing", snapshot_json=snap)
        led.audit("snapshot", iid, {"snapshot": snap})
        res = act.execute(intent)
        res['_observability'] = observation
        # 先把"执行后的期望状态"落进持久层,再做验证:
        # 否则对账万一在验证前后跑一轮,会用旧快照覆盖本次变更。
        led.update(iid, exec_json=res)
        led.audit("executed", iid, {"result": res})
        _persist_snapshot(intent)
        # 配置类变更在 execute 之后就落到磁盘/容器上了，即使后面 verify 不过也必须记下来，
        # 否则「改了但没记账」——重建后要么丢配置，要么没人知道该恢复成什么。
        _persist_config(intent, iid)
        checks = act.verify(intent, res)
        observation['after'] = effects.safe_capture(intent, (res.get('_verification_read') or {}).get('state') or res.get('state'))
        led.update(iid, exec_json=res)
        ok = _print_checks(checks)
        fields = dict(state="verified" if ok else "failed", verify_json=checks, error="")
        if not ok:
            bad = [f"{c.get('check')}: {str(c.get('detail'))[:60]}"
                   for c in checks if not c.get("ok")]
            fields["error"] = ("verify failed -> " + "; ".join(bad))[:500]
        led.update(iid, **fields)
        _persist_snapshot(intent)
        led.audit("verified", iid, {"ok": ok, "checks": checks})
        print(c("ok" if ok else "bad", f"\n{'意图达成' if ok else '意图未完全达成'} — {iid}"))
        return 0 if ok else 1
    except Exception as e:  # noqa: BLE001
        observation['after'] = effects.safe_capture(intent, res.get('state'))
        res['_observability'] = observation
        led.update(iid, exec_json=res)
        led.update(iid, state="failed", error=f"{type(e).__name__}: {e}")
        led.audit("error", iid, {"error": f"{type(e).__name__}: {e}"})
        print(c("bad", f"\n执行失败: {e}"))
        print(c("dim", f"可用 `intentctl rollback {iid}` 回滚已落下的变更"))
        return 1
    finally:
        _hold_reconcile(False)


def cmd_list(args) -> int:
    for r in Ledger().list(args.limit):
        state = r["state"]
        color = "ok" if state == "verified" else ("bad" if state == "failed" else "warn")
        print(f"{r['id']}  {r['created_at'][:19]}  {c(color, state):<10}  "
              f"{r['action']:<15} imsi={r['imsi']}  {c('dim', (r['raw_text'] or '')[:50])}")
    return 0


def cmd_show(args) -> int:
    rec = Ledger().get(args.id)
    if not rec:
        raise SystemExit("没有这个意图")
    intent = json.loads(rec["intent_json"])
    _print_intent(intent)
    print(f"\n状态: {rec['state']}  来源: {rec['source']}  创建: {rec['created_at']}")
    if rec["plan_json"]:
        print()
        _print_plan(json.loads(rec["plan_json"]))
    if rec["exec_json"]:
        print(f"\n执行结果: {rec['exec_json']}")
    if rec["verify_json"]:
        print()
        _print_checks(json.loads(rec["verify_json"]))
    if rec["error"]:
        print(c("bad", f"\n错误: {rec['error']}"))
    return 0


def cmd_verify(args) -> int:
    led = Ledger()
    rec = led.get(args.id)
    if not rec:
        raise SystemExit("没有这个意图")
    if not rec["exec_json"]:
        raise SystemExit("这个意图还没有执行记录")
    intent = json.loads(rec["intent_json"])
    act = actions.get(intent["action"])
    checks = act.verify(intent, json.loads(rec["exec_json"]))
    ok = _print_checks(checks)
    fields = dict(state="verified" if ok else "failed", verify_json=checks, error="")
    if not ok:
        bad = [f"{c.get('check')}: {str(c.get('detail'))[:60]}" for c in checks if not c.get("ok")]
        fields["error"] = ("verify failed -> " + "; ".join(bad))[:500]
    led.update(args.id, **fields)
    led.audit("re-verified", args.id, {"ok": ok, "checks": checks})
    return 0 if ok else 1


def cmd_rollback(args) -> int:
    led = Ledger()
    rec = led.get(args.id)
    if not rec:
        raise SystemExit("没有这个意图")
    if not rec["snapshot_json"]:
        raise SystemExit("没有快照,无法回滚")
    intent = json.loads(rec["intent_json"])
    act = actions.get(intent["action"])
    if rec.get("state") == "rolled_back":
        print("该意图已经回滚")
        return 0
    snap = json.loads(rec["snapshot_json"])
    items = act.config_footprint(intent)
    try:
        persist_cfg.assert_current_owner(items, args.id)
    except persist_cfg.CfgError as exc:
        raise actions.IntentError(str(exc)) from exc
    _hold_reconcile(True)
    try:
        msg = act.rollback(intent, snap)
        _persist_snapshot(intent)
        if "_intent_config_before" in snap:
            persist_cfg.restore_footprint(items, snap["_intent_config_before"])
        else:
            _drop_config(intent, args.id)
    except Exception as exc:
        led.update(args.id, state="failed", error=f"rollback failed: {type(exc).__name__}: {exc}")
        led.audit("rollback-error", args.id, {"error": str(exc)})
        raise
    finally:
        _hold_reconcile(False)
    led.update(args.id, state="rolled_back", error="")
    led.audit("rolled_back", args.id, {"message": msg})
    print(c("ok", msg))
    return 0


def cmd_replay(args) -> int:
    """用账本里的结构化意图重新执行(审计/回放用,不再走 NL 解析)。"""
    rec = Ledger().get(args.id)
    if not rec:
        raise SystemExit("没有这个意图")
    tmp = os.path.join("/tmp", f"intent-{args.id}.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(json.loads(rec["intent_json"]), f, ensure_ascii=False)
    print(c("dim", f"回放 {args.id} → {tmp}"))
    return cmd_apply(argparse.Namespace(nl=None, file=tmp, id=None, no_llm=True,
                                        dry_run=args.dry_run, yes=args.yes))


def main() -> int:
    ap = argparse.ArgumentParser(prog="intentctl", description="意图驱动控制面")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="只解析自然语言,不执行")
    p.add_argument("text")
    p.add_argument("--no-llm", action="store_true", help="强制用规则解析")
    p.add_argument("--json", action="store_true", help="附完整 JSON")
    p.set_defaults(func=cmd_parse)

    a = sub.add_parser("apply", help="解析并执行意图")
    a.add_argument("--nl")
    a.add_argument("--file")
    a.add_argument("--id", help="用账本里已有的结构化意图")
    a.add_argument("--no-llm", action="store_true")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--yes", action="store_true")
    a.add_argument("--preflight", help="由 UI 保存的预览指纹；执行前在全局锁内重查")
    a.set_defaults(func=cmd_apply)

    l = sub.add_parser("list", help="最近的意图")
    l.add_argument("--limit", type=int, default=20)
    l.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="看单个意图")
    s.add_argument("id")
    s.set_defaults(func=cmd_show)

    v = sub.add_parser("verify", help="重新验证")
    v.add_argument("id")
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("rollback", help="回滚")
    r.add_argument("id")
    r.set_defaults(func=cmd_rollback)

    rp = sub.add_parser("replay", help="按账本回放")
    rp.add_argument("id")
    rp.add_argument("--dry-run", action="store_true")
    rp.add_argument("--yes", action="store_true")
    rp.set_defaults(func=cmd_replay)

    pe = sub.add_parser("persist", help="查看/应用意图持久层(种子 SQL)")
    pe.add_argument("--apply", action="store_true", help="立刻写入 mysql")
    pe.add_argument("--apply-config", action="store_true", help="把 compose env / 辅助容器按回期望值")
    pe.add_argument("--record-id", metavar="ID",
                    help="按账本里某条已有意图补记配置足迹（给新加的 footprint 做回填）")
    pe.set_defaults(func=cmd_persist)

    args = ap.parse_args()
    try:
        if args.cmd in ("apply", "rollback", "replay", "verify", "persist"):
            with operation.exclusive():
                return args.func(args)
        return args.func(args)
    except (db.DBError, actions.IntentError, operation.BusyError) as e:
        print(c("bad", f"失败: {e}"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
