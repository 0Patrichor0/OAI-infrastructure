"""Small measured windows, keyed by UE identity; missing data stays missing."""
import concurrent.futures
import json
import re
import subprocess
import time

from . import preflight, ue


def command(row, args):
    prefix = [] if row['node']=='ue1' else ['sudo','ip','netns','exec',row['node']]
    return subprocess.run(prefix+args,capture_output=True,text=True,timeout=8)


def counters(row):
    try:
        p = command(row,['ip','-s','-j','link','show','dev',ue.TUN])
        data = json.loads(p.stdout)[0]
        stats = data.get('stats64') or data.get('stats')
        return {'ifindex':data['ifindex'],'rx':stats['rx']['bytes'],'tx':stats['tx']['bytes'],
                'at':time.monotonic()}
    except (OSError,ValueError,IndexError,KeyError,subprocess.TimeoutExpired):
        return None


def rtt(row):
    try:
        p = command(row,['ping','-n','-c','3','-i','0.2','-W','1','-I',ue.TUN,ue.EXT_DN])
        loss = re.search(r'([\d.]+)% packet loss',p.stdout)
        avg = re.search(r'(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = ([\d.]+)/([\d.]+)/([\d.]+)',p.stdout)
        return {'rtt_ms':float(avg[2]) if avg else None,'loss_percent':float(loss[1]) if loss else None,
                'probe_target':ue.EXT_DN, 'probe_packets':3}
    except (OSError,subprocess.TimeoutExpired) as exc:
        return {'rtt_ms':None,'loss_percent':None,'error':str(exc)}


def measure_ue(row, window_s):
    start = time.monotonic()
    before = counters(row)
    probe = rtt(row)
    time.sleep(max(0,window_s-(time.monotonic()-start)))
    after = counters(row)
    stable = before and after and before['ifindex']==after['ifindex'] and after['rx']>=before['rx'] and after['tx']>=before['tx']
    elapsed = after['at']-before['at'] if stable else 0
    return {**{k:row.get(k) for k in ('node','imsi','ip','nssai')}, **probe,
            'window_s':round(elapsed,3) if elapsed else None,
            'dl_mbps':round((after['rx']-before['rx'])*8/elapsed/1e6,6) if elapsed else None,
            'ul_mbps':round((after['tx']-before['tx'])*8/elapsed/1e6,6) if elapsed else None,
            'rate_status':'measured' if elapsed else 'unavailable_or_interface_reset'}


def prb_delta(before, after):
    same = before and after and all(before.get(k)==after.get(k) for k in ('pid','revision','sst','sd'))
    keys = ('new_grants','new_prbs','limited_requests','violations','retx_prbs','control_prbs')
    elapsed = (after.get('timestamp_us',0)-before.get('timestamp_us',0))/1e6 if same else 0
    if not same or elapsed<=0 or any(after.get(k,0)<before.get(k,0) for k in keys):
        return {'status':'unavailable_or_policy_reset','window_s':None}
    return {'status':'measured','window_s':round(elapsed,3),
            'delta':{k:after.get(k,0)-before.get(k,0) for k in keys},
            'new_prbs_per_s':round((after['new_prbs']-before['new_prbs'])/elapsed,2)}


def capture(intent, initial_ran=None, window_s=3):
    started = time.time()
    rows = preflight.affected(intent,preflight.sessions())
    # A new UE/switch may not have been online before execution; record absence explicitly.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        measured = list(pool.map(lambda r:measure_ue(r,window_s),rows))
    ran = None
    if intent['action']=='set_ran_slice_prb':
        from . import prb_control
        p = intent['params']
        ran = prb_control.request('query',p['sst'],p['sd'])['state']
    elif intent['action']=='set_ran_dl_mcs':
        if initial_ran is not None:
            ran = dict(initial_ran)
        else:
            from . import ran_control
            reply = ran_control.request('query')
            ran = {**reply['state'],'telemetry':reply.get('telemetry',{})}
    result = {'started_at':started,'finished_at':time.time(),'ues':measured,'ran':ran,
              'source':'UE TUN 字节差分 + 3 次 ICMP；RAN 指标来自匹配的 E2 DU 回执',
              'note':'吞吐为当前业务负载下的实测速率，含探测流量，不是最大容量；不同采样窗口不能证明因果改善。'}
    if intent['action']=='set_ran_slice_prb':
        result['prb_window'] = prb_delta(initial_ran,ran)
    return result


def safe_capture(intent, initial_ran=None):
    try:
        return capture(intent,initial_ran)
    except Exception as exc:
        return {'error':f'{type(exc).__name__}: {exc}','finished_at':time.time(),'ues':[],
                'note':'效果采样不可用，不能据此宣称性能改善；执行验证另列。'}
