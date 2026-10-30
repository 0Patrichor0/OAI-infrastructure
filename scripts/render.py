#!/usr/bin/env python3
"""Render source/configuration and fresh private lab credentials into .local/."""
import argparse,copy,json,os,re,secrets,shlex
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def load_env(path):
    result={}
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith('#'):continue
            key,sep,value=line.partition('=')
            if not sep or not re.fullmatch('[A-Z][A-Z0-9_]*',key):raise ValueError('Invalid environment key')
            result[key]=value.strip().strip('\"\'')
    return result
def sql_value(value):
    if value is None:return 'NULL'
    if isinstance(value,bool):return '1' if value else '0'
    if isinstance(value,(int,float)):return str(value)
    if isinstance(value,(dict,list)):value=json.dumps(value,separators=(',',':'))
    return "'"+str(value).replace('\\','\\\\').replace("'","\\'")+"'"
def render(home,output,env_file,user):
    home=home.resolve();output=output.resolve()
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',str(home)):raise ValueError('Use a Linux home path without spaces or shell metacharacters')
    if not re.fullmatch('[a-z_][a-z0-9_-]*',user):raise ValueError('Invalid service user')
    if output.exists() and any(output.iterdir()):raise ValueError('Render output is nonempty; keep the previous render as a backup')
    env=load_env(env_file)
    for key in ['MYSQL_ROOT_PASSWORD','MYSQL_APP_PASSWORD','S3_SECRET_KEY','POSTGRES_PASSWORD','MONGO_INITDB_ROOT_PASSWORD']:
        env[key]=env.get(key) or secrets.token_hex(24)
    env['S3_ACCESS_KEY']=env.get('S3_ACCESS_KEY') or 'lab'+secrets.token_hex(8)
    for key in ['SIM_KEY','SIM_OPC']:
        env[key]=env.get(key) or secrets.token_hex(16)
        if not re.fullmatch('[0-9a-fA-F]{32}',env[key]):raise ValueError(key+' must be 32 hexadecimal characters')
    env.setdefault('INTENT_LLM_BASE','https://api.deepseek.com');env.setdefault('INTENT_LLM_MODEL','deepseek-chat');env.setdefault('INTENT_LLM_KEY','')
    env['INTENT_BIND']=env.get('INTENT_BIND') or '127.0.0.1'
    for key,value in env.items():
        if any(c in value for c in '\n\r\x00'):raise ValueError('Multiline environment value refused: '+key)
    env_file.parent.mkdir(parents=True,exist_ok=True)
    env_file.write_text(''.join(k+'='+shlex.quote(v)+'\n' for k,v in sorted(env.items())));env_file.chmod(0o600)
    output.mkdir(parents=True,mode=0o700)
    def text(value):
        value=value.replace('/home/ubuntu',str(home)).replace('User=ubuntu','User='+user).replace('Group=ubuntu','Group='+user)
        def token(m):
            if m[1] not in env:raise ValueError('Undefined template variable: '+m[1])
            return env[m[1]]
        return re.sub(r'@([A-Z][A-Z0-9_]*)@',token,value)
    def write(relative,value,mode=0o644):
        p=output/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value,encoding='utf8');p.chmod(mode)
    for folder in ['intent-driven','overlays','systemd']:
        for p in (ROOT/folder).rglob('*'):
            if not p.is_file() or '__pycache__' in p.parts:continue
            rel=p.relative_to(ROOT/folder)
            if folder=='overlays':dest=Path('home')/rel
            elif folder=='systemd':dest=Path('systemd')/rel
            else:dest=Path('home/intent-driven')/rel
            if p.name=='.env.template':dest=dest.with_name('.env')
            value=text(p.read_text(encoding='utf8'))
            mode=0o755 if p.suffix=='.sh' or p.name=='intentctl.py' else 0o644
            if p.name.startswith('ue') and p.suffix=='.conf' or p.name in ('.env.template','seaweedfs-s3.json'):mode=0o600
            if folder=='systemd' and p.suffix=='.service':
                additions=''
                if p.name.startswith('oran-'):additions+='Environment=LD_LIBRARY_PATH='+str(home/'oai-ran-e2/cmake_targets/ran_build/build')+'\n'
                if p.name=='intent-ui.service':additions+='EnvironmentFile=-'+str(home/'.config/oai-infrastructure/lab.env')+'\n'
                value=value.replace('[Service]\n','[Service]\n'+additions)
            write(dest,value,mode)
    # Compose loads credentials from a project-local .env; services share the same values.
    envtext=''.join(k+'='+shlex.quote(v)+'\n' for k,v in sorted(env.items()))
    write('home/nwdaf-rel17/oai-cn5g-fed/docker-compose/.env',envtext,0o600)
    write('home/.config/oai-infrastructure/lab.env',envtext,0o600)
    template=json.loads((ROOT/'subscriber-template.json').read_text())
    profiles=json.loads((ROOT/'ue-profiles.json').read_text()) if (ROOT/'ue-profiles.json').exists() else {}
    seed=['USE oai_db;','DELETE FROM AuthenticationSubscription;','DELETE FROM SessionManagementSubscriptionData;']
    for config in sorted((output/'home/oai-ran-conf').glob('ue*.conf')):
        value=config.read_text();imsi=re.search(r'\bimsi\s*=\s*"(\d{15})"',value)[1]
        sst=int(re.search(r'nssai_sst\s*=\s*(\d+)',value)[1]);sd=format(int(re.search(r'nssai_sd\s*=\s*(0x[0-9a-fA-F]+|\d+)',value)[1],0),'06x')
        for table,spec in template.items():
            row=copy.deepcopy(spec['row']);row['ueid']=imsi
            for col,v in row.items():
                if isinstance(v,str) and '@' in v:row[col]=text(v.replace('@IMSI@',imsi))
            if table=='SessionManagementSubscriptionData':
                row['singleNssai']={'sst':sst,'sd':sd}
                dnn=row['dnnConfigurations']
                if isinstance(dnn,str):dnn=json.loads(dnn)
                default=dnn['default'];profile=profiles.get(imsi,{})
                if profile.get('fiveQI') is not None:default['5gQosProfile']['5qi']=profile['fiveQI']
                for source,dest in [('ambr_ul','uplink'),('ambr_dl','downlink')]:
                    if profile.get(source):default['sessionAmbr'][dest]=profile[source]
                default.pop('staticIpAddress',None);row['dnnConfigurations']=dnn
            seed.append('INSERT INTO `'+table+'` ('+','.join('`'+c+'`' for c in spec['columns'])+') VALUES ('+','.join(sql_value(row.get(c)) for c in spec['columns'])+');')
    # Startup calls persist.render() even before MySQL exists. Keep the fresh
    # lab seed separate from its writable zz_intent_state.sql, or that call
    # would erase all generated subscriber rows on the very first startup.
    database='home/nwdaf-rel17/oai-cn5g-fed/docker-compose/database/'
    write(database+'z_lab_subscribers.sql','\n'.join(seed)+'\n',0o600)
    write(database+'zz_intent_state.sql','-- Intent reconciliation; populated by persist.render().\n',0o600)
    write('render.json',json.dumps({'home':str(home),'user':user,'env_file':str(env_file.resolve())},indent=2),0o600)
    return {'rendered':str(output),'credentials':str(env_file),'profile':'server-rfsim'}
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--home',type=Path,required=True);ap.add_argument('--output',type=Path,default=ROOT/'.local/rendered')
    ap.add_argument('--env-file',type=Path,default=ROOT/'.env');ap.add_argument('--user');a=ap.parse_args()
    print(json.dumps(render(a.home,a.output,a.env_file,a.user or a.home.name)))
if __name__=='__main__':main()
