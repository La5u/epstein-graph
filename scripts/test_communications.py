import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_communications
import emails as email_source
from build_communications import Builder, join_split_addresses, join_wrapped_names, normalized_date, parse_recipient, parse_sender, valid_email
import ocr_consensus as _ocr_consensus
_ocr_consensus.PATH = _ocr_consensus.PATH.with_name('nonexistent_ocr_consensus_for_tests.json')  # tests never read the live re-OCR output


class CommunicationTests(unittest.TestCase):
    def test_grouped_header_prepasses_preserve_occurrence_counts(self):
        rows = [('Example Person', '["Other Name", "Other Name"]', '["Third Person"]')] * 23
        rows += [(None, 'null', 'not json'), ('Other Name', '["Example Person <a@example.test>"]', '[]')]
        expected = build_communications.header_name_counts(rows)
        with contextlib.closing(sqlite3.connect(':memory:')) as db:
            db.execute('CREATE TABLE headers(sender_name, to_json, cc_json, noise)')
            db.executemany('INSERT INTO headers VALUES (?,?,?,0)', rows)
            db.execute('INSERT INTO headers VALUES (?,?,?,1)', ('Noise Name', '[]', '[]'))
            counted = build_communications.counted_header_names(db)
        self.assertEqual(counted, expected)
        self.assertEqual(build_communications.wrapped_full_names(rows),
                         build_communications.wrapped_full_names(counts=counted))
        self.assertEqual(build_communications.frequent_names(rows),
                         build_communications.frequent_names(counts=counted))

    def test_bulk_ocr_search_matches_individual_distance_cutoffs(self):
        import random
        rng = random.Random(0)
        names = {size: {''.join(rng.choices('abcdef ', k=size)) for _ in range(25)}
                 for size in range(6, 25)}
        labels = ['Example Person', 'Fable Wexle', 'short', 'a b', 'longer full name']
        for values in names.values():
            for name in values:
                labels.extend((name, name[:-1], 'a' + name[1:], name + 'b'))
        for label in labels:
            letters = build_communications.name_letters(label)
            limit = 2 if len(letters) >= 12 else 1
            expected = (2 <= len(letters.split()) <= 3 and len(letters) >= 8 and
                        any(build_communications.edit_distance(letters, name, limit) <= limit
                            for size in range(len(letters) - limit, len(letters) + limit + 1)
                            for name in names.get(size, ())))
            self.assertEqual(build_communications.ocr_name_variant(label, names), expected, label)

    def test_webmail_fragment_folds_into_previous_recipient(self):
        # "Name <user@g" | "mail.com>": the halves are rejoined when they form an address.
        self.assertEqual(join_split_addresses([('Example Person <user®g', None), ('mail.com>', None)]),
                         (('Example Person', 'user@gmail.com'),))
        # Otherwise the earlier item already names the recipient; the tail is not a person.
        self.assertEqual(join_split_addresses([('Example Person', None), ('ail.com', None), ('Other Person', None)]),
                         (('Example Person', None), ('Other Person', None)))
        self.assertEqual(join_split_addresses([('', 'a@example.test'), ('mail.com', None)]), (('', 'a@example.test'),))
        self.assertEqual(join_split_addresses([('mail.com', None)]), ())
        # Real names and non-webmail domains are untouched.
        self.assertEqual(join_split_addresses([('Classmates.com', None), ('com', None)]), (('Classmates.com', None), ('com', None)))

    def test_wrapped_name_rejoined_only_when_known_as_full_name(self):
        full = build_communications.wrapped_full_names(
            [('Example Person', json.dumps(['Other Name'] * 25), '[]')] + [('Example Person', '[]', '[]')] * 20)
        self.assertEqual(full, {'example person', 'other name'})
        # "...; Example" | "Person <address>": one recipient, keeping the address.
        self.assertEqual(join_wrapped_names([('Lead Name', None), ('Example', None), ('Person', 'p@example.test')], full),
                         (('Lead Name', None), ('Example Person', 'p@example.test')))
        self.assertEqual(join_wrapped_names([(', Other', None), ('Name', None)], full), (('Other Name', None),))
        # Unknown pairs, sign-offs and items with their own address stay separate.
        for pairs in ([('Example', None), ('Stranger', None)], [('Thanks', None), ('Person', None)],
                      [('Example', 'e@example.test'), ('Person', None)]):
            self.assertEqual(join_wrapped_names(pairs, full | {'thanks person'}), tuple(pairs))

    def test_sender_signoff_is_not_a_recipient(self):
        self.assertTrue(build_communications.signed_own_name('Neria', None, 'Neria Example'))
        self.assertFalse(build_communications.signed_own_name('Neria', 'n@example.test', 'Neria Example'))
        self.assertFalse(build_communications.signed_own_name('Neria', None, 'Neria'))
        self.assertFalse(build_communications.signed_own_name('Lesley', None, 'Neria Example'))

    def test_bare_first_name_attributed_from_its_document(self):
        labels = {'participant_bare': 'melanie', 'participant_a': 'melanie example', 'participant_b': 'melanie other',
                  'kg:1': 'Jeffrey Epstein'}
        texts = {'D2': 'Thanks, Melanie Example', 'D3': 'From: Melanie <mo@example.test>',
                 'D4': 'Melanie Example and Melanie Other', 'D5': 'nothing here'}
        rows = [(1, 'D1', 'participant_bare', 'kg:1'), (2, 'D1', 'participant_a', 'kg:1'),  # header in the same document
                (3, 'D2', 'kg:1', 'participant_bare'),  # full name in the page text
                (4, 'D3', 'participant_bare', 'kg:1'),  # address quoted in the page text
                (5, 'D4', 'participant_bare', 'kg:1'),  # two Melanies: left alone
                (6, 'D5', 'participant_bare', 'kg:1')]  # nothing: left alone
        owners = build_communications.first_name_owners(rows, labels, lambda i: i, {'mo@example.test': 'participant_b'},
                                                        lambda efta: texts.get(efta, ''))
        self.assertEqual(owners, {(1, 'source'): 'participant_a', (3, 'target'): 'participant_a', (4, 'source'): 'participant_b'})

    def test_bare_surname_and_couples(self):
        labels = {'kg:7': 'Dubin', 'kg:8': 'Eva Dubin', 'kg:9': 'Glenn Dubin', 'participant_s': 'merrivella',
                  'participant_l': 'lynn merrivella', 'participant_j': 'coco merrivella',
                  'participant_c': 'lynn and coco merrivella', 'kg:1': 'Jeffrey Epstein'}
        rows = [(1, 'D1', 'kg:1', 'kg:7'), (2, 'D1', 'kg:1', 'kg:8'),  # one Dubin in the headers
                (3, 'D2', 'kg:1', 'kg:7'),  # Glenn and Eva named in the text: ambiguous
                (4, 'D3', 'kg:1', 'participant_s'), (5, 'D3', 'kg:1', 'participant_l'), (6, 'D3', 'kg:1', 'participant_j'),
                (7, 'D3', 'kg:1', 'participant_c')]  # both Merrivellas and their couple node
        owners = build_communications.first_name_owners(rows, labels, lambda i: i, {},
                                                        lambda efta: 'Glenn Dubin and Eva Dubin' if efta == 'D2' else '')
        self.assertEqual(owners, {(1, 'target'): 'kg:8', (4, 'target'): 'participant_c'})
        self.assertEqual(build_communications.join_wrapped_names([('Lynn and', None), ('Coco', None)], {'lynn and coco'}),
                         (('Lynn and Coco', None),))

    def test_automated_company_sender(self):
        company = build_communications.automated_company
        self.assertEqual(company('messages-noreply@bounce.linkedin.com'), 'linkedin.com')
        self.assertEqual(company('fixture-notify@orders.apple.com'), 'apple.com')
        self.assertIsNone(company('person.name@linkedin.com'))
        self.assertIsNone(company('mailer-daemon@googlemail.com'))

    def test_context_person_filters_form_fields(self):
        cp = build_communications.context_person
        self.assertTrue(cp('Example Person', None, True))
        self.assertTrue(cp('Some Label', 'ok_person', False))
        self.assertFalse(cp('Example Person', None, False))  # not attested
        for label in ('skype name', 'lynn and', 'airline tickets', 'michael example da to'):
            self.assertFalse(cp(label, None, True), label)
        self.assertFalse(cp('Example Person', 'boilerplate', True))
        self.assertTrue(build_communications.bare_first_name('Melanie', None))
        self.assertFalse(build_communications.bare_first_name('Melanie', 'boilerplate'))

    def test_header_parsing(self):
        self.assertEqual(parse_recipient('Example Person <person@example.test>'), [('Example Person', 'person@example.test')])
        self.assertEqual(parse_recipient('Example Person'), [('Example Person', None)])
        self.assertEqual(parse_recipient('a@example.test, b@example.test'), [('', 'a@example.test'), ('', 'b@example.test')])
        self.assertIsNone(valid_email('[REDACTED]'))
        self.assertIsNone(valid_email('bad-address'))
        self.assertIsNone(valid_email('image001.png@01D0778D.2D49CD50'))  # inline image Content-ID
        self.assertEqual(normalized_date('2010-02-03'), '2010-02-03')
        self.assertIsNone(normalized_date('not a date'))
        self.assertIsNone(normalized_date('2010-02-30'))
        self.assertIsNone(normalized_date('2010-13-01'))
        self.assertEqual(normalized_date('2020-02-29'), '2020-02-29')
        self.assertEqual(parse_recipient(['Example Person', 'person@example.test']), [('Example Person', 'person@example.test')])
        self.assertEqual(parse_recipient([None, 'person@example.test']), [('', 'person@example.test')])
        self.assertEqual(parse_recipient(['Example Person', None]), [('Example Person', None)])
        self.assertEqual(parse_recipient([None, None]), [])
        # Mixed and ';'-separated headers keep every usable recipient.
        self.assertEqual(parse_recipient('Alice Smith <alice@corp.test>, Bob Jones'), [('Alice Smith', 'alice@corp.test'), ('Bob Jones', None)])
        self.assertEqual(parse_recipient('"Smith, Alice" <alice@corp.test>; Bob Jones; Ann'), [('Smith, Alice', 'alice@corp.test'), ('Bob Jones', None)])
        self.assertEqual(parse_recipient('Smith, John'), [('Smith, John', None)])
        self.assertEqual(parse_recipient('Alice Smith; Bob Jones'), [('Alice Smith', None), ('Bob Jones', None)])
        self.assertEqual(parse_recipient('Alice Smith, Bob Jones'), [('Alice Smith', None), ('Bob Jones', None)])
        # Sender address embedded in the header string is recovered.
        self.assertEqual(parse_sender('Example Person <person@corp.test>', None), ('Example Person', 'person@corp.test'))
        self.assertEqual(parse_sender('Example Person', 'person@corp.test'), ('Example Person', 'person@corp.test'))
        # Placeholders are exact; real addresses containing the words survive.
        for placeholder in ('someone@example.com', 'redacted@corp.test', 'unknown@corp.test', 'a@redacted.org'):
            self.assertIsNone(valid_email(placeholder), placeholder)
        for real in ('unredacted@corp.test', 'jane@notexample.com'):
            self.assertEqual(valid_email(real), real)

    def test_pseudonyms_and_identity_scope(self):
        entities = [{'id': 1, 'name': 'Example Person', 'normalized_name': 'example person', 'entity_type': 'person'},
                    {'id': 2, 'name': 'Example', 'normalized_name': 'example', 'entity_type': 'person'}]
        b = Builder(b'key', entities)
        self.assertEqual(b.endpoint('EXAMPLE PERSON', None), 'kg:1')
        identity = b.endpoint('Private Name', 'private@example.test')
        self.assertTrue(identity.startswith('email_'))
        self.assertEqual(identity, b.endpoint(None, 'private@example.test'))
        unknown = b.endpoint('Private Name', None)
        self.assertTrue(unknown.startswith('participant_'))
        self.assertNotEqual(b.endpoint('Example', None), 'kg:2')  # single-token labels never match a person
        self.assertTrue(b.endpoint('Example', None).startswith('participant_'))  # kept as an unresolved label
        self.assertIsNone(b.endpoint('[redacted]', None))
        self.assertIsNone(b.endpoint('bad@header', None))
        self.assertNotIn('private@example.test', repr(b.nodes))
        self.assertNotIn('Private Name', repr(b.nodes))

    def test_end_to_end_roles_dedup_and_resolver_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'output').mkdir()
            (root / 'data/raw').mkdir(parents=True)
            (root / 'data/private').mkdir()
            (root / 'data/private/contact_hmac.key').write_bytes(b'test key')
            mime_value = 'image042.png@fixture47b7.test'
            (root / 'data/raw/extracted_entities_filtered.json').write_text(json.dumps({
                'emails': [{'entity_value': mime_value}]}))
            entity = dict(id=1, name='Example Person', normalized_name='example person', entity_type='person', original_entity_ids=[1])
            (root / 'output/normalized_entities.json').write_text(json.dumps([entity]))
            with contextlib.closing(sqlite3.connect(root / 'data/raw/email_metadata.db')) as con, con:
                con.execute('CREATE TABLE emails_merged(id,efta_number,message_index,sender,sender_email,to_recipients,cc_recipients,date_sent,is_noise)')
                con.execute('INSERT INTO emails_merged VALUES(?,?,?,?,?,?,?,?,?)',
                            (1, 'EFTA001', 0, 'Example Person', 'sender@example.test',
                             json.dumps(['recipient@example.test', 'recipient@example.test', ['Another Person', 'recipient@example.test']]),
                             json.dumps(['recipient@example.test']), '2010-01-01', 0))
                con.execute('INSERT INTO emails_merged VALUES(?,?,?,?,?,?,?,?,?)',
                            (2, 'EFTA002', 0, None, None, '[]', '[]', None, 0))
            with contextlib.closing(sqlite3.connect(root / 'data/raw/communications.db')) as con, con:
                con.executescript('''CREATE TABLE emails(id,efta_number,date_normalized,is_noise);
                    CREATE TABLE email_participants(email_address,resolved_entity_id,email_id,role,name);
                    CREATE TABLE resolved_identities(id,canonical_name,confidence,match_method);
                    INSERT INTO emails VALUES(1,'EFTA001','2010-01-01',0);
                    INSERT INTO resolved_identities VALUES(1853,'Example Person',0.9,'exact');
                    INSERT INTO email_participants VALUES('sender@example.test',1853,1,'from',NULL);
                    INSERT INTO email_participants VALUES('recipient@example.test',NULL,1,'to',NULL);
                    INSERT INTO emails VALUES(2,'EFTA003','2010-01-02',0);
                    INSERT INTO email_participants VALUES('sender@example.test',NULL,2,'from',NULL);
                    INSERT INTO email_participants VALUES('extra@example.test',NULL,2,'to',NULL);''')
            with patch.object(build_communications, 'ROOT', root), patch.object(build_communications, 'contact_key', lambda: b'test key'), email_source.use_root(root), patch('sys.argv', ['build_communications.py']), \
                    contextlib.redirect_stdout(io.StringIO()):
                build_communications.main()
            with contextlib.closing(sqlite3.connect(root / 'output/communication_graph.db')) as con:
                totals = dict(con.execute('SELECT relationship_type,sum(record_count) FROM edges GROUP BY relationship_type'))
                self.assertEqual(totals, {'sent_to': 2, 'cc_to': 1, 'header_label_association': 1, 'upstream_resolved_header_identity': 1})
                self.assertEqual(con.execute("SELECT count(*) FROM nodes WHERE id='kg:1'").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT count(*) FROM nodes WHERE label LIKE '%@%'").fetchone()[0], 0)
                mime_id = build_communications.contact_id(mime_value, 'email', b'test key')
                self.assertEqual(con.execute('SELECT label_quality FROM node_classes WHERE id=?', (mime_id,)).fetchone()[0], 'not_a_name')
            exported_nodes = json.loads((root / 'output/communication_nodes.json').read_text())
            self.assertEqual(next(n for n in exported_nodes if n['id'] == mime_id)['label_quality'], 'not_a_name')
            summary = json.loads((root / 'output/communication_summary.json').read_text())
            self.assertEqual(summary['non_noise_message_records'], 2)
            self.assertEqual(summary['message_records_with_edges'], 1)
            self.assertEqual(summary['supplemental_new_pair_types'], 1)
            self.assertEqual(summary['supplemental_source_message_records'], 1)
            self.assertEqual(summary['missing_usable_sender'], 1)
            self.assertEqual(summary['missing_usable_recipients'], 1)
            self.assertNotIn('sender@example.test', (root / 'output/communication_nodes.json').read_text())
            secret = 'private@corp.test +1-212-555-0199'
            curated = {'email_reviewed': (1, {'reason': secret, 'confidence': 'confirmed'})}
            recipient_id = build_communications.contact_id('recipient@example.test', 'email', b'test key')
            review = {'participants': {'participant_reviewed': {'merge_into': 1, 'verdict': secret},
                                       recipient_id: {'verdict': secret}}}
            with patch.object(build_communications, 'ROOT', root), email_source.use_root(root), \
                    patch.object(build_communications, 'contact_key', return_value=b'test key'), \
                    patch.object(build_communications, 'load_identity_merges', return_value=curated), \
                    patch.object(build_communications, 'node_review', return_value=review), \
                    patch('sys.argv', ['build_communications.py']), contextlib.redirect_stdout(io.StringIO()):
                build_communications.main()
            for path in (root / 'output').glob('*.json'):
                self.assertNotIn(secret, path.read_text(), str(path))
            nodes = json.loads((root / 'output/communication_nodes.json').read_text())
            recipient = next(n for n in nodes if n['id'] == recipient_id)
            self.assertEqual(recipient['label_quality'], 'uncertain')
            exported = json.loads((root / 'output/identity_map.json').read_text())
            self.assertEqual(exported['email_reviewed']['rule'], 'reviewed_identity_merge')
            with contextlib.closing(sqlite3.connect(root / 'output/communication_graph.db')) as con:
                rules = dict(con.execute('SELECT member_id,rule FROM identity_map'))
                self.assertEqual(rules['participant_reviewed'], 'reviewed_audit_merge')
                self.assertNotIn(secret, repr(list(con.iterdump())))


class DeterministicSampleTests(unittest.TestCase):
    def test_samples_do_not_depend_on_insertion_order(self):
        rows = [('a', 'b', 'sent_to', f'EFTA{i % 4}', f'db{i % 2}', 'emails', i, i % 3, 'a', 'b')
                for i in range(12)]
        # Include nulls and ties to exercise the complete exported tuple ordering.
        rows += [('a', 'b', 'sent_to', None, 'db0', 'emails', 0, None, 'a', 'b')]
        samples = []
        for ordered in (rows, list(reversed(rows)), rows[::2] + rows[1::2]):
            with contextlib.closing(sqlite3.connect(':memory:')) as db:
                db.execute('''CREATE TABLE evidence(source_id,target_id,relationship_type,efta_number,
                    source_db,source_table,source_record_id,message_index,canonical_source,canonical_target)''')
                db.executemany('INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?)', ordered)
                samples.append((build_communications.sample_documents(db, 'a'),
                                build_communications.sample_references(db, 'a', 'b', 'sent_to')))
        self.assertEqual(samples[0], samples[1])
        self.assertEqual(samples[0], samples[2])
        self.assertEqual(samples[0][0], ['EFTA0', 'EFTA1', 'EFTA2'])
        self.assertEqual(len(samples[0][1]), 5)


class LabelQualityTests(unittest.TestCase):
    def test_body_labels_remain_registered_without_becoming_parties(self):
        builder = Builder(b'fixture key', [])
        i = builder.mark_nonparty('Budget review next week')
        self.assertIn(i, builder.nodes)
        self.assertIn(i, builder.nonparty_ids)
        self.assertNotIn(i, builder.party_ids)
        self.assertEqual(builder.endpoint('Budget review next week', None), i)
        self.assertIn(i, builder.party_ids)
        builder.mark_nonparty('Budget review next week')
        self.assertIn(i, builder.party_ids)
        for label in ('Assistant Vice President', 'Su bject', 'Good afternoon Zelda'):
            i = builder.endpoint(label, None)
            self.assertIn(i, builder.nodes)
            self.assertEqual(build_communications.label_quality(label), 'not_a_name')

    def test_bare_field_body_records_are_dropped_and_checks_are_cached(self):
        from adapters import efta_emails
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'output').mkdir()
            (root / 'data/raw').mkdir(parents=True)
            (root / 'data/private').mkdir()
            (root / 'data/private/contact_hmac.key').write_bytes(b'test key')
            (root / 'output/normalized_entities.json').write_text('[]')
            corpus_path = root / 'data/raw/test_corpus.db'
            with contextlib.closing(sqlite3.connect(corpus_path)) as corpus, corpus:
                corpus.execute('CREATE TABLE pages(doc_id, page, text)')
                corpus.executemany('INSERT INTO pages VALUES (?,1,?)', [
                    ('D1', 'From: sender@example.test\nTo:\nblue parcel arrived tahnks\nOn Mon, Dec 22, 2014'),
                    ('D2', 'From:\nblue parcel arrived tahnks\nOn Mon, Dec 22, 2014'),
                    ('D3', 'From: Actual Sender\nSubject: Budget review next week\nTo: recipient@example.test'),
                    ('D4', 'From:\nSent:\nTo:\nSubject:\nPrinted Sender\nTuesday, Jul 19, 2016 11:33 AM\n'
                     'Printed Recipient\nRE:\nReview of requested records\nToll-Free Dial-In Number: 123'),
                ])
            efta_emails.create(root / 'data/raw/email_metadata.db', root / 'data/raw/communications.db', headers=[
                (1, 'D1', 0, 'Sender Person', 'sender@example.test', '["blue parcel arrived tahnks"]', '[]', None, 0),
                (2, 'D2', 0, 'blue parcel arrived tahnks', None, '["recipient@example.test"]', '[]', None, 0),
                (3, 'D3', 0, 'Budget review next week', None, '["recipient@example.test"]', '[]', None, 0),
                (4, 'D4', 0, 'Printed Sender', 'sender@example.test',
                 '["Review of requested records", "Toll-Free Dial-In Number"]', '[]', None, 0),
            ])
            connections, reads = [], []
            prefetch_class = build_communications.DocumentPrefetch
            def connect():
                con = sqlite3.connect(corpus_path, check_same_thread=False)
                con.set_trace_callback(lambda query: reads.append(query) if 'ORDER BY page' in query else None)
                connections.append(con)
                return con
            with patch.object(build_communications, 'ROOT', root), email_source.use_root(root), \
                    patch.object(build_communications, 'contact_key', return_value=b'test key'), \
                    patch.object(build_communications, 'DocumentPrefetch', side_effect=lambda connect, fallback, enabled:
                                 prefetch_class(connect, fallback, False)), \
                    patch.object(build_communications.corpus_source, 'PATH', corpus_path), \
                    patch.object(build_communications.corpus_source, 'connect', side_effect=connect), \
                    patch.object(build_communications.corpus_source, 'stamp', return_value=['synthetic']), \
                    patch('sys.argv', ['build_communications.py']), contextlib.redirect_stdout(io.StringIO()), \
                    patch.object(build_communications, 'nonparty_header_text', wraps=build_communications.nonparty_header_text) as nonparty, \
                    patch.object(build_communications, 'header_block_text', wraps=build_communications.header_block_text) as blocks:
                builder_path = root / 'builder.py'
                for version in range(3):
                    builder_path.write_text(f'# unrelated builder edit {version}\n')
                    with patch.object(build_communications, '__file__', str(builder_path)):
                        build_communications.main()
                    summary = json.loads((root / 'output/communication_summary.json').read_text())
                    self.assertEqual(summary['body_text_after_bare_field_dropped'], 2)
                    self.assertEqual(summary['nonparty_header_text_dropped'], 3)
                    self.assertEqual(summary['missing_usable_sender'], 2)
                    self.assertEqual(summary['missing_usable_recipients'], 2)
                # A current review overrides a persisted document-only verdict.
                identity = Builder(b'test key', []).participant_id('budget review next week')
                (root / 'config').mkdir()
                (root / 'config/node_review.json').write_text(json.dumps({
                    'participants': {identity: {'verdict': 'group_or_role'}}, 'persons': {}}))
                build_communications.main()
                summary = json.loads((root / 'output/communication_summary.json').read_text())
                self.assertEqual(summary['nonparty_header_text_dropped'], 2)
                self.assertEqual(summary['missing_usable_sender'], 1)
                self.assertEqual(nonparty.call_count, 2)  # D1/D2 stop at the bare-field check
                blocks.assert_not_called()
            for con in connections:
                con.close()
            self.assertEqual(len(reads), 4)  # lazy checks share a read; later builds use persisted booleans
            with contextlib.closing(sqlite3.connect(root / 'output/communication_graph.db')) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM edges WHERE relationship_type='sent_to'").fetchone()[0], 1)
                for label in ('blue parcel arrived tahnks', 'Review of requested records'):
                    i = Builder(b'test key', []).participant_id(build_communications.name_key(label))
                    self.assertIsNotNone(db.execute('SELECT id FROM nodes WHERE id=?', (i,)).fetchone())
                    self.assertEqual(db.execute('SELECT label_quality FROM node_classes WHERE id=?', (i,)).fetchone()[0], 'not_a_name')

    def test_bare_fields_reject_body_with_typos_and_wrapped_lines(self):
        from build_communications import body_after_bare_field, word_key
        cases = [
            ('To:\nthey should meet my danihs freind\nOn Tue, Oct 20, 2009',
             ['they should meet my danihs freind']),
            ('To:\ntrain leaves at 3 45 for the coast, we will take saml and\nxyz on tue or wed\nThe information contained is confidential',
             ['xyz on tue or wed']),
            ('To:\nblue parcel arrived tahnks\nOn Mon, Dec 22, 2014 at 12:40 PM,',
             ['blue parcel arrived tahnks']),
            ('Cc:\npleese check\nThis message is confidential', ['pleese check']),
            ('To:\npleese check\nOn Mon, Dec 22, 2014, Bob <bob@example.test> wrote:\nSubject: old',
             ['pleese check']),
            ('From:\npleese check\nOn Monday someone wrote:', ['pleese check']),
            ('To:\npleese check\nCan you send it?\nFrom: quoted@example.test', ['pleese check']),
            ('> To:\n> pleese check\n> The information contained is confidential', ['pleese check']),
            ('To:\n\nlongish mesage\n' + 'continuing the body ' * 8, ['longish mesage']),
            ('To:\nwe should talk, pleese check the details, thank yo\nOn Mon, Dec 22, 2014',
             ['pleese check the details', 'thank yo']),
            ('To:\nplease read this mesage\nOn Mon, Dec 22, 2014 someone wrote:\nreply with a typo tahnks\nSubject: old',
             ['reply with a typo tahnks']),
        ]
        for text, labels in cases:
            with self.subTest(text=text):
                bad = body_after_bare_field(text)
                for label in labels:
                    self.assertIn(word_key(label), bad)

    def test_bare_fields_preserve_header_values_and_recipient_lists(self):
        from build_communications import body_after_bare_field, word_key
        texts = [
            'To:\nAlice Exampl\nCc:\nBea Sample(\nFrom:\nExample Clinic Followup Office\nSent:\nFri 6/10/2016',
            'To:\nA\nB\nCc:\nC\nSubject: hello',
            'To:\nAlice Example\nBob Example\nThis message is confidential',
            'To:\nAlice Example\nSubject: hello\n' + 'long message ' * 10,
            'To: Alice Example\nThis message is confidential',
            'To:\nAlice Example\nBob <bob@example.test>\nSubject: hi',
            'To:\nAlice Example',
            'To:\nAlice Example\n' + '\n' * 8 + 'This message is confidential',
            'From:\nSample Office Inc.\nSent: Friday\nTo: Other Person',
        ]
        for text in texts:
            with self.subTest(text=text):
                bad = body_after_bare_field(text)
                for label in ('Alice Exampl', 'Bea Sample(', 'Example Clinic Followup Office',
                              'A', 'B', 'Alice Example', 'Bob Example', 'Sample Office Inc.'):
                    self.assertNotIn(word_key(label), bad)
        for field in ('From', 'To', 'Cc', 'Bcc', 'Sent', 'Date', 'Subject', 'Attachments', 'Importance'):
            self.assertNotIn(word_key('Sample Person'), body_after_bare_field(
                f'To:\nSample Person\n{field}: value\nThis message is confidential'))

    def test_body_spelling_attested_in_another_header_is_preserved(self):
        from build_communications import body_after_bare_field, word_key
        for header in ('To: Sample Person\nSubject: hello', 'To:\nSample Person\nCc: Other Person'):
            text = 'To:\nSample Person\nThis message is confidential\n' + header
            self.assertNotIn(word_key('Sample Person'), body_after_bare_field(text))

    def test_body_filter_shares_address_known_review_and_frequency_exemptions(self):
        from build_communications import body_name_exempt, name_key, frequent_names, ocr_name_variant
        entities = [dict(id=1, name='Known Person', normalized_name='known person', entity_type='person')]
        builder = Builder(b'key', entities)
        refs = frequent_names([('Frequent Person', '[]', '[]')] * 5)
        variant = lambda name: ocr_name_variant(name, refs)
        addressed = {name_key('Addressed Person')}
        for verdict in build_communications.REVIEWED_NAMES | {'group_or_role', 'place'}:
            builder.review = {builder.participant_id(name_key('Reviewed Person')): {'verdict': verdict}}
            for label in ('Known Person', 'Reviewed Person', 'ADDRESSED PERSON', 'Frequent Person', 'Frequent Persom'):
                self.assertTrue(body_name_exempt(label, builder, addressed, variant), (verdict, label))
            self.assertFalse(body_name_exempt('pleese check', builder, addressed, variant))

    def test_nonparty_filter_catches_subjects_metadata_and_distant_body_lines(self):
        from build_communications import nonparty_header_text, word_key
        text = ('From: Sender Person\nTo: Recipient Person\nSubject: Budget review\n'
                'Attachments: Meeting notes.txt\nPlease read the attached document.\n' +
                'continuing narrative text\n' * 20 +
                'Annual Summary\nToll-Free Dial-In Number: 123\n'
                'Participant Passcode: 456\nRouting Number: 789')
        bad = nonparty_header_text(text)
        for label in ('Budget review', 'Meeting notes', 'Annual Summary', 'Toll-Free Dial-In Number',
                      'Participant Passcode', 'Routing Number'):
            self.assertIn(word_key(label), bad, label)
        for label in ('Sender Person', 'Recipient Person'):
            self.assertNotIn(word_key(label), bad, label)

    def test_nonparty_filter_preserves_printed_columns_with_page_stamps_and_missing_colons(self):
        from build_communications import nonparty_header_text, word_key
        for fields in ('From:\nSent:\nTo:\nCc:\nSubject:', 'From\nSent\nTo\nCc\nSubject'):
            text = (fields + '\nDocument 12-3\nFiled 01/01/20\nPage 2 of 6\n'
                    'Printed Sender\nTuesday, Jul 19, 2016 11:33 AM\n'
                    "'Printed Recipient\nOther Recipient\nRE:\n- Review of requested records\n"
                    'Please confirm the details.')
            bad = nonparty_header_text(text)
            self.assertIn(word_key('Review of requested records'), bad)
            for label in ('Printed Sender', 'Printed Recipient', 'Other Recipient'):
                self.assertNotIn(word_key(label), bad, (fields, label))

    def test_nonparty_filter_preserves_wrapped_and_repeated_party_spellings(self):
        from build_communications import nonparty_header_text, word_key
        cases = [
            'From:\nSender Person\nSent:\nTuesday, Jul 19, 2016\nTo:\nFirst Person\nSecond Person\n'
            'Third Person\nPlease read the document.',
            'To: First Person;\nSecond Person\nCc: Third Person\nSubject: Budget review',
            'To: First Person\nSecond Person <second@example.test>\nSubject: Budget review',
            'To:\nFirst Person\nSubject: Budget review\nPlease read this.\nFirst Person',
            '> To: First Person\n> Subject: Budget review\n> Please read this.',
            'To: First Person; Second Person; Third Person; Fourth Person; Fifth Person; Sixth Person; Seventh Person\n'
            'Subject: Budget review',
            'To: First Person; Second Person\nSubject: First Person\nSecond Person',
            'To: First Person <first@example.test>\nSubject: First Person\nPlease read this.',
        ]
        for text in cases:
            with self.subTest(text=text):
                bad = nonparty_header_text(text)
                for label in ('First Person', 'Second Person', 'Third Person'):
                    self.assertNotIn(word_key(label), bad, label)
        # A short, otherwise unstructured document has no header evidence.
        self.assertEqual(nonparty_header_text('First Person\nPlease read this.'), '')

    def test_nonparty_filter_catches_wrapped_subject_and_form_text_without_headers(self):
        from build_communications import nonparty_header_text, word_key
        self.assertIn(word_key('Budget review next week'), nonparty_header_text(
            'From: Sender Person\nSubject: Budget review\nnext week\nTo: Recipient Person'))
        self.assertIn(word_key('Toll-Free Dial-In Number'), nonparty_header_text('Toll-Free Dial-In Number: 123'))
        self.assertIn(word_key('Archive Summary'), nonparty_header_text(
            'From: Sender Person\nTo:\nRecipient Person\n<?xml version="1.0"?>\nArchive Summary\nSubject: old'))
        # A phrase attested as an actual party wins over a body occurrence.
        self.assertNotIn(word_key('Example Clinic'), nonparty_header_text(
            'To: Example Clinic\nSubject: Example Clinic\nPlease read this.\nExample Clinic'))

    def test_repeated_form_labels_do_not_become_reference_names(self):
        from build_communications import frequent_names, ocr_name_variant
        rows = [('Frequent Person', '["Account Number", "Toll-Free Dial-In Number", "Access Code"]', '[]')] * 10
        refs = frequent_names(rows)
        self.assertTrue(ocr_name_variant('Frequent Persom', refs))
        for label in ('Account Number', 'Toll-Free Dial-In Number', 'Access Code'):
            self.assertFalse(ocr_name_variant(label, refs), label)

    def test_grouped_header_prepass_collects_addressed_senders_and_recipients(self):
        with contextlib.closing(sqlite3.connect(':memory:')) as db:
            db.execute('CREATE TABLE headers(sender_name, sender_email, to_json, cc_json, noise)')
            db.executemany('INSERT INTO headers VALUES (?,?,?,?,?)', [
                ('Sender Person', 'sender@example.test', '["To Person <to@example.test>"]',
                 '[["Cc Person", "cc@example.test"]]', 0),
                ('No Address', None, '["Bare Person"]', '[]', 0),
                ('Recovered Person <recovered@example.test>', None, '[]', '[]', 0),
                ('Noise Person', 'noise@example.test', '[]', '[]', 1),
            ])
            addressed = set()
            build_communications.counted_header_names(db, addressed_names=addressed)
        self.assertEqual(addressed, {'sender person', 'to person', 'cc person', 'recovered person'})

    def test_boilerplate_and_noise_are_not_names(self):
        from build_communications import label_quality
        for junk in ('oog e a en ar', 'Untitled attachment', 'With warm regards', 'Reply to', 'On Fri', 'Best',
                     'where are you', 'III', 'MINIII', 'Sent from my iPhone', 'date-last-viewed'):
            expected = 'not_a_name' if build_communications.header_junk_reason(junk) else 'drop'
            self.assertEqual(label_quality(junk), expected, junk)
        for name in ('Leon Black', 'Co-Co Merrivella', 'J R R Smith', 'Karim Wade', 'da Silva', 'George Best',
                     'Larry', 'A Berwick', 'Hyejin Ko', 'LSJ', 'Jonah de la Verra', 'Soon Yi Previn aa'):
            self.assertEqual(label_quality(name), 'ok', name)
        self.assertEqual(label_quality('Assistant to Jeffrey Epstein'), 'group_or_role')

    def test_sentence_fragments_read_as_text(self):
        from build_communications import plain_text
        for label in ('as soy', 'i spoke to sue', 'As discussed', 'My best', 'of Jeffrey Epstein'):
            self.assertTrue(plain_text(label), label)
        for label in ('a sferrazza', 'A Vennor', 'At HMF', 'The Boston Globe', 'de la Cruz'):
            self.assertFalse(plain_text(label), label)

    def test_ocr_forms_of_frequent_names_are_not_body_text(self):
        from build_communications import frequent_names, ocr_name_variant
        rows = [('Fable Wexley', '[]', '[]')] * 5 + [('Lyn Merrivella', '[]', '[]')] * 5 + [('Thank you', '[]', '[]')] * 5
        names = frequent_names(rows)
        for label in ('fable wexle', 'I n Merrivella'):
            self.assertTrue(ocr_name_variant(label, names), label)
        for label in ('Thank yo', 'Fable', 'Sultan'):
            self.assertFalse(ocr_name_variant(label, names), label)

    def test_header_blocks_leave_out_body_lines(self):
        from build_communications import header_block_text, word_key
        blocks = header_block_text('From: a@b.com\nTo:\ncome back\nThe information is private')
        self.assertNotIn(word_key('come back'), blocks)
        blocks = header_block_text('To:\nAmy Ravenell\nthe list\nSubject: x\nhello\nFrom: Jane\nTo: one@b.com,\nBob Jones\nsee you')
        for line in ('Amy Ravenell', 'the list', 'Jane', 'Bob Jones'):
            self.assertIn(word_key(line), blocks)
        for line in ('hello', 'see you'):
            self.assertNotIn(word_key(line), blocks)

    def test_lowercase_dictionary_words_are_plain_words(self):
        from build_communications import dictionary_words, plain_words
        if not dictionary_words()[0]:
            self.skipTest('no system word list')
        for words in (['dissemination'], ['destination'], ['Destination'], ['corporate chasing'], ['dissemination', 'Dissemination'],
                      ['undisclosed-recipients', 'undisclosed recipients'], ['blocked'], ['house'], ['Commodities'], ['Info for'],
                      ['Requested items'], ["You're correct"], ['Here you go'], ['Hope you are doing good'],
                      ['Bank has received your request'], ["Couldn't load plugin"], ["Per Mr. Fraguela's request"],
                      ['Good evening Mr. Kahn'], ['Paul has been traveling']):
            self.assertTrue(plain_words(words), words)
        for names in (['larry'], ['bill'], ['rose'], ['xqzt'], [], ['Brian Day'], ['Smith I'], ['Rober'], ['Woody'], ['Mark Long'],
                      ['Dept of Neurology'], ['School of Earth and Space Exploration'], ["Nur'sel Veskalova"], ['VAM Luen-lan'],
                      ['IT Services'], ['Amazon.com Reviews'], ['Dick'], ['Peggy Siegal calla'], ['Alan Glifomo call list'], ["Painter's Supply Interiors"]):
            self.assertFalse(plain_words(names), names)
        self.assertFalse(plain_words(['groff'], frozenset({'groff'})))


