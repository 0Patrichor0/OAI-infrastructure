"""切片与 UE 相关的动作。

本环境的硬约束(实测):
  · SMF 按 S-NSSAI 精确匹配，当前 UPF 支持多切片并存；
  · 多个 UE 通过独立网络命名空间同时运行，切片激活需恢复全部在线 UE；
  · UE 请求哪个 S-NSSAI 由 ue.conf 的 pdu_sessions.nssai_sst / nssai_sd 决定,
    与订户签约的 singleNssai 是两回事(两者不一致时以 UE 请求的为准去选 UPF)。
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List

from . import db, persist_cfg, ue
from .actions import AUTH_T, SM_T, Action, IntentError
from .actions_more import _compose, _restart_smf, _wait_upf, _compose_env_get, _compose_env_set
from .slices import CONF_DIR, UE_CONF, ran_snssais, sd_hex, sd_int

RAN_CONFS = [os.path.join(CONF_DIR, n) for n in ("cucp.conf", "cuup.conf", "du.conf")]


CORE_CONF = ("/home/ubuntu/nwdaf-rel17/oai-cn5g-fed/docker-compose/conf/"
             "basic_vpp_nrf_config.yaml")


def core_slice_known(sst: int, sd: str) -> bool:
    from .slice_status import core_document, core_checks
    try:return all(core_checks(core_document(CORE_CONF), sst, sd).values())
    except (OSError, ValueError, TypeError):return False


def _append_core_list(text, header, entry, item_indent):
    """Append to a known YAML list without depending on a specific slice anchor."""
    match = re.search(header, text, re.M)
    if not match:raise IntentError(f"核心网配置缺少列表: {header}")
    pos = match.end()
    if text[pos:pos+1] == "\n":pos += 1
    for line in text[pos:].splitlines(keepends=True):
        if line.strip() and not line.lstrip().startswith('#'):
            indent=len(line)-len(line.lstrip())
            if indent < item_indent:break
        pos += len(line)
    return text[:pos] + entry + text[pos:]


def add_core_slice(sst: int, sd: str) -> bool:
    """Ensure definition and all core references; no dependency on deleted templates."""
    from .slice_status import core_document, core_checks
    from .storage import atomic_text
    from pathlib import Path
    import yaml
    text=Path(CORE_CONF).read_text()
    status=core_checks(core_document(CORE_CONF),sst,sd)
    if all(status.values()):return False
    name=None
    for m in re.finditer(r"- &(\S+)\n\s+sst:\s*(\d+)\n(?:\s+sd:\s*([0-9a-fA-Fx]+))?", text):
        if int(m[2])==sst and sd_int(m[3])==sd_int(sd):name=m[1];break
    if name is None:
        name=f"slice_{sst}_{sd}"
        text=_append_core_list(text,r'^snssais:[ \t]*$',f"  - &{name}\n    sst: {sst}\n    sd: {sd} # in hex\n",2)
    additions=[
        ('AMF nssai',r'^      nssai:[ \t]*$',f'        - *{name}\n',8),
        ('SMF discovery/default DNN',r'^    sNssaiSmfInfoList:[ \t]*$',f'      - sNssai: *{name}\n        dnnSmfInfoList:\n          - dnn: "default"\n',6),
        ('SMF local subscription/default DNN',r'^  local_subscription_infos:[ \t]*$',f'    - single_nssai: *{name}\n      dnn: "default"\n      qos_profile:\n        5qi: 9\n        session_ambr_ul: "100Mbps"\n        session_ambr_dl: "200Mbps"\n',4),
    ]
    for key,header,entry,indent in additions:
        if not status[key]:text=_append_core_list(text,header,entry,indent)
    if not all(core_checks(yaml.load(text,Loader=yaml.BaseLoader),sst,sd).values()):
        raise IntentError("核心网配置未能完整生成，未写入配置")
    atomic_text(CORE_CONF,text)
    return True


def _backup(paths: List[str]) -> Dict[str, str]:
    snap = {}
    for p in paths:
        try:
            snap[p] = open(p, encoding="utf-8").read()
        except OSError:
            snap[p] = ""
    return snap


def _restore(snap: Dict[str, str]) -> None:
    for p, text in snap.items():
        if text:
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)


def _set_ran_snssai(sst: int, sd: int) -> None:
    """三份 RAN 配置的 snssaiList 改成目标切片(单切片环境只能替换)。"""
    repl = f"snssaiList = ({{ sst = {sst}, sd = 0x{sd:06x} }})"
    for path in RAN_CONFS:
        text = open(path, encoding="utf-8").read()
        new, n = re.subn(r"snssaiList\s*=\s*\([^)]*\)", repl, text, count=1)
        if n == 0:
            raise IntentError(f"{os.path.basename(path)} 里找不到 snssaiList")
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        # 立刻把新值记成期望值：否则持久层定时器可能拿旧值把文件改回去
        # （实测：switch_ue 等会话的 78 秒里被 timer 覆盖，verify 读到旧值）
        persist_cfg.record_file(str(path), r"snssaiList\s*=\s*\(([^)]*)\)", None,
                                f"{os.path.basename(path)} snssaiList",
                                {"cucp.conf": "oran-cu-cp", "cuup.conf": "oran-cu-up",
                                 "du.conf": "oran-du"}.get(os.path.basename(path), ""))


def _add_ran_snssai(sst: int, sd: int) -> bool:
    """把切片追加进三份 RAN 配置的 snssaiList（已存在则不动）。返回是否改动。

    多切片环境下 RAN 可以同时广播多个 S-NSSAI，不能像单切片那样整段替换，
    否则每激活一个切片就把别的切片从广播里抹掉。
    """
    entry = f"{{ sst = {sst}, sd = 0x{sd:06x} }}"
    changed = False
    svc = {"cucp.conf": "oran-cu-cp", "cuup.conf": "oran-cu-up", "du.conf": "oran-du"}
    for path in RAN_CONFS:
        text = open(path, encoding="utf-8").read()
        m = re.search(r"snssaiList\s*=\s*\(([^)]*)\)", text)
        if not m:
            raise IntentError(f"{os.path.basename(path)} 里找不到 snssaiList")
        body = m.group(1).strip()
        if any(int(e[1]) == sst and sd_int(e[2]) == sd for e in re.finditer(r"sst\s*=\s*(\d+)\s*,\s*sd\s*=\s*(0x[0-9a-fA-F]+|[0-9a-fA-F]+)", body)):
            continue
        new_body = f"{body}, {entry}" if body else entry
        text = text[:m.start(1)] + new_body + text[m.end(1):]
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        persist_cfg.record_file(str(path), r"snssaiList\s*=\s*\(([^)]*)\)", None,
                                f"{os.path.basename(path)} snssaiList",
                                svc.get(os.path.basename(path), ""))
        changed = True
    return changed


def _upf_list_add(sst: int, sd: int) -> bool:
    """把切片追加进 compose 的 SNSSAI_LIST（十进制形式，见下方说明），返回是否改动。

    SNSSAI_LIST 里的 SD 必须写**十进制数值**：UPF 把它原样写进 NRF profile，
    而 SMF 读 profile 时按十进制解析。写 "000123" 会被当成 123（0x7b），
    与 YAML 里按十六进制解析的 000123（291）对不上，会话就被拒。
    """
    cur = _compose_env_get("SNSSAI_LIST") or ""
    items = [x.strip() for x in cur.split(",") if x.strip()]
    want = f"{sst}/{sd}"          # sd 已经是数值
    if want in items:
        return False
    items.append(want)
    _compose_env_set("SNSSAI_LIST", ",".join(items))
    return True


def _set_ue_conf(imsi: str, key_hex: str, opc_hex: str, sst: int, sd: int) -> None:
    text = open(UE_CONF, encoding="utf-8").read()
    text = re.sub(r'imsi\s*=\s*"\d{15}"', f'imsi = "{imsi}"', text, count=1)
    if key_hex:
        text = re.sub(r'key\s*=\s*"[0-9a-fA-F]*"', f'key  = "{key_hex}"', text, count=1)
    if opc_hex:
        text = re.sub(r'opc\s*=\s*"[0-9a-fA-F]*"', f'opc  = "{opc_hex}"', text, count=1)
    text = re.sub(r"nssai_sst\s*=\s*\d+", f"nssai_sst = {sst}", text, count=1)
    if re.search(r"nssai_sd\s*=\s*(0x[0-9a-fA-F]+|\d+)", text):
        text = re.sub(r"nssai_sd\s*=\s*(0x[0-9a-fA-F]+|\d+)", f"nssai_sd = 0x{sd:06x}", text, count=1)
    else:
        text = re.sub(r"(nssai_sst\s*=\s*\d+)", rf"\1; nssai_sd = 0x{sd:06x}", text, count=1)
    with open(UE_CONF, "w", encoding="utf-8") as f:
        f.write(text)
    for pat, label in ((r'imsi\s*=\s*"(\d{15})"', "ue.conf imsi"),
                       (r"nssai_sst\s*=\s*(\d+)", "ue.conf nssai_sst"),
                       (r"nssai_sd\s*=\s*(0x[0-9a-fA-F]+|\d+)", "ue.conf nssai_sd")):
        persist_cfg.record_file(UE_CONF, pat, None, label, "oran-ue")


def _creds(imsi: str) -> tuple[str, str]:
    r = db.rows(f"SELECT encPermanentKey, encOpcKey FROM {AUTH_T} WHERE ueid={db.quote(imsi)}")
    if not r:
        raise IntentError(f"{imsi} 没有鉴权数据,无法生成 UE 配置")
    return (r[0][0] or "", r[0][1] if len(r[0]) > 1 else "")


def _restart_ran_and_ue() -> Dict[str, Any]:
    """RAN 顺序:cu-cp → cu-up → du → ue(du 依赖 cu,ue 依赖 du)。"""
    import subprocess
    for svc, wait in (("oran-cu-cp", 8), ("oran-cu-up", 6), ("oran-du", 10)):
        result = subprocess.run(["sudo", "systemctl", "restart", svc], capture_output=True)
        if result.returncode:
            raise IntentError(f"RAN 重启失败: {svc}")
        time.sleep(wait)
    return ue.restart_and_wait(restore_all=True)


def _session_nssai() -> tuple[int, int] | None:
    """从 ue.log 最近一段里取 UE 实际请求的 NSSAI。"""
    try:
        with open(ue.UE_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 300_000))
            tail = f.read().decode(errors="replace")
    except OSError:
        return None
    m = re.findall(r"requested PDU session ID \d+ type \d+ NSSAI (\d+)\.(\d+)", tail)
    if not m:
        return None
    return int(m[-1][0]), sd_int(int(m[-1][1]))


def _ensure_main_identity_available(imsi: str) -> None:
    """Changing the main UE must not duplicate an online independent UE's IMSI."""
    from .slices import all_sessions
    for row in all_sessions():
        if row.get("imsi") == imsi and row.get("node") != "ue1":
            raise IntentError(f"{imsi} 已在 {row.get('node')} 在线，不能同时用于主 UE；请选择离线身份或先停止该节点")


