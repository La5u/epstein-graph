"""Offline tests: python -m unittest discover -s scripts -p test_epsteinexposed_api.py"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import urllib.error

import audit_epsteinexposed_api as audit


class AuditTests(unittest.TestCase):
    def test_url_allowlist(self):
        for path in audit.PATHS.values():
            self.assertEqual(audit.safe_url(audit.BASE + path), audit.BASE + path)
        for url in ('http://epsteinexposed.com/api/v2/stats',
                    'https://evil.test/api/v2/stats',
                    audit.BASE + '/api/v2/persons', audit.BASE + '/api/v2/export/persons',
                    audit.BASE + '/api/v2/stats?paid=true',
                    'https://epsteinexposed.com@evil.test/api/v2/stats'):
            with self.assertRaises(audit.AuditError):
                audit.safe_url(url)
        with self.assertRaises(audit.AuditError):
            audit.Redirects().redirect_request(None, None, 302, '', {}, 'https://evil.test/')

    def test_retry_and_bounds(self):
        response = Mock(status=200, headers={'Content-Type': 'application/json'})
        response.geturl.return_value = audit.BASE + audit.PATHS['stats']
        response.read.return_value = b'{}'
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        opener, sleep = Mock(), Mock()
        opener.open.side_effect = [urllib.error.HTTPError(audit.BASE, 429, '', {'Retry-After': '999'}, None), context]
        fetcher = audit.Fetcher(max_bytes=10, opener=opener, sleep=sleep)
        self.assertEqual(fetcher.fetch(audit.BASE + audit.PATHS['stats'])[0], b'{}')
        self.assertEqual(sleep.call_args_list[0].args, (30,))
        self.assertEqual(sleep.call_args_list[1].args, (1,))
        response.read.assert_called_with(11)
        opener.open.side_effect = None
        opener.open.return_value = context
        response.read.return_value = b'x' * 11
        with self.assertRaisesRegex(audit.AuditError, 'response_too_large'):
            fetcher.fetch(audit.BASE + audit.PATHS['stats'])
        response.read.return_value = b'{}'
        response.geturl.return_value = 'https://evil.test/'
        with self.assertRaisesRegex(audit.AuditError, 'unsafe_url'):
            fetcher.fetch(audit.BASE + audit.PATHS['stats'])
        opener.open.side_effect = urllib.error.HTTPError(audit.BASE, 429, '', {}, None)
        fetcher.retries = 1
        before = opener.open.call_count
        with self.assertRaisesRegex(audit.AuditError, 'http_429'):
            fetcher.fetch(audit.BASE + audit.PATHS['stats'])
        self.assertEqual(opener.open.call_count - before, 2)
        self.assertEqual(audit.retry_delay('-1'), 1)
        self.assertEqual(audit.retry_delay('Thu, 01 Jan 1970 00:00:10 GMT', now=0), 10)

    def test_csv_types_and_validation(self):
        baseline = [{'id': '001', 'count': 2, 'lat': 1.25, 'aliases': ['a'], 'flag': True, 'name': '007'}]
        raw = b'id,count,lat,aliases,flag,name\n001,2,1.25,"[""a""]",true,007\n'
        rows, fmt = audit.parse_export(raw, {}, baseline)
        self.assertEqual(fmt, 'csv')
        self.assertEqual(rows, baseline)
        self.assertEqual(audit.diff_records(baseline, rows)['changed'], [])
        for raw in (b'<html>blocked</html>', b'{"data":{}}', b'{"data":[{"id":"a"},{"id":"a"}]}',
                    b'[{"name":"x"}]', b'id,id\na,a\n', b'id,count\na,oops\n',
                    b'[{"id":"a","value":NaN}]'):
            with self.assertRaises(audit.AuditError):
                audit.parse_export(raw, {}, baseline)

    def test_live_export_index_envelope(self):
        index = {'api_version': '2.0', 'data': {'datasets': [
            {'id': 'locations', 'record_count': 83},
            {'id': 'persons', 'record_count': 1893}]}}
        self.assertEqual(audit.advertised_count(index, 'locations'), 83)
        self.assertEqual(audit.advertised_count(index, 'persons'), 1893)
        self.assertIsNone(audit.advertised_count(index, 'missing'))

    def test_detail_probe_summary_is_not_a_content_export(self):
        value = {'status': 'ok', 'data': [{'name': 'PRIVATE-NAME', 'source_url': 'PRIVATE-URL'}],
                 'citation': {'source': 'upstream'}}
        summary = audit.probe_summary(value)
        self.assertEqual(summary['returned_records'], 1)
        self.assertEqual(summary['reference_fields'], ['source_url'])
        self.assertNotIn('PRIVATE', json.dumps(summary))
        self.assertFalse(summary['complete'])
        with self.assertRaises(audit.AuditError):
            audit.probe_summary({'status': 'error', 'data': {}})

    def test_diff_field_presence_and_groups(self):
        result = audit.diff_records(
            [{'id': 'same', 'null': None, 'document_count': 1, 'email_count': 0}, {'id': 'gone'}],
            [{'id': 'same', 'added_null': None, 'document_count': 2, 'email_count': 1}, {'id': 'new'}])
        self.assertEqual(result['added'], ['new'])
        self.assertEqual(result['removed'], ['gone'])
        groups = result['changed'][0]['fields']
        self.assertEqual(groups['content'], {'added': ['added_null'], 'removed': ['null']})
        self.assertEqual(groups['document_citation'], {'changed': ['document_count']})
        self.assertEqual(groups['metadata'], {'changed': ['email_count']})

    def test_run_private_partial_and_public(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / 'data/external/epsteinexposed'
            active.mkdir(parents=True)
            original = b'{"data":[{"id":"SECRET-ID","name":"SECRET-NAME","count":1}],"total":1}'
            for name in audit.DATASETS:
                (active / (name + '.json')).write_bytes(original)
            responses = {
                'spec': {'openapi': '3.0.0', 'paths': {'/paid': {'get': {'tags': ['premium']}}}},
                'index': {'exports': {'persons': {'count': 100}}}, 'stats': {},
                **{name: {'data': [{'id': 'SECRET-ID', 'name': 'SECRET-CHANGED', 'count': 2}], 'total': 1}
                   for name in audit.DATASETS}}
            responses['flights'] = {'data': [{'id': 'duplicate'}, {'id': 'duplicate'}]}
            class Offline:
                def fetch(self, url):
                    name = next(k for k, path in audit.PATHS.items() if url == audit.BASE + path)
                    return json.dumps(responses[name]).encode(), {'Content-Type': 'application/json'}
            self.assertEqual(audit.run(root, Offline()), 1)
            stage = next((root / 'data/private/epsteinexposed_api').iterdir())
            manifest = json.loads((stage / 'manifest.json').read_text())
            self.assertNotIn('flights', manifest['datasets'])
            self.assertEqual(manifest['resources']['flights']['error'], 'duplicate_id')
            self.assertEqual(manifest['datasets']['persons']['advertised_count'], 100)
            for name, entry in manifest['resources'].items():
                raw = (stage / entry['path']).read_bytes()
                self.assertEqual(entry['sha256'], hashlib.sha256(raw).hexdigest())
                self.assertEqual(entry['bytes'], len(raw))
                self.assertEqual((active / (name + '.json')).read_bytes(), original) if name in audit.DATASETS else None
            for p in stage.iterdir():
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertEqual(stage.stat().st_mode & 0o777, 0o700)
            self.assertIn('SECRET-ID', (stage / 'details.json').read_text())
            for p in (root / 'output').iterdir():
                self.assertNotIn('SECRET', p.read_text())
                self.assertNotIn('/paid', p.read_text())
            public = json.loads((root / 'output/epsteinexposed_api_audit.json').read_text())
            self.assertFalse(public['datasets']['persons']['advertised_matches_actual'])
            self.assertEqual(public['spec']['premium_flagged'], 1)


if __name__ == '__main__':
    unittest.main()
