#!/usr/bin/env python3
"""Bounded read-only audit; never installs exports into active data.

Manifest v1 (private): resources maps spec/index/stats/persons/flights/locations/
organizations to {url, status, error?, path?, fetched_utc?, sha256?, bytes?,
headers?}. Paths are relative to the manifest directory; files preserve exact
response bytes, including CSV. status is ok/error; error is a safe code, not
server text. headers contains only Content-Type, ETag, Last-Modified. Exports
must pass validation before appearing in datasets; datasets[name] records
resource, format, actual_count, advertised_count (possibly null), and diff.
Detailed IDs/field changes live in details.json, not the public reports.
Consumers MUST check resource status and datasets membership before use.
"""
import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import tempfile
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request

BASE = 'https://epsteinexposed.com'
DATASETS = ('persons', 'flights', 'locations', 'organizations')
PATHS = {'spec': '/api/v2/openapi.json', 'index': '/api/v2/export',
         'stats': '/api/v2/stats', **{n: f'/api/v2/export/{n}?format=json' for n in DATASETS}}
# Fixed small sample tied to the current research; never crawl discovered URLs.
PROBES = {
    'hbrk_detail': '/api/v2/organizations/hbrk-associates',
    'haze_detail': '/api/v2/organizations/haze-trust',
    'hyperion_detail': '/api/v2/organizations/hyperion-air-inc',
    'beller_detail': '/api/v2/persons/harry-beller',
    'beller_documents': '/api/v2/persons/harry-beller/documents?page=1&per_page=5',
    'beller_connections': '/api/v2/persons/harry-beller/connections',
}


class AuditError(Exception):
    pass


def utc():
    return datetime.now(timezone.utc).isoformat()


def safe_url(url):
    p = urllib.parse.urlsplit(url)
    if (p.scheme != 'https' or p.netloc != 'epsteinexposed.com' or p.fragment
            or p.path + ('?' + p.query if p.query else '') not in (*PATHS.values(), *PROBES.values())):
        raise AuditError('unsafe_url')
    return url


class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def retry_delay(value, now=None):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError):
            seconds = 1
    return min(30, max(1, seconds)) if math.isfinite(seconds) else 1


class Fetcher:
    def __init__(self, timeout=30, max_bytes=32 * 1024 * 1024, retries=2,
                 opener=None, sleep=time.sleep):
        self.timeout, self.max_bytes, self.retries = timeout, max_bytes, retries
        self.opener = opener or urllib.request.build_opener(Redirects())
        self.sleep = sleep
        self.started = False

    def fetch(self, url):
        safe_url(url)
        for attempt in range(self.retries + 1):
            if self.started:
                self.sleep(1)
            self.started = True
            req = urllib.request.Request(url, headers={
                'Accept': 'application/json, text/csv;q=0.8',
                'User-Agent': 'EpsteinExposed-ReadOnly-Audit/1.0'})
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    safe_url(response.geturl())
                    if response.status != 200:
                        raise AuditError('http_status')
                    raw = response.read(self.max_bytes + 1)
                    if len(raw) > self.max_bytes:
                        raise AuditError('response_too_large')
                    headers = {k: response.headers[k] for k in
                               ('Content-Type', 'ETag', 'Last-Modified') if k in response.headers}
                    return raw, headers
            except urllib.error.HTTPError as exc:
                delay = retry_delay(exc.headers.get('Retry-After'))
                code = exc.code
                exc.close()
                if code != 429 or attempt == self.retries:
                    raise AuditError('http_' + str(code)) from None
                self.sleep(delay)
            except (urllib.error.URLError, OSError, TimeoutError, ValueError):
                raise AuditError('transport_error') from None
        raise AuditError('retry_exhausted')


