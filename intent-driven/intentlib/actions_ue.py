"""`add_ue`：新增一个 UE **节点**（不是新增订户）。

语义区别：
  · add_subscriber  → 只往 UDR/MySQL 里加签约，没有新的 UE 进程
  · switch_ue       → 复用唯一那个 UE 进程，把它切到另一个订户（同一时刻仍只有一个 UE）
  · add_ue（本文件）→ 起一个**新的 UE 实例**：独立 netns + 独立 conf + 独立 systemd 单元，
                      可以和已有 UE 同时在线（可跑在不同切片上）

实现依赖 `ue_nodes`（netns/veth/conf/unit 的渲染与生命周期）。
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any, Dict, List

from . import db, ue_nodes
from .actions import AUTH_T, SM_T, Action, IntentError
from .actions_slice import _creds


class AddUe(Action):
    name = "add_ue"
    description = "新增 UE 节点：独立 netns + 独立 nr-uesoftmodem 进程 + 独立 systemd 单元"

    # ---------------------------------------------------------------- 校验
    def validate(self, intent: Dict[str, Any]) -> None:
        tgt = intent.setdefault("target", {})
        imsi = (tgt.get("imsi") or "").strip()
        if not re.fullmatch(r"\d{15}", imsi):
            raise IntentError("需要 target.imsi（15 位数字）")
        p = intent.setdefault("params", {})
        idx = p.get("index")
        if idx is None:
            idx = ue_nodes.next_index()
            p["index"] = idx
        if idx is not None:
            idx = int(idx)
            if not 2 <= idx <= 254:
                raise IntentError("index 必须在 2-254（用于 netns IPv4 地址）")
            if os.path.exists(os.path.join(ue_nodes.UNIT_DIR, ue_nodes.unit_name(idx))):
                raise IntentError(f"oran-ue{idx}.service 已存在，换一个 index 或先回滚")
            p["index"] = idx

        from .slices import all_sessions
        if any(row.get("imsi") == imsi for row in all_sessions()):
            raise IntentError(f"{imsi} 已有活动会话，不能重复创建相同 IMSI 的 UE 节点")
        # 切片：没指定就沿用订户签约里的；订户不存在时也允许，稍后自动开户
        sm_ueid = db.resolve_ueid(imsi, [SM_T])
        p["repair_auth"] = not bool(db.resolve_ueid(imsi, [AUTH_T]))
        if not sm_ueid:
            p.setdefault("create_subscriber", True)
            if p.get("sst") is None:
                raise IntentError(f"{imsi} 还没有签约数据，请显式给 params.sst / params.sd（我会顺带开户）")
        sm = db.json_one(f"SELECT singleNssai FROM {SM_T} WHERE ueid={db.quote(sm_ueid or imsi)}") or {}
        if p.get("sst") is None:
            p["sst"] = int(sm.get("sst"))
            p["sd"] = sm.get("sd")
        if p.get("sd") is None and sm.get("sd") is not None:
            from .slices import sd_hex
            p["sd"] = sd_hex(sm["sd"])
        if p.get("sd") is None:
            raise IntentError("需要 params.sd（六位十六进制，如 000003）")
        # Keep the preview, execution and inventory key representation identical.
        p["sst"] = int(p["sst"])
        p["sd"] = str(p["sd"]).lower()

        # 切片必须已经在 UPF 里注册，否则会话建不起来
        from .slices import upf_snssais
        from .slices import sd_int
        want = (int(p["sst"]), sd_int(p["sd"]))
        # upf_snssais() 里 sd 是 "000003" 这样的字符串，比较前统一成数值
        have = {(x["sst"], sd_int(x["sd"])) for x in upf_snssais()}
        if want not in have:
            raise IntentError(
                f"切片 {want[0]}/{p['sd']} 未在 UPF 注册（已注册 {sorted(have)}）；"
                f"先跑 activate_slice 激活该切片，再 add_ue")
        from .slices import ran_snssais, RAN_CONFS
        ran = next((x for x in ran_snssais() if (x["sst"], sd_int(x["sd"])) == want), None)
        if not ran or not set(RAN_CONFS).issubset(ran.get("conf_files", [])):
            raise IntentError(f"切片 {want[0]}/{p['sd']} 尚未在全部 CU/DU 配置中就绪，请先激活切片")
        if not 0 <= int(p["sst"]) <= 255 or not re.fullmatch(r"[0-9a-fA-F]{6}", str(p["sd"])):
            raise IntentError("SST 必须为 0-255，SD 必须为六位十六进制")
        from .actions import ACTIONS
        sub = self._subscriber_intent(intent, defaults=p.get("create_subscriber", False))
        ACTIONS["add_subscriber"].validate(sub)
        intent.setdefault("constraints", {})["allow_session_rebuild"] = False

    @staticmethod
    def _subscriber_intent(intent, defaults=False):
        p = intent["params"]
        params = {"mode": "ensure", "sst": int(p["sst"]), "sd": str(p["sd"])}
        if defaults:
            params.update(fiveQI=9, ambr_ul="100Mbps", ambr_dl="200Mbps")
        for key in ("fiveQI", "ambr_ul", "ambr_dl", "template_imsi"):
            if p.get(key) is not None:
                params[key] = p[key]
        return {"action": "add_subscriber", "target": dict(intent["target"]),
                "params": params, "constraints": {"allow_session_rebuild": False}}

    # ---------------------------------------------------------------- 计划
    def plan(self, intent: Dict[str, Any]) -> List[str]:
        p = intent["params"]
        imsi = intent["target"]["imsi"]
        idx = p.get("index") or "（自动分配）"
        steps = []
        if p.get("create_subscriber"):
            steps.append(f"{imsi} 还没有签约 → 先按模板开户（切片 {p['sst']}/{p['sd']}）")
        if p.get("repair_auth"):
            steps.append(f"{imsi} 缺少鉴权 → 从实验模板补齐后再启动 UE")
        steps.append(f"建独立 netns ue{idx} + veth（宿主 10.250.{idx}.1/24 ↔ 内 10.250.{idx}.2/24）")
        steps.append(f"生成 /home/ubuntu/oai-ran-conf/ue{idx}.conf（IMSI {imsi}，切片 {p['sst']}/{p['sd']}，rfsim 指宿主 veth）")
        steps.append(f"写 oran-ue{idx}.service（ip netns exec + Restart=always）并 enable --now")
        steps.append("等 PDU Session Establishment Accept（新 UE 拿到自己的 12.1.1.x）")
        steps.append("代价：不影响已有 UE 的会话；只是多一个 UE 上线、占一个 netns 和一个进程")
        return steps

    # ---------------------------------------------------------------- 快照
    def snapshot(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        p = intent["params"]
        idx = p.get("index") or ue_nodes.next_index()
        p["index"] = idx
        imsi = intent["target"]["imsi"]
        existing_ueid = db.resolve_ueid(imsi, [SM_T])
        return {"kind": "add_ue", "index": idx, "imsi": imsi,
                "sm_dump_before": db.dump_rows(SM_T, f"ueid={db.quote(existing_ueid)}") if existing_ueid else "",
                "unit_existed": os.path.exists(os.path.join(ue_nodes.UNIT_DIR, ue_nodes.unit_name(idx))),
                "conf_existed": os.path.exists(ue_nodes.conf_path(idx)),
                "netns_existed": ue_nodes.netns(idx) in _netns_list()}

    # ---------------------------------------------------------------- 执行
    def execute(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        p = intent["params"]
        imsi = intent["target"]["imsi"]
        idx = int(p["index"])
        sst, sd = int(p["sst"]), p["sd"]
        from .slices import sd_int
        sdi = sd_int(sd)

        # 0) 没有签约就先开户（复用 add_subscriber，幂等）
        created_sub = False
        # Reconcile the requested slice and optional QoS fields.
        from .actions import ACTIONS
        sub_intent = self._subscriber_intent(intent, defaults=p.get("create_subscriber", False))
        sub_act = ACTIONS["add_subscriber"]
        sub_act.validate(sub_intent)          # 补 template_imsi / mode 等默认值
        sub_act.execute(sub_intent)
        created_sub = bool(p.get("create_subscriber"))
        key, opc = _creds(imsi)

        # 1) netns + veth
        ok, err = ue_nodes.ensure_netns(idx)
        if not ok:
            raise IntentError(f"netns 建不起来: {err}")

        # 2) conf
        conf = ue_nodes.render_conf(idx, imsi, key, opc, sst, sdi)

        # 3) systemd 单元
        unit = ue_nodes.render_unit(idx, imsi)
        ue_nodes.daemon_reload()

        # 4) 启动
        ok, out = ue_nodes.start(idx)
        if not ok:
            raise IntentError(f"systemctl enable --now 失败: {out}")

        ip, waited = ue_nodes.wait_session(idx, timeout=120)
        return {"index": idx, "imsi": imsi, "sst": sst, "sd": sd, "ip": ip,
                "waited": round(waited, 1), "conf": conf, "unit": unit,
                "created_subscriber": created_sub}

    # ---------------------------------------------------------------- 验证
    def verify(self, intent: Dict[str, Any], exec_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        idx = int(exec_result["index"])
        imsi = exec_result["imsi"]
        sst, sd = int(exec_result["sst"]), exec_result["sd"]
        checks: List[Dict[str, Any]] = []

        checks.append({"check": f"oran-ue{idx}.service 在运行",
                       "ok": ue_nodes.is_active(idx)})

        ip = ue_nodes.tun_ip(idx)
        checks.append({"check": f"netns ue{idx} 里的 oaitun_ue1 拿到地址",
                       "ok": bool(ip), "detail": f"IP {ip}" if ip else "没有 tun 地址"})

        log = os.path.join(ue_nodes.LOGS, f"ue{idx}.log")
        try:
            with open(log, encoding="utf-8", errors="replace") as fh:
                # PHY 统计刷得很快，只读尾部会把 IMSI/Accept 这些开机行挤出去（实测 76KB 就到 20KB 之外了）
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 2_000_000))
                text = fh.read()
        except OSError:
            text = ""
        checks.append({"check": f"UE 用的 IMSI = {imsi}",
                       "ok": f"IMSI={imsi}" in text, "detail": f"日志 {log}"})
        checks.append({"check": "收到 PDU Session Establishment Accept",
                       "ok": "PDU Session Establishment Accept" in text})

        if ip:
            checks.append({"check": f"从 netns ue{idx} ping 通 ext-dn",
                           "ok": ue_nodes.ping(idx)})
        else:
            checks.append({"check": "从 netns ping 通 ext-dn", "ok": False, "detail": "无 tun 地址"})

        from .actions import ACTIONS
        sub_intent = self._subscriber_intent(intent)
        sub_intent["params"]["mode"] = "ensure"
        checks.extend(ACTIONS["add_subscriber"].verify(sub_intent, {"imsi": imsi, "ueid": db.resolve_ueid(imsi, [SM_T]) or imsi}))

        # 与既存 UE 不冲突
        main_ip = _main_ue_ip()
        checks.append({"check": "与主 UE 的地址不冲突",
                       "ok": bool(ip) and ip != main_ip,
                       "detail": f"主 UE {main_ip}，新 UE {ip}"})
        return checks

    # ---------------------------------------------------------------- 回滚
    def rollback(self, intent: Dict[str, Any], snapshot: Dict[str, Any]) -> str:
        idx = int(snapshot["index"])
        # add_ue 在 validate 阶段就拒绝“单元已存在”的情况，所以这里可以整包清理。
        # 之前按 conf_existed 决定是否删除，结果是“停止了但文件还在”，而且提示还写着已删除（假话）。
        ue_nodes.stop(idx, remove=True)
        left = {
            "unit": os.path.exists(os.path.join(ue_nodes.UNIT_DIR, ue_nodes.unit_name(idx))),
            "conf": os.path.exists(ue_nodes.conf_path(idx)),
            "netns": ue_nodes.netns(idx) in _netns_list(),
        }
        msg = [f"已停止 oran-ue{idx}.service 并清理 ue{idx}.conf / netns ue{idx}"]
        if any(left.values()):
            raise IntentError(f"UE 节点回滚未完成，仍有残留: {left}")
        if snapshot.get("sm_dump_before"):
            from .actions_more import _restore_sm
            _restore_sm(intent["target"]["imsi"], snapshot["sm_dump_before"])
            msg.append("已有订户的签约已恢复到新增节点前")
        else:
            msg.append(f"新建订户 {intent['target']['imsi']} 的签约保留")
        return "；".join(msg)

    # ---------------------------------------------------------------- 持久化足迹
    def config_footprint(self, intent: Dict[str, Any]) -> List[Dict[str, Any]]:
        p = intent["params"]
        idx = int(p.get("index") or ue_nodes.next_index())
        return [
            {"kind": "ue_node", "index": idx, "imsi": intent["target"]["imsi"],
             "sst": int(p["sst"]), "sd": str(p["sd"])},
            {"kind": "file_key", "file": ue_nodes.conf_path(idx),
             "pattern": r'imsi\s*=\s*"(\d{15})"', "label": f"ue{idx}.conf imsi",
             "service": f"oran-ue{idx}"},
        ]


def _netns_list() -> List[str]:
    p = subprocess.run(["sudo", "ip", "netns", "list"], capture_output=True, text=True)
    return [x.split()[0] for x in p.stdout.splitlines() if x.strip()]


def _main_ue_ip() -> str | None:
    p = subprocess.run(["ip", "-4", "-o", "addr", "show", "oaitun_ue1"], capture_output=True, text=True)
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", p.stdout)
    return m.group(1) if m else None


# ---------------------------------------------------------------- 注册
from .actions import ACTIONS  # noqa: E402

_a = AddUe()
ACTIONS[_a.name] = _a