class PersonMatcherTests(unittest.TestCase):
    def test_short_surnames_are_not_ocr_tails_or_false_exact_matches(self):
        from build_communications import PersonMatcher, name_tokens
        names = ['John David Li', 'John David Ng', 'Jane Mary Fox', 'Jane Mary', 'Richard Kahn']
        matcher = PersonMatcher([dict(id=i, name=name, entity_type='person')
                                 for i, name in enumerate(names)])
        for i, name in enumerate(names):
            self.assertEqual(name_tokens(name), name.casefold().split())
            self.assertEqual(matcher.match(name), (i, 'exact'))
        self.assertEqual(matcher.match('John David Wu'), (None, None))
        self.assertEqual(matcher.match('Jane Mary Lee'), (None, None))
        self.assertEqual(matcher.match('Jane Mary Song'), (None, None))
        long_given = PersonMatcher([dict(id=10, name='Christopher Lke', entity_type='person')])
        for surname in ('Ng', 'Wu', 'Lu'):
            self.assertEqual(long_given.match(f'Christopher {surname}'), (None, None))
        # Keep the well-supported single-character OCR-tail recovery.
        self.assertEqual(matcher.match('Richard Kahn c'), (4, 'exact'))

    def test_variants_resolve_and_namesakes_do_not(self):
        from build_communications import PersonMatcher
        people = [dict(id=i, name=n, entity_type='person') for i, n in
                  [(1, 'Jeffrey Epstein'), (2, 'Mark Epstein'), (3, 'Leon Black'), (4, 'Roy Black'), (5, 'Richard Kahn'), (6, 'Karyna Verland')]]
        m = PersonMatcher(people)
        self.assertEqual(m.match('jeff epstein'), (1, 'given_name_variant'))
        self.assertEqual(m.match('epstein jeffrey'), (1, 'reordered'))
        self.assertEqual(m.match('mark l. epstein'), (2, 'given_name_variant'))
        self.assertEqual(m.match('rich kahn c'), (5, 'given_name_variant'))
        self.assertEqual(m.match('karina verland'), (6, 'ocr_spelling'))
        self.assertEqual(m.match('l black'), (3, 'given_name_variant'))
        self.assertEqual(m.match('cynthia epstein'), (None, None))
        self.assertEqual(m.match('black'), (None, None))
        self.assertEqual(m.match('jeffrey epstein new york'), (1, 'leading_name_exact'))
        self.assertEqual(m.match('richardkahn'), (5, 'concatenated'))
        self.assertEqual(m.match('je rey eps em'), (1, 'ligature_loss'))  # lost ff/t, in->m

    def test_ligature_loss_leaves_another_persons_surname(self):
        from build_communications import PersonMatcher
        m = PersonMatcher([dict(id=1, name='Peter Mandelson', entity_type='person'),
                           dict(id=2, name='Mandy Elliston', entity_type='person')])
        # "mandelson" fits inside "mandyelliston", but it is Peter Mandelson's surname.
        self.assertEqual(m.match('Lord Mandelson'), (None, None))
        self.assertEqual(m.match('mandy elli ston'), (2, 'ligature_loss'))


