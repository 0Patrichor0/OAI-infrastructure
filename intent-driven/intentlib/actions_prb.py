"""Slice-wide DL new-transmission PRB ceilings with PID/revision-safe rollback."""
import re
from .actions import Action,IntentError,ACTIONS
from . import prb_control,ue,ue_nodes,slices

class SetRanSlicePrb(Action):
    name='set_ran_slice_prb'
    description='经 xApp/FlexRIC/E2 设置切片共享的下行新传 PRB 上限；单 DU 单小区单波束，支持取消与回滚'
    def validate(self,intent):
        p=intent.setdefault('params',{});sst=p.get('sst');sd=p.get('sd');enabled=p.setdefault('enabled',True)
        if isinstance(sst,bool) or not isinstance(sst,int) or not 0<=sst<=255 or not isinstance(sd,str) or not re.fullmatch('[0-9a-fA-F]{6}',sd):raise IntentError('需要整数 SST 和六位十六进制 SD')
        p['sd']=sd.lower()
        if not isinstance(enabled,bool):raise IntentError('enabled 必须为布尔值')
        value=p.get('max_prb_percent')
        if enabled and (isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=100):raise IntentError('max_prb_percent 必须为 1-100 的整数')
        if intent.setdefault('target',{}).get('imsi'):raise IntentError('该动作按切片共享预算，不能指定单 IMSI')
        if p.get('cell_index',0)!=0:raise IntentError('当前只支持 cell_index=0')
        p['cell_index']=0
    def plan(self,intent):
        p=intent['params'];change=f"设为 {p.get('max_prb_percent')}%" if p['enabled'] else '取消限额'
        return [f"经 E2 查询切片 {p['sst']}/{p['sd']} 的 DU 实际策略、DRB 和 PRB 统计",f'控制 xApp → FlexRIC → E2 → DU：下行新传 PRB 上限{change}',
                '同一切片内所有 UE 共享每个下行时隙预算；HARQ 重传和纯控制授权单独统计',
                '回读调度实际分配计数、超限计数及用户面通信；不重启服务',
                '这是使用上限，不是最低预留；DU 重启后复位，旧进程快照不直接回滚']
    def snapshot(self,intent,before=None):
        p=intent['params']
        if p['enabled'] and not any(x['sst']==p['sst'] and x['sd']==p['sd'] and x['network_ready'] for x in slices.inventory()['slices']):raise IntentError('切片核心网/RAN/UPF 未就绪，请先激活切片')
        before=before if before is not None else prb_control.request('query',p['sst'],p['sd'])['state']
        if p['enabled']:
            if before['num_beams']!=1:raise IntentError('当前 PRB 控制只支持单波束小区')
            if before['matched_users']==0:raise IntentError('DU 尚无该切片的 DRB，无法验证调度效果，请先接入 UE')
            if before['bwp_prbs']*p['max_prb_percent']//100<5:raise IntentError('比例低于 DU 最小 5 PRB 授权，请提高上限')
        return {'kind':self.name,'before':before,'prior_owner':prb_control.current_owner(before),'expected_revision':before['revision']+1}
    def execute(self,intent):
        p=intent['params'];result=prb_control.request('set' if p['enabled'] else 'clear',p['sst'],p['sd'],p['max_prb_percent'] if p['enabled'] else 0)
        prb_control.remember_owner(result['state'],result['state']['revision']);return result
    def verify(self,intent,result):
        p=intent['params'];probes=[];main=slices.ue_conf()
        for session in slices.all_sessions():
            ns=session.get('nssai') or ((main['sst'],main['sd']) if session['node']=='ue1' else None)
            if ns and (ns[0],slices.sd_hex(ns[1]))==(p['sst'],p['sd']):
                ok=ue.ping_ok(src=ue.TUN) if session['node']=='ue1' else ue_nodes.ping(int(session['node'][2:]));probes.append(ok)
        live=prb_control.request('query',p['sst'],p['sd']);s=live['state'];t=live['telemetry'];enabled=p['enabled']
        result['_verification_read']={'state':s,'telemetry':t}
        desired=s['enabled']==enabled and (not enabled or s['max_prb_percent']==p['max_prb_percent'])
        checks=[{'check':'E2 ACK 与 DU 执行回执已确认','ok':bool(result.get('ack'))},
                {'check':'DU 进程保持且切片限额与目标一致','ok':s['pid']==result['state']['pid'] and desired,'detail':f"PID {s['pid']}, enabled={s['enabled']}, percent={s['max_prb_percent']}"},
                {'check':'E2 MAC 遥测持续到达','ok':t.get('indications',0)>0,'detail':str(t)},
                {'check':'目标切片用户面通信','ok':all(probes) and (bool(probes) or not enabled)}]
        if enabled:checks.extend([
            {'check':'已观测目标切片的真实新传 PRB 授权','ok':s['new_grants']>0 and s['matched_users']>0,'detail':f"users={s['matched_users']}, grants={s['new_grants']}, prbs={s['new_prbs']}"},
            {'check':'同切片合计新传 PRB 未超过时隙上限','ok':s['violations']==0 and s['peak_new_prbs']<=s['limit_at_peak'],'detail':f"peak={s['peak_new_prbs']}, limit_at_peak={s['limit_at_peak']}, violations={s['violations']}, limited_requests={s['limited_requests']}"}])
        return checks
    def rollback(self,intent,snapshot):
        p=intent['params'];before=snapshot['before'];current=prb_control.request('query',p['sst'],p['sd'])['state']
        if current['pid']!=before['pid']:raise IntentError('DU 已重启，旧 PRB 快照不再适用')
        if current['revision']==before['revision'] and current['enabled']==before['enabled'] and current['max_prb_percent']==before['max_prb_percent']:return 'PRB 策略未改变'
        owner=prb_control.current_owner(current)
        if not owner or owner.get('owner_id')!=snapshot['expected_revision']:raise IntentError('该切片已有后续 PRB 控制，请先回滚后续操作')
        restored=prb_control.request('set' if before['enabled'] else 'clear',p['sst'],p['sd'],before['max_prb_percent'] if before['enabled'] else 0)
        prb_control.remember_owner(restored['state'],(snapshot.get('prior_owner') or {}).get('owner_id'))
        return f"已经 E2 恢复切片 {p['sst']}/{p['sd']} 的 PRB 策略：enabled={before['enabled']}, percent={before['max_prb_percent']}"

ACTIONS[SetRanSlicePrb.name]=SetRanSlicePrb()
