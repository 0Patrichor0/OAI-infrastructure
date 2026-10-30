#!/usr/bin/env python3
"""Fetch the exact upstream commits; never reset an existing working checkout."""
import argparse,json,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def run(*args,cwd=None):subprocess.run(args,cwd=cwd,check=True)
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--home',type=Path,required=True);args=ap.parse_args()
    for spec in json.loads((ROOT/'upstreams.lock.json').read_text())['repositories']:
        dest=args.home.resolve()/spec['path']
        if (dest/'.git').exists():
            head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=dest,text=True).strip()
            if head!=spec['commit']:raise SystemExit(f'{dest}: commit differs; use a clean deployment directory')
        else:
            if dest.exists() and any(dest.iterdir()):raise SystemExit(f'Nonempty destination: {dest}')
            dest.mkdir(parents=True,exist_ok=True)
            run('git','init',str(dest));run('git','remote','add','origin',spec['url'],cwd=dest)
            run('git','fetch','--depth','1','origin',spec['commit'],cwd=dest)
            run('git','checkout','--detach',spec['commit'],cwd=dest)
        if spec.get('patch'):
            patch=str(ROOT/spec['patch'])
            ready=subprocess.run(['git','apply','--check',patch],cwd=dest,capture_output=True).returncode==0
            if ready:run('git','apply',patch,cwd=dest)
            elif subprocess.run(['git','apply','--reverse','--check',patch],cwd=dest,capture_output=True).returncode:
                raise SystemExit(f'Patch conflicts with existing changes: {dest}')
        print(f"READY {spec['name']} {spec['commit']}",flush=True)
if __name__=='__main__':main()
