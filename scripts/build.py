"""Incremental pipeline build: rerun only steps whose inputs changed.

Each step runs under scripts/_trace.py, which records every project file it
reads (data, config, imported scripts), lists, probes or writes. The stamp in
data/private/build_workspace/output/.build/state.json holds fingerprints of those inputs and outputs. A
step reruns when its command, any input's contents, a listed directory, a
probed path's existence or one of its outputs changed, or with --force. A
rerun whose outputs come out byte-identical leaves later steps up to date.

  scripts/build.py                  build what changed
  scripts/build.py -n               explain what would run, and why
  scripts/build.py --force          rebuild everything
  scripts/build.py --force involvement   rerun one step (and whatever its new outputs affect)
  scripts/build.py visualize_graph  only that step and what it needs

Fingerprints: content hash (BLAKE2b), except raw source files above 256 MB
(size+mtime). Digests are cached until a file's stat changes. Large derived
databases must use content too: an identical rebuild must not cascade.
The pipeline runs in a private workspace; only audited contact-safe artifacts
are exported to the checkout's output/. See private_build.py.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output'
STATE_DIR = OUT / '.build'
STATE = STATE_DIR / 'state.json'
PYTHON = [str(ROOT / '.venv/bin/python'), '-B']
# Fixed string hashing: set iteration order (and so ties, float sums, chosen
# canonicals) is the same every run, so a rebuild is byte-reproducible.
STEP_ENV = {**os.environ, 'PYTHONHASHSEED': '0'}
HASH_LIMIT = 256 * 1024 * 1024
IGNORED = ('.venv/', 'output/.build/', '.git/')
TOOLCHAIN = ('requirements.txt', 'requirements-dev.txt')


class Step:
    def __init__(self, name, argv, log, after=(), when=None, traced=True):
        self.name, self.argv, self.log, self.after = name, argv, log, tuple(after)
        self.when, self.traced = when, traced


def viewer():
    """VIEWER=public leaves the encrypted real names out of output/graph.html; private/both (default) include them."""
    choice = os.environ.get('VIEWER', 'private')
    if choice not in ('private', 'public', 'both'):
        raise SystemExit(f'VIEWER must be private, public or both, not {choice!r}')
    return 'both' if choice == 'public' and os.environ.get('PRIVATE') == '1' else choice


def steps():
    env = os.environ.get
    private_db = ROOT / 'data/private/email_places.db'
    return [
        # Validates the contact key and writes empty email-place layers when the
        # optional private input is missing; cheap, so it always runs.
        Step('contact_key', ['-c', 'import sys; sys.path.insert(0, "scripts"); from build_contacts import contact_key, '
                             'email_places_available; contact_key(); email_places_available()'], None, traced=False),
        # Before its readers: private_contacts used to write it last, so any change cost two full passes.
        Step('contact_lookup', ['scripts/private_contacts.py', '--lookup-only'], None, after=['contact_key'],
             traced=False),
        Step('audit_normalization', ['scripts/audit_normalization.py'], 'normalization_audit.log'),
        Step('normalize_graph', ['scripts/normalize_graph.py'], None, after=['contact_key']),
        Step('build_places', ['scripts/build_places.py'], 'places_build.log', after=['normalize_graph']),
        Step('build_communications', ['scripts/build_communications.py', '--view-edges', env('VIEW_EDGES', '20000')],
             'communication_build.log', after=['normalize_graph']),
        Step('build_email_places', ['scripts/build_email_places.py'], 'email_places_build.log',  # cached Luna verdicts only
             after=['build_communications'], when=private_db.is_file),
        # Near-duplicate document clusters (MinHash); co-mention layers count copies once.
        Step('dedupe_documents', ['scripts/dedupe_documents.py'], 'dedupe_documents.log'),
        Step('build_comentions', ['scripts/build_comentions.py', '--view-edges', env('COMENTION_EDGES', '8000')],
             'comention_build.log', after=['build_places', 'build_communications', 'dedupe_documents']),
        Step('build_vehicles', ['scripts/build_vehicles.py'], 'vehicles_build.log',
             after=['build_places', 'build_email_places', 'dedupe_documents']),
        # Full-text company layer: regex candidates, then reviewed ones (cached model verdicts from
        # scripts/classify_companies.py, run by hand) become nodes linked by shared documents.
        # Text the corpus lacks: read off images, handwriting transcriptions (upstream v5.2 extra databases).
        Step('build_supplement', ['scripts/build_supplement.py'], 'supplement_build.log'),
        Step('extract_companies', ['scripts/extract_companies.py'], 'companies_extract.log', after=['normalize_graph', 'build_supplement']),
        Step('build_companies', ['scripts/build_companies.py', '--view-edges', env('COMPANY_EDGES', '10000')],
             'companies_build.log', after=['extract_companies', 'build_comentions', 'contact_lookup']),
        Step('extract_entities', ['scripts/extract_entities.py', '--workers', '2', '--output', 'data/private/entity_mentions.db'], 'entities_extract.log',
             after=['normalize_graph', 'build_supplement', 'contact_key', 'build_companies',
                    'build_places', 'build_communications', 'build_email_places', 'build_victims', 'build_vehicles']),
        Step('build_entities', ['scripts/build_entities.py', '--mentions', 'data/private/entity_mentions.db', '--output', 'data/private/entity_layer'], 'entities_build.log',
             after=['extract_entities', 'build_companies', 'build_places', 'build_communications', 'dedupe_documents',
                    'build_victims', 'build_vehicles']),
        Step('publish_entity_contacts', ['scripts/publish_entity_contacts.py'], 'entity_publication.log', after=['build_entities']),
        # Every email address / phone number written in the full text (raw values stay in data/private).
        Step('extract_contacts', ['scripts/extract_contacts.py', '--workers', '2'], 'contacts_extract.log', after=['contact_key', 'build_supplement']),
        Step('build_fulltext_contacts', ['scripts/build_fulltext_contacts.py'], 'fulltext_contacts_build.log',
             after=['extract_contacts', 'build_communications', 'contact_lookup', 'build_companies', 'publish_entity_contacts']),
        Step('build_victims', ['scripts/build_victims.py', '--view-edges', env('VICTIM_EDGES', '6000')],
             'victims_build.log', after=['build_comentions']),
        Step('involvement', ['scripts/involvement.py'], 'involvement.log', after=['build_comentions']),
        Step('analyze_graph', ['scripts/analyze_graph.py'], 'analysis.log',  # also writes the combined graph
             after=['build_email_places', 'build_victims', 'involvement', 'build_companies', 'build_vehicles', 'publish_entity_contacts', 'build_fulltext_contacts']),
        # One viewer, output/graph.html. Real names are embedded encrypted unless
        # VIEWER=public (the reveal lookup is still refreshed, encryption is omitted).
        Step('build_documents', ['scripts/build_documents.py'], 'documents_build.log', after=['analyze_graph']),
        # Per-document facts and subpoena targets from the upstream v5.2 analysis databases.
        Step('build_doc_info', ['scripts/build_doc_info.py'], 'doc_info_build.log'),
        Step('visualize_graph', ['scripts/visualize_graph.py', '--no-rebuild', *(['--public'] if viewer() == 'public' else [])],
             'visualization.log', after=['analyze_graph', 'build_documents', 'private_contacts', 'build_doc_info']),
        # Ranked leads (who to read about next) from email, flight, schedule, company and DOJ signals.
        Step('build_leads', ['scripts/build_leads.py'], 'leads_build.log',
             after=['build_communications', 'involvement', 'build_companies', 'build_doc_info', 'analyze_graph', 'private_contacts']),
        Step('audit_coverage', ['scripts/audit_coverage.py'], 'audit_coverage.log', after=['analyze_graph']),
        # Pseudonymous Neo4j CSVs + Cypher + GraphML (output/neo4j); --private / --full by hand.
        Step('export_neo4j', ['scripts/export_neo4j.py'], 'neo4j_export.log', after=['analyze_graph', 'build_fulltext_contacts']),
        Step('tests', ['-m', 'unittest', 'discover', '-s', 'scripts', '-p', 'test_*.py'], 'tests.log', after=['analyze_graph']),
        # Local-only real contacts; uses worker processes the tracer cannot see, so it always runs.
        Step('private_contacts', ['scripts/private_contacts.py', *(['--no-pages'] if viewer() == 'public' else [])], None, after=['analyze_graph', 'build_documents', 'build_fulltext_contacts'],
             traced=False),
        # Terminal report over the actual viewer; no other step depends on it.
        Step('audit_nodes', ['scripts/audit_nodes.py'], 'node_audit.log',
             after=['visualize_graph', 'build_leads', 'audit_coverage', 'export_neo4j', 'tests']),
    ]


def relative(path):
    try:
        rel = Path(path).relative_to(ROOT).as_posix()
    except ValueError:
        return None
    # An imported project module is read as its bytecode (scripts/__pycache__/x.cpython-314.pyc),
    # which stays stale under python -B: depend on the source file instead.
    if (cached := re.fullmatch(r'(.*/)?__pycache__/([^/.]+)\.[^/]+\.pyc', rel)):
        rel = f'{cached.group(1) or ""}{cached.group(2)}.py'
    return None if rel.startswith(IGNORED) or '__pycache__' in rel else rel


class Fingerprints:
    """Content/stat fingerprints, reusing a digest while a file's stat is unchanged."""

    def __init__(self, cache):
        self.cache, self.lock = cache, threading.Lock()

    def file(self, rel):
        path = ROOT / rel
        try:
            st = path.stat()
        except OSError:
            return None
        if path.is_dir():
            return 'dir'
        if st.st_size > HASH_LIMIT and rel.startswith('data/raw/'):
            return f'stat:{st.st_size}:{st.st_mtime_ns}'
        key = [st.st_size, st.st_mtime_ns, st.st_ino]
        with self.lock:
            cached = self.cache.get(rel)
        if cached and cached[0] == key:
            return cached[1]
        with path.open('rb') as f:
            digest = hashlib.file_digest(f, 'blake2b').hexdigest()[:32]
        with self.lock:
            self.cache[rel] = [key, digest]
        return digest

    def matches(self, rel, old):
        current = self.file(rel)
        if current == old:
            return True
        # Older stamps used size+mtime for large derived files too. Accept an
        # unchanged legacy stat once, then upgrade it to a content fingerprint
        # below. Changing the fingerprint format alone must not force rebuilds.
        if (isinstance(old, str) and old.startswith('stat:') and current is not None
                and not current.startswith('stat:')):
            st = (ROOT / rel).stat()
            return old == f'stat:{st.st_size}:{st.st_mtime_ns}'
        return False

    @staticmethod
    def listing(rel):
        try:
            names = sorted(os.listdir(ROOT / rel))
        except OSError:
            return None
        return hashlib.blake2b('\n'.join(names).encode()).hexdigest()[:32]

    @staticmethod
    def kind(rel):
        path = ROOT / rel
        return 'dir' if path.is_dir() else 'file' if path.exists() else None


