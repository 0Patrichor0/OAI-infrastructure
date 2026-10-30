#!/usr/bin/env python3
"""Static checks plus isolated Python/JS/C regressions; no live network actions."""
import argparse,ast,json,shutil,subprocess,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def run(*args):subprocess.run([str(x) for x in args],cwd=ROOT,check=True)
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--static-only',action='store_true');ap.add_argument('--skip-js',action='store_true');a=ap.parse_args()
    files=[p for p in ROOT.rglob('*.py') if not any(x in p.parts for x in ['.git','.local','__pycache__'])]
    for p in files:ast.parse(p.read_text(),filename=str(p))
    for p in ROOT.rglob('*.json'):
        if not any(x in p.parts for x in ['.git','.local']):json.loads(p.read_text())
    run(sys.executable,'scripts/check_secrets.py')
    print('Python syntax and JSON checked:',len(files),'Python files',flush=True)
    if a.static_only:return
    for p in ROOT.rglob('*.sh'):
        if not any(x in p.parts for x in ['.git','.local']):run('bash','-n',p)
    if not a.skip_js:
        for p in (ROOT/'intent-driven/ui').glob('*.js'):run('node','--check',p)
    run(sys.executable,'tests/test_render.py')
    for script in ['test_entry_consistency.py','test_action_registration.py']:
        run(sys.executable,ROOT/'intent-driven/tests'/script,ROOT/'intent-driven')
    run(sys.executable,'tests/test_observability.py',ROOT/'intent-driven')
    for script in ['test_mcs_backend.py','test_prb_backend.py']:run(sys.executable,'tests/'+script,'--root',ROOT/'intent-driven')
    if not a.skip_js:
        for script in ['test_ui.cjs','test_frontend_regressions.cjs']:run('node','tests/'+script,ROOT/'intent-driven/ui/app.js')
    else:print('JavaScript checks skipped; run the two Node test scripts separately.')
    with tempfile.TemporaryDirectory(prefix='oai-policy-check-') as temp:
        for name,source in [('mcs','intent-driven/e2-tests/test_mcs_policy.c'),('prb','intent-driven/e2-tests/prb/test_prb_policy.c')]:
            compiler=shutil.which('gcc-13') or shutil.which('gcc')
            if not compiler:raise SystemExit('Install GCC to run the C policy regressions')
            binary=Path(temp)/name;run(compiler,'-std=gnu11','-Iintent-driven/ran',source,'-o',binary);run(binary)
    print('All isolated checks completed; live service/UE acceptance is separate.')
if __name__=='__main__':main()
