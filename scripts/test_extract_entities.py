import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import sqlite3
import tempfile
import subprocess
import sys

import extract_entities as extractor
from build_communications import Builder
from normalize_graph import name_key as person_key

from extract_entities import clean_span, domain_key, extract, gazetteer_patterns
from extract_companies import name_key


class ExtractionTests(unittest.TestCase):
    def test_impossible_spans_are_never_mentions(self):
        for value in ['LLC', 'Inc.', 'Corp', 'Ltd', 'L.L.C.', '••', '•-0', 'r~', '~4', '• f••', '1234']:
            with self.subTest(value=value):
                self.assertIsNone(clean_span(value))
                ent = SimpleNamespace(start_char=0, end_char=len(value), text=value, label_='ORG')
                self.assertEqual(list(extract(value, SimpleNamespace(ents=[ent]), set(), [])), [])
        for value in ['A', 'Li', 'Example LLC', 'Bl00d Qxarble', 'Q@uel', 'Жан Дюпон', 'J•••hn', 'R~']:
            self.assertEqual(clean_span(value), value)

    def test_dropped_spans_never_reach_public_or_private_counts(self):
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE pages(page_id,doc_id,text)')
        db.execute("INSERT INTO pages VALUES (1,'doc','LLC • f••')")
        ents = [SimpleNamespace(start_char=0, end_char=3, text='LLC', label_='PERSON'),
                SimpleNamespace(start_char=4, end_char=9, text='• f••', label_='ORG')]
        with patch.multiple(extractor, _corpus=db, _nlp=lambda text: SimpleNamespace(ents=ents),
                            _dictionary=set(), _patterns=[], _builder=Builder(b'fixture', [])):
            self.assertEqual(extractor.process([1]), ([1], [], []))
        db.close()

    def test_sample_files_and_page_selection(self):
        from adapters.efta import create
        import corpus
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ids = root / 'docs.txt'
            ids.write_text('b\na\nb\n')
            self.assertEqual(extractor.sample_documents(ids), ['a', 'b'])
            ids.write_text('["b", "a"]')
            self.assertEqual(extractor.sample_documents(ids), ['a', 'b'])
            source_path = root / 'corpus.db'
            db = sqlite3.connect(source_path)
            create(db, [('a', 1, 'Hello'), ('b', 1, 'World'), ('c', 1, 'Outside'), ('a', 2, 'Again')])
            db.close()
            progress = root / 'sample.db'
            db = sqlite3.connect(progress)
            db.execute('CREATE TABLE done(page_id PRIMARY KEY)')
            db.execute('INSERT INTO done VALUES (1)'); db.commit(); db.close()
            db = corpus.connect(source_path)
            self.assertEqual(list(extractor.pending_pages(db, progress, ['a', 'b'], None)), [(2, 5), (4, 5)])
            db.close()
            db = corpus.connect(source_path)
            with self.assertRaisesRegex(ValueError, 'no corpus pages'):
                extractor.pending_pages(db, progress, ['missing'], None)
            db.close()

    def test_sample_mode_protects_real_databases(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            docs = root / 'docs.txt'; docs.write_text('sample\n')
            base = [sys.executable, str(Path(extractor.__file__)), '--sample-docs', str(docs)]
            result = subprocess.run(base, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('resumable corpus DB is protected', result.stderr)
            result = subprocess.run(base + ['--output', str(root / 'sample.db'), '--private-output', str(extractor.PRIVATE)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('separate private spelling DB', result.stderr)
            self.assertFalse((root / 'sample.db').exists())

    def test_clean(self):
        self.assertEqual(clean_span('From: the Zachary Wrenford;\nignore'), 'Zachary Wrenford')
        self.assertEqual(clean_span('To: Jorge Amador <jorge@example.com>'), 'Jorge Amador')
        self.assertIsNone(clean_span('1234'))

    def test_sites_and_abbreviations(self):
        config = {'Bank of America': {'subtype':'bank','aliases':['BofA']}, 'Internal Revenue Service': {'subtype':'government','aliases':['IRS']}}
        text = 'BofA bofa IRS irs https://www.nytimes.com/news user@wsj.com nytimes com news.bbc.co.uk'
        rows = list(extract(text,SimpleNamespace(ents=[]),set(),gazetteer_patterns(config)))
        self.assertEqual(sum(r[0]=='Bank of America' for r in rows),2)
        self.assertEqual(sum(r[0]=='Internal Revenue Service' for r in rows),1)
        self.assertEqual({r[0] for r in rows if r[1]=='website'}, {'nytimes.com','bbc.co.uk'})
        self.assertEqual(domain_key('https://www.foo.co.uk/a'), 'foo.co.uk')

    def test_weak_tokens_outside_spans(self):
        text = 'Zachary Wrenford Quux Ordinary'
        ent=SimpleNamespace(start_char=0,end_char=16,text='Zachary Wrenford',label_='PERSON')
        rows=list(extract(text,SimpleNamespace(ents=[ent]),{'ordinary'},[]))
        self.assertEqual([(r[0],r[1]) for r in rows], [('Zachary Wrenford','PERSON'),('Quux','unknown_word')])

    def test_ocr_ampersand(self):
        self.assertEqual(name_key('JPMorgan Chase 8','Co.'),name_key('JPMorgan Chase &','Co.'))

    def test_public_mentions_use_header_namespace(self):
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE pages(page_id,doc_id,text)')
        db.execute("INSERT INTO pages VALUES (1,'doc','Zachary Wrenford')")
        ent = SimpleNamespace(start_char=0,end_char=16,text='Zachary Wrenford',label_='PERSON')
        builder = Builder(b'fixture', [])
        with patch.multiple(extractor, _corpus=db, _nlp=lambda text: SimpleNamespace(ents=[ent]),
                            _dictionary=set(), _patterns=[], _builder=builder):
            ids, public, private = extractor.process([1])
        identity = builder.participant_id(person_key('Zachary Wrenford'))
        self.assertEqual(public[0][0], identity)
        self.assertNotIn('Zachary', str(public))
        self.assertEqual(private[0][:2], (identity, 'Zachary Wrenford'))
        db.close()

    def test_ocr_emails_are_never_sites_or_organizations(self):
        text = 'jeevacation®gmail.com jeevacationegmail.com jeevacationggmail.com JPM@gmail.com real@example.org'
        ent = SimpleNamespace(start_char=0,end_char=20,text=text[:20],label_='ORG')
        rows = list(extract(text,SimpleNamespace(ents=[ent]),set(),gazetteer_patterns({'JPMorgan Chase':{'subtype':'bank','aliases':['JPM']}})))
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row[1] == 'email' for row in rows))
        self.assertEqual(sum(row[0]=='jeevacation@gmail.com' for row in rows), 3)

    def test_ner_people_before_public_storage(self):
        for value, label, expected in [('Brandon Mellin','FAC','PERSON'),('Brenda Norell','GPE','PERSON'),('Nancy Jones','ORG','PERSON'),('NANCY','ORG','person_weak')]:
            ent = SimpleNamespace(start_char=0,end_char=len(value),text=value,label_=label)
            rows = list(extract(value,SimpleNamespace(ents=[ent]),set(),[]))
            self.assertEqual(rows[0][1], expected)

    def test_full_page_anchor_recovers_name_missed_by_ner(self):
        from entity_resolution import ExistingIndex
        name = 'Deutsche Bank Trust Company Americas'
        tail = 'Bank Trust Company Americas'
        text = name + '\n' + tail
        ent = SimpleNamespace(start_char=len(name)+1,end_char=len(text),text=tail,label_='ORG')
        resolver = ExistingIndex({1:{'id':1,'name':name,'entity_type':'organization'}}, {})
        rows = list(extract(text,SimpleNamespace(ents=[ent]),set(),[],resolver))
        self.assertIn((name,'ORG',name,'',0),rows)
        self.assertIn((tail,'ORG',tail,'',0),rows)
        rows = list(extract(tail,SimpleNamespace(ents=[SimpleNamespace(start_char=0,end_char=len(tail),text=tail,label_='ORG')]),set(),[],resolver))
        self.assertNotIn((name,'ORG',name,'',0),rows)

    def test_explicit_markup_keeps_tag_markers_for_hiding(self):
        from entity_labels import label_reason
        for value in ['<meta>', '<b>', '</custom-widget>', '<span class="x">', '<img src="x"/>']:
            cleaned = clean_span(value)
            self.assertEqual(cleaned,value)
            self.assertEqual(label_reason(cleaned,set()),'markup')
