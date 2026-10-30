"""Fail-closed evidence for core configuration and loaded network processes."""
import json, os, subprocess, time
from pathlib import Path
import yaml

def core_document(path):
    # BaseLoader preserves SD scalars such as 000123 rather than treating them as octal.
    return yaml.load(Path(path).read_text(), Loader=yaml.BaseLoader) or {}

def core_checks(document, sst, sd):
    def match(value):
        try:return int(value.get('sst', -1)) == int(sst) and int(str(value.get('sd', 'ffffff')),16) == int(sd,16)
        except (TypeError,ValueError,AttributeError):return False
    amf=document.get('amf',{});smf=document.get('smf',{})
    return {
        'AMF nssai': any(match(x) for plmn in amf.get('plmn_support_list',[]) for x in plmn.get('nssai',[])),
        'SMF discovery/default DNN': any(match(x.get('sNssai')) and any(d.get('dnn')=='default' for d in x.get('dnnSmfInfoList',[])) for x in smf.get('smf_info',{}).get('sNssaiSmfInfoList',[])),
        'SMF local subscription/default DNN': any(match(x.get('single_nssai')) and x.get('dnn')=='default' for x in smf.get('local_subscription_infos',[])),
    }

def run(args):
    result=subprocess.run(args,capture_output=True,text=True,timeout=25)
    if result.returncode:raise RuntimeError((result.stderr or result.stdout or 'command failed')[:200])
    return result.stdout

def registered_upf_slices():
    try:
        address=run(['sudo','docker','inspect','oai-nrf','--format','{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}']).strip()
        data=json.loads(run(['curl','--http2-prior-knowledge','-fsS','--max-time','8',f'http://{address}:8080/nnrf-disc/v1/nf-instances?target-nf-type=UPF&requester-nf-type=SMF']))
        out=set()
        for nf in data.get('nfInstances',[]):
            if nf.get('nfStatus')!='REGISTERED' or nf.get('nfType')!='UPF':continue
            for item in nf.get('upfInfo',{}).get('sNssaiUpfInfoList',[]):
                if not any(d.get('dnn')=='default' for d in item.get('dnnUpfInfoList',[])):continue
                ns=item.get('sNssai',{})
                # This deployed SMF interprets the UPF profile SD as decimal.
                try:out.add((int(ns['sst']),format(int(str(ns.get('sd','16777215')),10),'06x')))
                except (ValueError,KeyError):continue
        return out,''
    except Exception as exc:return set(),str(exc)

def ran_runtime(paths):
    names={'cucp.conf':'oran-cu-cp','cuup.conf':'oran-cu-up','du.conf':'oran-du'}
    checks=[]
    for path in paths:
        unit=names[os.path.basename(path)]
        try:
            props=dict(line.split('=',1) for line in run(['systemctl','show',unit,'-p','ActiveState','-p','MainPID','-p','ExecMainStartTimestampMonotonic']).splitlines() if '=' in line)
            pid=int(props.get('MainPID','0'))
            cmd=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            started=time.time()-time.monotonic()+int(props.get('ExecMainStartTimestampMonotonic','0'))/1e6
            ok=props.get('ActiveState')=='active' and pid>0 and os.fsencode(path) in cmd and started+0.1>=os.stat(path).st_mtime
            checks.append({'check':f'{unit} 已加载当前配置','ok':ok,'detail':f'PID {pid}；配置须早于进程启动' })
        except Exception as exc:checks.append({'check':f'{unit} 已加载当前配置','ok':False,'detail':str(exc)[:200]})
    return checks

def core_runtime(path):
    checks=[]
    try:containers=json.loads(run(['sudo','docker','inspect','oai-amf','oai-smf']))
    except Exception as exc:return [{'check':'AMF/SMF 运行态可读','ok':False,'detail':str(exc)}]
    for item in containers:
        name=item['Name'].lstrip('/');state=item.get('State',{})
        mounts=[m for m in item.get('Mounts',[]) if m.get('Source')==path]
        try:
            # Read the actual mounted file in the container as well as health.
            same=bool(mounts) and run(['sudo','docker','exec',name,'cat',mounts[0]['Destination']])==Path(path).read_text()
            from datetime import datetime
            started=datetime.fromisoformat(state['StartedAt'].replace('Z','+00:00')).timestamp()
            ok=state.get('Running') and state.get('Health',{}).get('Status')=='healthy' and same and started+0.1>=os.stat(path).st_mtime
            checks.append({'check':f'{name} 健康且已加载当前核心网配置','ok':bool(ok)})
        except Exception as exc:checks.append({'check':f'{name} 已加载当前核心网配置','ok':False,'detail':str(exc)[:200]})
    return checks
