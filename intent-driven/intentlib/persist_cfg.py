"""非签约类变更的持久化（compose env + 意图自建的辅助容器）。

背景：签约级变更（用户/QoS/切片）已经由 `persist.py` 落成 SQL 在 mysql init 时重放；
但 `boost_capacity`（UPF worker 数）和 `set_egress`（N6 下一跳）改的是 **基础 compose 的 env**，
而 `set_egress` 还会 `docker run` 出第二个 DN 容器。这两类东西：

- compose 文件在磁盘上，down/up 之后还在（所以多半能活下来），但**没人知道"当前期望值是什么"**：
  有人改回去、或从仓库重新拉一份 compose，意图结果就静默丢失；
- 辅助容器（oai-ext-dn2）没有 restart 策略时，重启/重建后不会自己回来。

这里把"意图改过的配置"记成**期望状态**（state/intent_config.json），并由
`apply()` 幂等地把它按回去：compose 值不一致就改回；容器不在就按记录重建；
配置文件里的关键字段（RAN 的 snssaiList、ue.conf 的 imsi/nssai）不一致就改回。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any, Dict, List

from .storage import atomic_text

HOME = os.environ.get("INTENT_HOME", "/home/ubuntu/intent-driven")
STATE = os.path.join(HOME, "state", "intent_config.json")
COMPOSE_DIR = "/home/ubuntu/nwdaf-rel17/oai-cn5g-fed/docker-compose"
COMPOSE_BASE = "docker-compose-basic-vpp-nrf.yaml"


class CfgError(RuntimeError):
    pass


# ---------------------------------------------------------------- 状态读写
def _load() -> Dict[str, Dict[str, Any]]:
    try:
        with open(STATE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    data.setdefault("compose_env", {})
    data.setdefault("containers", {})
    data.setdefault("files", {})
    data.setdefault("core_slices", {})
    data.setdefault("ue_nodes", {})
    return data


def _save(data: Dict[str, Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    atomic_text(STATE, json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))


def state() -> Dict[str, Dict[str, Any]]:
    return _load()


# ---------------------------------------------------------------- compose env
def _compose_path() -> str:
    return os.path.join(COMPOSE_DIR, COMPOSE_BASE)


def compose_env_get(key: str) -> str | None:
    m = re.search(rf"-\s*{key}=([^\s#]+)", open(_compose_path(), encoding="utf-8").read())
    return m.group(1) if m else None


def compose_env_set(key: str, value: str) -> str | None:
    """改基础 compose 的值，返回旧值。改基础文件而不是 override：
    start-all / ue-repair 重建时只读基础文件，override 会被丢掉。"""
    path = _compose_path()
    text = open(path, encoding="utf-8").read()
    m = re.search(rf"(-\s*{key}=)([^\s#]+)", text)
    if not m:
        raise CfgError(f"compose 里找不到 {key}")
    old = m.group(2)
    if old == value:
        return old
    text = text[:m.start(2)] + value + text[m.end(2):]
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return old


def record_env(key: str, value: str, service: str, intent_id: str | None = None) -> None:
    data = _load()
    data["compose_env"][key] = {
        "file": COMPOSE_BASE, "service": service, "value": value,
        "intent": intent_id, "recorded_at": time.strftime("%F %T"),
    }
    _save(data)


def record_container(name: str, intent_id: str | None = None) -> bool:
    """把容器当前的启动参数存下来（够重建即可）。"""
    p = subprocess.run(["docker", "inspect", name], capture_output=True, text=True)
    if p.returncode != 0:
        return False
    info = json.loads(p.stdout)[0]
    cfg, host = info["Config"], info["HostConfig"]
    nets = []
    for net, ep in (info["NetworkSettings"].get("Networks") or {}).items():
        nets.append({"network": net, "ip": ep.get("IPAddress") or None})
    spec = {
        "image": cfg.get("Image"),
        "cmd": cfg.get("Cmd") or [],
        "entrypoint": cfg.get("Entrypoint") or [],
        "env": [e for e in (cfg.get("Env") or []) if not e.startswith(("PATH=", "HOSTNAME="))],
        "privileged": bool(host.get("Privileged")),
        "init": bool(host.get("Init")),
        "restart": (host.get("RestartPolicy") or {}).get("Name") or "no",
        "networks": nets,
    }
    data = _load()
    data["containers"][name] = {"spec": spec, "intent": intent_id,
                                "recorded_at": time.strftime("%F %T")}
    _save(data)
    return True


def forget_env(key: str) -> None:
    data = _load()
    data["compose_env"].pop(key, None)
    _save(data)


def forget_container(name: str) -> None:
    data = _load()
    data["containers"].pop(name, None)
    _save(data)


# ---------------------------------------------------------------- 配置文件关键字段
# 为什么需要这一层：activate_slice / switch_ue 改的是 cucp/cuup/du.conf 的 snssaiList
# 和 ue.conf 的 imsi / nssai_*，这些不在 compose 里、也不在数据库里。
# 文件在磁盘上能活过 down/up，但一旦有人重跑 gen_configs.py 或换回旧配置，就静默丢失。
def record_file(path: str, pattern: str, intent_id: str | None = None,
                label: str = "", service: str = "") -> bool:
    """把文件里当前匹配到的值记下来（pattern 必须且只能有一个捕获组）。"""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return False
    m = re.search(pattern, text)
    if not m or m.lastindex is None:
        return False
    data = _load()
    data["files"][f"{path}::{pattern}"] = {
        "file": path, "pattern": pattern, "value": m.group(1),
        "label": label or os.path.basename(path), "service": service,
        "intent": intent_id, "recorded_at": time.strftime("%F %T"),
    }
    _save(data)
    return True


def record_ue_node(index: int, imsi: str, sst: int, sd: str,
                   intent_id: str | None = None) -> None:
    """记录一个“应当存在并运行的 UE 节点”。netns/veth 是易失资源，
    容器或宿主重启后由 apply() 重建。"""
    data = _load()
    data["ue_nodes"][str(index)] = {"index": int(index), "imsi": imsi,
                                    "sst": int(sst), "sd": str(sd),
                                    "intent": intent_id,
                                    "recorded_at": time.strftime("%F %T")}
    _save(data)


def forget_ue_node(index: int) -> None:
    data = _load()
    data["ue_nodes"].pop(str(index), None)
    _save(data)


def record_core_slice(sst: int, sd: str, intent_id: str | None = None) -> None:
    """记录“AMF/SMF 必须认识这个切片”。core 配置结构复杂（anchor + 三处引用），
    不适合用 file_key 正正则匹配，所以单独记一条，apply 时用 add_core_slice 补。"""
    data = _load()
    data["core_slices"][f"{sst}/{sd}"] = {"sst": int(sst), "sd": sd,
                                          "intent": intent_id,
                                          "recorded_at": time.strftime("%F %T")}
    _save(data)


def forget_core_slice(sst: int, sd: str) -> None:
    data = _load()
    data["core_slices"].pop(f"{sst}/{sd}", None)
    _save(data)


def forget_file(path: str, pattern: str) -> None:
    data = _load()
    data["files"].pop(f"{path}::{pattern}", None)
    _save(data)


def _apply_file(rec: dict) -> str:
    path, pat, want = rec["file"], rec["pattern"], rec["value"]
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return f"文件不存在: {path}"
    m = re.search(pat, text)
    if not m:
        return f"按 pattern 找不到目标: {os.path.basename(path)}"
    if m.group(1) == want:
        return "已是期望值"
    new = text[:m.start(1)] + want + text[m.end(1):]
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    note = f"{os.path.basename(path)}: {m.group(1)!r} → {want!r}"
    if rec.get("service"):
        note += f"（需重启 {rec['service']} 生效）"
    return note


# ---------------------------------------------------------------- 应用
def _container_exists(name: str) -> bool:
    return subprocess.run(["docker", "inspect", name],
                          capture_output=True).returncode == 0


def _image_entrypoint(image: str) -> list[str]:
    p = subprocess.run(["docker", "image", "inspect", "-f", "{{json .Config.Entrypoint}}", image],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return []
    try:
        return json.loads(p.stdout.strip() or "null") or []
    except ValueError:
        return []


def _ensure_container(name: str, spec: Dict[str, Any]) -> str:
    if _container_exists(name):
        return "存在"
    cmd = ["docker", "run", "-d", "--name", name]
    if spec.get("privileged"):
        cmd.append("--privileged")
    if spec.get("init"):
        cmd.append("--init")
    if spec.get("restart") and spec["restart"] != "no":
        cmd += ["--restart", spec["restart"]]
    for kv in spec.get("env") or []:
        cmd += ["-e", kv]
    nets = spec.get("networks") or []
    if nets:
        cmd += ["--network", nets[0]["network"]]
        if nets[0].get("ip"):
            cmd += ["--ip", nets[0]["ip"]]

    # entrypoint 要和镜像默认值比较后再决定要不要覆盖。
    # 直接把容器的 entrypoint 当 --entrypoint 传，会把镜像自带的多段 entrypoint
    # （例：[/bin/bash, /tmp/trfgen_entrypoint.sh]）压成一段，再叠上原 Cmd，
    # 结果容器起来就退（实测 `exec: --: invalid option`）。
    img_ep = _image_entrypoint(spec["image"])
    ep = spec.get("entrypoint") or []
    args = list(spec.get("cmd") or [])
    if ep and ep != img_ep:
        cmd += ["--entrypoint", ep[0]]
        args = ep[1:] + args
    cmd.append(spec["image"])
    cmd += args
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise CfgError(f"重建 {name} 失败: {(p.stderr or p.stdout)[-300:]}")
    time.sleep(3)
    # 注意：容器重建后 IP/MAC 可能变。对端如果缓存了旧邻居（典型是 vpp-upf 的 N6 下一跳），
    # 流量会静默中断 —— 实测把 dn2 重建后 UPF 仍指旧 MAC，dn2 rx 一直 +0B。
    return "已重建（若对端缓存了旧地址，需要重建依赖它的服务，如 vpp-upf）"


def _container_env(name: str) -> list[str]:
    p = subprocess.run(["docker", "inspect", "-f", "{{json .Config.Env}}", name],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return []
    try:
        return json.loads(p.stdout)
    except ValueError:
        return []


def apply(verbose: bool = True) -> List[Dict[str, str]]:
    """把记录下来的期望状态按回去；幂等。返回做过的动作。"""
    data = _load()
    done: List[Dict[str, str]] = []

    for key, rec in sorted(data["compose_env"].items()):
        want = rec["value"]
        cur = compose_env_get(key)
        if cur == want:
            done.append({"item": key, "action": "compose 已是期望值"})
        else:
            compose_env_set(key, want)
            done.append({"item": key, "action": f"compose {cur} → {want}"})
        svc = rec.get("service")
        if svc and not _container_exists(svc):
            # compose 服务被别人删了/从没起来：按 compose（此时已是期望值）拉起来
            p2 = subprocess.run(["docker", "compose", "-f", COMPOSE_BASE,
                                 "up", "-d", "--no-deps", svc],
                                cwd=COMPOSE_DIR, capture_output=True, text=True, timeout=600)
            done.append({"item": key,
                         "action": f"{svc} 不存在，已按 compose 拉起" if p2.returncode == 0
                         else f"拉起 {svc} 失败: {(p2.stderr or p2.stdout)[-160:]}"})
        if svc and _container_exists(svc):
            if f"{key}={want}" not in _container_env(svc):
                done.append({"item": key,
                             "action": f"⚠️ 运行中的 {svc} 仍是旧值，需要重建才生效"})
                if os.environ.get("INTENT_CFG_RECREATE") == "1":
                    p = subprocess.run(["docker", "compose", "-f", COMPOSE_BASE,
                                        "up", "-d", "--no-deps", "--force-recreate", svc],
                                       cwd=COMPOSE_DIR, capture_output=True, text=True, timeout=600)
                    done.append({"item": key,
                                 "action": f"已重建 {svc}" if p.returncode == 0
                                 else f"重建 {svc} 失败: {(p.stderr or p.stdout)[-160:]}"})

    if data.get("core_slices"):
        try:
            from .actions_slice import add_core_slice, core_slice_known
            for k, rec in sorted(data["core_slices"].items()):
                if core_slice_known(rec["sst"], rec["sd"]):
                    done.append({"item": f"core slice {k}", "action": "已在 AMF/SMF 配置里"})
                elif add_core_slice(rec["sst"], rec["sd"]):
                    done.append({"item": f"core slice {k}",
                                 "action": "已补进 AMF/SMF 配置（需重启 oai-amf / oai-smf 生效）"})
        except Exception as exc:  # noqa: BLE001
            done.append({"item": "core slice", "action": f"补写失败: {exc}"})

    if data.get("ue_nodes"):
        try:
            from . import ue_nodes
            from .slices import sd_int
            for k, rec in sorted(data["ue_nodes"].items()):
                acts = ue_nodes.ensure_node(int(k), rec["imsi"], rec["sst"], sd_int(rec["sd"]))
                for a in acts:
                    done.append({"item": f"UE 节点 #{k}", "action": a})
        except Exception as exc:  # noqa: BLE001
            done.append({"item": "UE 节点", "action": f"重建失败: {exc}"})

    for key, rec in sorted(data.get("files", {}).items()):
        done.append({"item": rec.get("label") or rec["file"], "action": _apply_file(rec)})

    for name, rec in sorted(data["containers"].items()):
        try:
            done.append({"item": name, "action": _ensure_container(name, rec["spec"])})
        except CfgError as exc:
            done.append({"item": name, "action": f"失败: {exc}"})

    if verbose:
        for d in done:
            print(f"  [cfg] {d['item']}: {d['action']}")
    return done


def footprint_keys(items):
    for item in items:
        kind = item["kind"]
        if kind == "compose_env": yield "compose_env", item["key"]
        elif kind == "container": yield "containers", item["name"]
        elif kind == "file_key": yield "files", f"{item['file']}::{item['pattern']}"
        elif kind == "core_slice": yield "core_slices", f"{item['sst']}/{item['sd']}"
        elif kind == "ue_node": yield "ue_nodes", str(item["index"])


def assert_current_owner(items, intent_id):
    data = state()
    for group, key in footprint_keys(items):
        owner = (data.get(group, {}).get(key) or {}).get("intent")
        if owner and owner != intent_id:
            raise CfgError(f"{key} 已被更新的意图 {owner} 管理，请先回滚后续意图")


def restore_footprint(items, before):
    data = state()
    for group, key in footprint_keys(items):
        previous = before.get(group, {}).get(key)
        if previous is None:
            data.setdefault(group, {}).pop(key, None)
        else:
            data.setdefault(group, {})[key] = previous
    _save(data)
