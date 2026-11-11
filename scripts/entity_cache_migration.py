"""Move existing resumable NER data to private staging without re-extraction.

Only a fully valid old entity build stamp may be rebased: all input/output
hashes and the toolchain must match. Otherwise the normal rebuild is used.
"""
import json
from pathlib import Path
import sqlite3

FILES=('entity_graph.db','entity_nodes.json','entity_edges.json','entity_summary.json')


def migrate(root, state, fp, stale_reason, step_type, move_entities=True):
    root=Path(root)
    raw='output/.build/entity_mentions.db'
    destination='data/private/entity_mentions.db'
    staging='data/private/entity_layer'
    private=root/'data/private'
    private.mkdir(parents=True,exist_ok=True);private.chmod(0o700)
    target=root/staging;target.mkdir(parents=True,exist_ok=True);target.chmod(0o700)
    stamp=state.get('steps',{}).get('build_entities')
    valid=bool(move_entities and stamp and stamp.get('command')==['scripts/build_entities.py']
               and stale_reason(step_type('build_entities',['scripts/build_entities.py'],None),stamp,fp) is None)
    # A path change alone does not alter the extractor's signature (it hashes
    # the corpus/config/code, not the output path). done rows remain intact.
    if (root/raw).exists() and not (root/destination).exists():
        db=sqlite3.connect(root/raw);db.execute('PRAGMA wal_checkpoint(TRUNCATE)');db.close()
        (root/raw).replace(root/destination);(root/destination).chmod(0o600)
        for suffix in ('-wal','-shm'):
            old=Path(str(root/raw)+suffix)
            if old.exists(): old.replace(Path(str(root/destination)+suffix))
        metric=(root/raw).with_suffix('.metrics.json')
        if metric.exists(): metric.replace((root/destination).with_suffix('.metrics.json'))
    if valid:
        outputs={}
        for filename in FILES:
            old=root/'output'/filename;new=target/filename
            if not new.exists(): old.replace(new)
            new.chmod(0o600)
            expected=stamp['outputs'].get('output/'+filename)
            if expected is None or fp.file(staging+'/'+filename)!=expected:
                return False
            outputs[staging+'/'+filename]=expected
        stamp['outputs']=outputs
        stamp['command']=['scripts/build_entities.py','--mentions',destination,'--output',staging]
        stamp['inputs'][destination]=fp.file(destination)
        stamp['inputs'].pop(raw,None)
        stamp['probed'][staging]='dir'
        return True
    return False
