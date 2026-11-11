import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from involvement import RANKING_COLUMNS, document_reason_rows, input_stamp, write_ranking_tables

from build_comentions import clean_phrase, entity_phrases, fts_query, overlapping_terms, possibly_same_entity
from cooccur import build_query
from normalize_graph import registry_aliases


def person(i, name, **extra):
    return dict(id=i, name=name, normalized_name=' '.join(name.casefold().split()), entity_type='person', **extra)


class CoMentionTests(unittest.TestCase):
    def test_phrase_safety(self):
        self.assertEqual(clean_phrase('Leon Black'), 'Leon Black')
        for unsafe in ('Ross', 'Warren ?', 'J Voss', 'Passenger (2)', '36752-G-1159B-N908JE-PBI'):
            self.assertIsNone(clean_phrase(unsafe), unsafe)
        self.assertIsNone(clean_phrase('9 East 71st Street'))
        self.assertEqual(clean_phrase('9 East 71st Street', allow_digits=True), '9 East 71st Street')
        self.assertEqual(fts_query('Jean-Luc "Brunel"'), '"Jean Luc Brunel"')

    def test_entity_phrases(self):
        self.assertEqual(entity_phrases(dict(id=1, name='N908JE (Boeing 727-31)', entity_type='aircraft'), {}), {'N908JE'})
        self.assertEqual(entity_phrases(person(2, 'Hidden Name', metadata='{"person_type": "victim"}'), {}), set())
        self.assertIn('Clara Wexley-Orvale', entity_phrases(person(3, 'Wexley-Orvale, Clara'), {}))
        self.assertEqual(entity_phrases(dict(id=4, name='Zorro Ranch, Stanley, NM', entity_type='property'), {}), {'Zorro Ranch'})

    def test_identity_and_overlap_rules(self):
        same = lambda a, b: possibly_same_entity(person(1, a), person(2, b), {1, 2}, {3, 4})
        self.assertTrue(same('Jeffrey Epstein', 'Jeffrey E. Epstein'))
        self.assertTrue(same('Ghislaine Maxwell', 'Ms. Maxwell'))
        self.assertTrue(same('Glenn Dubin', 'Glen Dubin'))
        self.assertFalse(same('Larry Summers', 'Lori Summers'))
        self.assertFalse(same('Jeffrey Epstein', 'Mark Epstein'))
        self.assertFalse(possibly_same_entity(person(1, 'Leon Black'), person(2, 'Jes Staley'), {1, 2}, {1, 2}))
        self.assertFalse(possibly_same_entity(
            dict(id=1, name='Alpha Company', entity_type='organization'),
            dict(id=2, name='Beta Company', entity_type='organization'), {1}, {1}))
        self.assertTrue(overlapping_terms({'Deutsche Bank'}, {'Deutsche Bank Securities Inc.'}))
        self.assertFalse(overlapping_terms({'Leon Black'}, {'Jes Staley'}))

    def test_registry_aliases_are_unambiguous(self):
        registry = [{'name': 'Alexander Acosta', 'aliases': ['Alex Acosta']}]
        self.assertEqual(dict(registry_aliases([person(1, 'Alexander Acosta')], registry)), {1: ['Alex Acosta']})
        # A registry row naming two different graph people is ignored entirely.
        registry[0]['aliases'].append('Alex Smith')
        self.assertEqual(dict(registry_aliases([person(1, 'Alexander Acosta'), person(2, 'Alex Smith')], registry)), {})

    def test_cooccur_query(self):
        self.assertEqual(build_query(['Leon Black', 'Jes Staley'], 0), 'text_content:"Leon Black" AND text_content:"Jes Staley"')
        self.assertEqual(build_query(['A B', 'C'], 10), 'text_content:NEAR("A B" "C", 10)')


class InvolvementProvenanceTests(unittest.TestCase):
    def test_all_reasons_survive_without_sample_truncation(self):
        statements = {f'EFTA{i:08}' for i in range(10)}
        overlap = 'EFTA00000000'
        reasons = list(document_reason_rows(
            7, statements, {overlap}, {overlap}, {overlap, 'privacy-only'}))
        reasons += list(document_reason_rows(8, {overlap}, set(), set(), set()))
        with closing(sqlite3.connect(':memory:')) as db:
            write_ranking_tables(db, [], reasons)
            self.assertEqual(db.execute('SELECT count(*) FROM document_reasons').fetchone()[0], 15)
            self.assertEqual(set(db.execute(
                'SELECT reason FROM document_reasons WHERE entity_id=7 AND efta=?',
                (overlap,))), {('statement',), ('testimony',), ('lexical_victim_context',), ('privacy',)})
            self.assertEqual(db.execute(
                "SELECT reason FROM document_reasons WHERE efta='privacy-only'"
            ).fetchall(), [('privacy',)])
            self.assertEqual(db.execute(
                "SELECT count(*) FROM document_reasons WHERE reason='statement'"
            ).fetchone()[0], 11)
            indexes = db.execute('PRAGMA index_list(document_reasons)').fetchall()
            self.assertGreaterEqual(len(indexes), 3)

    def test_empty_ranking_preserves_legacy_schema(self):
        with closing(sqlite3.connect(':memory:')) as db:
            write_ranking_tables(db, [], [])
            self.assertEqual(tuple(r[1] for r in db.execute('PRAGMA table_info(ranking)')),
                             RANKING_COLUMNS)
            self.assertEqual(db.execute('SELECT * FROM ranking').fetchall(), [])
            self.assertEqual(db.execute('SELECT * FROM document_reasons').fetchall(), [])

    def test_input_stamps_are_metadata_only(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'input.json'
            path.write_text('private contents')
            stamp = input_stamp(path)
            self.assertEqual(stamp, {'path': str(path.resolve()), 'exists': True,
                                     'size_bytes': path.stat().st_size,
                                     'mtime_ns': path.stat().st_mtime_ns})
            self.assertNotIn('private contents', str(stamp))
            path.unlink()
            self.assertFalse(input_stamp(path)['exists'])


if __name__ == '__main__':
    unittest.main()
