import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from build_entities import build
from build_documents import write
from export_neo4j import full_text_layers, LABELS
import export_neo4j
from unittest.mock import patch
import ocr_consensus as _ocr_consensus
_ocr_consensus.PATH = _ocr_consensus.PATH.with_name('nonexistent_ocr_consensus_for_tests.json')  # tests never read the live re-OCR output


class BuildEntityTests(unittest.TestCase):
    def build_labels(self, root, labels, existing=None, evidence=None, airports=None):
        mentions = root / 'input.db'
        db = sqlite3.connect(mentions)
        db.execute('CREATE TABLE mentions(key,kind,spelling,page_id,doc_id,n,subtype,gazetteer)')
        for i, (label, kind) in enumerate(labels):
            db.executemany('INSERT INTO mentions VALUES (?,?,?,1,?,1,\'\',0)',
                           [(label if kind == 'website' else f'key{i}', kind, label, d) for d in ('one', 'two')])
        db.commit(); db.close()
        with patch('build_entities.dictionary_words', return_value={'blood', 'type', 'ordinary', 'words', 'rose'}):
            summary = build(mentions, root, existing or {}, [], root / 'absent', b'fixture', evidence=evidence, airports=airports)
        return json.loads((root / 'entity_nodes.json').read_text()), summary

    def test_short_spans_and_gazetteer_aliases(self):
        labels = 'DP IA MM NS OW ST CA FF SI VS DEC SIN ISO US NY LA Va A Li'.split()
        with tempfile.TemporaryDirectory() as d:
            nodes, _ = self.build_labels(Path(d), [(s, 'GPE') for s in labels] + [('IRS', 'ORG')])
            self.assertEqual({n['name'] for n in nodes if n.get('label_quality') == 'not_a_name'}, set(labels) - {'US'})
            self.assertEqual({n['name'] for n in nodes if not n.get('label_quality')}, {'IRS', 'United States'})

    def test_codes_fold_only_to_one_existing_location(self):
        existing = {1: {'id': 1, 'name': 'United States', 'entity_type': 'location'},
                    2: {'id': 2, 'name': 'Virginia', 'entity_type': 'location'},
                    3: {'id': 3, 'name': 'CA', 'entity_type': 'organization'},
                    4: {'id': 4, 'name': 'DP', 'entity_type': 'location'},
                    5: {'id': 5, 'name': 'New York', 'entity_type': 'location'},
                    6: {'id': 6, 'name': 'New York', 'entity_type': 'location'},
                    7: {'id': 7, 'name': 'India', 'entity_type': 'location'}}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            nodes, _ = self.build_labels(root, [(s, 'ORG') for s in ('US', 'Va', 'CA', 'DP', 'NY', 'IN', 'in')], existing)
            visible = [n for n in nodes if n.get('label_quality') != 'not_a_name']
            self.assertEqual({n['id'] for n in visible}, {1, 2, 7})
            self.assertTrue(all(n['entity_type'] == 'location' for n in visible))
            self.assertEqual({n['name'] for n in nodes if n.get('label_quality')}, {'CA', 'DP', 'NY', 'in'})

    def test_form_labels_and_generic_variants_are_hidden(self):
        labels = ['Blood Type', 'Allergies or Health Concerns', "Driver's License No", 'of Birth',
                  'DNS Name', 'MB Cache', 'Title/Position', 'Mailing Address', 'Suite B-3', 'HP Slimline', '5VDC',
                  'and Associates', 'or Something', 'Patient Contact Number', 'Apt 4C', 'Floor 19', 'Unit B',
                  '12 GHz', 'Memory 32GB', '250ml', '4th Floor', '2nd fl.']
        with tempfile.TemporaryDirectory() as d:
            nodes, _ = self.build_labels(Path(d), [(s, 'ORG') for s in labels])
            self.assertEqual({n['name'] for n in nodes}, set(labels))
            self.assertTrue(all(n.get('label_quality') == 'not_a_name' for n in nodes))

    def test_dictionary_rule_preserves_title_case_and_ocr_names(self):
        visible = ['Ordinary Words', 'Bl00d Qxarble', 'J0rgé D’Ambrø', 'Жан Дюпон']
        hidden = ['blood type', 'ordinary words', 'ordinary Words', 'ORDINARY WORDS', 'Rose', 'J•••hn', 'R~']
        with tempfile.TemporaryDirectory() as d:
            nodes, _ = self.build_labels(Path(d), [(s, 'ORG') for s in visible + hidden + ['Q@uel']])
            self.assertEqual({n['name'] for n in nodes if not n.get('label_quality')}, set(visible))
            self.assertEqual({n['name'] for n in nodes if n.get('label_quality') and n['entity_type'] != 'email'}, set(hidden))

    def test_impossible_names_hidden_before_matching_and_counts(self):
        dropped = ['LLC', 'Inc.', 'Corp', 'Ltd', 'L.L.C.', '••', '•-0', 'r~', '~4', '• f••', '1234', '---']
        real = ['Example LLC', 'Inc Partners', 'Qxarble Ltd', 'A-1 Services']
        existing = {i: {'id': i, 'name': s, 'entity_type': 'organization'} for i, s in enumerate(dropped)}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            nodes, summary = self.build_labels(root, [(s, 'ORG') for s in dropped + real], existing)
            self.assertEqual({n['name'] for n in nodes if not n.get('label_quality')}, set(real))
            self.assertEqual({n['name'] for n in nodes if n.get('label_quality')}, set(dropped))
            self.assertEqual(summary['hidden_impossible_keys'], len(dropped))
            self.assertFalse(set(existing) & {n['id'] for n in nodes})
            db = sqlite3.connect(root / 'entity_graph.db')
            self.assertEqual(db.execute('SELECT count(*) FROM mentions').fetchone()[0], len(real + dropped) * 2)
            self.assertEqual(db.execute('SELECT count(*) FROM key_nodes').fetchone()[0], len(real + dropped))
            db.close()

    def test_copies_do_not_create_phantom_pairs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db = sqlite3.connect(root / 'input.db')
            db.execute('CREATE TABLE mentions(key,kind,spelling,page_id,doc_id,n,subtype,gazetteer)')
            for key, docs in [('alpha', ['one','other']), ('beta', ['copy','other']), ('gamma', ['one','copy','other'])]:
                db.executemany('INSERT INTO mentions VALUES (?,?,?,1,?,1,?,0)',
                               [(key,'ORG',key.title(),doc,'') for doc in docs])
            db.commit(); db.close()
            db = sqlite3.connect(root / 'copies.db')
            db.execute('CREATE TABLE docs(efta_number,canonical)')
            db.executemany('INSERT INTO docs VALUES (?,?)', [('one','one'),('copy','one'),('other','other')])
            db.commit(); db.close()
            build(root/'input.db', root, {}, [], root/'absent', b'fixture', clusters=root/'copies.db')
            db = sqlite3.connect(root / 'entity_graph.db')
            a, b = [db.execute('SELECT id FROM entities WHERE name=?', (name,)).fetchone()[0] for name in ('Alpha','Beta')]
            self.assertEqual(db.execute('SELECT shared_documents FROM pairs WHERE (a=? AND b=?) OR (a=? AND b=?)',
                                        (a,b,b,a)).fetchone()[0], 1)
            db.close()

    def test_threshold_folding_privacy_survivors_and_full_pairs(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); mentions=root/'mentions.db'
            db=sqlite3.connect(mentions)
            db.execute('CREATE TABLE mentions(key,kind,spelling,page_id,doc_id,n,subtype,gazetteer)')
            rows=[]
            for doc in ('d1','d2','copy'):
                rows.extend([('bank of america','gazetteer','BofA',1,doc,1,'bank',1),
                             ('participant_secret','PERSON','HeaderLabel_secret',1,doc,1,'',0),
                             ('example.com','website','https://example.com/a',1,doc,1,'',0),
                             ('jane roe','ORG','Jane Roe',1,doc,1,'',0)])
            rows.append(('single','ORG','Single',1,'d1',1,'',0))
            rows.append(('internal revenue service','gazetteer','IRS',1,'d1',1,'government',1))
            db.executemany('INSERT INTO mentions VALUES (?,?,?,?,?,?,?,?)',rows);db.commit();db.close()
            clusters=root/'clusters.db';db=sqlite3.connect(clusters)
            db.execute('CREATE TABLE docs(efta_number,canonical)');db.executemany('INSERT INTO docs VALUES (?,?)',[('d1','d1'),('copy','d1'),('d2','d2')]);db.commit();db.close()
            summary=build(mentions,root,{42:{'id':42,'name':'Bank of America Corp','entity_type':'organization'}},['Jane Roe'],root/'absent',b'key',1,1,clusters)
            nodes=json.loads((root/'entity_nodes.json').read_text())
            self.assertIn(42,{n['id'] for n in nodes})
            self.assertNotIn('Jane Roe',{n['name'] for n in nodes})
            self.assertNotIn('Single',{n['name'] for n in nodes})
            self.assertIn('example.com',{n['name'] for n in nodes})
            self.assertIn('HeaderLabel_secret',{n['name'] for n in nodes})
            self.assertEqual(summary['viewer_edges'],1)
            db=sqlite3.connect(root/'entity_graph.db')
            self.assertGreater(db.execute('SELECT count(*) FROM pairs').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT max(shared_documents) FROM pairs').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM mentions WHERE entity_id=42').fetchone()[0],3)
            db.close()
            write(root/'documents.db', [], {}, {}, {}, {}, root/'entity_graph.db')
            db = sqlite3.connect(root/'documents.db')
            self.assertEqual(db.execute("SELECT count(*) FROM node_documents WHERE node_id='42'").fetchone()[0],3)
            db.close()
            with patch.object(export_neo4j, 'OUT', root):
                exported, edges = {}, []
                full_text_layers(exported, edges)
            self.assertIn('kg:42', exported)
            self.assertTrue(any(n['entity_type']=='website' for n in exported.values()))
            self.assertEqual(LABELS['website'], 'Website')
            self.assertEqual(len(edges), summary['pairs'])


