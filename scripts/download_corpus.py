"""Resume the verified v5.2 clean corpus download, then stream-decompress it."""
import gzip
import hashlib
import json
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / 'data/raw'


def main():
    release = json.loads((RAW / 'release-v5.2.json').read_text())
    names = ['ftc_clean_part_aa', 'ftc_clean_part_ab', 'ftc_clean_part_ac']
    parts = []
    for name in names:
        asset = next(a for a in release['assets'] if a['name'] == name)
        path = RAW / name
        if not path.exists() or path.stat().st_size != asset['size']:
            print(f'Downloading {name} ({asset["size"]:,} bytes)', flush=True)
            subprocess.run(['curl', '--fail', '--location', '--retry', '5', '--continue-at', '-', '--output', str(path), asset['browser_download_url']], check=True)
        if path.stat().st_size != asset['size']:
            raise RuntimeError(f'Unexpected size: {name}')
        digest = asset.get('digest')
        if digest:
            algorithm, expected = digest.split(':', 1)
            h = hashlib.new(algorithm)
            with path.open('rb') as f:
                for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
                    h.update(block)
            if h.hexdigest() != expected:
                raise RuntimeError(f'Checksum mismatch: {name}')
        parts.append(path)
    database = RAW / 'full_text_corpus.db'
    if database.exists():
        print(f'Already exists: {database}', flush=True)
        return
    # gzip data is split mid-stream, so concatenate before decompressing.
    archive = RAW / 'full_text_corpus.db.gz'
    print('Assembling compressed corpus', flush=True)
    with archive.open('wb') as out:
        for path in parts:
            with path.open('rb') as f:
                shutil.copyfileobj(f, out, 8 * 1024 * 1024)
    temporary = database.with_suffix('.db.partial')
    print('Decompressing corpus', flush=True)
    with gzip.open(archive, 'rb') as source, temporary.open('wb') as target:
        shutil.copyfileobj(source, target, 8 * 1024 * 1024)
    with closing(sqlite3.connect(f'file:{temporary}?mode=ro', uri=True)) as con:
        result = con.execute('PRAGMA quick_check').fetchall()
        if result != [('ok',)]:
            raise RuntimeError(f'SQLite validation failed: {result}')
        schema = con.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
    temporary.rename(database)
    (ROOT / 'output/corpus_schema.json').write_text(json.dumps(schema, indent=2))
    archive.unlink()  # Keep original parts for reproducibility/resume.
    print(f'Corpus ready: {database}', flush=True)


if __name__ == '__main__':
    main()
