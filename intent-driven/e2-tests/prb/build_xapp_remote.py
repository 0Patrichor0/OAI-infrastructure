from pathlib import Path
import os,subprocess
F=Path('/home/ubuntu/oai-ran-e2/openair2/E2AP/flexric');S=Path('/home/ubuntu/intent-e2-prb-stage')
cmd=['cc','-g','-DASN','-DE2AP_V3','-DKPM_V3_00','-DSQLITE3_XAPP','-I'+str(F/'src'),'-I'+str(F/'src/xApp'),'-I'+str(S/'ran'),'-iquote',str(F/'examples/xApp/c/helloworld'),str(S/'ran/xapp_prb_control.c'),str(F/'src/util/alg_ds/alg/defer.c'),str(F/'build/src/xApp/libe42_xapp.a'),'-pthread','-lsctp','-ldl',str(F/'build/examples/xApp/c/monitor/RRC_MESSAGES/libasn1_nr_rrc.a'),'-o',str(S/'xapp_prb_control')]
subprocess.run(cmd,check=True)
root=Path('/home/ubuntu/intent-driven/ran');temp=root/'xapp_prb_control.new';temp.write_bytes((S/'xapp_prb_control').read_bytes());temp.chmod(0o755);os.replace(temp,root/'xapp_prb_control')
for name in ['xapp_prb_control.c','intent_prb_policy.h']:(root/name).write_bytes((S/'ran'/name).read_bytes())
print('PRB xApp built and atomically installed; own temporary DB removed on normal exit')
