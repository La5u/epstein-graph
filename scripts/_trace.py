"""Run one pipeline step and record which project files it touched.

Usage: python _trace.py TRACE.json (SCRIPT.py | -m MODULE) [ARGS...]

Records, via Python audit hooks plus an os.stat wrapper: files read (including
imported project modules), files written/renamed/removed, directories listed
and paths probed for existence. scripts/build.py turns this into the step's
dependency stamp, so dependencies never have to be listed by hand.
"""
import json
import os
import runpy
import sys
from urllib.parse import unquote, urlsplit

reads, writes, listed, probed = set(), set(), set(), set()
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _path(value):
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    return os.path.abspath(value) if isinstance(value, (str, os.PathLike)) else None


def _sqlite(database):
    database = os.fsdecode(database) if isinstance(database, bytes) else str(database)
    if database in ('', ':memory:') or database.startswith('file::memory:'):
        return
    mode = 'rwc'
    if database.startswith('file:'):
        parts = urlsplit(database)
        query = dict(p.split('=', 1) for p in parts.query.split('&') if '=' in p)
        mode = query.get('mode', 'rwc')
        database = unquote(parts.path)
    path = _path(database)
    (reads if mode == 'ro' else writes).add(path)


def _hook(event, args):
    if event == 'open':
        path, mode, flags = args
        path = _path(path)
        if path is None:
            return
        writing = any(c in mode for c in 'wax+') if isinstance(mode, str) else bool(flags & WRITE_FLAGS)
        (writes if writing else reads).add(path)
        if isinstance(mode, str) and '+' in mode and 'w' not in mode:
            reads.add(path)  # r+ / a+ also read existing contents
    elif event == 'sqlite3.connect':
        _sqlite(args[0])
    elif event == 'os.rename':
        writes.update(p for p in map(_path, args[:2]) if p)
    elif event == 'os.remove':
        if (path := _path(args[0])):
            writes.add(path)
    elif event == 'epstein.output':
        # A producer verified its rebuilt file against the existing bytes and
        # kept the old inode/mtime. It still owns this output on cache hits.
        writes.add(_path(args[0]))
    elif event in ('os.scandir', 'os.listdir'):
        # The import system lists sys.path entries; imported modules are already reads.
        if 'importlib' in sys._getframe(1).f_code.co_filename:
            return
        if args and (path := _path(args[0] if args[0] is not None else '.')):
            listed.add(path)


_stat = os.stat


def _probing_stat(path, *args, **kwargs):
    # Path.exists()/is_file() go through os.stat and raise no audit event.
    if isinstance(path, (str, bytes, os.PathLike)):
        probed.add(_path(path))
    return _stat(path, *args, **kwargs)


def main():
    trace, target, *rest = sys.argv[1:]
    os.stat = _probing_stat
    sys.addaudithook(_hook)
    if os.environ.get('EPSTEIN_PRIVATE_BUILD') == '1':
        # SQLite canonicalizes the workspace's shared data symlink in
        # database_list. Match that path when recognizing the real corpus;
        # otherwise every existing FTS cache entry would be bypassed. This
        # changes cache recognition only, never query results or stamps.
        import fts_cache
        fts_cache.CORPUS = fts_cache.CORPUS.resolve()
    status = 0
    try:
        if target == '-m':
            module, *rest = rest
            sys.argv = [module, *rest]
            runpy.run_module(module, run_name='__main__', alter_sys=True)
        else:
            sys.argv = [target, *rest]
            sys.path.insert(0, os.path.dirname(os.path.abspath(target)))
            runpy.run_path(target, run_name='__main__')
    except SystemExit as error:
        status = error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
    finally:
        os.stat = _stat
        with open(trace, 'w') as f:
            json.dump({'reads': sorted(reads), 'writes': sorted(writes),
                       'listed': sorted(listed), 'probed': sorted(probed)}, f)
    sys.exit(status)


if __name__ == '__main__':
    main()
