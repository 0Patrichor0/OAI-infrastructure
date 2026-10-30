import argparse,json,pathlib,subprocess,sys,tempfile,types,unittest
from unittest.mock import patch,MagicMock
ap=argparse.ArgumentParser();ap.add_argument('--root',required=True);opts=ap.parse_args();sys.path.insert(0,opts.root)
from intentlib import actions_ran as actions,ran_control as ran,nl
from intentlib.actions import IntentError

class McsTests(unittest.TestCase):
    def test_parse_cell_action(self):
        for text in ['将 DU 下行 MCS 上限设为 20','max_mcs=20']:
            it=nl.parse(text,use_llm=False);self.assertEqual(it['action'],'set_ran_dl_mcs');self.assertEqual(it['params']['max_mcs'],20)

    def test_reject_wrong_scope_and_fraction(self):
        for text in ['将上行 MCS 上限设为 20','给208950000000097 下行 MCS上限设为20','MCS上限设为20.5']:
            with self.assertRaises(ValueError):nl.parse(text,use_llm=False)

    def test_validate_limits(self):
        for bad in [True,-1,29,1.5,'20']:
            with self.assertRaises(IntentError):actions.SetRanDlMcs().validate({'params':{'max_mcs':bad}})

    def receipt_request(self,changes=None):
        state={'pid':123,'token':7,'op':1,'ok':True,'timestamp_us':2000,'dl_max_mcs':28,'dl_min_mcs':0,'revision':0}
        state.update(changes or {})
        receipt=MagicMock();receipt.lstat.return_value=types.SimpleNamespace(st_mode=0o100644,st_uid=0);receipt.read_text.return_value=json.dumps(state)
        proc=subprocess.CompletedProcess([],0,stdout='[xApp]: CONTROL ACK rx\nMCS_CONTROL_ACK token=7 received=1\nMCS_TELEMETRY {"indications":1,"observations":3,"observed_max_mcs":28}\n',stderr='')
        with patch.object(ran,'RECEIPT',receipt),patch.object(ran,'du_pid',return_value=123),patch.object(ran.secrets,'randbelow',return_value=6),patch.object(ran.time,'time_ns',return_value=1000000),patch.object(ran.subprocess,'run',return_value=proc):
            return ran._request('query')

    def test_readback_is_correlated(self):self.assertEqual(self.receipt_request()['state']['dl_max_mcs'],28)

    def test_unpatched_agent_is_not_sent_new_wire_format(self):
        with patch.object(ran,'du_pid',return_value=123),patch.object(ran.subprocess,'run',return_value=subprocess.CompletedProcess([],1)) as command:
            with self.assertRaises(IntentError):ran._request('query')
            self.assertEqual(command.call_count,1)
            self.assertIn('grep',command.call_args.args[0])

    def test_ack_with_stale_or_wrong_receipt_is_not_success(self):
        for wrong in [{'token':8},{'pid':456},{'timestamp_us':999},{'ok':False},{'op':2}]:
            with self.assertRaises(IntentError):self.receipt_request(wrong)

    def test_nested_rollback_and_owner_guard(self):
        state={'pid':123,'revision':0,'dl_min_mcs':0,'dl_max_mcs':28}
        def request(op,mcs=0):
            if op=='set':state.update(dl_max_mcs=mcs,revision=state['revision']+1)
            return {'state':dict(state),'ack':True,'telemetry':{'observations':3,'indications':1,'observed_max_mcs':state['dl_max_mcs']}}
        a=actions.SetRanDlMcs();first={'params':{'max_mcs':20}};second={'params':{'max_mcs':18}}
        with tempfile.TemporaryDirectory() as d,patch.object(ran,'OWNER',pathlib.Path(d,'owner.json')),patch.object(ran,'request',side_effect=request),patch.object(actions.ue,'ping_ok',return_value=True):
            s1=a.snapshot(first);a.execute(first);s2=a.snapshot(second);a.execute(second)
            with self.assertRaises(IntentError):a.rollback(first,s1)
            a.rollback(second,s2);self.assertEqual(state['dl_max_mcs'],20)
            a.rollback(first,s1);self.assertEqual(state['dl_max_mcs'],28)

    def test_restart_invalidates_old_snapshot(self):
        with patch.object(ran,'request',return_value={'state':{'pid':456}}):
            with self.assertRaises(IntentError):actions.SetRanDlMcs().rollback({}, {'before':{'pid':123}})

    def test_absent_telemetry_prevents_verified_state(self):
        result={'state':{'pid':123,'dl_max_mcs':20},'ack':True}
        with patch.object(ran,'request',return_value={**result,'telemetry':{}}),patch.object(actions.ue,'ping_ok',return_value=True):
            self.assertFalse(all(c['ok'] for c in actions.SetRanDlMcs().verify({'params':{'max_mcs':20}},result)))

if __name__=='__main__':unittest.main(argv=[sys.argv[0]],verbosity=2)
