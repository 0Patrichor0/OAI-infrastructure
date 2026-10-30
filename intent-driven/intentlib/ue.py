"""活动 UE 的会话生命周期操作(重建 = 断几十秒,oran-ue-watchdog 会兜底)。"""
from __future__ import annotations

import os
import subprocess
import time

UE_LOG = "/home/ubuntu/oai-ran-logs/ue.log"
TUN = "oaitun_ue1"
EXT_DN = "192.168.73.135"


def _log_offset() -> int:
    try:
        return os.path.getsize(UE_LOG)
    except OSError:
        return 0


def _tail_since(offset: int) -> str:
    try:
        with open(UE_LOG, "rb") as f:
            f.seek(offset)
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def tunnel_ip() -> str | None:
    p = subprocess.run(["ip", "-4", "addr", "show", TUN], capture_output=True, text=True)
    for line in p.stdout.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            return line.split()[1].split("/")[0]
    return None


def ping_ok(target: str = EXT_DN, src: str | None = None, count: int = 2) -> bool:
    cmd = ["ping", "-c", str(count), "-W", "2"]
    if src:
        cmd += ["-I", src]
    cmd.append(target)
    return subprocess.run(cmd, capture_output=True).returncode == 0


def restart_and_wait(timeout: int = 90, attempts: int = 3, restore_all: bool = False) -> dict:
    """重启 UE 并等待重新拿到 PDU 会话。

    UPF/SMF 刚重建时,UE 第一次附着经常失败(AMF 还没重新选到 SMF),所以重试多轮;
    每一轮都重新 systemctl restart,而不是干等。
    """
    total_waited = 0
    evidence = ""
    for attempt in range(1, attempts + 1):
        off = _log_offset()
        t0 = time.time()
        subprocess.run(["sudo", "systemctl", "restart", "oran-ue"], capture_output=True, timeout=120)
        while time.time() - t0 < timeout:
            time.sleep(5)
            chunk = _tail_since(off)
            if "PDU Session Establishment Accept" in chunk:
                evidence = [ln.strip() for ln in chunk.splitlines()
                            if "PDU Session Establishment Accept" in ln][-1]
                time.sleep(3)
                result = {"ok": True, "waited": total_waited + int(time.time() - t0),
                          "ip": tunnel_ip(), "evidence": evidence, "attempts": attempt}
                if restore_all:
                    from . import ue_nodes
                    restored = []
                    for idx in ue_nodes.existing_indexes():
                        if not ue_nodes.is_active(idx): continue
                        p = subprocess.run(["sudo", "systemctl", "restart", ue_nodes.unit_name(idx)], capture_output=True)
                        extra_ip, waited = ue_nodes.wait_session(idx, timeout=90) if p.returncode == 0 else (None, 0)
                        extra_ok = bool(extra_ip) and ue_nodes.ping(idx)
                        restored.append({"index": idx, "ip": extra_ip, "ok": extra_ok})
                        result["ok"] = result["ok"] and extra_ok
                    result["extra_ues"] = restored
                return result
        total_waited += int(time.time() - t0)
        chunk = _tail_since(off)
        errs = [ln.strip() for ln in chunk.splitlines()
                if "FAIL" in ln or "error" in ln.lower() or "Bye" in ln][-3:]
        evidence = f"attempt {attempt}: " + " | ".join(errs)
    return {"ok": False, "waited": total_waited, "ip": tunnel_ip(),
            "evidence": evidence, "attempts": attempts}


def snapshot() -> dict:
    return {"tunnel_ip": tunnel_ip(), "ext_dn_reachable": ping_ok()}
