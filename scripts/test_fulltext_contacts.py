import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_fulltext_contacts
import export_neo4j
from extract_contacts import extract, mentions_identical
from node_documents import written_documents


class ExtractTests(unittest.TestCase):
    def test_reordered_mentions_preserve_bytes_only_for_equal_multisets(self):
        with tempfile.TemporaryDirectory() as directory:
            old, new = (Path(directory) / name for name in ('old.db', 'new.db'))
            row = ('email', 'a@example.test', 'D1', 0, 'email', 0, None, '')
            other = ('phone', '+12125550208', 'D2', 1, 'phone', 0, 'Jane Roe', 'line')
            for path, rows in ((old, [row, row, other]), (new, [other, row, row])):
                with sqlite3.connect(path) as db:
                    db.execute('CREATE TABLE mentions(kind,value,efta,page,label,header,name,name_how)')
                    db.executemany('INSERT INTO mentions VALUES (?,?,?,?,?,?,?,?)', rows)
            self.assertTrue(mentions_identical(new, old))
            with sqlite3.connect(new) as db:
                db.execute('DELETE FROM mentions WHERE rowid=2')
                db.execute('INSERT INTO mentions VALUES (?,?,?,?,?,?,?,?)', other)
            self.assertFalse(mentions_identical(new, old))  # same count and set, different multiplicities
            with sqlite3.connect(new) as db:
                db.execute('DELETE FROM mentions')
                db.executemany('INSERT INTO mentions VALUES (?,?,?,?,?,?,?,?)', [row, row, other])
                db.execute("UPDATE mentions SET name='' WHERE name IS NULL")
            self.assertFalse(mentions_identical(new, old))
            self.assertFalse(mentions_identical(new, Path(directory) / 'missing'))

    def test_phones(self):
        # Positive NANP fixtures use 555-02xx: extraction rejects fictional 555-01xx.
        found = {(v, l) for k, v, l, h, *_ in extract('Tel: 212 555-0208 Fax: (212) 555.0209\nCell 917-555-0286') if k == 'phone'}
        self.assertEqual(found, {('+12125550208', 'tel'), ('+12125550209', 'fax'), ('+19175550286', 'cell')})
        self.assertEqual({v for k, v, *_ in extract('+44 20 5550 0147 and +33 (0)1 55 50 01 48')},
                         {'+442055500147', '+33155500148'})

    def test_not_phones(self):
        text = 'EFTA00012345 account 2125550208 ref 212-555-0208-7 date 2010-555-1234 555-0123 212-555-0142'
        self.assertEqual([v for k, v, *_ in extract(text) if k == 'phone'], [])

    def test_emails_and_header_lines(self):
        text = 'From: Jane Roe <jane.roe@Roe-Law.org>\nPlease write to Mail@Firm.COM or mailto:x.y@z.com\nimage001.png@01D2A.B3C4'
        found = {(v, l, h) for k, v, l, h, *_ in extract(text) if k == 'email'}
        self.assertEqual(found, {('jane.roe@roe-law.org', 'email', True), ('Mail@firm.com', 'email', False),
                                 ('x.y@z.com', 'mailto', False)})


class NameTests(unittest.TestCase):
    def test_names_next_to_values(self):
        from extract_contacts import name_near
        names = lambda text: {(v, n, how) for k, v, l, h, n, how in extract(text)}
        self.assertIn(('jee@gmail.com', 'Jeffrey Epstein', 'line'), names('On Jun 25, 2013, at 7:59 AM, Jeffrey Epstein <jee@gmail.com> wrote:'))
        self.assertIn(('jee@gmail.com', 'Jeffrey Epstein', 'line'), names('From: Jeffrey Epstein [mailto:jee@gmail.com]'))
        self.assertIn(('+19175550286', 'Jane Roe', 'line'), names('Jane Roe, Cell: 917-555-0286'))
        self.assertIn(('+12125550281', 'Jane van Roe', 'block'), names('Jane van Roe\nManaging Director\nTel. 212 555-0281'))
        self.assertEqual({n for _, n, _ in names('Please call 212-555-0295 Thanks\nTel: 212 555 0208')}, {None})
        self.assertEqual(name_near('Sent: Tuesday, June 4 jee@gmail.com', 27, 'email'), (None, ''))


