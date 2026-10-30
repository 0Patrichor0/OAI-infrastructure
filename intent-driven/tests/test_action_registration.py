"""Real entry points in fresh interpreters, without DB access or RAN mutation."""
import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path

HOME=Path(sys.argv.pop(1))
class ActionRegistrationTests(unittest.TestCase):
    def registered(self,relative):
        with tempfile.TemporaryDirectory(prefix='intent-registry-test-') as temporary:
            code='''import importlib.util,json,sys
sys.path.insert(0,sys.argv[1])
spec=importlib.util.spec_from_file_location("registry_entry",sys.argv[2])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
from intentlib.actions import ACTIONS
print(json.dumps(sorted(ACTIONS)))
'''
            p=subprocess.run([sys.executable,'-c',code,str(HOME),str(HOME/relative)],capture_output=True,text=True,
                env=dict(os.environ,INTENT_HOME=temporary),timeout=20)
            self.assertEqual(p.returncode,0,p.stderr)
            return json.loads(p.stdout.strip().splitlines()[-1])
    def test_web_startup_registers_add_ue_without_a_prior_parse(self):
        self.assertIn('add_ue',self.registered('ui/server.py'))
    def test_web_preflight_and_cli_execute_support_same_actions(self):
        self.assertEqual(self.registered('ui/server.py'),self.registered('intentctl.py'))

if __name__=='__main__':unittest.main()
