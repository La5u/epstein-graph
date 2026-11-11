"""Rules tested with invented spellings; no corpus, network, or model loads."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ocr_consensus import Consensus
from reocr_nodes import (choose_nodes, consensus_map, crop_box, Downloader, matched_line,
                         page_cache_path, private_json, reading_span, source_pages,
                         suspicious, summary, tsv_rows, vote)


def node(identity, name, kind='participant', sub=None):
    return {'id': identity, 'label': name, 'real': None, 'type': kind, 'sub': sub}


def readings(**values):
    return {k: {'spelling': v} for k, v in values.items()}


def check(raw='Jane Rne', target='Jane Roe', engines=('tesseract', 'rapidocr'), page=1):
    return {'raw': raw, 'consensus': target, 'engines': list(engines), 'doc_id': 'EFTA00000001',
            'page_id': page, 'page': page - 1, 'agreement': len(engines) / 3, 'verdict': 'corrected'}


class SelectionTests(unittest.TestCase):
    def test_each_suspicion_rule(self):
        nodes = [node('a', 'Jane Rne'), node('b', 'Jane Roe'), node('c', 'Roe Jane'),
                 node('d', 'One Two Three Four Five Six'), node('e', 'jane®example.com'),
                 node('f', 'Qrstvwxz Mmmmmm'), node('address', '123 Long Street West Palm Beach Road', 'place', 'address'),
                 node('car', 'One Two Three Four Five Six', 'vehicle')]
        found = {n['id']: n['reasons'] for n in suspicious(nodes)}
        self.assertIn('near_ocr_edit', found['a'])
        self.assertIn('word_order_or_punctuation', found['c'])
        self.assertIn('long_non_address', found['d'])
        self.assertIn('garbled_contact', found['e'])
        self.assertIn('ocr_looking', found['f'])
        self.assertNotIn('address', found)
        self.assertNotIn('car', found)

    def test_decrypted_label_and_kind_limit(self):
        nodes = [dict(node('p', 'HeaderLabel_x'), real='Jane Rne'), node('e', 'jane®example.com', 'contact', 'email')]
        args = argparse.Namespace(kinds=['email'], names=None, seed=7, pilot=False, limit=1)
        self.assertEqual([n['id'] for n in choose_nodes(nodes, args)], ['e'])
        args.limit = 0
        self.assertEqual(choose_nodes(nodes, args), [])

    def test_explicit_curated_list_includes_clean_controls(self):
        args = argparse.Namespace(kinds=None, names=None, seed=7, pilot=False, limit=2, all_nodes=True)
        self.assertEqual({n['id'] for n in choose_nodes([node('a', 'Ordinary'), node('b', 'Jane Roe')], args)}, {'a', 'b'})

    def test_two_edits_are_candidates_three_or_more_are_not(self):
        found = {n['id']: n['reasons'] for n in suspicious([
            node('a', 'Marina Ellerton'), node('b', 'Marina Elwexton'), node('c', 'Marina Qzxvxton')])}
        self.assertIn('near_ocr_edit', found['b'])
        self.assertNotIn('near_ocr_edit', found.get('c', []))

    def test_provider_damage_does_not_need_a_nearby_snapshot_node(self):
        found = suspicious([node('email_a', 'invented@cunail.com', 'contact', 'email')])
        self.assertEqual(found[0]['reasons'], ['garbled_contact'])


class LineTests(unittest.TestCase):
    def test_fuzzy_line_and_wrapped_name(self):
        self.assertEqual(matched_line('Other text\nFrom: Jane Rne\nMore', 'Jane Rne')['line'], 'From: Jane Rne')
        self.assertIn('Jane Roe', matched_line('From: Jane\nRoe <jane@example.com>', 'Jane Roe')['line'])
        self.assertIsNone(matched_line('Completely unrelated content', 'Qrstuvwxyz'))

    def test_span_recovers_missing_letters_and_order(self):
        self.assertEqual(reading_span('Jane Rne', 'From: Jane Rne <jane@example.com>',
                                      'From: Jane Roe <jane@example.com>'), 'Jane Roe')
        self.assertEqual(reading_span('Jane Rohananof', 'To: Jane Rohananof', 'To: Jane Rohananoff'), 'Jane Rohananoff')
        self.assertEqual(reading_span('Roe Jane', 'To: Roe Jane', 'To: Roe Jane'), 'Roe Jane')
        self.assertIsNone(reading_span('Jane Rne', 'From: Jane Rne <jane@example.com>', 'Entirely unrelated subject'))

    def test_span_does_not_absorb_other_party(self):
        self.assertEqual(reading_span('Jane Rne', 'To: Jane Rne; Alan Doe', 'To: Jane Roe; Alan Doe'), 'Jane Roe')
        self.assertEqual(reading_span('jane®example.com', 'To: jane®example.com', 'To: jane@example.com'), 'jane@example.com')

    def test_glued_context_and_spaced_email_punctuation(self):
        self.assertEqual(reading_span('jane@exarnple.com', 'return e-mail to jane@exarnple.com and',
                                      'returne-mailtojane@example.com,and'), 'jane@example.com')
        self.assertEqual(reading_span('jane@exarnple.com', 'return e-mail to jane@exarnple.com and',
                                      'return e-mail to jane@example .com, and'), 'jane@example.com')

    def test_canonical_label_is_not_evidence_of_ocr_error(self):
        # A name with "Inc." must not be projected onto another clause ending
        # in a city merely because fuzzy line similarity is high.
        raw = 'Insured Aircraft Title Service, Inc.'
        line = 'to Insured Aircraft Title Service, Oklahoma'
        self.assertIsNone(reading_span(raw, line, line))
        result = vote('Acme, Inc.', readings(doj='Acme Inc', tesseract='Acme Inc', rapidocr='Acme Inc'), 'Acme Inc')
        self.assertEqual(result['verdict'], 'same')
        self.assertFalse(consensus_map([{'checks': [check(raw='Acme, Inc.', target='Acme Inc') | result]}])['variants'])

    def test_unicode_casefold_span_offsets(self):
        self.assertEqual(reading_span('Straße', 'To: Straße', 'To: STRASSE'), 'STRASSE')
        self.assertEqual(reading_span('Straße', 'To: Straße', 'To: Straße'), 'Straße')
        self.assertEqual(reading_span('Rne', 'To: Straße Rne', 'To: Straße Roe'), 'Roe')

    def test_tsv_and_crop_have_matching_row_and_bounds(self):
        tsv = 'level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        tsv += '5\t1\t1\t1\t1\t10\t0\t30\t12\t90\tJane\n5\t1\t1\t1\t1\t45\t0\t20\t12\t90\tRoe\n'
        rows = tsv_rows(tsv)
        self.assertEqual(rows[0]['line'], 'Jane Roe')
        self.assertEqual(crop_box(rows, 'Jane Rne', 500, 100), [0, 0, 500, 17])
        self.assertIsNone(crop_box(rows, 'zzzzzzzzzzzzzzzzzzzzzzz', 500, 100))


class VotingTests(unittest.TestCase):
    def test_two_independent_engines_no_fuzzy_votes(self):
        result = vote('Jane Rne', readings(doj='Jane Rne', tesseract='Jane Roe', rapidocr='Jane Roe'))
        self.assertEqual(result['verdict'], 'corrected')
        self.assertEqual(result['engines'], ['rapidocr', 'tesseract'])
        self.assertAlmostEqual(result['agreement'], .6667)
        self.assertEqual(vote('Jane Rne', readings(doj='Jane Rne', tesseract='Jane Roe', rapidocr='Jane Row'))['verdict'], 'disagree')

    def test_luna_cannot_create_a_second_vote(self):
        self.assertEqual(vote('Jane Rne', readings(tesseract='Jane Roe', luna='Jane Roe'))['verdict'], 'disagree')
        # A 2:2 split with the optional fourth engine is withheld.
        self.assertEqual(vote('Jane Rne', readings(doj='Jane Rne', tesseract='Jane Rne', rapidocr='Jane Roe', onnxtr='Jane Roe'))['verdict'], 'disagree')

    def test_case_is_not_a_correction(self):
        self.assertEqual(vote('Jane Roe', readings(doj='Jane Roe', tesseract='JANE ROE', rapidocr='Jane Roe'))['verdict'], 'same')

    def test_global_alias_needs_consistent_pages_and_per_page_votes(self):
        reports = [{'checks': [check(), check(page=2)]}]
        self.assertEqual(len(consensus_map(reports)['variants']['Jane Rne']['pages']), 2)
        reports[0]['checks'].append(check(target='Jane Rne', page=3))
        self.assertFalse(consensus_map(reports)['variants'])
        self.assertEqual(consensus_map(reports)['conflicting_spellings_withheld'], 1)
        self.assertFalse(consensus_map([{'checks': [check(engines=['tesseract']), check(engines=['rapidocr'], page=2)]}])['variants'])

    def test_summary_counts_pages_once(self):
        report = [{'checks': [check()]}, {'checks': [check()]}]
        result = summary(report)
        self.assertEqual(result['node_page_checks'], 2)
        self.assertEqual(result['unique_pages'], 1)
        self.assertEqual(result['variants_resolved'], 1)


class PersistenceTests(unittest.TestCase):
    def test_private_atomic_output_and_validated_build_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'new' / 'aliases.json'
            self.assertEqual(Consensus(path).correct('Jane Rne'), 'Jane Rne')
            private_json(path, consensus_map([{'checks': [check()]}]))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            reader = Consensus(path)
            self.assertEqual(reader.correct('  JANE  RNE '), 'Jane Roe')
            self.assertEqual(reader.correct('Jane R.ne'), 'Jane R.ne')
            self.assertEqual(reader.correct(None), None)
            self.assertTrue(reader.evidence('Jane Rne'))
            self.assertFalse(reader.evidence('Jane Roe'))

    def test_build_reader_rejects_bad_votes_chains_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'aliases.json'
            data = consensus_map([{'checks': [check(), check(raw='Jane Roe', target='Jane Row')]}])
            data['variants']['Broken'] = {'consensus': 'Valid', 'pages': [{'engines': ['tesseract', 'luna'], 'page_id': 1}]}
            private_json(path, data)
            reader = Consensus(path)
            self.assertEqual(reader.correct('Jane Rne'), 'Jane Rne')
            self.assertEqual(reader.correct('Jane Roe'), 'Jane Row')
            self.assertEqual(reader.correct('Broken'), 'Broken')
            data['variants']['Jane Row'] = data['variants']['Jane Rne'] | {'consensus': 'Jane Rne'}
            private_json(path, data)
            self.assertFalse(Consensus(path).aliases)

    def test_resolution_pairs_require_one_existing_literal_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'aliases.json'
            private_json(path, consensus_map([{'checks': [check()]}]))
            reader = Consensus(path)
            labels = {'a': 'Jane Rne', 'b': 'Jane Roe'}
            self.assertEqual(list(reader.pairs(labels)), [('a', 'b')])
            self.assertEqual(labels['a'], 'Jane Rne')  # preserve raw identity
            self.assertEqual(list(reader.pairs({'a': 'Jane Rne'})), [])
            self.assertEqual(list(reader.pairs(labels | {'c': 'JANE ROE'})), [])

    def test_cache_invalidates_pdf_and_dpi(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / 'test.pdf'
            pdf.write_bytes(b'%PDF test')
            first = page_cache_path('EFTA00000001', 0, 240, pdf)
            self.assertEqual(first, page_cache_path('EFTA00000001', 0, 240, pdf))
            self.assertNotEqual(first, page_cache_path('EFTA00000001', 0, 300, pdf))
            pdf.write_bytes(b'%PDF changed')
            self.assertNotEqual(first, page_cache_path('EFTA00000001', 0, 240, pdf))

    def test_cached_downloader_never_uses_network_or_reads_whole_pdf(self):
        with tempfile.TemporaryDirectory() as tmp, patch('reocr_nodes.WORK', Path(tmp)), patch('subprocess.run') as run:
            pdf = Path(tmp) / 'EFTA00000001.pdf'
            pdf.write_bytes(b'%PDF test')
            self.assertEqual(Downloader().get('EFTA00000001', None, True), pdf)
            self.assertIsNone(Downloader().get('EFTA00000002', None, True))
            self.assertIsNone(Downloader().get('../bad', 'https://www.justice.gov/bad'))
            run.assert_not_called()

    def test_source_page_provenance_from_all_three_tables(self):
        from adapters.efta import create
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for directory in ('data/raw', 'data/external/reocr', 'output/.build'):
                (root / directory).mkdir(parents=True)
            db = sqlite3.connect(root / 'data/raw/full_text_corpus.db')
            create(db, [('EFTA00000001', 0, 'From: Jane Rne'), ('EFTA00000002', 0, 'Acme L1mited LLC'),
                        ('EFTA00000003', 0, 'Taxablc Bonds'), ('EFTA00000010', 1, 'To: Alan Rne')],
                   [('EFTA00000001', 1), ('EFTA00000002', 1), ('EFTA00000003', 1), ('EFTA00000010', 3)])
            db.close()
            for doc in range(1, 4):
                (root / f'data/external/reocr/EFTA{doc:08}.pdf').write_bytes(b'%PDF')
            (root / 'data/external/reocr/EFTA00000010.pdf').write_bytes(b'%PDF')
            db = sqlite3.connect(root / 'output/communication_graph.db')
            db.executescript('CREATE TABLE evidence(source_id,target_id,efta_number); CREATE TABLE identity_map(member_id,canonical_id);')
            db.execute('INSERT INTO evidence VALUES (?,?,?)', ('participant_a', 'other', 'EFTA00000001'))
            db.execute('INSERT INTO evidence VALUES (?,?,?)', ('participant_d', 'other', 'EFTA00000011'))
            db.commit(); db.close()
            db = sqlite3.connect(root / 'output/.build/company_mentions.db')
            db.executescript('CREATE TABLE mentions(key,spelling,page_id,efta);')
            db.execute('INSERT INTO mentions VALUES (?,?,?,?)', ('acme l1mited llc', 'Acme L1mited LLC', 2, 'EFTA00000002'))
            db.commit(); db.close()
            db = sqlite3.connect(root / 'output/.build/entity_mentions.db')
            db.executescript("CREATE TABLE mentions(key,page_id,doc_id); INSERT INTO mentions VALUES ('taxablc bonds',3,'EFTA00000003');")
            db.commit(); db.close()
            db = sqlite3.connect(root / 'output/entity_graph.db')
            db.executescript("CREATE TABLE key_nodes(key,entity_id); INSERT INTO key_nodes VALUES ('taxablc bonds','entity_c');")
            db.commit(); db.close()
            plans = source_pages([node('participant_a', 'Jane Rne'), node('company_b', 'Acme L1mited LLC', 'organization'),
                                  node('entity_c', 'Taxablc Bonds', 'organization'), node('participant_d', 'Alan Rne')], root, 1, True)
            self.assertEqual([plans[i][0]['page_id'] for i in ('participant_a', 'company_b', 'entity_c')], [1, 2, 3])
            self.assertEqual(plans['participant_a'][0]['source'], 'communication_records')
            self.assertEqual(plans['company_b'][0]['source'], 'company_mentions')
            self.assertEqual(plans['entity_c'][0]['source'], 'entity_mentions')
            self.assertEqual(plans['participant_d'][0]['doc_id'], 'EFTA00000010')
            self.assertEqual(plans['participant_d'][0]['page'], 1)
            self.assertEqual(plans['participant_d'][0]['page_id'], 4)




class PlausibleReadingTests(unittest.TestCase):
    def test_rejects_neighbouring_partial_or_renumbered_spans(self):
        from ocr_consensus import plausible_reading
        for raw, target in [('Eton Musk', 'Elon Musk'), ('jecvacation(agmail.com', 'jeevacation@gmail.com'),
                            ('waww 6 llc', 'WAWW6LLC'), ('Daiian Keldry', 'Darian Keldry')]:
            self.assertTrue(plausible_reading(raw, target), raw)
        for raw, target in [('appaloosa management lp', 'Appaloosa'), ('nosa inc.', 'C'), ('email.com', '@gmail.com'),
                            ('shipping@wexlynmovers.test', 'rawexlyn'), ('waww 15 llc', 'WAWW17LLC'),
                            ('millennium usa lp', 'Millennium Funds'), ('innail.com', 'n@gmail.com')]:
            self.assertFalse(plausible_reading(raw, target), raw)


if __name__ == '__main__':
    unittest.main()