class ResolveIdentityTests(unittest.TestCase):
    def resolve(self, emails=None, curated=None, never=(), labels=None, review=None, rows=(), **kwargs):
        db = self.enterContext(contextlib.closing(sqlite3.connect(':memory:')))
        db.execute('CREATE TABLE evidence(source_id, target_id, relationship_type)')
        db.executemany('INSERT INTO evidence VALUES (?,?,?)', rows)
        with patch.object(build_communications, 'node_review', return_value=review or {'participants': {}}):
            return build_communications.resolve_identities(
                db, emails or {}, curated or {}, never, labels, **kwargs)

    def test_structural_header_names_resolve_to_graph_people(self):
        variants = ['Hargrave. Zelda', 'Hargrave, Zelda', 'Hargrave Zelda', 'k Zelda Hargrave',
                    'Zelda Hargrave 4', 'ab Zelda Hargrave xy', 'Zelda Marian Hargrave',
                    'Zelda M.Hargrave', 'ZeldaHargrave', 'HargraveZelda', 'HargraveZelda Marian',
                    'Z elda Hargrave']
        labels = {f'participant_v{n}': s for n, s in enumerate(variants)}
        labels['participant_full'] = 'Zelda Marian Hargrave'
        rows = [('participant_full', 'email_x', 'sent_to')] * 100
        merges = self.resolve(labels=labels, person_names={'kg:1': 'Zelda Hargrave'}, rows=rows)
        for i in labels:
            with self.subTest(label=labels[i]):
                self.assertEqual(merges.get(i, (None,))[0], 'kg:1')

    def test_middle_names_and_glued_ocr_tokens_keep_ambiguity_guards(self):
        merges = self.resolve(labels={'participant_v': 'HargraveNerissa Marian',
                                      'participant_full': 'Nerissa Marian Hargrave'},
                              person_names={'kg:1': 'Nerissa Hargrave'},
                              rows=[('participant_full', 'email_x', 'sent_to')] * 100)
        self.assertEqual(merges.get('participant_v', (None,))[0], 'kg:1')
        for label in ('Zelda Hargrave', 'Zelda Anne Hargrave', 'Zelda A Hargrave'):
            merges = self.resolve(labels={'participant_v': label}, person_names={
                'kg:1': 'Zelda Marian Hargrave', 'kg:2': 'Zelda Beth Hargrave'})
            self.assertNotIn('participant_v', merges)
        merges = self.resolve(labels={'participant_v': 'Zelda C Hargrave'},
                              person_names={'kg:1': 'Zelda B Hargrave'})
        self.assertNotIn('participant_v', merges)

    def test_unique_long_surname_ocr_edit_and_nearby_rival(self):
        for label in ('Zelda Hargraave', 'Zelda Hargrve', 'Zelda Hargraue'):
            with self.subTest(label=label):
                merges = self.resolve(labels={'participant_v': label}, person_names={'kg:1': 'Zelda Hargrave'})
                self.assertEqual(merges.get('participant_v', (None,))[0], 'kg:1')
                self.assertEqual(merges['participant_v'][1], 'header_name_ocr_family:surname_one_ocr_edit')
                merges = self.resolve(labels={'participant_v': label}, person_names={
                    'kg:1': 'Zelda Hargrave', 'kg:2': 'Zelda Hargraves'})
                self.assertNotIn('participant_v', merges)
        for label in ('Other Hargraave', 'Zelda Ng'):
            self.assertNotIn('participant_v', self.resolve(labels={'participant_v': label},
                                                         person_names={'kg:1': 'Zelda Hargrave'}))

    def test_new_header_rules_respect_transitive_and_reviewed_separations(self):
        for label in ('Hargrave Zelda', 'k Zelda Hargrave', 'Zelda Marian Hargrave', 'HargraveZelda', 'Zelda Hargraave'):
            with self.subTest(label=label):
                labels = {'participant_v': label, 'participant_alias': label}
                merges = self.resolve(labels=labels, person_names={'kg:1': 'Zelda Hargrave'},
                                      curated={'participant_alias': ('participant_v', {'confidence': 'confirmed'})},
                                      never=[['participant_alias', 'kg:1']])
                self.assertNotIn('participant_v', merges)

    def test_mailbox_families_and_transport_ids(self):
        reference = 'longvacation@gmail.com'
        variants = ['ongvacation@grnail.com', 'longvacatio@gmair.comi', 'longvaxation@cunail.com',
                    'junklongvacation@sinail.com', 'longvacationjunk@gmail.co',
                    'longvacationlongvacation@gmad.com', 'junkongvacation@gmail.com']
        emails = {'email_ref': reference, **{f'email_v{n}': s for n, s in enumerate(variants)},
                  'email_other': 'longvacation1@me.com', 'email_number': 'longvacation1@gmail.com',
                  'email_domain': 'longvacation@me.com'}
        merges = self.resolve(emails=emails, rows=[('email_ref', 'kg:1', 'sent_to')] * 100)
        for n in range(len(variants)):
            self.assertEqual(merges.get(f'email_v{n}', (None,))[0], 'email_ref')
        for i in ('email_other', 'email_number', 'email_domain'):
            self.assertNotIn(i, merges)
        for value in ('image042.png@fixture47b7.test', 'image032.gif@fixture2a98.test',
                      '20150706114344.8482.54721@computer.home', '12345678@example.test', 'abc@host.local',
                      'fixturetoken1.us.firm.test@discard.maildb.coml'):
            self.assertIsNotNone(build_communications.non_mailbox_reason(value))
        self.assertIsNone(build_communications.non_mailbox_reason('person@example.test'))
        self.assertIsNone(build_communications.non_mailbox_reason('1234567890@mms.att.net'))
        self.assertIsNone(build_communications.non_mailbox_reason('1234567890@sms.example.test'))

    def test_nickname_cut_word_and_noise_prefix_variants(self):
        rows = [('participant_full', 'email_x', 'sent_to')] * 100
        cases = {'Zed Hargrave': 'nick', 'Zeddy Hargrave': 'nick', 'Zedekiah Hargrav': 'cut',
                 'on behalf of Zedekiah Hargrave': 'noise'}
        for label in cases:
            with self.subTest(label=label):
                merges = self.resolve(labels={'participant_v': label, 'participant_full': 'Zedekiah Hargrave'},
                                      person_names={'kg:1': 'Zedekiah Hargrave'}, rows=rows)
                if cases[label] != 'nick':
                    self.assertEqual(merges.get('participant_v', (None,))[0], 'kg:1')
        # A greeting addresses a person; it does not identify the sender.
        merges = self.resolve(labels={'participant_v': 'Hi Zedekiah Hargrave'},
                              person_names={'kg:1': 'Zedekiah Hargrave'}, rows=rows)
        self.assertNotIn('participant_v', merges)
        # Nickname table is only used with the same surname; a namesake with another surname stays apart.
        merges = self.resolve(labels={'participant_v': 'Joshua Vellerson'}, person_names={'kg:1': 'Josh Veller'})
        self.assertNotIn('participant_v', merges)
        merges = self.resolve(labels={'participant_v': 'Joshua Veller'}, person_names={'kg:1': 'Josh Veller'})
        self.assertEqual(merges.get('participant_v', (None,))[0], 'kg:1')
        # A surname other people carry is not a cut-off word.
        merges = self.resolve(labels={'participant_v': 'Roy Velde'},
                              person_names={'kg:1': 'Roy Veldes', 'kg:2': 'Ann Velde'})
        self.assertNotIn('participant_v', merges)

    def test_mangled_address_labels_resolve_to_the_address_owner(self):
        labels = {'participant_a': 'ongvacation c mail.com', 'participant_b': 'Zelda Hargraveilongvacationegmail.com',
                  'participant_c': 'longvacatlon u ail.com', 'participant_d': 'Zelda H. <longvacation gmail.com',
                  'participant_e': 'longvacationer mail.com',   # a different word, not the mailbox
                  'participant_f': 'Marcus Webber longvacationegmail.com'}  # another person's name in front
        emails = {'email_ref': 'longvacation@gmail.com'}
        merges = self.resolve(emails=emails, labels=labels, person_names={'kg:1': 'Zelda Hargrave'},
                              curated={'email_ref': (1, {'confidence': 'confirmed'})})
        for i in 'abcd':
            with self.subTest(i=i):
                self.assertEqual(merges.get(f'participant_{i}', (None,))[0], 'kg:1')
        self.assertNotIn('participant_f', merges)

    def test_extracted_mime_ids_get_private_safe_quality_overlays(self):
        values = ['image042.png@fixture47b7.test', 'image032.gif@fixture2a98.test', 'real@example.test']
        rows = [dict(entity_value=value) for value in values]
        nodes = build_communications.non_mailbox_contact_nodes(rows, b'fixture')
        self.assertEqual(len(nodes), 2)
        for node, value in zip(nodes, values):
            self.assertEqual(node['id'], build_communications.contact_id(value, 'email', b'fixture'))
            self.assertEqual(node['label_quality'], 'not_a_name')
            self.assertEqual(node['entity_type'], 'email')
        self.assertNotIn('@', json.dumps(nodes))
        self.assertEqual(len(rows), 3)  # An overlay does not delete source contacts.

    def test_transport_ids_cannot_become_a_person_through_headers(self):
        email = '20150706114344.8482.54721@computer.home'
        rows = [('kg:1', 'email_message', 'header_label_association')] * 10
        merges = self.resolve(emails={'email_message': email}, rows=rows,
                              person_names={'kg:1': 'Zelda Hargrave'})
        self.assertNotIn('email_message', merges)

    def test_mailbox_ties_dominance_and_vetoes(self):
        emails = {'email_a': 'longvacation@gmail.com', 'email_b': 'longvocation@gmail.com',
                  'email_v': 'longvecation@gmail.com'}
        rows = [('email_a', 'kg:1', 'sent_to')] * 100 + [('email_b', 'kg:1', 'sent_to')] * 100
        self.assertNotIn('email_v', self.resolve(emails=emails, rows=rows))
        self.assertNotIn('email_v', self.resolve(emails={'email_a': emails['email_a'], 'email_v': emails['email_v']},
                                               rows=rows, never=[['email_a', 'email_v']]))
        self.assertFalse(build_communications.address_family_merges(
            {'email_a': emails['email_a'], 'email_v': emails['email_v']},
            build_communications.Counter({'email_a': 90, 'email_v': 10})))

    def test_mailbox_variants_cannot_extend_a_fuzzy_chain(self):
        emails = {'email_a': 'abcdefghij@gmail.com', 'email_b': 'abcdefghxx@gmail.com',
                  'email_c': 'abcdefgxxx@gmail.com'}
        counts = build_communications.Counter({'email_a': 1000, 'email_b': 100, 'email_c': 10})
        result = build_communications.address_family_merges(emails, counts)
        self.assertEqual(result['email_b'][0], 'email_a')
        self.assertNotIn('email_c', result)
        self.assertEqual(result, build_communications.address_family_merges(dict(reversed(list(emails.items()))), counts))

    def test_a_dominant_direct_mailbox_anchor_beats_a_minor_reference(self):
        emails = {'email_a': 'abcdefghij@gmail.com', 'email_b': 'abcxyzghij@gmail.com',
                  'email_v': 'abcdyzghij@gmail.com'}
        counts = build_communications.Counter(email_a=100000, email_b=1000, email_v=10)
        result = build_communications.address_family_merges(emails, counts)
        self.assertEqual(result['email_v'][0], 'email_a')
        self.assertNotIn('email_b', result)  # Three edits: the references stay distinct.

    def test_mailbox_reviewed_different_is_a_transitive_veto(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / 'data/private/luna_review'
            private.mkdir(parents=True)
            (private / 'merges_test.json').write_text(json.dumps({
                'email_v|email_alias': {'verdict': 'different', 'confidence': 'high'}}))
            with patch.object(build_communications, 'ROOT', root):
                merges = self.resolve(emails={'email_ref': 'longvacation@gmail.com',
                                              'email_alias': 'longvacation@gmail.co',
                                              'email_v': 'longvaxation@gmail.com'},
                                      rows=[('email_ref', 'kg:1', 'sent_to')] * 100,
                                      curated={'email_alias': ('email_ref', {'confidence': 'confirmed'})})
            self.assertNotIn('email_v', merges)

    def test_contacts_absent_from_headers_share_the_same_address_resolver(self):
        key = b'fixture'
        builder = Builder(key, [])
        reference = builder.endpoint(None, 'longvacation@gmail.com')
        raw = 'longvaxation@sinail.com'
        values = {**builder.email_values, **build_communications.extracted_contact_addresses([
            {'entity_value': raw}], key)}
        member = build_communications.contact_id(raw, 'email', key)
        self.assertNotIn(member, builder.nodes)  # It only came from extracted contacts.
        merges = self.resolve(emails=values, rows=[(reference, 'kg:1', 'sent_to')] * 100)
        self.assertEqual(merges[member][0], reference)

    def test_popular_kg_families_do_not_promote_header_noise_to_people(self):
        variants = ['Zelda Hargrave cza', 'Zelda Hargrave ctl', 'Zelda Hargrave czi',
                    'Zelda Hargrave cti', 'Zelda Hargrave AOL', 'Zelda Hargrave clEa',
                    'Zelda Hargrave cl', 'Zelda Hargrave al', 'Zelda Hargrave La',
                    'Zelda Hargrave -la', 'Zelda Hargrave cO', 'Zelda Hargra ve',
                    'Zelda Hargrace', 'Zelda Hargraee', 'Zelda Hargrace ctl', 'Zelda Hargra v',
                    'Zelda Hargrace cl', 'Zelda Hargra v CC', 'Zelda Hargrave rnaillto',
                    'Zelda Hargrave lrnallto']
        labels = {f'participant_v{n}': label for n, label in enumerate(variants)}
        # These spurious two-word headers used to poison the global surname set.
        labels.update({f'participant_noise{n}': 'Other ' + tail
                       for n, tail in enumerate(('Cza', 'Ctl', 'Czi', 'Cti', 'AOL', 'ClEa'))})
        labels.update(participant_trusted='Rx Other Lark ctl', participant_clean='Other Lark')
        rows = [('kg:1', 'email_x', 'sent_to')] * 500
        rows += [(i, 'email_x', 'sent_to') for i in labels for _ in range(10)]
        merges = self.resolve(labels=labels, rows=rows, person_names={'kg:1': 'Zelda Hargrave',
                                                                   'kg:2': 'Another Lea', 'kg:3': 'Another Ito'},
                              review={'participants': {'participant_trusted': {'verdict': 'ok_person'}}},
                              spellings={i: [label] for i, label in labels.items()})
        for n in range(len(variants)):
            with self.subTest(variant=n):
                self.assertEqual(merges.get(f'participant_v{n}', (None,))[0], 'kg:1')

    def test_kg_spelling_context_preserves_different_people_and_cluster_vetoes(self):
        labels = {'participant_v': 'Zelda Hargrace', 'participant_alias': 'Zelda Hargrace'}
        rows = [('kg:1', 'email_shared', 'sent_to')] * 200
        rows += [('participant_v', 'email_shared', 'sent_to')] * 10
        # Sharing a recipient does not override different attested personal addresses.
        rows += [('kg:1', 'email_own', 'header_name_association'),
                 ('participant_v', 'email_other', 'header_name_association')] * 3
        merges = self.resolve(labels=labels, rows=rows, person_names={'kg:1': 'Zelda Hargrave'})
        self.assertNotEqual(merges.get('participant_v', (None,))[0], 'kg:1')
        shared = [('kg:1', 'email_shared', 'sent_to')] * 200
        shared += [('participant_v', 'email_shared', 'sent_to')] * 10
        for kwargs in ({'never': [['participant_alias', 'kg:1']],
                        'curated': {'participant_alias': ('participant_v', {'confidence': 'confirmed'})}},
                       {'person_names': {'kg:1': 'Zelda Hargrave', 'kg:2': 'Another Hargrace'}}):
            options = {'person_names': {'kg:1': 'Zelda Hargrave'}, **kwargs}
            merges = self.resolve(labels=labels, rows=shared, **options)
            self.assertNotEqual(merges.get('participant_v', (None,))[0], 'kg:1')

    def test_rare_attested_surname_cannot_follow_a_damaged_reference(self):
        labels = {'participant_v': 'Zelda Hargrace', 'participant_alias': 'Zelda Hargracee'}
        rows = [('kg:1', 'email_x', 'sent_to')] * 500
        rows += [('participant_alias', 'email_x', 'sent_to')] * 5
        rows += [('participant_v', 'email_x', 'sent_to')]
        merges = self.resolve(labels=labels, rows=rows,
                              person_names={'kg:1': 'Zelda Hargrave', 'kg:2': 'Another Hargrace'})
        self.assertEqual(merges.get('participant_alias', (None,))[0], 'kg:1')
        self.assertNotEqual(merges.get('participant_v', (None,))[0], 'kg:1')

    def test_dotted_surname_initial_requires_contacts_even_with_one_dominant_person(self):
        labels = {'participant_v': 'Zelda H.', 'participant_tail': 'Zelda H. Imailto',
                  'participant_bad': 'Zelda H. Other Smith'}
        rows = [('kg:1', 'email_x', 'sent_to')] * 500
        rows += [('participant_v', 'email_y', 'sent_to')] * 50
        options = dict(labels=labels, person_names={'kg:1': 'Zelda Hargrave', 'kg:2': 'Other Smith'})
        separate = self.resolve(rows=rows, **options)
        self.assertNotIn('participant_v', separate)
        self.assertNotIn('participant_tail', separate)
        backed = self.resolve(rows=rows + [('participant_v', 'email_a', 'header_name_association')] * 50,
                              emails={'email_a': 'zeldabox@example.test'},
                              curated={'email_a': (1, {'confidence': 'confirmed'})}, **options)
        self.assertEqual(backed['participant_v'][0], 'kg:1')
        self.assertEqual(backed['participant_tail'][0], 'kg:1')
        self.assertNotIn('participant_bad', backed)

    def test_initial_and_glued_damaged_mailbox_preserves_domain_and_owner(self):
        labels = {'participant_v': 'Zelda HMeadowboxaexample.testj',
                  'participant_bad': 'Zelda HMeadowboxaOtherSmith'}
        merges = self.resolve(labels=labels, emails={'email_a': 'meadowbox@example.test'},
                              curated={'email_a': (1, {'confidence': 'confirmed'})},
                              person_names={'kg:1': 'Zelda Hargrave', 'kg:2': 'Other Smith'})
        self.assertEqual(merges.get('participant_v', (None,))[0], 'kg:1')
        self.assertNotIn('participant_bad', merges)

    def test_literal_surname_gap_beats_an_optional_middle_initial_tail(self):
        labels = {'participant_v': 'Zelda H argrave CC', 'participant_split': 'Zelda H argrave',
                  'participant_other': 'Zelda Argrave'}
        rows = [('kg:1', 'email_x', 'sent_to')] * 500
        rows += [(i, 'email_x', 'sent_to') for i in labels for _ in range(10)]
        merges = self.resolve(labels=labels, rows=rows,
                              person_names={'kg:1': 'Zelda Hargrave', 'kg:2': 'Another Argrave'})
        self.assertEqual(merges.get('participant_v', (None,))[0], 'kg:1')
        self.assertEqual(merges.get('participant_split', (None,))[0], 'kg:1')
        self.assertNotEqual(merges.get('participant_other', (None,))[0], 'kg:1')
        dotted = self.resolve(labels={**labels, 'participant_dotted': 'Zelda H. Argrave'},
                              rows=rows + [('participant_dotted', 'email_y', 'sent_to')] * 10,
                              person_names={'kg:1': 'Zelda Hargrave', 'kg:2': 'Another Argrave'})
        self.assertNotEqual(dotted.get('participant_dotted', (None,))[0], 'kg:1')

    def test_frequent_ligature_fragment_needs_context_and_cannot_poison_a_tail(self):
        labels = {'participant_v': 'Zelda Hargm', 'participant_tail': 'Zelda Hargm CC'}
        rows = [('kg:1', 'email_x', 'sent_to')] * 500
        rows += [(i, 'email_x', 'sent_to') for i in labels for _ in range(10)]
        merges = self.resolve(labels=labels, rows=rows, person_names={'kg:1': 'Zelda Hargint'})
        self.assertEqual(merges.get('participant_v', (None,))[0], 'kg:1')
        self.assertEqual(merges.get('participant_tail', (None,))[0], 'kg:1')
        separate = self.resolve(labels=labels, rows=[('kg:1', 'email_x', 'sent_to')] * 500
                                + [('participant_v', 'email_y', 'sent_to')] * 10,
                                person_names={'kg:1': 'Zelda Hargint'})
        self.assertNotIn('participant_v', separate)

    def test_person_self_descriptions_keep_organizations_and_other_mailboxes_apart(self):
        variants = ['Zelda Hargrave Office', 'Zelda Hargrave - Friend of Monarch',
                    'Zelda Hargrave VI Foundation', 'Zelda Hargrave Assistant',
                    'Zelda Hargrave Co', 'Zelda Hargrave Other Smith', 'Zelda Hargrave Lea', 'Zelda Hargrave Ito']
        labels = {f'participant_v{n}': label for n, label in enumerate(variants)}
        merges = self.resolve(labels=labels, person_names={'kg:1': 'Zelda Hargrave', 'kg:2': 'Other Smith',
                                                          'kg:3': 'Another Lea', 'kg:4': 'Another Ito'},
                              rows=[('kg:1', 'email_x', 'sent_to')] * 100
                              + [(i, 'email_x', 'sent_to') for i in labels for _ in range(10)],
                              spellings={i: [label] for i, label in labels.items()})
        for n in range(2):
            self.assertEqual(merges.get(f'participant_v{n}', (None,))[0], 'kg:1')
        for n in range(2, len(variants)):
            self.assertNotEqual(merges.get(f'participant_v{n}', (None,))[0], 'kg:1')

    def test_automated_office_review_does_not_override_person_label_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / 'data/private/luna_review'
            private.mkdir(parents=True)
            (private / 'merges_test.json').write_text(json.dumps({
                '1|participant_office': {'verdict': 'different', 'confidence': 'high'},
                '1|participant_other': {'verdict': 'different', 'confidence': 'high'}}))
            labels = {'participant_office': 'Zelda Hargrave Office', 'participant_other': 'Zelda Hargrace'}
            options = dict(labels=labels, person_names={'kg:1': 'Zelda Hargrave'},
                           rows=[('kg:1', 'email_x', 'sent_to')] * 100
                           + [(i, 'email_x', 'sent_to') for i in labels for _ in range(10)])
            with patch.object(build_communications, 'ROOT', root):
                merges = self.resolve(**options)
                self.assertEqual(merges['participant_office'][0], 'kg:1')
                self.assertNotIn('participant_other', merges)
                explicit = self.resolve(never=[['participant_office', 'kg:1']], **options)
                self.assertNotIn('participant_office', explicit)

    def test_family_arbitrary_tails_and_kg_canonical(self):
        variants = ['Zelda Hargrave leevacationegmailcom', 'Zelda Hargrave leevacationegmail.com',
                    'Zelda Hargrave leevacation',
                    'Zelda Hargrave imailto', 'Zelda Hargrave Mila', 'Zelda Hargrave MINIM',
                    'Zelda Hargraveill', 'Zelda Hargravem',
                    'Zelda Hargrave zzz qqq vvv zzz qqq vvv zzz']
        labels = {'participant_ref': 'Zelda Hargrave'}
        labels.update({f'participant_v{n}': label for n, label in enumerate(variants)})
        rows = [('participant_ref', 'email_x', 'sent_to')] * 100
        rows += [(i, 'email_x', 'sent_to') for i in labels for _ in range(5)]
        merges = self.resolve(labels=labels, rows=rows, person_names={'kg:1': 'Zelda A. Hargrave'})
        for identity in labels:
            self.assertEqual(merges.get(identity, (None,))[0], 'kg:1', labels[identity])

    def test_family_long_tails_preserve_second_people_and_vetoes(self):
        labels = {'participant_ref': 'Zelda Hargrave', 'participant_other': 'Other Smith',
                  'participant_a': 'Zelda Hargrave Other Smith',
                  'participant_b': 'Zelda Hargraveothersmith',
                  'participant_c': 'Zelda Hargrave xyz Roe',
                  'participant_d': 'Zelda Hargravexyzroe',
                  'participant_e': 'Zelda Hargrave xyz Roe qqq',
                  'participant_f': 'Zelda Hargrave Smith Jr',
                  'participant_v': 'Zelda Hargrave longjunklongjunk'}
        rows = [('participant_ref', 'email_x', 'sent_to')] * 100
        rows += [('participant_other', 'email_x', 'sent_to')] * 10
        merges = self.resolve(labels=labels, rows=rows, person_names={'kg:2': 'Another Roe'},
                              never=[['participant_v', 'participant_ref']])
        for identity in ('participant_a', 'participant_b', 'participant_c', 'participant_d', 'participant_e', 'participant_f', 'participant_v'):
            self.assertNotEqual(merges.get(identity, (None,))[0], 'participant_ref')

    def test_family_short_truncations_require_unique_name_and_context(self):
        labels = {'participant_ref': 'Zelda Hargrave', 'participant_v': 'Zelda Har'}
        rows = [('participant_ref', 'email_x', 'sent_to')] * 20
        for extra in ([], [('participant_v', 'email_other', 'sent_to')]):
            self.assertNotIn('participant_v', self.resolve(labels=labels, rows=rows + extra))
        shared = rows + [('participant_v', 'email_x', 'sent_to')] * 5
        self.assertEqual(self.resolve(labels=labels, rows=shared)['participant_v'][0], 'participant_ref')
        ambiguous = {**labels, 'participant_other': 'Zelda Harper'}
        self.assertNotIn('participant_v', self.resolve(labels=ambiguous, rows=shared +
                         [('participant_other', 'email_x', 'sent_to')] * 20))
        self.assertNotIn('participant_v', self.resolve(labels=ambiguous, rows=rows +
                         [('participant_v', 'email_x', 'sent_to')]
                         + [('participant_other', 'email_other', 'sent_to')] * 20))

    def test_family_truncation_can_share_only_a_document(self):
        db = self.enterContext(contextlib.closing(sqlite3.connect(':memory:')))
        db.execute('CREATE TABLE evidence(source_id,target_id,relationship_type,efta_number)')
        db.executemany('INSERT INTO evidence VALUES (?,?,?,?)',
                       [('participant_ref', 'email_x', 'sent_to', 'D1')] * 20
                       + [('email_y', 'participant_v', 'sent_to', 'D1')])
        with patch.object(build_communications, 'node_review', return_value={'participants': {}}):
            merges = build_communications.resolve_identities(db, {}, {}, participant_labels={
                'participant_ref': 'Zelda Hargrave', 'participant_v': 'Zelda Har'},
                person_names={'kg:1': 'Zelda A. Hargrave'})
        self.assertEqual(merges['participant_v'][0], 'kg:1')

    def test_scan_consensus_alias_merges_through_guards(self):
        # Two OCR engines read 'Qorvalda Pinsk' where the text layer had 'Qorva1da Pinsk'.
        db = self.enterContext(contextlib.closing(sqlite3.connect(':memory:')))
        db.execute('CREATE TABLE evidence(source_id,target_id,relationship_type,efta_number)')
        db.executemany('INSERT INTO evidence VALUES (?,?,?,?)', [('participant_ref', 'email_x', 'sent_to', 'D1')] * 5
                       + [('participant_v', 'email_y', 'sent_to', 'D2')])
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(tmp) / 'ocr_consensus.json'
        page = {'doc_id': 'EFTA00000001', 'page_id': 1, 'page': 0, 'engines': ['tesseract', 'rapidocr']}
        path.write_text(json.dumps({'version': 1, 'variants': {'Qorva1da Pinsk': {'consensus': 'Qorvalda Pinsk', 'pages': [page]}}}))
        with patch.object(_ocr_consensus, 'PATH', path), \
                patch.object(build_communications, 'node_review', return_value={'participants': {}}):
            merges = build_communications.resolve_identities(db, {}, {}, participant_labels={
                'participant_ref': 'Qorvalda Pinsk', 'participant_v': 'Qorva1da Pinsk'})
        self.assertEqual(merges['participant_v'][:2], ('participant_ref', 'ocr_scan_consensus'))

    def test_scan_consensus_merges_a_frequent_misreading(self):
        # 'Corin Wexlin' (19 records, a frequent name itself) is a scan-confirmed misreading of 'Corin Wextin' (1869).
        db = self.enterContext(contextlib.closing(sqlite3.connect(':memory:')))
        db.execute('CREATE TABLE evidence(source_id,target_id,relationship_type,efta_number)')
        db.executemany('INSERT INTO evidence VALUES (?,?,?,?)', [('participant_ref', 'email_x', 'sent_to', f'D{i}') for i in range(60)]
                       + [('participant_v', 'email_y', 'sent_to', f'E{i}') for i in range(40)])
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(tmp) / 'ocr_consensus.json'
        page = {'doc_id': 'EFTA00000001', 'page_id': 1, 'page': 0, 'engines': ['tesseract', 'rapidocr']}
        path.write_text(json.dumps({'version': 1, 'variants': {'Qorvin Marlin': {'consensus': 'Qorvin Martin', 'pages': [page]}}}))
        with patch.object(_ocr_consensus, 'PATH', path), \
                patch.object(build_communications, 'node_review', return_value={'participants': {}}):
            merges = build_communications.resolve_identities(db, {}, {}, participant_labels={
                'participant_ref': 'Qorvin Martin', 'participant_v': 'Qorvin Marlin'})
        self.assertEqual(merges['participant_v'][0], 'participant_ref')

    def test_dominant_spelling_only_absorbs_misreadings(self):
        ok = build_communications.dominant_spelling_ok
        self.assertTrue(ok('header_name_one_edit', 'Rikard Markin', 'Rikard Merkin'))
        self.assertTrue(ok('header_name_ocr_family:surname_edits', 'Teodor Ne us', 'Teodor Negus'))
        self.assertTrue(ok('header_name_truncated', 'yn Halvorsen', 'Lyn Halvorsen'))
        self.assertTrue(ok('header_name_is_address', 'Ma ur Quorbin', 'Mayur Quorbin'))
        self.assertFalse(ok('header_name_one_edit', 'Teodor Mailer', 'Teodor Miller'))  # two real surnames
        self.assertFalse(ok('header_name_one_edit', 'Teodor Haig', 'Teodor Hall'))
        self.assertFalse(ok('header_name_truncated', 'Anna Halvorsen', 'Brianna Halvorsen'))  # a real given name
        self.assertFalse(ok('header_name_is_address', 'Zorro Office', 'Quilla Gordon'))  # no shared word
        self.assertFalse(ok('header_name_ocr_family:initial', 'Purl V', 'Viren Purl'))

    def test_frequent_misspelling_merges_into_far_more_frequent_spelling(self):
        # The OCR spelling is repeated through one thread (a 'reference' itself);
        # the correct spelling has >= 3x the records.
        labels = {'participant_ref': 'Quorin Halvorsen', 'participant_v': 'Quorin Halvorsem'}
        rows = [('participant_ref', 'email_x', 'sent_to')] * 40 + [('participant_v', 'email_y', 'sent_to')] * 8
        merges = self.resolve(labels=labels, rows=rows)
        self.assertEqual(merges['participant_v'][0], 'participant_ref')

    def test_family_frequent_misspelling_yields_to_correct_kg_name(self):
        labels = {'participant_ref': 'Lesley Groff', 'participant_typo': 'Lesley Grofr',
                  'participant_v': 'Lesley Grofr longjunklongjunk'}
        rows = [('participant_ref', 'email_x', 'sent_to')] * 100
        rows += [('participant_typo', 'email_x', 'sent_to')] * 5
        merges = self.resolve(labels=labels, rows=rows, person_names={'kg:1': 'Lesley Groff'})
        self.assertEqual(merges['participant_typo'][0], 'kg:1')
        self.assertEqual(merges['participant_v'][0], 'kg:1')
        merges = self.resolve(labels=labels, rows=rows)
        self.assertEqual(merges['participant_typo'][0], 'participant_ref')
        self.assertEqual(merges['participant_v'][0], 'participant_ref')

    def test_family_initial_plus_damaged_address_uses_resolved_owner(self):
        labels = {'participant_a': 'Zelda H. leevacationegmail.com',
                  'participant_b': 'Zelda H leevacation',
                  'participant_ref': 'Zelda H leevacation',
                  'participant_c': 'Zelda Hargrave leevacation',
                  'participant_other': 'Other Vacation',
                  'participant_bad': 'Zelda H totallyunrelated'}
        merges = self.resolve(labels=labels, emails={'email_a': 'jeevacation@gmail.com'},
                              curated={'email_a': (1, {'confidence': 'confirmed'})},
                              person_names={'kg:1': 'Zelda Hargrave'},
                              rows=[('participant_a', 'email_x', 'sent_to')] * 5
                              + [('participant_ref', 'email_x', 'sent_to')] * 20
                              + [('participant_other', 'email_x', 'sent_to')] * 5)
        self.assertEqual(merges['participant_a'][0], 'kg:1')
        self.assertEqual(merges['participant_b'][0], 'kg:1')
        self.assertEqual(merges['participant_ref'][0], 'kg:1')
        self.assertEqual(merges['participant_c'][0], 'kg:1')
        self.assertNotIn('participant_bad', merges)

    def test_family_full_kg_spelling_beats_frequent_partial_reference(self):
        labels = {'participant_ref': 'Zelda Hargrave', 'participant_prefix': 'Zelda Har',
                  'participant_other': 'Zelda Harper', 'participant_v': 'Zelda Hargrave longjunklongjunk'}
        rows = [(i, 'email_x', 'sent_to') for i in labels for _ in range(10)]
        merges = self.resolve(labels=labels, rows=rows, person_names={'kg:1': 'Zelda Hargrave'})
        self.assertEqual(merges['participant_ref'][0], 'kg:1')
        self.assertEqual(merges['participant_v'][0], 'kg:1')
        self.assertNotIn('participant_prefix', merges)

    def test_frequent_ocr_families_cover_header_damage(self):
        variants = ['Zelda Ha rove', 'Zelda Har rove', 'Zelda Hargrov', 'Zelda Hargr',
                    'Zelda Hargroyea', 'Zelda Hargrovel', 'Zelda Hargroverel', 'Zelda Hargi lLI',
                    'Hargrove, Zelda', 'Hargrove Zelda', 'Zelda H a rgrove', 'Z. Hargrove']
        variants += ['Zelda Hargrove ' + tail for tail in ['C', 'ctl', 'cci', 'ccl', 'ca', 'czi', 'SF', '-a', 'a']]
        labels = {'participant_ref': 'Zelda Hargrove'}
        labels.update({f'participant_v{n}': label for n, label in enumerate(variants)})
        rows = [('participant_ref', 'email_x', 'sent_to')] * 50
        rows += [(i, 'email_x', 'sent_to') for i in labels if i != 'participant_ref']
        merges = self.resolve(labels=labels, rows=rows)
        for i in labels.keys() - {'participant_ref'}:
            self.assertEqual(merges.get(i, (None,))[0], 'participant_ref', labels[i])
            self.assertTrue(merges[i][1].startswith('header_name_ocr_family:'), labels[i])

    def test_family_visual_confusions_spacing_apostrophes_and_initials(self):
        pairs = [("Zelda O'Neill", 'Zelda 0 Ne1ll'), ('Zelda Carmody', 'Zelda Carrnody'),
                 ('Zelda Danner', 'Zelda Clanner'), ('Zelda Heaton', 'Zelda Lieaton'),
                 ('Zelda Hargrave', 'Zelda M. Hargrave')]
        pairs += [('Zelda Lillian', 'Zelda Iillian')]
        for reference, variant in pairs:
            with self.subTest(reference=reference, variant=variant):
                merges = self.resolve(labels={'participant_ref': reference, 'participant_v': variant},
                                      rows=[('participant_ref', 'email_x', 'sent_to')] * 20)
                self.assertEqual(merges.get('participant_v', (None,))[0], 'participant_ref')

    def test_frequent_equivalent_spellings_keep_the_most_frequent_form(self):
        labels = {'participant_a': 'Zelda Hillman', 'participant_b': 'Zelda Hi1lrnan'}
        rows = [('participant_a', 'email_x', 'sent_to')] * 50 + [('participant_b', 'email_x', 'sent_to')] * 5
        merges = self.resolve(labels=labels, rows=rows)
        self.assertEqual(merges['participant_b'][0], 'participant_a')
        self.assertNotIn('participant_a', merges)

    def test_family_safeguards_survive_legacy_name_rules(self):
        cases = [
            # The second equally close frequent surname prevents a tie-break by frequency.
            ({'participant_a': 'Zelda Hargrave', 'participant_b': 'Zelda Hargravy',
              'participant_v': 'Zelda Hargrav'}, {'participant_a': 100, 'participant_b': 5}, {}),
            # A frequent spelling is itself a different full name, even at 100:5.
            ({'participant_a': 'Zelda Hargrave', 'participant_v': 'Zelda Hargrace'},
             {'participant_a': 100, 'participant_v': 5}, {}),
            # Reviewed people are protected from ordinary edits / truncated surnames.
            ({'participant_a': 'Zelda Hargrave', 'participant_v': 'Zelda Hargrace'},
             {'participant_a': 100}, {'participant_v': {'verdict': 'ok_person'}}),
            ({'participant_a': 'Zelda Hargrave', 'participant_v': 'Zelda Hargr'},
             {'participant_a': 100}, {'participant_v': {'verdict': 'ok_person'}}),
            ({'participant_a': 'Zelda Weatherford', 'participant_v': 'Zelda Weat'}, {'participant_a': 100}, {}),
            ({'participant_a': 'Zelda B Hargrave', 'participant_v': 'Zelda C Hargrave'}, {'participant_a': 100}, {}),
            ({'participant_a': 'Zelda Ang', 'participant_v': 'Zelda Ng'},
             {'participant_a': 100, 'participant_v': 5}, {}),
            # Roles, legal/generational suffixes and a real second surname stay separate.
            ({'participant_a': 'Zelda Hargrave', 'participant_v': 'Zelda Hargrave Jr'}, {'participant_a': 100}, {}),
            ({'participant_a': 'Zelda Hargrave', 'participant_v': 'Zelda Hargrave LLC'}, {'participant_a': 100}, {}),
            ({'participant_a': 'Zelda Hargrave', 'participant_v': 'Zelda Hargrave Lee',
              'participant_b': 'Other Lee'}, {'participant_a': 100, 'participant_b': 5}, {}),
            # Initials cannot select between two frequent given names with the same surname.
            ({'participant_a': 'Zelda Hargrave', 'participant_b': 'Zara Hargrave', 'participant_v': 'Z Hargrave'},
             {'participant_a': 100, 'participant_b': 5}, {}),
        ]
        for labels, counts, review in cases:
            with self.subTest(labels=labels):
                rows = [(i, 'email_x', 'sent_to') for i, count in counts.items() for _ in range(count)]
                merges = self.resolve(labels=labels, rows=rows, review={'participants': review})
                self.assertNotIn('participant_v', merges)

    def test_reviewed_ocr_equal_surname_can_join_a_frequent_name(self):
        merges = self.resolve(labels={'participant_a': 'Zelda Hillman', 'participant_v': 'Zelda Hi1lrnan'},
                              rows=[('participant_a', 'email_x', 'sent_to')] * 20,
                              review={'participants': {'participant_v': {'verdict': 'ok_person'}}})
        self.assertEqual(merges['participant_v'][0], 'participant_a')

    def test_family_unique_nearest_and_frozen_references(self):
        labels = {'participant_a': 'Zelda Hargrave', 'participant_b': 'Zelda Hargrace',
                  'participant_v': 'Zelda Hargracee'}
        rows = [('participant_a', 'email_x', 'sent_to')] * 100 + [('participant_b', 'email_x', 'sent_to')] * 5
        first = self.resolve(labels=labels, rows=rows)
        second = self.resolve(labels=dict(reversed(list(labels.items()))), rows=list(reversed(rows)))
        self.assertEqual(first, second)
        # The second full surname is within two edits, so even a unique
        # nearest one-edit reading now stays unresolved.
        self.assertNotIn('participant_v', first)
        self.assertNotIn('participant_b', first)

    def test_family_threshold_and_vetoes(self):
        labels = {'participant_a': 'Zelda Hargrave', 'participant_v': 'Zelda Ha rave'}
        self.assertNotIn('participant_v', self.resolve(labels=labels, rows=[('participant_a', 'email_x', 'sent_to')] * 4))
        self.assertNotIn('participant_v', self.resolve(labels={**labels, 'participant_v': 'Zelda Hargr'},
                                                     rows=[('participant_a', 'email_x', 'sent_to')] * 4))
        rows = [('participant_a', 'email_x', 'sent_to')] * 20
        self.assertNotIn('participant_v', self.resolve(labels=labels, rows=rows,
                                                     never=[['participant_v', 'participant_a']]))
        # Review vetoes also protect a non-canonical cluster member transitively.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / 'data/private/luna_review'
            private.mkdir(parents=True)
            (private / 'merges_test.json').write_text(json.dumps({
                'participant_v|participant_alias': {'verdict': 'different', 'confidence': 'high'}}))
            labels['participant_alias'] = 'Zelda Hargrave'
            with patch.object(build_communications, 'ROOT', root):
                merges = self.resolve(labels=labels, rows=rows,
                                      curated={'participant_alias': ('participant_a', {'confidence': 'confirmed'})})
            self.assertNotIn('participant_v', merges)

    def test_short_name_with_reviewed_address_outside_import(self):
        merges = self.resolve(labels={'participant_short': 'gmax'},
                              emails={'email_a': 'gmax@corp.test', 'email_b': 'other@corp.test'},
                              curated={'email_b': ('email_missing', {'confidence': 'confirmed'})},
                              rows=[('participant_short', 'email_a', 'header_name_association')] * 5
                              + [('participant_short', 'email_b', 'header_name_association')])
        self.assertEqual(merges['participant_short'][0], 'email_a')

    def test_frequent_reference_is_not_redirected_by_a_later_fuzzy_rule(self):
        entities = [dict(id=1, name='Zelda Hargraves', entity_type='person')]
        merges = self.resolve(labels={'participant_ref': 'Zelda Hargrace', 'participant_v': 'Zelda Hargrace CC'},
                              person_names={'kg:1': 'Zelda Hargraves'},
                              matcher=build_communications.PersonMatcher(entities),
                              rows=[('participant_ref', 'email_x', 'sent_to')] * 20
                              + [('participant_v', 'email_x', 'sent_to')])
        self.assertEqual(merges['participant_v'][0], 'participant_ref')
        self.assertNotIn('participant_ref', merges)

    def test_trailing_fragment_merges_into_the_name(self):
        f = build_communications.strip_trailing_fragments
        for written, name in [('Lesley Groff cza', 'Lesley Groff'), ('Jeffrey Epstein CC cj', 'Jeffrey Epstein'),
                              ('Darian Keldry AOL', 'Darian Keldry'), ('Lesley Groff halltgiMMI', 'Lesley Groff'),
                              ('Paul Berwick imailto', 'Paul Berwick'), ('Gary Vellor ..tj', 'Gary Vellor'),
                              ('Ian Osborne a', 'Ian Osborne')]:
            self.assertEqual(f(written), name)
        for kept in ['Shing Tung Yau', 'Maria Ellyn Li', 'Lesley Groff Mia', 'Bill Gates foundation', 'Ronald McDonald',
                     "Conan O'Brien", 'Terje Rod-Larsen', 'JOHN SMITH JR', 'Joseph Kennedy III', 'John Smith IV',
                     'Robert Jones Jr.', 'Henry Ford II']:
            self.assertEqual(f(kept), kept)
        self.assertEqual(f('Shing Tung Yau C'), 'Shing Tung Yau')
        labels = {'participant_a': 'lesley groff cza', 'participant_b': 'paul berwick', 'participant_c': 'paul berwick a',
                  'participant_d': 'maria ellyn li', 'participant_e': 'maria ellyn'}
        spellings = {'participant_a': {'Lesley Groff cza'}, 'participant_c': {'Paul Berwick a'}, 'participant_d': {'Maria Ellyn Li'}}
        merges = self.resolve(labels=labels, person_names={'kg:1': 'Lesley Groff'}, spellings=spellings)
        self.assertEqual(merges['participant_a'][:2], ('kg:1', 'header_name_ocr_family:long_tail'))
        self.assertEqual(merges['participant_c'][:2], ('participant_b', 'header_name_trailing_fragment'))
        self.assertNotIn('participant_d', merges)

    def test_name_tail_merges_into_the_name(self):
        f = build_communications.strip_name_tail
        for written, name in [('Sigrid S. McCawley - Boies', 'Sigrid S. McCawley'), ('Ykeh Nma I Cellist', 'Ykeh Nma'),
                              ('ELAINE MCVELLON I INTERIOR DESIGN', 'ELAINE MCVELLON'), ('JESSICA WREN II CO-FOUNDER', 'JESSICA WREN'),
                              ('John Veah Architect', 'John Veah'), ('Eva Dubin czli', 'Eva Dubin'),
                              ('Lesley Groff MINIM', 'Lesley Groff'), ('Larry Visoski Lar', 'Larry Visoski')]:
            self.assertEqual(f(written)[0], name)
        for kept in ['Shing Tung Yau', 'Tavia Vellam Coleman', 'Lisa Emelia Svensson', 'Woody Allen Assistant',
                     'Yuri Milner Office', 'Peggy Siegal Company', 'Philip Levine for Mayor', 'ANTOINE VERGLAS STUDIO',
                     'DARIAN K. KELDRY PLLC', 'Bill Gates foundation', 'Henry Ford II', 'Martin B. O\'Connor II',
                     'george church genetics', 'Sam Varell Boris Friend', 'Dean Kamen - Admin', 'Maria Ellyn Lin',
                     'not sure I understand', 'Until then I will', 'Matthew Vela - on behalf of']:
            self.assertIsNone(f(kept), kept)
        labels = {'participant_a': 'sigrid s. mccawley - boies', 'participant_b': 'sigrid mccawley',
                  'participant_c': 'woody allen assistant', 'participant_d': 'woody allen',
                  'participant_e': 'paul smith i partner',
                  'participant_h': 'jane roe - john doe', 'participant_i': 'jane roe', 'participant_j': 'john doe'}
        spellings = {'participant_a': {'Sigrid S. McCawley - Boies'}, 'participant_c': {'Woody Allen Assistant'},
                     'participant_e': {'Paul Smith I Partner'}, 'participant_h': {'Jane Roe - John Doe'}}
        labels.update({'participant_k': 'jvengu bdorganl abascal', 'participant_l': 'jvengu bdorganl',
                       'participant_m': 'eva dubin czli', 'participant_n': 'eva dubin', 'participant_o': 'ann lee czli',
                       'participant_p': 'sunday night - perfect', 'participant_q': 'sunday night'})
        spellings.update({'participant_k': {'Jvengu Bdorganl abascal'}, 'participant_m': {'Eva Dubin czli'},
                          'participant_o': {'Ann Lee czli'}, 'participant_p': {'sunday night - perfect'}})
        merges = self.resolve(labels=labels, spellings=spellings, person_names={
            'kg:1': 'Paul Smith', 'kg:2': 'Paul A. Smith', 'kg:3': 'Sigrid Roe', 'kg:4': 'Eva Roe', 'kg:5': 'Jimena Roe', 'kg:6': 'Eva Moe', 'kg:7': 'John Doe'})
        self.assertEqual(merges['participant_a'][:2], ('participant_b', 'header_name_tail:separator'))
        self.assertNotIn('participant_c', merges)
        self.assertNotIn('participant_e', merges)  # two Paul Smiths
        self.assertNotIn('participant_h', merges)  # two people
        self.assertEqual(merges['participant_m'][:2], ('participant_n', 'header_name_tail:lowercase_noise'))  # 'czli' twice
        self.assertNotIn('participant_k', merges)  # a second surname, written once
        self.assertNotIn('participant_p', merges)  # no given name, not capitalized

    def test_look_alike_spellings(self):
        f = build_communications.confusable_equal
        self.assertTrue(f('lavon Al Veyra', 'Javon Al Veyra'))
        self.assertTrue(f('Etik Wexdrick', 'Erik Wexdrick'))
        self.assertFalse(f('Evan Teth', 'Evan Reth'))
        self.assertFalse(f('Mailer', 'Miller'))
        self.assertFalse(f('Javon', 'javon'))

    def test_greeting_and_tag_prefixes_merge_into_the_name(self):
        f = build_communications.strip_name_head
        for written, name in [('Hello Lynne', 'Lynne'), ('Thx Liam', 'Liam'), ('EXTERNAL-Jonah Veldon', 'Jonah Veldon'),
                              ('Cc Ray Wenton', 'Ray Wenton'), ('-Napoleon Hill', 'Napoleon Hill'), ('xoxo Peggy', 'Peggy'),
                              ('p.o. Gisela Wreyton', 'Gisela Wreyton'), ('Alert - Louella', 'Louella')]:
            self.assertEqual(f(written), name)
        for kept in ['Hillary Clinton', 'Ray Wenton', 'Hi there', 'Dearborn Capital', 'Cecil Brown', 'Hi-Tech Concrete Pumping, LLC',
                     'HI LP', 'P.O. Box', 'Ccosta Real']:
            self.assertIsNone(f(kept), kept)
        labels = {'participant_a': 'hello lynne', 'participant_b': 'lynne', 'participant_c': 'external-jonah veldon',
                  'participant_d': 'jonah veldon', 'participant_e': 'dear zed',
                  'participant_h': 'cc ray wenton'}
        spellings = {'participant_a': {'Hello Lynne'}, 'participant_c': {'EXTERNAL-Jonah Veldon'}, 'participant_e': {'Dear Zed'},
                     'participant_h': {'Cc Ray Wenton'}}
        merges = self.resolve(labels=labels, spellings=spellings, person_names={'kg:1': 'Ray Wenton'})
        self.assertEqual(merges['participant_a'][:2], ('participant_b', 'header_name_head_noise'))
        self.assertEqual(merges['participant_c'][:2], ('participant_d', 'header_name_head_noise'))
        self.assertEqual(merges['participant_h'][0], 'kg:1')
        self.assertNotIn('participant_e', merges)  # no 'Zed' node

    def test_company_without_legal_form_merges_into_the_company(self):
        labels = {'participant_a': 'hbrk associates', 'participant_b': 'jeff koons', 'participant_c': 'southern financial',
                  'participant_d': 'jsc interiors llc', 'participant_e': 'jsc interiors inc'}
        names = {'kg:1': 'HBRK Associates, Inc.', 'kg:2': 'Jeff Koons LLC', 'kg:3': 'Southern Financial LLC',
                 'kg:4': 'Southern Financial, Inc.', 'kg:5': 'JSC Interiors, LLC'}
        merges = self.resolve(labels=labels, entity_names=names)
        self.assertEqual(merges['participant_a'][:2], ('kg:1', 'header_name_company_legal_form'))
        self.assertEqual(merges['participant_d'][0], 'kg:5')
        for kept in ('participant_b', 'participant_c', 'participant_e'):  # owner, two forms, another form
            self.assertNotIn(kept, merges)

    def test_header_name_reviewed_into_a_company_node_keeps_it_canonical(self):
        labels = {'participant_a': 'HBRK Associates', 'participant_b': 'HBRK ASSOCIATES'}
        curated = {'participant_a': ('company_x', {'confidence': 'probable'})}
        rows = [('participant_b', 'email_x', 'sent_to')] * 5  # more records than the company node
        merges = self.resolve(labels=labels, curated=curated, rows=rows)
        self.assertNotIn('company_x', merges)
        self.assertEqual(merges['participant_b'][:2], ('company_x', 'header_name_ocr_skeleton'))

    def test_one_word_one_edit_needs_a_cut_ending_or_a_long_word(self):
        labels = {'participant_andrea': 'Andrea', 'participant_andrew': 'Andrew',
                  'participant_anthon': 'Anthon', 'participant_anthony': 'Anthony',
                  'participant_marg': 'Margaretha', 'participant_marge': 'Margarethe'}
        rows = [(i, 'email_x', 'sent_to') for i, n in [('participant_andrea', 1), ('participant_andrew', 20),
                ('participant_anthon', 1), ('participant_anthony', 20), ('participant_marg', 1), ('participant_marge', 20)]
                for _ in range(n)]
        merges = self.resolve(labels=labels, rows=rows)
        self.assertNotIn('participant_andrea', merges)  # another given name, not a misread
        self.assertEqual(merges['participant_anthon'][:2], ('participant_anthony', 'header_name_one_edit'))
        self.assertEqual(merges['participant_marg'][:2], ('participant_marge', 'header_name_one_edit'))

    def test_reviewed_rules_are_fixed_codes(self):
        secret = 'private@corp.test +1-212-555-0199'
        merges = self.resolve(
            curated={'email_private': (1, {'reason': secret, 'confidence': 'confirmed'})},
            review={'participants': {'participant_private': {'merge_into': 1, 'verdict': secret}}})
        self.assertEqual(merges['email_private'][1], 'reviewed_identity_merge')
        self.assertEqual(merges['participant_private'][1], 'reviewed_audit_merge')
        self.assertNotIn(secret, json.dumps(merges))

    def test_never_merge_blocks_transitive_reviewed_and_automatic_links(self):
        item = {'reason': 'test', 'confidence': 'confirmed'}
        cases = [
            # A reviewed chain cannot connect the forbidden endpoints.
            dict(curated={'email_a': ('email_b', item), 'email_b': ('email_c', item)},
                 never=[['email_a', 'email_c']]),
            # An OCR-equivalent address cannot enter a forbidden reviewed cluster.
            dict(curated={'email_a': (1, item)},
                 emails={'email_a': 'same@gmail.com', 'email_b': 's.ame@gmail.com'},
                 never=[['email_b', 'kg:1']]),
            # A late association must check all members, not just its root.
            dict(curated={'email_a': ('email_b', item)},
                 rows=[('kg:1', 'email_b', 'header_label_association')] * 3,
                 never=[['email_a', 'kg:1']]),
            # Audit and graph-name matches obey the same guard; integer KG IDs work.
            dict(review={'participants': {'participant_a': {'merge_into': 1, 'verdict': 'same'}}},
                 never=[['participant_a', 1]]),
            dict(labels={'participant_a': 'Jane Smith'},
                 matcher=build_communications.PersonMatcher([dict(id=1, name='Jane Smith', entity_type='person')]),
                 never=[['participant_a', 'kg:1']]),
            # Skeleton clusters cannot evade a forbidden non-canonical pair.
            dict(labels={'participant_a': 'Alice Jones', 'participant_b': 'Jones Alice',
                         'participant_c': 'Alice Jones'},
                 never=[['participant_a', 'participant_b']]),
        ]
        for case in cases:
            with self.subTest(case=case):
                merges = self.resolve(**case)
                for a, b in case['never']:
                    a, b = (f'kg:{v}' if isinstance(v, int) else v for v in (a, b))
                    self.assertNotEqual(merges.get(a, (a,))[0], merges.get(b, (b,))[0])
        skeleton_case = cases[-1]
        merges = self.resolve(**skeleton_case)
        self.assertEqual(merges.get('participant_a', (None,))[0], 'participant_c')
        self.assertNotIn('participant_b', merges)
        # Unblocked reviewed chains still collapse normally.
        merges = self.resolve(curated={'email_a': ('email_b', item), 'email_b': (1, item)})
        self.assertEqual(merges['email_a'][0], 'kg:1')

    def test_webmail_tld_damage_merges_with_com(self):
        from build_communications import ocr_repair
        # Gmail has no country domains: gmail.co is a .com cut off at a line end (user report).
        for tld in ('co', 'cm', 'oam', 'tom', 'corn', 'eom', 'coml', 'c0m'):
            self.assertEqual(ocr_repair(f'private@gmail.{tld}')[1], 'gmail.com')
        for domain, repaired in (('aol.co', 'aol.com'), ('me.co', 'me.com'), ('hotmail.cpm', 'hotmail.com'),
                                 ('hotmail.fr', 'hotmail.fr'), ('yahoo.co.uk', 'yahoo.co.uk'), ('yahoo.es', 'yahoo.es'),
                                 ('verizon.net', 'verizon.net'), ('example.co', 'example.co')):
            self.assertEqual(ocr_repair(f'private@{domain}')[1], repaired)
        self.assertEqual(ocr_repair('p.rivate+tag@googlemail.com'), ('private', 'gmail.com'))
        # Company domains: endings that are no TLD at all are a misread .com; .cam is real.
        for domain, repaired in (('db.comj', 'db.com'), ('corcoran.corn', 'corcoran.com'), ('bakerbotts.corm', 'bakerbotts.com'),
                                 ('jetsuite.coin', 'jetsuite.com'), ('example.cam', 'example.cam')):
            self.assertEqual(ocr_repair(f'private@{domain}')[1], repaired)
        # Real domains a few letters from a provider keep their own country ending (not gmail.com).
        for domain in ('mail.ru', 'email.it', 'gmx.de'):
            self.assertEqual(ocr_repair(f'p.rivate@{domain}'), ('p.rivate', domain))
        for domain, repaired in (('gmall.co', 'gmail.com'), ('gmall.00m', 'gmail.com'), ('verlzon.net', 'verizon.net'), ('hotman.fr', 'hotmail.fr')):
            self.assertEqual(ocr_repair(f'private@{domain}')[1], repaired)

    def test_sign_offs_initials_and_vague_names(self):
        from build_communications import PersonMatcher, resolve_identities
        people = [dict(id=1, name='Jeffrey Epstein', entity_type='person'), dict(id=12, name='Lesley Groff', entity_type='person')]
        labels = {'participant_je': 'jeffrey e', 'participant_jedot': 'jeffrey e.', 'participant_jeff': 'jeffrey',
                  'participant_glue1': 'jeffrey epstein lesley groff', 'participant_glue2': 'lesley groff jeffrey epstein',
                  'participant_ann': 'amy ravenell', 'participant_anntrunc': 'amy rcsz', 'participant_annr': 'amy r',
                  'participant_neria': 'neria', 'participant_rich': 'Rich', 'participant_gmax': 'Gmax',
                  'participant_michelle': 'Michelle', 'participant_bradr': 'Bram R', 'participant_yoni': 'Yoni',
                  'participant_misread': 'jeevacationegmail.com'}
        rows = [('participant_jeff', 'email_jee', 'header_label_association')] * 25 \
            + [('participant_ann', 'email_x', 'sent_to')] * 100 + [('participant_anntrunc', 'email_x', 'sent_to')] * 3 \
            + [('participant_neria', 'email_nerja', 'header_label_association')] * 10 \
            + [('kg:1', 'email_nerja', 'header_label_association')] * 4 \
            + [('participant_rich', 'email_rk', 'header_name_association')] * 4 \
            + [('participant_gmax', 'email_gm', 'header_name_association')] * 3 \
            + [('participant_michelle', 'email_jee', 'header_name_association')] * 10 \
            + [('participant_bradr', 'email_bo', 'header_name_association')] * 4 \
            + [('participant_yoni', 'email_y', 'header_name_association')] * 3 + [('participant_yoni', 'email_y2', 'header_name_association')]
        db = self.enterContext(contextlib.closing(sqlite3.connect(':memory:')))
        db.execute('CREATE TABLE evidence(source_id, target_id, relationship_type)')
        db.executemany('INSERT INTO evidence VALUES (?,?,?)', rows)
        curated = {m: (1, {'reason': 'test', 'confidence': 'probable'}) for m in ('participant_je', 'email_jee')}
        curated['email_rk'] = (39, {'reason': 'test', 'confidence': 'probable'})
        curated['email_bo'] = (50, {'reason': 'test', 'confidence': 'probable'})
        with patch.object(build_communications, 'node_review', return_value={'participants': {}}):
            merges = resolve_identities(db, {'email_jee': 'jeevacation@gmail.com', 'email_nerja': 'nerja4102@yahoo.com',
                                             'email_x': 'x@corp.test', 'email_rk': 'rk12@corp.test', 'email_gm': 'gmax1@corp.test',
                                         'email_bo': 'bo@corp.test', 'email_y': 'yoniek@net.test', 'email_y2': 'oniek@net.test'},
                                        curated, (), labels, PersonMatcher(people),
                                        {'kg:1': 'Jeffrey Epstein', 'kg:12': 'Lesley Groff', 'kg:39': 'Richard Kahn', 'kg:50': 'Bram Ovel'},
                                        {'kg:1': 'Jeffrey Epstein', 'kg:12': 'Lesley Groff', 'kg:39': 'Richard Kahn', 'kg:50': 'Bram Ovel'})
        target = lambda member: merges.get(member, (member,))[0]
        # An address with its "@" misread as a letter, used as a header name, is that address's identity.
        self.assertEqual(merges['participant_misread'][:2], ('kg:1', 'header_name_is_misread_address'))
        # Glued two-person labels share a key but no longer chain the spelling clusters.
        self.assertEqual(target('participant_jedot'), 'kg:1')
        self.assertEqual(target('participant_glue2'), 'kg:12')
        # "Jeffrey" signs nearly all records on Epstein's address.
        self.assertEqual(merges['participant_jeff'][:2], ('kg:1', 'header_name_on_one_identity_addresses'))
        # One full name outweighs a leftover misread ("amy rcsz").
        self.assertEqual(merges['participant_annr'][:2], ('participant_ann', 'header_name_initial_dominant'))
        # 10 "Neria" records outvote 4 for Epstein on her address.
        self.assertNotIn('email_nerja', merges)
        # One-word sign-offs: "neria" takes her unresolved address, "Rich" fits Richard Kahn,
        # "Gmax" is in its address; an assistant's first name on Epstein's mailbox stays apart.
        self.assertEqual(merges['participant_neria'][:2], ('email_nerja', 'short_header_name_on_one_identity'))
        self.assertEqual(merges['participant_rich'][:2], ('kg:39', 'short_header_name_on_one_identity'))
        self.assertEqual(merges['participant_gmax'][:2], ('email_gm', 'short_header_name_on_one_identity'))
        self.assertNotIn('participant_michelle', merges)
        self.assertNotIn('participant_bradr', merges)  # trailing initial R does not start Ovel
        self.assertEqual(merges['participant_yoni'][0], 'email_y')  # truncated oniek@ counts for yoniek@


class ExternalEmailTests(unittest.TestCase):
    def test_reads_eml_and_mbox_headers(self):
        from adapters.external_emails import header_date, messages as external_messages
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'external/emails/published_dump'
            source.mkdir(parents=True)
            directory = str(Path(directory) / 'external/emails')
            (source / 'a.eml').write_bytes(b'From: Example Sender <sender@corp.test>\r\nTo: [REDACTED], Other Person <other@corp.test>\r\nDate: Tue, 3 Feb 2015 10:00:00 -0500\r\nMessage-ID: <1@x>\r\n\r\nbody')
            (source / 'b.mbox').write_bytes(b'From x Tue Feb  3 10:00:00 2015\nFrom: a@corp.test\nTo: b@corp.test\nMessage-ID: <2@x>\n\nbody\n')
            (source.parent.parent / 'jmail').mkdir()
            import gzip
            with gzip.open(source.parent.parent / 'jmail/emails-slim.ndjson.gz', 'wt') as f:
                for drop, promo in (('yahoo_2', 'False'), ('yahoo_2', 'True'), ('ehud_ddos_dropsite_1', 'False'), ('VOL00009', 'False')):
                    f.write(json.dumps({'id': drop + promo, 'doc_id': 'd', 'email_drop_id': drop, 'is_promotional': promo,
                                        'sender': 'Some Name <\u2588\u2588\u2588\u2588>', 'to_recipients': '["<a@corp.test>"]',
                                        'cc_recipients': '[]', 'sent_at': '2015-07-01T06:26:22'}) + '\n')
            messages = list(external_messages(Path(directory).parent))
        messages = [m for m in messages if m[0] in ('published_dump', 'jmail_yahoo_leak')]
        self.assertEqual([(m[0], m[1], m[2]) for m in messages[:2]], [('published_dump', 'a.eml', 1), ('published_dump', 'b.mbox', 2)])
        self.assertEqual(parse_recipient(messages[0][5][0]), [('Other Person', 'other@corp.test')])
        # Only the non-promotional Yahoo record; Barak's hacked mailbox and DOJ volumes skipped.
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[2][4], 'Some Name')
        self.assertEqual(messages[2][7], '2015-07-01')
        self.assertEqual(header_date('Tue, 3 Feb 2015 10:00:00 -0500'), '2015-02-03')
        self.assertIsNone(header_date('garbage'))


