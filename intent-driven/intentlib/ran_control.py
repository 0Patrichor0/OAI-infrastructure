"""Control DU through an E42 xApp; the local file is a read-only execution receipt."""
import fcntl, json, os, pathlib, re, secrets, stat, subprocess, threading, time
from .actions import IntentError

HOME = pathlib.Path('/home/ubuntu/intent-driven')
XAPP = HOME/'ran/xapp_mcs_control'
RECEIPT = pathlib.Path('/run/oran-du-e2-control/state.json')
FLEXRIC = '/home/ubuntu/oai-ran-e2/openair2/E2AP/flexric'
OWNER = HOME/'state/e2_mcs_owner.json'
REQUEST_LOCK = HOME/'state/e2_mcs_request.lock'
LOCAL_LOCK = threading.RLock()

def current_owner(state):
    try:owner=json.loads(OWNER.read_text())
    except (OSError,ValueError):return None
    return owner if owner.get('pid')==state['pid'] and owner.get('revision')==state['revision'] else None

def remember_owner(state, owner_id):
    from .storage import atomic_text
    atomic_text(OWNER,json.dumps({'pid':state['pid'],'revision':state['revision'],'owner_id':owner_id}))

def du_pid():
    p=subprocess.run(['systemctl','show','oran-du','-p','MainPID','--value'],capture_output=True,text=True,timeout=10)
    value=p.stdout.strip()
    if p.returncode or not value.isdigit() or int(value)==0:
        raise IntentError('DU 没有运行，未下发 E2 控制')
    return int(value)

def request(operation, mcs=0):
    # Read-only UI queries share this lock, without occupying the mutation lock.
    # Hold it through receipt validation so another query cannot replace the nonce.
    deadline=time.monotonic()+55
    if not LOCAL_LOCK.acquire(timeout=55):raise IntentError('E2 控制通道忙，请稍后再试')
    try:
        with REQUEST_LOCK.open('a') as lock:
            while True:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise IntentError('E2 控制通道忙，请稍后再试')
                    time.sleep(.05)
            try:return _request(operation,mcs,deadline=deadline)
            finally:fcntl.flock(lock,fcntl.LOCK_UN)
    finally:LOCAL_LOCK.release()

def _request(operation, mcs=0, deadline=None):
    if operation not in ('query','set') or isinstance(mcs,bool) or not isinstance(mcs,int) or not 0<=mcs<=28:
        raise IntentError('无效的 E2 MCS 命令')
    if deadline is not None and deadline-time.monotonic()<21:
        raise IntentError('E2 等待已到时限，未发送新的控制请求')
    pid=du_pid()
    # Older deployed MAC agents abort on any command other than the demo value 42.
    # Probe the running ELF before sending this deployment's extended wire format.
    supported=subprocess.run(['sudo','grep','-aFq','[E2-MCS] token=',f'/proc/{pid}/exe'],capture_output=True,timeout=10)
    if supported.returncode:
        raise IntentError('当前 DU 进程未加载 MCS E2 扩展，未发送控制；请先加载配套 RAN 版本')
    token=secrets.randbelow(0xffffff)+1;started=time.time_ns()//1000
    remaining=min(35,deadline-time.monotonic()-10) if deadline is not None else 35
    if remaining<=0:raise IntentError('E2 等待已到时限，未发送新的控制请求')
    try:
        p=subprocess.run([str(XAPP),operation,str(token),str(mcs)],cwd=FLEXRIC,capture_output=True,text=True,timeout=remaining)
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise IntentError(f'E2 控制未确认；若为 set 请核对账本并回滚: {exc}') from exc
    output=(p.stdout or '')+(p.stderr or '')
    if p.returncode or f'MCS_CONTROL_ACK token={token} received=1' not in output or '[xApp]: CONTROL ACK rx' not in output:
        raise IntentError('控制 xApp 未确认 E2 ACK: '+output[-800:])
    try:
        info=RECEIPT.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o022:
            raise ValueError('DU 执行回执文件权限不可信')
        state=json.loads(RECEIPT.read_text())
    except (OSError,ValueError) as exc:raise IntentError(f'ACK 已收到，但缺少可靠 DU 执行回执: {exc}') from exc
    expected_op=1 if operation=='query' else 2
    if state.get('token')!=token or state.get('op')!=expected_op or state.get('pid')!=pid or du_pid()!=pid or state.get('timestamp_us',0)<started:
        raise IntentError('DU 回执与本次请求或运行进程不匹配，不能判定控制成功')
    if not state.get('ok'):
        raise IntentError(f"DU 拒绝控制；允许下限 {state.get('dl_min_mcs')}，当前上限 {state.get('dl_max_mcs')}")
    if operation=='set' and state.get('dl_max_mcs')!=mcs:
        raise IntentError('DU 实际 MCS 上限与请求不一致')
    found=re.findall(r'MCS_TELEMETRY (\{[^\n]+\})',output)
    return {'state':state,'telemetry':json.loads(found[-1]) if found else {},'transport':'FlexRIC custom MAC SM 142 over E2','ack':True}
