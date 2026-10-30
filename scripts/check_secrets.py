#!/usr/bin/env python3
"""Reject credential literals and runtime artifacts; never print matched values."""
import re,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def scan(root=ROOT):
    tracked=subprocess.run(['git','ls-files','-z'],cwd=root,capture_output=True)
    paths=[root/p.decode() for p in tracked.stdout.split(b'\0') if p] if tracked.returncode==0 and tracked.stdout else [p for p in root.rglob('*') if p.is_file() and not any(x in p.relative_to(root).parts for x in ['.git','.local','__pycache__']) and p.name!='.env']
    findings=[]
    patterns={
        'private key':r'-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----',
        'API token':r'\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{24,})\b',
        'SIM credential literal':r'''\b(?:key|opc)\s*=\s*["'][0-9a-fA-F]{32}["']''',
        'fixed database password flag':r'-p(?:linux|test)\b',
        'WireGuard private key':r'(?m)^\s*PrivateKey\s*=\s*[A-Za-z0-9+/]{43}=',
    }
    for p in paths:
        rel=p.relative_to(root)
        if p.name=='.env' or any(x in rel.parts for x in ['state','logs','audit','.local']) or p.suffix in ['.db','.sqlite','.pem','.key','.pcap','.tgz','.exe']:
            findings.append((str(rel),'runtime/credential artifact'));continue
        try:text=p.read_text(encoding='utf8')
        except (UnicodeError,OSError):findings.append((str(rel),'unexpected binary'));continue
        for label,pattern in patterns.items():
            if re.search(pattern,text):findings.append((str(rel),label))
        if p.suffix in ['.yaml','.yml','.conf','.json','.template']:
            for line in text.splitlines():
                m=re.search(r'''(?i)(?:password|secretKey|secret_key)\s*["']?\s*[:=]\s*["']?([^\s"',}]+)''',line)
                if m and not m[1].startswith(('@','$','<')) and m[1].lower() not in ('null','none','false'):
                    findings.append((str(rel),'credential assignment'))
    return findings
if __name__=='__main__':
    rows=scan()
    for path,label in rows:print(path+': '+label)
    print(f'Secret/artifact scan: {len(rows)} finding(s)')
    raise SystemExit(bool(rows))
