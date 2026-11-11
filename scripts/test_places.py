import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_places
from build_communications import PersonMatcher


class ReviewedPlaceLinkTests(unittest.TestCase):
    def test_prepared_alias_supplements_do_not_guess_ambiguous_streets(self):
        entities = [dict(id=1, name='9 East 71st Street', entity_type='property'),
                    dict(id=2, name='12 Main Street, Boston', entity_type='location'),
                    dict(id=3, name='12 Main Street, Miami', entity_type='location'),
                    dict(id=4, name='20 Main Street', entity_type='location'),
                    dict(id=5, name='20 Main Avenue', entity_type='location')]
        prepared = build_places.prepared_place_nodes([], entities)
        aliases = {n['id']: n.get('aliases', []) for n in prepared}
        self.assertIn('9 east 71', aliases[1])
        self.assertIn('9 East 71st', aliases[1])
        self.assertNotIn('12 main', aliases.get(2, []))
        self.assertNotIn('12 main', aliases.get(3, []))
        self.assertNotIn('20 main', aliases.get(4, []))
        self.assertNotIn('20 main', aliases.get(5, []))
        # Existing full labels are untouched, and source inputs are not mutated.
        self.assertEqual(entities[0]['name'], '9 East 71st Street')
        self.assertNotIn('aliases', entities[0])
        from entity_resolution import ExistingIndex
        enriched = {n['id']: n for n in prepared}
        self.assertEqual(ExistingIndex(enriched, {}).hits('9 East 71st'), {1})

    def test_airport_aliases_reach_existing_entity_resolution(self):
        from entity_resolution import ExistingIndex
        prepared = build_places.prepared_place_nodes([
            dict(id='lga', name='LaGuardia Airport', entity_type='location'),
            dict(id='stt', name='Cyril E. King Airport (St. Thomas)', entity_type='location'),
            dict(id='mcc', name='Metropolitan Correctional Center (MCC New York)', entity_type='location')], [])
        index = ExistingIndex({n['id']: n for n in prepared}, {})
        self.assertEqual(index.locations('LGA Airport'), {'lga'})
        self.assertEqual(index.locations('Cyril E King'), {'stt'})
        self.assertEqual(index.locations('MCC New York'), {'mcc'})
        self.assertEqual(index.locations('St Thomas'), set())

    def test_main_normalizes_curated_identity_and_retains_unlinked_airports(self):
        class Airports:
            def resolve(self, text):
                return None, 'unmatched'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, out = root / 'source', root / 'output'
            source.mkdir()
            out.mkdir()
            entities = [dict(id=1, name='Jane Roe', entity_type='person'),
                        dict(id=2, name='West Palm Beach', entity_type='location')]
            locations = [dict(slug='city', name='WEST PALM BEACH FL', type='city'),
                         dict(slug='venue', name='Sample Venue', type='venue'),
                         dict(slug='venue-duplicate', name='SAMPLE VENUE', type='venue'),
                         dict(slug='noise', name='Avenue', type='street')]
            (out / 'normalized_entities.json').write_text(json.dumps(entities))
            (source / 'locations.json').write_text(json.dumps({'data': locations}))
            (source / 'flights.json').write_text(json.dumps({'data': [dict(id=1, date='2000-01-01',
                origin='Unknown Field', destination=None, passenger_names=['Jane Roe'])]}))
            with patch.multiple(build_places, ROOT=root, SOURCE=source), patch.object(build_places, 'Airports', Airports), contextlib.redirect_stdout(io.StringIO()):
                build_places.main()
            nodes = json.loads((out / 'place_nodes.json').read_text())
            self.assertEqual({n['id'] for n in nodes}, {'place_venue', 'place_noise', 'place_airport_unknown_field'})
            self.assertEqual(next(n for n in nodes if n['id'] == 'place_noise')['label_quality'], 'not_a_name')
            self.assertEqual(json.loads((out / 'place_edges.json').read_text()), [])

    def test_links_resolve_skip_known_and_missing(self):
        entities = [dict(id=1, name='Jeffrey Epstein', entity_type='person'), dict(id=2, name='Ghislaine Maxwell', entity_type='person'),
                    dict(id=475, name='Southern Trust Company, Inc.', entity_type='shell_company'), dict(id=484, name='9 East 71st Street', entity_type='property')]
        links = [{'place': 'maxwell-townhouse', 'person': 'Ghislaine Maxwell', 'relationship': 'home_of', 'basis': 'she lived there', 'date_first': '2000'},
                 {'place': 'office', 'person': 'Southern Trust Company, Inc.', 'relationship': 'used_by', 'basis': 'its office'},
                 {'place': 'townhouse', 'person': 'Jeffrey Epstein', 'relationship': 'owned_by', 'basis': 'already in the graph'},
                 {'place': 'townhouse', 'person': 'Nobody Known', 'relationship': 'home_of', 'basis': 'not a graph person'}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            (root / 'output').mkdir()
            (root / 'config/place_links.json').write_text(json.dumps({'links': links}))
            (root / 'output/normalized_relationships.json').write_text(json.dumps(
                [dict(source_entity_id=484, target_entity_id=1, relationship_type='owned_by')]))
            with patch.object(build_places, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()) as log:
                edges = build_places.reviewed_links({'maxwell-townhouse': 'place_maxwell-townhouse', 'office': 'place_office', 'townhouse': 484},
                                                    entities, PersonMatcher(entities))
        self.assertEqual([(e['source_entity_id'], e['target_entity_id'], e['relationship_type']) for e in edges],
                         [('place_maxwell-townhouse', 2, 'home_of'), ('place_office', 475, 'used_by')])
        self.assertEqual(edges[0]['metadata']['basis'], 'she lived there')
        self.assertEqual(edges[0]['date_first'], '2000')
        self.assertIn('Nobody Known', log.getvalue())


if __name__ == '__main__':
    unittest.main()
