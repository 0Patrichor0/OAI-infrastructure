import importlib.util,json,os,sqlite3,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('lab_render',ROOT/'scripts/render.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
class RenderTests(unittest.TestCase):
    def test_fresh_credentials_configs_seed_and_unit_paths_are_consistent(self):
        with tempfile.TemporaryDirectory() as temporary:
            top=Path(temporary);env=top/'lab.env';out=top/'render';home=top/'ubuntu'
            module.render(home,out,env,'ubuntu');values=module.load_env(env)
            self.assertEqual(len(values['SIM_KEY']),32);self.assertEqual(env.stat().st_mode & 0o777,0o600)
            files=list((out/'home/oai-ran-conf').glob('ue*.conf'));self.assertEqual(len(files),4)
            for f in files:
                text=f.read_text();self.assertIn(values['SIM_KEY'],text);self.assertIn(values['SIM_OPC'],text);self.assertNotIn('@SIM_',text)
            seed=(out/'home/nwdaf-rel17/oai-cn5g-fed/docker-compose/database/z_lab_subscribers.sql').read_text()
            self.assertIn(values['SIM_KEY'],seed);self.assertIn(values['SIM_OPC'],seed);self.assertEqual(seed.count('INSERT INTO'),8)
            self.assertNotIn('@',seed);self.assertNotIn('"5qi":null',seed)
            unit=(out/'systemd/intent-ui.service').read_text();self.assertIn(str(home/'intent-driven'),unit);self.assertIn('--host 127.0.0.1',unit)
            self.assertNotIn('/home/ubuntu',unit)
            s3=json.loads((out/'home/nwdaf-rel17/mtlf/_vendor/seaweedfs-s3.json').read_text())
            self.assertEqual(s3['identities'][0]['credentials'][0]['secretKey'],values['S3_SECRET_KEY'])
    def test_startup_reconciliation_does_not_erase_fresh_lab_seed(self):
        import subprocess
        with tempfile.TemporaryDirectory() as temporary:
            top=Path(temporary);out=top/'render';home=top/'ubuntu'
            module.render(home,out,top/'lab.env','ubuntu')
            # Use the rendered home directly, matching how stage.py installs it.
            import shutil
            shutil.copytree(out/'home',home)
            database=home/'nwdaf-rel17/oai-cn5g-fed/docker-compose/database'
            seed=database/'z_lab_subscribers.sql';before=seed.read_bytes()
            code='from intentlib import persist; persist.render()'
            subprocess.run([sys.executable,'-c',code],cwd=home/'intent-driven',env={**os.environ,'INTENT_HOME':str(home/'intent-driven')},check=True)
            self.assertEqual(before,seed.read_bytes())
            self.assertEqual(seed.read_text().count('INSERT INTO'),8)
            self.assertNotIn('INSERT INTO',(database/'zz_intent_state.sql').read_text())
            compose=(database.parent/'docker-compose-basic-vpp-nrf.yaml').read_text()
            self.assertIn('./database/z_lab_subscribers.sql:/docker-entrypoint-initdb.d/z_lab_subscribers.sql',compose)
    def test_rerender_cannot_silently_rotate_existing_credentials_or_replace_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            top=Path(temporary);env=top/'lab.env';first=top/'first'
            module.render(top/'ubuntu',first,env,'ubuntu');before=env.read_bytes()
            with self.assertRaisesRegex(ValueError,'nonempty'):module.render(top/'ubuntu',first,env,'ubuntu')
            module.render(top/'ubuntu',top/'second',env,'ubuntu');self.assertEqual(before,env.read_bytes())
    def test_stage_pins_pulled_images_but_preserves_locally_built_tags(self):
        stage_spec=importlib.util.spec_from_file_location('lab_stage',ROOT/'scripts/stage.py')
        stage=importlib.util.module_from_spec(stage_spec);stage_spec.loader.exec_module(stage)
        with tempfile.TemporaryDirectory() as temporary:
            top=Path(temporary);out=top/'render';home=top/'ubuntu'
            module.render(home,out,top/'lab.env','ubuntu')
            fixture=out/'home/nwdaf-rel17/mtlf/docker-compose.yaml'
            fixture.write_text('services:\n  app:\n    image: mtlf-ai-fabric:latest\n  db:\n    image: mysql:8.0\n')
            stage.stage(out)
            result=(home/'nwdaf-rel17/mtlf/docker-compose.yaml').read_text()
            self.assertIn('image: mtlf-ai-fabric:latest',result)
            self.assertIn('image: mysql@sha256:',result)
            self.assertNotIn('mtlf-ai-fabric@sha256:',result)
if __name__=='__main__':unittest.main()