class ActivateSlice(Action):
    name = "activate_slice"
    description = "端到端创建切片：AMF/SMF、UPF、CU/DU、订户及 UE 均生效后才成功（断全部会话）"

    def _target_imsi(self, intent) -> str:
        imsi = (intent.get("target") or {}).get("imsi") or db.active_ue_imsi()
        if not imsi:
            raise IntentError("找不到目标 IMSI(也没找到活动 UE)")
        return imsi

    def validate(self, intent):
        p = intent.setdefault("params", {})
        if p.get("sst") is None or not (0 <= int(p["sst"]) <= 255):
            raise IntentError("需要 sst(0-255)")
        sd = p.get("sd", "ffffff")
        p["sd"] = sd_hex(sd)
        p["sst"] = int(p["sst"])
        intent.setdefault("target", {})["imsi"] = self._target_imsi(intent)
        if not re.fullmatch(r"[0-9a-fA-F]{6}", p["sd"]):
            raise IntentError("SD 必须为六位十六进制")
        intent.setdefault("constraints", {}).setdefault("allow_core_config_edit", True)
        imsi = intent["target"]["imsi"]
        _ensure_main_identity_available(imsi)
        if not db.resolve_ueid(imsi, [AUTH_T]) or not db.resolve_ueid(imsi, [SM_T]):
            raise IntentError(f"验收 UE {imsi} 缺少鉴权或签约，请先开户；未创建切片")
        if not core_slice_known(p["sst"], p["sd"]) and not intent.setdefault(
                "constraints", {}).get("allow_core_config_edit", False):
            raise IntentError(
                f"AMF/SMF 的切片清单里没有 {p['sst']}/{p['sd']},会话会被拒"
                f"(SMF: UPF could not be selected);需要写 core 配置并重启 AMF/SMF,"
                f"请显式设置 constraints.allow_core_config_edit=true")
        if not intent.setdefault("constraints", {}).get("allow_upf_recreate", False):
            raise IntentError("激活切片要改 UPF 的 S-NSSAI 并重建 UPF(断所有会话);"
                              "请显式设置 constraints.allow_upf_recreate=true")
        intent["constraints"].setdefault("allow_session_rebuild", True)

    def plan(self, intent):
        p = intent["params"]
        imsi = self._target_imsi(intent)
        cur = ran_snssais()
        return [
            f"核心网:补齐 AMF nssai、SMF discovery 与本地签约的 {p['sst']}/{p['sd']}，并重启加载",
            "RAN 配置:保留 {cur}，向 CU-CP/CU-UP/DU 追加 {{sst={sst}, sd=0x{sd:06x}}}".format(
                cur=[str(x["sst"]) + "." + x["sd"] for x in cur],
                sst=p["sst"], sd=sd_int(p["sd"])),
            f"UPF 注册:把 {p['sst']}/{p['sd']} 追加进 SNSSAI_LIST（已在列表里就跳过，不重建、不断会话）",
            f"订户签约:{imsi} 的 singleNssai → {p['sst']}/{p['sd']}",
            f"UE 请求:ue.conf 的 nssai_sst/sd → {p['sst']}/{p['sd']}",
            "重启 RAN(cu-cp → cu-up → du)与 UE,让 E2/会话按新切片重建",
            "验收:核心网完整引用及进程加载、三份 RAN 配置及进程加载、NRF 实际注册、目标 UE 会话和 ping；任一失败则意图失败",
            "说明:UPF 已打多切片补丁,新旧切片可并存;只有新增切片需要重建一次 UPF",
        ]

    def config_footprint(self, intent):
        """切片改动散落在 6 处：compose 的 SNSSAI_* 和三份 RAN 配置的 snssaiList。"""
        items = [{"kind": "core_slice", "sst": int(intent["params"]["sst"]),
                  "sd": str(intent["params"]["sd"])},
                 {"kind": "compose_env", "key": "SNSSAI_LIST",
                  "value": _compose_env_get("SNSSAI_LIST"), "service": "vpp-upf"}]
        svc = {"cucp.conf": "oran-cu-cp", "cuup.conf": "oran-cu-up", "du.conf": "oran-du"}
        for path in RAN_CONFS:
            items.append({"kind": "file_key", "file": path,
                          "pattern": r"snssaiList\s*=\s*\(([^)]*)\)",
                          "label": f"{os.path.basename(path)} snssaiList",
                          "service": svc.get(os.path.basename(path), "")})
        items.extend(SwitchUe().config_footprint(intent))
        return items

    def snapshot(self, intent):
        imsi = self._target_imsi(intent)
        return {"kind": "activate_slice", "imsi": imsi,
                "ran": _backup(RAN_CONFS), "ue_conf": _backup([UE_CONF]),
                "core_conf": _backup([CORE_CONF]),
                "upf_env": {"sst": _compose_env_get("SNSSAI_SST"),
                            "sd": _compose_env_get("SNSSAI_SD"),
                            "list": _compose_env_get("SNSSAI_LIST")},
                "core_changed": not core_slice_known(int(intent["params"]["sst"]), intent["params"]["sd"]),
                "sm_dump": db.dump_rows(SM_T, f"ueid='{imsi}'"),
                "before_snssai": db.json_one(
                    f"SELECT singleNssai FROM {SM_T} WHERE ueid={db.quote(imsi)}")}

    def execute(self, intent):
        p = intent["params"]
        imsi = self._target_imsi(intent)
        sst, sdi = p["sst"], sd_int(p["sd"])

        # 1) 订户签约(先改库,后面会话才会按它走)
        db.sql(f"UPDATE {SM_T} SET singleNssai=JSON_SET(singleNssai, '$.sst', {sst}, "
               f"'$.sd', {db.quote(p['sd'])}) WHERE ueid={db.quote(imsi)}")

        # 2) 核心切片清单(AMF/SMF),不认识就加进去并重启
        core_changed = add_core_slice(sst, p["sd"])
        result = __import__("subprocess").run(["sudo", "docker", "restart", "oai-amf"], capture_output=True)
        if result.returncode:raise IntentError("AMF 重启失败")
        time.sleep(10)

        # 3) RAN 广播：追加而不是替换
        ran_changed = _add_ran_snssai(sst, sdi)

        # 4) UPF 注册：列表里没有才重建，避免每激活一个切片就断所有会话
        upf_changed = _upf_list_add(sst, sdi)
        if upf_changed:
            _compose(["up", "-d", "--no-deps", "--force-recreate", "vpp-upf"])
            if not _wait_upf():
                raise IntentError("vpp-upf 重建后未 healthy")
        if not _restart_smf():
            raise IntentError("SMF 未恢复 healthy(检查 N4)")

        # 4) UE 请求 + 重启 RAN/UE
        key, opc = _creds(imsi)
        _set_ue_conf(imsi, key, opc, sst, sdi)
        rebuild = _restart_ran_and_ue()
        return {"imsi": imsi, "sst": sst, "sd": p["sd"], "rebuild": rebuild,
                "ran_changed": ran_changed, "upf_changed": upf_changed}

    def verify(self, intent, exec_result):
        p = intent["params"]
        imsi = exec_result["imsi"]
        sst, sd = exec_result["sst"], exec_result["sd"]
        nssai = _session_nssai()
        checks = [
            {"check": f"UE 请求的 NSSAI = {sst}.{sd}", "ok": nssai == (sst, sd_int(sd)),
             "detail": f"实际 {nssai[0]}.{sd_hex(nssai[1])}" if nssai else "日志里没有 NSSAI 行"},
            {"check": "RAN 三份配置都广告了新切片",
             "ok": any(x["sst"] == sst and x["sd"] == sd and set(x.get("conf_files", [])) >= {os.path.basename(f) for f in RAN_CONFS} for x in ran_snssais())},
        ]
        env = __import__("subprocess").run(
            ["sudo", "docker", "inspect", "vpp-upf", "--format",
             "{{range .Config.Env}}{{println .}}{{end}}"], capture_output=True, text=True).stdout
        checks.append({"check": f"AMF/SMF 配置含切片 {sst}/{sd}",
                       "ok": core_slice_known(sst, sd)})
        # 多切片：UPF 现在按 SNSSAI_LIST 注册多条，判据是“集合里包含”而不是 env 里等于
        from .slices import upf_snssais
        have = {(x["sst"], x["sd"]) for x in upf_snssais()}
        checks.append({"check": f"UPF 注册 S-NSSAI 含 {sst}/{sd}",
                       "ok": (sst, sd) in have,
                       "detail": f"已注册 {sorted(have)}"})
        from .slice_status import core_checks, core_document, registered_upf_slices, ran_runtime, core_runtime
        for name, ok in core_checks(core_document(CORE_CONF), sst, sd).items():
            checks.append({"check": f"核心网 {name} 含 {sst}/{sd}", "ok": ok})
        checks.extend(core_runtime(CORE_CONF))
        checks.extend(ran_runtime(RAN_CONFS))
        registered, error = registered_upf_slices()
        checks.append({"check": f"NRF 实际 UPF 注册含 {sst}/{sd}", "ok": (sst, sd) in registered,
                       "detail": error or f"已注册 {sorted(registered)}"})
        row = db.json_one(f"SELECT singleNssai FROM {SM_T} WHERE ueid={db.quote(imsi)}") or {}
        checks.append({"check": f"订户签约 singleNssai = {sst}.{sd}",
                       "ok": int(row.get("sst", -1)) == sst and sd_hex(row.get("sd")) == sd,
                       "detail": f"实际 {row.get('sst')}.{sd_hex(row.get('sd'))}"})
        r = exec_result.get("rebuild") or {}
        checks.append({"check": "UE 已重建会话", "ok": bool(r.get("ok")),
                       "detail": f"{r.get('waited')}s, IP {r.get('ip')}"})
        if r.get("ok"):
            checks.append({"check": "新切片上能访问 ext-dn", "ok": ue.ping_ok(src=ue.TUN)})
        return checks

    def rollback(self, intent, snapshot):
        if snapshot.get("core_conf"):
            _restore(snapshot["core_conf"])
        _restore(snapshot["ran"])
        _restore(snapshot["ue_conf"])
        if snapshot["upf_env"].get("sst"):
            _compose_env_set("SNSSAI_SST", snapshot["upf_env"]["sst"])
        if snapshot["upf_env"].get("sd"):
            _compose_env_set("SNSSAI_SD", snapshot["upf_env"]["sd"])
        if snapshot["upf_env"].get("list") is not None:
            _compose_env_set("SNSSAI_LIST", snapshot["upf_env"]["list"])
        if snapshot.get("core_changed"):
            __import__("subprocess").run(["sudo", "docker", "restart", "oai-amf"], check=True, capture_output=True)
            time.sleep(10)
        imsi = snapshot["imsi"]
        variants = ",".join(db.quote(v) for v in db.imsi_variants(imsi))
        db.sql(f"DELETE FROM {SM_T} WHERE ueid IN ({variants})")
        if snapshot["sm_dump"].strip():
            db.restore(snapshot["sm_dump"])
        _compose(["up", "-d", "--no-deps", "--force-recreate", "vpp-upf"])
        if not _wait_upf():raise IntentError("回滚后 UPF 未 healthy")
        if not _restart_smf():raise IntentError("回滚后 SMF 未 healthy")
        r = _restart_ran_and_ue()
        if not r.get("ok") or not ue.ping_ok(src=ue.TUN):raise IntentError("回滚后 UE 通信未恢复")
        return (f"已恢复 RAN/UE/UPF 到 {snapshot['before_snssai']}"
                f";会话重建{'成功' if r.get('ok') else '失败'}({r.get('waited')}s)")


