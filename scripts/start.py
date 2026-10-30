#!/usr/bin/env python3
"""Run the captured startup flow and activate the enabled automation profile."""
import argparse,json,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--home',type=Path,required=True);ap.add_argument('--check',action='store_true');a=ap.parse_args()
    command=['bash',str(a.home/'nwdaf-rel17/start-all.sh')]+(['--check'] if a.check else [])
    subprocess.run(command,check=True)
    if not a.check:
        subprocess.run(['sudo','systemctl','start','intent-ui'],check=True)
        states=json.loads((ROOT/'services.lock.json').read_text())
        timers=[name for name,state in states.items() if state['enabled'] and name.endswith('.timer')]
        if timers:subprocess.run(['sudo','systemctl','start',*timers],check=True)
        if states.get('nwdaf-loop-netperf.service',{}).get('enabled'):
            subprocess.run(['sudo','systemctl','start','nwdaf-loop-netperf.service'],check=True)
