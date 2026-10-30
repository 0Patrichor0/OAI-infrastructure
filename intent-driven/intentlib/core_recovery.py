"""Restore SMF/PFCP before resubscribing NWDAF; retain concise failure evidence."""
import json
import subprocess
import time


def run(args, timeout=15):
    p = subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    return p.returncode,(p.stdout or '')+(p.stderr or '')


def state(name):
    rc, text = run(['sudo','docker','inspect',name,'--format','{{json .State}}'])
    if rc:
        return {'error':text[-200:]}
    raw = json.loads(text)
    return {k:raw.get(k) for k in ('Status','Running','StartedAt','ExitCode','OOMKilled')} | {
        'health':(raw.get('Health') or {}).get('Status')}


def association():
    return run(['sudo','docker','exec','vpp-upf','/openair-upf/bin/vppctl','show','upf','association'])[1][-1000:]


def diagnostics():
    evidence = {'sampled_at':time.time()}
    for name in ('oai-smf','vpp-upf','oai-nwdaf-sbi'):
        try:evidence[name] = state(name)
        except Exception as exc:evidence[name] = {'error':str(exc)}
    try:evidence['pfcp'] = association()
    except Exception as exc:evidence['pfcp_error'] = str(exc)
    try:
        _, log = run(['sudo','docker','logs','--tail','300','oai-smf'])
        evidence['smf_events'] = [s for s in log.splitlines() if any(word in s for word in (
            'ASSOCIATION','association','Caught signal','exiting','terminate message','Freeing Allocated','PFCP HEARTBEAT'))][-20:]
    except Exception as exc:evidence['log_error'] = str(exc)
    return evidence


def recover_smf(timeout=150):
    """Health alone cannot prove PFCP association or stable process identity."""
    sbi = state('oai-nwdaf-sbi')
    was_running = bool(sbi.get('Running'))
    if was_running:
        rc,_ = run(['sudo','docker','stop','-t','10','oai-nwdaf-sbi'],timeout=30)
        if rc:return False
    try:
        rc,_ = run(['sudo','docker','restart','oai-smf'],timeout=60)
        if rc:return False
        deadline = time.monotonic()+timeout
        stable_start, stable_since = None,None
        while time.monotonic()<deadline:
            smf = state('oai-smf')
            start = smf.get('StartedAt')
            if start != stable_start:
                stable_start,stable_since = start,time.monotonic()
            if smf.get('health')=='healthy' and time.monotonic()-stable_since>=8:
                rc,log = run(['sudo','docker','logs','--since',start,'oai-smf'])
                if rc==0 and 'Received N4 ASSOCIATION SETUP RESPONSE' in log and 'Node: oai-smf' in association():
                    return True
            time.sleep(2)
        return False
    finally:
        if was_running:
            rc,_ = run(['sudo','docker','start','oai-nwdaf-sbi'],timeout=45)
            if rc:
                raise RuntimeError('SMF 恢复后 NWDAF SBI 未能启动，请查看诊断记录')
