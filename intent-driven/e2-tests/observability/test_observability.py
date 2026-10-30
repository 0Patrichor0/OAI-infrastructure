import concurrent.futures
import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0,sys.argv.pop(1))
from intentlib import actions, actions_ran, actions_prb, actions_more, preflight, effects, live_reads, core_recovery
from intentlib.actions import IntentError


class ReadTests(unittest.TestCase):
    def test_read_age_uses_du_receipt_time(self):
        broker=live_reads.ReadBroker();sample=time.time()-10
        data=broker.read('mcs',lambda:{'state':{'timestamp_us':int(sample*1e6)}})
        self.assertAlmostEqual(data['read']['sampled_at'],sample,places=4)
        self.assertGreaterEqual(data['read']['age_s'],9.9)
        self.assertLess(data['read']['cache_age_s'],1)
    def test_expired_channel_budget_sends_no_request(self):
        from intentlib import ran_control,prb_control
        for module,args in ((ran_control,('query',0)),(prb_control,('query',3,'000003',0))):
            with patch.object(module,'du_pid') as pid,patch.object(module.subprocess,'run') as command:
                with self.assertRaisesRegex(IntentError,'未发送'):
                    module._request(*args,deadline=time.monotonic()-1)
                pid.assert_not_called();command.assert_not_called()
    def test_ten_tabs_share_one_query(self):
        broker=live_reads.ReadBroker()
        release=threading.Event();started=threading.Event();calls=[]
        def loader():
            calls.append(1);started.set();release.wait(2);return {'state':{'pid':5}}
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            tasks=[pool.submit(broker.read,'mcs',loader) for _ in range(10)]
            started.wait(1)
            # Wait for the callers to reach the condition, not for a network duration.
            deadline=time.monotonic()+2
            while broker.stats['shared']<9 and time.monotonic()<deadline:time.sleep(.005)
            release.set();rows=[t.result() for t in tasks]
        self.assertEqual(len(calls),1)
        self.assertEqual(sum(r['read']['shared'] for r in rows),9)
        rows[0]['state']['pid']=8
        self.assertEqual(broker.read('mcs',loader)['state']['pid'],5)

    def test_failure_not_cached(self):
        b=live_reads.ReadBroker()
        with self.assertRaisesRegex(ValueError,'lost ACK'):
            b.read('mcs',Mock(side_effect=ValueError('lost ACK')))
        self.assertEqual(b.read('mcs',lambda:{'ok':True})['ok'],True)
        self.assertEqual(b.diagnostics()['failures'],1)

    def test_control_invalidates_cache_and_inflight(self):
        b=live_reads.ReadBroker()
        def load():b.invalidate();return {'ok':True}
        with self.assertRaisesRegex(IntentError,'控制操作'):b.read('mcs',load)
        self.assertTrue(b.read('mcs',lambda:{'ok':True})['ok'])

    def test_distinct_queue_is_bounded(self):
        b=live_reads.ReadBroker();b.slots.acquire();b.slots.acquire()
        with self.assertRaisesRegex(IntentError,'长队列'):b.read('third',lambda:{})
        b.slots.release();b.slots.release()


