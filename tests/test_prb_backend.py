import argparse,json,pathlib,subprocess,sys,tempfile,types,unittest
from unittest.mock import patch,MagicMock
ap=argparse.ArgumentParser();ap.add_argument('--root',required=True);opts=ap.parse_args();sys.path.insert(0,opts.root)
from intentlib import actions_prb as actions,prb_control as ran,nl
from intentlib.actions import IntentError

def intent(percent=30,sd='000003',enabled=True):
    return {'params':{'sst':3,'sd':sd,'enabled':enabled,'max_prb_percent':percent},'target':{}}

class PrbTests(unittest.TestCase):
    def test_parse_set_and_clear(self):
        it=nl.parse('将切片 SST 3 SD 000003 的下行 PRB 上限设为 30%',use_llm=False)
        self.assertEqual(it['action'],'set_ran_slice_prb');self.assertEqual(it['params']['max_prb_percent'],30)
        self.assertFalse(nl.parse('取消切片 SST 3 SD 000003 的 PRB 上限',use_llm=False)['params']['enabled'])
    def test_reject_unsupported_meaning(self):
        for text in ['SST3 SD000003 上行PRB 30%','SST3 SD000003 PRB保底30%','SST3 SD000003 PRB30.5%','SST3 PRB30%','SST3 SD000003 IMSI208950000000095 PRB30%']:
            with self.subTest(text=text),self.assertRaises(ValueError):nl.parse(text,use_llm=False)
    def test_validation(self):
        for value in [True,0,101,30.5,'30',None]:
            with self.assertRaises(IntentError):actions.SetRanSlicePrb().validate(intent(value))
        for change in [{'sst':256},{'sst':True},{'sd':'3'},{'enabled':'false'},{'cell_index':1}]:
            it=intent();it['params'].update(change)
            with self.assertRaises(IntentError):actions.SetRanSlicePrb().validate(it)
        it=intent(enabled=False);it['params'].pop('max_prb_percent');actions.SetRanSlicePrb().validate(it)
    def receipt(self,changes=None,mode=0o100644,uid=0):
        state={'pid':123,'token':7,'op':0xd1,'ok':True,'timestamp_us':2000,'sst':3,'sd':'000003','enabled':False,'max_prb_percent':100}
        state.update(changes or {})
        file=MagicMock();file.lstat.return_value=types.SimpleNamespace(st_mode=mode,st_uid=uid);file.read_text.return_value=json.dumps(state)
        proc=subprocess.CompletedProcess([],0,stdout='[xApp]: CONTROL ACK rx\nPRB_CONTROL_ACK token=7 received=1\nPRB_TELEMETRY {"indications":1}\n',stderr='')
        with patch.object(ran,'RECEIPT',file),patch.object(ran,'du_pid',return_value=123),patch.object(ran.secrets,'randbelow',return_value=6),patch.object(ran.time,'time_ns',return_value=1000000),patch.object(ran.subprocess,'run',return_value=proc):return ran._request('query',3,'000003',0)
    def test_correlated_receipt(self):self.assertEqual(self.receipt()['state']['sst'],3)
    def test_ack_does_not_replace_trusted_receipt(self):
        for change in [{'token':8},{'op':0xd2},{'sst':1},{'sd':'000005'},{'pid':456},{'timestamp_us':999},{'ok':False}]:
            with self.subTest(change=change),self.assertRaises(IntentError):self.receipt(change)
        for mode,uid in [(0o100666,0),(0o120644,0),(0o100644,1000)]:
            with self.assertRaises(IntentError):self.receipt(mode=mode,uid=uid)
    def test_old_agent_blocked_before_sdk_call(self):
        with patch.object(ran,'du_pid',return_value=123),patch.object(ran.subprocess,'run',return_value=subprocess.CompletedProcess([],1)) as cmd:
            with self.assertRaises(IntentError):ran._request('set',3,'000003',30)
            self.assertEqual(cmd.call_count,1)
    def test_preflight_requires_drb_and_feasible_budget(self):
        for change in [{'matched_users':0},{'num_beams':2},{'bwp_prbs':10}]:
            state={'matched_users':1,'num_beams':1,'bwp_prbs':106};state.update(change)
            with patch.object(actions.slices,'inventory',return_value={'slices':[{'sst':3,'sd':'000003','network_ready':True}]}),patch.object(ran,'request',return_value={'state':state}):
                with self.assertRaises(IntentError):actions.SetRanSlicePrb().snapshot(intent())
    def test_slice_independent_nested_rollback(self):
        states={}
        def request(op,sst,sd,percent=0):
            s=states.setdefault(sd,{'pid':123,'revision':0,'sst':sst,'sd':sd,'enabled':False,'max_prb_percent':100,'num_beams':1,'bwp_prbs':106,'matched_users':1})
            if op!='query':s.update(enabled=op=='set',max_prb_percent=percent if op=='set' else 100,revision=s['revision']+1)
            return {'state':dict(s),'ack':True}
        inventory={'slices':[{'sst':3,'sd':sd,'network_ready':True} for sd in ['000003','000007']]}
        a=actions.SetRanSlicePrb();first=intent();second=intent(50);other=intent(40,'000007')
        with tempfile.TemporaryDirectory() as d,patch.object(ran,'OWNER',pathlib.Path(d,'owners.json')),patch.object(ran,'request',side_effect=request),patch.object(actions.slices,'inventory',return_value=inventory):
            s1=a.snapshot(first);a.execute(first);s2=a.snapshot(second);a.execute(second);s3=a.snapshot(other);a.execute(other)
            with self.assertRaises(IntentError):a.rollback(first,s1)
            a.rollback(second,s2);self.assertEqual(states['000003']['max_prb_percent'],30)
            a.rollback(first,s1);self.assertFalse(states['000003']['enabled']);self.assertTrue(states['000007']['enabled'])
            a.rollback(other,s3);self.assertFalse(states['000007']['enabled'])
    def test_restart_invalidates_snapshot(self):
        with patch.object(ran,'request',return_value={'state':{'pid':456}}):
            with self.assertRaises(IntentError):actions.SetRanSlicePrb().rollback(intent(),{'before':{'pid':123}})
    def test_verification_rejects_real_violations(self):
        state={'pid':123,'enabled':True,'max_prb_percent':30,'matched_users':2,'new_grants':10,'new_prbs':310,'violations':1,'peak_new_prbs':32,'limit_at_peak':31,'limited_requests':3}
        response={'state':state,'telemetry':{'indications':1},'ack':True}
        with patch.object(actions.slices,'ue_conf',return_value={}),patch.object(actions.slices,'all_sessions',return_value=[{'node':'ue3','nssai':(3,'000003')}]),patch.object(actions.ue_nodes,'ping',return_value=True),patch.object(ran,'request',return_value=response):
            checks=actions.SetRanSlicePrb().verify(intent(),response);self.assertFalse(all(c['ok'] for c in checks))

if __name__=='__main__':unittest.main(argv=[sys.argv[0]],verbosity=2)
