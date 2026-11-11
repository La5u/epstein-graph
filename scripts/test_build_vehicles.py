"""Vehicle evidence gates, FTS matching, deduplication and combined graph wiring."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build as build_pipeline
import build_contacts
import build_vehicles as vehicles
import corpus
from adapters import efta


def spec(name, kind='boat', aliases=(), registration=None, **extra):
    return dict(name=name, kind=kind, aliases=list(aliases), registration=registration,
                note='Reviewed synthetic evidence.', sample_eftas=['EFTA00000001', 'EFTA00000002'], **extra)


class VehicleCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'corpus.db'
        pages = [
            ('EFTA00000001', 1, 'Jeffrey Epstein and Jane Pilot used motor yacht Sea Bird at Little St. James. Aircraft N123AB was a Cessna. Hidden Person was named.'),
            ('EFTA00000001', 2, 'Sea Bird yacht. Jeffrey Epstein. Cessna aircraft N123AB.'),
            ('EFTA00000002', 1, 'Boat Sea-Bird, tender registration 1234-TC, took Jeffrey Epstein and Jane Pilot to Little St. James. Helicopter N123AB. Hidden Person.'),
            ('EFTA00000003', 1, 'Yacht Sea Bird. Jeffrey Epstein and Jane Pilot. Little St. James. Aircraft N123AB.'),
            ('EFTA00000004', 1, 'M/Y Lone Star. Jane Pilot. S/Y Wind Song. USCG Doc. No.: 1079189.'),
            ('EFTA00000005', 1, 'M/Y Lone Star. Jane Pilot. S/Y Wind Song.'),
            ('EFTA00000006', 1, 'Jeffrey Epstein wrote poetry titled Sea Bird. Sea Birds and Sea Birding are other titles.'),
            ('EFTA00000007', 1, 'M/Y Unreviewed. Hull identification number: ABC12345D678. Helicopter N777ZZ.'),
            ('EFTA00000008', 1, 'M/Y Unreviewed. Helicopter N777ZZ. Bad aircraft registrations N0001, N123IO and N123456.'),
            ('N999ZZ', 1, 'Nothing relevant here.'),
        ]
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            efta.create(db, pages, [(d, 2 if d == 'EFTA00000001' else 1) for d in sorted({p[0] for p in pages})])
        self.canonical = {'EFTA00000003': 'EFTA00000001', 'EFTA00000005': 'EFTA00000004'}
        self.specs = [spec('Sea Bird', aliases=['Sea-Bird'], registration='1234-TC'),
                      spec('Lone Star'), spec('N123AB (Cessna)', 'aircraft', ['N123AB'], 'N123AB'),
                      spec('N34JE (Helicopter)', 'aircraft', ['N34JE'], 'N34JE', kg_id=496)]
        self.entities = [
            dict(id=1, name='Jeffrey Epstein', normalized_name='jeffrey epstein', entity_type='person'),
            dict(id=2, name='Jane Pilot', normalized_name='jane pilot', entity_type='person'),
            dict(id=3, name='Hidden Person', normalized_name='hidden person', entity_type='person'),
            dict(id=4, name='Little St. James, USVI', entity_type='property'),
            dict(id=496, name='N34JE (Helicopter)', entity_type='aircraft', metadata='{"original": true}'),
        ]

    def put(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value))

    def test_discovery_never_promotes_unreviewed_names_and_ignores_invalid_tails(self):
        with contextlib.closing(corpus.connect(self.path)) as db:
            candidates, pages = vehicles.discover(db, self.canonical)
            docs, samples = vehicles.reviewed_mentions(db, self.specs)
        names = {c['name']: c for c in candidates}
        self.assertIn('N777ZZ', names)
        self.assertIn('wind song', names)
        self.assertIn('1079189', names)
        self.assertIn('ABC12345D678'.casefold(), names)
        for invalid in ('N0001', 'N123IO', 'N123456', 'N999ZZ'):
            self.assertNotIn(invalid, names)
        nodes, _, _, rejected = vehicles.make_nodes(self.specs, docs, samples, self.entities, self.canonical)
        self.assertEqual({n['name'] for n in nodes}, {'Sea Bird', 'N123AB (Cessna)', 'N34JE (Helicopter)'})
        self.assertEqual(rejected, [{'name': 'Lone Star', 'distinct_documents': 1}])
        self.assertGreater(pages, 0)

    def test_documents_not_pages_and_duplicate_copies_count_once(self):
        with contextlib.closing(corpus.connect(self.path)) as db:
            docs, samples = vehicles.reviewed_mentions(db, self.specs)
        self.assertEqual(docs[0], {'EFTA00000001', 'EFTA00000002', 'EFTA00000003'})
        self.assertEqual(vehicles.distinct_count(docs[0], self.canonical), 2)
        nodes, mentions, phrases, _ = vehicles.make_nodes(self.specs, docs, samples, self.entities, self.canonical)
        by_name = {n['name']: n for n in nodes}
        sea = by_name['Sea Bird']['id']
        self.assertEqual(by_name['Sea Bird']['metadata']['document_count'], 3)
        self.assertEqual(by_name['Sea Bird']['metadata']['distinct_documents'], 2)
        legacy = by_name['N34JE (Helicopter)']
        self.assertEqual(legacy['id'], 496)
        self.assertEqual(legacy['entity_type'], 'vehicle')
        self.assertEqual(legacy['vehicle_type'], 'aircraft')
        self.assertFalse(legacy['metadata']['corpus_corroborated'])
        self.assertTrue(legacy['metadata']['original'])
        self.assertEqual(mentions[496], set())
        target_docs = {1: set(docs[0]), 2: {'EFTA00000001', 'EFTA00000003'},
                       4: {'EFTA00000001', 'EFTA00000002'}}
        phrases.update({1: {'Jeffrey Epstein'}, 2: {'Jane Pilot'}, 4: {'Little St. James'}})
        edges = vehicles.make_edges(mentions, target_docs, phrases, self.canonical, 8)
        sea_edges = [e for e in edges if e['source_entity_id'] == sea]
        self.assertEqual({e['target_entity_id'] for e in sea_edges}, {1, 4})
        self.assertTrue(all(e['weight'] == 2 for e in sea_edges))
        self.assertTrue(all(e['metadata']['shared_documents'] == 2 for e in sea_edges))
        for edge in edges:
            self.assertEqual(edge['relationship_type'], 'co_mentioned_in_documents')
            self.assertIn('specificity_score', edge['metadata'])
            self.assertIn('lift_vs_chance', edge['metadata'])
            citations = {d['efta_number'] for d in edge['metadata']['sample_documents']}
            self.assertTrue(citations <= mentions[edge['source_entity_id']] & target_docs[edge['target_entity_id']])
            self.assertEqual(len({self.canonical.get(d, d) for d in citations}), len(citations))

    def test_second_occurrence_and_token_boundaries(self):
        text = 'Sea Bird is a poem. ' + 'irrelevant ' * 70 + 'M/Y Sea Bird sails.'
        matches = list(vehicles.qualified_matches(text, vehicles.phrase_pattern('Sea Bird'), 'boat'))
        self.assertEqual(len(matches), 1)
        self.assertIn('M/Y Sea Bird', matches[0])
        self.assertEqual(list(vehicles.qualified_matches('Boat Sea Birds.', vehicles.phrase_pattern('Sea Bird'), 'boat')), [])

    def test_main_outputs_with_synthetic_corpus_and_survivor_exclusion(self):
        self.put('config/vehicles.json', {'vehicles': self.specs})
        self.put('config/survivors.json', {'names': ['Hidden Person']})
        self.put('data/raw/persons_registry.json', [])
        self.put('output/normalized_entities.json', self.entities)
        self.put('output/normalized_relationships.json', [])
        # A place layer node participates without adding a duplicate KG place.
        self.put('output/place_nodes.json', [dict(id='place_lsj', name='Little St. James', entity_type='location')])
        with contextlib.closing(sqlite3.connect(self.root / 'output/document_clusters.db')) as db:
            db.execute('CREATE TABLE docs(efta_number TEXT, canonical TEXT)')
            db.executemany('INSERT INTO docs VALUES (?, ?)', self.canonical.items())
            db.commit()
        with patch.object(vehicles, 'ROOT', self.root), patch.object(corpus, 'PATH', self.path), \
                patch.object(corpus, 'document_urls', return_value={}), contextlib.redirect_stdout(io.StringIO()):
            vehicles.main()
        out = self.root / 'output'
        nodes = json.loads((out / 'vehicle_nodes.json').read_text())
        edges = json.loads((out / 'vehicle_edges.json').read_text())
        summary = json.loads((out / 'vehicle_summary.json').read_text())
        evidence = json.loads((out / 'vehicle_evidence.json').read_text())
        self.assertEqual(len(nodes), 3)
        self.assertEqual(summary['reused_kg_ids'], [496])
        self.assertEqual(summary['survivors_excluded'], 1)
        self.assertTrue(any(e['target_entity_id'] == 'place_lsj' for e in edges))
        self.assertFalse(any(e['target_entity_id'] == 3 for e in edges))
        self.assertFalse(any(e['source_entity_id'] == 496 for e in edges))
        self.assertEqual(len(evidence), len(nodes))
        self.assertEqual(summary['edges'], len(edges))
        # Stable IDs and edge counts across reruns; no previous output drives discovery.
        with patch.object(vehicles, 'ROOT', self.root), patch.object(corpus, 'PATH', self.path), \
                patch.object(corpus, 'document_urls', return_value={}), contextlib.redirect_stdout(io.StringIO()):
            vehicles.main()
        self.assertEqual(json.loads((out / 'vehicle_nodes.json').read_text()), nodes)
        self.assertEqual(json.loads((out / 'vehicle_edges.json').read_text()), edges)

    def test_ambiguous_cross_vehicle_alias_rejected(self):
        self.put('review.json', {'vehicles': [spec('One', aliases=['Same Boat']), spec('Two', aliases=['Same-Boat'])]})
        with self.assertRaisesRegex(ValueError, 'Duplicate vehicle alias'):
            vehicles.load_review(self.root / 'review.json')

    def test_kg_aircraft_registration_reused_without_explicit_config_id(self):
        kg = dict(id=493, name='N123AB (Cessna)', entity_type='aircraft')
        nodes, _, _, _ = vehicles.make_nodes([self.specs[2]], {0: {'a', 'b'}}, {0: {}}, [kg], {})
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]['id'], 493)

    def test_duplicate_cluster_does_not_join_names_on_different_copies(self):
        edges = vehicles.make_edges({'boat': {'a', 'b'}}, {1: {'copy-a', 'copy-b'}},
                                    {'boat': {'Sea Bird'}, 1: {'Jane Pilot'}},
                                    {'copy-a': 'a', 'copy-b': 'b'}, 2)
        self.assertEqual(edges, [])

    def test_bounded_samples_prefer_efta_and_one_copy_per_cluster(self):
        samples = {}
        vehicles.remember_sample(samples, 'HOUSE_OTHER', 'other')
        for i in range(20):
            vehicles.remember_sample(samples, f'EFTA{i:08}', str(i), {'EFTA00000001': 'EFTA00000000'})
        self.assertEqual(len(samples), vehicles.SAMPLE_LIMIT)
        self.assertNotIn('HOUSE_OTHER', samples)
        self.assertIn('EFTA00000000', samples)
        self.assertNotIn('EFTA00000001', samples)

    def test_long_documents_still_have_actual_pair_citations(self):
        target_docs = {i: {'a', 'b'} for i in range(45)}
        phrases = {i: {f'Example Person {i}'} for i in target_docs}
        phrases['boat'] = {'Sea Bird'}
        edges = vehicles.make_edges({'boat': {'a', 'b'}}, target_docs, phrases, {}, 2)
        self.assertEqual(len(edges), 45)
        self.assertTrue(all(e['metadata']['specificity_score'] == 0 for e in edges))
        self.assertTrue(all({d['efta_number'] for d in e['metadata']['sample_documents']} == {'a', 'b'} for e in edges))


class VehicleIntegrationTests(unittest.TestCase):
    def test_build_dependency_precedes_combined_graph(self):
        steps = {s.name: s for s in build_pipeline.steps()}
        self.assertIn('build_vehicles', steps['analyze_graph'].after)
        self.assertTrue({'build_places', 'build_email_places', 'dedupe_documents'} <= set(steps['build_vehicles'].after))

    def test_combined_graph_preserves_kg_ids_and_loads_vehicle_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / 'output'
            out.mkdir()
            (root / 'data/raw').mkdir(parents=True)
            (root / 'data/raw/extracted_entities_filtered.json').write_text('{}')
            kg = [dict(id=493, name='N908JE (Boeing)', entity_type='aircraft'),
                  dict(id=496, name='N34JE (Helicopter)', entity_type='aircraft'),
                  dict(id=1, name='Jeffrey Epstein', entity_type='person')]
            boat = dict(id='vehicle_boat_example', name='Example', entity_type='vehicle', vehicle_type='boat')
            enriched = {**kg[0], 'entity_type': 'vehicle', 'vehicle_type': 'aircraft', 'registration': 'N908JE'}
            edge = dict(id='vehicle_test', source_entity_id=boat['id'], target_entity_id=1,
                        relationship_type='co_mentioned_in_documents', weight=2)
            older = {**edge, 'id': 'older_plain_name', 'source_entity_id': 1, 'target_entity_id': boat['id']}
            ownership = {**edge, 'id': 'ownership', 'relationship_type': 'owned_by'}
            for name, data in [('normalized_entities.json', kg), ('normalized_relationships.json', []),
                               ('vehicle_nodes.json', [boat, enriched]), ('vehicle_edges.json', [edge]),
                               ('comention_edges.json', [older, ownership])]:
                (out / name).write_text(json.dumps(data))
            with patch.object(build_contacts, 'ROOT', root), patch.object(build_contacts, 'contact_key', return_value=b'x'), \
                    patch.object(build_contacts, 'email_places_available', return_value=False), \
                    patch.object(build_contacts, 'normalize_main'), patch.object(build_contacts, 'build', return_value=([], [], [], {})), \
                    patch.object(build_contacts, 'apply_identity_map', side_effect=lambda nodes, edges, path: (nodes, edges)), \
                    patch.object(build_contacts, 'apply_victim_type'), patch.object(build_contacts, 'load_annotations'), \
                    contextlib.redirect_stdout(io.StringIO()):
                build_contacts.main()
            nodes = {n['id']: n for n in json.loads((out / 'graph_entities.json').read_text())}
            self.assertEqual(nodes[493]['registration'], 'N908JE')
            self.assertEqual(nodes[496]['entity_type'], 'vehicle')
            self.assertEqual(nodes[496]['vehicle_type'], 'aircraft')
            self.assertEqual(nodes[boat['id']], boat)
            self.assertEqual(json.loads((out / 'graph_relationships.json').read_text()), [ownership, edge])


if __name__ == '__main__':
    unittest.main()
