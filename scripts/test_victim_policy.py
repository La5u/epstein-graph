"""Victim node typing comes only from curated lists; missing configs stay empty."""
import json
import tempfile
import unittest
from pathlib import Path

import victim_policy


class VictimPolicyTests(unittest.TestCase):
    def put(self, directory, name, value):
        path = Path(directory) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def test_ids_from_all_curated_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            victim = self.put(directory, 'victims.json', {'names': ['Nerina Vellorin']})
            survivor = self.put(directory, 'survivors.json', {'names': ['VELLOR']})
            registry = self.put(directory, 'registry.json', [
                {'name': 'Registry Victim', 'aliases': ['Alias Victim'], 'category': 'victim'},
                {'name': 'Not Victim', 'category': 'associate'}])
            entities = [
                {'id': 1, 'name': 'VELLOR'},
                {'id': 2, 'name': 'Nerina Vellorin'},
                {'id': 3, 'name': 'Person', 'original_names': ['Alias Victim']},
                {'id': 4, 'name': 'Labelled', 'metadata': '{"person_type": "victim"}'},
                {'id': 5, 'name': 'Not Victim'},
                {'id': 6, 'name': 'Bad Metadata', 'metadata': 'not json'},
            ]
            relationships = [{'source_entity_id': 7, 'target_entity_id': 1, 'relationship_type': 'victim_of'}]
            ids = victim_policy.victim_ids(entities, relationships, config=victim,
                                           survivor_config=survivor, registry=registry)
            self.assertEqual(ids, {1, 2, 3, 4, 7})

    def test_missing_configs_degrade_to_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'missing.json'
            self.assertEqual(victim_policy.victim_ids([{'id': 1, 'name': 'Someone'}],
                                                      config=missing, survivor_config=missing,
                                                      registry=missing), set())
            self.assertEqual(victim_policy.extra_nodes(missing), [])

    def test_label_on_merged_record_survives_merge(self):
        from survivor_policy import survivor_ids
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'missing.json'
            survivors = self.put(directory, 'survivors.json', {'names': []})
            merged = {'id': 1, 'name': 'Same Name', 'original_records': [
                {'id': 1, 'name': 'Same Name'},
                {'id': 9, 'name': 'Same Name', 'metadata': '{"person_type": "victim"}'}]}
            self.assertEqual(victim_policy.victim_ids([merged], config=missing, survivor_config=missing,
                                                      registry=missing), {1})
            self.assertEqual(survivor_ids([merged], config=survivors), {1})

    def test_designator_search_includes_plural_forms(self):
        from contextlib import closing
        import sqlite3
        from build_victims import collect_designator_docs
        from adapters import efta
        with closing(sqlite3.connect(':memory:')) as db:
            efta.create(db, [('EFTA1', 0, 'Minor Victims-3 testified'), ('EFTA2', 0, 'Jane Does #2 and others')])
            efta.install_views(db)
            docs = collect_designator_docs(db)
        self.assertEqual(docs['Minor Victim-3'], {'EFTA1'})
        self.assertEqual(docs['Jane Doe #2'], {'EFTA2'})

    def test_links_in_crowded_documents_count_less(self):
        from contextlib import closing
        import sqlite3
        from build_victims import build
        with tempfile.TemporaryDirectory() as directory:
            corpus, comention = Path(directory) / 'corpus.db', Path(directory) / 'comention.db'
            crowd = list(range(10, 30))  # 20 extra names in every crowded document
            crowded = [f'C{i}' for i in range(4)]
            quiet = [f'Q{i}' for i in range(3)]
            from adapters import efta
            with closing(sqlite3.connect(corpus)) as db:
                efta.create(db, [(d, 0, 'Jane Doe #1') for d in crowded + quiet])
            with closing(sqlite3.connect(comention)) as db, db:
                db.execute('CREATE TABLE entities (id, name, entity_type)')
                db.execute('CREATE TABLE mentions (entity_id, efta_number)')
                db.executemany('INSERT INTO entities VALUES (?, ?, ?)',
                               [(1, 'Jeffrey Epstein', 'person'), (2, 'Crowded Name', 'person'),
                                (3, 'Quiet Name', 'person')] + [(i, f'P{i}', 'person') for i in crowd])
                db.executemany('INSERT INTO mentions VALUES (?, ?)',
                               [(1, d) for d in crowded + quiet] + [(2, d) for d in crowded]
                               + [(3, d) for d in quiet] + [(i, d) for i in crowd for d in crowded])
            nodes, edges, summary, documents = build(corpus, comention, min_shared=3)
        self.assertEqual(documents['victim_jane_doe_1'], sorted(crowded + quiet))
        self.assertEqual(nodes[0]['pages_by_context'], {'unspecified': 7})
        by_target = {e['target_entity_id']: e for e in edges}
        self.assertIn(3, by_target)        # 3 documents naming only it and the designator
        self.assertNotIn(2, by_target)     # 4 documents, each naming 22 people
        self.assertEqual(by_target[3]['metadata']['specificity_score'], 1.5)  # 3 x 1/(3 - 1)
        self.assertEqual(edges[0]['target_entity_id'], 3)
        self.assertGreater(summary['dropped_as_only_crowded_documents'], 0)

    def test_extra_nodes_rejects_bad_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.put(directory, 'victims.json',
                              {'extra_nodes': [{'name': 'A', 'efta': 'EFTA1'}, {'note': 'no name'}, 'junk']})
            self.assertEqual([n['name'] for n in victim_policy.extra_nodes(config)], ['A'])

    def test_shipped_names_cover_curated_lists(self):
        configs = []
        for path in (victim_policy.VICTIM_CONFIG, victim_policy.SURVIVOR_CONFIG):
            configs.append(json.loads(path.read_text()) if path.exists() else {})
        if not any(config.get('names') for config in configs):
            self.skipTest('Shipped curated name lists are empty')
        expected = {victim_policy.name_key(name)
                    for config in configs for name in config.get('names', [])
                    if isinstance(name, str) and name.strip()}
        expected.update(victim_policy.name_key(name)
                        for name in victim_policy.registry_victim_names() if name)
        self.assertEqual(victim_policy.victim_names(), expected)
        self.assertEqual(victim_policy.extra_nodes(),
                         [node for node in configs[0].get('extra_nodes', [])
                          if isinstance(node, dict) and node.get('name')])

    def test_viewer_defines_victim_type_and_edge(self):
        from visualize_graph import COLORS, EDGE_COLORS
        self.assertIn('survivor', COLORS)
        self.assertNotIn('victim', COLORS)
        self.assertIn('victim_of', EDGE_COLORS)
        self.assertIn('victim_document_co_mention', EDGE_COLORS)

    def test_designators_in_text_parses_and_skips_citations(self):
        from build_victims import designators_in_text, slug
        text = ('Jane Doe #2 met Jane Doe No. 2 and Minor Victim-3; '
                'see Doherty v. John Doe No. 22; JOHN DOE #1, an individual')
        found = designators_in_text(text)
        self.assertEqual(found['Jane Doe #2'], 2)
        self.assertEqual(found['Minor Victim-3'], 1)
        self.assertEqual(found['John Doe #1'], 1)
        self.assertNotIn('John Doe #22', found)
        self.assertEqual(slug('Jane Doe #2'), 'victim_jane_doe_2')
        self.assertEqual(designators_in_text(None), {})

    def test_context_of(self):
        from build_victims import context_of
        self.assertEqual(context_of('Jane Doe #1 and Jane Doe #2 v. United States'), 'CVRA case (Jane Doe v. United States)')
        self.assertEqual(context_of("Claimant Jane Doe 981 presents this Creditor's Claim"), 'estate / compensation claims')
        self.assertEqual(context_of('Jane Doe No. 4 v. Jeffrey Epstein'), 'civil suits against Epstein')
        self.assertEqual(context_of('Jane Doe 7'), 'unspecified')


if __name__ == '__main__':
    unittest.main()
