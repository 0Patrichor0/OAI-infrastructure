#!/usr/bin/env python3
"""意图驱动控制面的 Web 前端（标准库实现，无第三方依赖）。

设计原则：
- 所有写操作都通过 `intentctl.py` 子进程执行，不复制业务逻辑 —— 账本/审计/回滚只有一套实现；
- 动作会跑几分钟（重建 UPF、重建 UE 会话），所以 POST 一律返回 job id，日志走 SSE；
- 只读接口直接读 sqlite / 调 docker，用来给人看「现在是什么状态」。

用法: python3 ui/server.py [--host 127.0.0.1] [--port 8099]
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOME = os.environ.get("INTENT_HOME", "/home/ubuntu/intent-driven")
CLI = os.path.join(HOME, "intentctl.py")
DB = os.path.join(HOME, "ledger.db")
UI_DIR = os.path.join(HOME, "ui")
JOB_DIR = os.path.join(HOME, "logs", "ui-jobs")
APPLY_STATE = os.path.join(HOME, "tools", "apply_state.py")
# 固定解释器：服务可能被 venv/别的 python 拉起，子进程不该跟着变
PY = os.environ.get("INTENT_PY", "/usr/bin/python3")

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
EXECUTION_LOCK = threading.Lock()

# 只读的切片/UE 清册直接在本进程里查(不写库、不改配置)
sys.path.insert(0, HOME)
from intentlib import slices as slice_lib  # noqa: E402
from intentlib import ue_nodes as ue_lib  # noqa: E402
from intentlib import actions_prb
from intentlib import actions_ran  # Register the E2 action for preview and execution.
from intentlib import actions_more, actions_slice, actions_ue  # Match CLI actions before HTTP preflight.
from intentlib import preflight, submission, live_reads
from intentlib.storage import atomic_text  # noqa: E402


# ---------------------------------------------------------------- 基础设施
def sh(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def ledger_rows(limit: int = 60) -> list[dict]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select id,created_at,action,state,intent_json,error from intents "
        "order by rowid desc limit ?", (limit,)).fetchall()
    out = []
    for r in rows:
        intent = {}
        try:
            intent = json.loads(r["intent_json"] or "{}")
        except ValueError:
            pass
        out.append({
            "id": r["id"], "created_at": r["created_at"], "action": r["action"],
            "state": r["state"], "error": r["error"] or "",
            "text": intent.get("text") or intent.get("raw_text") or "",
            "imsi": (intent.get("target") or {}).get("imsi") or "",
            "params": intent.get("params") or {},
            "confidence": intent.get("confidence"),
            "parser": intent.get("parser"),
        })
    con.close()
    return out


def ledger_one(iid: str) -> dict | None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    r = con.execute("select * from intents where id=?", (iid,)).fetchone()
    con.close()
    if not r:
        return None
    rec = dict(r)
    for k in ("intent_json", "plan_json", "snapshot_json", "exec_json", "verify_json"):
        try:
            rec[k.replace("_json", "")] = json.loads(rec.get(k) or "null")
        except ValueError:
            rec[k.replace("_json", "")] = None
    for key in ("intent_json", "plan_json", "snapshot_json", "exec_json", "verify_json"):
        rec.pop(key, None)
    def redact(value, key=""):
        if any(word in key.lower() for word in ("dump", "ue_conf", "_intent_config_before")):
            return "[快照已保存，敏感内容不在页面显示]"
        if isinstance(value, dict): return {k: redact(v, k) for k, v in value.items()}
        if isinstance(value, list): return [redact(v) for v in value]
        return value
    rec["snapshot"] = redact(rec.get("snapshot"))
    return rec


def ue_node_list() -> list[dict]:
    """主 UE（宿主）+ 每个 oran-ueN.service 的现状。"""
    out = [{
        "name": "ue1（主）", "unit": "oran-ue", "netns": None,
        "active": sh(["systemctl", "is-active", "oran-ue"])[1] == "active",
        "ip": (re.search(r"inet (\d+\.\d+\.\d+\.\d+)", sh(["ip", "-4", "-o", "addr", "show", "oaitun_ue1"])[1]) or [None, None])[1],
        "imsi": slice_lib.ue_conf().get("imsi"),
    }]
    for idx in ue_lib.existing_indexes():
        st = ue_lib.status(idx)
        # IMSI 以 ue{idx}.conf 为准：日志文件是复用的，上一个节点的 IMSI 还在里面
        imsi = None
        try:
            with open(ue_lib.conf_path(idx), encoding="utf-8") as fh:
                m = re.search(r'imsi\s*=\s*"(\d{15})"', fh.read())
                imsi = m.group(1) if m else None
        except OSError:
            pass
        out.append({"name": f"ue{idx}", "unit": st["unit"], "netns": st["netns"],
                    "active": st["active"], "ip": st["ip"], "imsi": imsi})
    return out


def persist_state() -> dict:
    code = (
        "import json,sys;sys.path.insert(0,%r);"
        "from intentlib import persist,persist_cfg;"
        "st=persist.state();cfg=persist_cfg.state();"
        "summary={k:{'auth':bool(v.get('auth','').strip()),'sm':bool(v.get('sm','').strip())} for k,v in st.items()};"
        "cfg['containers']={k:{'intent':v.get('intent'),'recorded_at':v.get('recorded_at')} for k,v in cfg.get('containers',{}).items()};"
        "print(json.dumps({'db':summary,'cfg':cfg,"
        "'rendered':persist.RENDERED},ensure_ascii=False,default=str))" % HOME
    )
    rc, out = sh([PY, "-c", code], timeout=60)
    if rc != 0 or not out:
        return {"db": {}, "cfg": {}, "rendered": "", "error": out[:300]}
    try:
        return json.loads(out.strip().splitlines()[-1])
    except ValueError:
        return {"db": {}, "cfg": {}, "rendered": "", "error": out[:300]}


def system_state() -> dict:
    st: dict = {}
    rc, ip = sh(["ip", "-4", "-o", "addr", "show", "oaitun_ue1"])
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", ip)
    st["ue_ip"] = m.group(1) if m else None
    if st["ue_ip"]:
        rc, _ = sh(["ping", "-c", "2", "-W", "2", "-I", "oaitun_ue1", "192.168.73.135"], timeout=10)
        st["ue_ping"] = rc == 0
    else:
        st["ue_ping"] = False

    rc, smf = sh_both(["docker", "logs", "--since", "60s", "oai-smf"], timeout=20)
    st["n4_beat"] = smf.count("handle_receive_pfcp_msg msg type 2")

    rc, units = sh(["systemctl", "is-active", "flexric-ric", "oran-cu-cp", "oran-cu-up",
                    "oran-du", "oran-ue", "oran-traffic", "flexric-xapp-kpm",
                    "flexric-xapp-gtp-mac"], timeout=15)
    st["units"] = [u for u in units.split() if u]
    st["units_ok"] = sum(1 for u in st["units"] if u == "active")

    rc, cnt = sh(["docker", "ps", "-q"], timeout=10)
    st["containers"] = len([x for x in cnt.split() if x])

    rc, timers = sh(["systemctl", "is-active", "oran-ue-watchdog.timer",
                     "intent-state-apply.timer"], timeout=10)
    st["timers"] = [t for t in timers.split() if t]

    rc, mongo = sh(["docker", "exec", "oai-nwdaf-database", "mongosh", "--quiet", "testing",
                    "--eval", "let n=0; db.smf.find().forEach(d=>n+=(d.qosmonlist||[]).length); print(n)"], timeout=25)
    st["qos"] = mongo.strip() if mongo.strip().isdigit() else "?"

    rc, dep = sh(["curl", "-s", "--max-time", "4", "http://localhost:8000/deployments"], timeout=10)
    st["deployments_ready"] = dep.count('"status":"ready"')

    rc, workers = sh(["docker", "exec", "vpp-upf", "sh", "-c",
                      "grep -o 'corelist-workers .*' /openair-upf/etc/startup*.conf 2>/dev/null"],
                     timeout=20)
    st["upf_workers"] = workers.split("corelist-workers")[-1].strip() if "corelist-workers" in workers else "?"
    st["ts"] = time.strftime("%H:%M:%S")
    return st



def sh_both(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    """同 sh(),但同时收 stderr —— OAI 容器的 docker logs 内容走 stderr。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