class SwitchUe(Action):
    name = "switch_ue"
    description = "把活动 UE 切到指定订户(改 ue.conf 的 IMSI/NSSAI 并重启 UE)"

    def validate(self, intent):
        imsi = (intent.get("target") or {}).get("imsi")
        if not imsi:
            raise IntentError("需要 target.imsi")
        _ensure_main_identity_available(imsi)
        if not db.exists(AUTH_T, f"ueid={db.quote(imsi)}"):
            raise IntentError(f"{imsi} 没有鉴权数据,先做 add_subscriber")
        if not db.exists(SM_T, f"ueid={db.quote(imsi)}"):
            raise IntentError(f"{imsi} 没有签约数据,先做 add_subscriber")
        # 订户的 S-NSSAI 必须在 UPF 注册的切片集合里，否则 SMF 选不到 UPF。
        # 多切片补丁之后一个 UPF 可以注册多个 S-NSSAI，所以这里是“属于集合”而不是“相等”。
        from .slices import upf_snssais
        sm = db.json_one(f"SELECT singleNssai FROM {SM_T} WHERE ueid={db.quote(imsi)}") or {}
        want = (int(sm.get("sst", -1)), sd_hex(sm.get("sd")))
        have = {(x["sst"], x["sd"]) for x in upf_snssais()}
        if want not in have:
            raise IntentError(
                f"订户 {imsi} 的切片 {want[0]}/{want[1]} 不在 UPF 注册的切片里 "
                f"{sorted(have)}，会话会建不起来；"
                f"先 activate_slice 激活该切片，或用 add_subscriber 把订户改成已注册的切片")
        intent.setdefault("constraints", {})["allow_session_rebuild"] = True

    def plan(self, intent):
        imsi = intent["target"]["imsi"]
        sm = db.json_one(f"SELECT singleNssai FROM {SM_T} WHERE ueid={db.quote(imsi)}") or {}
        return [f"读 {imsi} 的 K/OPc 与签约切片 {sm.get('sst')}.{sd_hex(sm.get('sd'))}",
                "写 ue.conf(imsi / key / opc / nssai_sst / nssai_sd)",
                "重启 oran-ue 并等待 PDU Session Establishment Accept(断当前 UE 会话)",
                "验证:UE 日志里的 IMSI 与 NSSAI 是新订户、能 ping 通 ext-dn"]

    def snapshot(self, intent):
        return {"kind": "switch_ue", "imsi": intent["target"]["imsi"],
                "ue_conf": _backup([UE_CONF])}

    def execute(self, intent):
        imsi = intent["target"]["imsi"]
        sm = db.json_one(f"SELECT singleNssai FROM {SM_T} WHERE ueid={db.quote(imsi)}") or {}
        sst, sd = int(sm.get("sst", 1)), sd_int(sm.get("sd"))
        key, opc = _creds(imsi)
        _set_ue_conf(imsi, key, opc, sst, sd)
        return {"imsi": imsi, "sst": sst, "sd": sd_hex(sd), "rebuild": ue.restart_and_wait()}

    def config_footprint(self, intent):
        """switch_ue 只改 ue.conf：IMSI 与 UE 请求的 NSSAI。
        K/OPc 不入持久层（避免在 state 里再存一份密钥，可从订户表反推）。"""
        return [
            {"kind": "file_key", "file": UE_CONF,
             "pattern": r'imsi\s*=\s*"(\d{15})"', "label": "ue.conf imsi", "service": "oran-ue"},
            {"kind": "file_key", "file": UE_CONF,
             "pattern": r"nssai_sst\s*=\s*(\d+)", "label": "ue.conf nssai_sst", "service": "oran-ue"},
            {"kind": "file_key", "file": UE_CONF,
             "pattern": r"nssai_sd\s*=\s*(0x[0-9a-fA-F]+|\d+)", "label": "ue.conf nssai_sd", "service": "oran-ue"},
        ]

    def verify(self, intent, exec_result):
        imsi = exec_result["imsi"]
        r = exec_result.get("rebuild") or {}
        nssai = _session_nssai()
        checks = [{"check": f"ue.conf 的 IMSI = {imsi}", "ok": f'imsi = "{imsi}"' in open(UE_CONF).read()},
                  {"check": f"UE 请求切片 = {exec_result['sst']}.{exec_result['sd']}",
                   "ok": nssai == (exec_result["sst"], sd_int(exec_result["sd"])),
                   "detail": f"实际 {nssai[0]}.{sd_hex(nssai[1])}" if nssai else "无 NSSAI 行"},
                  {"check": "UE 已重建会话", "ok": bool(r.get("ok")),
                   "detail": f"{r.get('waited')}s, IP {r.get('ip')}"}]
        if r.get("ok"):
            checks.append({"check": "用户面可达 ext-dn", "ok": ue.ping_ok(src=ue.TUN)})
        return checks

    def rollback(self, intent, snapshot):
        _restore(snapshot["ue_conf"])
        r = ue.restart_and_wait()
        return f"已恢复上一个 UE 配置;会话重建{'成功' if r.get('ok') else '失败'}({r.get('waited')}s)"


from .actions import ACTIONS  # noqa: E402

for _a in (ActivateSlice(), SwitchUe()):
    ACTIONS[_a.name] = _a
