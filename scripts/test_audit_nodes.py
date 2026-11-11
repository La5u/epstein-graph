import base64
import contextlib
import gzip
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import audit_nodes as audit
import build


class AuditTests(unittest.TestCase):
    def nodes(self, rows):
        return [dict(id=i, label='Participant_' + str(i), real=label, type=kind, sub=None, deg=1)
                for i, (label, kind) in enumerate(rows)]

    def test_every_section_has_a_positive_fixture(self):
        nodes = self.nodes([
            ('Jane Roe', 'person'), ('Roe Jane', 'participant'), ('Jane M Roe', 'person'),
            ('Jane Maria Roe', 'person'), ('Jane Rne', 'person'), ('JaneRoe', 'person'),
            ('Jane Roe', 'organization'), ('Jane Roel', 'person'), ('Jane Roei', 'person'),
            ('A Very Long Organization With Seven Words', 'organization'), ('image001.png', 'organization'),
            ('jane@example.com', 'person'), ('jane®gmail.com', 'person'), ('gmaii.com', 'website'),
            ('abcdef0123456789abcdef@example.com', 'contact'), ('Mr. Jane Roe', 'person'),
            ('jane Roe', 'person'), ('+1 212 555 0134', 'contact'), ('(212) 555-0134', 'contact'),
            ('Eastern Time', 'place'),
        ])
        nodes[0]['deg'] = 0
        sections = audit.audit(nodes)
        self.assertEqual(set(sections), set(audit.SECTIONS))
        for name, section in sections.items():
            self.assertGreater(section['count'], 0, name)
            self.assertLessEqual(len(section['examples']), 20)
        self.assertEqual(sections['isolated_nodes_by_type']['by_type'], {'person': 1})
        public = json.dumps(audit.report(nodes, sections))
        self.assertNotIn('Jane', public)
        self.assertNotIn('example.com', public)
        self.assertIn('Jane', json.dumps(audit.report(nodes, sections, private=True)))

    def test_guards_and_candidate_pair_count(self):
        nodes = self.nodes([('Jane Roe', 'person'), ('Janet Roe', 'person'), ('Jane Brown', 'person'),
                            ('Jane Anne Roe', 'person'), ('Jane Alice Roe', 'person'),
                            ('12 Really Very Long Named Street City', 'place'),
                            ('gmail.com', 'website'), ('john@example.com', 'contact')])
        sections = audit.audit(nodes)
        self.assertEqual(sections['one_ocr_edit_same_first_name']['count'], 0)
        self.assertEqual(sections['middle_name_initial_subsets']['count'], 2)
        self.assertEqual(sections['long_labels_by_type']['count'], 0)
        self.assertEqual(sections['invalid_websites']['count'], 0)
        self.assertEqual(sections['message_content_id_contacts']['count'], 0)
        for a, b, result in [('roe', 'roe', False), ('roe', 'roel', True), ('roe', 'rne', True), ('roe', 'rxx', False)]:
            self.assertEqual(audit.one_edit(a, b), result)
        for value in ('drive.goo', 'income.com', 'ab.com', 'foo..com', 'thing.con'):
            self.assertIsNotNone(audit.website_reason(value), value)
        for value in ('https://example.com/path#fragment', 'yahoo.com', 'example.org'):
            self.assertIsNone(audit.website_reason(value), value)
        self.assertTrue(audit.message_id('image001.png@01ABCDEF.local'))
        self.assertEqual(audit.website_reason('sending.com', {'sending'}), 'fragment_or_common_word_domain')
        self.assertIsNone(audit.website_reason('https://sending.com', {'sending'}))

    def test_examples_are_bounded_and_phone_groups_count_once(self):
        nodes = self.nodes([('lower case ' + str(i), 'person') for i in range(40)])
        result = audit.audit(nodes)['lowercase_punct_person_labels']
        self.assertEqual(result['count'], 40)
        self.assertEqual(len(result['examples']), 20)
        nodes = self.nodes([('Jane Roe', 'person'), ('Jane Roel', 'person'), ('Jane Rne', 'person')])
        self.assertEqual(audit.audit(nodes)['one_ocr_edit_same_first_name']['count'], 2)

    def viewer(self, root, encrypted=True):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        payload = {'nodes': [{'id': 'participant_1', 'label': 'Participant_1', 'record': {'entity_type': 'person'}},
                             {'id': 'email_1', 'label': 'Email_1', 'record': {'entity_type': 'contact'}}],
                   'edges': [{'from': 'participant_1', 'to': 'email_1'}]}
        encode = lambda value: base64.b64encode(value).decode()
        secret = None
        if encrypted:
            salt, iv = b'0123456789abcdef', b'0123456789ab'
            key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=10).derive(b'fixture password')
            raw = gzip.compress(json.dumps({'reveal': {'participant_1': {'value': 'jane Private'}}}).encode())
            secret = dict(salt=encode(salt), iv=encode(iv), iterations=10, compression='gzip', data=encode(AESGCM(key).encrypt(iv, raw, None)))
        html = root / 'output/graph.html'
        html.parent.mkdir(exist_ok=True)
        html.write_text('<script type="application/octet-stream" id="viewer-data">' + encode(gzip.compress(json.dumps(payload).encode()))
                        + '</script><script>const SECRET = ' + json.dumps(secret) + '; const PUBLIC_REVEAL = {"email_1":"john@example.com"};</script>')
        return html

    def test_public_hmac_label_is_not_a_real_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            html = self.viewer(root)
            html.write_text(html.read_text().replace('john@example.com', 'email:123456abcdef'))
            nodes, decrypted, warnings = audit.read_viewer(html, root / 'absent')
            self.assertFalse(decrypted)
            self.assertIsNone(nodes[1]['real'])

    def test_decode_decrypt_and_private_report_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            html = self.viewer(root)
            private = root / 'data/private'
            private.mkdir(parents=True)
            password = private / 'viewer_password'
            # No password: decrypt is never attempted; public contacts still work.
            nodes, decrypted, warnings = audit.read_viewer(html, password)
            self.assertFalse(decrypted)
            self.assertEqual(nodes[1]['real'], 'john@example.com')
            self.assertEqual([n['deg'] for n in nodes], [1, 1])
            password.write_text('fixture password')
            findings = private / 'audit/full_read/all_findings.json'
            findings.parent.mkdir(parents=True)
            findings.write_text(json.dumps([dict(ids=['participant_1'], labels=['jane Private'],
                **{'class': 'leading_noise'}, fix='hide', confidence='high')]))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(audit.main([], root=root), 0)
            public = (root / 'output/node_audit.json').read_text()
            self.assertNotIn('jane Private', public)
            self.assertEqual(json.loads(public)['evaluation']['count'], 1)
            self.assertNotIn('participant_1', public)
            report = private / 'audit/node_audit.json'
            self.assertIn('jane Private', report.read_text())
            self.assertEqual(len(json.loads(report.read_text())['evaluation']['results']), 1)
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)
            password.unlink()
            with contextlib.redirect_stdout(io.StringIO()):
                audit.main([], root=root)
            self.assertFalse(report.exists())
            password.write_text('wrong password')
            self.assertTrue(audit.read_viewer(html, password)[2])

    def test_missing_viewer_still_reports_and_returns_zero(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            self.assertEqual(audit.main([], root=root), 0)
            report = json.loads((root / 'output/node_audit.json').read_text())
            self.assertTrue(report['warnings'])
            self.assertEqual(len(report['sections']), len(audit.SECTIONS))

    def test_write_failure_never_rejects_build(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            with patch.object(audit, 'write_report', side_effect=OSError('not writable')):
                self.assertEqual(audit.main([], root=root), 0)

    def test_build_registration_is_terminal(self):
        steps = {step.name: step for step in build.steps()}
        self.assertIn('visualize_graph', steps['audit_nodes'].after)
        self.assertTrue(steps['audit_nodes'].traced)
        self.assertFalse(any('audit_nodes' in step.after for step in steps.values()))

    def test_evaluation_matches_ids_members_and_labels_conservatively(self):
        nodes = self.nodes([('Jane Roe', 'person'), ('Some Place', 'place'),
                            ('Example Corporation', 'organization'), ('junk', 'person')])
        nodes[0]['rec'] = {'merged_identities': [{'id': 'old_jane', 'value': 'Jane Rne'}]}
        nodes[1]['rec'] = {'aliases': ['SomePlace']}
        nodes[3]['rec'] = {'label_quality': 'not_a_name'}
        findings = [
            dict(ids=['old_jane', '0'], labels=['Jane Rne', 'Jane Roe'], **{'class': 'duplicate'}, fix='merge', confidence='high'),
            dict(ids=['old_place'], labels=['SomePlace'], **{'class': 'wrong_type_place'}, fix='retype to place', confidence='high'),
            dict(ids=['2'], labels=['Esq Example Corporation'], **{'class': 'leading_noise'}, fix='strip Esq', confidence='high'),
            dict(ids=['3'], labels=['junk'], **{'class': 'junk'}, fix='hide', confidence='low'),
            dict(ids=['absent'], labels=['Absent Private Name'], **{'class': 'junk'}, fix='hide', confidence='high'),
            dict(ids=['2'], labels=['Entirely Different'], **{'class': 'label'}, fix='normalize', confidence='high'),
        ]
        baseline = [dict(id='old_place', label='SomePlace', type='participant')]
        scored = audit.evaluation(nodes, findings, baseline)
        self.assertEqual([r['status'] for r in scored['results']],
                         ['resolved', 'resolved', 'resolved', 'resolved', 'unverifiable', 'unresolved'])
        self.assertEqual(scored['results'][-1]['nodes'][0]['status'], 'changed_unverified')
        public = json.dumps(audit.evaluation_report(scored))
        for private in ('Jane', 'Absent Private Name', 'old_jane', 'SomePlace', 'Esq'):
            self.assertNotIn(private, public)
        self.assertEqual(scored['by_confidence']['low']['findings'], {'resolved': 1})

    def test_evaluation_ambiguous_label_and_survivor_guard(self):
        nodes = self.nodes([('Shared Name', 'person'), ('Shared Name', 'person'), ('Jane Roe', 'survivor')])
        nodes[2]['rec'] = {'label_quality': 'not_a_name', 'merged_identities': [{'id': 'survivor_old'}]}
        findings = [dict(ids=['missing'], labels=['Shared Name'], **{'class': 'junk'}, fix='hide'),
                    dict(ids=['2'], labels=['Jane Roe'], **{'class': 'junk'}, fix='hide'),
                    dict(ids=['survivor_old', '2'], labels=['Jane Roe', 'Jane Roe'], **{'class': 'duplicate'}, fix='merge')]
        scored = audit.evaluation(nodes, findings)
        self.assertEqual([r['status'] for r in scored['results']], ['unverifiable', 'unresolved', 'resolved'])
        self.assertTrue(scored['results'][1]['nodes'][0]['protected_survivor'])

    def test_evaluation_truncated_members_use_identity_map_and_good_ids(self):
        nodes = self.nodes([('Jane Roe', 'person')])
        findings = [dict(ids=['old', 'not_real'], good=['old'], labels=['Jane Rne', 'Bad ID'],
                         **{'class': 'duplicate'}, fix='merge', confidence='high')]
        scored = audit.evaluation(nodes, findings, identity_map={'old': {'canonical': 'kg:0'}})
        self.assertEqual(scored['findings'], {'resolved': 1})
        self.assertEqual(len(scored['results'][0]['nodes']), 1)

    def test_evaluation_non_actionable_findings_are_review_only(self):
        nodes = self.nodes([('Jane Roe', 'person')])
        scored = audit.evaluation(nodes, [dict(ids=['0'], labels=['Jane Roe'], **{'class': 'possible_same'},
                                              fix='leave / review', confidence='low')])
        self.assertEqual(scored['findings'], {'review_only': 1})

    def test_evaluation_mixed_fix_scope_is_not_certified_automatically(self):
        nodes = self.nodes([('Jane Roe', 'person')])
        nodes[0]['rec'] = {'label_quality': 'not_a_name'}
        scored = audit.evaluation(nodes, [dict(ids=['0'], labels=['Jane Roe'], **{'class': 'wrong_type'},
                                              fix='retype to place; hide others', confidence='high')])
        self.assertEqual(scored['findings'], {'unresolved': 1})
        self.assertTrue(scored['results'][0]['nodes'][0]['needs_fix_scope_review'])

    def test_evaluation_place_classes_get_their_own_theme(self):
        nodes = self.nodes([('Mumbai', 'place')])
        nodes[0]['rec'] = {'label_quality': 'not_a_name'}
        findings = [dict(ids=['0'], labels=['Cursor'], **{'class': 'place_junk'}, fix='hide', confidence='high'),
                    dict(ids=['x'], labels=['CA 90017'], **{'class': 'zip_code_as_entity'}, fix='hide', confidence='high'),
                    dict(ids=['y'], labels=['JFK'], **{'class': 'duplicate_place'}, fix='merge', confidence='high')]
        scored = audit.evaluation(nodes, findings)
        self.assertEqual(scored['by_theme']['places']['findings'].get('resolved'), 1)
        self.assertEqual(set(scored['by_place_kind']), {'not_a_place', 'zip_code', 'duplicate'})

    def test_corrupt_evaluation_does_not_erase_the_quality_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.viewer(root, encrypted=False)
            path = root / 'data/private/audit/full_read/all_findings.json'
            path.parent.mkdir(parents=True)
            path.write_text('not JSON')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(audit.main([], root=root), 0)
            result = json.loads((root / 'output/node_audit.json').read_text())
            self.assertEqual(result['sections']['lowercase_punct_person_labels']['count'], 0)
            self.assertEqual(result['nodes'], 2)
            self.assertIn('review evaluation unavailable; build continues', result['warnings'])

    def test_lazy_node_detail_chunk_keeps_merge_and_quality_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            html = self.viewer(root, encrypted=False)
            # The startup payload normally contains only hot fields.
            raw = html.read_text()
            chunk = base64.b64encode(gzip.compress(json.dumps([
                {'merged_identities': [{'id': 'old', 'value': 'Private Alias'}], 'label_quality': 'not_a_name',
                 'documents': {'large': ['discard me']}}, {}]).encode())).decode()
            # Rebuild the compact data fixture with detail indexes.
            import re
            match = re.search(r'id="viewer-data">(.*?)</script>', raw)
            payload = json.loads(gzip.decompress(base64.b64decode(match[1])))
            payload['nodes'][0]['detail'] = 0
            raw = raw.replace(match[1], base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode())
            html.write_text(raw + '<script id="detail-0">' + chunk + '</script>')
            nodes, _, _ = audit.read_viewer(html, root / 'no_password')
            self.assertEqual(nodes[0]['rec']['merged_identities'][0]['id'], 'old')
            self.assertEqual(nodes[0]['rec']['label_quality'], 'not_a_name')
            self.assertNotIn('documents', nodes[0]['rec'])