MCS={'action':'set_ran_dl_mcs','params':{'max_mcs':20},'target':{},'constraints':{}}
MCS_STATE={'pid':7,'revision':2,'dl_min_mcs':5,'dl_max_mcs':28,'timestamp_us':1,'dl_bytes':1}
PRB={'action':'set_ran_slice_prb','params':{'sst':3,'sd':'000003','max_prb_percent':30},'target':{},'constraints':{}}
PRB_STATE={'pid':7,'revision':2,'sst':3,'sd':'000003','enabled':False,'max_prb_percent':100,
    'bwp_prbs':106,'num_beams':1,'matched_users':1,'timestamp_us':1000000,'new_prbs':10,
    'new_grants':2,'limited_requests':0,'violations':0}


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.session_patch=patch.object(preflight,'sessions',return_value=[{'node':'ue1','imsi':'208950000000097','ip':'12.1.1.2','nssai':[3,'000003']}]);self.session_patch.start();self.addCleanup(self.session_patch.stop)

    def test_mcs_current_target_and_under_floor(self):
        with patch('intentlib.ran_control.request',return_value={'state':MCS_STATE}):
            p=preflight.build(MCS)
            self.assertTrue(p['valid']);self.assertEqual(p['changes'][0]['current'],28)
            self.assertEqual(p['changes'][0]['target'],20);self.assertEqual(len(p['affected_ues']),1)
            low=copy.deepcopy(MCS);low['params']['max_mcs']=2
            self.assertFalse(preflight.build(low)['valid'])

    def test_fingerprint_checks_policy_not_counters(self):
        with patch('intentlib.ran_control.request',return_value={'state':MCS_STATE}):p=preflight.build(MCS)
        changed={**MCS_STATE,'dl_bytes':900,'timestamp_us':900}
        with patch('intentlib.ran_control.request',return_value={'state':changed}):
            preflight.enforce(MCS,{'expires_at':time.time()+60,'fingerprint':p['fingerprint']})
        changed['revision']=3
        with patch('intentlib.ran_control.request',return_value={'state':changed}):
            with self.assertRaisesRegex(IntentError,'状态发生变化'):
                preflight.enforce(MCS,{'expires_at':time.time()+60,'fingerprint':p['fingerprint']})

    def test_expired_preview_rejected(self):
        with patch('intentlib.ran_control.request',return_value={'state':MCS_STATE}):
            p=preflight.build(MCS)
            with self.assertRaisesRegex(IntentError,'过期'):
                preflight.enforce(MCS,{'expires_at':time.time()-1,'fingerprint':p['fingerprint']})

    def test_prb_requires_actual_drb_and_ready_network(self):
        inventory={'slices':[{'sst':3,'sd':'000003','network_ready':True}]}
        with patch('intentlib.prb_control.request',return_value={'state':PRB_STATE}),patch('intentlib.slices.inventory',return_value=inventory):
            self.assertTrue(preflight.build(PRB)['valid'])
        with patch('intentlib.prb_control.request',return_value={'state':{**PRB_STATE,'matched_users':0}}),patch('intentlib.slices.inventory',return_value=inventory):
            self.assertFalse(preflight.build(PRB)['valid'])

    def test_cli_reuses_locked_fresh_state_and_keeps_independent_verification(self):
        import argparse
        import intentctl
        act=actions_ran.SetRanDlMcs()
        intent=copy.deepcopy(MCS)
        preview={'intent':intent,'plan':[],'ran_state':MCS_STATE,'valid':True}
        response={'state':{**MCS_STATE,'dl_max_mcs':20},'ack':True,
                  'telemetry':{'indications':1,'observations':1,'observed_max_mcs':20}}
        ledger=Mock();ledger.create.return_value='fixture'
        before={'ran':MCS_STATE,'ues':[]};after={'ran':response['state'],'ues':[]}
        with patch.object(intentctl,'_load_intent',return_value=(intent,'file')), \
             patch.object(intentctl.preflight,'enforce',return_value=preview), \
             patch.object(intentctl,'Ledger',return_value=ledger), \
             patch.object(intentctl.actions,'get',return_value=act), \
             patch.object(act,'execute',return_value=response), \
             patch.object(actions_ran.ran_control,'current_owner',return_value=None), \
             patch.object(actions_ran.ran_control,'request',return_value=copy.deepcopy(response)) as query, \
             patch.object(actions_ran.ue,'ping_ok',return_value=True), \
             patch.object(intentctl.effects,'safe_capture',side_effect=[before,after]), \
             patch.object(intentctl.persist_cfg,'state',return_value={}), \
             patch.object(intentctl,'_hold_reconcile'):
            rc=intentctl.cmd_apply(argparse.Namespace(preflight=None,dry_run=False,yes=True))
        self.assertEqual(rc,0)
        self.assertEqual(query.call_count,1,'independent post-write verification must remain')
        snapshots=[call.kwargs['snapshot_json'] for call in ledger.update.call_args_list if 'snapshot_json' in call.kwargs]
        self.assertEqual(snapshots[0]['before'],MCS_STATE)
        final=[call.kwargs['exec_json'] for call in ledger.update.call_args_list if 'exec_json' in call.kwargs][-1]
        self.assertEqual(final['_observability']['after'],after)


