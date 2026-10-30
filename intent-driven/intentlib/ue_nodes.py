"""多 UE 节点管理：每个 UE 跑在自己的 network namespace 里。

为什么必须隔离：
  · 一个 nr-uesoftmodem 进程 = 一个 UE；
  · tun 名由 tun_generate_ifname("oaitun_ue", instance_id) 合成，单进程实例号恒为 1
    → 再起一个进程就会撞 `oaitun_ue1`；
  · 放进独立 netns 后，各自的 oaitun_ue1 互不冲突，rfsim 走 veth 连宿主（DU 的 4043 监听 *）。

约定（写死在这里，动作/持久化/UI 都按这个来）：
  第 idx 个 UE: netns = ue{idx}, 宿主侧 10.250.{idx}.1/24, netns 侧 10.250.{idx}.2/24
  配置文件: /home/ubuntu/oai-ran-conf/ue{idx}.conf
  systemd : oran-ue{idx}.service（ExecStart 用 ip netns exec）
  日志     : /home/ubuntu/oai-ran-logs/ue{idx}.log
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any, Dict, List

CONF_DIR = "/home/ubuntu/oai-ran-conf"
BASE_CONF = os.path.join(CONF_DIR, "ue.conf")
SETUP_SCRIPT = os.path.join(CONF_DIR, "ue-netns-setup.sh")
BIN = "/home/ubuntu/oai-ran-e2/cmake_targets/ran_build/build/nr-uesoftmodem"
LOGS = "/home/ubuntu/oai-ran-logs"
UNIT_DIR = "/etc/systemd/system"

SETUP_TEMPLATE = """#!/usr/bin/env bash
# 由 intentctl 生成：为第 $1 个 UE 准备独立 netns + veth（幂等，可反复执行）
set -u
IDX="$1"
NS="ue${IDX}"
HOST_IP="10.250.${IDX}.1/24"
NS_IP="10.250.${IDX}.2/24"

ip netns list | grep -qw "$NS" || ip netns add "$NS"
if ! ip link show "veth-$NS" >/dev/null 2>&1; then
  ip link add "veth-$NS" type veth peer name "veth-$NS-ns"
  ip link set "veth-$NS-ns" netns "$NS"
fi
ip addr show dev "veth-$NS" | grep -q "$HOST_IP" || ip addr add "$HOST_IP" dev "veth-$NS"
ip link set "veth-$NS" up
ip netns exec "$NS" ip addr show dev "veth-$NS-ns" | grep -q "$NS_IP" || \
  ip netns exec "$NS" ip addr add "$NS_IP" dev "veth-$NS-ns"
ip netns exec "$NS" ip link set "veth-$NS-ns" up
ip netns exec "$NS" ip link set lo up
"""


def _write_root(path: str, text: str, mode: str = "644") -> None:
    """写需要 root 权限的文件（/etc/systemd/system 等）。"""
    tmp = f"/tmp/.intent-{os.path.basename(path)}.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    rc, out = _sh(["sudo", "install", "-m", mode, "-o", "root", "-g", "root", tmp, path])
    try:
        os.unlink(tmp)
    except OSError:
        pass
    if rc != 0:
        raise OSError(f"写入 {path} 失败: {out}")


def _rm_root(path: str) -> None:
    _sh(["sudo", "rm", "-f", path])


def _sh(cmd: List[str], timeout: int = 60) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or p.stderr or "").strip()


def conf_path(idx: int) -> str:
    return os.path.join(CONF_DIR, f"ue{idx}.conf")


def unit_name(idx: int) -> str:
    return f"oran-ue{idx}.service"


def netns(idx: int) -> str:
    return f"ue{idx}"


def host_ip(idx: int) -> str:
    return f"10.250.{idx}.1"


def existing_indexes() -> List[int]:
    out = []
    for name in os.listdir(UNIT_DIR):
        m = re.fullmatch(r"oran-ue(\d+)\.service", name)
        if m and int(m.group(1)) >= 2:
            out.append(int(m.group(1)))
    return sorted(out)


def next_index() -> int:
    used = set(existing_indexes())
    idx = 2
    while idx in used:
        idx += 1
    return idx


def ensure_setup_script() -> None:
    body = SETUP_TEMPLATE
    if not os.path.exists(SETUP_SCRIPT) or open(SETUP_SCRIPT, encoding="utf-8").read() != body:
        try:
            with open(SETUP_SCRIPT, "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(SETUP_SCRIPT, 0o755)
        except PermissionError:
            _write_root(SETUP_SCRIPT, body, mode="755")


def ensure_netns(idx: int) -> tuple[bool, str]:
    ensure_setup_script()
    rc, out = _sh(["sudo", SETUP_SCRIPT, str(idx)])
    if rc != 0:
        return False, out[-300:]
    return True, ""


def render_conf(idx: int, imsi: str, key: str, opc: str, sst: int, sd: int) -> str:
    """以现有 ue.conf 为模板生成 ue{idx}.conf（替换 IMSI/K/OPc/切片/rfsim 地址）。"""
    text = open(BASE_CONF, encoding="utf-8").read()
    text = re.sub(r'imsi\s*=\s*"\d{15}"', f'imsi = "{imsi}"', text, count=1)
    if key:
        text = re.sub(r'key\s*=\s*"[0-9a-fA-F]*"', f'key  = "{key}"', text, count=1)
    if opc:
        text = re.sub(r'opc\s*=\s*"[0-9a-fA-F]*"', f'opc  = "{opc}"', text, count=1)
    text = re.sub(r"nssai_sst\s*=\s*\d+", f"nssai_sst = {sst}", text, count=1)
    text = re.sub(r"nssai_sd\s*=\s*(0x[0-9a-fA-F]+|\d+)", f"nssai_sd = 0x{sd:06x}", text, count=1)
    text = re.sub(r'serveraddr\s*=\s*"[0-9.]*"', f'serveraddr = "{host_ip(idx)}"', text, count=1)
    path = conf_path(idx)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, 0o644)
    except PermissionError:
        _write_root(path, text)
    return path


def render_unit(idx: int, imsi: str) -> str:
    ns = netns(idx)
    unit = f"""[Unit]
