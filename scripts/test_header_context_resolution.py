import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import build_communications as comm
from header_context_resolution import ContextNames, compound_people, decorated_name, ocr_cost, quoted_handle_documents, sender_signature


class ContextNameTests(unittest.TestCase):
    def test_visual_spellings_need_both_source_headers_and_a_stronger_reference(self):
        names = {'a': 'Zelda Hargraye', 'kg:1': 'Zelda Hargrave'}
        rows = [(i, 'peer', 'sent_to', 'D1') for i in names]
        c = self.context(names, rows)
        self.assertIsNone(c.propose('a'))
        c.attest_spellings(lambda doc: 'From: Zelda Hargraye\nSubject: Update\nFrom: Zelda Hargrave\nSubject: RE: Update')
        self.assertEqual(c.propose('a'), ('kg:1', 'header_context:header_spelling'))
        c = self.context(names, rows)
        c.attest_spellings(lambda doc: 'From: Sender Other\nSubject: Update\nZelda Hargraye and Zelda Hargrave')
        self.assertIsNone(c.propose('a'))
        self.assertFalse(c.visual_spelling(('Eric', 'Toth'), ('Eric', 'Roth')))
        self.assertFalse(c.visual_spelling(('Zelda', 'Green'), ('Zelda', 'Greene')))
        self.assertFalse(c.visual_spelling(('Zelda', 'A', 'Hargrave'), ('Zelda', 'B', 'Hargrave')))

    def test_source_signature_recovers_a_left_cut_given_name(self):
        c = self.context({'a': 'ichard Hargrave', 'kg:1': 'Richard Hargrave'}, [('a', 'peer', 'sent_to', 'D1')])
        self.assertIsNone(c.propose('a'))
        text = 'From: ichard Hargrave\nTo: Other Person\nSubject: Update\nAll done.\nThanks,\nRichard Hargrave\n'
        c.attest_spellings(lambda doc: text)
        self.assertEqual(c.propose('a'), ('kg:1', 'header_context:sender_signature'))
        self.assertFalse(sender_signature(text.replace('Thanks,', 'I spoke to'), 'ichard Hargrave', 'Richard Hargrave'))
        self.assertFalse(sender_signature(text.replace('Thanks,', 'From: Different Sender\nSubject: Update\nThanks,'),
                                          'ichard Hargrave', 'Richard Hargrave'))
        self.assertFalse(sender_signature(text.replace('Thanks,', 'On Oct 6, 2015, at 10:17 AM, Other Person wrote:\nThanks,'),
                                          'ichard Hargrave', 'Richard Hargrave'))

    def test_signature_does_not_override_a_conflicting_mailbox(self):
        names = {'a': 'ichard Hargrave', 'kg:1': 'Richard Hargrave'}
        c = self.context(names, [('a', 'email_a', 'header_name_association', 'D1'),
                                 ('kg:1', 'email_b', 'header_name_association', 'D2')])
        c.attest_spellings(lambda doc: 'From: ichard Hargrave\nSubject: Update\nThanks,\nRichard Hargrave\n')
        self.assertIsNone(c.propose('a'))

    def test_credentials_and_glued_fields_keep_generations_and_legal_forms(self):
        for suffix in ('MD', 'M.D.', 'MD PhD', 'CPA', 'Esq.'):
            self.assertEqual(decorated_name('Zelda Hargrave ' + suffix), ('Zelda Hargrave', 'credential_tail'))
            c = self.context({'a': 'Zelda Hargrave ' + suffix, 'kg:1': 'Zelda Hargrave'})
            self.assertEqual(c.propose('a'), ('kg:1', 'header_context:credential_tail'))
        self.assertEqual(decorated_name('Zelda HargraveSubject'), ('Zelda Hargrave', 'header_field_tail'))
        for suffix in ('Jr.', 'III', 'PC', 'PA', 'LLC'):
            self.assertEqual(decorated_name('Zelda Hargrave ' + suffix), ('Zelda Hargrave ' + suffix, None))

    def context(self, names, rows=(), aliases=None, review=None, mailbox_values=None):
        db = self.enterContext(contextlib.closing(sqlite3.connect(':memory:')))
        db.execute('CREATE TABLE evidence(source_id,target_id,relationship_type,efta_number)')
        db.executemany('INSERT INTO evidence VALUES (?,?,?,?)', rows)
        final = lambda i: (aliases or {}).get(i, i)
        return ContextNames(db, names, Counter(dict.fromkeys(names, 20)), review or {}, final,
                            comm.header_name_words, comm.nickname_root, comm.label_quality,
                            comm.FUNCTION_WORDS | comm.ROLE_TOKENS | comm.COMPANY_WORDS, mailbox_values=mailbox_values)

    def test_spelling_and_hub_alone_are_not_evidence(self):
        names = {'a': 'Zelda Har', 'b': 'Zelda Hargrave'}
        rows = [('a', 'hub', 'sent_to', None), ('b', 'hub', 'sent_to', None)]
        rows += [('hub', f'other{n}', 'sent_to', None) for n in range(102)]
        c = self.context(names, rows)
        self.assertIn('hub', c.hubs)
        self.assertIsNone(c.propose('a'))
        self.assertIsNone(self.context(names).propose('a'))

    def test_partial_surname_handle_and_initials_share_a_mailbox(self):
        for label in ('Zelda', 'Hargrave', 'zhargrave', 'Zelda Har', 'Zelda H.', 'ZMH'):
            with self.subTest(label=label):
                c = self.context({'a': label, 'b': 'Zelda Marian Hargrave'}, [
                    ('a', 'email_x', 'header_name_association', 'D1'),
                    ('b', 'email_x', 'header_label_association', 'D2')])
                self.assertEqual(c.propose('a'), ('b', 'header_context:mailbox'))

    def test_competing_compatible_mailbox_owners_stay_separate(self):
        c = self.context({'a': 'Zelda', 'b': 'Zelda Hargrave', 'c': 'Zelda Other'}, [
            (i, 'email_x', 'header_name_association', 'D') for i in ('a', 'b', 'c')])
        self.assertIsNone(c.propose('a'))

    def test_wrong_name_on_a_hub_mailbox_is_not_ownership_evidence(self):
        names = {'a': 'Zelda', 'b': 'Zelda Hargrave', 'hub': 'Other Owner'}
        rows = [('a', 'email_x', 'header_name_association', 'D1'),
                ('b', 'email_x', 'header_name_association', 'D2')]
        rows += [('hub', 'email_x', 'header_name_association', 'D3')] * 50
        self.assertIsNone(self.context(names, rows).propose('a'))

    def test_established_owner_can_attest_a_second_physical_mailbox(self):
        # Physical mailbox keys remain distinct; the owner is separate evidence.
        c = self.context({'a': 'Zelda Har', 'b': 'Zelda Hargrave'}, [
            ('a', 'email_a', 'header_name_association', 'D1'),
            ('b', 'email_b', 'header_name_association', 'D2')], aliases={'email_a': 'b', 'email_b': 'b'})
        self.assertEqual(set(c.addresses['a']), {'email_a'})
        self.assertEqual(set(c.addresses['b']), {'email_b'})
        self.assertEqual(c.propose('a'), ('b', 'header_context:mailbox'))

    def test_reviewed_mailbox_owner_does_not_need_a_second_display_name_association(self):
        c = self.context({'a': 'Zelda Har', 'b': 'Zelda Hargrave'}, [
            ('a', 'email_a', 'header_name_association', 'D1')], aliases={'email_a': 'b'})
        self.assertEqual(c.propose('a'), ('b', 'header_context:mailbox'))

    def test_conflicting_mailboxes_outweigh_shared_documents_and_correspondents(self):
        rows = [('a', 'email_a', 'header_name_association', 'D'),
                ('b', 'email_b', 'header_name_association', 'D')]
        rows += [(i, p, 'sent_to', 'D') for i in ('a', 'b') for p in ('p', 'q', 'r')]
        self.assertIsNone(self.context({'a': 'Zelda Har', 'b': 'Zelda Hargrave'}, rows).propose('a'))

    def test_multiple_mailboxes_must_mostly_agree(self):
        rows = [('a', 'email_x', 'header_name_association', 'D')]
        rows += [('a', 'email_y', 'header_name_association', 'D')] * 2
        rows += [('b', 'email_x', 'header_name_association', 'D')]
        self.assertIsNone(self.context({'a': 'Zelda Har', 'b': 'Zelda Hargrave'}, rows).propose('a'))

    def test_same_short_document_is_enough_for_a_truncated_full_name(self):
        c = self.context({'a': 'Zelda Har', 'b': 'Zelda Hargrave'}, [
            ('a', 'hub', 'sent_to', 'D'), ('b', 'hub', 'sent_to', 'D')])
        self.assertEqual(c.propose('a'), ('b', 'header_context:document'))

    def test_distinct_full_names_in_one_recipient_list_are_not_duplicates(self):
        db = self.enterContext(contextlib.closing(sqlite3.connect(':memory:')))
        db.execute('CREATE TABLE evidence(source_id,target_id,relationship_type,efta_number,source_db,source_table,source_record_id,message_index)')
        db.executemany('INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?)', [
            ('sender', 'a', 'sent_to', 'D1', 'headers', 'messages', 1, 0),
            ('sender', 'b', 'sent_to', 'D1', 'headers', 'messages', 1, 0)])
        c = ContextNames(db, {'a': 'Sam Hargrave', 'b': 'Samuel Hargrave'}, Counter(a=20, b=20), {},
                         lambda i: i, comm.header_name_words, comm.nickname_root, comm.label_quality)
        self.assertIsNone(c.propose('a'))
        for names in ({'a': 'Zelda Hargrave', 'b': 'Zelda Hargraves'},
                      {'a': 'Zelda Hargrave', 'b': 'Zelda Marian Hargrave'}):
            c = ContextNames(db, names, Counter(a=20, b=20), {}, lambda i: i,
                             comm.header_name_words, comm.nickname_root, comm.label_quality)
            self.assertIsNone(c.propose('a'))

    def test_one_document_does_not_globally_assign_a_common_first_name(self):
        rows = [('a', 'hub', 'sent_to', 'D1'), ('b', 'hub', 'sent_to', 'D1'),
                ('a', 'hub', 'sent_to', 'D2')]
        self.assertIsNone(self.context({'a': 'Zelda', 'b': 'Zelda Hargrave', 'c': 'Zelda Other'}, rows).propose('a'))

    def test_one_globally_unique_bare_name_can_use_a_document(self):
        c = self.context({'a': 'Zelda', 'b': 'Zelda Hargrave'}, [
            ('a', 'peer', 'sent_to', 'D1'), ('b', 'peer', 'sent_to', 'D1')])
        self.assertEqual(c.propose('a'), ('b', 'header_context:document'))

    def test_cut_given_name_with_trailing_hyphen_is_not_a_bare_name(self):
        c = self.context({'a': 'Zelda-', 'b': 'Zelda Hargrave'}, [
            ('a', 'peer', 'sent_to', 'D1'), ('b', 'peer', 'sent_to', 'D1')])
        self.assertIsNone(c.propose('a'))

    def test_initials_and_handles_can_use_one_document(self):
        for label in ('ZMH', 'zmhargrave'):
            c = self.context({'a': label, 'b': 'Zelda Marian Hargrave'}, [
                ('a', 'hub', 'sent_to', 'D1'), ('b', 'hub', 'sent_to', 'D1'),
                ('a', 'hub', 'sent_to', 'D2')])
            self.assertEqual(c.propose('a'), ('b', 'header_context:document'))

    def test_quoted_full_name_attests_an_otherwise_unresolved_handle(self):
        c = self.context({'a': 'zmhargrave', 'b': 'Zelda Marian Hargrave'})
        c.quoted['a', 'b'] = {'D1'}
        self.assertEqual(c.propose('a'), ('b', 'header_context:quoted_header'))

    def test_quoted_header_search_is_batched_bounded_and_ignores_body_names(self):
        import corpus
        db = self.enterContext(contextlib.closing(sqlite3.connect(':memory:')))
        db.execute('CREATE TABLE pages(doc_id,page,text)')
        text = 'From: Zelda Marian Hargrave\nOn Thu, 1 Jan, zmhargrave\nwrote:\n'
        db.executemany('INSERT INTO pages VALUES (?,?,?)', [('D1', 0, text),
            ('D2', 0, 'Discuss Zelda Marian Hargrave and zmhargrave in the body.'),
            ('D4', 0, text + 'From: bulk list\n' * 25)]
            + [('D3', p, text if p == 0 else 'long archive') for p in range(7)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'corpus.db'
            path.touch()
            with patch.object(corpus, 'PATH', path), patch.object(corpus, 'connect', return_value=db), \
                    patch.object(corpus, 'search_text', side_effect=lambda con, q: con.execute('SELECT doc_id,text FROM pages')) as search:
                found = quoted_handle_documents([('a', 'b', 'zmhargrave', 'Zelda Marian Hargrave'),
                                                  ('c', 'd', 'rother', 'Ramon Other')])
        self.assertEqual(found, {('a', 'b'): {'D1'}})
        self.assertIn(' OR ', search.call_args.args[1])

    def test_bare_name_document_coverage_and_competitor(self):
        rows = [(i, 'hub', 'sent_to', d) for i in ('a', 'b') for d in ('D1', 'D2')]
        c = self.context({'a': 'Zelda', 'b': 'Zelda Hargrave'}, rows)
        self.assertEqual(c.propose('a'), ('b', 'header_context:document'))
        c = self.context({'a': 'Zelda', 'b': 'Zelda Hargrave', 'c': 'Zelda Other'},
                         rows + [('c', 'hub', 'sent_to', 'D1'), ('c', 'hub', 'sent_to', 'D2')])
        self.assertIsNone(c.propose('a'))

    def test_crowded_document_is_not_evidence(self):
        names = {'a': 'Zelda Har', 'b': 'Zelda Hargrave', **{f'c{n}': f'Other Person{n}' for n in range(25)}}
        self.assertIsNone(self.context(names, [(i, 'hub', 'sent_to', 'D') for i in names]).propose('a'))

    def test_three_independent_correspondents_and_a_comparable_rival(self):
        names = {'a': 'Hargrave', 'b': 'Zelda Hargrave', 'c': 'Nerissa Hargrave'}
        rows = [(i, p, 'sent_to', None) for i in ('a', 'b') for p in ('p', 'q', 'r')]
        c = self.context(names, rows)
        self.assertEqual(c.propose('a'), ('b', 'header_context:correspondents'))
        # K-1 does not pass; repeated messages to one peer do not count as K.
        self.assertIsNone(self.context(names, rows[:2] * 50).propose('a'))
        self.assertIsNone(self.context(names, rows + [('c', p, 'sent_to', None) for p in ('p', 'q')]).propose('a'))

    def test_small_overlap_with_a_large_neighborhood_is_insufficient(self):
        rows = [(i, p, 'sent_to', None) for i in ('a', 'b') for p in ('p', 'q', 'r')]
        rows += [(i, f'{i}{n}', 'sent_to', None) for i in ('a', 'b') for n in range(10)]
        self.assertIsNone(self.context({'a': 'Zelda Har', 'b': 'Zelda Hargrave'}, rows).propose('a'))

    def test_mailbox_backed_visual_ocr_in_both_words(self):
        names = {'a': 'ZeIda Hargraue', 'b': 'Zelda Hargrave'}
        c = self.context(names, [(i, 'email_x', 'header_name_association', 'D') for i in names])
        self.assertEqual(c.propose('a'), ('b', 'header_context:mailbox_ocr'))
        self.assertIsNone(self.context(names, [(i, 'peer', 'sent_to', 'D') for i in names]).propose('a'))
        self.assertLess(ocr_cost('rnclvv', 'mdw'), 1)
        self.assertGreater(ocr_cost('abcdef', 'uvwxyz'), 4)

    def test_mailbox_backed_ocr_can_repair_word_boundaries(self):
        for label in ('Zel da Hargraue', 'Ze Ida Hargrave'):
            c = self.context({'a': label, 'b': 'Zelda Hargrave'}, [
                ('a', 'email_x', 'header_name_association', 'D1'),
                ('b', 'email_x', 'header_name_association', 'D2')])
            self.assertEqual(c.propose('a'), ('b', 'header_context:mailbox_ocr'))

    def test_generation_and_legal_entity_suffixes_never_disappear_under_ocr(self):
        for label in ('Zelda Hargrave Jr', 'Zelda A Hargrave PC', 'Zelda Hargrave II', 'Zelda Hargrave PLLC'):
            c = self.context({'a': label, 'b': 'Zelda Hargrave'}, [
                ('a', 'email_x', 'header_name_association', 'D1'),
                ('b', 'email_x', 'header_name_association', 'D2')])
            self.assertIsNone(c.propose('a'))

    def test_ocr_does_not_ignore_other_people_or_middle_initials(self):
        for other in ('Nerissa Other', 'Zelda B Hargrave'):
            names = {'a': 'Zelda A Hargrave', 'b': other}
            c = self.context(names, [(i, 'email_x', 'header_name_association', 'D') for i in names])
            self.assertIsNone(c.propose('a'))

    def test_decoration_strips_only_to_an_existing_complete_identity(self):
        for label, rule in [('NYC - Zelda Hargrave', 'location_prefix'), ('Governor Zelda Hargrave', 'title_prefix'),
                            ('Zelda Hargrave Secretary', 'role_tail'), ('Zelda Hargrave-X', 'id_suffix'),
                            ('Zelda-A Hargrave', 'id_suffix')]:
            with self.subTest(label=label):
                self.assertEqual(decorated_name(label), ('Zelda Hargrave', rule))
                c = self.context({'a': label, 'b': 'Zelda Hargrave'})
                self.assertEqual(c.propose('a'), ('b', 'header_context:' + rule))
        self.assertIsNone(self.context({'a': 'NY Zelda', 'b': 'Zelda Hargrave'}).propose('a'))
        self.assertEqual(decorated_name('Anne-Marie Hargrave'), ('Anne-Marie Hargrave', None))
        self.assertEqual(decorated_name('Zelda Hargrave Assistant'), ('Zelda Hargrave Assistant', None))

    def test_bad_organization_and_phrase_references_are_excluded(self):
        for label, full in [('American', 'AMERICAN AIRLINES'), ('Zelda', 'Zelda Customer Service'),
                            ('Jefrey', 'west palm beach Jefrey')]:
            c = self.context({'a': label, 'b': full}, [(i, 'email_x', 'header_name_association', 'D') for i in ('a', 'b')])
            self.assertIsNone(c.propose('a'))

    def test_signature_brand_matching_the_domain_is_not_a_full_name_reference(self):
        c = self.context({'a': 'Zelda', 'b': 'Zelda I Examplebrand'}, [
            ('a', 'email_x', 'header_name_association', 'D1'),
            ('b', 'email_x', 'header_name_association', 'D2')],
            mailbox_values={'email_x': 'zelda@examplebrand.test'})
        self.assertIsNone(c.propose('a'))


class CompoundLabelTests(unittest.TestCase):
    def test_two_full_names_and_shared_surname_keep_the_compound(self):
        names = {'pair': 'Zelda Hargrave and Ramon Other', 'shared': 'Zelda and Ramon Other',
                 'z': 'Zelda Hargrave', 'r': 'Ramon Other', 'zo': 'Zelda Other'}
        links = compound_people(names, lambda i: i, comm.header_name_words, {'z', 'r', 'zo'})
        self.assertEqual(links, {'pair': {'z', 'r'}, 'shared': {'zo', 'r'}})

    def test_unknown_constituents_are_not_invented_and_survivors_are_not_overlaid(self):
        names = {'pair': 'Zelda Hargrave and Ramon Other', 'z': 'Zelda Hargrave'}
        self.assertEqual(compound_people(names, lambda i: i, comm.header_name_words, {'z'}), {'pair': {'z'}})
        self.assertEqual(compound_people(names, lambda i: i, comm.header_name_words, (), {'pair'}), {})
        self.assertEqual(compound_people(names, lambda i: i, comm.header_name_words, {'z'}, {'z'}), {'pair': set()})

    def test_companies_bare_pairs_and_ambiguous_constituents(self):
        names = {'pair': 'Zelda Hargrave and Ramon Other', 'z1': 'Zelda Hargrave', 'z2': 'Zelda Hargrave',
                 'bare': 'Zelda and Ramon', 'company': 'Zelda and Ramon LLC'}
        links = compound_people(names, lambda i: i, comm.header_name_words, {'z1', 'z2'}, forbidden={'llc'})
        self.assertEqual(links, {'pair': set()})
        self.assertEqual(compound_people({'pair': 'Zero and Negative Balances', 'b': 'Negative Balances'},
                         lambda i: i, comm.header_name_words, {'b'}, common_words={'zero', 'negative'}), {})


class ContextGuardTests(unittest.TestCase):
    def test_pending_mailbox_merge_cannot_bootstrap_name_or_document_evidence(self):
        db = self.enterContext(contextlib.closing(sqlite3.connect(':memory:')))
        db.execute('CREATE TABLE evidence(source_id,target_id,relationship_type,efta_number)')
        db.execute('INSERT INTO evidence VALUES (?,?,?,?)', ('participant_a', 'email_x', 'header_name_association', 'D1'))
        with patch.object(comm, 'node_review', return_value={'participants': {}}):
            merges = comm.resolve_identities(db, {'email_x': 'zh@corp.test'},
                {'email_x': (1, {'confidence': 'needs_review'})},
                participant_labels={'participant_a': 'Zelda Har'}, person_names={'kg:1': 'Zelda Hargrave'})
        self.assertNotIn('participant_a', merges)

    def test_compound_export_keeps_node_links_and_public_values_pseudonymous(self):
        import emails
        from adapters import efta_emails
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'output').mkdir()
            (root / 'data/raw').mkdir(parents=True)
            (root / 'config').mkdir()
            (root / 'config/survivors.json').write_text('{"names": []}')
            people = [dict(id=n, name=label, normalized_name=label.lower(), entity_type='person')
                      for n, label in enumerate(['Sender Example', 'Zelda Hargrave', 'Ramon Other'], 1)]
            (root / 'output/normalized_entities.json').write_text(json.dumps(people))
            pair = 'Zelda Hargrave and Ramon Other'
            efta_emails.create(headers_path=root / 'data/raw/email_metadata.db',
                parties_path=root / 'data/raw/communications.db', headers=[
                    (1, 'D1', 0, 'Sender Example', 'sender@corp.test', json.dumps([pair]), '[]', None, 0)])
            with patch.object(comm, 'ROOT', root), patch.object(comm, 'contact_key', return_value=b'test key'), \
                    patch.object(comm.corpus_source, 'PATH', root / 'absent.db'), emails.use_root(root), \
                    patch('sys.argv', ['build_communications.py']), contextlib.redirect_stdout(io.StringIO()):
                comm.main()
            nodes = json.loads((root / 'output/communication_nodes.json').read_text())
            marked = next(n for n in nodes if n.get('compound'))
            self.assertEqual(set(marked['compound_members']), {2, 3})
            edges = json.loads((root / 'output/communication_edges.json').read_text())
            links = [e for e in edges if e['relationship_type'] == 'compound_member']
            self.assertEqual({e['target_entity_id'] for e in links}, {2, 3})
            with contextlib.closing(sqlite3.connect(root / 'output/communication_graph.db')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM compound_members').fetchone()[0], 2)
                self.assertEqual(db.execute('SELECT count(*) FROM nodes WHERE id=?', (marked['id'],)).fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT count(*) FROM identity_map WHERE member_id=?', (marked['id'],)).fetchone()[0], 0)
            for path in (root / 'output').glob('*.json'):
                self.assertNotIn(pair, path.read_text())

    def test_explicit_two_person_label_cannot_merge_into_one_constituent(self):
        db = self.enterContext(contextlib.closing(sqlite3.connect(':memory:')))
        db.execute('CREATE TABLE evidence(source_id,target_id,relationship_type,efta_number)')
        db.executemany('INSERT INTO evidence VALUES (?,?,?,?)', [
            ('participant_pair', 'email_x', 'header_name_association', 'D1'),
            ('kg:1', 'email_x', 'header_label_association', 'D1')])
        with patch.object(comm, 'node_review', return_value={'participants': {}}):
            merges = comm.resolve_identities(db, {'email_x': 'zh@example.test'}, {},
                participant_labels={'participant_pair': 'Zelda Hargrave and Ramon Other'},
                matcher=comm.PersonMatcher([dict(id=1, name='Zelda Hargrave', entity_type='person')]),
                person_names={'kg:1': 'Zelda Hargrave', 'kg:2': 'Ramon Other'})
        self.assertNotIn('participant_pair', merges)

    def test_context_still_respects_transitive_and_imported_separations(self):
        db = self.enterContext(contextlib.closing(sqlite3.connect(':memory:')))
        db.execute('CREATE TABLE evidence(source_id,target_id,relationship_type,efta_number)')
        db.executemany('INSERT INTO evidence VALUES (?,?,?,?)', [
            ('participant_a', 'email_x', 'header_name_association', 'D1'),
            ('kg:1', 'email_x', 'header_label_association', 'D2')])
        with patch.object(comm, 'node_review', return_value={'participants': {}}):
            merges = comm.resolve_identities(db, {'email_x': 'zh@example.test'}, {},
                never=[['participant_a', 1]], participant_labels={'participant_a': 'Hargrave'},
                person_names={'kg:1': 'Zelda Hargrave'})
        self.assertNotIn('participant_a', merges)
        with patch.object(comm, 'node_review', return_value={'participants': {}}):
            merges = comm.resolve_identities(db, {'email_x': 'zh@example.test'},
                {'participant_alias': ('participant_a', {'confidence': 'confirmed'})},
                never=[['participant_alias', 1]], participant_labels={'participant_a': 'Hargrave'},
                person_names={'kg:1': 'Zelda Hargrave'})
        self.assertNotIn('participant_a', merges)
        self.assertEqual(merges['participant_alias'][0], 'participant_a')
