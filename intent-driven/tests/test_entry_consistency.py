"""Exercise preview validation and the actual QoS setter without network writes."""
import copy,json,re,sqlite3,sys,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv.pop(1))
from intentlib import actions,actions_more,actions_slice,actions_ue,preflight,slices,ue_nodes
MAIN='208950000000097';EXTRA='208950000000031';OFFLINE='208950000090088'
SM={'default':{'5gQosProfile':{'5qi':9,'arp':{'priorityLevel':8}},'sessionAmbr':{'uplink':'50Mbps','downlink':'50Mbps'}}}
class EntryTests(unittest.TestCase):
    def setUp(self):
        def start(p):self.addCleanup(p.stop);return p.start()
        start(patch.object(actions.db,'exists',return_value=True))
        start(patch.object(actions.db,'resolve_ueid',side_effect=lambda imsi,*a:imsi))
        start(patch.object(actions.db,'active_ue_imsi',return_value=MAIN))
        start(patch.object(actions.db,'json_one',side_effect=lambda q:{'sst':3,'sd':'000003'} if 'singleNssai' in q else copy.deepcopy(SM)))
        start(patch.object(slices,'all_sessions',return_value=[{'node':'ue1','imsi':MAIN,'ip':'12.1.1.4','nssai':[3,'000003']},{'node':'ue2','imsi':EXTRA,'ip':'12.1.1.2','nssai':[3,'000003']}]))
        start(patch.object(slices,'ue_conf',return_value={'imsi':MAIN,'sst':3,'sd':'000003'}))
        start(patch.object(slices,'upf_snssais',return_value=[{'sst':3,'sd':'000003'},{'sst':1,'sd':'00007c'}]))
        start(patch.object(slices,'ran_snssais',return_value=[{'sst':3,'sd':'000003','conf_files':list(slices.RAN_CONFS)},{'sst':1,'sd':'00007c','conf_files':list(slices.RAN_CONFS)}]))
        start(patch.object(slices,'inventory',return_value={'slices':[{'sst':3,'sd':'000003','network_ready':True},{'sst':1,'sd':'00007c','network_ready':True}]}))
        start(patch.object(actions_slice,'core_slice_known',return_value=True))
        start(patch.object(ue_nodes,'next_index',return_value=250))
    def test_switch_cannot_clone_an_online_extra_identity(self):
        with self.assertRaisesRegex(actions.IntentError,'在线|活动'):
            actions_slice.SwitchUe().validate({'target':{'imsi':EXTRA}})
    def test_slice_acceptance_cannot_clone_an_online_extra_identity(self):
        with self.assertRaisesRegex(actions.IntentError,'在线|活动'):
            actions_slice.ActivateSlice().validate({'target':{'imsi':EXTRA},'params':{'sst':3,'sd':'000003'},'constraints':{'allow_upf_recreate':True}})
    def test_switch_still_accepts_offline_identity(self):
        actions_slice.SwitchUe().validate({'target':{'imsi':OFFLINE}})
    def test_slice_can_keep_the_main_identity(self):
        actions_slice.ActivateSlice().validate({'target':{'imsi':MAIN},'params':{'sst':3,'sd':'000003'},'constraints':{'allow_upf_recreate':True}})
    def test_ue_uppercase_sd_reaches_same_ready_slice(self):
        p=preflight.build({'action':'add_ue','target':{'imsi':OFFLINE},'params':{'sst':1,'sd':'00007C'}})
        self.assertTrue(p['valid'],p['checks']);self.assertEqual(p['intent']['params']['sd'],'00007c')
    def test_provision_can_keep_existing_sd(self):
        p=preflight.build({'action':'provision_service','target':{'imsi':MAIN},'params':{'sst':3,'fiveQI':6}})
        self.assertTrue(p['valid'],p['checks']);self.assertEqual(p['intent']['params']['sd'],'000003')
    def test_provision_uppercase_sd_reaches_ready_slice(self):
        p=preflight.build({'action':'provision_service','target':{'imsi':MAIN},'params':{'sst':1,'sd':'00007C'}})
        self.assertTrue(p['valid'],p['checks']);self.assertEqual(p['intent']['params']['sd'],'00007c')
    def test_normalized_provision_has_a_stable_preview_fingerprint(self):
        first=preflight.build({'action':'provision_service','target':{'imsi':MAIN},'params':{'sst':'3','fiveQI':6}})
        self.assertTrue(first['valid'],first['checks'])
        second=preflight.build(first['intent'])
        self.assertTrue(second['valid'],second['checks']);self.assertEqual(first['fingerprint'],second['fingerprint'])
    def test_arp_write_is_observed_by_real_qos_verifier(self):
        state=copy.deepcopy(SM);commands=[]
        def sql(statement):
            nonlocal state
            commands.append(statement)
            match=re.search(r'SET dnnConfigurations=JSON_SET\(dnnConfigurations, (.+)\) WHERE',statement)
            self.assertIsNotNone(match,statement)
            # Evaluate the real setter's JSON path/arguments with SQLite JSON_SET.
            # No UPDATE is sent to the server; both engines use this JSON path syntax.
            with closing(sqlite3.connect(':memory:')) as con:
                state=json.loads(con.execute('SELECT json_set(?, '+match.group(1)+')',(json.dumps(state),)).fetchone()[0])
        it={'target':{'imsi':OFFLINE},'params':{'arp_priority':2},'constraints':{'allow_session_rebuild':False}}
        a=actions.SetQoS()
        with patch.object(actions.db,'sql',side_effect=sql),patch.object(actions.db,'json_one',side_effect=lambda q:state):
            res=a.execute(it);checks=a.verify(it,res)
        self.assertTrue(all(c['ok'] for c in checks),checks)
        self.assertEqual(state['default']['5gQosProfile']['arp']['priorityLevel'],2)

if __name__=='__main__':unittest.main()