class DomainTypoTest(unittest.TestCase):
    def test_rare_domain_merges_into_same_mailbox_at_common_domain(self):
        values = {f'email_{i}': f'user{i}@wilmerhale.com' for i in range(30)}
        values.update({'email_a': 'anjan@wilmerhale.com', 'email_b': 'anjan@wilmerhaie.com',
                       'email_c': 'other@wilmerhaie.com',          # no such mailbox at the common domain
                       'email_d': 'x@nbc.com', 'email_e': 'x@db.com'})  # short domains never qualify
        values.update({f'email_db{i}': f'u{i}@db.com' for i in range(30)})
        merges = build_communications.domain_typo_merges(values)
        self.assertEqual(merges, {'email_b': 'email_a'})

    def test_unique_nearest_domain_wins_but_equal_distances_stay_separate(self):
        values = {f'email_{d}_{i}': f'user{i}@{domain}'
                  for d, domain in enumerate(('alphapaper.com', 'alphapoper.com')) for i in range(12)}
        values.update({'email_a': 'shared@alphapaper.com', 'email_b': 'shared@alphapoper.com',
                       'email_tied': 'shared@alphapeper.com', 'email_closer': 'shared@alphapaperx.com'})
        self.assertEqual(build_communications.domain_typo_merges(values), {'email_closer': 'email_a'})


if __name__ == '__main__':
    unittest.main()
