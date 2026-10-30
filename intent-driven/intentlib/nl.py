"""自然语言 → 结构化意图。

优先走 LLM(OpenAI 兼容接口,env: INTENT_LLM_BASE / INTENT_LLM_KEY / INTENT_LLM_MODEL),
没有 key 或调用失败时退回确定性规则解析 —— 两种情况都会在 parser 字段里标注,
方便事后审计"这条意图是谁解析的、可不可信"。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

SCHEMA_HINT = """{
  "action": "one action from the catalog",
  "target": {"imsi": "15位数字或null"},
  "params": {"直接填写所选动作的参数，不要再按动作名嵌套"},
  "constraints": {"allow_session_rebuild": true, "allow_upf_recreate": false, "allow_core_config_edit": false},
  "confidence": 0.0,
  "rationale": "依据"
}"""

SYSTEM_PROMPT = f"""你是 5G 网络意图解析器。只输出严格 JSON。
支持动作与 params：
- add_subscriber：新增订户签约；fiveQI,ambr_ul,ambr_dl,sst,sd,template_imsi。
- set_qos：修改签约 QoS；fiveQI,ambr_ul,ambr_dl,arp_priority。
- provision_service：给已有订户配置业务；sst,sd,fiveQI,ambr_ul,ambr_dl。
- add_ue：新增独立 UE 节点；sst,sd,fiveQI,ambr_ul,ambr_dl,index；不等于新增订户。
- set_ran_slice_prb：切片共享的下行新传 PRB 比例上限；sst,sd,max_prb_percent(1-100整数),enabled(默认true，取消限额false)。不是最低预留，不指定IMSI。
- set_ran_dl_mcs：通过 xApp/FlexRIC/E2 设置 DU 小区下行 MCS 上限；max_mcs（0-28整数），作用于全小区，不指定IMSI。
- switch_ue：把主 UE 切到指定 IMSI；params 为空。
- activate_slice：激活切片；sst,sd；需要 allow_upf_recreate。
- set_egress：切换 N6 出口；target_dn_ip；需要 allow_upf_recreate。
- boost_capacity：调整 UPF worker 数；workers；需要 allow_upf_recreate。
- reduce_latency：低时延 QoS 配置与实测；fiveQI（默认7）。
- sla_guard：SLA 检测与补偿；metric（rtt/thp_dl/delay_dl）,threshold,window_s,compensation。
字段格式：{SCHEMA_HINT}
IMSI 必须是15位数字，缺失时填null，不要编造。SD为六位十六进制。
AMBR 用50Mbps这样的字符串；上下行各X表示ambr_ul与ambr_dl都为X。
allow_session_rebuild 表示允许重建会话。只有用户明确要求切片、容量或出口变更时才允许UPF重建。
新建/创建/激活切片必须使用activate_slice，并设置allow_core_config_edit=true及allow_upf_recreate=true，统一配置核心网和RAN；provision_service只用于已有订户业务档案，不能代替新建切片。
只返回一个动作；复合或含糊请求应降低置信度，不要悄悄丢弃未支持的要求。
"""


def _llm_conf() -> tuple[str, str, str] | None:
    key = os.environ.get("INTENT_LLM_KEY") or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    base = os.environ.get("INTENT_LLM_BASE", DEFAULT_BASE).rstrip("/")
    model = os.environ.get("INTENT_LLM_MODEL", DEFAULT_MODEL)
    return base, key, model


def _llm_parse(text: str) -> dict | None:
    conf = _llm_conf()
    if not conf:
        return None
    base, key, model = conf
    payload = json.dumps({
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            body = json.loads(r.read().decode())
        content = body["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return None
        parsed = json.loads(m.group(0))
        parsed.setdefault("parser", {})
        parsed["parser"] = {"type": "llm", "model": model, "confidence": float(parsed.get("confidence", 0.7))}
        return _normalize(parsed)
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError, OSError) as e:
        return None


_UNIT = {"mbps": 1, "m": 1, "gbps": 1000, "g": 1000}


def _ambr(num: str, unit: str) -> str:
    try:
        v = float(num) * _UNIT.get(unit.lower(), 1)
    except ValueError:
        return f"{num}Mbps"
    return f"{int(v) if v == int(v) else v}Mbps"


def _rules_parse_ext(t: str, low: str) -> dict | None:
    """第二批意图:切片/出口/容量/时延/SLA。命中即返回,未命中返回 None。"""
    imsi_m = re.search(r"\b(?:imsi[-: ]?)?(\d{15})\b", low)
    imsi = imsi_m.group(1) if imsi_m else None
    imsi_or_default = imsi  # 切片动作允许不给 IMSI,执行时用活动 UE 兜底

    def params_common() -> dict:
        out = {}
        m = re.search(r"5\s*qi\D{0,12}?(\d{1,2})", low)
        if m:
            out["fiveQI"] = int(m.group(1))
        amb = re.findall(r"(\d+(?:\.\d+)?)\s*(mbps|gbps|m|g)\b", low)
        if amb:
            out["ambr_ul"] = out["ambr_dl"] = _ambr(*amb[0])
        return out

    # A UE node is distinct from a subscriber record.
    if imsi and re.search(r"(?:新增|添加|创建|新建)\s*(?:一个)?\s*ue(?:\s*节点)?|add\s+ue", low):
        p = params_common()
        m_sst = re.search(r"sst\D{0,6}?(\d{1,3})", low)
        m_sd = re.search(r"\bsd\D{0,6}?([0-9a-f]{6})\b", low)
        if m_sst: p["sst"] = int(m_sst.group(1))
        if m_sd: p["sd"] = m_sd.group(1)
        return {"action": "add_ue", "target": {"imsi": imsi}, "params": p,
                "constraints": {"allow_session_rebuild": False},
                "parser": {"type": "rules", "model": "regex-v2", "confidence": 0.9}}

    # SLA
    if re.search(r"\bsla\b|保障.*(业务|体验|质量)|sla\s*保障", low) and imsi:
        p = {"metric": "rtt", "threshold": 30, "window_s": 60,
             "compensation": {"fiveQI": 7}}
        if re.search(r"吞吐|throughput|速率", low):
            p["metric"] = "thp_dl"
            m = re.search(r"(?:不低于|大于|>=|至少)\s*(\d+(?:\.\d+)?)\s*(mbps|gbps|m|g)?", low)
            if m:
                mult = {"gbps": 1000, "g": 1000}.get((m.group(2) or "mbps").lower(), 1)
                p["threshold"] = float(m.group(1)) * mult
            p["compensation"] = {"ambr_dl": f"{int(p['threshold'])}Mbps", "fiveQI": 7}
        else:
            m = re.search(r"(?:不高于|不超过|低于|<=|小于)\s*(\d+(?:\.\d+)?)\s*(ms|毫秒)?", low)
            if m:
                p["threshold"] = float(m.group(1))
            p["compensation"] = dict(p["compensation"], **params_common())
        return {"action": "sla_guard", "target": {"imsi": imsi}, "params": p,
                "constraints": {"allow_session_rebuild": True},
                "confidence": 0.8, "rationale": f"SLA 保障:{p['metric']} 阈值 {p['threshold']}",
                "parser": {"type": "rules", "model": "regex-v1", "confidence": 0.8}}

    # 数据出口
    if re.search(r"数据出口|出口|出网|分流|本地卸载|dna[i]?|n6|dn\b", low):
        ip_m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", t)
        p = {"target_dn_ip": ip_m.group(1) if ip_m else "192.168.73.136"}
        return {"action": "set_egress", "target": {"imsi": imsi},
                "params": p,
                "constraints": {"allow_upf_recreate": True, "allow_session_rebuild": True},
                "confidence": 0.75, "rationale": f"切换 N6 下一跳到 {p['target_dn_ip']}(会重建 UPF)",
                "parser": {"type": "rules", "model": "regex-v1", "confidence": 0.75}}

    # 扩容
    if re.search(r"扩容|增加容量|扩容量|加容量|容量|worker|并发", low):
        m = re.search(r"(?:worker|核|线程)\D{0,6}?(\d)", low)
        p = {"workers": int(m.group(1)) if m else 2}
        return {"action": "boost_capacity", "target": {"imsi": imsi}, "params": p,
                "constraints": {"allow_upf_recreate": True, "allow_session_rebuild": True},
                "confidence": 0.7, "rationale": f"UPF worker → {p['workers']}(会重建 UPF)",
                "parser": {"type": "rules", "model": "regex-v1", "confidence": 0.7}}

    # 降时延
    if re.search(r"(降低?|优化|压低)\S{0,4}(时延|延时)|(时延|延时)\S{0,4}(降|优化)|低时延|latency", low) and imsi:
        p = {"fiveQI": 7}
        p.update(params_common())
        return {"action": "reduce_latency", "target": {"imsi": imsi}, "params": p,
                "constraints": {"allow_session_rebuild": True},
                "confidence": 0.8, "rationale": f"切低时延档 5QI {p['fiveQI']}",
                "parser": {"type": "rules", "model": "regex-v1", "confidence": 0.8}}

    # 新建/激活切片(会换 UPF 的 S-NSSAI,断全部会话)
    # 切片动作不要求给 IMSI(执行时用活动 UE 兜底),所以这里不加 imsi 条件
    if re.search(r"(新建|创建|新增|增加|激活|切换|开通)\s*(一个|个)?\s*(新的?|新)?\s*切片|切片\s*(新建|创建|激活)", low):
        m_sst = re.search(r"sst\D{0,6}?(\d{1,3})", low)
        m_sd = re.search(r"\bsd\D{0,6}?([0-9a-f]{1,6})\b", low)
        if not m_sst:
            return None
        p = {"sst": int(m_sst.group(1))}
        if m_sd:
            p["sd"] = m_sd.group(1).zfill(6)
        return {"action": "activate_slice", "target": {"imsi": imsi},
                "params": p,
                "constraints": {"allow_upf_recreate": True, "allow_session_rebuild": True, "allow_core_config_edit": True},
                "confidence": 0.8,
                "rationale": f"激活切片 {p['sst']}.{p.get('sd', 'ffffff')}(换 UPF 的 S-NSSAI)",
                "parser": {"type": "rules", "model": "regex-v1", "confidence": 0.8}}

    # 把活动 UE 切到某个订户
    if imsi and re.search(r"切换\s*(活动\s*)?ue|让\s*ue.{0,8}(跑|用|连)|激活\s*ue|(切到|切换到|切为).{0,8}\d{15}", low):
        return {"action": "switch_ue", "target": {"imsi": imsi}, "params": {},
                "constraints": {"allow_session_rebuild": True},
                "confidence": 0.8, "rationale": f"把活动 UE 切到 {imsi}",
                "parser": {"type": "rules", "model": "regex-v1", "confidence": 0.8}}

    # 开通新业务 / 新切片
    # 注意:"给新用户 ... 开通,切片 SST x" 说的是给订户配切片档案,不是开通新切片业务;
    # 这种要先让 add_subscriber 分支处理,否则会把新用户当成切片业务搞错。
    new_sub = re.search(r"新增用户|新用户|添加用户|开通用户|加个用户|new subscriber|add subscriber", low)
    if not new_sub and re.search(r"开通.{0,4}业务|新业务|新切片|切片开通|切片|slice", low) and imsi:
        m_sst = re.search(r"sst\D{0,6}?(\d{1,3})", low)
        m_sd = re.search(r"\bsd\D{0,6}?([0-9a-f]{6})\b", low)
        if not m_sst and not m_sd:
            return None
        p = params_common()
        if m_sst:
            p["sst"] = int(m_sst.group(1))
        if m_sd:
            p["sd"] = m_sd.group(1)
        return {"action": "provision_service", "target": {"imsi": imsi}, "params": p,
                "constraints": {"allow_session_rebuild": True},
                "confidence": 0.8, "rationale": f"开通业务档案 S-NSSAI {p.get('sst')}.{p.get('sd', '')}",
                "parser": {"type": "rules", "model": "regex-v1", "confidence": 0.8}}

    return None


def _rules_parse(text: str) -> dict:
    t = text.strip()
    low = t.lower()
    ext = _rules_parse_ext(t, low)
    if ext:
        ext["raw_text"] = text
        return _normalize(ext)
    imsi_m = re.search(r"\b(?:imsi[-: ]?)?(\d{15})\b", low)
    imsi = imsi_m.group(1) if imsi_m else None

    action = None
    if re.search(r"新增|添加|开通|加个|加入|new subscriber|add subscriber|provision", low):
        action = "add_subscriber"
    if re.search(r"qos|5qi|限速|带宽|ambr|时延保障", low):
        # "开通用户并设置 QoS" 这种同时出现时,以显式 QoS 动词为准
        if re.search(r"改|调|设|修改|调整|变更|set|modify|change|update", low) or action is None:
            action = "set_qos"
    if action is None:
        action = "set_qos" if imsi else "add_subscriber"

    params: dict = {}
    m = re.search(r"5\s*qi\D{0,12}?(\d{1,2})", low) or re.search(r"(?:qos等级|业务等级)\D{0,6}?(\d{1,2})", low)
    if m:
        params["fiveQI"] = int(m.group(1))

    amb = re.findall(r"(\d+(?:\.\d+)?)\s*(mbps|gbps|m|g)\b", low)
    if len(amb) >= 2:
        params["ambr_ul"], params["ambr_dl"] = _ambr(*amb[0]), _ambr(*amb[1])
    elif len(amb) == 1:
        v = _ambr(*amb[0])
        params["ambr_ul"] = params["ambr_dl"] = v

    m = re.search(r"sst\s*[:：=]?\s*(\d{1,2})", low)
    if m:
        params["sst"] = int(m.group(1))
    m = re.search(r"\bsd\s*[:：=]?\s*([0-9a-f]{6})\b", low)
    if m:
        params["sd"] = m.group(1)
    m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", t)
    if m:
        params["static_ip"] = m.group(1)
    m = re.search(r"(?:arp|优先级)\D{0,8}?(\d{1,2})", low)
    if m:
        params["arp_priority"] = int(m.group(1))

    if action == "add_subscriber" and "static_ip" not in params:
        pass

    warnings = []
    if re.search(r"5\s*qi", low) and "fiveQI" not in params:
        warnings.append("提到 5QI 但没解析出数值")
    if re.search(r"(mbps|gbps|限速|带宽)", low) and "ambr_ul" not in params:
        warnings.append("提到带宽但没解析出 AMBR")

    confidence = 0.55
    if imsi:
        confidence += 0.25
    if params.get("fiveQI"):
        confidence += 0.1
    if params.get("ambr_ul"):
        confidence += 0.05

    return _normalize({
        "action": action,
        "target": {"imsi": imsi},
        "params": params,
        "constraints": {"allow_session_rebuild": True},
        "confidence": round(min(confidence, 0.95), 2),
        "warnings": warnings,
        "rationale": f"规则解析:{action} / imsi={imsi} / {params}",
        "parser": {"type": "rules", "model": "regex-v1", "confidence": round(min(confidence, 0.95), 2)},
    })


def _normalize(d: dict) -> dict:
    action = d.get("action")
    if action not in ("add_subscriber", "set_qos", "provision_service", "set_egress",
                      "boost_capacity", "reduce_latency", "sla_guard",
                      "activate_slice", "switch_ue", "add_ue", "set_ran_dl_mcs", "set_ran_slice_prb"):
        raise ValueError(f"不支持的动作: {action}")
    target = d.get("target") or {}
    if not isinstance(target, dict): raise ValueError("target 必须是对象")
    imsi = target.get("imsi")
    if imsi is not None:
        imsi = re.sub(r"\D", "", str(imsi))
        if len(imsi) != 15:
            imsi = None
    target["imsi"] = imsi
    d["target"] = target
    d.setdefault("params", {})
    d.setdefault("constraints", {"allow_session_rebuild": True})
    if not isinstance(d["constraints"], dict): raise ValueError("constraints 必须是对象")
    if not isinstance(d["params"], dict):
        raise ValueError("params 必须是对象")
    return d


def parse(text: str, use_llm: bool = True) -> dict:
    if re.search(r"prb",text,re.I):
        if re.search(r"上行|uplink|保底|最低|最小预留|保证预留",text,re.I):raise ValueError("当前只支持下行新传 PRB 使用上限，不支持上行或最低预留")
        if re.search(r"\d{15}",text):raise ValueError("PRB 预算按切片共享，请使用 SST/SD，不指定单 IMSI")
        sst=re.search(r"sst\s*[=:]?\s*(\d{1,3})(?!\d)",text,re.I)
        sd=re.search(r"sd\s*[=:]?\s*(?:0x)?([0-9a-f]{6})(?![0-9a-f])",text,re.I)
        if not sst or not sd:raise ValueError("请指定 SST 和六位 SD，例如切片 SST 3 SD 000003")
        enabled=not bool(re.search(r"取消|清除|移除|撤销",text))
        p={"sst":int(sst.group(1)),"sd":sd.group(1).lower(),"enabled":enabled}
        if enabled:
            value=re.search(r"(?<![\d.])(\d+)(?![\d.])\s*[%％]",text)
            if not value:raise ValueError("请给出整数百分比，例如：将切片 SST 3 SD 000003 的下行 PRB 上限设为 30%")
            p["max_prb_percent"]=int(value.group(1))
        return _normalize({"action":"set_ran_slice_prb","target":{},"params":p,"constraints":{},
            "parser":{"type":"rules","model":"e2-prb-v1","confidence":1.0},"confidence":1.0,
            "rationale":"经 xApp/FlexRIC/E2 设置切片共享的下行新传 PRB 上限","raw_text":text})
    if re.search(r"mcs",text,re.I):
        if re.search(r"\d{15}",text):raise ValueError("当前动作作用于整个 DU 小区，请勿指定单个 IMSI")
        match=re.search(r"mcs\s*(?:上限|最大值|最大)?\s*(?:设为|设置为|设置成|设置|设成|改为|改成|调到|限制为|到|为|=|:)\s*(\d+)(?![\d.])",text,re.I)
        if not match:
            match=re.search(r"(?:max[_ -]?mcs)\s*[=:]?\s*(\d+)(?![\d.])",text,re.I)
        if not match:raise ValueError("请给出 DU 下行 MCS 上限，例如：将 DU 下行 MCS 上限设为 20")
        if re.search(r"上行|uplink",text,re.I):raise ValueError("当前 E2 动作只支持下行 MCS 上限")
        return _normalize({"action":"set_ran_dl_mcs","target":{},"params":{"max_mcs":int(match.group(1))},
            "constraints":{},"parser":{"type":"rules","model":"e2-mcs-v1","confidence":1.0},
            "confidence":1.0,"rationale":"DU 小区下行 MCS 上限经控制 xApp/FlexRIC/E2 热更新","raw_text":text})
    # Explicit slice creation always uses the end-to-end action.
    explicit = _rules_parse_ext(text, text.lower())
    if explicit and explicit.get("action") == "activate_slice":
        explicit["raw_text"] = text
        return _normalize(explicit)
    if use_llm:
        got = _llm_parse(text)
        if got:
            got["raw_text"] = text
            return got
    got = _rules_parse(text)
    got["raw_text"] = text
    return got
