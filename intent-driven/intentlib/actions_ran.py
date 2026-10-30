"""Runtime-only RAN actions using a real control xApp and E2 DU callback."""
from .actions import Action, IntentError, ACTIONS
from . import ran_control, ue

class SetRanDlMcs(Action):
    name='set_ran_dl_mcs'
    description='通过控制 xApp/FlexRIC/E2 热更新 DU 小区下行 MCS 上限；影响全小区，DU 重启后复位'

    def validate(self,intent):
        p=intent.setdefault('params',{})
        value=p.get('max_mcs')
        if isinstance(value,bool) or not isinstance(value,int) or not 0<=value<=28:
            raise IntentError('max_mcs 必须是 0-28 的整数，且不得低于 DU 配置的最小 MCS')
        target=intent.setdefault('target',{})
        if target.get('imsi'):
            raise IntentError('此动作作用于 DU 小区全部 UE，不能指定某个 IMSI')
        if p.get('cell_index',0)!=0:
            raise IntentError('当前只支持单 DU 的 cell_index=0')
        p['cell_index']=0

    def plan(self,intent):
        return ['通过控制 xApp 经 FlexRIC/E2 查询 DU 当前下行 MCS 上限并保存快照',
                f"通过 E2 MAC SM 142 把小区 0 的下行 MCS 上限设为 {intent['params']['max_mcs']}；不重启 CU/DU/UE",
                '回读真实调度参数、核对 DU 进程及 MAC 遥测，并验证用户面通信',
                '影响全小区的新下行调度；HARQ 重传保持原参数。DU 重启后恢复启动配置，需重新下发']

    def snapshot(self,intent,before=None):
        before=before if before is not None else ran_control.request('query')['state']
        if intent['params']['max_mcs']<before['dl_min_mcs']:
            raise IntentError(f"目标低于 DU 配置下限 {before['dl_min_mcs']}；未修改调度参数")
        return {'kind':self.name,'before':before,'expected_revision':before['revision']+1,
                'prior_owner':ran_control.current_owner(before)}

    def execute(self,intent):
        result=ran_control.request('set',intent['params']['max_mcs'])
        ran_control.remember_owner(result['state'],result['state']['revision'])
        return result

    def verify(self,intent,result):
        live=ran_control.request('query');state=live['state'];wanted=intent['params']['max_mcs'];t=live['telemetry']
        result['_verification_read']={'state':state,'telemetry':t}
        return [
            {'check':'控制 xApp 收到 E2 ACK','ok':bool(result.get('ack'))},
            {'check':'DU 执行回执确认写入实际调度参数','ok':result.get('state',{}).get('dl_max_mcs')==wanted},
            {'check':'DU 进程保持且运行态上限与目标一致','ok':state['pid']==result.get('state',{}).get('pid') and state['dl_max_mcs']==wanted,'detail':f"PID {state['pid']}, cap {state['dl_max_mcs']}"},
            {'check':'E2 MAC 遥测持续到达','ok':t.get('indications',0)>0 and t.get('observations',0)>0,'detail':str(t)},
            {'check':'MAC 回报的下行 MCS 不超过新上限','ok':t.get('observations',0)>0 and t.get('observed_max_mcs',99)<=wanted,'detail':str(t.get('observed_max_mcs'))},
            {'check':'主 UE 用户面可达','ok':ue.ping_ok(src=ue.TUN)},
        ]

    def rollback(self,intent,snapshot):
        current=ran_control.request('query')['state'];before=snapshot['before']
        if current['pid']!=before['pid']:
            raise IntentError('DU 已重启，旧进程快照不再适用；请读取当前值后重新下发')
        if current['revision']==before['revision'] and current['dl_max_mcs']==before['dl_max_mcs']:
            return 'DU 策略未改变，无需恢复'
        owner=ran_control.current_owner(current)
        if not owner or owner.get('owner_id')!=snapshot['expected_revision']:
            raise IntentError('已有后续 E2 控制修改策略，请先回滚后续操作')
        restored=ran_control.request('set',before['dl_max_mcs'])
        prior=snapshot.get('prior_owner') or {}
        ran_control.remember_owner(restored['state'],prior.get('owner_id'))
        if restored['state']['dl_max_mcs']!=before['dl_max_mcs'] or not ue.ping_ok(src=ue.TUN):
            raise IntentError('回滚后参数或通信未恢复')
        return f"已通过 E2 恢复 DU 下行 MCS 上限为 {before['dl_max_mcs']}，DU PID 保持 {current['pid']}"

ACTIONS[SetRanDlMcs.name]=SetRanDlMcs()
