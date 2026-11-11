import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import node_documents as docs
import private_contacts as private
import emails
from adapters import efta_emails


from private_contacts import duplicate_groups, email_keys, name_keys, phone_keys
import ocr_consensus as _ocr_consensus
_ocr_consensus.PATH = _ocr_consensus.PATH.with_name('nonexistent_ocr_consensus_for_tests.json')  # tests never read the live re-OCR output


class PrivateContactTests(unittest.TestCase):
    def test_duplicate_rules(self):
        self.assertIn(('email: Gmail ignores dots and +tags', 'jeevacation@gmail.com'), list(email_keys('jee.vacation+x@googlemail.com')))
        self.assertIn(('email: same address without +tag', 'a@corp.test'), list(email_keys('A+news@corp.test')))
        self.assertEqual(list(name_keys('Smith, John')), list(name_keys('john smith.')))
        self.assertEqual(list(name_keys('Madonna')), [])
        self.assertIn(('phone: US number with/without leading 1 (assumed)', '2125551234'), list(phone_keys('+12125551234')))
        lookup = {'e1': {'kind': 'email', 'value': 'Jane@corp.test', 'occurrences': 1},
                  'e2': {'kind': 'email', 'value': 'jane@corp.test', 'occurrences': 5},
                  'e3': {'kind': 'email', 'value': 'other@corp.test', 'occurrences': 1}}
        self.assertEqual(duplicate_groups(lookup), [('email: same address ignoring case', ['e2', 'e1'])])