def command(step):
    return list(step.argv)  # VIEW_EDGES etc. are already in the arguments


def toolchain(fp):
    return {'python': sys.version, **{name: fp.file(name) for name in TOOLCHAIN}}


def stale_reason(step, stamp, fp):
    """Why the step must rerun, or None if its stamp still holds."""
    if stamp is None:
        return 'never built'
    if stamp['command'] != command(step):
        return 'command changed'
    if stamp['toolchain'] != toolchain(fp):
        return 'python or requirements changed'
    for rel, old in stamp['inputs'].items():
        if not fp.matches(rel, old):
            return f'input changed: {rel}'
        stamp['inputs'][rel] = fp.file(rel)
    for rel, old in stamp['listed'].items():
        if fp.listing(rel) != old:
            return f'directory contents changed: {rel}/'
    for rel, old in stamp['probed'].items():
        if fp.kind(rel) != old:
            return f'path {"appeared" if old is None else "disappeared"}: {rel}'
    for rel, old in stamp['outputs'].items():
        if not fp.matches(rel, old):
            return f'output missing or modified: {rel}'
        stamp['outputs'][rel] = fp.file(rel)
    return None


def make_stamp(step, trace, fp):
    def rels(paths):
        return {r for r in map(relative, paths) if r}
    writes = rels(trace['writes'])
    reads = rels(trace['reads']) - writes
    if step.name == 'build_entities':
        reads.add('data/private/entity_mentions.db')  # ATTACH is not a sqlite.connect audit event
    return {
        'command': command(step),
        'toolchain': toolchain(fp),
        'inputs': {r: fp.file(r) for r in sorted(reads) if fp.kind(r) != 'dir'},
        'listed': {r: fp.listing(r) for r in sorted(rels(trace['listed']) - writes)},
        'probed': {r: fp.kind(r) for r in sorted(rels(trace['probed']) - writes - reads)},
        # By content: analyze_graph rewrites normalize_graph's files identically.
        'outputs': {r: fp.file(r) for r in sorted(writes) if fp.kind(r) == 'file'},
    }