# NWDAF 采集侧:AnLF 的 Mongo(testing 库,smf/amf 两个集合,一个 SUPI 一条文档,
# 文档里是按事件累积的列表)。这里只做只读聚合,不碰 NWDAF 的写路径。
NWDAF_JS = r"""
const out = {smf: [], amf: []};
db.smf.find().forEach(d => {
  const q = d.qosmonlist || [], s = d.pdusesestlist || [];
  const last = q.length ? q[q.length - 1] : null;
  const sess = s.length ? s[s.length - 1] : null;
  const ur = last && last.customized_data && last.customized_data.usagereport;
  const vol = ur && ur.volume;
  out.smf.push({
    supi: String(d._id),
    updated: (d.lastmodified && d.lastmodified.low) || 0,
    qos: q.length, sessions: s.length, ueip: (d.ueipchlist || []).length,
    last_qos_ts: (last && last.timestamp && last.timestamp.low) || 0,
    ul: vol ? vol.uplink.low : null, dl: vol ? vol.downlink.low : null,
    dnn: sess ? sess.dnn : null,
    sst: sess && sess.snssai ? sess.snssai.sst : null,
    sd: sess && sess.snssai ? sess.snssai.sd : null
  });
});
db.amf.find().forEach(d => {
  const l = d.locationlist || [], r = d.rminfolist || [], x = d.lossofconnectreasonlist || [];
  out.amf.push({
    supi: String(d._id), updated: (d.lastmodified && d.lastmodified.low) || 0,
    location: l.length, rm: r.length, loss: x.length
  });
});
print(JSON.stringify(out));
"""


