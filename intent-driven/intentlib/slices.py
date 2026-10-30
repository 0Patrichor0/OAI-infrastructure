"""切片清册:把"一个切片在四个面上是否一致"读出来。

本环境一个切片要真正可用,必须四处对齐(缺一处就建不起会话):
  1. RAN 广告:  du.conf / cucp.conf / cuup.conf 的 snssaiList
  2. UPF 注册:  vpp-upf 容器 env 的 SNSSAI_SST / SNSSAI_SD(注册进 NRF 的 sNssais)
  3. 订户签约:  oai_db.SessionManagementSubscriptionData.singleNssai
  4. UE 请求:   ue.conf 的 pdu_sessions.nssai_sst / nssai_sd

SMF 选 UPF 时按 S-NSSAI 精确匹配,所以 UPF 那一项最容易漏 —— 漏了就是
`UPF could not be selected for the PDU session`。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Dict, List

from . import db, ue
from .actions import SM_T

RAN_CONFS = ["cucp.conf", "cuup.conf", "du.conf"]
CONF_DIR = "/home/ubuntu/oai-ran-conf"
UE_CONF = os.path.join(CONF_DIR, "ue.conf")
COMPOSE = "/home/ubuntu/nwdaf-rel17/oai-cn5g-fed/docker-compose/docker-compose-basic-vpp-nrf.yaml"
UPF = "vpp-upf"


def sd_int(sd: Any) -> int:
    """把各种写法的 SD 归一成 24 位整数。

    同一台机器上见过四种写法:0xffffff / "ffffff" / "123"(16 进制字符串,=0x000123)
    / "16777215"(我们自己的 JSON_SET 写成了十进制)。约定:
      · ≤6 位且全是 hex 字符 → 按 16 进制
      · 更长的纯数字 → 按 10 进制
    """
    if sd in (None, "", "None"):
        return 0xFFFFFF
    if isinstance(sd, int):
        return sd if sd <= 0xFFFFFF else int(str(sd), 16)
    s = str(sd).strip().lower().removeprefix("0x")
    if not s:
        return 0xFFFFFF
    if len(s) <= 6 and all(c in "0123456789abcdef" for c in s):
        return int(s, 16)
    return int(s)


def sd_hex(sd: Any) -> str:
    """SD 统一显示成 6 位 hex。"""
    try:
        return format(sd_int(sd), "06x")
    except (TypeError, ValueError):
        return str(sd)


def ran_snssais() -> List[Dict[str, Any]]:
    """RAN 三个配置里广告的 S-NSSAI(取并集)。"""
    out: Dict[tuple, Dict[str, Any]] = {}
    for name in RAN_CONFS:
        path = os.path.join(CONF_DIR, name)
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        for m in re.finditer(r"snssaiList\s*=\s*\((.*?)\)\s*;", text, re.S):
            for e in re.finditer(r"sst\s*=\s*(\d+)(?:\s*,\s*sd\s*=\s*([0-9a-fA-Fx]+))?", m.group(1)):
                sst, sd = int(e.group(1)), sd_hex(e.group(2))
                out.setdefault((sst, sd), {"sst": sst, "sd": sd, "conf_files": []})
                out[(sst, sd)]["conf_files"].append(name)
    return sorted(out.values(), key=lambda x: (x["sst"], x["sd"]))


def upf_snssais() -> List[Dict[str, Any]]:
    """UPF 当前注册的**全部** S-NSSAI。

    优先读 SNSSAI_LIST（多切片补丁引入，形如 "3/000003,1/000123"），
    没有就退回上游的单值 SNSSAI_SST/SNSSAI_SD。
    """
    p = subprocess.run(["sudo", "docker", "inspect", UPF, "--format",
                        "{{range .Config.Env}}{{println .}}{{end}}"], capture_output=True, text=True)
    env = p.stdout
    dnn = re.search(r"DNN=([\w.-]+)", env)
    out: List[Dict[str, Any]] = []
    m = re.search(r"SNSSAI_LIST=([^\s]+)", env)
    if m:
        for item in m.group(1).replace(";", ",").split(","):
            item = item.strip()
            if not item:
                continue
            sst_s, _, sd_s = item.partition("/")
            try:
                out.append({"sst": int(sst_s), "sd": sd_hex(int(sd_s or "16777215", 10)),
                            "dnn": dnn.group(1) if dnn else "default"})
            except ValueError:
                continue
    if not out:
        sst = re.search(r"SNSSAI_SST=(\d+)", env)
        sd = re.search(r"SNSSAI_SD=([0-9a-fA-Fx]+)", env)
        if sst:
            out.append({"sst": int(sst.group(1)), "sd": sd_hex(sd.group(1)) if sd else "ffffff",
                        "dnn": dnn.group(1) if dnn else "default"})
    return out


def upf_snssai() -> Dict[str, Any]:
    """兼容旧调用：返回第一个注册的切片。"""
    allx = upf_snssais()
    return allx[0] if allx else {"sst": None, "sd": "ffffff", "dnn": "default"}


def ue_conf() -> Dict[str, Any]:
    try:
        text = open(UE_CONF, encoding="utf-8").read()
    except OSError:
        return {}
    imsi = re.search(r'imsi\s*=\s*"(\d{15})"', text)
    sst = re.search(r"nssai_sst\s*=\s*(\d+)", text)
    sd = re.search(r"nssai_sd\s*=\s*(0x[0-9a-fA-F]+|\d+)", text)
    return {"imsi": imsi.group(1) if imsi else None,
            "sst": int(sst.group(1)) if sst else 1,
            "sd": sd_hex(int(sd.group(1), 16 if sd.group(1).lower().startswith("0x") else 10)) if sd else "ffffff"}


def active_session() -> Dict[str, Any]:
    """最近的 PDU 会话:从 ue.log 取请求的 NSSAI 与分配到的 IP。"""
    ip = ue.tunnel_ip()
    out = {"nssai": None, "ip": ip, "ok": bool(ip)}
    if not ip:
        return out
    try:
        with open(ue.UE_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            tail = f.read().decode(errors="replace")
    except OSError:
        return out
    m = re.findall(r"requested PDU session ID \d+ type \d+ NSSAI (\d+)\.(\d+)", tail)
    if m:
        out["nssai"] = (int(m[-1][0]), sd_hex(int(m[-1][1])))
    acc = re.findall(r"PDU Session Establishment Accept, UE IPv4: ([\d.]+)", tail)
    # Historical Accept lines cannot replace the current interface address.
    return out


def all_sessions() -> List[Dict[str, Any]]:
    """所有在跑的 UE 的会话：主 UE（宿主）+ 每个 netns 节点。

    以前只看主 UE（ue.log + oaitun_ue1），多 UE 之后切片表和订户表都只显示一个会话。
    """
    out: List[Dict[str, Any]] = []
    conf = ue_conf()
    main = active_session()
    if main.get("ip"):
        out.append({"node": "ue1", "imsi": conf.get("imsi"), "ip": main["ip"],
                    "nssai": main.get("nssai"), "source": "host"})
    try:
        from . import ue_nodes
    except Exception:  # noqa: BLE001
        return out
    for idx in ue_nodes.existing_indexes():
        ip = ue_nodes.tun_ip(idx) if ue_nodes.is_active(idx) else None
        if not ip:
            continue
        imsi = sst = sd = None
        try:
            text = open(ue_nodes.conf_path(idx), encoding="utf-8").read()
            m = re.search(r'imsi\s*=\s*"(\d{15})"', text)
            imsi = m.group(1) if m else None
            m2 = re.search(r"nssai_sst\s*=\s*(\d+)", text)
            m3 = re.search(r"nssai_sd\s*=\s*(0x[0-9a-fA-F]+|\d+)", text)
            sst = int(m2.group(1)) if m2 else None
            sd = sd_hex(int(m3.group(1), 16 if m3.group(1).lower().startswith("0x") else 10)) if m3 else None
        except OSError:
            pass
        out.append({"node": f"ue{idx}", "imsi": imsi, "ip": ip,
                    "nssai": (sst, sd) if sst is not None else None, "source": f"netns ue{idx}"})
    return out


def subscribers() -> List[Dict[str, Any]]:
    rows = db.rows(
        f"SELECT ueid, "
        f"JSON_UNQUOTE(JSON_EXTRACT(singleNssai,'$.sst')), "
        f"JSON_UNQUOTE(JSON_EXTRACT(singleNssai,'$.sd')), "
        f"JSON_UNQUOTE(JSON_EXTRACT(dnnConfigurations,'$.\"default\".\"5gQosProfile\".\"5qi\"')), "
        f"JSON_UNQUOTE(JSON_EXTRACT(dnnConfigurations,'$.\"default\".\"sessionAmbr\".\"downlink\"')) "
        f"FROM {SM_T} ORDER BY ueid")
    out = []
    for r in rows:
        while len(r) < 5:
            r.append("")
        out.append({"imsi": r[0].removeprefix("imsi-"), "sst": int(r[1]) if r[1] else None, "sd": sd_hex(r[2]),
                    "fiveQI": int(r[3]) if r[3] else None, "ambr_dl": r[4] or None})
    return out


def inventory() -> Dict[str, Any]:
    ran = ran_snssais()
    upf = upf_snssais()
    conf = ue_conf()
    sess = active_session()
    sessions = all_sessions()
    subs = subscribers()
    auth = {r[0].removeprefix("imsi-") for r in db.rows(
        "SELECT ueid FROM AuthenticationSubscription WHERE encPermanentKey IS NOT NULL "
        "AND encPermanentKey <> '' AND encOpcKey IS NOT NULL AND encOpcKey <> ''")}
    subscribed = {x["imsi"] for x in subs}
    known_subs = set(subscribed)
    # Running nodes can exist even when their subscription was removed.
    for node in sessions:
        if node.get("imsi") and node["imsi"] not in known_subs:
            ns = node.get("nssai") or (conf.get("sst"), conf.get("sd"))
            subs.append({"imsi": node["imsi"], "sst": ns[0], "sd": ns[1], "fiveQI": None, "ambr_dl": None})
            known_subs.add(node["imsi"])
    template_ready = "208950000000031" in (auth & subscribed)

    def k(sst, sd):
        return (int(sst), sd_int(sd))

    ran_keys = {k(x["sst"], x["sd"]) for x in ran}
    upf_keys = {k(x["sst"], x["sd"]) for x in upf}
    upf_key = next(iter(upf_keys)) if len(upf_keys) == 1 else None
    conf_key = k(conf["sst"], conf["sd"]) if conf else None
    sess_key = k(*sess["nssai"]) if sess.get("nssai") else None
    sess_keys = {k(*x["nssai"]) for x in sessions if x.get("nssai")}
    ip_by_imsi = {x["imsi"]: x["ip"] for x in sessions if x.get("imsi")}
    sub_keys = {k(x["sst"], x["sd"]) for x in subs if x["sst"] is not None}

    keys = set(ran_keys) | sub_keys
    for extra in (conf_key, sess_key):
        if extra:
            keys.add(extra)
    keys |= upf_keys
    for extra in ():
        if extra:
            keys.add(extra)

    from .slice_status import core_document, core_checks
    from .actions_slice import CORE_CONF
    try: core = core_document(CORE_CONF)
    except Exception: core = {}
    slices = []
    for sst, sd_i in sorted(keys):
        members = [x["imsi"] for x in subs if x["sst"] is not None and k(x["sst"], x["sd"]) == (sst, sd_i)]
        ran_ok = (sst, sd_i) in ran_keys
        all_ran = any(k(x["sst"], x["sd"]) == (sst, sd_i) and set(RAN_CONFS).issubset(x.get("conf_files", [])) for x in ran)
        upf_ok = (sst, sd_i) in upf_keys
        ue_ok = conf_key == (sst, sd_i) or (sst, sd_i) in sess_keys
        sess_ok = (sst, sd_i) in sess_keys
        on_this = [x for x in sessions if x.get("nssai") and k(*x["nssai"]) == (sst, sd_i)]
        core_ok = all(core_checks(core, sst, format(sd_i, "06x")).values())
        slices.append({
            "core_configured": core_ok,
            "sst": sst, "sd": format(sd_i, "06x"),
            "label": f"SST {sst} / SD {format(sd_i, '06x')}",
            "ran_advertised": ran_ok,
            "upf_registered": upf_ok,
            "ue_requested": ue_ok,
            "session_on_it": sess_ok,
            "sessions": [{"node": x["node"], "imsi": x["imsi"], "ip": x["ip"]} for x in on_this],
            "subscribers": members,
            "network_ready": all_ran and upf_ok and core_ok,
            "usable": all_ran and upf_ok and core_ok,
            "unavailable_reason": "" if all_ran and upf_ok and core_ok else ("核心网配置不完整，请先激活切片" if not core_ok else ("UPF 未注册，请先激活切片" if not upf_ok else "CU/DU 广告不完整，请先激活切片")),
        })

    ues = []
    for x in subs:
        active = x["imsi"] in ip_by_imsi
        has_auth = x["imsi"] in auth
        has_sm = x["imsi"] in subscribed
        ready = any(s["network_ready"] and (s["sst"], s["sd"]) == (x["sst"], x["sd"]) for s in slices)
        reason = "已在线，请用新 IMSI 新增 UE" if active else ("缺少鉴权，新增节点时自动补齐" if not has_auth else ("缺少签约，新增时选择切片自动补齐" if not has_sm else ("签约切片未就绪，请选择可用切片新增" if not ready else "可用于新增 UE")))
        ues.append({
            "has_auth": has_auth, "has_subscription": has_sm,
            "can_add_node": not active and (has_auth or template_ready),
            "can_activate": not active and has_auth and has_sm and ready,
            "status_reason": reason,
            "imsi": x["imsi"],
            "slice": {"sst": x["sst"], "sd": x["sd"]},
            "fiveQI": x["fiveQI"], "ambr_dl": x["ambr_dl"],
            "provisioned": x["imsi"] in auth,
            "is_active_ue": active,
            "session_ip": ip_by_imsi.get(x["imsi"]),
            "session_ok": x["imsi"] in ip_by_imsi,
        })

    used = auth | subscribed | set(ip_by_imsi)
    suggested = int(conf.get("imsi") or "208950000000031") + 1
    while str(suggested) in used:
        suggested += 1
    return {
        "suggested_imsi": str(suggested),
        "template_ready": template_ready,
        "ok": True,
        "slices": slices, "ues": ues,
        "ran": ran, "upf": upf, "ue_conf": conf, "session": sess,
        "sessions": sessions,
        "ts": __import__("time").strftime("%H:%M:%S"),
        "note": "切片可用需要三面对齐:RAN snssaiList + UPF 的 S-NSSAI + 订户签约;"
                "当前 UPF 支持的 S-NSSAI 以本页实际注册列表为准。"
                "UE 请求的 NSSAI 也必须与 UPF 一致,否则 SMF 报 UPF could not be selected。",
    }
