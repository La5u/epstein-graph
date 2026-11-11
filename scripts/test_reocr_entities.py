"""Corpus-free checks for choosing short company names and judging their re-read pages."""
import sqlite3
import unittest

from reocr_entities import (best_angle, candidates, check, choose_pages, company_index, mention_line, near,
                            near_common, related, suggestion, suspect_reason, verdict, window)


def node(identity, name, spellings=(), documents=0):
    return {'id': identity, 'name': name, 'metadata': {'spellings': list(spellings), 'document_count': documents}}


NODES = [node('e', 'E, Inc.'), node('jege', 'JEGE, Inc.', ['jebe inc']), node('jege_llc', 'JEGE, LLC'),
         node('mon', 'Mon, Inc.'), node('mondo', 'Mondo, Inc.')]
INDEX = company_index(NODES)
GOOD = {'words': 40, 'dictionary_words': 36, 'ratio': .9, 'score': 32.4}
BAD = {'words': 30, 'dictionary_words': 6, 'ratio': .2, 'score': 1.2}


class EntityReocrTest(unittest.TestCase):
    def test_suspects(self):
        self.assertEqual(suspect_reason('E, Inc.'), 'short')
        self.assertEqual(suspect_reason('UBS AG'), 'short')
        self.assertEqual(suspect_reason('E Management New York LLC'), 'bare_letter')
        for name in ('J. Epstein & Company, Inc.', 'K & F Capital Corp.', "D'Agostino & Co. Ltd.", 'M-Related LLC',
                     'JEGE, Inc.', 'Informa plc'):
            self.assertIsNone(suspect_reason(name), name)

    def test_related_keeps_legal_form(self):
        self.assertTrue(related('e inc', 'jege inc'))
        self.assertTrue(related('mon inc', 'mondo inc'))
        self.assertFalse(related('e inc', 'jege llc'))  # Inc and LLC are different companies
        self.assertFalse(related('jege inc', 'e inc'))
        self.assertFalse(related('e inc', 'e inc'))

    def test_turned_page_reads_longer_name(self):
        corpus_line = 'J E. Inc.     Cypress, Inc.'
        self.assertEqual(mention_line('Case list\n\n' + corpus_line + '\nmore', {'e inc'}), ['Case list', corpus_line, 'more'])
        reading = {'text_layer': 'Case list\nJ E. Inc. Cypress, Inc.\n',
                   'tesseract': {'0': 'l1I ~~ :: ,,\n', '90': 'Case list\nJEGE, Inc.   Cypress, Inc.\n',
                                 '270': '', '180': ''},
                   'scores': {'0': BAD, '90': GOOD, '270': BAD, '180': BAD}}
        result = check(reading, {'e inc'}, ['', corpus_line, ''], 'Case list\n' + corpus_line, INDEX, 'e')
        self.assertEqual(result['best_angle'], 90)
        self.assertEqual(result['verdict'], 'longer_name')
        self.assertEqual(result['longer_ids'], ['jege'])
        self.assertEqual(suggestion([result]), 'jege')

    def test_same_and_unclear(self):
        doj = 'Paid to E, Inc. on 3/4'
        self.assertEqual(verdict({'e inc'}, 'Paid to E, Inc. on 3/4', doj, INDEX, 'e')[0], 'same')
        self.assertEqual(verdict({'e inc'}, 'XXXXXXXX', doj, INDEX, 'e')[0], 'unclear')
        # A longer name DOJ's text already has is another company on the page.
        self.assertEqual(verdict({'e inc'}, 'JEGE, Inc.\nXXXX', 'JEGE, Inc.\n' + doj, INDEX, 'e')[0], 'unclear')
        # The short name read as often as before: both companies are on the page.
        self.assertEqual(verdict({'e inc'}, 'JEGE, Inc. and E, Inc.', doj, INDEX, 'e')[0], 'same')
        # The legal form is often garbled: the bare name picks the node with the same form.
        # Run together, the name is still read.
        self.assertEqual(verdict({'sfl llc'}, 'SFLLLC Loan', 'SFL LLC Loan', INDEX, 'sfl')[0], 'same')
        self.assertEqual(verdict({'e inc'}, 'PPG INDUSTRIES JEGE NG', 'PPG INOUSTRIES ØE INC', INDEX, None),
                         ('longer_name', {'jege inc': 'jege'}))
        # A page showing the short name blocks the suggestion, even if another reads longer.
        longer = {'verdict': 'longer_name', 'longer_ids': ['jege']}
        self.assertIsNone(suggestion([longer, {'verdict': 'same', 'longer_ids': []}]))
        self.assertEqual(suggestion([longer, longer, {'verdict': 'same', 'longer_ids': []}]), 'jege')
        self.assertIsNone(suggestion([{'verdict': 'unclear', 'longer_ids': []}]))
        self.assertEqual(candidates({'e inc'}, ['JEBE Inc and JEGE LLC'], INDEX, 'e'), {'jebe inc': 'jege'})
        self.assertEqual(candidates({'e inc'}, ['JEGE, Inc.'], INDEX, 'jege'), {})  # never the node itself

    def test_whole_word_misread_on_the_mention_line(self):
        doj = 'subpoenas to MOE, Inc. and Mondo, Inc. have been'
        reading = 'subpoenas to JEGE, Inc, and Mondo, Inc. have been'
        self.assertEqual(verdict({'moe inc'}, reading, doj, INDEX, 'moe', reading, doj), ('misread', {'jege inc': 'jege'}))
        # The right name elsewhere on DOJ's page (an email quoting it) doesn't hide the misread.
        self.assertEqual(verdict({'moe inc'}, reading, 'JEGE, Inc.\n' + doj, INDEX, 'moe', reading, doj)[0], 'misread')
        # Not on the mention line, or with another legal form: unclear.
        self.assertEqual(verdict({'moe inc'}, reading, doj, INDEX, 'moe', 'have been')[0], 'unclear')
        self.assertEqual(verdict({'moe inc'}, 'Bill To: JEGE LLC', 'Bill To: MOE INC', INDEX, 'moe', 'Bill To: JEGE LLC'),
                         ('unclear', {}))
        self.assertEqual(suggestion([{'verdict': 'misread', 'longer_ids': ['jege']}]), 'jege')
        self.assertIsNone(suggestion([{'verdict': 'misread', 'longer_ids': ['jege']},
                                      {'verdict': 'misread', 'longer_ids': ['mondo']}]))  # a tie

    def test_near_common_names(self):
        self.assertTrue(near('six designs llc', 'slk designs llc'))
        self.assertTrue(near('apo 02 trust', 'apo2 trust'))
        self.assertFalse(near('six designs llc', 'slk designs inc'))  # another legal form
        self.assertFalse(near('mgr llc', 'jege llc'))  # too short to judge by spelling
        self.assertFalse(near('six designs llc', 'six designs llc'))
        nodes = [node('six', 'SIX Designs, LLC', documents=4), node('slk', 'SLK Designs LLC', documents=370),
                 node('sik', 'SIK Designs LLC', documents=15), node('inc', 'SLK Designs, Inc.', documents=900),
                 node('apo', 'APO-02 Trust', documents=3), node('apo2', 'APO2 Trust', documents=10)]
        # The most common near name; not one only 4x as common, nor one too rare to trust.
        self.assertEqual(near_common(nodes), {'six': 'slk', 'sik': 'slk'})

    def test_near_only_counts_near_names(self):
        index = company_index([node('six', 'SIX Designs, LLC'), node('slk', 'SLK Designs LLC'),
                               node('jege', 'JEGE, LLC')])
        doj = 'Invoice from SIX Designs, LLC and JEGE, LLC'
        reading = 'Invoice from SLK Designs, LLC and JEGE, LLC'
        self.assertEqual(verdict({'six designs llc'}, reading, doj, index, 'six', reading, doj, near_only=True),
                         ('misread', {'slk designs llc': 'slk'}))
        # Another company with the same form on the line is not a misread of a near name.
        self.assertEqual(verdict({'six designs llc'}, 'Invoice from JEGE, LLC', 'Invoice from SIX Designs, LLC', index,
                                 'six', 'Invoice from JEGE, LLC', 'Invoice from SIX Designs, LLC', near_only=True),
                         ('unclear', {}))
        self.assertEqual(verdict({'six designs llc'}, doj, doj, index, 'six', doj, doj, near_only=True)[0], 'same')

    def test_upright_kept_unless_turned_is_clearly_better(self):
        self.assertEqual(best_angle({0: GOOD, 90: GOOD, 270: BAD, 180: BAD}), 0)
        self.assertEqual(best_angle({0: BAD, 90: BAD, 270: BAD, 180: GOOD}), 180)

    def test_window_around_closest_line(self):
        lines = '\n'.join(f'line {i}' for i in range(10)) + '\nJEGE, Inc.\n' + 'tail\n' * 5
        self.assertEqual(window(lines, ['', 'J E. Inc.', ''])[2], 'JEGE, Inc.')
        self.assertEqual(len(window(lines, ['', 'J E. Inc.', ''])), 5)
        # One-letter lines of a turned page don't win; the match stays at [WINDOW] at the top.
        self.assertEqual(window('N\nJEGE, Inc. Case No. 1:19\no', ['', 'J E. Inc. Case No. 1:19', '']),
                         ['', 'N', 'JEGE, Inc. Case No. 1:19', 'o'])
        self.assertEqual(window('', ['', 'x', '']), [])
        # A short mention is placed by its neighbours, not by the first line ending in ", Inc.".
        page = 'among Foundation Medicine, Inc. (the Company)\nother text\nAccounts of\nJEGE, Inc.\nPalm Beach'
        self.assertEqual(window(page, ['Accounts of', 'E, INC.', 'Palm Beach'])[2], 'JEGE, Inc.')

    def test_choose_pages_one_per_document_cached_first(self):
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE mentions(key TEXT, spelling TEXT, page_id INTEGER, efta TEXT)')
        db.executemany('INSERT INTO mentions VALUES (?,?,?,?)', [
            ('e inc', 'E Inc', 9, 'EFTA2'), ('e inc', 'E. Inc.', 5, 'EFTA2'), ('e inc', 'E INC', 7, 'EFTA3'),
            ('e inc', 'E INC', 1, 'EFTA1'), ('e inc', 'E INC', 200_000_000, 'EFTA4'), ('x inc', 'X', 2, 'EFTA1')])
        pages = choose_pages(db, {'e inc'}, 2, {'EFTA3'})
        self.assertEqual([p[:2] for p in pages], [('EFTA3', 7), ('EFTA1', 1)])
        self.assertEqual([p[:2] for p in choose_pages(db, {'e inc'}, 5, set())],
                         [('EFTA1', 1), ('EFTA2', 5), ('EFTA3', 7)])  # supplement pages have no PDF page


if __name__ == '__main__':
    unittest.main()
