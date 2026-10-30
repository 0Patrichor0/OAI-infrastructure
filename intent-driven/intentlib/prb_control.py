"""E2 customized MAC SM 142 slice PRB commands and correlated DU receipts."""
import fcntl,json,pathlib,re,secrets,stat,subprocess,threading,time
from .actions import IntentError
from .ran_control import du_pid, FLEXRIC, HOME
from .storage import atomic_text

XAPP=HOME/'ran/xapp_prb_control'
RECEIPT=pathlib.Path('/run/oran-du-e2-control/prb.json')
OWNER=HOME/'state/e2_prb_owners.json'
REQUEST_LOCK=HOME/'state/e2_mcs_request.lock'
LOCAL_LOCK=threading.RLock()

def owner_key(state):return f"{state['sst']}/{state['sd']}"
def owners():
    try:return json.loads(OWNER.read_text())
    except (OSError,ValueError):return {}
def current_owner(state):
    owner=owners().get(owner_key(state))
    return owner if owner and owner.get('pid')==state['pid'] and owner.get('revision')==state['revision'] else None
def remember_owner(state,owner_id):
    data=owners();data[owner_key(state)]={'pid':state['pid'],'revision':state['revision'],'owner_id':owner_id}
    atomic_text(OWNER,json.dumps(data))

def request(operation,sst,sd,percent=0):
    if operation not in ('query','set','clear') or isinstance(sst,bool) or not isinstance(sst,int) or not 0<=sst<=255 or not isinstance(sd,str) or not re.fullmatch('[0-9a-fA-F]{6}',sd):
        raise IntentError('SST 必须为 0-255 整数，SD 必须为六位十六进制')
    if isinstance(percent,bool) or not isinstance(percent,int) or not 0<=percent<=100 or (operation!='set' and percent!=0) or (operation=='set' and percent==0):
        raise IntentError('下行新传 PRB 上限必须为 1-100 的整数百分比')
    deadline=time.monotonic()+55
    if not LOCAL_LOCK.acquire(timeout=55):raise IntentError('E2 控制通道忙')
    try:
        with REQUEST_LOCK.open('a') as lock:
            while True:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise IntentError('E2 控制通道忙')
                    time.sleep(.05)
            try:return _request(operation,sst,sd.lower(),percent,deadline=deadline)
            finally:fcntl.flock(lock,fcntl.LOCK_UN)
    finally:LOCAL_LOCK.release()

def _request(operation,sst,sd,percent,deadline=None):
    if deadline is not None and deadline-time.monotonic()<21:
        raise IntentError('E2 等待已到时限，未发送新的控制请求')
    pid=du_pid()
    supported=subprocess.run(['sudo','grep','-aFq','[E2-PRB] token=',f'/proc/{pid}/exe'],capture_output=True,timeout=10)
    if supported.returncode:raise IntentError('当前 DU 未加载 PRB 控制扩展，未发送控制')
    token=secrets.randbelow(0x1ffff)+1;started=time.time_ns()//1000
    remaining=min(35,deadline-time.monotonic()-10) if deadline is not None else 35
    if remaining<=0:raise IntentError('E2 等待已到时限，未发送新的控制请求')
    try:p=subprocess.run([str(XAPP),operation,str(token),str(sst),sd,str(percent)],cwd=FLEXRIC,capture_output=True,text=True,timeout=remaining)
    except (OSError,subprocess.TimeoutExpired) as exc:raise IntentError(f'E2 PRB 请求未确认，请核对账本后回滚: {exc}') from exc
    output=(p.stdout or '')+(p.stderr or '')
    if p.returncode or f'PRB_CONTROL_ACK token={token} received=1' not in output or '[xApp]: CONTROL ACK rx' not in output:raise IntentError('PRB 控制 xApp 未确认 ACK: '+output[-800:])
    try:
        info=RECEIPT.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o022:raise ValueError('DU 回执权限不可信')
        state=json.loads(RECEIPT.read_text())
    except (OSError,ValueError) as exc:raise IntentError('缺少可靠 DU PRB 执行回执: '+str(exc)) from exc
    op={'query':0xd1,'set':0xd2,'clear':0xd3}[operation]
    if state.get('token')!=token or state.get('op')!=op or state.get('pid')!=pid or du_pid()!=pid or state.get('timestamp_us',0)<started or state.get('sst')!=sst or state.get('sd')!=sd:
        raise IntentError('PRB 回执与本次请求、切片或 DU 进程不匹配')
    if not state.get('ok'):raise IntentError('DU 拒绝 PRB 控制：请检查最小 5 PRB 授权、单波束限制及切片表容量')
    if operation=='set' and (not state.get('enabled') or state.get('max_prb_percent')!=percent):raise IntentError('PRB 策略没有按请求生效')
    if operation=='clear' and state.get('enabled'):raise IntentError('PRB 限额未清除')
    found=re.findall(r'PRB_TELEMETRY (\{[^\n]+\})',output)
    return {'state':state,'telemetry':json.loads(found[-1]) if found else {},'ack':True,'transport':'FlexRIC custom MAC SM 142 over E2'}
