"""Read-only execution preview and repeatable checks under the mutation lock."""
import copy
import hashlib
import json
import subprocess
import time

from . import actions, db, slices
from .actions import IntentError, AUTH_T, SM_T, _extract, _sm_json

GLOBAL = {'activate_slice', 'boost_capacity', 'set_egress', 'set_ran_dl_mcs'}
QOS = {'set_qos', 'provision_service', 'reduce_latency', 'sla_guard'}


def sessions():
    rows = slices.all_sessions()
    conf = slices.ue_conf()
    for row in rows:
        if row['node'] == 'ue1' and not row.get('nssai'):
            row['nssai'] = [conf.get('sst'), conf.get('sd')]
            row['slice_source'] = 'UE 配置（日志无近期 NSSAI，不能替代 DRB 回读）'
    return rows


def affected(intent, rows):
    name, p = intent['action'], intent.get('params', {})
    imsi = (intent.get('target') or {}).get('imsi')
    if name in GLOBAL:
        return rows
    if name == 'set_ran_slice_prb':
        return [r for r in rows if r.get('nssai') and
                (r['nssai'][0], slices.sd_hex(r['nssai'][1])) == (p['sst'], p['sd'])]
    if name == 'switch_ue':
        return [r for r in rows if r['node'] == 'ue1']
    return [r for r in rows if r.get('imsi') == imsi]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def build(raw):
    intent = copy.deepcopy(raw)
    name = intent.get('action')
    act = actions.get(name)
    checks, changes, warnings = [], [], []
    current, ran = {}, None
    rows, selected, plan = [], [], []

    def check(label, ok, detail=''):
        checks.append({'check':label, 'ok':bool(ok), 'detail':str(detail)})

    def change(label, before, after):
        changes.append({'label':label, 'current':before, 'target':after})

    try:
        if act is None:
            raise IntentError('不支持该意图动作')
        act.validate(intent)
        check('动作参数与已有业务规则', True)
        rows = sessions()
        selected = affected(intent, rows)
        p = intent.setdefault('params', {})
        intent.setdefault('constraints', {})
        plan = act.plan(intent)
        imsi = (intent.get('target') or {}).get('imsi')
        if not imsi and name == 'activate_slice':
            imsi = slices.ue_conf().get('imsi')
        if imsi:
            uid = db.resolve_ueid(imsi, [SM_T])
            current = {'imsi':imsi, 'has_auth':bool(db.resolve_ueid(imsi, [AUTH_T])),
                       'has_subscription':bool(uid), 'qos':_extract(_sm_json(uid) or {}) if uid else {}}
            ns = db.json_one(f'SELECT singleNssai FROM {SM_T} WHERE ueid={db.quote(uid)}') if uid else None
            current['slice'] = {'sst':int(ns['sst']), 'sd':slices.sd_hex(ns.get('sd'))} if ns else None
            wanted = p.get('compensation',{}) if name=='sla_guard' else p
            for key in ('fiveQI','ambr_ul','ambr_dl','arp_priority'):
                if key in wanted:
                    change(key, current['qos'].get(key), wanted[key])
            if name=='sla_guard':
                change('SLA 判据', '执行时采样 RTT', {'metric':p['metric'],'threshold':p['threshold']})
            if p.get('sst') is not None:
                change('订户切片 SST/SD', current['slice'], {'sst':p['sst'],'sd':p.get('sd')})
            if name in QOS | {'switch_ue','activate_slice'}:
                check('目标订户鉴权和会话签约完整', current['has_auth'] and bool(uid))
            if name == 'add_ue':
                change('独立 UE 节点', '未创建', f"ue{p['index']} / {imsi}")
            if name == 'add_subscriber':
                change('鉴权 / 签约', [current['has_auth'],bool(uid)], [True,True])
        if name == 'set_ran_dl_mcs':
            from . import ran_control
            ran = ran_control.request('query')['state']
            current = {k:ran.get(k) for k in ('pid','revision','dl_min_mcs','dl_max_mcs')}
            change('DU 下行 MCS 上限',ran['dl_max_mcs'],p['max_mcs'])
            check('目标不低于 DU 配置下限', p['max_mcs'] >= ran['dl_min_mcs'], ran['dl_min_mcs'])
        if name == 'set_ran_slice_prb':
            from . import prb_control
            ran = prb_control.request('query',p['sst'],p['sd'])['state']
            current = {k:ran.get(k) for k in ('pid','revision','sst','sd','enabled','max_prb_percent','bwp_prbs','num_beams')}
            change('切片下行新传 PRB 上限', f"{ran['max_prb_percent']}%" if ran['enabled'] else '未限额',
                   f"{p['max_prb_percent']}%" if p['enabled'] else '未限额')
            if p['enabled']:
                check('单波束小区', ran['num_beams']==1)
                check('DU 存在目标切片 DRB',ran['matched_users']>0, f"{ran['matched_users']} 个 UE")
                check('满足最小 5 PRB 授权',ran['bwp_prbs']*p['max_prb_percent']//100 >= 5)
        if name in {'set_ran_slice_prb','add_ue','activate_slice','provision_service'}:
            inventory = slices.inventory()
            target = next((s for s in inventory['slices'] if s['sst']==p.get('sst') and s['sd']==p.get('sd')), None)
            if name == 'activate_slice':
                current['network_ready'] = bool(target and target['network_ready'])
                change('核心网 / CU / DU / UPF 切片',current['network_ready'],'全部加载并以 UE 会话和 ping 验收')
            elif name != 'set_ran_slice_prb' or p['enabled']:
                check('目标切片核心网、CU/DU、UPF 已就绪', target and target['network_ready'],
                      (target or {}).get('unavailable_reason','切片不存在'))
        if name in {'boost_capacity','set_egress'}:
            from .actions_more import _compose_env_get, BoostCapacity
            key = 'IF_3_IP_REMOTE' if name=='set_egress' else 'VPP_CORE_WORKER'
            current[key] = _compose_env_get(key)
            change('N6 下一跳' if name=='set_egress' else 'UPF worker 核列表',current[key],
                   p.get('target_dn_ip') if name=='set_egress' else BoostCapacity._corelist(p['workers']))
        if name == 'switch_ue':
            current['main_imsi'] = slices.ue_conf().get('imsi')
            change('主 UE IMSI',current['main_imsi'],imsi)
        if name in {'activate_slice','boost_capacity','set_egress'}:
            warnings.append('会中断全部 UE 会话，重建并逐个恢复；通常需要 2–4 分钟，失败时以账本为准。')
            if name=='set_egress':
                warnings.append('SMF 退出异常尚未根治；切换失败会尝试恢复原出口，并记录实际恢复结果。')
        elif name in QOS:
            main = slices.ue_conf().get('imsi')
            if selected and imsi != main:
                warnings.append('目标是附加 UE：当前动作仅修改签约，现有附加 UE 会话不会自动重建，下次建会话才生效。')
            elif selected and intent['constraints'].get('allow_session_rebuild',True):
                warnings.append('可能重建目标主 UE 会话，短时断流；SLA 达标时不会补偿。')
            else:
                warnings.append('当前仅修改签约，下一次建立会话时生效。')
        if name in {'set_ran_dl_mcs','set_ran_slice_prb'}:
            warnings.append('运行中通过 E2 生效；DU 重启后复位。吞吐和 RTT 受负载影响，不承诺一定改善。')
        if name == 'set_ran_slice_prb':
            warnings.append('同切片共享下行新传 PRB 上限；HARQ 重传单独计数，不提供最低带宽预留。')
    except Exception as exc:
        check('执行前检查',False,f'{type(exc).__name__}: {exc}')
    basis = {'action':name,'params':intent.get('params',{}),'target':intent.get('target',{}),
             'constraints':intent.get('constraints',{}),'current':current,
             'affected':[{k:r.get(k) for k in ('node','imsi','ip','nssai')} for r in selected]}
    return {'ok':True,'valid':all(c['ok'] for c in checks),'intent':intent,'description':getattr(act,'description',''),
            'plan':plan,'checks':checks,'changes':changes,'affected_ues':selected,'warnings':warnings,
            'sampled_at':time.time(),'fingerprint':digest(basis),'ran_state':ran}


def enforce(intent, expected=None):
    preview = build(intent)
    if not preview['valid']:
        raise IntentError('预检查未通过：'+'；'.join(c['detail'] or c['check'] for c in preview['checks'] if not c['ok']))
    if expected and (time.time()>expected['expires_at'] or preview['fingerprint']!=expected['fingerprint']):
        raise IntentError('预览已过期或当前状态发生变化，未执行变更；请重新预检查')
    return preview
