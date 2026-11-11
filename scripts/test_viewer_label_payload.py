import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import viewer_secret
import viewer_label_payload
from viewer_label_payload import secret_entries, label_payload, fold_viewer_resolutions, early_retypes, label_signature, reviewed_resolutions, include_reviewed_targets


class ViewerLabelPayloadTests(unittest.TestCase):
    def test_reapplying_label_verdicts_preserves_explicit_identity_folds(self):
        import label_review
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'config').mkdir()
            path = root/'config/label_review.json'
            merges = {'source': {'target': 'target', 'sig': 'old-source-hash', 'target_sig': 'target-hash'}}
            path.write_text(json.dumps({'labels': {}, 'merges': merges}))
            (root/'config/node_review.json').write_text('{}')
            labels = root/'labels.json'
            labels.write_text(json.dumps([{'id': 'source', 'label': 'Example Person', 'type': 'organization'}]))
            answers = {'source': {'label': 'Example Person', 'node_type': 'organization', 'c': 'high',
                                  'v': 'wrong_type', 'type': 'person'}}
            with patch.object(label_review, 'ROOT', root), patch.object(label_review, 'LABELS', labels), \
                    patch.object(label_review, 'final_answers', return_value=answers), \
                    patch.object(viewer_label_payload, 'LABEL_REVIEW', path):
                label_review.apply(SimpleNamespace(model='fixture'))
            written = json.loads(path.read_text())
            self.assertEqual(written['merges'], merges)
            self.assertEqual(written['labels']['source']['v'], 'retype')

    def test_off_slice_target_is_hash_bound_and_runs_through_person_privacy(self):
        import public_names
        from survivor_privacy import scrubber
        source = {'id': 'source', 'name': 'HeaderLabel_abcdef', 'entity_type': 'participant'}
        target = {'id': 'target', 'name': 'Private Fullname', 'entity_type': 'person'}
        review = {'source': {'target': 'target', 'sig': label_signature('Private Fullname Partner'),
                             'target_sig': label_signature(target['name'])}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'graph.json'
            path.write_text(json.dumps([source, target]))
            secret = Path(d)/'secret.json'
            secret.write_text(json.dumps({'reveal': {'source': {'value': 'Private Fullname Partner'}}}))
            with patch.object(viewer_secret, 'SECRET', secret):
                included = include_reviewed_targets([source], path, review)
                self.assertEqual([n['id'] for n in included], ['source', 'target'])
                private, protected, _ = public_names.apply(included, {}, b'key', scrubber)
                self.assertTrue(private[1]['name'].startswith('Person '))
                self.assertEqual(protected['target']['value'], target['name'])
                for change in ({'sig': 'stale'}, {'target_sig': 'stale'}, {'target': 'missing'}):
                    self.assertEqual(include_reviewed_targets([source], path, {'source': {**review['source'], **change}}), [source])
                path.write_text(json.dumps([source, {**target, 'entity_type': 'survivor'}]))
                self.assertEqual(include_reviewed_targets([source], path, review), [source])

    def test_reviewed_folds_keep_private_names_encrypted_and_canonical_policy(self):
        import public_names
        from survivor_privacy import scrubber
        original = [{'id': 'entity_alias', 'name': 'Private Example Partner', 'entity_type': 'organization'},
                    {'id': 'entity_person', 'name': 'Private Example', 'entity_type': 'person'},
                    {'id': 99, 'name': 'Public Example', 'entity_type': 'person', 'metadata': {'public_figure': True}}]
        retypes = {'entity_alias': {'v': 'retype', 'type': 'person', 'sig': label_signature(original[0]['name'])}}
        review = {'entity_alias': {'target': 'entity_person', 'sig': label_signature(original[0]['name']),
                                  'target_sig': label_signature(original[1]['name'])}}
        prepared, protected, scrub = public_names.apply(early_retypes(original, retypes), {}, b'key', scrubber)
        self.assertTrue(prepared[0]['name'].startswith('Person '))
        self.assertTrue(prepared[1]['name'].startswith('Person '))
        for secret in (False, True):
            with tempfile.TemporaryDirectory() as d, patch.object(viewer_secret, 'SECRET', Path(d)/'absent'), \
                    patch.object(viewer_label_payload, 'reviewed_labels', return_value=retypes):
                labels = {}
                safe, _, _ = label_payload(prepared, Path(d)/'graph.html', secret=secret,
                                            protected=protected, labels_out=labels)
            mapping = reviewed_resolutions(safe, labels, review)
            self.assertEqual(mapping, {'entity_alias': ('entity_person', 'reviewed_label_merge')})
            folded, _ = fold_viewer_resolutions(safe, [], mapping, never=[])
            rendered = json.dumps(scrub(folded))
            self.assertNotIn('Private Example', rendered)
            self.assertEqual(folded[0]['name'], prepared[1]['name'])
            self.assertEqual(folded[1]['name'], 'Public Example')
            self.assertEqual(public_names.shown_names(folded, labels, {}), {})
            public_review = {'entity_alias': {**review['entity_alias'], 'target': 99,
                                             'target_sig': label_signature('Public Example')}}
            public_mapping = reviewed_resolutions(safe, labels, public_review)
            folded, _ = fold_viewer_resolutions(safe, [], public_mapping, never=[])
            self.assertEqual(next(n['name'] for n in folded if n['id'] == 99), 'Public Example')
            self.assertNotIn('Private Example', json.dumps(scrub(folded)))

    def test_reviewed_fold_requires_both_current_labels_and_existing_unprotected_target(self):
        nodes = [{'id': 'source', 'name': 'Safe source', 'entity_type': 'organization'},
                 {'id': 'target', 'name': 'Safe target', 'entity_type': 'person'}]
        labels = {'source': 'Real source', 'target': 'Real target'}
        item = {'target': 'target', 'sig': label_signature('Real source'), 'target_sig': label_signature('Real target')}
        self.assertTrue(reviewed_resolutions(nodes, labels, {'source': item}))
        for change in ({'sig': 'stale'}, {'target_sig': 'stale'}, {'target': 'missing'}):
            self.assertEqual(reviewed_resolutions(nodes, labels, {'source': {**item, **change}}), {})
        for index in (0, 1):
            protected = [dict(n) for n in nodes]
            protected[index]['entity_type'] = 'survivor'
            self.assertEqual(reviewed_resolutions(protected, labels, {'source': item}), {})

    def test_folding_guards_survivors_and_transitive_separations_in_merged_members(self):
        nodes = [{'id': 'a', 'name': 'A', 'entity_type': 'organization', 'merged_identities': [{'id': 'kg:41'}]},
                 {'id': 'b', 'name': 'B', 'entity_type': 'person'},
                 {'id': 'c', 'name': 'C', 'entity_type': 'person', 'original_entity_ids': [42]},
                 {'id': 's', 'name': 'S', 'entity_type': 'survivor'}]
        folded, _ = fold_viewer_resolutions(nodes, [], {'a': ('b', 'reviewed'), 'c': ('b', 'reviewed'),
                                                     'b': ('s', 'reviewed')}, never=[(41, 42)])
        self.assertEqual(len(folded), 4)  # chain and survivor destination are rejected
        folded, _ = fold_viewer_resolutions(nodes, [], {'a': ('b', 'reviewed'), 'c': ('b', 'reviewed')}, never=[(41, 42)])
        self.assertEqual({n['id'] for n in folded}, {'b', 'c', 's'})
        self.assertEqual(nodes[1].get('merged_identities'), None)

    def test_unlinked_canonical_organization_is_added_when_a_participant_resolves_to_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'secret.json').write_text(json.dumps({'reveal': {'participant_one': {'value': 'ExampleAir'}}, 'pages': {}, 'reasons': {}}))
            (root/'company_nodes.json').write_text(json.dumps([{'id': 'company_one', 'name': 'ExampleAir LLC', 'entity_type': 'organization'}]))
            resolutions = {}
            with patch.object(viewer_secret, 'SECRET', root/'secret.json'):
                nodes, _, _ = label_payload([{'id': 'participant_one', 'name': 'HeaderLabel_one', 'entity_type': 'person'}],
                    root/'graph.html', secret=False, resolutions=resolutions, reference_out=root)
            self.assertEqual(resolutions, {'participant_one': ('company_one', 'exact_existing_organization')})
            self.assertEqual({n['id'] for n in nodes}, {'participant_one', 'company_one'})
            self.assertEqual(nodes[0]['name'], 'HeaderLabel_one')

    def test_exact_private_brand_folds_without_revealing_raw_label_and_keeps_edges(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            table = root/'secret.json'
            table.write_text(json.dumps({'reveal': {'participant_a': {'value': 'ExampleAir'}}, 'pages': {}, 'reasons': {}}))
            nodes = [{'id': 'participant_a', 'name': 'HeaderLabel_a', 'entity_type': 'person'},
                     {'id': 1, 'name': 'ExampleAir', 'entity_type': 'organization'},
                     {'id': 2, 'name': 'Another', 'entity_type': 'person'},
                     {'id': 3, 'name': 'ExampleAir', 'entity_type': 'survivor'}]
            resolutions = {}
            with patch.object(viewer_secret, 'SECRET', table):
                prepared, _, _ = label_payload(nodes, root/'graph.html', secret=False, resolutions=resolutions)
            self.assertEqual(resolutions, {'participant_a': (1, 'exact_existing_organization')})
            self.assertEqual(prepared[0]['name'], 'HeaderLabel_a')
            relationships = [{'id': 'a', 'source_entity_id': 'participant_a', 'target_entity_id': 2},
                             {'id': 'b', 'source_entity_id': 'participant_a', 'target_entity_id': 1}]
            folded, edges = fold_viewer_resolutions(prepared, relationships, resolutions)
            self.assertEqual({n['id'] for n in folded}, {1, 2, 3})
            self.assertEqual(edges[0]['source_entity_id'], 1)
            self.assertEqual(edges[0]['original_source_entity_id'], 'participant_a')
            self.assertEqual(len(edges), 1)
            self.assertEqual(folded[0]['merged_identities'][0]['value'], 'HeaderLabel_a')
            self.assertEqual(relationships[0]['source_entity_id'], 'participant_a')

    def test_public_labels_are_cleaned_without_private_table(self):
        with tempfile.TemporaryDirectory() as d, patch.object(viewer_secret, 'SECRET', Path(d)/'absent'):
            nodes, encrypted, contacts = label_payload([
                {'id': 1, 'name': 'Statement.pdf', 'entity_type': 'organization'},
                {'id': 2, 'name': '100 Example Road [signature: logo.png]', 'entity_type': 'place'}], Path(d)/'graph.html')
            self.assertEqual(nodes[0]['label_quality_reason'], 'file_name')
            self.assertEqual(nodes[1]['name'], '100 Example Road')
            self.assertIsNone(encrypted)
            self.assertEqual(contacts, {})

    def test_reviewed_labels_hide_or_retype_only_while_the_label_is_unchanged(self):
        sig = label_signature
        review = {'1': {'v': 'not_a_name', 'sig': sig('Arrival')}, '2': {'v': 'not_a_name', 'sig': sig('old spelling')},
                  '3': {'v': 'retype', 'type': 'place', 'sig': sig('Doha Qatar')},
                  '4': {'v': 'retype', 'type': 'person', 'sig': sig('Example Person')},
                  '5': {'v': 'retype', 'type': 'organization', 'sig': sig('Example Desk')}}
        with tempfile.TemporaryDirectory() as d, patch.object(viewer_secret, 'SECRET', Path(d)/'absent'), \
                patch.object(viewer_label_payload, 'reviewed_labels', lambda: review):
            nodes, _, _ = label_payload([
                {'id': 1, 'name': 'Arrival', 'entity_type': 'person'},
                {'id': 2, 'name': 'Arrival Hall', 'entity_type': 'organization'},
                {'id': 3, 'name': 'Doha Qatar', 'entity_type': 'person'},
                {'id': 4, 'name': 'Example Person', 'entity_type': 'organization'},
                {'id': 5, 'name': 'Example Desk', 'entity_type': 'participant'}], Path(d)/'graph.html')
        by_id = {n['id']: n for n in nodes}
        self.assertEqual(by_id[1]['label_quality'], 'not_a_name')
        self.assertNotIn('label_quality', by_id[2])  # label changed since the review
        self.assertEqual(by_id[3]['entity_type'], 'place')
        self.assertEqual(by_id[4]['entity_type'], 'organization')  # to-person happens before public names, not here
        self.assertEqual(by_id[5]['entity_type'], 'participant')  # participants are never retyped
        early = early_retypes([{'id': 4, 'name': 'Example Person', 'entity_type': 'organization'},
                               {'id': 3, 'name': 'Doha Qatar', 'entity_type': 'person'}], review)
        self.assertEqual([e['entity_type'] for e in early], ['person', 'person'])

    def test_private_machine_formats_retype_and_mail_fragments_hide_without_revealing_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {'participant_url': 'www.example.com', 'participant_fragment': 'ail.co', 'participant_aircraft': 'G550'}
            (root/'secret.json').write_text(json.dumps({'reveal': {i: {'value': v} for i, v in values.items()}, 'pages': {}, 'reasons': {}}))
            with patch.object(viewer_secret, 'SECRET', root/'secret.json'):
                nodes, _, _ = label_payload([{'id': i, 'name': 'HeaderLabel_' + i, 'entity_type': 'person'} for i in values],
                    root/'graph.html', secret=False, resolutions={})
            by_id = {n['id']: n for n in nodes}
            self.assertEqual(by_id['participant_url']['entity_type'], 'website')
            self.assertEqual(by_id['participant_fragment']['label_quality_reason'], 'mail_provider_fragment')
            self.assertEqual(by_id['participant_aircraft']['entity_type'], 'vehicle')
            self.assertNotIn('www.example.com', json.dumps(nodes))

    def test_stream_handles_unicode_escapes_large_entries_and_chunk_boundaries(self):
        value = {'reveal': {'participant_1': {'value': 'Émile "Alpha"' * 1000, 'possible_duplicates': []},
                            'participant_2': {'value': 'Beta'}},
                 'pages': {'participant_1': 'node_docs/one.html'}, 'reasons': {'participant_1': 'reviewed'}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'secret.json'
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2))
            for chunk in (1, 7, 64, 65536):
                self.assertEqual(list(secret_entries(path, chunk)),
                                 [(table, key, item) for table, rows in value.items() for key, item in rows.items()])
            path.write_text('{"reveal":{"a":')
            with self.assertRaises(ValueError):
                list(secret_entries(path, 3))

    def test_private_cleanup_is_encrypted_and_public_flags_do_not_reveal_labels(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            table = root/'secret.json'
            password = root/'password'
            password.write_text('fixture-password-long-enough')
            table.write_text(json.dumps({'reveal': {
                'participant_file': {'value': 'Statement.pdf'},
                'address_one': {'value': '1755 N Brown Rd [signature: image001.png]'},
                'email_one': {'value': 'test@example.com'},
                'participant_absent': {'value': 'Private absent label'}},
                'pages': {'address_one': 'node_docs/one.html', 'participant_absent': 'absent.html'},
                'reasons': {'participant_file': 'private review', 'participant_absent': 'absent reason'}}))
            entities = [{'id': 'participant_file', 'name': 'HeaderLabel_opaque', 'entity_type': 'participant'},
                        {'id': 'address_one', 'name': 'Address_opaque', 'entity_type': 'place'},
                        {'id': 'email_one', 'name': 'Email_opaque', 'entity_type': 'contact'}]
            with patch.object(viewer_secret, 'SECRET', table), patch.object(viewer_secret, 'PASSWORD', password), \
                 patch.object(viewer_secret, 'PRIVATE', root), patch.object(viewer_secret, 'encrypt', return_value={'v': 1}) as encrypt:
                nodes, encrypted, contacts = label_payload(entities, root/'graph.html')
                self.assertEqual(nodes[0]['label_quality_reason'], 'file_name')
                self.assertNotIn('Statement.pdf', json.dumps(nodes))
                self.assertEqual(nodes[1]['name'], 'Address_opaque')
                payload = json.loads(gzip.decompress(encrypt.call_args.args[0]))
                self.assertEqual(payload['reveal']['address_one']['value'], '1755 N Brown Rd')
                self.assertNotIn('participant_absent', payload['reveal'])
                self.assertNotIn('participant_absent', payload['pages'])
                self.assertNotIn('participant_absent', payload['reasons'])
                self.assertEqual(encrypted, {'v': 1, 'compression': 'gzip'})
                self.assertEqual(contacts, {'email_one': 'test@example.com'})
                encrypt.reset_mock()
                nodes, encrypted, contacts = label_payload(entities, root/'graph.html', secret=False)
                self.assertIsNone(encrypted)
                encrypt.assert_not_called()
                self.assertNotIn('Statement.pdf', json.dumps(nodes))
                self.assertEqual(nodes[0]['label_quality'], 'not_a_name')
                self.assertEqual(contacts, {'email_one': 'test@example.com'})


if __name__ == '__main__':
    unittest.main()
