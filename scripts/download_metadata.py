"""Download pinned v5.2 release assets with published digest checks.

    .venv/bin/python scripts/download_metadata.py [asset names...]

Default: the two communication metadata databases. Extra small assets used by
the pipeline: VERIFICATION_URLS.csv.gz document_entities.db.gz
external_mentions.db.gz deposition_transcripts.db.gz
"""
import gzip
import sys
import hashlib
import json
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    raw = ROOT / 'data/raw'
    release = json.loads((raw / 'release-v5.2.json').read_text())
    schemas = {}
    for name in sys.argv[1:] or ('communications.db.gz', 'email_metadata.db.gz'):
        asset = next(a for a in release['assets'] if a['name'] == name)
        archive = raw / name
        if not archive.exists() or archive.stat().st_size != asset['size']:
            subprocess.run(['curl', '--fail', '--location', '--retry', '5', '--continue-at', '-', '--output', str(archive), asset['browser_download_url']], check=True)
        if archive.stat().st_size != asset['size']:
            raise RuntimeError('Download size mismatch: ' + name)
        algorithm, expected = asset['digest'].split(':', 1)
        digest = hashlib.new(algorithm)
        with archive.open('rb') as source:
            for block in iter(lambda: source.read(8 * 1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise RuntimeError('Download checksum mismatch: ' + name)
        database = archive.with_suffix('')
        if not database.name.endswith('.db'):
            print('Verified archive:', archive.name, flush=True)
            continue  # e.g. CSV archives are read compressed
        if not database.exists():
            temporary = database.with_suffix('.db.partial')
            with gzip.open(archive, 'rb') as source, temporary.open('wb') as target:
                shutil.copyfileobj(source, target, 8 * 1024 * 1024)
            temporary.rename(database)
        with closing(sqlite3.connect(f'file:{database}?mode=ro', uri=True)) as con:
            if con.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise RuntimeError('SQLite check failed: ' + str(database))
            schemas[database.name] = con.execute("SELECT name,sql FROM sqlite_master WHERE type='table'").fetchall()
        print('Verified and ready:', database.name, flush=True)
    path = ROOT / 'output/metadata_schemas.json'
    existing = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps({**existing, **schemas}, indent=2) + '\n')


if __name__ == '__main__':
    main()
