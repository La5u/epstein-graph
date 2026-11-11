import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build_contacts
from build_contacts import build, contact_id, normalize_contact


class ContactTests(unittest.TestCase):
    def test_normalization_is_conservative(self):
        self.assertEqual(normalize_contact('Case@EXAMPLE.TEST', 'email'), 'Case@example.test')
        self.assertNotEqual(normalize_contact('Case@example.test', 'email'), normalize_contact('case@example.test', 'email'))
        self.assertEqual(normalize_contact('+1 (202) 555-0100', 'phone'), '+12025550100')
        self.assertIsNone(normalize_contact('555-0100 ext 2', 'phone'))
        self.assertIsNone(normalize_contact('123', 'phone'))
        self.assertEqual(contact_id('a', 'email', b'key'), contact_id('a', 'email', b'key'))
        self.assertNotEqual(contact_id('a', 'email', b'key'), contact_id('a', 'email', b'other'))

    def test_evidence_links_and_no_raw_values(self):
        entities = [{'id': 1, 'entity_type': 'person', 'normalized_name': 'example person'},
                    {'id': 2, 'entity_type': 'person', 'normalized_name': 'example other'},
                    {'id': 3, 'entity_type': 'person', 'normalized_name': 'surname'}]
        def row(value, refs):
            return dict(entity_value=value, efta_numbers=refs, document_count=2)
        export = dict(names=[row('EXAMPLE PERSON', ['EFTA001']), row('Example Other', ['EFTA001']), row('Surname', ['EFTA002'])], organizations=[],
                      emails=[row('sensitive@example.test', ['EFTA001']), row('unlinked@example.test', ['EFTA002'])],
                      phones=[row('+1 (202) 555-0100', ['EFTA001']), row('+12025550100', ['EFTA001'])])
        all_nodes, nodes, edges, summary = build(entities, export, b'secret')
        self.assertEqual(len(all_nodes), 3)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(len(edges), 4)
        self.assertEqual(summary['linked_contacts_shown'], 2)
        self.assertTrue(all(e['relationship_type'] == 'shares_exported_efta_reference' for e in edges))
        self.assertTrue(all(e['weight'] == 1 for e in edges))
        self.assertTrue(all(e['source_entity_id'] != 3 for e in edges))
        serialized = json.dumps([all_nodes, nodes, edges, summary])
        for raw in ['sensitive@example.test', 'unlinked@example.test', '+12025550100', '555-0100']:
            self.assertNotIn(raw, serialized)


class ContactFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch.object(build_contacts, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def put(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value))
        return target

    def pin(self, key):
        self.put('config/contact_key_fingerprint.json', {'sha256': hashlib.sha256(key).hexdigest()})

    def test_fresh_key_is_stable_and_private(self):
        key = build_contacts.contact_key()
        self.assertEqual(len(key), 32)
        self.assertEqual(build_contacts.contact_key(), key)
        self.assertEqual((self.root / 'data/private/contact_hmac.key').stat().st_mode & 0o777, 0o600)
        self.pin(key)
        self.assertEqual(build_contacts.contact_key(), key)

    def test_missing_pinned_key_fails_before_generation(self):
        self.pin(b'original secret')
        with patch.object(build_contacts, 'normalize_main') as normalize:
            with self.assertRaisesRegex(ValueError, 'key is missing'):
                build_contacts.main()
            normalize.assert_not_called()
        self.assertFalse((self.root / 'data').exists())
        self.assertFalse((self.root / 'output').exists())

    def test_mismatch_has_no_secret_values(self):
        key = build_contacts.contact_key()
        other = b'other private secret'
        self.pin(other)
        with self.assertRaisesRegex(ValueError, 'fingerprint mismatch') as raised:
            build_contacts.contact_key()
        for secret in (key.hex(), other.decode(), hashlib.sha256(key).hexdigest(), hashlib.sha256(other).hexdigest()):
            self.assertNotIn(secret, str(raised.exception))
        self.assertEqual((self.root / 'data/private/contact_hmac.key').read_bytes(), key)

    def test_invalid_pin_cannot_initialize_key(self):
        self.put('config/contact_key_fingerprint.json', {'sha256': 'invalid'})
        with self.assertRaisesRegex(ValueError, 'Invalid sha256'):
            build_contacts.contact_key()
        self.assertFalse((self.root / 'data').exists())

    def test_unpinned_pseudonymous_reviews_prevent_silent_rekey(self):
        for name, review in [('identity_merges.json', {'merges': [{'members': ['email_old']}]}),
                             ('node_review.json', {'participants': {'participant_old': {}}})]:
            with self.subTest(name=name):
                path = self.put('config/' + name, review)
                with self.assertRaisesRegex(ValueError, 'pseudonymous reviews exist'):
                    build_contacts.contact_key()
                self.assertFalse((self.root / 'data').exists())
                path.unlink()
        self.put('config/identity_merges.json', {'merges': [{'members': [1], 'canonical': 2}]})
        self.assertEqual(len(build_contacts.contact_key()), 32)

    def test_missing_optional_input_replaces_stale_artifacts(self):
        for name in ('email_place_nodes.json', 'email_place_edges.json'):
            self.put('output/' + name, [{'id': 'stale'}])
        self.assertFalse(build_contacts.email_places_available())
        for name in ('email_place_nodes.json', 'email_place_edges.json'):
            self.assertEqual(json.loads((self.root / 'output' / name).read_text()), [])
        self.put('data/private/email_places.db', {})
        path = self.put('output/email_place_nodes.json', [{'id': 'current'}])
        self.assertTrue(build_contacts.email_places_available())
        self.assertEqual(json.loads(path.read_text()), [{'id': 'current'}])

    def test_assembly_gates_optional_input_even_if_stale_outputs_reappear(self):
        self.put('output/normalized_entities.json', [{'id': 1, 'entity_type': 'person', 'normalized_name': 'example person'}])
        self.put('output/normalized_relationships.json', [])
        self.put('data/raw/extracted_entities_filtered.json', dict(names=[], organizations=[], emails=[], phones=[]))
        self.put('output/email_place_nodes.json', [{'id': 'stale'}])
        self.put('output/email_place_edges.json', [{'source_entity_id': 1, 'target_entity_id': 1}])
        with patch.object(build_contacts, 'email_places_available', return_value=False), \
                patch.object(build_contacts, 'normalize_main'), \
                patch.object(build_contacts, 'load_annotations'), patch('builtins.print'):
            build_contacts.main()
        self.assertEqual(json.loads((self.root / 'output/graph_relationships.json').read_text()), [])
        self.assertEqual([n['id'] for n in json.loads((self.root / 'output/graph_entities.json').read_text())], [1])

    def test_apply_victim_type_reclassifies_and_adds_extra(self):
        self.put('config/victims.json', {'names': ['Known Victim'],
                                         'extra_nodes': [{'name': 'Extra Victim', 'efta': 'EFTA9'}]})
        self.put('config/survivors.json', {'names': []})
        self.put('data/raw/persons_registry.json', [])
        nodes = {1: {'id': 1, 'name': 'Known Victim', 'entity_type': 'person',
                     'suspected_parse_artifact': True, 'label_quality': 'group_or_role'}}
        build_contacts.apply_victim_type(nodes)
        self.assertEqual(nodes[1]['entity_type'], 'survivor')
        self.assertEqual(nodes[1]['original_entity_type'], 'person')
        self.assertNotIn('suspected_parse_artifact', nodes[1])
        self.assertNotIn('label_quality', nodes[1])
        extras = [n for n in nodes.values() if n.get('added_by') == 'config/victims.json']
        self.assertEqual([n['name'] for n in extras], ['Extra Victim'])

    def test_legacy_review_rule_is_not_exported_even_on_existing_merge(self):
        secret = 'private@example.test +1-202-555-0199'
        path = self.put('output/identity_map.json', {
            'email_old': {'canonical': 1, 'rule': 'reviewed: ' + secret, 'confidence': 'confirmed'}})
        for merged in ([], [{'id': 'email_old', 'rule': 'reviewed: ' + secret}]):
            nodes, _ = build_contacts.apply_identity_map({1: {'merged_identities': merged}, 'email_old': {}}, [], path)
            self.assertNotIn(secret, json.dumps(nodes))
            self.assertEqual(nodes[1]['merged_identities'][0]['rule'], 'reviewed_identity_merge')
            self.assertNotIn('email_old', nodes)


if __name__ == '__main__':
    unittest.main()
