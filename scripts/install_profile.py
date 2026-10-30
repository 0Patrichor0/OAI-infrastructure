#!/usr/bin/env python3
"""Install a previously rendered profile; does not start or stop the lab."""
import argparse,json,os,pwd,shutil,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--rendered',type=Path,default=ROOT/'.local/rendered');a=ap.parse_args()
    if os.geteuid()!=0:raise SystemExit('Run with sudo')
    meta=json.loads((a.rendered/'render.json').read_text());home=Path(meta['home']);owner=pwd.getpwnam(meta['user'])
    if not (home/'oai-ran-e2/cmake_targets/ran_build/build/nr-softmodem').exists():raise SystemExit('Build the RAN first')
    # Keep the prior files as a private backup rather than deleting or resetting them.
    from datetime import datetime
    backup=home/'.local/oai-infrastructure-backups'/datetime.now().strftime('%Y%m%d-%H%M%S');backup.mkdir(parents=True,mode=0o700)
    def install(source,dest,relative,owned):
        if dest.exists():
            snap=backup/relative;snap.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dest,snap)
        dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
        if owned:os.chown(dest,owner.pw_uid,owner.pw_gid)
    for p in (a.rendered/'home').rglob('*'):
        if not p.is_file():continue
        relative=p.relative_to(a.rendered/'home')
        if relative.parts[0]=='usr-local-bin':
            install(p,Path('/usr/local/bin')/relative.name,Path('home')/relative,False)
        elif not (home/relative).exists():
            raise SystemExit('Run stage.py before installation; missing '+str(relative))
    for p in (a.rendered/'systemd').rglob('*'):
        if p.is_file():
            relative=p.relative_to(a.rendered/'systemd');install(p,Path('/etc/systemd/system')/relative,Path('systemd')/relative,False)
    for rel in ['intent-driven','intent-driven/state','intent-driven/logs','intent-driven/audit','oai-ran-logs','nwdaf-rel17/.run']:
        p=home/rel;p.mkdir(parents=True,exist_ok=True);os.chown(p,owner.pw_uid,owner.pw_gid)
    # Stateful directories must be accessible to the unprivileged web service.
    for p in (home/'intent-driven').rglob('*'):
        if p.is_dir():os.chown(p,owner.pw_uid,owner.pw_gid)
    fbuild=home/'oai-ran-e2/openair2/E2AP/flexric/build'
    subprocess.run(['cmake','--install',str(fbuild)],check=True);subprocess.run(['ldconfig'],check=True)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    enabled=json.loads((ROOT/'services.lock.json').read_text()) if (ROOT/'services.lock.json').exists() else {}
    for name,state in enabled.items():
        if state.get('enabled') and (Path('/etc/systemd/system')/name).exists():subprocess.run(['systemctl','enable',name],check=True)
    print(json.dumps({'installed_home':str(home),'backup':str(backup),'services_started':False}))
if __name__=='__main__':main()
