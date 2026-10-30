"""意图动作:每个动作都要能 plan / snapshot / execute / verify / rollback。

新增动作时只要实现这四个方法并注册进 ACTIONS,CLI 与账本不用改。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from . import db, ue

AUTH_T = "AuthenticationSubscription"
SM_T = "SessionManagementSubscriptionData"
DEFAULT_TEMPLATE = "208950000000031"   # 本环境里能正常附着的那份订户数据
SM_DATA = "dnnConfigurations"

# MySQL JSON path:以数字开头的键(5gQosProfile/5qi)必须用双引号包起来,否则 ERROR 3143
Q5QI = "'$.\"default\".\"5gQosProfile\".\"5qi\"'"
QARP = "'$.\"default\".\"5gQosProfile\".\"arp\".\"priorityLevel\"'"
QUL  = "'$.\"default\".\"sessionAmbr\".\"uplink\"'"
QDL  = "'$.\"default\".\"sessionAmbr\".\"downlink\"'"
QIP  = "'$.\"default\".\"staticIpAddress\"'"


class IntentError(RuntimeError):
    pass


class Action:
    name = ""
    description = ""

    def config_footprint(self, intent: Dict[str, Any]) -> List[Dict[str, Any]]:
        """这条动作改过的非签约类配置（compose env / 辅助容器）。
        返回空表示只动数据库，由 persist.py 的 SQL 重放覆盖。"""
        return []

    def validate(self, intent: Dict[str, Any]) -> None:
        raise NotImplementedError

    def plan(self, intent: Dict[str, Any]) -> List[str]:
        raise NotImplementedError

    def snapshot(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def execute(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def verify(self, intent: Dict[str, Any], exec_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def rollback(self, intent: Dict[str, Any], snapshot: Dict[str, Any]) -> str:
        raise NotImplementedError

    # ---- 公共工具 ----
    @staticmethod
    def _imsi(intent: Dict[str, Any]) -> str:
        imsi = (intent.get("target") or {}).get("imsi")
        if not imsi or not re.fullmatch(r"\d{15}", imsi):
            raise IntentError("意图缺少合法 IMSI(15 位数字),拒绝执行")
        return imsi


def _sm_json(ueid: str) -> dict | None:
    return db.json_one(f"SELECT {SM_DATA} FROM {SM_T} WHERE ueid={db.quote(ueid)}")


def _extract(sm: dict) -> Dict[str, Any]:
    d = (sm or {}).get("default", {})
    qos = d.get("5gQosProfile", {})
    ambr = d.get("sessionAmbr", {})
    ips = d.get("staticIpAddress") or []
    return {
        "fiveQI": qos.get("5qi"),
        "ambr_ul": ambr.get("uplink"),
        "ambr_dl": ambr.get("downlink"),
        "arp_priority": qos.get("arp", {}).get("priorityLevel"),
        "static_ip": ips[0].get("ipv4Addr") if ips else None,
    }


class AddSubscriber(Action):
    name = "add_subscriber"
    description = "新增用户:收敛到「有鉴权 + 有签约」的期望状态;可只补缺失部分"

    def __init__(self):
        self._conflict_tables: list[str] = []

    def validate(self, intent):
        imsi = self._imsi(intent)
        p = intent.setdefault("params", {})
        tpl = p.get("template_imsi") or DEFAULT_TEMPLATE
        p["template_imsi"] = tpl
        for t in (AUTH_T, SM_T):
            # A complete subscriber never needs a clone template.
            if db.resolve_ueid(imsi, [t]):
                continue
            if not db.exists(t, f"ueid={db.quote(tpl)}"):
                raise IntentError(f"模板订户 {tpl} 在 {t} 里不存在,无法补齐 {imsi}")
        if re.fullmatch(r"\d{15}", tpl) is None:
            raise IntentError("template_imsi 必须是 15 位数字")
        if p.get("fiveQI") is not None and not (1 <= int(p["fiveQI"]) <= 255):
            raise IntentError("fiveQI 超出范围")
        for k in ("ambr_ul", "ambr_dl"):
            if p.get(k) and not re.fullmatch(r"\d+(\.\d+)?(Mbps|Gbps)", str(p[k])):
                raise IntentError(f"{k} 格式应为 50Mbps 这种")
        if p.get("static_ip") and not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", p["static_ip"]):
            raise IntentError("static_ip 不是合法 IPv4")
        if p.get("sd") and not re.fullmatch(r"[0-9a-fA-F]{6}", p["sd"]):
            raise IntentError("sd 必须是 6 位 hex")

        mode = p.setdefault("mode", "auto")
        if mode not in ("auto", "create", "ensure"):
            raise IntentError("mode 只支持 auto / create / ensure")
        have = [t for t in (AUTH_T, SM_T) for v in db.imsi_variants(imsi) if db.exists(t, f"ueid={db.quote(v)}")]
        self._conflict_tables = sorted(set(have))
        if mode == "create" and self._conflict_tables:
            raise IntentError(f"{imsi} 已存在于 {', '.join(self._conflict_tables)};"
                              f"要收敛到期望状态请用 mode=ensure")
        if mode == "auto":
            # 已在库里 → 视为"补齐/收敛",不报错(意图应幂等)
            p["mode"] = "ensure" if self._conflict_tables else "create"

    def plan(self, intent):
        imsi = self._imsi(intent)
        p = intent["params"]
        mode = p["mode"]
        steps = [f"识别 {imsi} 现状(鉴权/签约是否已存在)"]
        if mode == "create":
            steps.append(f"克隆模板 {p['template_imsi']} 的鉴权密钥与签约结构 → ueid={imsi}")
        else:
            steps.append(f"{imsi} 已有部分数据 → 只补缺失的表,再按意图覆盖字段")
        steps.append("写入 SessionManagementSubscriptionData(S-NSSAI / 5QI / AMBR / 静态地址按意图覆盖)")
        steps.append("回读校验:鉴权行存在 + 签约 JSON 与意图一致")
        if p.get("static_ip"):
            steps.insert(2, f"静态地址 {p['static_ip']}(注意:与现有 UE 冲突会导致会话建立失败)")
        return steps

    def snapshot(self, intent):
        imsi = self._imsi(intent)
        snap = {"kind": "create", "imsi": imsi, "tables": [AUTH_T, SM_T]}
        for t, key in ((AUTH_T, "have_auth"), (SM_T, "have_sm")):
            present = any(db.exists(t, f"ueid={db.quote(v)}") for v in db.imsi_variants(imsi))
            snap[key] = present
            snap[f"{key}_dump"] = db.dump_rows(t, f"ueid='{imsi}'") if present else ""
        return snap

    def execute(self, intent):
        imsi = self._imsi(intent)
        p = intent["params"]
        tpl = p["template_imsi"]
        created = []
        for t in (AUTH_T, SM_T):
            if any(db.exists(t, f"ueid={db.quote(v)}") for v in db.imsi_variants(imsi)):
                continue
            dump = db.dump_rows(t, f"ueid='{tpl}'")
            if not dump.strip():
                raise IntentError(f"模板 {tpl} 在 {t} 无数据")
            db.restore(dump.replace(tpl, imsi))
            created.append(t)

        ueid = db.resolve_ueid(imsi, [SM_T])
        if not ueid:
            raise IntentError("签约数据写入后仍读不到,中止")

        sets = []
        if p.get("fiveQI") is not None:
            sets.append(f"{Q5QI}, {int(p['fiveQI'])}")
        if p.get("arp_priority") is not None:
            sets.append(f"{QARP}, {int(p['arp_priority'])}")
        if p.get("ambr_ul"):
            sets.append(f"{QUL}, {db.quote(p['ambr_ul'])}")
        if p.get("ambr_dl"):
            sets.append(f"{QDL}, {db.quote(p['ambr_dl'])}")
        if p.get("static_ip"):
            sets.append(f"{QIP}, CAST({db.quote(json.dumps([{'ipv4Addr': p['static_ip']}]))} AS JSON)")
        if sets:
            db.sql(f"UPDATE {SM_T} SET {SM_DATA}=JSON_SET({SM_DATA}, {', '.join(sets)}) WHERE ueid={db.quote(ueid)}")

        if p.get("sst") is not None or p.get("sd"):
            sn = []
            if p.get("sst") is not None:
                sn.append(f"'$.sst', {int(p['sst'])}")
            if p.get("sd"):
                sn.append(f"'$.sd', {db.quote(p['sd'])}")
            db.sql(f"UPDATE {SM_T} SET singleNssai=JSON_SET(singleNssai, {', '.join(sn)}) WHERE ueid={db.quote(ueid)}")

        return {"imsi": imsi, "ueid": ueid, "cloned_from": tpl, "created_tables": created, "mode": p["mode"]}

    def verify(self, intent, exec_result):
        imsi = exec_result["imsi"]
        ueid = exec_result.get("ueid") or imsi
        p = intent["params"]
        checks = []
        checks.append({"check": f"{AUTH_T} 有 {imsi} 的鉴权数据",
                       "ok": db.exists(AUTH_T, f"ueid={db.quote(imsi)}"),
                       "detail": "本次克隆" if AUTH_T in (exec_result.get("created_tables") or []) else "复用原有"})
        sm = _sm_json(ueid)
        checks.append({"check": f"{SM_T} 有 {imsi} 的签约数据", "ok": sm is not None})
        if sm:
            got = _extract(sm)
            if p.get("fiveQI") is not None:
                checks.append({"check": f"5QI = {p['fiveQI']}", "ok": int(got["fiveQI"] or -1) == int(p["fiveQI"]),
                               "detail": f"实际 {got['fiveQI']}"})
            if p.get("ambr_ul"):
                checks.append({"check": f"上行 AMBR = {p['ambr_ul']}", "ok": got["ambr_ul"] == p["ambr_ul"],
                               "detail": f"实际 {got['ambr_ul']}"})
            if p.get("ambr_dl"):
                checks.append({"check": f"下行 AMBR = {p['ambr_dl']}", "ok": got["ambr_dl"] == p["ambr_dl"],
                               "detail": f"实际 {got['ambr_dl']}"})
            if p.get("static_ip"):
                checks.append({"check": f"静态地址 = {p['static_ip']}", "ok": got["static_ip"] == p["static_ip"],
                               "detail": f"实际 {got['static_ip']}"})
            if p.get("sst") is not None:
                nssai = db.json_one(f"SELECT singleNssai FROM {SM_T} WHERE ueid={db.quote(ueid)}") or {}
                checks.append({"check": f"SST = {p['sst']}", "ok": int(nssai.get("sst", -1)) == int(p["sst"]),
                               "detail": f"实际 {nssai.get('sst')}"})
        checks.append({"check": "鉴权密钥非空(K/OPc)",
                       "ok": db.scalar(f"SELECT COUNT(*) FROM {AUTH_T} WHERE ueid={db.quote(imsi)} "
                                       f"AND encPermanentKey IS NOT NULL AND encOpcKey IS NOT NULL", "0") == "1"})
        return checks

    def rollback(self, intent, snapshot):
        imsi = snapshot["imsi"]
        notes = []
        variants = ",".join(db.quote(v) for v in db.imsi_variants(imsi))
        # 先删干净再按快照恢复:直接恢复 dump 会撞 PRIMARY KEY
        for t, key in ((SM_T, "have_sm"), (AUTH_T, "have_auth")):
            dump = snapshot.get(f"{key}_dump") or ""
            db.sql(f"DELETE FROM {t} WHERE ueid IN ({variants})")
            if dump.strip():
                db.restore(dump)
                notes.append(f"{t} 已还原到变更前")
            else:
                notes.append(f"{t} 已删除(变更前不存在)")
        return f"{imsi}: " + ";".join(notes)


class SetQoS(Action):
    name = "set_qos"
    description = "修改 QoS:改 SessionManagementSubscriptionData 的 5QI/AMBR/ARP,必要时重建 UE 会话"

    def validate(self, intent):
        self._imsi(intent)
        p = intent.setdefault("params", {})
        if not any(k in p for k in ("fiveQI", "ambr_ul", "ambr_dl", "arp_priority")):
            raise IntentError("set_qos 至少要给出 5QI / AMBR / ARP 之一")
        if p.get("fiveQI") is not None and not (1 <= int(p["fiveQI"]) <= 255):
            raise IntentError("fiveQI 超出范围")
        for k in ("ambr_ul", "ambr_dl"):
            if p.get(k) and not re.fullmatch(r"\d+(\.\d+)?(Mbps|Gbps)", str(p[k])):
                raise IntentError(f"{k} 格式应为 100Mbps 这种")

    def _ueid(self, imsi):
        u = db.resolve_ueid(imsi, [SM_T])
        if not u:
            raise IntentError(f"{imsi} 在 {SM_T} 里没有签约数据,先做 add_subscriber")
        return u

    def plan(self, intent):
        imsi = self._imsi(intent)
        p = intent["params"]
        u = self._ueid(imsi)
        active = db.active_ue_imsi()
        steps = [f"快照 {u} 当前 QoS 配置(可回滚)"]
        if p.get("fiveQI") is not None:
            steps.append(f"5QI → {p['fiveQI']}")
        if p.get("arp_priority") is not None:
            steps.append(f"ARP priorityLevel → {p['arp_priority']}")
        for k, label in (("ambr_ul", "上行"), ("ambr_dl", "下行")):
            if p.get(k):
                steps.append(f"{label} AMBR → {p[k]}")
        if active and active == imsi and intent["constraints"].get("allow_session_rebuild", True):
            steps.append("目标是当前活动 UE:重建 PDU 会话使其生效(断几十秒,oran-ue-watchdog 兜底)")
        elif active and active == imsi:
            steps.append("目标是当前活动 UE,但策略禁止重建会话 → 改动只落库,下次建会话才生效")
        return steps

    def snapshot(self, intent):
        imsi = self._imsi(intent)
        u = self._ueid(imsi)
        return {"kind": "update", "imsi": imsi, "ueid": u,
                "before": _extract(_sm_json(u) or {}),
                "dump": db.dump_rows(SM_T, f"ueid='{u}'")}

    def execute(self, intent):
        imsi = self._imsi(intent)
        p = intent["params"]
        u = self._ueid(imsi)
        sets = []
        if p.get("fiveQI") is not None:
            sets.append(f"{Q5QI}, {int(p['fiveQI'])}")
        if p.get("arp_priority") is not None:
            sets.append(f"{QARP}, {int(p['arp_priority'])}")
        if p.get("ambr_ul"):
            sets.append(f"{QUL}, {db.quote(p['ambr_ul'])}")
        if p.get("ambr_dl"):
            sets.append(f"{QDL}, {db.quote(p['ambr_dl'])}")
        db.sql(f"UPDATE {SM_T} SET {SM_DATA}=JSON_SET({SM_DATA}, {', '.join(sets)}) WHERE ueid={db.quote(u)}")

        rebuild = None
        active = db.active_ue_imsi()
        if active and active == imsi and intent["constraints"].get("allow_session_rebuild", True):
            before = ue.snapshot()
            rebuild = ue.restart_and_wait()
            rebuild["before"] = before
        return {"imsi": imsi, "ueid": u, "rebuild": rebuild}

    def verify(self, intent, exec_result):
        u = exec_result["ueid"]
        p = intent["params"]
        sm = _sm_json(u) or {}
        got = _extract(sm)
        checks = [{"check": "签约数据仍可读", "ok": bool(sm)}]
        if p.get("fiveQI") is not None:
            checks.append({"check": f"5QI = {p['fiveQI']}", "ok": int(got["fiveQI"] or -1) == int(p["fiveQI"]),
                           "detail": f"实际 {got['fiveQI']}"})
        if p.get("arp_priority") is not None:
            checks.append({"check": f"ARP priorityLevel = {p['arp_priority']}",
                           "ok": int(got["arp_priority"] or -1) == int(p["arp_priority"]),
                           "detail": f"实际 {got['arp_priority']}"})
        if p.get("ambr_ul"):
            checks.append({"check": f"上行 AMBR = {p['ambr_ul']}", "ok": got["ambr_ul"] == p["ambr_ul"],
                           "detail": f"实际 {got['ambr_ul']}"})
        if p.get("ambr_dl"):
            checks.append({"check": f"下行 AMBR = {p['ambr_dl']}", "ok": got["ambr_dl"] == p["ambr_dl"],
                           "detail": f"实际 {got['ambr_dl']}"})
        r = exec_result.get("rebuild")
        if r:
            checks.append({"check": "UE 已重建 PDU 会话", "ok": bool(r.get("ok")),
                           "detail": f"等 {r.get('waited')}s, IP {r.get('ip')}, {r.get('evidence')}"})
            if r.get("ok"):
                checks.append({"check": "用户面可达 ext-dn 192.168.73.135", "ok": ue.ping_ok(src=ue.TUN)})
        return checks

    def rollback(self, intent, snapshot):
        ueid = snapshot["ueid"]
        variants = ",".join(db.quote(v) for v in db.imsi_variants(ueid))
        db.sql(f"DELETE FROM {SM_T} WHERE ueid IN ({variants})")   # 直接 restore 会撞主键
        db.restore(snapshot["dump"])
        msg = f"已恢复 {ueid} 的签约数据到 {snapshot['before']}"
        active = db.active_ue_imsi()
        if active and active == snapshot["imsi"]:
            r = ue.restart_and_wait()
            msg += f";会话重建{'成功' if r.get('ok') else '失败'}({r.get('waited')}s)"
        return msg


ACTIONS: Dict[str, Action] = {a.name: a for a in (AddSubscriber(), SetQoS())}


def get(action: str) -> Action:
    if action not in ACTIONS:
        raise IntentError(f"未知动作 {action},可用: {', '.join(ACTIONS)}")
    return ACTIONS[action]