class ResolverTests(unittest.TestCase):
    def test_known_case_and_ocr_variants(self):
        lookup = {'email_known': {'kind': 'email', 'value': 'jane.roe@gmail.com'}, 'phone_known': {'kind': 'phone', 'value': '+12125550100'}}
        resolve = build_fulltext_contacts.resolver(lookup, b'k')
        self.assertEqual(resolve('email', 'jane.roe@gmail.com'), ('email_known', 'header_value'))
        self.assertEqual(resolve('email', 'Jane.Roe@gmail.com'), ('email_known', 'header_value_case'))
        member, rule = resolve('email', 'janeroe@grnail.com')
        self.assertTrue(member.startswith('email_') and member != 'email_known')
        self.assertEqual(rule, 'ocr_variant:email_known')
        self.assertEqual(resolve('phone', '+12125550100'), ('phone_known', 'header_value'))
        self.assertEqual(resolve('email', 'new@x.org')[1], 'fulltext')


class NameResolverTests(unittest.TestCase):
    def test_fuzzy_variants(self):
        found = build_fulltext_contacts.fuzzy_variants({
            ('email', 'jeevacation@gmail.com'): 1000, ('email', 'jecvocation@gmail.com'): 6,
            ('email', 'rhea.veldon@firm.test'): 100, ('email', 'irhea.veldon@firm.test'): 3,
            ('email', 'ann@x.com'): 100, ('email', 'anne@x.com'): 1,  # short local part: another person
            ('phone', '+18005550150'): 33, ('phone', '+16005550150'): 1,  # area code misread
            ('phone', '+12125550181'): 900, ('phone', '+12125550161'): 1,  # last four: another line
            ('phone', '+12125550152'): 30, ('phone', '+12125560152'): 1,  # exchange: needs 50x (555/556 preserves the one-digit misread)
            ('phone', '+13405550151'): 745, ('phone', '+3405550151'): 2})
        self.assertEqual(found, {('email', 'jecvocation@gmail.com'): ('jeevacation@gmail.com', 'ocr_edit'),
                                 ('email', 'irhea.veldon@firm.test'): ('rhea.veldon@firm.test', 'ocr_edit'),
                                 ('phone', '+16005550150'): ('+18005550150', 'ocr_digit'),
                                 ('phone', '+3405550151'): ('+13405550151', 'missing_country_code')})

    def test_kg_header_swapped_and_new(self):
        lookup = {'participant_l': {'kind': 'participant', 'value': 'Lesley Groff'}}
        entities = [{'id': 1, 'name': 'Jeffrey Epstein', 'entity_type': 'person', 'original_names': ['Jeffrey E. Epstein']}]
        resolve = build_fulltext_contacts.name_resolver(lookup, entities, b'k')
        self.assertEqual(resolve('Jeffrey Epstein'), ('kg:1', 'kg_person'))
        self.assertEqual(resolve('Epstein Jeffrey'), ('kg:1', 'kg_person'))
        self.assertEqual(resolve('LESLEY  GROFF'), ('participant_l', 'header_name'))
        new, rule = resolve('Jane Roe')
        self.assertEqual((new[:12], rule), ('participant_', 'fulltext_name'))
        self.assertEqual(resolve('jane roe')[0], new)