PAGE_MB = os.sysconf('SC_PAGE_SIZE') / 2 ** 20


def tree_rss_mb(pid):
    """Resident memory in MB of a process and all its descendants (pool workers included)."""
    children = defaultdict(list)
    rss = {}
    for stat in Path('/proc').glob('[0-9]*/stat'):
        try:
            text = stat.read_text()
            fields = text[text.rindex(')') + 2:].split()
        except (OSError, ValueError):
            continue
        child = int(stat.parent.name)
        children[int(fields[1])].append(child)
        rss[child] = int(fields[21])
    total, todo = 0, [pid]
    while todo:
        p = todo.pop()
        total += rss.get(p, 0)
        todo.extend(children.get(p, ()))
    return total * PAGE_MB



def wait_peak(process, interval=0.5):
    """Wait for a child; returns (exit code, peak resident memory in MB).

    The peak is the largest sampled sum over the child's whole process tree:
    ru_maxrss alone is the biggest single process, so a step with N pool workers
    would count as one worker. ru_maxrss still covers spikes between samples."""
    peak, done = 0.0, threading.Event()

    def sample():
        nonlocal peak
        while not done.wait(interval):
            peak = max(peak, tree_rss_mb(process.pid))
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    _, status, usage = os.wait4(process.pid, 0)
    done.set()
    sampler.join()
    process.returncode = os.waitstatus_to_exitcode(status)
    return process.returncode, max(peak, usage.ru_maxrss / 1024)


