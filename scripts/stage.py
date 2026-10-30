#!/usr/bin/env python3
"""Copy the rendered home tree before building, preserving any previous files."""
import argparse,json,shutil
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def stage(rendered):
    metadata=json.loads((rendered/'render.json').read_text());home=Path(metadata['home'])
    backup=home/'.local/oai-stage-backups'/datetime.now().strftime('%Y%m%d-%H%M%S')
    for source in (rendered/'home').rglob('*'):
        if not source.is_file():continue
        relative=source.relative_to(rendered/'home')
        if relative.parts[0]=='usr-local-bin':continue
        dest=home/relative
        if dest.exists():
            saved=backup/relative;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dest,saved)
        dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
    # Pin pulled images where the captured deployment supplied a public digest.
    import re
    # Locally built NWDAF/MTLF tags have no public registry artifact to pull.
    # Keep their tags so build.py and Compose use the locally rebuilt image.
    image_map={tag:row['digests'][0] for row in json.loads((ROOT/'images.lock.json').read_text()) if row['digests'] for tag in row['tags'] if not tag.startswith(('mtlf-','oai-nwdaf-'))}
    replaced=0
    for p in (home/'nwdaf-rel17').rglob('*'):
        if not p.is_file() or '.git' in p.parts or p.suffix not in ('.yaml','.yml'):continue
        before=p.read_text()
        def image(m):
            nonlocal replaced
            value=image_map.get(m[3])
            if value:replaced+=1;return m[1]+m[2]+value+m[2]+m[4]
            return m[0]
        after=re.sub(r'''(?m)^(\s*image:\s*)(["']?)([^\s"']+)\2(\s*(?:#.*)?)$''',image,before)
        if after!=before:p.write_text(after)
    return {'home':str(home),'backup':str(backup),'public_image_references_pinned':replaced}
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--rendered',type=Path,default=ROOT/'.local/rendered');a=ap.parse_args();print(json.dumps(stage(a.rendered)))
