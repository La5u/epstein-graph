"""Offline: python -m unittest discover -s scripts -p test_api_reconciliation.py"""
import json
from pathlib import Path
import tempfile
import unittest

import reconcile_api_entities as audit


def entity(eid, name, kind='person', **extra):
    return {'id': eid, 'name': name, 'entity_type': kind, **extra}


def upstream(eid, name, **extra):
    return {'id': eid, 'name': name, 'slug': 'slug-' + eid, **extra}


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        self.config = self.root / 'config/survivors.json'
        self.config.write_text('{"names": ["Protected Name"]}')

    def reconcile(self, entities, persons=(), organizations=(), relationships=()):
        return audit.reconcile(entities, relationships,
                               {'persons': list(persons), 'organizations': list(organizations)}, self.config)

    def test_unicode_punctuation_suffixes_and_divisions(self):
        self.assertEqual(audit.name_key('  CAFE\u0301—Straße, Inc. '), 'café strasse inc')
        entities = [entity(1, 'ACME, Inc.', 'organization'),
                    entity(2, 'ACME LLC', 'shell_company'),
                    entity(3, 'Bank', 'organization'),
                    entity(4, 'Bank Private Division', 'organization'),
                    entity(5, 'ACME', 'person')]
        rows = [upstream('a', 'acme inc'), upstream('b', 'acme llc'),
                upstream('c', 'Bank'), upstream('d', 'Bank Private Division'), upstream('e', 'ACME')]
        public, private = self.reconcile(entities, organizations=rows)
        records = private['datasets']['organizations']['export_candidates']
        self.assertEqual([r['status'] for r in records], ['exact_candidate'] * 4 + ['unmatched'])
        self.assertEqual([r['candidates'][0]['normalized_id'] for r in records[:4]], [1, 2, 3, 4])
        self.assertEqual(public['datasets']['organizations']['primary_name_links'], 4)

    def test_aliases_and_ambiguous_both_directions(self):
        entities = [entity(1, 'Alice Example', aliases='["A. Example"]'),
                    entity(2, 'Bob Other', aliases=['Shared']),
                    entity(3, 'Carol Other', original_names=['Shared'])]
        public, private = self.reconcile(entities, [upstream('a', 'A Example'),
            upstream('b', 'Shared'), upstream('c', 'Different', aliases=['A. Example']),
            upstream('d', 'Unknown', aliases=['Example'])])
        records = private['datasets']['persons']['export_candidates']
        self.assertEqual([r['status'] for r in records], ['ambiguous', 'ambiguous', 'ambiguous', 'unmatched'])
        self.assertEqual(len(records[1]['candidates']), 2)
        self.assertEqual(public['datasets']['persons']['export_status_counts']['exact_candidate'], 0)
        _, private = self.reconcile(entities, [upstream('only', 'A. Example')])
        self.assertEqual(private['datasets']['persons']['export_candidates'][0]['status'], 'exact_candidate')

    def test_exclusions_aliases_labels_and_relationships(self):
        entities = [entity(1, 'Protected Name', aliases=['Private Alias']),
                    entity(2, 'Labelled', metadata='{"person_type":"victim"}', aliases=['Label Alias']),
                    entity(3, 'Relationship Name', original_names=['Relationship Alias']),
                    entity(4, 'Safe Person'), entity(5, 'Typed', 'victim', aliases=['Typed Alias'])]
        rows = [upstream('a', 'Private—Alias'), upstream('b', 'Label Alias'),
                upstream('c', 'Relationship Alias'), upstream('d', 'Other', aliases=['Protected Name']),
                upstream('e', 'Upstream Victim', category='victim', aliases=['Export Alias']),
                upstream('f', 'Export Alias'), upstream('g', 'Safe Person', short_bio='SECRET BIO', email='SECRET CONTACT'),
                upstream('h', 'Typed Alias')]
        public, private = self.reconcile(entities, rows, relationships=[
            {'source_entity_id': 3, 'relationship_type': 'victim_of'}])
        self.assertEqual(public['datasets']['persons']['export_status_counts']['excluded'], 7)
        records = private['datasets']['persons']['export_candidates']
        self.assertEqual([r['upstream_id'] for r in records], ['g'])
        text = json.dumps(private)
        for forbidden in ('Private Alias', 'Label Alias', 'Relationship Alias', 'Export Alias', 'SECRET'):
            self.assertNotIn(forbidden, text)
        self.assertNotIn('Safe Person', json.dumps(public))

    def test_missing_exclusions_fail_closed(self):
        self.config.unlink()
        with self.assertRaises(OSError):
            self.reconcile([], [])

    def test_run_read_only_and_private_permissions(self):
        data = self.root / 'data/external/epsteinexposed'
        data.mkdir(parents=True)
        output = self.root / 'output'
        output.mkdir()
        snapshots = {
            output / 'normalized_entities.json': [entity(1, 'Sensitive Name')],
            output / 'normalized_relationships.json': [],
            data / 'persons.json': {'data': [upstream('id-secret', 'Sensitive Name', short_bio='BIO SECRET')]},
            data / 'organizations.json': {'data': []}}
        for path, value in snapshots.items():
            path.write_text(json.dumps(value))
        before = {p: p.read_bytes() for p in [*snapshots, self.config]}
        self.assertEqual(audit.run(self.root), 0)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        private = self.root / 'data/private/epsteinexposed_api_entity_candidates.json'
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
        self.assertIn('Sensitive Name', private.read_text())
        self.assertNotIn('BIO SECRET', private.read_text())
        for suffix in ('json', 'md'):
            text = (output / ('api_entity_coverage.' + suffix)).read_text()
            self.assertNotIn('Sensitive Name', text)
            self.assertNotIn('id-secret', text)
        # Replacement also repairs a pre-existing overly broad candidate mode.
        private.chmod(0o644)
        audit.run(self.root)
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
