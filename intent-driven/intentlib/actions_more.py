"""第二批意图动作:开通业务(切片档案)、调整数据出口、扩容、降时延、SLA 保障。

全部基于本机实测过的落点:
- 核心侧:oai_db 签约 + UPF 容器环境变量(vpp-upf 重建会断会话,属高代价)
- 出口:第二条 ext-dn 容器 + compose override 切 IF_3_IP_REMOTE
- 容量:VPP worker 数(VPP_CORE_WORKER)
- 时延:5QI 档位 + 实测 RTT / KPM DRB.RlcSduDelayDl
- SLA:KPM(DRB.UEThpDl / DRB.RlcSduDelayDl)+ ping,违约则补偿

RAN 侧的 RC/切片/TC 控制在本架构不可用(CU-CP 的 defn.rc.ctrl=NULL,DU 全空,
write_ctrl_rc_sm 只打 printf),因此这里不承诺 RAN 隔离/调度类效果。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any, Dict, List

from . import db, ue
from .actions import (ACTIONS, AUTH_T, SM_DATA, SM_T, Action, IntentError,
                      Q5QI, QARP, QUL, QDL, _extract, _sm_json)

COMPOSE_DIR = "/home/ubuntu/nwdaf-rel17/oai-cn5g-fed/docker-compose"
COMPOSE_BASE = "docker-compose-basic-vpp-nrf.yaml"
UPF = "vpp-upf"
DN1 = "oai-ext-dn"
DN2 = "oai-ext-dn2"
DN2_IP = "192.168.73.136"
DN1_IP = "192.168.73.135"
CORE_NET = "oai-public-core"


# ---------------- 公共工具 ----------------
def _compose(args: List[str], override: str | None = None) -> str:
    cmd = ["sudo", "docker", "compose", "-f", COMPOSE_BASE]
    if override:
        cmd += ["-f", override]
    cmd += args
    p = subprocess.run(cmd, cwd=COMPOSE_DIR, capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise IntentError((p.stderr or p.stdout)[-400:])
    return p.stdout


def _rebuild_if_active(imsi: str, intent: Dict[str, Any]) -> Dict[str, Any] | None:
    if db.active_ue_imsi() != imsi:
        return None
    if not intent.get("constraints", {}).get("allow_session_rebuild", True):
        return None
    r = ue.restart_and_wait()
    r["before"] = r.get("before") or {}
    return r



def _restart_smf(timeout: int = 150) -> bool:
    """UPF 重建后 SMF 会失去 PFCP 关联(日志: PDU session does not have a UPF association),
    必须重启 SMF 才会重新关联;代价是所有 UE 会话丢失。"""
    from .core_recovery import recover_smf
    return recover_smf(timeout)


def _wait_upf(timeout: int = 90) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        p = subprocess.run(["sudo", "docker", "inspect", UPF, "--format",
                            "{{.State.Status}}|{{.State.Health.Status}}"],
                           capture_output=True, text=True)
        if "running|healthy" in p.stdout:
            return True
        time.sleep(4)
    return False


def _rtt(count: int = 20) -> Dict[str, float] | None:
    p = subprocess.run(["ping", "-c", str(count), "-i", "0.2", "-W", "2",
                        "-I", ue.TUN, ue.EXT_DN], capture_output=True, text=True)
    m = re.search(r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", p.stdout)
    if not m:
        return None
    return {"min": float(m.group(1)), "avg": float(m.group(2)),
            "max": float(m.group(3)), "mdev": float(m.group(4)), "loss": 0.0}


def _ping_host(ip: str, count: int = 3) -> bool:
    return subprocess.run(["ping", "-c", str(count), "-W", "2", "-I", ue.TUN, ip],
                          capture_output=True).returncode == 0


def _kpm_db() -> str | None:
    """挑「有 KPM 数据」的那个库。

    本机两个 xApp 各写各的库:xapp_kpm_moni 写 KPM_MeasRecord,
    xapp_gtp_mac_rlc_pdcp_moni 写 MAC_UE/RLC_bearer。按 mtime 取最新会挑错。
    """
    out = subprocess.run(["bash", "-lc",
                          "ls -t /tmp/xapp_db* 2>/dev/null | grep -v -- '-wal$\\|-shm$'"],
                         capture_output=True, text=True).stdout
    for path in out.split():
        n = subprocess.run(["sqlite3", f"file:{path}?mode=ro",
                            "select count(*) from KPM_MeasRecord;"],
                           capture_output=True, text=True).stdout.strip()
        if n.isdigit() and int(n) > 0:
            return path
    return None


def _kpm_avg(meas: str, window_s: int = 60) -> tuple[float | None, int]:
    """返回(均值, 样本数)。

    注意时间戳单位不统一:KPM 库是 epoch **秒**,MAC/PDCP 库是 epoch **微秒**。
    早期按微秒硬编码过滤,导致 KPM 指标永远查不到(看起来像"没有数据")。
    这里先取 max(tstamp) 推断单位。
    """
    path = _kpm_db()
    if not path:
        return None, 0
    mx = subprocess.run(["sqlite3", f"file:{path}?mode=ro",
                         "select coalesce(max(tstamp),0) from KPM_MeasRecord;"],
                        capture_output=True, text=True).stdout.strip()
    try:
        max_ts = int(mx or 0)
    except ValueError:
        max_ts = 0
    unit = 1_000_000 if max_ts > 10**11 else 1
    since = int((time.time() - window_s) * unit)
    sql = (f"select avg(measure_value), count(*) from KPM_MeasRecord "
           f"where meas_name='{meas}' and tstamp>={since};")
    p = subprocess.run(["sqlite3", "-readonly", f"file:{path}?mode=ro", sql],
                       capture_output=True, text=True)
    try:
        avg, n = p.stdout.strip().split("|")
        return (float(avg) if avg not in ("", "None") else None), int(n)
    except (ValueError, AttributeError):
        return None, 0


def _set_qos_fields(imsi: str, p: Dict[str, Any]) -> str:
    ueid = db.resolve_ueid(imsi, [SM_T])
    if not ueid:
        raise IntentError(f"{imsi} 没有签约数据")
    sets = []
    if p.get("fiveQI") is not None:
        sets.append(f"{Q5QI}, {int(p['fiveQI'])}")
    if p.get("arp_priority") is not None:
        sets.append(f"{QARP}, {int(p['arp_priority'])}")
    if p.get("ambr_ul"):
        sets.append(f"{QUL}, {db.quote(p['ambr_ul'])}")
    if p.get("ambr_dl"):
        sets.append(f"{QDL}, {db.quote(p['ambr_dl'])}")
    if sets:
        db.sql(f"UPDATE {SM_T} SET {SM_DATA}=JSON_SET({SM_DATA}, {', '.join(sets)}) "
               f"WHERE ueid={db.quote(ueid)}")
    return ueid


def _dump_sm(imsi: str) -> str:
    ueid = db.resolve_ueid(imsi, [SM_T]) or imsi
    return db.dump_rows(SM_T, f"ueid='{ueid}'")


def _restore_sm(imsi: str, dump: str) -> None:
    ueid = db.resolve_ueid(imsi, [SM_T]) or imsi
    variants = ",".join(db.quote(v) for v in db.imsi_variants(ueid))
    db.sql(f"DELETE FROM {SM_T} WHERE ueid IN ({variants})")
    if dump.strip():
        db.restore(dump)


# ---------------- 1. 开通新业务 / 新切片档案 ----------------
class ProvisionService(Action):
    name = "provision_service"
    description = "开通新业务:下发 S-NSSAI(sst/sd) + 业务 QoS 档案,并在活动 UE 上生效"

    def validate(self, intent):
        self._imsi(intent)
        p = intent.setdefault("params", {})
        if p.get("sst") is None or not (0 <= int(p["sst"]) <= 255):
            raise IntentError("开通业务需要 sst(0-255)")
        if p.get("sd") and not re.fullmatch(r"[0-9a-fA-F]{6}", p["sd"]):
            raise IntentError("sd 必须是 6 位 hex")
        if p.get("fiveQI") is not None and not (1 <= int(p["fiveQI"]) <= 255):
            raise IntentError("fiveQI 超出范围")
        if not db.resolve_ueid(self._imsi(intent), [SM_T]):
            raise IntentError("该 IMSI 没有签约数据,先做 add_subscriber")
        # An omitted SD means keep the subscriber's SD, including during preview.
        from .slices import sd_hex
        p["sst"] = int(p["sst"])
        if not p.get("sd"):
            ueid = db.resolve_ueid(self._imsi(intent), [SM_T])
            current = db.json_one(f"SELECT singleNssai FROM {SM_T} WHERE ueid={db.quote(ueid)}") or {}
            p["sd"] = sd_hex(current.get("sd"))
        else:
            p["sd"] = sd_hex(p["sd"])

    def plan(self, intent):
        p = intent["params"]
        imsi = self._imsi(intent)
        steps = [f"写 S-NSSAI:sst={p['sst']}, sd={p.get('sd', '(保持)')}",
                 "写业务 QoS 档案(5QI/AMBR)" if any(p.get(k) for k in ("fiveQI", "ambr_ul", "ambr_dl")) else "QoS 档案保持不变"]
        if db.active_ue_imsi() == imsi and intent["constraints"].get("allow_session_rebuild", True):
            steps.append("目标是活动 UE:重建 PDU 会话使新业务档案生效(断 ~10s)")
        steps.append("使用核心网、CU/DU、UPF 均已就绪的切片；此动作只修改订户业务签约，不新建网络切片")
        return steps

    def snapshot(self, intent):
        imsi = self._imsi(intent)
        return {"kind": "update_sm", "imsi": imsi, "dump": _dump_sm(imsi),
                "before": _extract(_sm_json(db.resolve_ueid(imsi, [SM_T]) or imsi) or {}),
                "before_nssai": db.json_one(
                    f"SELECT singleNssai FROM {SM_T} WHERE ueid={db.quote(db.resolve_ueid(imsi, [SM_T]) or imsi)}")}

    def execute(self, intent):
        imsi = self._imsi(intent)
        p = intent["params"]
        ueid = db.resolve_ueid(imsi, [SM_T])
        _set_qos_fields(imsi, p)
        sn = [f"'$.sst', {int(p['sst'])}"]
        if p.get("sd"):
            sn.append(f"'$.sd', {db.quote(p['sd'])}")
        db.sql(f"UPDATE {SM_T} SET singleNssai=JSON_SET(singleNssai, {', '.join(sn)}) "
               f"WHERE ueid={db.quote(ueid)}")
        rebuild = _rebuild_if_active(imsi, intent)
        return {"imsi": imsi, "ueid": ueid, "rebuild": rebuild}

    def verify(self, intent, exec_result):
        ueid = exec_result["ueid"]
        p = intent["params"]
        nssai = db.json_one(f"SELECT singleNssai FROM {SM_T} WHERE ueid={db.quote(ueid)}") or {}
        got = _extract(_sm_json(ueid) or {})
        checks = [{"check": f"SST = {p['sst']}", "ok": int(nssai.get("sst", -1)) == int(p["sst"]),
                   "detail": f"实际 {nssai.get('sst')}"}]
        if p.get("sd"):
            checks.append({"check": f"SD = {p['sd']}", "ok": str(nssai.get("sd", "")).lower() == p["sd"].lower(),
                           "detail": f"实际 {nssai.get('sd')}"})
        if p.get("fiveQI") is not None:
            checks.append({"check": f"5QI = {p['fiveQI']}", "ok": int(got["fiveQI"] or -1) == int(p["fiveQI"]),
                           "detail": f"实际 {got['fiveQI']}"})
        if p.get("ambr_dl"):
            checks.append({"check": f"下行 AMBR = {p['ambr_dl']}", "ok": got["ambr_dl"] == p["ambr_dl"],
                           "detail": f"实际 {got['ambr_dl']}"})
        r = exec_result.get("rebuild")
        if r:
            checks.append({"check": "UE 已按新业务档案重建会话", "ok": bool(r.get("ok")),
                           "detail": f"{r.get('waited')}s, IP {r.get('ip')}"})
            if r.get("ok"):
                checks.append({"check": "用户面可达 ext-dn", "ok": ue.ping_ok(src=ue.TUN)})
        return checks

    def rollback(self, intent, snapshot):
        _restore_sm(snapshot["imsi"], snapshot["dump"])
        msg = f"已恢复 S-NSSAI={snapshot['before_nssai']} / QoS={snapshot['before']}"
        r = _rebuild_if_active(snapshot["imsi"], {"constraints": {"allow_session_rebuild": True}})
        if r:
            msg += f";会话重建{'成功' if r.get('ok') else '失败'}({r.get('waited')}s)"
        return msg


# ---------------- 2. 调整数据出口 ----------------

def _dn_rx_bytes(container: str) -> int:
    """取 DN 容器 eth0 的 rx 字节数,用来证明流量真的走了新出口。"""
    p = subprocess.run(["sudo", "docker", "exec", container, "sh", "-c",
                        "awk '/eth0:/ {print $2}' /proc/net/dev"],
                       capture_output=True, text=True)
    try:
        return int(p.stdout.strip().splitlines()[0])
    except (IndexError, ValueError):
        return -1


def _compose_path() -> str:
    return os.path.join(COMPOSE_DIR, COMPOSE_BASE)


def _compose_env_get(key: str) -> str | None:
    m = re.search(rf"-\s*{key}=([^\s#]+)", open(_compose_path(), encoding="utf-8").read())
    return m.group(1) if m else None


def _compose_env_set(key: str, value: str) -> str | None:
    """直接改基础 compose 的 env 值(而不是 override):start-all/ue-repair 重建时会
    用基础文件,override 会被丢掉。返回旧值。"""
    path = _compose_path()
    text = open(path, encoding="utf-8").read()
    m = re.search(rf"(-\s*{key}=)([^\s#]+)", text)
    if not m:
        raise IntentError(f"compose 里找不到 {key}")
    old = m.group(2)
    text = text[:m.start(2)] + value + text[m.end(2):]
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return old


def _compose_env_restore(key: str, old: str) -> None:
    _compose_env_set(key, old)


class SetEgress(Action):
    name = "set_egress"
    description = "调整数据出口:起第二条 DN,切 UPF 的 N6 下一跳(重建 UPF,断会话)"

    def _current_remote(self) -> str:
        return _compose_env_get("IF_3_IP_REMOTE") or ""

    def _dn2_ip(self) -> str | None:
        p = subprocess.run(["sudo", "docker", "inspect", DN2, "--format",
                            "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"],
                           capture_output=True, text=True)
        return p.stdout.strip() or None if p.returncode == 0 else None

    def validate(self, intent):
        p = intent.setdefault("params", {})
        target = p.get("target_dn_ip") or DN2_IP
        if target not in (DN1_IP,DN2_IP):
            raise IntentError(f"出口仅支持已知实验 DN：{DN1_IP} / {DN2_IP}")
        p["target_dn_ip"] = target
        if target == self._current_remote():
            raise IntentError(f"N6 下一跳已经是 {target},无需变更")
        if not intent.setdefault("constraints", {}).get("allow_upf_recreate", False):
            raise IntentError("调整出口要重建 vpp-upf(断所有会话);请显式设置 "
                              "constraints.allow_upf_recreate=true")
        intent["constraints"].setdefault("allow_session_rebuild", True)

    def plan(self, intent):
        t = intent["params"]["target_dn_ip"]
        return [f"确认/启动第二条 DN 容器 {DN2}({t})在 {CORE_NET} 上,带 12.1.1.0/24 回程路由",
                f"改基础 compose:IF_3_IP_REMOTE {self._current_remote()} → {t}(这样 start-all 重建也不会丢)",
                f"重建 {UPF}(代价:所有 UE 会话中断)",
                "重建 UE 会话",
                '先恢复 SMF 与 PFCP 关联，再恢复 NWDAF 订阅及全部 UE',
                '核对 VPP N6 路由，并按目标 UE/目的地址抓取探测包；失败自动恢复原出口']

    def _ensure_dn2(self, ip: str) -> str:
        cur = self._dn2_ip()
        if cur:
            if cur.strip()!=ip:
                raise IntentError(f'{DN2} 地址是 {cur}，与目标 {ip} 不符，未复用')
            running = subprocess.run(['sudo','docker','inspect',DN2,'--format','{{.State.Running}}'],capture_output=True,text=True).stdout.strip()
            if running!='true':
                raise IntentError(f'{DN2} 未运行，未修改出口')
            return f"复用已有 {DN2}({cur})"
        cmd = ["sudo", "docker", "run", "-d", "--name", DN2, "--privileged", "--init",
               "--network", CORE_NET, "--ip", ip,
               "oaisoftwarealliance/trf-gen-cn5g:latest", "/bin/bash", "-c",
               "iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE;"
               "ip route add 12.1.1.0/24 via 192.168.73.201 dev eth0; sleep infinity"]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            raise IntentError(f"起 {DN2} 失败: {(p.stderr or p.stdout)[-300:]}")
        time.sleep(4)
        return f"已启动 {DN2} @ {ip}"

    def config_footprint(self, intent):
        return [{"kind": "compose_env", "key": "IF_3_IP_REMOTE",
                 "value": _compose_env_get("IF_3_IP_REMOTE"), "service": UPF},
                {"kind": "container", "name": DN2}]

    def snapshot(self, intent):
        return {"kind": "egress", "key": "IF_3_IP_REMOTE",
                "old": self._current_remote(),
                "dn2_existed": self._dn2_ip() is not None,
                "compose_file": _compose_path()}

    def execute(self, intent):
        from . import core_recovery
        from .storage import atomic_text
        from pathlib import Path
        ip = intent["params"]["target_dn_ip"]
        old, existed = self._current_remote(), self._dn2_ip() is not None
        changed = False
        try:
            note = self._ensure_dn2(ip) if ip==DN2_IP else '使用原 DN'
            if ip==DN2_IP:
                # Off-link local endpoint proves the UE traverses the N6 next hop.
                rc = subprocess.run(['sudo','docker','exec',DN2,'ip','addr','replace','198.18.0.2/32','dev','lo'],capture_output=True).returncode
                if rc:raise IntentError('无法配置 DN2 受控探测地址，未切换出口')
            _compose_env_set('IF_3_IP_REMOTE',ip)
            changed = True
            _compose(['up','-d','--no-deps','--force-recreate',UPF])
            if not _wait_upf():raise IntentError('UPF 未恢复 healthy')
            if not _restart_smf():raise IntentError('SMF/PFCP 关联未稳定恢复')
            imsi = db.active_ue_imsi()
            rebuild = ue.restart_and_wait(restore_all=True) if imsi else None
            result = {'dn2':note,'target':ip,'rebuild':rebuild}
            checks = self.verify(intent,result)
            if not all(c['ok'] for c in checks):
                raise IntentError('；'.join(c['check'] for c in checks if not c['ok']))
            result['transaction_checks'] = checks
            return result
        except Exception as exc:
            evidence = {'error':str(exc),'before_recovery':core_recovery.diagnostics()}
            recovery_error = None
            try:
                if changed:
                    _compose_env_restore('IF_3_IP_REMOTE',old)
                    _compose(['up','-d','--no-deps','--force-recreate',UPF])
                    if not _wait_upf() or not _restart_smf():raise IntentError('原出口核心网未恢复')
                    restored = ue.restart_and_wait(restore_all=True)
                    if not restored.get('ok') or not ue.ping_ok(src=ue.TUN):raise IntentError('原出口 UE 通信未恢复')
                if not existed:
                    subprocess.run(['sudo','docker','rm','-f',DN2],capture_output=True,timeout=30)
            except Exception as restore_exc:
                recovery_error = str(restore_exc)
            evidence.update(after_recovery=core_recovery.diagnostics(),recovery_error=recovery_error)
            path = Path(os.environ.get('INTENT_HOME','/home/ubuntu/intent-driven'))/'logs'/f'egress-failure-{time.time_ns()}.json'
            atomic_text(path,json.dumps(evidence,ensure_ascii=False))
            raise IntentError(f"出口切换失败：{exc}；"+('恢复失败：'+recovery_error if recovery_error else '已恢复原出口')+f'；诊断 {path}') from exc

    def verify(self, intent, exec_result):
        t = exec_result["target"]
        env = subprocess.run(["sudo", "docker", "inspect", UPF, "--format",
                              "{{range .Config.Env}}{{println .}}{{end}}"],
                             capture_output=True, text=True).stdout
        checks = [{"check": f"UPF 的 IF_3_IP_REMOTE = {t}", "ok": f"IF_3_IP_REMOTE={t}" in env}]
        r = exec_result.get("rebuild")
        checks.append({'check':'全部在线 UE 会话恢复','ok':bool(r and r.get('ok')),
                       'detail':str((r or {}).get('extra_ues',[]))})
        fib = subprocess.run(['sudo','docker','exec',UPF,'/openair-upf/bin/vppctl','show','ip','fib','table','1'],capture_output=True,text=True,timeout=15).stdout
        default = re.search(r'0\.0\.0\.0/0\s*\n(.*?)(?=\n\S|\Z)',fib,re.S)
        checks.append({'check':'VPP N6 默认路由使用目标下一跳','ok':bool(default and f'ipv4 via {t} host-n6-3' in default[1])})
        if r:
            checks.append({"check": "UE 会话已重建", "ok": bool(r.get("ok")),
                           "detail": f"{r.get('waited')}s, IP {r.get('ip')}"})
        if r and r.get("ok"):
            # 注意:DN2 是容器路由器,ping 旧出口仍可能通,所以不能用"ping 不通"当判据。
            # 正确判据:UE 打新出口的包是否真的进了 DN2 的网卡。
            destination = '198.18.0.2' if t==DN2_IP else DN1_IP
            dn = DN2 if t==DN2_IP else DN1
            source = ue.tunnel_ip()
            capture = subprocess.Popen(['sudo','docker','exec',dn,'timeout','8','tcpdump','-l','-n','-i','eth0','-c','1',
                f'icmp and src host {source} and dst host {destination} and icmp[icmptype] = icmp-echo'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            time.sleep(1)
            ok_ping = _ping_host(destination)
            packets, _ = capture.communicate(timeout=12)
            checks.append({"check": f"UE ping 新出口 {t} 通", "ok": ok_ping})
            checks.append({'check':'目标 DN 收到指定 UE 的探测包','ok':capture.returncode==0 and f'{source} > {destination}' in packets,
                           'detail':packets.strip()[-300:]})
        return checks

    def rollback(self, intent, snapshot):
        _compose_env_restore(snapshot["key"], snapshot["old"])
        _compose(["up", "-d", "--no-deps", "--force-recreate", UPF])
        if not _wait_upf() or not _restart_smf():raise IntentError('出口回滚后核心网未恢复')
        if not snapshot.get("dn2_existed"):
            subprocess.run(["sudo", "docker", "rm", "-f", DN2], capture_output=True)
        imsi = db.active_ue_imsi()
        r = ue.restart_and_wait(restore_all=True) if imsi else None
        if not r or not r.get('ok') or not ue.ping_ok(src=ue.TUN):raise IntentError('出口回滚后 UE 通信未恢复')
        return (f"N6 下一跳已恢复 {snapshot['old']}"
                + (";会话重建" + ("成功" if r and r.get("ok") else "失败") if r else "")
                + (";已清理 " + DN2 if not snapshot.get("dn2_existed") else ""))


class BoostCapacity(Action):
    name = "boost_capacity"
    description = "扩容:调整 UPF 的 VPP worker 数(重建 UPF,断会话)"

    def _current_workers(self) -> int:
        v = _compose_env_get("VPP_CORE_WORKER") or ""
        return len([x for x in v.split(",") if x.strip()]) if v else -1

    @staticmethod
    def _corelist(n: int) -> str:
        # 这个镜像的模板是 corelist-workers $VPP_CORE_WORKER,值是**核列表**而不是数量
        return ",".join(str(i) for i in range(1, n + 1))

    def validate(self, intent):
        p = intent.setdefault("params", {})
        w = p.get("workers")
        if w is None:
            raise IntentError("需要 workers(目标 worker 数,当前 1)")
        if not (1 <= int(w) <= 8):
            raise IntentError("workers 取 1-8")
        p["workers"] = int(w)
        if not intent.setdefault("constraints", {}).get("allow_upf_recreate", False):
            raise IntentError("扩容要重建 vpp-upf(断所有会话);请设置 constraints.allow_upf_recreate=true")
        intent["constraints"].setdefault("allow_session_rebuild", True)

    def plan(self, intent):
        w = intent["params"]["workers"]
        return [f"当前 UPF worker 数 {self._current_workers()} → 目标 {w}",
                f"改基础 compose 的 VPP_CORE_WORKER={w} 并重建 {UPF}(断所有会话)",
                "重建 UE 会话",
                "验证:容器环境 + 生成的 VPP startup 配置 corelist-workers + 用户面仍通"]

    def snapshot(self, intent):
        return {"kind": "capacity", "key": "VPP_CORE_WORKER",
                "old": _compose_env_get("VPP_CORE_WORKER"), "compose_file": _compose_path()}

    def execute(self, intent):
        w = intent["params"]["workers"]
        if w == self._current_workers():
            return {"workers": w, "noop": True, "rebuild": None}
        _compose_env_set("VPP_CORE_WORKER", self._corelist(w))
        _compose(["up", "-d", "--no-deps", "--force-recreate", UPF])
        if not _wait_upf():
            raise IntentError("vpp-upf 重建后未 healthy")
        if not _restart_smf():
            raise IntentError("vpp-upf 起来了但 SMF 未恢复 healthy(需要人工看 N4)")
        imsi = db.active_ue_imsi()
        rebuild = ue.restart_and_wait(restore_all=True) if imsi and intent["constraints"].get("allow_session_rebuild", True) else None
        return {"workers": w, "rebuild": rebuild}

    def config_footprint(self, intent):
        return [{"kind": "compose_env", "key": "VPP_CORE_WORKER",
                 "value": _compose_env_get("VPP_CORE_WORKER"), "service": UPF}]

    def verify(self, intent, exec_result):
        w = exec_result["workers"]
        env = subprocess.run(["sudo", "docker", "inspect", UPF, "--format",
                              "{{range .Config.Env}}{{println .}}{{end}}"],
                             capture_output=True, text=True).stdout
        corelist = self._corelist(w)
        checks = [{"check": f"UPF 环境 VPP_CORE_WORKER={corelist}",
                   "ok": f"VPP_CORE_WORKER={corelist}" in env}]
        startup = subprocess.run(["sudo", "docker", "exec", UPF, "sh", "-c",
                                  "sed -n '/cpu {/,/}/p' /openair-upf/etc/startup*.conf 2>/dev/null"],
                                 capture_output=True, text=True).stdout
        m = re.search(r"corelist-workers\s+([\d,\-]+)", startup)
        if m:
            got = len([x for x in m.group(1).split(",") if x.strip()])
            checks.append({"check": f"VPP startup worker 数 = {w}", "ok": got == w,
                           "detail": f"corelist-workers {m.group(1)}"})
        r = exec_result.get("rebuild")
        if r:
            checks.append({"check": "UE 会话已重建", "ok": bool(r.get("ok")),
                           "detail": f"{r.get('waited')}s, IP {r.get('ip')}"})
        if r and r.get("ok"):
            checks.append({"check": "用户面可达 ext-dn", "ok": ue.ping_ok(src=ue.TUN)})
            avg, n = _kpm_avg("DRB.UEThpUl", 60)
            if n:
                checks.append({"check": "KPM 吞吐仍在采样", "ok": True,
                               "detail": f"DRB.UEThpUl 近 60s 均值 {avg:.0f}({n} 点)"})
        return checks

    def rollback(self, intent, snapshot):
        _compose_env_restore(snapshot["key"], snapshot["old"])
        _compose(["up", "-d", "--no-deps", "--force-recreate", UPF])
        _wait_upf()
        _restart_smf()
        imsi = db.active_ue_imsi()
        r = ue.restart_and_wait(restore_all=True) if imsi else None
        return (f"worker 数已恢复 {snapshot['old']}"
                + (";会话重建" + ("成功" if r and r.get("ok") else "失败") if r else ""))


# ---------------- 4. 降时延 ----------------
class ReduceLatency(Action):
    name = "reduce_latency"
    description = "降低时延:切低时延 5QI 档位,并用实测 RTT/KPM 时延对比"

    def validate(self, intent):
        self._imsi(intent)
        p = intent.setdefault("params", {})
        p.setdefault("fiveQI", 7)          # 会话类/低时延档
        if db.active_ue_imsi() != self._imsi(intent):
            raise IntentError("低时延实测目前只支持主 UE 的 IMSI，请先切换主 UE")
        if not (1 <= int(p["fiveQI"]) <= 255):
            raise IntentError("fiveQI 超出范围")
        if not db.resolve_ueid(self._imsi(intent), [SM_T]):
            raise IntentError("该 IMSI 没有签约数据")

    def plan(self, intent):
        p = intent["params"]
        cur = _extract(_sm_json(db.resolve_ueid(self._imsi(intent), [SM_T]) or "") or {})
        steps = [f"记录基线:RTT 20 次采样 + KPM DRB.RlcSduDelayDl",
                 f"5QI {cur.get('fiveQI')} → {p['fiveQI']}(低时延档)"]
        if p.get("ambr_dl") or p.get("ambr_ul"):
            steps.append(f"AMBR 同步调整:{p.get('ambr_ul', '-')} / {p.get('ambr_dl', '-')}")
        if db.active_ue_imsi() == self._imsi(intent) and intent["constraints"].get("allow_session_rebuild", True):
            steps.append("重建 PDU 会话生效(断 ~10s)")
        steps.append("本动作修改核心网 QoS；不包含 PRB 调度或 RAN 切片隔离，RTT 是否改善必须实测")
        return steps

    def snapshot(self, intent):
        imsi = self._imsi(intent)
        return {"kind": "latency", "imsi": imsi, "dump": _dump_sm(imsi),
                "before": _extract(_sm_json(db.resolve_ueid(imsi, [SM_T]) or "") or {}),
                "rtt_before": _rtt(), "delay_before": _kpm_avg("DRB.RlcSduDelayDl", 60)[0]}

    def execute(self, intent):
        p = intent["params"]
        imsi = self._imsi(intent)
        ueid = _set_qos_fields(imsi, p)
        rebuild = _rebuild_if_active(imsi, intent)
        return {"imsi": imsi, "ueid": ueid, "rebuild": rebuild}

    def verify(self, intent, exec_result):
        p = intent["params"]
        got = _extract(_sm_json(exec_result["ueid"]) or {})
        checks = [{"check": f"5QI = {p['fiveQI']}", "ok": int(got["fiveQI"] or -1) == int(p["fiveQI"]),
                   "detail": f"实际 {got['fiveQI']}"}]
        r = exec_result.get("rebuild")
        if r:
            checks.append({"check": "UE 会话已重建", "ok": bool(r.get("ok")),
                           "detail": f"{r.get('waited')}s, IP {r.get('ip')}"})
        if r and r.get("ok"):
            after = _rtt() or {}
            delay_after = _kpm_avg("DRB.RlcSduDelayDl", 60)[0]
            checks.append({"check": "用户面可达 ext-dn", "ok": ue.ping_ok(src=ue.TUN)})
            checks.append({"check": "RTT 采样完成", "ok": bool(after),
                           "detail": f"avg {after.get('avg')}ms / max {after.get('max')}ms"})
            if delay_after is not None:
                checks.append({"check": "KPM 下行 RLC 时延可读", "ok": True,
                               "detail": f"{delay_after:.3f}ms(近 60s 均值)"})
        return checks

    def rollback(self, intent, snapshot):
        _restore_sm(snapshot["imsi"], snapshot["dump"])
        msg = f"已恢复 5QI={snapshot['before'].get('fiveQI')} / AMBR={snapshot['before'].get('ambr_dl')}"
        r = _rebuild_if_active(snapshot["imsi"], {"constraints": {"allow_session_rebuild": True}})
        if r:
            msg += f";会话重建{'成功' if r.get('ok') else '失败'}({r.get('waited')}s)"
        return msg


# ---------------- 5. SLA 保障 ----------------
class SlaGuard(Action):
    name = "sla_guard"
    description = "保障 SLA:按 KPI 判定是否违约,违约则补偿(改 QoS)并复测"

    METRICS = {
        "thp_dl": ("DRB.UEThpDl", "min"),        # 吞吐:不低于阈值(value >= thr)
        "delay_dl": ("DRB.RlcSduDelayDl", "max"),  # 时延:不高于阈值(value <= thr)
        "rtt": ("rtt", "max"),
    }

    def validate(self, intent):
        self._imsi(intent)
        p = intent.setdefault("params", {})
        if p.get("metric") not in self.METRICS:
            raise IntentError(f"metric 只支持 {', '.join(self.METRICS)}")
        if db.active_ue_imsi() != self._imsi(intent):
            raise IntentError("SLA 实测目前只支持主 UE 的 IMSI，请先切换主 UE")
        if p["metric"] != "rtt":
            raise IntentError("KPM 尚未按 IMSI 关联，暂不能据此验证单用户 SLA；当前支持主 UE 的 RTT")
        if p.get("threshold") is None:
            raise IntentError("需要 threshold")
        p["window_s"] = int(p.get("window_s", 60))
        comp = p.setdefault("compensation", {})
        if not comp:
            raise IntentError("需要 compensation(如 {fiveQI:7, ambr_dl:'200Mbps'})")
        if not db.resolve_ueid(self._imsi(intent), [SM_T]):
            raise IntentError("该 IMSI 没有签约数据")

    def _measure(self, metric: str, window_s: int) -> float | None:
        name, _ = self.METRICS[metric]
        if name == "rtt":
            r = _rtt(10)
            return r["avg"] if r else None
        return _kpm_avg(name, window_s)[0]

    def _violated(self, metric: str, value: float | None, threshold: float) -> bool:
        if value is None:
            return True
        _, sense = self.METRICS[metric]
        return value < threshold if sense == "min" else value > threshold

    def plan(self, intent):
        p = intent["params"]
        name, sense = self.METRICS[p["metric"]]
        rel = ">=" if sense == "min" else "<="
        return [f"采样 {name}(窗口 {p['window_s']}s),判据:{rel} {p['threshold']}",
                f"若违约:补偿 {json.dumps(p['compensation'], ensure_ascii=False)} 并重建会话",
                "复测同一 KPI,确认回到阈值内;若仍不达标则如实报告未达成"]

    def snapshot(self, intent):
        imsi = self._imsi(intent)
        p = intent["params"]
        return {"kind": "sla", "imsi": imsi, "dump": _dump_sm(imsi),
                "before": _extract(_sm_json(db.resolve_ueid(imsi, [SM_T]) or "") or {}),
                "metric_before": self._measure(p["metric"], p["window_s"])}

    def execute(self, intent):
        p = intent["params"]
        imsi = self._imsi(intent)
        before = self._measure(p["metric"], p["window_s"])
        violated = self._violated(p["metric"], before, float(p["threshold"]))
        out: Dict[str, Any] = {"metric": p["metric"], "before": before, "violated": violated,
                               "compensated": False, "rebuild": None}
        if not violated:
            return out
        _set_qos_fields(imsi, p["compensation"])
        out["compensated"] = True
        out["rebuild"] = _rebuild_if_active(imsi, intent)
        return out

    def verify(self, intent, exec_result):
        p = intent["params"]
        before = exec_result.get("before")
        checks = [{"check": f"基线采样 {p['metric']}", "ok": before is not None, "detail": f"{before}"}]
        if not exec_result.get("violated"):
            checks.append({"check": f"已达标(无需补偿):{p['metric']} 未越过 {p['threshold']}", "ok": True,
                           "detail": f"{before}"})
            return checks
        r = exec_result.get("rebuild")
        if r:
            checks.append({"check": "补偿后会话已重建", "ok": bool(r.get("ok")),
                           "detail": f"{r.get('waited')}s"})
        after = self._measure(p["metric"], p["window_s"])
        checks.append({"check": f"复测 {p['metric']} 回到阈值内",
                       "ok": not self._violated(p["metric"], after, float(p["threshold"])),
                       "detail": f"{before} → {after}(阈值 {p['threshold']})"})
        return checks

    def rollback(self, intent, snapshot):
        _restore_sm(snapshot["imsi"], snapshot["dump"])
        r = _rebuild_if_active(snapshot["imsi"], {"constraints": {"allow_session_rebuild": True}})
        return (f"已恢复补偿前的 QoS={snapshot['before']}"
                f"{';会话重建' + ('成功' if r and r.get('ok') else '失败') if r else ''}")


for _a in (ProvisionService(), SetEgress(), BoostCapacity(), ReduceLatency(), SlaGuard()):
    ACTIONS[_a.name] = _a