def nwdaf_state() -> dict:
    rc, out = sh(["docker", "exec", "oai-nwdaf-database", "mongosh", "--quiet", "testing",
                  "--eval", NWDAF_JS], timeout=30)
    data: dict = {"smf": [], "amf": []}
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue
    now = time.time()
    for row in data.get("smf", []):
        row["age_s"] = int(now - row["updated"]) if row.get("updated") else None
        row["qos_age_s"] = int(now - row["last_qos_ts"]) if row.get("last_qos_ts") else None
        if row.get("sd") not in (None, ""):
            try:
                row["sd_hex"] = format(int(row["sd"]), "06x")
            except (TypeError, ValueError):
                row["sd_hex"] = str(row["sd"])
        if row.get("dl") is not None:
            row["dl_kb"] = round(row["dl"] / 1024, 1)
            row["ul_kb"] = round((row.get("ul") or 0) / 1024, 1)
    for row in data.get("amf", []):
        row["age_s"] = int(now - row["updated"]) if row.get("updated") else None

    _, sbi_log = sh_both(["docker", "logs", "--since", "120s", "oai-nwdaf-sbi"], timeout=25)
    _, sbi_h = sh(["docker", "inspect", "oai-nwdaf-sbi", "-f", "{{.State.Health.Status}}"], timeout=10)
    return {
        "ok": bool(data.get("smf") or data.get("amf")),
        "source": "oai-nwdaf-database:27017 · testing(smf/amf)",
        "smf": data.get("smf", []),
        "amf": data.get("amf", []),
        "totals": {
            "smf_docs": len(data.get("smf", [])),
            "amf_docs": len(data.get("amf", [])),
            "qos_samples": sum(r.get("qos", 0) for r in data.get("smf", [])),
            "sessions": sum(r.get("sessions", 0) for r in data.get("smf", [])),
        },
        "sbi": {"health": sbi_h.strip() or "?",
                "writes_2m": sbi_log.count("Storing SMF notification")},
        "ts": time.strftime("%H:%M:%S"),
    }


PARSE_SNIPPET = r"""
import json, sys
sys.path.insert(0, %r)
from intentlib import nl, actions, actions_more, actions_slice, actions_ue, actions_ran, actions_prb
text = sys.argv[1]
use_llm = sys.argv[2] == "1"
out = {"ok": True}
try:
    it = nl.parse(text, use_llm=use_llm)
except Exception as exc:
    print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
    raise SystemExit
out["intent"] = it
act = actions.get(it.get("action") or "")
if act:
    out["description"] = getattr(act, "description", "")
    try:
        act.validate(it)
        out["valid"] = True
    except Exception as exc:
        out["valid"] = False
        out["invalid"] = f"{type(exc).__name__}: {exc}"
    try:
        out["plan"] = act.plan(it)
    except Exception as exc:
        out["plan"] = []
        out["plan_error"] = str(exc)
print(json.dumps(out, ensure_ascii=False, default=str))
"""

