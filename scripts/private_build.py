"""Private incremental workspace and contact-safe public release boundary.

Extraction caches, classifier reviews, old snapshots and logs can contain raw
corpus text. They must never share a directory with the published artifacts.
Run the existing pipeline unchanged in a private working copy, then export a
sanitized release. Source copies avoid hundreds of independent ROOT overrides;
data/config/assets are shared with the checkout. Cache stamps remain relative.
"""
import hashlib
import hmac
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

from contact_publication import publication_key, text_scrubber
from publish_entity_contacts import clone

SUFFIXES = {'.json', '.csv', '.md', '.txt', '.graphml', '.cypher', '.db', '.npz'}


def prepare(root):
    root = Path(root)
    private = root / 'data/private'
    private.mkdir(parents=True, exist_ok=True); private.chmod(0o700)
    work = private / 'build_workspace'
    work.mkdir(exist_ok=True); work.chmod(0o700)
    # Migrate caches intact, including SQLite files and their journals. No
    # other pipeline process may run concurrently with build.py.
    if not (work / 'output').exists():
        if (root / 'output').exists():
            (root / 'output').replace(work / 'output')
        else:
            (work / 'output').mkdir()
    shutil.copytree(root / 'scripts', work / 'scripts', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('data', 'config', 'viewer', '.venv', 'docs'):
        link = work / name
        if not link.exists(): link.symlink_to(root / name, target_is_directory=True)
    for name in ('requirements.txt', 'requirements-dev.txt'):
        if (root / name).exists(): shutil.copy2(root / name, work / name)
    return work


def sanitize_database(path, scrub, key):
    """Redact textual cells, preserving evidence rows, IDs and numeric indexes.

    secure_delete also clears replaced bytes in freed SQLite cells. Primary
    keys containing raw contacts use a full-field HMAC to avoid collisions.
    """
    from audit_public_contacts import COLUMNS
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA secure_delete=ON')
        db.execute('PRAGMA cache_size=-16000')
        tables = db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for table, ddl in tables:
            if not ddl or 'CREATE VIRTUAL TABLE' in ddl.upper(): continue
            quote = lambda name: '"' + name.replace('"', '""') + '"'
            info = db.execute('PRAGMA table_info(' + quote(table) + ')').fetchall()
            columns = [r[1] for r in info if r[1] in COLUMNS]
            if not columns: continue
            primary = {r[1] for r in info if r[5]}
            if 'WITHOUT ROWID' in ddl.upper():
                identifiers = [r[1] for r in info if r[5]]
            else:
                identifiers = ['rowid']
            select = ','.join(map(quote, identifiers + columns))
            for row in db.execute('SELECT ' + select + ' FROM ' + quote(table)):
                identity, values = row[:len(identifiers)], row[len(identifiers):]
                changes = {}
                for column, value in zip(columns, values):
                    if not isinstance(value, str): continue
                    clean = scrub(value)
                    if clean != value:
                        if column in primary:
                            clean = 'key:' + hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:24]
                        changes[column] = clean
                if changes:
                    sql = ('UPDATE ' + quote(table) + ' SET ' + ','.join(quote(c)+'=?' for c in changes)
                           + ' WHERE ' + ' AND '.join(quote(c)+'=?' for c in identifiers))
                    db.execute(sql, (*changes.values(), *identity))
    db.close()


def release(work, destination, private):
    work, destination, private = map(Path, (work, destination, private))
    pending = private / 'public_release.partial'
    if pending.exists(): shutil.rmtree(pending)
    pending.mkdir(mode=0o700)
    html = (work / 'graph.html').read_text()
    match = re.search(r'const PUBLIC_REVEAL = (\{.*?\});', html, re.S)
    public = json.loads(match[1]) if match else {}
    key = publication_key(private)
    scrub = text_scrubber(public, key=key)
    for source in sorted(work.rglob('*')):
        relative = source.relative_to(work)
        # Hidden caches, historical screenshots, logs, partial databases and
        # process markers are working material, never release artifacts.
        if not source.is_file() or any(p.startswith('.') for p in relative.parts): continue
        if '.partial' in source.name or source.suffix not in SUFFIXES and relative.as_posix() != 'graph.html': continue
        target = pending / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix == '.db':
            clone(source, target)
            sanitize_database(target, scrub, key)
        elif source.suffix == '.npz' or relative.as_posix() == 'graph.html':
            clone(source, target)
        else:
            value = source.read_text()
            target.write_text(scrub(value))
        target.chmod(0o644)
    for directory in pending.rglob('*'):
        if directory.is_dir(): directory.chmod(0o755)
    from audit_public_contacts import audit
    result = audit(pending, private)
    if result['leaks']:
        raise RuntimeError('Public contact audit failed; release kept private. See data/private/gdpr_review/leak_audit.json')
    # Do not retain a public symlink to private working material.
    previous = private / 'previous_public_release'
    if previous.exists(): shutil.rmtree(previous)
    if destination.exists(): destination.replace(previous)
    pending.chmod(0o755)
    pending.replace(destination)


def run(root, arguments):
    root = Path(root)
    private = root / 'data/private'
    private.mkdir(parents=True, exist_ok=True)
    lock = (private / 'build.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another build is using the private workspace')
    work = root / 'data/private/build_workspace'
    dry = '-n' in arguments or '--dry-run' in arguments
    # The first dry run is read-only; subsequent ones explain the private
    # workspace's actual stamps. Source synchronization changes no artifacts.
    if dry and not work.exists():
        env = {**os.environ, 'EPSTEIN_PRIVATE_BUILD': '1'}
        return subprocess.call([sys.executable, '-B', str(root/'scripts/build.py'), *arguments], cwd=root, env=env)
    work = prepare(root)
    env = {**os.environ, 'EPSTEIN_PRIVATE_BUILD': '1', 'PYTHONHASHSEED': '0'}
    status = subprocess.call([sys.executable, '-B', str(work/'scripts/build.py'), *arguments], cwd=work, env=env)
    if status == 0 and not dry:
        print('Publishing contact-safe artifacts; raw caches and logs stay in data/private/build_workspace/output.', flush=True)
        release(work/'output', root/'output', root/'data/private')
    return status