class PrivateContactPerformanceTests(unittest.TestCase):
    def test_lookup_stamp_ignores_unrelated_configs_but_tracks_name_reviews(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            review = root / 'config/node_review.json'
            unrelated = root / 'config/places.json'
            review.write_text('{}')
            unrelated.write_text('{}')
            with patch.object(private, 'ROOT', root), patch.object(private, 'PRIVATE', root / 'private'), \
                    patch.object(private.email_source, 'stamp_paths', return_value=[]):
                before = private.lookup_stamp(b'key')
                unrelated.write_text('{"places": []}')
                self.assertEqual(private.lookup_stamp(b'key'), before)
                review.write_text('{"participants": {}}')
                after = private.lookup_stamp(b'key')
                self.assertNotEqual(after, before)
                (root / 'config/document_attributions.json').write_text('{}')
                self.assertNotEqual(private.lookup_stamp(b'key'), after)

    def test_grouped_participants_keep_first_seen_spelling_ties(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data/raw').mkdir(parents=True)
            rows = [(1, 'D1', 0, 'Jane Roe', None, '["Other Person"]', '[]', None, 0),
                    (2, 'D2', 0, 'JANE ROE', None, '["Third Person"]', '[]', None, 0),
                    (3, 'D3', 0, 'Jane Roe', None, '["Other Person"]', '[]', None, 0),
                    (4, 'D4', 0, 'JANE ROE', None, '["Third Person"]', '[]', None, 0)]
            efta_emails.create(headers_path=root / 'data/raw/email_metadata.db', headers=rows)
            with emails.use_root(root), patch('build_communications.node_review', return_value={'participants': {}}):
                labels = private.collect_participants(b'key', external=[])
                builder = private.Builder(b'key', [])
                identity = builder.participant_id('jane roe')
            self.assertEqual(labels[identity].most_common(), [('Jane Roe', 2), ('JANE ROE', 2)])

    def test_lookup_revalidation_preserves_artifact_and_checks_its_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'private'
            folder.mkdir()
            cache = folder / '.lookup_cache.json'
            lookup = {'email_a': {'kind': 'email', 'value': 'a@example.test'}}
            original = json.dumps({'stamp': 'old', 'lookup': lookup})
            cache.write_text(original)
            with patch.object(private, 'ROOT', root), patch.object(private, 'PRIVATE', folder), \
                    patch.object(private, 'lookup_stamp', return_value='new'), \
                    patch.object(private, 'replay_lookup', return_value=lookup) as replay:
                self.assertEqual(private.build_lookup(b'key'), lookup)
                replay.assert_called_once()
                self.assertEqual(cache.read_text(), original)
                replay.reset_mock()
                self.assertEqual(private.build_lookup(b'key'), lookup)
                replay.assert_not_called()
                cache.write_text(json.dumps({'stamp': 'old', 'lookup': {}}))
                self.assertEqual(private.build_lookup(b'key'), lookup)
                replay.assert_called_once()

    def test_link_caps_keep_totals_order_and_first_records(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'documents.db'
            with sqlite3.connect(path) as db:
                db.executescript('CREATE TABLE edge_documents(edge_id,efta,page,how);'
                                 'CREATE TABLE edge_records(edge_id,kind,ref,url);'
                                 'CREATE TABLE node_documents(node_id,efta,edge_id,page);'
                                 'CREATE TABLE node_records(node_id,kind,ref,url);')
                db.executemany('INSERT INTO edge_documents VALUES (?,?,0,?)',
                               [(edge, f'E{i}', 'names') for edge, count in [('a', 5), ('b', 3)] for i in range(count)])
                db.executemany('INSERT INTO edge_records VALUES (?,?,?,?)', [('a', 'kind', str(i), 'url') for i in range(4)])
            rels = [dict(id=edge, source_entity_id=1, target_entity_id=2, relationship_type='link', weight=weight)
                    for edge, weight in [('b', 100), ('a', 1)]]
            with patch.object(docs, 'LINK_LIMIT', 2):
                sections = docs.link_sections(path, rels, str)['1']
            self.assertEqual([s[0] for s in sections], ['e-a', 'e-b'])
            self.assertEqual([s[3] for s in sections], [5, 3])
            self.assertEqual([d[0] for d in sections[0][2]], ['E0', 'E1'])
            self.assertEqual([r[1] for r in sections[0][4]], ['0', '1'])

    def test_page_revalidation_keeps_private_stamp_and_mapping_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'output').mkdir()
            folder = root / 'private/node_docs'
            folder.mkdir(parents=True)
            entities = [dict(id=i, name=f'Person {i}') for i in (1, 2)]
            (root / 'output/graph_entities.json').write_text(json.dumps(entities))
            (root / 'output/identity_map.json').write_text('{}')
            (root / 'output/graph_relationships.json').write_text('[]')
            (folder / '.cache-key').write_text(b'key'.hex())
            pages = {str(i): f'node_docs/node_{i}.html' for i in (1, 2)}
            original = json.dumps({'stamp': 'old', 'pages': pages})
            (folder / '.stamp.json').write_text(original)
            for page in pages.values():
                (folder.parent / page).write_text('existing page')
            with patch.object(docs, 'ROOT', root), patch.object(docs, 'cache_stamp', return_value='new'), \
                    patch.object(docs, 'link_sections', return_value={}), patch.object(docs, 'url_registry', return_value={}), \
                    patch('multiprocessing.get_context') as context:
                pool = context.return_value.Pool
                pool.return_value.__enter__.return_value.imap_unordered.return_value = [2, None, 1]
                self.assertEqual(list(docs.write_pages({}, folder)), [1, 2])
                self.assertEqual((folder / '.stamp.json').read_text(), original)
                pool.reset_mock()
                self.assertEqual(list(docs.write_pages({}, folder)), [1, 2])
                pool.assert_not_called()


class FullerLabelTests(unittest.TestCase):
    def test_decorated_labels_keep_raw_values_and_protect_survivors(self):
        lookup = {f'p{n}': {'kind': 'participant', 'value': label} for n, label in enumerate([
            'Zelda-A Hargrave', 'Nerissa Hargrave-X', 'NYC - Zelda Hargrave', 'Governor Zelda Hargrave',
            'Zelda Hargrave Secretary', 'NY Zelda', 'Anne-Marie Hargrave', 'Zelda Hargrave Assistant',
            'NY Times best-', 'London St Pancras', 'Gov Mapp monday', "Ambassador Churkin's Office",
            'Zelda-B Hargrave', 'Zelda Hargrave-M'])}
        repairs = private.cleaned_header_labels(lookup)
        self.assertEqual(set(repairs), {'p0', 'p1', 'p2', 'p3', 'p4'})
        self.assertEqual(repairs['p0']['value'], 'Zelda Hargrave')
        self.assertEqual(lookup['p0']['value'], 'Zelda-A Hargrave')
        self.assertEqual(set(private.cleaned_header_labels(lookup, ['Zelda Hargrave'])), {'p1'})

    def test_truncated_word_completed_only_from_known_names(self):
        p = lambda v, n=1: {'kind': 'participant', 'value': v, 'occurrences': n}
        lookup = {'c1': p('Geor Harlowe', 9), 'm1': p('George Harlowe'), 'c2': p('Peter Thistl', 9), 'm2': p('Peter Thistle'),
                  'c3': p('Lesley Groff', 9), 'm3': p('Lesley Groff Imailto'), 'm4': p('Lesley Groffill')}
        lookup.update({f'k{i}': p(f'{first} {last}', 5) for i, (first, last) in enumerate(
            [('George', 'Smith'), ('George', 'Jones'), ('George', 'Brown'), ('Ann', 'Thistle'), ('Bo', 'Thistle'), ('Cy', 'Thistle'),
             ('Al', 'Thistl'), ('Hy', 'Groff'), ('Ed', 'Groffill'), ('Fa', 'Groffill'), ('Gu', 'Groffill')])})
        im = {'m1': {'canonical': 'c1'}, 'm2': {'canonical': 'c2'}, 'm3': {'canonical': 'c3'}, 'm4': {'canonical': 'c3'}}
        self.assertEqual(private.fuller_labels(lookup, im), {'c1': 'George Harlowe'})


if __name__ == '__main__':
    unittest.main()