def contact_db(path):
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE nodes(id, kind, canonical, in_headers, service_line, documents, pages, body_pages, labels);
        CREATE TABLE mentions(id, efta, page, label, header);
        CREATE TABLE pairs(a, b, shared_documents, shared_pages, specificity, sample_efta);
    ''')
    db.executemany('INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)', [
        ('email_a', 'email', '1', 1, 0, 3, 3, 2, None), ('email_ocr', 'email', '1', 0, 0, 1, 1, 1, None),
        ('phone_b', 'phone', 'phone_b', 0, 0, 2, 2, 2, '{"cell": 2}'), ('phone_toll', 'phone', 'phone_toll', 0, 1, 9, 9, 9, None)])
    db.executemany('INSERT INTO mentions VALUES (?,?,?,?,?)', [
        ('email_a', 'E1', 0, 'email', 1), ('email_a', 'E2', 3, 'email', 0), ('email_ocr', 'E3', 1, 'email', 0),
        ('phone_b', 'E2', 3, 'cell', 0)])
    db.executemany('INSERT INTO pairs VALUES (?,?,?,?,?,?)', [('1', 'phone_b', 1, 1, 1.0, 'E2')])
    db.commit()
    return db


class LayerTests(unittest.TestCase):
    def test_reviewed_owner_covers_misreadings_and_merges(self):
        f = build_fulltext_contacts.value_canonical
        rules = {'phone_m': 'missing_country_code:phone_p', 'phone_o': 'ocr_digit:phone_q'}
        owners = {'phone_p': 'participant_x', 'phone_q': 'participant_y'}
        identity_map = {'participant_y': {'canonical': 'kg:7'}, 'phone_z': {'canonical': 'kg:3'}}
        self.assertEqual(f('phone_p', rules, owners, identity_map), 'participant_x')
        self.assertEqual(f('phone_m', rules, owners, identity_map), 'participant_x')  # misreading follows the owner
        self.assertEqual(f('phone_o', rules, owners, identity_map), '7')  # owner merged into a graph person
        self.assertEqual(f('phone_z', rules, owners, identity_map), '3')
        self.assertEqual(f('phone_n', rules, owners, identity_map), 'phone_n')

    def test_node_page_written_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = contact_db(Path(tmp) / 'c.db')
            # Node 1 owns email_a (identity_map canonical) and its OCR variant; header lines are left out.
            docs = written_documents(db, 1, ['kg:1', 'email_a'])
            self.assertEqual([(e, p) for e, p, _, _ in docs], [('E2', [3]), ('E3', [1])])
            self.assertEqual(written_documents(db, 'phone_b', ['phone_b'])[0][3], ['cell'])
            self.assertEqual(written_documents(None, 1, []), [])

    def test_full_export_has_every_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            contact_db(out / 'contact_graph.db').close()
            with sqlite3.connect(out / 'company_graph.db') as db:
                db.execute('CREATE TABLE entities(id, name, entity_type, document_count, metadata)')
                db.execute('CREATE TABLE pairs(a, b, shared_documents, specificity_score, lift, kept, sample_eftas)')
                db.execute("INSERT INTO entities VALUES ('company_x', 'X LLC', 'shell_company', 4, '{}')")
                db.execute("INSERT INTO pairs VALUES ('company_x', '1', 3, 1, 2, 0, '[]')")
            nodes, edges = {'kg:1': {'id': 1, 'name': 'Jane', 'entity_type': 'person'}}, []
            with patch.object(export_neo4j, 'OUT', out):
                aliases = export_neo4j.full_text_layers(nodes, edges)
        self.assertEqual(set(nodes), {'kg:1', 'company_x', 'phone_b', 'phone_toll'})
        self.assertEqual(nodes['phone_toll']['service_line'], 1)
        self.assertEqual({(a, b, k) for a, b, k, *_ in edges},
                         {('company_x', 'kg:1', 'co_mentioned_in_documents'), ('kg:1', 'phone_b', 'written_on_same_page')})
        self.assertEqual({(m, c) for m, c, *_ in aliases}, {('email_a', 'kg:1'), ('email_ocr', 'kg:1')})


if __name__ == '__main__':
    unittest.main()