class ExistingFoldTests(unittest.TestCase):
    build_labels = BuildEntityTests.build_labels

    def test_brands_and_aliases_typed_person_reuse_organization(self):
        existing = {1: {'id': 1, 'name': 'ExampleAir LLC', 'aliases': ['ExampleAir'], 'entity_type': 'organization'},
                    'participant_old': {'id': 'participant_old', 'name': 'ExampleAir', 'entity_type': 'person'},
                    2: {'id': 2, 'name': 'Milken Example Institute', 'entity_type': 'organization'}}
        with tempfile.TemporaryDirectory() as d:
            nodes, summary = self.build_labels(Path(d), [('ExampleAir', 'PERSON'), ('Milken Example Institute', 'PERSON')], existing)
            self.assertEqual({n['id'] for n in nodes}, {1, 2})
            self.assertEqual(summary['folded_keys'], 2)

    def test_attested_dictionary_and_short_websites_remain_visible(self):
        from entity_build_evidence import LabelEvidence
        evidence = LabelEvidence(domains={'rr.com', 'match.com'})
        with tempfile.TemporaryDirectory() as d:
            nodes, _ = self.build_labels(Path(d), [('rr.com', 'website'), ('match.com', 'website'), ('gmall.com', 'website')], evidence=evidence)
            self.assertEqual({n['name'] for n in nodes if not n.get('label_quality')}, {'rr.com', 'match.com'})
            self.assertEqual(next(n['label_quality_reason'] for n in nodes if n['name'] == 'gmall.com'), 'mail_provider_lookalike')

    def test_reference_place_retyping_preserves_private_participant_id_and_spelling(self):
        from types import SimpleNamespace
        rows = {'ZZZZ': {'name': 'Charles de Example International Airport', 'municipality': 'Exampleville',
                          'type': 'large_airport', 'iso_region': 'CA-AB'}}
        airports = SimpleNamespace(rows=rows, resolve=lambda _: (None, None))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with sqlite3.connect(root/'input.db') as db:
                db.execute('CREATE TABLE mentions(key,kind,spelling,page_id,doc_id,n,subtype,gazetteer)')
                db.executemany("INSERT INTO mentions VALUES ('participant_opaque','PERSON','HeaderLabel_opaque',1,?,1,'',0)", [('a',), ('b',)])
            with sqlite3.connect(root/'private.db') as db:
                db.execute('CREATE TABLE spellings(key,spelling,n)')
                db.execute("INSERT INTO spellings VALUES ('participant_opaque','Charles de Example',2)")
            build(root/'input.db', root, {}, [], root/'private.db', b'fixture', airports=airports)
            nodes = json.loads((root/'entity_nodes.json').read_text())
            self.assertEqual(nodes[0]['entity_type'], 'location')
            self.assertEqual(nodes[0]['place_type'], 'airport')
            self.assertEqual(nodes[0]['name'], 'HeaderLabel_opaque')
            self.assertNotIn('Charles de Example', json.dumps(nodes))

    def test_company_prefixes_reuse_existing_and_files_stay_hidden(self):
        existing = {1: {'id': 1, 'name': 'Acme Inc.', 'entity_type': 'organization'},
                    2: {'id': 2, 'name': 'Statement.pdf', 'entity_type': 'organization'}}
        labels = ['See Acme Inc.', 'Legal Name Acme Inc.']
        files = ['image003.jpg', 'Inline-Images: EWLarge2.jpg', 'Statement.pdf', 'TEXT.htm', 'photo.JPG']
        with tempfile.TemporaryDirectory() as d:
            nodes, summary = self.build_labels(Path(d), [(s, 'ORG') for s in labels + files], existing)
            self.assertEqual(summary['folded_keys'], 2)
            self.assertEqual({n['id'] for n in nodes if not n.get('label_quality')}, {1})
            self.assertEqual({n['name'] for n in nodes if n.get('label_quality')}, set(files))

    def test_website_ocr_variants_share_identity_and_union_document_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with sqlite3.connect(root / 'input.db') as db:
                db.execute('CREATE TABLE mentions(key,kind,spelling,page_id,doc_id,n,subtype,gazetteer)')
                for name, count in [('flightaware.com', 12), ('flightawarc.com', 2), ('tlightaware.com', 2)]:
                    db.executemany("INSERT INTO mentions VALUES (?,'website',?,1,?,1,'',0)",
                                   [(name, name, name + str(i)) for i in range(count)])
                for name, spelling in [('summary.net', 'https://summary.net'), ('value.net', 'x@value.net')]:
                    db.executemany("INSERT INTO mentions VALUES (?,'website',?,1,?,1,'',0)",
                                   [(name, spelling, str(i)) for i in range(2)])
            summary = build(root/'input.db', root, {}, [], root/'absent', b'fixture')
            nodes = json.loads((root/'entity_nodes.json').read_text())
            self.assertEqual({n['name'] for n in nodes}, {'flightaware.com', 'summary.net', 'value.net'})
            canonical = next(n for n in nodes if n['name'] == 'flightaware.com')
            self.assertEqual(canonical['metadata']['document_count'], 16)
            self.assertEqual(summary['ocr_website_keys'], 2)
            self.assertEqual(next(n for n in nodes if n['name'] == 'value.net')['label_quality'], 'not_a_name')

    def test_spacing_and_punctuation_variants_share_one_node(self):
        existing = {7: {'id': 7, 'name': 'Chase & Co.', 'entity_type': 'organization'},
                    8: {'id': 8, 'name': 'Donald Trump', 'entity_type': 'person'},
                    9: {'id': 9, 'name': 'Ralph Lauren', 'entity_type': 'person'}}
        labels = [('TD Bank', 'ORG'), ('TDBank', 'ORG'), ('Vanity Fair', 'ORG'), ('Vanityfair', 'ORG'),
                  ('Chase& Co', 'ORG'), ('DONALDTRUMP', 'ORG'), ('Ralph Lauren Corporation', 'ORG'), ('Banks', 'ORG')]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            nodes, summary = self.build_labels(root, labels, existing)
            names = sorted(n['name'] for n in nodes)
            self.assertIn('TD Bank', names); self.assertNotIn('TDBank', names)
            self.assertIn('Vanity Fair', names); self.assertNotIn('Vanityfair', names)
            self.assertNotIn('DONALDTRUMP', names)
            with sqlite3.connect(root / 'entity_graph.db') as db:
                folds = dict(db.execute('SELECT span,entity_id FROM folds').fetchall())
            self.assertEqual(folds.get('DONALDTRUMP'), 8)
            self.assertEqual(folds.get('Chase& Co'), 7)

    def test_possessive_and_heading_heads_reuse_existing_legal_nodes(self):
        existing = {'llc': {'id': 'llc', 'name': 'Southern Financial LLC', 'entity_type': 'shell_company', 'metadata': {'document_count': 100}},
                    'inc': {'id': 'inc', 'name': 'Southern Financial Inc.', 'entity_type': 'shell_company'}}
        labels = ["Southern Financial's", 'Southern Financial’s', 'Southern Financial - IA Request',
                  'SOUTHERN FINANCIAL RELATIONSHIP', 'Southern Financial Inc. - IA Request']
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _, summary = self.build_labels(root, [(s, 'ORG') for s in labels], existing)
            self.assertEqual(summary['folded_keys'], len(labels))
            with sqlite3.connect(root / 'entity_graph.db') as db:
                self.assertEqual(db.execute('SELECT entity_id FROM folds ORDER BY key').fetchall(),
                                 [('llc',), ('llc',), ('llc',), ('llc',), ('inc',)])
        with tempfile.TemporaryDirectory() as d:
            labels = ['Southern Financial Foundation', 'Southern Financial and Partners', 'Unknown Financial - IA Request']
            _, summary = self.build_labels(Path(d), [(s, 'ORG') for s in labels], existing)
            self.assertEqual(summary.get('folded_keys', 0), 0)

    def test_organization_ocr_variants_and_ambiguous_nearest_names(self):
        existing = {1: {'id': 1, 'name': 'Deutsche Bank Trust Company Americas', 'entity_type': 'organization'},
                    2: {'id': 2, 'name': 'Example Capital', 'entity_type': 'organization'},
                    3: {'id': 3, 'name': 'Examples Capital', 'entity_type': 'organization'},
                    4: {'id': 4, 'name': 'Alpha Bank', 'entity_type': 'organization'}}
        labels = ['Detusche Bank Trust Company Americas', 'Deutsclle Bank Trust Company Americas',
                  'Examplex Capital', 'Alpba Bank', 'Example Qapitolsz']
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _, summary = self.build_labels(root, [(s, 'ORG') for s in labels], existing)
            self.assertEqual(summary['folded_keys'], 2)
            with sqlite3.connect(root / 'entity_graph.db') as db:
                self.assertEqual(db.execute('SELECT DISTINCT entity_id,reason FROM folds').fetchall(),
                                 [(1, 'ocr_organization_variant')])

    def test_state_and_airport_locations(self):
        existing = {1: {'id': 1, 'name': 'New York', 'entity_type': 'location'},
                    2: {'id': 2, 'name': 'John F. Kennedy International Airport', 'entity_type': 'location'}}
        with tempfile.TemporaryDirectory() as d:
            nodes, summary = self.build_labels(Path(d), [('State of New York', 'ORG'), ('New York JFK', 'GPE')], existing)
            self.assertEqual({n['id'] for n in nodes}, {1, 2})
            self.assertEqual(summary['folded_location'], 2)
        with tempfile.TemporaryDirectory() as d:
            nodes, _ = self.build_labels(Path(d), [('New York JFK', 'GPE')], {})
            self.assertEqual(nodes[0]['name'], 'New York JFK')

    def test_boilerplate_and_domain_remnants_hidden_even_with_existing_hits(self):
        hidden = ['Kind Regards', 'Best Regards', 'Best Regards/Mit', 'Sent from my iPhone',
                  'ail.com', 'by.net', 'the.net', 'db.com', 'un.org']
        visible = ['gmail.com', 'mail.com', 'the.netherlands.com']
        existing = {1: {'id': 1, 'name': 'Kind Regards', 'entity_type': 'organization'}}
        with tempfile.TemporaryDirectory() as d:
            nodes, summary = self.build_labels(Path(d), [(s, 'website' if '.' in s else 'ORG') for s in hidden + visible], existing)
            self.assertEqual(summary.get('folded_keys', 0), 0)
            self.assertEqual({n['name'] for n in nodes if n.get('label_quality') == 'not_a_name'}, set(hidden))
            self.assertEqual({n['name'] for n in nodes if not n.get('label_quality')}, set(visible))
        from build_entities import build_label_reason
        self.assertTrue(all(build_label_reason(s) for s in hidden if s not in ('db.com', 'un.org')))
        self.assertTrue(all(build_label_reason(s) is None for s in visible))

    def test_surname_requires_same_page_private_full_person_and_unambiguous_owner(self):
        existing = {1: {'id': 1, 'name': 'Brandon Mellin', 'entity_type': 'person'},
                    2: {'id': 2, 'name': 'Jane Mellin', 'entity_type': 'person'},
                    3: {'id': 3, 'name': 'Ron Wanek', 'entity_type': 'person'}}
        for other_page, ambiguous in [(False, False), (True, False), (False, True)]:
            with self.subTest(other_page=other_page, ambiguous=ambiguous), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                with sqlite3.connect(root / 'input.db') as db:
                    db.execute('CREATE TABLE mentions(key,kind,spelling,page_id,doc_id,n,subtype,gazetteer)')
                    for doc in ('one', 'two'):
                        db.executemany('INSERT INTO mentions VALUES (?,?,?,?,?,1,\'\',0)',
                            [('surname', 'ORG', 'Mellin', 1, doc), ('wanek', 'ORG', 'WANEK', 1, doc)])
                    # Full names are one-document anchors, not qualifying nodes.
                    db.execute('INSERT INTO mentions VALUES (?,?,?,?,?,1,\'\',0)',
                               ('participant_full', 'PERSON', 'HeaderLabel_full', 2 if other_page else 1, 'one'))
                    db.execute('INSERT INTO mentions VALUES (?,?,?,?,?,1,\'\',0)',
                               ('participant_wanek', 'PERSON', 'HeaderLabel_wanek', 1, 'one'))
                    if ambiguous:
                        db.execute("INSERT INTO mentions VALUES ('other','PERSON','Jane Mellin',1,'two',1,'',0)")
                with sqlite3.connect(root / 'private.db') as db:
                    db.execute('CREATE TABLE spellings(key,spelling,n)')
                    db.executemany('INSERT INTO spellings VALUES (?,?,1)',
                                   [('participant_full', 'Brandon Mellin'), ('participant_wanek', 'Ron Wanek')])
                build(root/'input.db', root, existing, [], root/'private.db', b'fixture')
                with sqlite3.connect(root / 'entity_graph.db') as db:
                    folds = dict(db.execute('SELECT key,entity_id FROM folds'))
                    self.assertEqual(folds.get('surname'), None if other_page or ambiguous else 1)
                    self.assertEqual(folds.get('wanek'), 3)
                self.assertNotIn('HeaderLabel_full', (root / 'entity_nodes.json').read_text())

    def test_surname_reuses_person_created_in_same_build(self):
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                labels = [('Brandon Mellin', 'PERSON'), ('Mellin', 'GPE')]
                if ambiguous:
                    labels.append(('Jane Mellin', 'PERSON'))
                nodes, summary = self.build_labels(root, labels)
                self.assertEqual(summary['same_page_person_keys'], 0 if ambiguous else 1)
                self.assertEqual(len(nodes), 3 if ambiguous else 1)
                if not ambiguous:
                    self.assertEqual(nodes[0]['entity_type'], 'person')
                    self.assertEqual(nodes[0]['metadata']['document_count'], 2)
                    with sqlite3.connect(root / 'entity_graph.db') as db:
                        self.assertEqual(db.execute('SELECT count(DISTINCT entity_id) FROM key_nodes').fetchone()[0], 1)

    def test_surname_can_reuse_dominant_full_person_header_with_ocr_variant(self):
        from build_communications import Builder
        from normalize_graph import name_key
        builder = Builder(b'fixture', [])
        identity = builder.participant_id(name_key('Brandon Mellin'))
        existing = {identity: {'id': identity, 'name': 'HeaderLabel_fixture', 'entity_type': 'participant'}}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with sqlite3.connect(root / 'input.db') as db:
                db.execute('CREATE TABLE mentions(key,kind,spelling,page_id,doc_id,n,subtype,gazetteer)')
                for spelling, kind, count in [('Brandon Mellin', 'PERSON', 6), ('Brando Mellin', 'PERSON', 2), ('Mellin', 'GPE', 2)]:
                    db.executemany('INSERT INTO mentions VALUES (?,?,?,1,?,1,\'\',0)',
                                   [(spelling, kind, spelling, str(i)) for i in range(count)])
            build(root/'input.db', root, existing, [], root/'absent', b'fixture')
            with sqlite3.connect(root / 'entity_graph.db') as db:
                self.assertEqual(db.execute("SELECT entity_id,reason FROM folds WHERE key='Mellin'").fetchone(),
                                 (identity, 'same_page_surname'))
                self.assertFalse(db.execute("SELECT 1 FROM entities WHERE name='Mellin'").fetchone())

    def test_rare_one_word_name_joins_its_only_full_name_owner(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with sqlite3.connect(root / 'input.db') as db:
                db.execute('CREATE TABLE mentions(key,kind,spelling,page_id,doc_id,n,subtype,gazetteer)')
                rows = [('Dan Ariely', 'a'), ('Dan Ariely', 'b'), ('Ariely', 'c'), ('Ariely', 'd'),
                        ('Anna Smithers', 'e'), ('Anna Smithers', 'f'), ('Bob Smithers', 'g'), ('Bob Smithers', 'h'),
                        ('Smithers', 'i'), ('Smithers', 'j')]
                db.executemany("INSERT INTO mentions VALUES (?,'PERSON',?,1,?,1,'',0)", [(n, n, doc) for n, doc in rows])
            summary = build(root/'input.db', root, {}, [], root/'absent', b'fixture')
            self.assertEqual(summary['unique_name_token_keys'], 1)
            with sqlite3.connect(root / 'entity_graph.db') as db:
                owner = dict(db.execute('SELECT key,entity_id FROM key_nodes').fetchall())
            self.assertEqual(owner['Ariely'], owner['Dan Ariely'])
            self.assertNotIn(owner['Smithers'], {owner['Anna Smithers'], owner['Bob Smithers']})

    def test_person_ocr_spellings_share_one_node(self):
        existing = {9: {'id': 9, 'name': 'Jean-Luc Brunel', 'entity_type': 'person'}}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with sqlite3.connect(root / 'input.db') as db:
                db.execute('CREATE TABLE mentions(key,kind,spelling,page_id,doc_id,n,subtype,gazetteer)')
                rows = [('Marissa Velikaska', 9), ('Marissa Velikaslca', 2), ('lean Luc Brunel', 2),
                        ('Anna Hansen', 9), ('Anne Hansen', 2), ('Peter Moll', 3), ('Peter Mol1', 2),
                        ('L. Ackerman', 9), ('J. Ackerman', 2), ('Spencer R. Wetherell', 9), ('Spencer Wetherell', 2),
                        ('Spencer P. Wetherell', 2)]
                db.executemany("INSERT INTO mentions VALUES (?,'PERSON',?,1,?,1,'',0)",
                               [(n, n, f'{n}{i}') for n, c in rows for i in range(c)])
            summary = build(root/'input.db', root, existing, [], root/'absent', b'fixture')
            with sqlite3.connect(root / 'entity_graph.db') as db:
                owner = dict(db.execute('SELECT key,entity_id FROM key_nodes').fetchall())
            self.assertEqual(owner['Marissa Velikaslca'], owner['Marissa Velikaska'])
            self.assertEqual(owner['lean Luc Brunel'], 9)
            self.assertNotEqual(owner['Anne Hansen'], owner['Anna Hansen'])
            self.assertNotEqual(owner['J. Ackerman'], owner['L. Ackerman'])
            self.assertEqual(owner['Spencer Wetherell'], owner['Spencer R. Wetherell'])
            self.assertNotEqual(owner['Spencer P. Wetherell'], owner['Spencer R. Wetherell'])
            self.assertNotEqual(owner['Peter Mol1'], owner['Peter Moll'])  # 3 vs 2 documents: not dominant

    def test_company_keys_aliases_and_ner_types(self):
        existing = {
            1: {'id': 1, 'name': 'JPMorgan Chase', 'entity_type': 'organization'},
            2: {'id': 2, 'name': 'Bank of America Corp.', 'entity_type': 'organization'},
            'llc': {'id': 'llc', 'name': 'Southern Financial LLC', 'entity_type': 'shell_company', 'metadata': {'document_count': 100}},
            'inc': {'id': 'inc', 'name': 'Southern Financial, Inc.', 'entity_type': 'shell_company', 'metadata': {'document_count': 10}},
            497: {'id': 497, 'name': 'Deutsche Bank', 'entity_type': 'organization', 'original_names': ['Deutsche Bank Securities', 'Deutsche Bank Securities Inc.', 'Deutsche Bank Trust Co Americas']},
            'dbt': {'id': 'dbt', 'name': 'Deutsche Bank Trust Company Americas', 'entity_type': 'organization'},
            'dbs': {'id': 'dbs', 'name': 'Deutsche Bank Securities Inc.', 'entity_type': 'organization'},
            'dbw': {'id': 'dbw', 'name': 'Deutsche Bank Wealth Management', 'entity_type': 'organization'},
            'alias': {'id': 'alias', 'name': 'Example Ltd', 'original_names': ['Example Group'], 'aliases': '["Example Alias"]', 'entity_type': 'organization'},
        }
        labels = ['Chase', 'JPM', 'JPMC', 'J.P. Morgan', 'BofA', 'The Southern Financial',
                  'Southern Financial Inc.', 'Deutsche Bank Securities', 'Deutsche Bank Securities Inc', 'Deutsche Bank Wealth Management',
                  'Deutsche Bank Trust Company Americas', 'Bank Trust Company Americas', 'Example Group', 'Example Alias']
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            nodes, summary = self.build_labels(root, [(s, 'FAC') for s in labels], existing)
            self.assertEqual(summary['folded_keys'], len(labels))
            self.assertEqual({n['id'] for n in nodes}, set(existing) - {497})
            db = sqlite3.connect(root / 'entity_graph.db')
            self.assertEqual(db.execute('SELECT entity_id FROM folds WHERE span=?', ('The Southern Financial',)).fetchone()[0], 'llc')
            self.assertEqual(db.execute('SELECT reason FROM folds WHERE span=?', ('Bank Trust Company Americas',)).fetchone()[0], 'same_page_left_fragment')
            self.assertEqual(db.execute('SELECT entity_id FROM folds WHERE span=?', ('Deutsche Bank Securities Inc',)).fetchone()[0], 'dbs')
            self.assertEqual(db.execute('SELECT entity_id FROM folds WHERE span=?', ('Deutsche Bank Trust Company Americas',)).fetchone()[0], 'dbt')
            self.assertTrue(all(n['metadata']['document_count'] == 2 for n in nodes))
            db.close()

    def test_left_fragment_requires_same_page_full_name(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db = sqlite3.connect(root / 'input.db')
            db.execute('CREATE TABLE mentions(key,kind,spelling,page_id,doc_id,n,subtype,gazetteer)')
            for doc in ['one','two']:
                db.execute('INSERT INTO mentions VALUES (?,?,?,?,?,1,\'\',0)', ('tail','ORG','Bank Trust Company Americas',1,doc))
                db.execute('INSERT INTO mentions VALUES (?,?,?,?,?,1,\'\',0)', ('full','ORG','Deutsche Bank Trust Company Americas',2,doc))
            db.commit(); db.close()
            build(root/'input.db', root, {42:{'id':42,'name':'Deutsche Bank Trust Company Americas','entity_type':'organization'}}, [], root/'absent', b'fixture')
            db = sqlite3.connect(root / 'entity_graph.db')
            self.assertEqual(db.execute('SELECT count(*) FROM folds WHERE span=?', ('Bank Trust Company Americas',)).fetchone()[0], 0)
            db.close()

    def test_place_canonicalization_and_existing_city_distinction(self):
        places = ['US','USA','U.S.','United States','United States of America','NY UNITED STATES','New York','New York City','NYC','Manhattan']
        with tempfile.TemporaryDirectory() as d:
            nodes, _ = self.build_labels(Path(d), [(p,'GPE') for p in places])
            self.assertEqual({n['name'] for n in nodes}, {'United States','New York','Manhattan'})
        existing = {1:{'id':1,'name':'New York','entity_type':'location'}, 2:{'id':2,'name':'New York City','entity_type':'location'},
                    3:{'id':3,'name':'United States','entity_type':'location'}}
        with tempfile.TemporaryDirectory() as d:
            nodes, summary = self.build_labels(Path(d), [(p,'GPE') for p in places[:-1]], existing)
            self.assertEqual({n['id'] for n in nodes}, set(existing))
            self.assertEqual(summary['folded_location'], len(places) - 1)

    def test_person_type_corrections_and_headings(self):
        labels = [('Brandon Mellin','FAC'),('Brenda Norell','GPE'),('Nancy Jones','ORG'),('NANCY','ORG'),
                  ('DOCTYPE','ORG'),('<html>','ORG'),('RM0082289 Daily Deposit Report','ORG')]
        with tempfile.TemporaryDirectory() as d:
            nodes, _ = self.build_labels(Path(d), labels)
            people = [n for n in nodes if n['entity_type'] == 'person']
            self.assertEqual(len(people), 4)
            self.assertTrue(all(n['name'].startswith('HeaderLabel_') for n in people))
            self.assertEqual(len([n for n in people if n.get('label_quality_reason') == 'person_weak']), 1)
            self.assertTrue(all(n.get('label_quality') == 'not_a_name' for n in nodes if n['entity_type'] != 'person'))
        existing = {1:{'id':1,'name':'Brandon Mellin','entity_type':'location'}}
        with tempfile.TemporaryDirectory() as d:
            nodes, _ = self.build_labels(Path(d), labels[:1], existing)
            self.assertEqual(nodes[0]['id'], 1)

    def test_ocr_emails_fold_to_existing_email_and_unknown_are_hidden(self):
        from build_contacts import contact_id
        identity = contact_id('jeevacation@gmail.com','email',b'fixture')
        existing = {identity:{'id':identity,'name':'Email_fixture','entity_type':'email'}}
        labels = ['jeevacation®gmail.com','jeevacationegmail.com','jeevacationggmail.com','jeevacation@gmail.com', 'unknown@example.test']
        with tempfile.TemporaryDirectory() as d:
            nodes, summary = self.build_labels(Path(d), [(s,'ORG') for s in labels], existing)
            self.assertEqual(summary['folded_email'], 4)
            self.assertEqual({n['entity_type'] for n in nodes}, {'email'})
            self.assertEqual(len(nodes), 2)
            self.assertTrue(all(n.get('label_quality') == 'not_a_name' for n in nodes if n['id'] != identity))
            self.assertNotIn('jeevacation', (Path(d)/'entity_nodes.json').read_text())

    def test_upstream_inputs_only_and_self_output_exclusion(self):
        from entity_resolution import read_nodes
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'normalized_entities.json').write_text(json.dumps([{'id':1,'name':'Original','entity_type':'organization'}]))
            (root/'victim_nodes.json').write_text(json.dumps([{'id':1,'name':'Original','aliases':['Layer Alias'],'entity_type':'organization'},
                {'id':'kg','name':'KG only','entity_type':'organization'},
                {'id':'entity_old','name':'Previous NER','entity_type':'organization'},
                {'id':'participant_old','name':'HeaderLabel_old','entity_type':'person','source':'Full-text entity mentions; NER is unverified'}]))
            # Downstream combined graphs contain this layer's own output: never inputs.
            for name in ('graph.json', 'graph_entities.json'):
                (root/name).write_text(json.dumps({'nodes':[{'id':'downstream','name':'Combined','entity_type':'organization'}]}))
            existing=read_nodes(root)
            self.assertEqual(set(existing), {1,'kg'})
            self.assertIn('Layer Alias', existing[1]['aliases'])

    def test_mailbox_repairs_use_existing_merged_email_identity(self):
        from entity_resolution import ExistingIndex
        with tempfile.TemporaryDirectory() as d:
            lookup = Path(d)/'lookup.json'
            lookup.write_text(json.dumps({'email_old':{'kind':'email','value':'m.ail+tag@googlemail.com','raw_spellings':['mail@grnail.corn']}}))
            index = ExistingIndex({'email_new':{'id':'email_new','name':'Email_new','entity_type':'email','merged_identities':[{'id':'email_old'}]}}, {}, b'fixture', lookup)
            self.assertEqual(index.email_hits('mail®gmail.com'), {'email_new'})
            self.assertEqual(index.email_hits('mail@grnail.corn'), {'email_new'})

    def test_unrelated_ambiguous_aliases_do_not_choose_arbitrarily(self):
        from entity_resolution import ExistingIndex, company_key
        self.assertEqual(company_key('The Example, L.L.C.'), company_key('EXAMPLE Incorporated'))
        self.assertEqual(company_key('Example GmbH'), company_key('Example'))
        index = ExistingIndex({1:{'id':1,'name':'Alpha Ltd','aliases':['Shared'],'entity_type':'organization'},
                               2:{'id':2,'name':'Beta Inc','aliases':['Shared'],'entity_type':'organization'}}, {})
        self.assertIsNone(index.choose(index.hits('Shared')))

    def test_gazetteer_canonical_can_find_alias_only_existing_node(self):
        from entity_resolution import ExistingIndex
        index = ExistingIndex({1:{'id':1,'name':'JPMC','entity_type':'organization'}},
                              {'JPMorgan Chase':{'aliases':['JPM','JPMC','Chase']}})
        self.assertEqual(index.hits('Chase'), {1})
        self.assertEqual(index.hits('JPMorgan Chase'), {1})

    def test_city_does_not_fold_to_retailer_with_company_suffix(self):
        from entity_resolution import company_key
        self.assertNotEqual(company_key('New York'),company_key('New York & Company'))
        existing = {1:{'id':1,'name':'New York & Company','entity_type':'organization'},
                    2:{'id':2,'name':'United States','entity_type':'organization'}}
        with tempfile.TemporaryDirectory() as d:
            nodes, summary = self.build_labels(Path(d), [(p,'GPE') for p in ['New York','New York City','NYC','Manhattan','US','United States']], existing)
            self.assertEqual({n['name'] for n in nodes}, {'New York','United States','Manhattan'})
            self.assertTrue(all(n['entity_type']=='location' and n['id'] not in existing for n in nodes))
            self.assertEqual(summary.get('folded_keys',0),0)

    def test_canonical_mailbox_wins_over_legacy_duplicate_email_nodes(self):
        from entity_resolution import ExistingIndex
        from build_contacts import contact_id
        key=b'fixture'
        canonical=contact_id('mail@gmail.com','email',key)
        legacy=contact_id('m.ail+tag@googlemail.com','email',key)
        with tempfile.TemporaryDirectory() as d:
            lookup=Path(d)/'lookup.json'
            lookup.write_text(json.dumps({canonical:{'kind':'email','value':'mail@gmail.com'},
                                         legacy:{'kind':'email','value':'m.ail+tag@googlemail.com'}}))
            nodes={canonical:{'id':canonical,'name':'Email_canonical','entity_type':'email','merged_identities':[{'id':legacy}]},
                   legacy:{'id':legacy,'name':'Email_legacy','entity_type':'email'}}
            index=ExistingIndex(nodes,{},key,lookup)
            self.assertEqual(index.email_hits('mail®gmail.com'),{canonical})
            self.assertEqual(index.email_hits('mailegmail.com'),{canonical})
            self.assertEqual(index.email_hits('m.ail+tag@googlemail.com'),{canonical})