Description=OAI nrUE node #{idx} (netns {ns}, IMSI {imsi})
After=network-online.target oran-du.service
Wants=oran-du.service

[Service]
Type=simple
# netns/veth 是易失资源，每次启动前幂等重建
ExecStartPre={SETUP_SCRIPT} {idx}
ExecStart=/usr/sbin/ip netns exec {ns} {BIN} -O {conf_path(idx)} -r 106 --numerology 1 --band 78 -C 3450720000 --rfsim
Restart=always
RestartSec=10
TimeoutStopSec=15
KillMode=mixed
StandardOutput=append:{LOGS}/ue{idx}.log
StandardError=append:{LOGS}/ue{idx}.log

[Install]
WantedBy=multi-user.target
"""
    path = os.path.join(UNIT_DIR, unit_name(idx))
    fresh = not os.path.exists(path)
    _write_root(path, unit)
    if fresh:
        # 同一 idx 之前可能跑过别的节点，日志不清会让人看到上一个 IMSI
        try:
            open(os.path.join(LOGS, f"ue{idx}.log"), "w").close()
        except OSError:
            _sh(["sudo", "truncate", "-s", "0", os.path.join(LOGS, f"ue{idx}.log")])
    return path


def daemon_reload() -> None:
    _sh(["sudo", "systemctl", "daemon-reload"])


def start(idx: int) -> tuple[bool, str]:
    rc, out = _sh(["sudo", "systemctl", "enable", "--now", unit_name(idx)])
    return rc == 0, out[-300:]


def stop(idx: int, remove: bool = False) -> None:
    _sh(["sudo", "systemctl", "disable", "--now", unit_name(idx)])
    if remove:
        for p in (os.path.join(UNIT_DIR, unit_name(idx)), conf_path(idx)):
            try:
                os.unlink(p)
            except OSError:
                _rm_root(p)
        ns = netns(idx)
        _sh(["sudo", "ip", "netns", "del", ns])
        _sh(["sudo", "ip", "link", "del", f"veth-{ns}"])
        daemon_reload()


def tun_ip(idx: int) -> str | None:
    rc, out = _sh(["sudo", "ip", "netns", "exec", netns(idx), "ip", "-4", "-o", "addr",
                   "show", "oaitun_ue1"], timeout=20)
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out or "")
    return m.group(1) if m else None


def ping(idx: int, target: str = "192.168.73.135") -> bool:
    rc, _ = _sh(["sudo", "ip", "netns", "exec", netns(idx), "ping", "-c", "2", "-W", "2",
                 "-I", "oaitun_ue1", target], timeout=20)
    return rc == 0


def is_active(idx: int) -> bool:
    rc, out = _sh(["systemctl", "is-active", unit_name(idx)], timeout=15)
    return out.strip() == "active"


def wait_session(idx: int, timeout: int = 120) -> tuple[str | None, float]:
    t0 = time.time()
    while time.time() - t0 < timeout:
        ip = tun_ip(idx)
        if ip:
            return ip, time.time() - t0
        time.sleep(3)
    return None, time.time() - t0


def status(idx: int) -> Dict[str, Any]:
    return {"index": idx, "unit": unit_name(idx), "netns": netns(idx),
            "active": is_active(idx), "ip": tun_ip(idx), "conf": conf_path(idx),
            "host_ip": host_ip(idx)}


def ensure_node(idx: int, imsi: str, sst: int, sd: int) -> List[str]:
    """幂等保证第 idx 个 UE 节点存在且在跑（持久层 apply 调用）。"""
    from . import db  # 延迟导入，避免循环
    acts: List[str] = []
    ok, err = ensure_netns(idx)
    if not ok:
        return [f"netns ue{idx}: 失败 {err}"]

    want_conf = conf_path(idx)
    if not os.path.exists(want_conf):
        from .actions_slice import _creds
        key, opc = _creds(imsi)
        render_conf(idx, imsi, key, opc, sst, sd)
        acts.append(f"ue{idx}.conf 已重新生成")

    unit = os.path.join(UNIT_DIR, unit_name(idx))
    if not os.path.exists(unit):
        render_unit(idx, imsi)
        daemon_reload()
        acts.append(f"{unit_name(idx)} 已重新生成")

    if not is_active(idx):
        ok, out = start(idx)
        acts.append(f"{unit_name(idx)} 已启动" if ok else f"{unit_name(idx)} 启动失败: {out}")

    return acts or [f"ue{idx} 已在运行"]