def atomic(path, raw, private=True):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.audit-')
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), 0o600 if private else 0o644)
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_json(path, value, private=True):
    atomic(path, (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode(), private)


def decode_json(raw):
    try:
        return json.loads(raw.decode('utf-8-sig'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise AuditError('invalid_json') from None


def validate_rows(rows):
    if not isinstance(rows, list):
        raise AuditError('invalid_records')
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'].strip():
            raise AuditError('invalid_id')
        if row['id'] in ids:
            raise AuditError('duplicate_id')
        ids.add(row['id'])
    return rows


def normalize_csv(rows, baseline):
    types = {}
    for row in baseline:
        for key, value in row.items():
            if value is not None:
                types.setdefault(key, set()).add(type(value))
    for row in rows:
        for key, value in row.items():
            if key is None or value is None:
                raise AuditError('invalid_csv_shape')
            kinds = types.get(key, set())
            if value == '' and kinds != {str}:
                row[key] = None
            elif kinds in ({int}, {float}, {int, float}, {bool}, {list}, {dict}):
                try:
                    parsed = json.loads(value)
                    if kinds == {float} and type(parsed) is int:
                        parsed = float(parsed)
                    if type(parsed) not in kinds or (isinstance(parsed, float) and not math.isfinite(parsed)):
                        raise ValueError()
                    row[key] = parsed
                except ValueError:
                    raise AuditError('invalid_csv_type') from None
            elif kinds and kinds != {str}:
                raise AuditError('ambiguous_csv_type')
    return rows


def parse_export(raw, headers, baseline):
    text = raw.decode('utf-8-sig')
    if 'html' in headers.get('Content-Type', '').lower() or text.lstrip().startswith('<'):
        raise AuditError('html_response')
    if text.lstrip().startswith(('{', '[')):
        value = decode_json(raw)
        rows = value.get('data') if isinstance(value, dict) else value
        if isinstance(value, dict) and ('total' in value and
                (type(value['total']) is not int or value['total'] < 0)):
            raise AuditError('invalid_total')
        return validate_rows(rows), 'json'
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or 'id' not in reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise AuditError('invalid_csv_header')
    return validate_rows(normalize_csv(list(reader), baseline)), 'csv'


def field_group(field):
    if 'citation' in field or 'document' in field or field in ('sources', 'references'):
        return 'document_citation'
    return 'metadata' if field.endswith('_count') or field in ('created_at', 'updated_at') else 'content'


def diff_records(old, new):
    before = {r['id']: r for r in validate_rows(old)}
    after = {r['id']: r for r in validate_rows(new)}
    changed = []
    for key in sorted(before.keys() & after.keys()):
        a, b = before[key], after[key]
        # Missing and null are distinct, and field additions/removals are explicit.
        groups = {}
        for field in sorted(a.keys() | b.keys()):
            kind = ('added' if field not in a else 'removed' if field not in b else
                    'changed' if type(a[field]) != type(b[field]) or a[field] != b[field] else None)
            if kind:
                groups.setdefault(field_group(field), {}).setdefault(kind, []).append(field)
        if groups:
            changed.append({'id': key, 'fields': groups})
    return {'added': sorted(after.keys() - before.keys()),
            'removed': sorted(before.keys() - after.keys()), 'changed': changed}


def advertised_count(index, name):
    if not isinstance(index, dict):
        return None
    index = index.get('data', index)
    if not isinstance(index, dict):
        return None
    entries = index.get('exports', index.get('datasets', index))
    if isinstance(entries, list):
        entry = next((x for x in entries if isinstance(x, dict) and
                      x.get('id', x.get('name', x.get('dataset'))) == name), None)
    else:
        entry = entries.get(name) if isinstance(entries, dict) else None
    count = entry.get('record_count', entry.get('count', entry.get('total'))) if isinstance(entry, dict) else entry
    return count if type(count) is int and count >= 0 else None


def inventory(spec):
    if not isinstance(spec, dict) or not isinstance(spec.get('openapi'), str) or not isinstance(spec.get('paths'), dict):
        raise AuditError('invalid_spec')
    result = []
    for path, operations in spec['paths'].items():
        if not isinstance(operations, dict):
            raise AuditError('invalid_spec')
        for method, op in operations.items():
            if method not in ('get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'trace'):
                continue
            if not isinstance(op, dict):
                raise AuditError('invalid_spec')
            tags = op.get('tags', [])
            premium = bool(op.get('x-premium', False) or op.get('x-paid', False) or
                           'premium' in json.dumps(op).lower() or 'paid' in str(tags).lower())
            result.append({'path': path, 'method': method, 'tags': tags, 'premium': premium})
    return result


def probe_summary(value):
    """Report shape/evidence availability only; never export names or assertions."""
    if value.get('status') not in (None, 'ok'):
        raise AuditError('api_error_response')
    data = value.get('data')
    if not isinstance(data, (dict, list)):
        raise AuditError('invalid_probe_data')
    rows = data if isinstance(data, list) else [data]
    fields = sorted({k for row in rows if isinstance(row, dict) for k in row})
    reference_fields = [k for k in fields if not k.endswith('_count') and
                        any(t in k.lower() for t in ('source', 'citation', 'reference', 'document', 'evidence'))]
    collections = {key: len(value) for key, value in data.items() if isinstance(value, list)} if isinstance(data, dict) else {}
    return {'response_kind': type(data).__name__, 'returned_records': len(rows),
            'fields': fields, 'reference_fields': reference_fields,
            'collection_counts': collections,
            'has_api_attribution': isinstance(value.get('citation'), dict),
            'complete': False, 'warning': 'Bounded sample; fields are not verified evidence or complete coverage.'}


def run(root, fetcher, probe_details=False):
    root = Path(root)
    private = root / 'data/private'
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(private, 0o700)
    parent = private / 'epsteinexposed_api'
    parent.mkdir(exist_ok=True, mode=0o700)
    os.chmod(parent, 0o700)
    stage = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-'), dir=parent))
    manifest = {'schema_version': 1, 'started_utc': utc(), 'resources': {}, 'datasets': {},
                'details_path': 'details.json'}
    details = {'endpoint_inventory': [], 'datasets': {}}
    public = {'schema_version': 1, 'started_utc': manifest['started_utc'], 'datasets': {}, 'errors': {}}
    index = {}
    paths = {**PATHS, **(PROBES if probe_details else {})}
    for name, path in paths.items():
        url = BASE + path
        entry = {'url': url, 'status': 'error'}
        manifest['resources'][name] = entry
        try:
            raw, headers = fetcher.fetch(url)
            filename = name + '.response'
            atomic(stage / filename, raw)
            entry.update(path=filename, fetched_utc=utc(), sha256=hashlib.sha256(raw).hexdigest(),
                         bytes=len(raw), headers=headers)
            if name in DATASETS:
                baseline_path = root / 'data/external/epsteinexposed' / (name + '.json')
                baseline_raw = baseline_path.read_bytes()
                baseline = decode_json(baseline_raw)
                if not isinstance(baseline, dict):
                    raise AuditError('invalid_baseline')
                old = validate_rows(baseline.get('data'))
                rows, fmt = parse_export(raw, headers, old)
                diff = diff_records(old, rows)
                count = advertised_count(index, name)
                details['datasets'][name] = diff
                manifest['datasets'][name] = {'resource': name, 'format': fmt, 'actual_count': len(rows),
                                             'advertised_count': count, 'diff': 'details.json',
                                             'baseline': {'path': str(baseline_path.relative_to(root)),
                                                          'sha256': hashlib.sha256(baseline_raw).hexdigest()}}
                public['datasets'][name] = {'baseline_count': len(old), 'actual_count': len(rows),
                    'advertised_count': count, 'advertised_matches_actual': None if count is None else count == len(rows),
                    **{k: len(v) for k, v in diff.items()},
                    'changed_records_by_group': {g: sum(g in x['fields'] for x in diff['changed'])
                                                for g in ('content', 'document_citation', 'metadata')}}
            else:
                value = decode_json(raw)
                if not isinstance(value, dict):
                    raise AuditError('invalid_schema')
                if name == 'spec':
                    details['endpoint_inventory'] = inventory(value)
                    public['spec'] = {'operations': len(details['endpoint_inventory']),
                                      'premium_flagged': sum(x['premium'] for x in details['endpoint_inventory'])}
                elif name == 'index':
                    index = value
                elif name in PROBES:
                    public.setdefault('detail_probes', {})[name] = probe_summary(value)
            entry['status'] = 'ok'
        except (AuditError, OSError, ValueError, TypeError, KeyError, csv.Error) as exc:
            code = str(exc) if isinstance(exc, AuditError) else 'local_or_schema_error'
            entry['error'] = code
            public['errors'][name] = code
        save_json(stage / 'manifest.json', manifest)
    manifest['completed_utc'] = public['completed_utc'] = utc()
    save_json(stage / 'details.json', details)
    save_json(stage / 'manifest.json', manifest)
    output = root / 'output'
    output.mkdir(exist_ok=True)
    save_json(output / 'epsteinexposed_api_audit.json', public, private=False)
    lines = ['# EpsteinExposed API audit', '', 'Read-only; active datasets were not replaced.', '',
             '| Dataset | Baseline | Fetched | Added | Removed | Changed |', '|---|---:|---:|---:|---:|---:|']
    for name, row in public['datasets'].items():
        lines.append('| ' + name + ' | ' + ' | '.join(str(row[k]) for k in
                     ('baseline_count', 'actual_count', 'added', 'removed', 'changed')) + ' |')
    lines += ['', '## Export-index discrepancies', '']
    for name, row in public['datasets'].items():
        if row['advertised_matches_actual'] is False:
            lines.append(f"- {name}: index advertises {row['advertised_count']}; export returned {row['actual_count']}.")
    if public.get('detail_probes'):
        lines += ['', '## Bounded detail probes', '',
                  'Empty lists are API coverage gaps, not proof of absent relationships.', '']
        for name, probe in public['detail_probes'].items():
            counts = ', '.join(f'{k}={v}' for k, v in probe['collection_counts'].items())
            lines.append(f"- {name}: {probe['returned_records']} returned records; {counts or 'no list fields'}.")
    lines += ['', 'Errors: ' + str(len(public['errors'])) + '. See aggregate JSON for safe error codes.',
              'Advertised counts are informational, not completeness guarantees.',
              'Detailed IDs, field changes, and endpoint inventory are private.']
    atomic(output / 'epsteinexposed_api_audit.md', ('\n'.join(lines) + '\n').encode(), private=False)
    return 1 if public['errors'] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--max-bytes', type=int, default=32 * 1024 * 1024)
    parser.add_argument('--retries', type=int, default=2)
    parser.add_argument('--probe-details', action='store_true',
                        help='also fetch six fixed, bounded organization/person detail samples')
    args = parser.parse_args(argv)
    if not (0 < args.timeout <= 120 and 0 < args.max_bytes <= 128 * 1024 * 1024 and 0 <= args.retries <= 3):
        parser.error('limits: timeout 0..120s, max-bytes 1..134217728, retries 0..3')
    try:
        return run(args.root, Fetcher(args.timeout, args.max_bytes, args.retries), args.probe_details)
    except OSError:
        print('Audit could not write local reports.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