def available_mb():
    try:
        return int(re.search(r'MemAvailable:\s+(\d+)', Path('/proc/meminfo').read_text()).group(1)) / 1024
    except (OSError, AttributeError):
        return float('inf')


def configure_resources():
    """Leave two CPUs free and give interactive work priority over the build.

    Affinity and niceness are inherited by children, including worker pools
    that would otherwise each use all CPUs. Only this build process changes.
    """
    if hasattr(os, 'sched_getaffinity'):
        cpus = sorted(os.sched_getaffinity(0))
        os.sched_setaffinity(0, cpus[:max(1, len(cpus) - 2)])
    if hasattr(os, 'nice'):
        os.nice(max(0, 10 - os.getpriority(os.PRIO_PROCESS, 0)))


def run(step, fp):
    """Run the step; returns (ok, stamp or None, seconds, peak MB)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    trace = STATE_DIR / f'{step.name}.trace.json'
    trace.unlink(missing_ok=True)
    # Unstamped script steps still need the workspace's canonical corpus path
    # for FTS cache recognition. Their worker accesses are not used as stamps.
    wrapped = step.traced or step.argv[0].endswith('.py')
    argv = PYTHON + (['scripts/_trace.py', str(trace)] if wrapped else []) + step.argv
    started = time.time()
    if step.log:
        with (OUT / step.log).open('w') as log:
            code, peak = wait_peak(subprocess.Popen(argv, cwd=ROOT, env=STEP_ENV, stdout=log, stderr=subprocess.STDOUT))
    else:
        code, peak = wait_peak(subprocess.Popen(argv, cwd=ROOT, env=STEP_ENV))
    seconds = time.time() - started
    if code or not step.traced:
        return code == 0, None, seconds, peak
    return True, make_stamp(step, json.loads(trace.read_text()), fp), seconds, peak


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('targets', nargs='*', help='steps to bring up to date (default: all)')
    parser.add_argument('-n', '--dry-run', action='store_true', help='explain what would run, without running')
    parser.add_argument('--force', action='store_true',
                        help='rerun the named steps (all steps if none are named); their dependencies only if stale')
    parser.add_argument('-j', '--jobs', type=int, default=2,
                        help='steps run at once (default 2); a step only starts if the recorded peak memory of the '
                             'running steps plus its own fits in the RAM available at start, so steps never push each other into swap')
    args = parser.parse_args()
    all_steps = {s.name: s for s in steps()}
    unknown = [t for t in args.targets if t not in all_steps]
    if unknown:
        parser.error(f'unknown step(s) {", ".join(unknown)}; choose from {", ".join(all_steps)}')
    selected, todo = set(), list(args.targets or all_steps)
    while todo:  # targets plus everything they depend on
        name = todo.pop()
        if name not in selected:
            selected.add(name)
            todo.extend(all_steps[name].after)
    order = [s for s in all_steps.values() if s.name in selected]
    forced = set(args.targets or all_steps) if args.force else set()

    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    stamps, fp = state.setdefault('steps', {}), Fingerprints(state.setdefault('digests', {}))
    if not args.dry_run and 'extract_entities' in selected:
        from entity_cache_migration import migrate
        migrate(ROOT, state, fp, stale_reason, Step, move_entities='build_entities' in selected)
    peaks = state.setdefault('peak_mb', {})  # largest peak resident memory per step (successful runs)
    lock = threading.Lock()

    def save():
        with lock:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            partial = STATE.with_suffix('.partial.json')
            partial.write_text(json.dumps(state, indent=1, sort_keys=True))
            partial.replace(STATE)

    if args.dry_run:
        pending = set()
        for step in order:
            if step.when and not step.when():
                print(f'off    {step.name}')
                continue
            upstream = [d for d in step.after if d in pending]
            reason = ('forced' if step.name in forced else 'always runs' if not step.traced
                      else stale_reason(step, stamps.get(step.name), fp))
            if reason:
                if step.traced or step.name in forced:  # the always-run key check changes nothing downstream
                    pending.add(step.name)
                print(f'run    {step.name}  ({reason})')
            elif upstream:
                pending.add(step.name)
                print(f'maybe  {step.name}  (reruns if new outputs of {", ".join(upstream)} differ)')
            else:
                print(f'ok     {step.name}')
        return

    configure_resources()
    # Large SQLite indexes spill several GB; /tmp is a small RAM-backed mount
    # on this machine. Keep sort files private and on the workspace SSD.
    sqlite_temp = ROOT / 'data/private/build_tmp'
    sqlite_temp.mkdir(parents=True, exist_ok=True, mode=0o700)
    sqlite_temp.chmod(0o700)
    STEP_ENV['SQLITE_TMPDIR'] = str(sqlite_temp)
    status = {}  # name -> 'ok' | 'failed' | 'blocked'
    started = time.time()

    def execute(step):
        if step.when and not step.when():
            return 'ok', f'off    {step.name}'
        reason = 'forced' if step.name in forced else 'always runs' if not step.traced else stale_reason(step, stamps.get(step.name), fp)
        if not reason:
            return 'ok', f'ok     {step.name}'
        print(f'start  {step.name}  ({reason})', flush=True)
        ok, stamp, seconds, peak = run(step, fp)
        if ok:  # a failed run stopped early; keep the largest successful peak (a cache hit is small)
            with lock:
                peaks[step.name] = max(round(peak), peaks.get(step.name, 0))
        if not ok:
            if step.traced:
                with lock:
                    stamps.pop(step.name, None)
            save()
            log = f'; see output/{step.log}' if step.log else ''
            return 'failed', f'FAILED {step.name} after {seconds:.1f}s{log}'
        if stamp is not None:
            with lock:
                stamps[step.name] = stamp
        save()
        return 'ok', f'done   {step.name}  {seconds:.1f}s  {peak / 1024:.1f} GB'

    available = available_mb()
    budget = min(available * 0.75, max(0, available - 2048))
    def need(name):  # MB; steps never measured count as 1.5 GB
        return peaks.get(name, 1500)
    with ThreadPoolExecutor(max(1, args.jobs)) as pool:
        running = {}
        while len(status) < len(order):
            for step in order:
                if step.name in status or step.name in running.values():
                    continue
                deps = [status.get(d) for d in step.after if d in selected]
                if any(d in ('failed', 'blocked') for d in deps):
                    status[step.name] = 'blocked'
                    print(f'skip   {step.name}  (an upstream step failed)')
                elif all(d == 'ok' for d in deps) and len(running) < max(1, args.jobs) and (
                        not running or sum(map(need, running.values())) + need(step.name) <= budget):
                    running[pool.submit(execute, step)] = step.name
            if not running:
                continue
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in finished:
                name = running.pop(future)
                status[name], message = future.result()
                print(message, flush=True)
    failed = [n for n, s in status.items() if s == 'failed']
    for name in failed:
        log = all_steps[name].log
        if log and (OUT / log).exists():
            print(f'\n--- tail of output/{log} ---', file=sys.stderr)
            print(''.join((OUT / log).read_text().splitlines(True)[-25:]), file=sys.stderr)
    print(f'{"FAILED" if failed else "Up to date"} in {time.time() - started:.1f}s')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    if os.environ.get('EPSTEIN_PRIVATE_BUILD') == '1':
        main()
    else:
        from private_build import run
        sys.exit(run(ROOT, sys.argv[1:]))