class EffectTests(unittest.TestCase):
    def test_counter_reset_never_becomes_negative_usage(self):
        self.assertEqual(effects.prb_delta(PRB_STATE,{**PRB_STATE,'revision':3})['status'],'unavailable_or_policy_reset')
        after={**PRB_STATE,'new_prbs':50,'timestamp_us':3000000}
        self.assertEqual(effects.prb_delta(PRB_STATE,after)['new_prbs_per_s'],20)

    def test_missing_measurement_is_null_not_zero(self):
        with patch.object(effects,'counters',return_value=None),patch.object(effects,'rtt',return_value={'rtt_ms':None}),patch.object(effects.time,'sleep'):
            r=effects.measure_ue({'node':'ue1'},0)
        self.assertIsNone(r['dl_mbps']);self.assertIsNone(r['rtt_ms'])

    def test_real_loss_parsed(self):
        out='3 packets transmitted, 2 received, 33.3333% packet loss\nrtt min/avg/max/mdev = 1.0/2.0/3.0/0.1 ms'
        with patch.object(effects,'command',return_value=Mock(stdout=out)):
            r=effects.rtt({'node':'ue1'})
        self.assertEqual(r['rtt_ms'],2);self.assertAlmostEqual(r['loss_percent'],33.3333)


class EgressTests(unittest.TestCase):
    def test_unknown_dn_cannot_change_compose(self):
        with self.assertRaisesRegex(IntentError,'仅支持'):
            actions_more.SetEgress().validate({'params':{'target_dn_ip':'999.1.1.1'}})

    def test_failed_switch_restores_runtime_not_just_file(self):
        a=actions_more.SetEgress();compose=Mock();env=Mock()
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,INTENT_HOME=d), \
             patch.object(a,'_current_remote',return_value=actions_more.DN1_IP), \
             patch.object(a,'_dn2_ip',return_value=None),patch.object(a,'_ensure_dn2',return_value='created'), \
             patch.object(actions_more,'_compose',compose),patch.object(actions_more,'_compose_env_set',env), \
             patch.object(actions_more,'_wait_upf',side_effect=[False,True]), \
             patch.object(actions_more,'_restart_smf',return_value=True), \
             patch.object(actions_more.subprocess,'run',return_value=Mock(returncode=0)), \
             patch.object(actions_more.ue,'restart_and_wait',return_value={'ok':True}), \
             patch.object(actions_more.ue,'ping_ok',return_value=True), \
             patch.object(core_recovery,'diagnostics',return_value={'health':'captured'}):
            with self.assertRaisesRegex(IntentError,'已恢复原出口'):
                a.execute({'params':{'target_dn_ip':actions_more.DN2_IP},'constraints':{}})
            self.assertEqual(compose.call_count,2)
            self.assertEqual(env.call_args.args,('IF_3_IP_REMOTE',actions_more.DN1_IP))
            self.assertEqual(len(list(__import__('pathlib').Path(d).glob('logs/egress-failure-*.json'))),1)

    def test_smf_health_requires_pfcp_and_restarts_sbi_last(self):
        calls=[]
        def run(args,timeout=15):
            calls.append(args)
            return 0,'Received N4 ASSOCIATION SETUP RESPONSE'
        clock=iter(range(200))
        with patch.object(core_recovery,'state',return_value={'Running':True,'health':'healthy','StartedAt':'fixed'}), \
             patch.object(core_recovery,'run',side_effect=run),patch.object(core_recovery,'association',return_value='Node: oai-smf'), \
             patch.object(core_recovery.time,'sleep'),patch.object(core_recovery.time,'monotonic',side_effect=lambda:next(clock)):
            self.assertTrue(core_recovery.recover_smf(50))
        self.assertIn('stop',calls[0]);self.assertEqual(calls[-1][-2:],['start','oai-nwdaf-sbi'])


if __name__=='__main__':unittest.main(verbosity=2)