# ---------------------------------------------------------------- 任务执行
PREVIEWS = {}
PREVIEWS_LOCK = threading.Lock()


def intent_job(title: str, intent: dict, expected=None) -> dict:
    """把结构化意图写成文件再交给 intentctl,避免表单值被 NL 解析改写。"""
    os.makedirs(JOB_DIR, exist_ok=True)
    path = os.path.join(JOB_DIR, f"intent-{uuid.uuid4().hex}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(intent, f, ensure_ascii=False)
    argv = [PY, CLI, 'apply', '--file', path, '--yes']
    if expected:
        guard = path + '.preflight.json'
        atomic_text(guard,json.dumps(expected,ensure_ascii=False))
        argv += ['--preflight',guard]
    return start_job(title,argv)


def public_job(job):
    return {key: job.get(key) for key in ("id", "title", "status", "started", "rc", "intent_id")}


def save_job(job):
    atomic_text(os.path.join(JOB_DIR, job["id"] + ".json"), json.dumps(job, ensure_ascii=False))


def load_jobs():
    if not os.path.isdir(JOB_DIR): return
    for path in Path(JOB_DIR).glob("*.json"):
        if not re.fullmatch(r"[0-9a-f]{12}\.json", path.name): continue
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if job.get("id") != path.stem: continue
            job["log"] = os.path.join(JOB_DIR, job["id"] + ".log")
            if job.get("status") in ("running", "queued"):
                job.update(status="interrupted", rc=-2)
                save_job(job)
            JOBS[job["id"]] = job
        except (OSError, ValueError):
            continue


def start_job(title: str, argv: list[str]) -> dict:
    jid = uuid.uuid4().hex[:12]
    os.makedirs(JOB_DIR, exist_ok=True)
    logpath = os.path.join(JOB_DIR, f"{jid}.log")
    job = {"id": jid, "title": title, "argv": argv, "status": "queued",
           "started": time.strftime("%F %T"), "rc": None, "log": logpath}
    with JOBS_LOCK:
        JOBS[jid] = job
        save_job(job)

    def run() -> None:
        with EXECUTION_LOCK:
            live_reads.broker.invalidate()
            try:
                with open(logpath, "w", encoding="utf-8") as fh:
                    with JOBS_LOCK:
                        job["status"] = "running"
                        save_job(job)
                    fh.write(f"$ {' '.join(argv)}\n")
                    fh.flush()
                    env = dict(os.environ, PYTHONUNBUFFERED="1")
                    with subprocess.Popen(argv, cwd=HOME, stdout=fh, stderr=subprocess.STDOUT, env=env) as child:
                        rc = child.wait()
                    fh.write(f"\n[exit {rc}]\n")
                match = re.search(r'意图 ID:\s*([0-9]{12}-[0-9a-f]{6})',Path(logpath).read_text())
                if match:
                    job['intent_id'] = match[1]
                live_reads.broker.invalidate()
                with JOBS_LOCK:
                    terminal = {**job, "rc": rc, "status": "done" if rc == 0 else "failed"}
                    save_job(terminal)
                    job.update(terminal)
            except Exception as exc:
                live_reads.broker.invalidate()
                with JOBS_LOCK:
                    job.update(status="failed", rc=-1)
                    save_job(job)
                try:
                    with open(logpath, "a", encoding="utf-8") as fh:
                        fh.write(f"\n[error] {type(exc).__name__}: {exc}\n")
                except OSError:
                    pass

    threading.Thread(target=run, daemon=True).start()
    return job


def read_log(job: dict, start: int = 0) -> tuple[str, int]:
    try:
        with open(job["log"], "rb") as fh:
            fh.seek(max(0, min(start, os.fstat(fh.fileno()).st_size)))
            data = fh.read()
            return data.decode("utf-8", errors="replace"), fh.tell()
    except OSError:
        return "", start


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "intent-ui/1.0"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    # --- helpers
    def _json(self, obj, code=200):
        if isinstance(obj, dict) and "argv" in obj and "log" in obj:
            obj = public_job(obj)
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: str, ctype: str):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 前端改完必须立刻生效：否则浏览器拿缓存的旧 app.js，看起来像“后端没更新”
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 <= n <= 1_048_576:
            raise ValueError("请求体超过大小限制")
        body = json.loads(self.rfile.read(n).decode() or "{}") if n else {}
        if not isinstance(body, dict): raise ValueError("请求体必须是 JSON 对象")
        return body

    # --- routes
    def do_GET(self):  # noqa: N802
        try:
            return self._get()
        except (ValueError, TypeError) as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _get(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)

        if p in ("/", "/index.html"):
            return self._file(os.path.join(UI_DIR, "index.html"), "text/html; charset=utf-8")
        if p.startswith("/static/"):
            name = os.path.basename(p)
            ctype = ("text/css" if name.endswith(".css") else
                     "application/javascript" if name.endswith(".js") else "text/plain")
            return self._file(os.path.join(UI_DIR, name), ctype + "; charset=utf-8")

        if p == "/api/intents":
            return self._json({"items": ledger_rows(max(1, min(200, int(q.get("limit", ["60"])[0]))))})
        if p.startswith("/api/intents/"):
            iid = p.rsplit("/", 1)[-1]
            rec = ledger_one(iid)
            return self._json(rec) if rec else self._json({"error": "not found"}, 404)
        if p == "/api/persist":
            return self._json(persist_state())
        if p == "/api/system":
            return self._json(system_state())
        if p == "/api/ran/prb":
            from intentlib import prb_control
            try:
                raw=q.get("sst",[""])[0];sd=q.get("sd",[""])[0]
                if not raw.isdigit() or not 0<=int(raw)<=255 or not re.fullmatch("[0-9a-fA-F]{6}",sd):
                    return self._json({"error":"需要 SST(0-255) 和六位 SD"},400)
                with JOBS_LOCK:
                    writing = any(j['status']=='running' for j in JOBS.values())
                if writing:
                    return self._json({'error':'意图执行中，稍后重新读取；本次读取未排队','retry_after_s':3},503)
                from intentlib.ran_control import du_pid
                return self._json(live_reads.broker.read(('prb',du_pid(),int(raw),sd.lower()),
                    lambda:prb_control.request('query',int(raw),sd.lower())))
            except Exception as exc:return self._json({"error":str(exc),'retry_after_s':2},503)
        if p == "/api/ran/mcs":
            try:
                from intentlib import ran_control
                with JOBS_LOCK:
                    writing = any(j['status']=='running' for j in JOBS.values())
                if writing:
                    return self._json({'error':'意图执行中，稍后重新读取；本次读取未排队','retry_after_s':3},503)
                return self._json(live_reads.broker.read(('mcs',ran_control.du_pid()),lambda:ran_control.request('query')))
            except Exception as exc:
                return self._json({"error": str(exc),'retry_after_s':2}, 503)
        if p == '/api/ran/read-health':
            return self._json(live_reads.broker.diagnostics())
        if p == "/api/nwdaf":
            return self._json(nwdaf_state())
        if p == "/api/ue-nodes":
            return self._json({"nodes": ue_node_list()})
        if p == "/api/slices":
            try:
                return self._json(slice_lib.inventory())
            except Exception as exc:  # noqa: BLE001
                return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
        if p == "/api/jobs":
            with JOBS_LOCK:
                items = sorted(JOBS.values(), key=lambda j: j["started"], reverse=True)[:20]
            return self._json({"items": [public_job(job) for job in items]})
        if p.startswith("/api/jobs/"):
            rest = p[len("/api/jobs/"):]
            if rest.endswith("/stream"):
                jid = rest[: -len("/stream")]
                return self._stream(jid)
            jid = rest
            job = JOBS.get(jid)
            if not job:
                return self._json({"error": "unknown job"}, 404)
            data, _ = read_log(job)
            return self._json({**public_job(job),
                               "log": data})
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        try:
            body = self._body()
            if not isinstance(body, dict): raise ValueError("请求体必须是 JSON 对象")
            for key in ("text", "imsi", "sd", "ambr", "ambr_ul", "ambr_dl"):
                if key in body and body[key] is not None and not isinstance(body[key], str):
                    raise ValueError(f"{key} 必须是字符串")
            for key, low, high in (("sst", 0, 255), ("fiveQI", 1, 255)):
                if body.get(key) not in (None, ""):
                    value = body[key]
                    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)) or not low <= int(value) <= high:
                        raise ValueError(f"{key} 必须为 {low}-{high} 的整数")
                    body[key] = int(value)
            if body.get("sd") and not re.fullmatch(r"[0-9a-fA-F]{6}", body["sd"]):
                raise ValueError("SD 必须是六位十六进制")
            for key in ("ambr", "ambr_ul", "ambr_dl"):
                if body.get(key) and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:Mbps|Gbps)", body[key]):
                    raise ValueError(f"{key} 格式应为 50Mbps")
            return self._post(body)
        except (ValueError, TypeError, AttributeError) as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _post(self, body):
        p = urlparse(self.path).path

        if p == '/api/preflight':
            route, data = body.get('route','/api/apply'), body.get('body',{})
            if not submission.supported(route) or not isinstance(data,dict):
                return self._json({'error':'无效的预检查请求'},400)
            try:
                it = submission.intent_for(route,data)
                preview = preflight.build(it)
            except Exception as exc:
                return self._json({'error':str(exc)},400)
            token = uuid.uuid4().hex
            expires = time.time()+180
            with PREVIEWS_LOCK:
                for key in list(PREVIEWS):
                    if PREVIEWS[key]['expires_at']<time.time(): PREVIEWS.pop(key)
                if len(PREVIEWS)>=256:
                    return self._json({'error':'预览数量过多，请稍后重试'},503)
                if preview['valid']:
                    PREVIEWS[token] = {'route':route,'intent':preview['intent'],
                        'fingerprint':preview['fingerprint'],'expires_at':expires}
            return self._json({**preview,'token':token if preview['valid'] else None,'expires_at':expires})

        if submission.supported(p) and body.get('preflight_token'):
            with PREVIEWS_LOCK:
                ticket = PREVIEWS.get(body['preflight_token'])
                if not ticket or ticket['route']!=p or ticket['expires_at']<time.time():
                    return self._json({'error':'预览已过期，请重新预检查'},409)
                PREVIEWS.pop(body['preflight_token'])
            return self._json(intent_job('apply: '+ticket['intent']['action'],ticket['intent'],
                {k:ticket[k] for k in ('fingerprint','expires_at')}))

        if p == "/api/parse":
            text = (body.get("text") or "").strip()
            if not text:
                return self._json({"error": "空意图"}, 400)
            snippet = PARSE_SNIPPET % HOME
            rc, out = sh([PY, "-c", snippet, text,
                          "0" if body.get("no_llm") else "1"], timeout=180)
            try:
                payload = json.loads(out.strip().splitlines()[-1])
            except (ValueError, IndexError):
                payload = {"ok": False, "error": out[-400:]}
            payload["raw"] = out
            return self._json(payload)

        if p == "/api/apply":
            text = (body.get("text") or "").strip()
            if not text:
                return self._json({"error": "空意图"}, 400)
            argv = [PY, CLI, "apply", "--nl", text, "--yes"]
            if body.get("no_llm"):
                argv.append("--no-llm")
            return self._json(start_job(f"apply: {text[:40]}", argv))

        m = re.fullmatch(r"/api/intents/([^/]+)/(rollback|verify|replay)", p)
        if m:
            iid, op = m.group(1), m.group(2)
            argv = [PY, CLI, op, iid] + (["--yes"] if op == "replay" else [])
            return self._json(start_job(f"{op} {iid[:12]}", argv))

        if p == "/api/ran/prb":
            from intentlib.actions import IntentError
            intent={"action":"set_ran_slice_prb","target":{},"params":{k:body[k] for k in ("sst","sd","max_prb_percent","enabled") if k in body},"constraints":{}}
            try:actions_prb.SetRanSlicePrb().validate(intent)
            except IntentError as exc:return self._json({"error":str(exc)},400)
            return self._json(intent_job("E2 切片下行新传 PRB 上限",intent))
        if p == "/api/ran/mcs":
            value=body.get("max_mcs")
            if isinstance(value,bool) or not isinstance(value,int) or not 0<=value<=28:
                return self._json({"error":"max_mcs 必须是 0-28 的整数"},400)
            return self._json(intent_job("E2 设置 DU 下行 MCS 上限", {
                "action":"set_ran_dl_mcs", "target":{}, "params":{"max_mcs":value}, "constraints":{}}))
        if p == "/api/ues":
            imsi = (body.get("imsi") or "").strip()
            if not re.fullmatch(r"\d{15}", imsi):
                return self._json({"error": "IMSI 必须是 15 位数字"}, 400)
            params = {"mode": "ensure"}
            for k, src in (("sst", "sst"), ("sd", "sd"), ("fiveQI", "fiveQI")):
                if body.get(src) not in (None, ""):
                    params[k] = body[src]
            for k in ("ambr_ul", "ambr_dl"):
                if body.get("ambr") not in (None, ""):
                    params[k] = body["ambr"]
            if body.get("ambr_ul"):
                params["ambr_ul"] = body["ambr_ul"]
            if body.get("ambr_dl"):
                params["ambr_dl"] = body["ambr_dl"]
            if params.get("sst") is not None:
                params["sst"] = int(params["sst"])
            # 语义修正：界面上的“新增 UE”是新增一个 UE 节点（独立进程 + netns），
            # 不是只往库里加一条订户；订户不存在时 add_ue 会自动开户
            ue_params = {k: params[k] for k in ("fiveQI", "ambr_ul", "ambr_dl") if k in params}
            if params.get("sst") is not None:
                ue_params["sst"] = int(params["sst"])
            if params.get("sd"):
                ue_params["sd"] = str(params["sd"])
            from intentlib.actions_ue import AddUe
            from intentlib.actions import IntentError
            intent = {"action": "add_ue", "target": {"imsi": imsi},
                      "params": ue_params, "constraints": {}}
            try:
                AddUe().validate(intent)
            except IntentError as exc:
                return self._json({"error": str(exc)}, 400)
            # Allocate again when the queued CLI action actually holds the lock.
            ue_params.pop("index", None)
            return self._json(intent_job(f"新增 UE 节点 {imsi}", intent))

        if p == "/api/slices/activate":
            if body.get("sst") in (None, ""):
                return self._json({"error": "需要 sst"}, 400)
            intent = {"action": "activate_slice",
                      "target": {"imsi": (body.get("imsi") or "").strip() or None},
                      "params": {"sst": int(body["sst"]),
                                 "sd": (body.get("sd") or "ffffff").strip()},
                      "constraints": {"allow_upf_recreate": True,
                                      "allow_session_rebuild": True,
                                      # 新建的切片核心可能不认识 → 允许写 AMF/SMF 配置
                                      "allow_core_config_edit": True}}
            return self._json(intent_job(
                f"新建切片 {intent['params']['sst']}/{intent['params']['sd']}", intent))

        if p == "/api/ues/activate":
            imsi = (body.get("imsi") or "").strip()
            if not re.fullmatch(r"\d{15}", imsi):
                return self._json({"error": "IMSI 必须是 15 位数字"}, 400)
            return self._json(intent_job(f"激活 UE {imsi}", {
                "action": "switch_ue", "target": {"imsi": imsi}, "params": {},
                "constraints": {"allow_session_rebuild": True}}))

        if p == "/api/persist/apply":
            return self._json(start_job("persist: apply DB + config",
                                        [PY, APPLY_STATE, "--wait"]))
        self.send_error(404)

    # --- SSE
    def _stream(self, jid: str):
        job = JOBS.get(jid)
        if not job:
            return self._json({"error": "unknown job"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            pos = max(0, int(self.headers.get("Last-Event-ID") or 0))
        except ValueError:
            pos = 0
        idle = 0
        try:
            while True:
                data, pos = read_log(job, pos)
                if data:
                    self.wfile.write(f"id: {pos}\ndata: {json.dumps({'text': data}, ensure_ascii=False)}\n\n".encode())
                if data:
                    idle = 0
                else:
                    idle += 1
                if job["status"] not in ("running", "queued") and not data:
                    self.wfile.write(
                        f"data: {json.dumps({'done': True, 'status': job['status'], 'rc': job['rc']})}\n\n".encode())
                    self.wfile.flush()
                    return
                self.wfile.flush()
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError):
            return


def main() -> None:
    host, port = "127.0.0.1", 8099
    argv = sys.argv[1:]
    if "--host" in argv:
        host = argv[argv.index("--host") + 1]
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"intent-ui on http://{host}:{port}", flush=True)
    srv.serve_forever()


load_jobs()

if __name__ == "__main__":
    main()
