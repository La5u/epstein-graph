"""Publish the entity layer from private staging, hashing incidental contacts.

The NER grouping/evidence cache stays unmodified in data/private. Public IDs,
rows, mentions and pairs are retained; only contact-bearing text is redacted.
Original labels/fields join the existing password reveal through a 0600 lookup.
"""
import argparse
from collections import Counter
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3

from contact_publication import EMAIL, OCR_EMAIL, PHONE, NANP, phone_text, publication_key, text_scrubber
from contact_resolution import email_key, phone_key
from viewer_secret import _write_private

ROOT = Path(__file__).resolve().parents[1]
FILES = ('entity_graph.db','entity_nodes.json','entity_edges.json','entity_summary.json')


def clone(source, target):
    """Copy-on-write on Linux filesystems, bounded-memory copy elsewhere."""
    target.unlink(missing_ok=True)
    try:
        with source.open('rb') as src, target.open('wb') as dst:
            fcntl.ioctl(dst.fileno(), 0x40049409, src.fileno())  # FICLONE
    except OSError:
        shutil.copyfile(source,target)
    target.chmod(0o600)


def contact_kind(value):
    if EMAIL.fullmatch(value) or OCR_EMAIL.fullmatch(value):
        fixed='@'.join(email_key(value))
        if EMAIL.fullmatch(fixed): return 'email',fixed
    # Ambiguous national date-like spans are masked but keep their entity type.
    if PHONE.fullmatch(value) and phone_text(value) and (value.startswith('+') or re.fullmatch(NANP,value,re.I)):
        return 'phone',phone_key(value)
    return 'participant',value


def publish(source, out, private):
    source,out,private=map(Path,(source,out,private))
    out.mkdir(parents=True,exist_ok=True)
    key=publication_key(private)
    scrub=text_scrubber({},key=key)
    pending=source/'entity_graph.public.partial.db'
    clone(source/'entity_graph.db',pending)
    db=sqlite3.connect(pending)
    db.execute('PRAGMA secure_delete=ON')
    db.execute('PRAGMA cache_size=-16000')
    lookup,counts={},Counter()
    def opaque(value):
        if scrub(value)==value: return value
        return 'entitykey:'+hmac.new(key,('entity-key\0'+value).encode(),hashlib.sha256).hexdigest()[:24]
    for rowid,identity,name,kind,metadata in db.execute('SELECT rowid,id,name,entity_type,metadata FROM entities ORDER BY rowid'):
        original=json.loads(metadata)
        clean=scrub(original)
        clean_name=scrub(name)
        if clean!=original or clean_name!=name:
            contact,real=contact_kind(name)
            fields={k:v for k,v in original.items() if k not in {'id','name','entity_type','original_entity_type'} and clean.get(k)!=v}
            entry={'kind':contact,'value':real,'raw_spellings':[name],'occurrences':0}
            if fields: entry['private_fields']=fields
            lookup[str(identity)]=entry
            if contact in {'email','phone'}:
                clean.setdefault('original_entity_type',kind)
                clean['entity_type']=kind=contact
            clean['name']=clean_name
            db.execute('UPDATE entities SET name=?,entity_type=?,metadata=? WHERE rowid=?',
                       (clean_name,kind,json.dumps(clean,ensure_ascii=False),rowid))
            counts['entity_labels_or_fields']+=1
    for table in ('key_nodes','folds'):
        columns=[r[1] for r in db.execute('PRAGMA table_info('+table+')')]
        if not columns: continue
        chosen=[c for c in ('key','span') if c in columns]
        for row in db.execute('SELECT rowid,'+','.join(chosen)+' FROM '+table+' ORDER BY rowid'):
            values=[opaque(v) if c=='key' else scrub(v) for c,v in zip(chosen,row[1:])]
            if values!=list(row[1:]):
                db.execute('UPDATE '+table+' SET '+','.join(c+'=?' for c in chosen)+' WHERE rowid=?',(*values,row[0]))
                counts[table+'_redactions']+=1
    db.commit()
    nodes=source/'entity_nodes.public.partial.json'
    with nodes.open('w') as stream:
        stream.write('[\n')
        first=True
        for payload, in db.execute('SELECT metadata FROM entities ORDER BY id'):
            if not first: stream.write(',\n')
            stream.write(payload);first=False
        stream.write('\n]\n')
    db.close()
    pending.replace(out/'entity_graph.db');(out/'entity_graph.db').chmod(0o644)
    nodes.replace(out/'entity_nodes.json');(out/'entity_nodes.json').chmod(0o644)
    for filename in FILES[2:]:
        raw=(source/filename).read_text(); clean=scrub(json.loads(raw))
        target=out/filename
        target.write_text(raw if clean==json.loads(raw) else json.dumps(clean,ensure_ascii=False,indent=2)+'\n')
    _write_private(private/'entity_contact_lookup.json',json.dumps(lookup,ensure_ascii=False,indent=1)+'\n')
    (out/'entity_contact_summary.json').write_text(json.dumps(dict(counts),indent=2)+'\n')
    return dict(counts)


def main():
    parser=argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--source',type=Path,default=ROOT/'data/private/entity_layer')
    parser.add_argument('--output',type=Path,default=ROOT/'output')
    parser.add_argument('--private',type=Path,default=ROOT/'data/private')
    args=parser.parse_args()
    print(json.dumps(publish(args.source,args.output,args.private),indent=2))


if __name__=='__main__': main()
