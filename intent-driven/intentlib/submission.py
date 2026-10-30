"""One intent constructor for previews and execution; all writes still use CLI."""
import copy
import json
import re

from . import nl
from .actions import IntentError
from .ledger import Ledger

ROUTES = {'/api/apply','/api/ran/mcs','/api/ran/prb','/api/ues',
          '/api/slices/activate','/api/ues/activate'}


def supported(route):
    return route in ROUTES or bool(re.fullmatch(r'/api/intents/[^/]+/replay',route))


def intent_for(route, body):
    if route == '/api/apply':
        text = (body.get('text') or '').strip()
        if not text:
            raise IntentError('空意图')
        return nl.parse(text,use_llm=not body.get('no_llm',False))
    if re.fullmatch(r'/api/intents/[^/]+/replay',route):
        row = Ledger().get(route.split('/')[3])
        if not row:
            raise IntentError('账本中没有这个意图')
        it = json.loads(row['intent_json'])
        if it['action']=='add_ue':
            it['params'].pop('index',None)
        return it
    target, params, constraints = {}, {}, {}
    if route == '/api/ran/mcs':
        action, params = 'set_ran_dl_mcs', {'max_mcs':body.get('max_mcs')}
    elif route == '/api/ran/prb':
        action = 'set_ran_slice_prb'
        params = {k:body[k] for k in ('sst','sd','enabled','max_prb_percent') if k in body}
    elif route == '/api/ues':
        action, target = 'add_ue', {'imsi':body.get('imsi')}
        params = {k:body[k] for k in ('sst','sd','fiveQI','ambr_ul','ambr_dl') if body.get(k) not in (None,'')}
        if body.get('ambr'):
            params.update(ambr_ul=body['ambr'],ambr_dl=body['ambr'])
    elif route == '/api/slices/activate':
        action = 'activate_slice'
        target = {'imsi':body.get('imsi') or None}
        params = {'sst':body.get('sst'),'sd':body.get('sd') or 'ffffff'}
        constraints = {'allow_upf_recreate':True,'allow_session_rebuild':True,'allow_core_config_edit':True}
    elif route == '/api/ues/activate':
        action, target = 'switch_ue', {'imsi':body.get('imsi')}
        constraints = {'allow_session_rebuild':True}
    else:
        raise IntentError('该操作不支持意图预检查')
    for key in ('sst','fiveQI'):
        if key in params:
            v = params[key]
            if isinstance(v,bool) or not re.fullmatch(r'[0-9]+',str(v)):
                raise IntentError(f'{key} 必须为整数')
            params[key] = int(v)
    return {'action':action,'target':target,'params':params,'constraints':constraints}
