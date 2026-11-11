"""Small synthetic fixtures only: no private corpus or rebuild required."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from collections import Counter, defaultdict
from pathlib import Path
from unittest.mock import patch

import build_email_places as places


class DefaultsTests(unittest.TestCase):
    def test_addresses_need_five_emails(self):
        self.assertEqual(places.MIN_ADDRESS_DOCS, 5)


@patch.object(places, 'MIN_ADDRESS_DOCS', 3)  # small fixtures; the shipped default is checked above
class AddressTests(unittest.TestCase):
    def build(self, rows, public=()):
        docs, spellings = defaultdict(set), defaultdict(Counter)
        for text, refs in rows:
            key = places.address_key(text)
            docs[key].update(refs)
            spellings[key][text] += len(refs)
        evidence, nodes = defaultdict(set), {}
        lookup = places.add_addresses(docs, spellings, places.PublicPlaces(public), b'test', evidence, nodes)
        return evidence, nodes, lookup

    def test_exact_property_and_original_name(self):
        public = [dict(id=485, name='358 El Brillo Way', entity_type='property',
                       original_names=['358 El Brillo Way, Palm Beach'], metadata='keep')]
        evidence, nodes, lookup = self.build([
            ('358 El Brillo Way', ['a', 'b', 'c']),
            ('358 El Brillo Way, Palm Beach', ['b', 'd', 'e'])], public)
        self.assertEqual(set(nodes), {485})
        self.assertEqual(evidence[485], set('abcde'))
        self.assertEqual(nodes[485]['metadata'], 'keep')
        self.assertEqual(lookup[485]['documents'], 5)
        self.assertEqual(len(lookup[485]['spellings']), 2)
        self.assertEqual(len(nodes[485]['merged_address_aliases']), 2)
        self.assertNotIn('Palm Beach', json.dumps(nodes[485]['merged_address_aliases']))

    def test_named_public_place_keeps_name_in_private_reveal(self):
        public = [dict(id='venue', name='Sample Institution', entity_type='location', address='12 Main Street')]
        _, nodes, lookup = self.build([('12 Main St [signature: image001.png]', ['a', 'b', 'c'])], public)
        self.assertEqual(lookup['venue']['value'], 'Sample Institution')
        self.assertEqual(lookup['venue']['metadata']['signature_annotations'], ['image001.png'])
        self.assertEqual(nodes['venue']['name'], 'Sample Institution')

    def test_junk_street_evidence_is_retained_but_hidden(self):
        _, nodes, lookup = self.build([('00 Meet Dr', ['a', 'b', 'c'])])
        self.assertEqual(len(nodes), 1)
        self.assertEqual(next(iter(nodes.values()))['place_quality_reason'], 'itinerary_or_greeting_address')
        self.assertEqual(len(lookup), 1)

    def test_airport_alias_resolution_is_unique_and_island_is_separate(self):
        index = places.PublicPlaces([
            dict(id='lga', name='LaGuardia Airport', entity_type='location'),
            dict(id='mia', name='Miami International Airport', entity_type='location'),
            dict(id='stt', name='Cyril E. King Airport (St. Thomas)', entity_type='location'),
            dict(id='island', name='St. Thomas', entity_type='location')])
        for label, expected in [('LGA Airport', 'lga'), ('Miami International', 'mia'),
                                ('Cyril E King', 'stt'), ('St Thomas', 'island')]:
            self.assertEqual(index.resolve(label), expected)

    def test_reviewed_redhook_union_and_private_spellings(self):
        variants = ['6100 Redhook St', '6100 Red Hook St.', '6100 Red Hook Quarter St',
                    '6100 Red Hook Quarters Street']
        evidence, nodes, lookup = self.build([(v, ['a', str(i)]) for i, v in enumerate(variants)])
        self.assertEqual(len(nodes), 1)
        node_id = places.address_id('6100 redhook street', b'test')
        self.assertEqual(evidence[node_id], {'a', '0', '1', '2', '3'})
        self.assertEqual(lookup[node_id]['spellings'], sorted(variants))
        self.assertEqual(len(nodes[node_id]['merged_address_aliases']), 4)
        self.assertNotIn('Red', json.dumps(nodes))
        self.assertNotIn('6100', json.dumps(nodes))

    def test_no_number_or_city_guessing_units_fold_into_building(self):
        values = ['358 El Brillo Way', '385 El Brillo Way', '6100 Red Hook St', '6300 Red Hook St',
                  '358 El Brillo Way, Miami', '358 El Brillo Way, Palm Beach',
                  '358 El Brillo Way Suite 1', '358 El Brillo Way Suite 2',
                  '358 El Brillo Way #1', '358 El Brillo Way Apt 1']
        _, nodes, lookup = self.build([(v, ['a', 'b', 'c']) for v in values])
        # Different numbers and different cities stay apart; units join the
        # unqualified building, which two conflicting cities keep separate.
        self.assertEqual(len(nodes), 6)
        building = places.address_id('358 el brillo way', b'test')
        self.assertEqual(len(lookup[building]['spellings']), 5)

    def test_short_street_misreads_join_the_frequent_spelling(self):
        docs = {'401 E. Las Olas Blvd. Suite 1200': 198, '401 Fast Las Olas Blvd., Suite 1200': 6, '767 Filth Avenue': 16,
                '767 Fifth Avenue, Suite 4200': 282, '7825 Fa Avenue': 6, '7825 Fay Avenue': 24,
                '1675 Palm Beach Lakes Blvd Suite One': 24, '1675 Palm Beach Lakes Blvd. Suite 1': 15,
                '12 Green Street': 10, '12 Greene Street': 40}
        weight = {places.address_key(v): n for v, n in docs.items()}
        groups = places.building_groups(list(weight), weight)
        same = lambda a, b: groups[places.address_key(a)] == groups[places.address_key(b)]
        self.assertTrue(same('401 Fast Las Olas Blvd., Suite 1200', '401 E. Las Olas Blvd. Suite 1200'))
        self.assertTrue(same('767 Filth Avenue', '767 Fifth Avenue, Suite 4200'))
        self.assertTrue(same('7825 Fa Avenue', '7825 Fay Avenue'))
        self.assertTrue(same('1675 Palm Beach Lakes Blvd Suite One', '1675 Palm Beach Lakes Blvd. Suite 1'))
        self.assertFalse(same('12 Green Street', '12 Greene Street'))
        # 'E Street' has no name left once the direction is dropped; it never joins a lettered street.
        weight = {places.address_key(v): n for v, n in {'1001 E Street NW': 4, '1001 F Street NW': 30}.items()}
        groups = places.building_groups(list(weight), weight)
        self.assertNotEqual(groups[places.address_key('1001 E Street NW')], groups[places.address_key('1001 F Street NW')])

    def test_building_merges_units_zip_city_suffix_and_ocr(self):
        rows = [('500 Fifth Avenue', ['a']), ('5OO Fifth Ave., Suite 1400', ['b']), ('500 Fifth', ['c']),
                ('500 Fifth Avenue, New York, NY 10110', ['d']), ('12 Srnithtown Avenue', ['e', 'f']),
                ('12 Smithtown Ave', ['g'])]
        evidence, nodes, lookup = self.build(rows)
        self.assertEqual(len(nodes), 2)
        fifth = places.address_id('500 fifth avenue, new york', b'test')
        self.assertEqual(evidence[fifth], set('abcd'))
        self.assertEqual(len(nodes[fifth]['merged_address_aliases']), 4)
        self.assertEqual(evidence[places.address_id('12 smithtown avenue', b'test')], set('efg'))
        self.assertNotIn('Fifth', json.dumps(nodes))  # still pseudonymous

    def test_ocr_folding_never_crosses_numbers_or_cities(self):
        groups = places.building_groups({'12 main street', '13 main street', '12 main street, boston',
                                         '12 main street, miami', '1 main road'}, {})
        self.assertEqual(len(set(groups.values())), 5)
        self.assertEqual(places.ocr_fold('1o1 barn street'), '101 bam street')
        self.assertEqual(places.split_address('12 main street suite 4 floor 2 new york ny 10022'),
                         ('12 main street', 'new york'))

    def test_ocr_misreadings_of_a_long_street_join(self):
        weight = {'2029 stierlin court': 390, '2029 stiedin court': 90, '2029 stierhn court': 5,
                  '55 water street': 8, '55 baker street': 7, '145 green street': 10, '145 greene street': 19,
                  '1519 ponce de leon avenue': 42, '1519 ponce deleon avenue': 63, '2030 stierlin court': 3}
        groups = places.building_groups(set(weight), weight)
        self.assertEqual({groups[k] for k in weight if k.startswith('2029')}, {'2029 stierlin court'})
        self.assertEqual(groups['1519 ponce de leon avenue'], groups['1519 ponce deleon avenue'])
        self.assertNotEqual(groups['55 water street'], groups['55 baker street'])
        self.assertNotEqual(groups['145 green street'], groups['145 greene street'])
        self.assertEqual(groups['2030 stierlin court'], '2030 stierlin court')
        self.assertEqual(places.split_address('00 meet drive'), (None, ''))

    def test_house_number_misread_under_one_signature_joins(self):
        lookup = {k: {'value': v} for k, v in [('top', '2013 Costa Del Mar Road'), ('ocr', '2023 Costa Del Mar Road'),
                                                 ('cut', '013 Costa Del Mar Road'), ('park', '345 Park Avenue'),
                                                 ('next', '340 Park Avenue')]}
        docs = {'top': set(range(1000)), 'ocr': {1}, 'cut': {2}, 'park': set(range(500)), 'next': {3}}
        signature = {'top': (0.5, 'Deepak Chopra MD'), 'ocr': (1.0, 'President Deepak Chopra LLC'), 'cut': (1.0, 'Deepak Chopra MD'),
                     'park': (0.6, 'Deutsche Bank'), 'next': (1.0, 'Other Firm LLC')}
        lines = {'top': {'deepak chopra md', 'president deepak chopra llc'}, 'park': {'deutsche bank'}}
        self.assertEqual(places.signature_number_merges(lookup, docs, signature, lines), {'ocr': 'top', 'cut': 'top'})

    def test_signature_block(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(directory) / 'corpus.db'
        corpus = sqlite3.connect(path)
        corpus.execute('CREATE TABLE pages(efta_number, page_number, text_content)')
        for i in range(10):
            corpus.execute('INSERT INTO pages VALUES (?, 0, ?)', (f'E{i}', f'Thanks  Richard  HBRK Associates Inc.  575 Lexington Avenue  NY {i}'))
        corpus.execute('INSERT INTO pages VALUES (?, 0, ?)', ('B', 'please send it to 575 Lexington Avenue tomorrow'))
        corpus.commit()
        corpus.close()
        corpus = places.corpus_source.connect(path)
        self.addCleanup(corpus.close)
        share, line = places.signature_block(['575 Lexington Avenue'], [f'E{i}' for i in range(10)] + ['B'], corpus)
        self.assertEqual(line, 'HBRK Associates Inc.')
        self.assertGreater(share, 0.8)
        self.assertEqual(places.signature_block(['575 Lexington Avenue'], ['B', 'E1'], corpus), (0.0, ''))  # too few to tell
        corpus.close()

    def test_suffix_completion_requires_one_public_street_and_locality(self):
        index = places.PublicPlaces([dict(id=1, name='9 East 71st Street', entity_type='property')])
        self.assertEqual(index.resolve('9 East 71st'), 1)
        self.assertEqual(index.resolve('9 E. 71st'), 1)
        self.assertIsNone(index.resolve('9 East 71st Suite 2'))
        ambiguous = places.PublicPlaces([dict(id=1, name='9 East 71st Street', entity_type='property'),
                                        dict(id=2, name='9 East 71st Avenue', entity_type='property')])
        self.assertIsNone(ambiguous.resolve('9 East 71st'))
        cities = places.PublicPlaces([dict(id=1, name='9 East 71st Street, Boston', entity_type='property'),
                                     dict(id=2, name='9 East 71st Street, Miami', entity_type='property')])
        self.assertIsNone(cities.resolve('9 East 71st'))
        self.assertEqual(places.PublicPlaces([dict(id=3, name='West Palm Beach', entity_type='location')]).resolve('WEST PALM BEACH FL'), 3)

    def test_signature_annotations_do_not_split_addresses_or_enter_reveal_value(self):
        text = '12 Main Street [signature: image001.png]'
        self.assertEqual(places.address_key(text), places.address_key('12 Main Street'))
        _, nodes, lookup = self.build([(text, ['a', 'b', 'c'])])
        self.assertEqual(next(iter(lookup.values()))['value'], '12 Main Street')
        self.assertEqual(next(iter(lookup.values()))['metadata']['signature_annotations'], ['image001.png'])
        self.assertNotIn('image001', json.dumps(nodes))

    def test_old_classification_keys_are_reused_without_guessing_conflicts(self):
        verdict = {'place': True, 'name': 'West Palm Beach', 'type': 'city'}
        normalized = places.normalized_verdicts({'west palm beach fl': verdict})
        self.assertEqual(normalized['west palm beach'], verdict)
        other = {'place': True, 'name': 'Other City', 'type': 'city'}
        self.assertNotIn('west palm beach', places.normalized_verdicts(
            {'west palm beach fl': verdict, 'west palm beach ny': other}))

    def test_normalization(self):
        for a, b in [('9 E. 71st St.', '9 East 71 Street'),
                     ('9 West 71st Avenue', '9 W 71 Ave.'),
                     ('12 Main Rd. N.', '12 Main Road North'),
                     ('12 Main Street Ste. 2', '12 Main St Suite 2')]:
            self.assertEqual(places.address_key(a), places.address_key(b))
        self.assertNotEqual(places.address_key('12 St John Street'), places.address_key('12 Street John Street'))
        self.assertNotEqual(places.address_key('12-14 Main St'), places.address_key('12 14 Main St'))

    def test_ambiguity_and_no_locality_inference(self):
        public = [dict(id=i, name='12 Main Street', entity_type='property',
                       original_names=[f'12 Main St, City {i}']) for i in (1, 2)]
        evidence, nodes, _ = self.build([('12 Main St', ['a', 'b', 'c'])], public)
        self.assertEqual(len(nodes), 1)
        self.assertTrue(next(iter(nodes)).startswith('address_'))
        self.assertNotIn(1, evidence)
        self.assertEqual(places.PublicPlaces(public).resolve('12 Main St, City 2'), 2)
        self.assertIsNone(places.PublicPlaces(public).resolve('12 Main St Suite 2'))

    def test_one_locality_spelled_twice_still_resolves(self):
        public = [dict(id=485, name='358 El Brillo Way', entity_type='property',
                       original_names=['358 El Brillo Way, Palm Beach', '358 El Brillo Way, Palm Beach FL 33480 (parcel 7)'])]
        self.assertEqual(places.PublicPlaces(public).resolve('358 El Brillo Way'), 485)
        two = [dict(id=1, name='12 Main Street', entity_type='property',
                    original_names=['12 Main St, Boston', '12 Main St, Miami'])]
        self.assertIsNone(places.PublicPlaces(two).resolve('12 Main St'))  # two cities: not guessed

    def test_explicit_address_fields_and_safe_prefix(self):
        public = [dict(id=486, name='Zorro Ranch', entity_type='property',
                       address='49 Zorro Ranch Road, Stanley, New Mexico'),
                  dict(id=492, name='Paris Apartment', entity_type='property',
                       metadata=json.dumps({'address': '22 Avenue Foch, 75116 Paris, France'})),
                  dict(id=493, name='Office', entity_type='location',
                       metadata={'address': '12 Main Street, Boston'})]
        index = places.PublicPlaces(public)
        for text, expected in [('49 Zorro Ranch Rd', 486), ('22 Avenue Foch', 492),
                               ('12 Main St', 493), ('12 Main Street, Boston', 493)]:
            self.assertEqual(index.resolve(text), expected)
        for text in ['49 Zorro Ranch Road, Miami', '22 Avenue Foch, London',
                     '12 Main Street Suite 2', '12 Main Street, Boston Suite 2',
                     '22 Avenue', '50 Zorro Ranch Road']:
            self.assertIsNone(index.resolve(text), text)
        _, nodes, lookup = self.build([('49 Zorro Ranch Rd', ['a', 'b', 'c'])], public)
        self.assertEqual(set(nodes), {486})
        self.assertNotIn('49 Zorro Ranch Rd', json.dumps(nodes))
        self.assertIn('49 Zorro Ranch Rd', lookup[486]['spellings'])

    def test_conflicting_cities_and_units_even_on_same_id(self):
        for aliases in [('12 Main Street, Boston', '12 Main Street, Miami'),
                        ('12 Main Street Suite 1', '12 Main Street Suite 2')]:
            for ids in [(1, 2), (1, 1)]:
                public = [dict(id=i, name='Office', entity_type='property', address=a)
                          for i, a in zip(ids, aliases)]
                index = places.PublicPlaces(public)
                self.assertIsNone(index.resolve('12 Main St'))
                for i, alias in zip(ids, aliases):
                    self.assertEqual(index.resolve(alias), i)
        # An unqualified alias must not bypass other public candidates.
        index = places.PublicPlaces([
            dict(id=1, name='12 Main Street', entity_type='property'),
            dict(id=2, name='Office', entity_type='property', address='12 Main Street Suite 2')])
        self.assertIsNone(index.resolve('12 Main St'))

    def test_curated_mapping_unique_existing_ids_without_export(self):
        public = [dict(id=486, name='Zorro Ranch', entity_type='property'),
                  dict(id=492, name='Paris apartment, 16th Arrondissement', entity_type='property')]
        curated = [dict(slug='zorro-ranch', name='Zorro Ranch', type='property',
                        address='49 Zorro Ranch Road, Stanley, New Mexico'),
                   dict(slug='paris-apartment', name='Paris Apartment', type='property',
                        address='22 Avenue Foch, 75116 Paris, France')]
        index = places.PublicPlaces(public, curated)
        self.assertEqual(index.resolve('49 Zorro Ranch Rd'), 486)
        self.assertEqual(index.resolve('22 Avenue Foch'), 492)
        self.assertNotIn('address', index.nodes[486])
        conflicting = public + [dict(id=999, name='Zorro Ranch, NM', entity_type='property')]
        self.assertIsNone(places.PublicPlaces(conflicting, curated).resolve('49 Zorro Ranch Road'))
        self.assertIsNone(places.PublicPlaces([], curated).resolve('22 Avenue Foch'))
        # Never extract a street from a description or locality-only metadata.
        self.assertIsNone(places.PublicPlaces([dict(id=1, name='Home', entity_type='property',
            metadata={'address': 'Paris, France'}, description='22 Avenue Foch')]).resolve('22 Avenue Foch'))

    def test_private_review_and_summary(self):
        public = places.PublicPlaces([dict(id=i, name='Office', entity_type='property',
                                           address=f'12 Main Street, City {i}') for i in (1, 2)])
        key = places.address_key('12 Main St')
        summary, review, nodes = Counter(), [], {}
        lookup = places.add_addresses({key: {'a', 'b', 'c'}}, {key: Counter({'12 Main St': 3})},
                                     public, b'test', defaultdict(set), nodes, summary, review)
        self.assertEqual(summary['ambiguous_address_keys'], 1)
        self.assertEqual(summary['private_address_targets_kept'], 1)
        self.assertEqual(review[0]['candidate_ids'], [1, 2])
        self.assertNotIn('12 Main', json.dumps(nodes))
        self.assertEqual(len(lookup), 1)

    def test_default_threshold_is_five_emails(self):
        with patch.object(places, 'MIN_ADDRESS_DOCS', 5):
            _, nodes, _ = self.build([('12 Main St', list('abcd')), ('99 Other St', list('vwxyz')),
                                      ('7 Joint St Suite 1', list('abc')), ('7 Joint Street', list('cde'))])
        self.assertEqual(len(nodes), 2)  # 4 emails dropped; 3 + 3 sharing one email = 5 kept

    def test_threshold_after_union_only(self):
        evidence, nodes, _ = self.build([('6100 Redhook St', ['a', 'b']),
                                         ('6100 Red Hook St', ['b', 'c']),
                                         ('99 Other St', ['z'])])
        self.assertEqual(len(nodes), 1)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(len(next(iter(evidence.values()))), 3)

    def test_weak_address_nodes_remain_hidden_in_full_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out, private = root / 'output', root / 'data/private'
            out.mkdir()
            private.mkdir(parents=True)
            dbpath = private / 'email_places.db'
            with contextlib.closing(sqlite3.connect(dbpath)) as db, db:
                db.execute('CREATE TABLE mentions (efta, kind, text, n)')
                db.executemany('INSERT INTO mentions VALUES (?, ?, ?, ?)',
                               [(ref, 'STREET', '12 Main Street', 1) for ref in 'abcde'])
            (out / 'normalized_entities.json').write_text(json.dumps([dict(id=1, name='Jane Roe', entity_type='person')]))
            (out / 'communication_nodes.json').write_text('[]')
            with contextlib.closing(sqlite3.connect(out / 'communication_graph.db')) as db, db:
                db.execute('CREATE TABLE evidence (efta_number, canonical_source, canonical_target, relationship_type)')
                db.executemany('INSERT INTO evidence VALUES (?, ?, ?, ?)',
                               [(ref, 'kg:1', 'kg:1', 'sent_to') for ref in 'abcde'])
            with patch.multiple(places, ROOT=root, MENTIONS=dbpath, ADDRESSES=private / 'lookup.json',
                                VERDICTS=private / 'verdicts.json'), patch.object(places, 'contact_key', return_value=b'test'), \
                    patch.object(places.corpus_source, 'PATH', root / 'missing-corpus'), \
                    patch('sys.argv', ['build_email_places.py']), contextlib.redirect_stdout(io.StringIO()):
                places.main()
            nodes = json.loads((out / 'email_place_nodes.json').read_text())
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0]['label_quality'], 'not_a_name')
            self.assertEqual(nodes[0]['place_quality_reason'], 'weak_address_evidence')
            self.assertEqual(json.loads((out / 'email_place_edges.json').read_text()), [])
            self.assertEqual(len(json.loads((private / 'lookup.json').read_text())), 1)
            self.assertNotIn('12 Main Street', json.dumps(nodes))

    def test_main_edge_counts_and_public_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / 'output'
            out.mkdir()
            private = root / 'data/private'
            private.mkdir(parents=True)
            dbpath = private / 'email_places.db'
            with contextlib.closing(sqlite3.connect(dbpath)) as db, db:
                db.execute('CREATE TABLE mentions (efta, kind, text, n)')
                for text in ('358 El Brillo Way', '358 El Brillo Way, Palm Beach'):
                    db.executemany('INSERT INTO mentions VALUES (?, ?, ?, ?)',
                                   [(ref, 'STREET', text, 1) for ref in ('a', 'b', 'c')])
            public = dict(id=485, name='358 El Brillo Way', entity_type='property',
                          original_names=['358 El Brillo Way, Palm Beach'], metadata='preserved')
            (out / 'normalized_entities.json').write_text(json.dumps([public, dict(id=1, name='Some Person', entity_type='person')]))
            (out / 'communication_nodes.json').write_text('[]')
            with contextlib.closing(sqlite3.connect(out / 'communication_graph.db')) as db, db:
                db.execute('CREATE TABLE evidence (efta_number, canonical_source, canonical_target, relationship_type)')
                db.executemany('INSERT INTO evidence VALUES (?, ?, ?, ?)',
                               [(ref, 'kg:1', 'kg:1', 'sent_to') for ref in ('a', 'b', 'c')] * 2)
            with patch.multiple(places, ROOT=root, MENTIONS=dbpath, ADDRESSES=private / 'lookup.json', MIN_ADDRESS_DOCS=3,
                                VERDICTS=private / 'verdicts.json'), patch.object(places, 'contact_key', return_value=b'test'), \
                    patch('sys.argv', ['build_email_places.py']), contextlib.redirect_stdout(io.StringIO()), \
                    patch.object(places, 'signature_block', return_value=(0.8, 'Room 1-211')):
                places.main()
            nodes = json.loads((out / 'email_place_nodes.json').read_text())
            edges = json.loads((out / 'email_place_edges.json').read_text())
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0]['id'], 485)
            self.assertEqual(nodes[0]['metadata'], 'preserved')
            self.assertEqual(nodes[0]['email_document_count'], 3)
            lookup = json.loads((private / 'lookup.json').read_text())
            self.assertNotIn('signature_block', lookup['485'])
            self.assertEqual(lookup['485']['metadata']['signature_block'], 'Room 1-211')
            self.assertNotIn('[signature:', lookup['485']['value'])
            self.assertEqual(len(edges), 1)
            self.assertEqual(edges[0]['weight'], 3)
            self.assertEqual(edges[0]['metadata']['record_count'], 3)
            self.assertEqual(len(edges[0]['metadata']['sample_documents']), 3)
            self.assertEqual((private / 'lookup.json').stat().st_mode & 0o777, 0o600)
            self.assertEqual((private / 'address_merge_review.json').stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads((private / 'address_merge_review.json').read_text()), [])


if __name__ == '__main__':
    unittest.main()
