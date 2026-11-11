import sqlite3
import unittest
from collections import Counter, defaultdict

import build_leads


def evidence_db(rows):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE evidence(efta_number, message_index, canonical_source, canonical_target, relationship_type, date_sent)')
    db.executemany('INSERT INTO evidence VALUES (?,?,?,?,?,?)', rows)
    return db


class EmailSignals(unittest.TestCase):
    def run_signals(self, rows):
        out, samples, docs = defaultdict(Counter), defaultdict(lambda: defaultdict(list)), defaultdict(set)
        build_leads.email_signals(evidence_db(rows), out, samples, docs)
        return out

    def test_direct_broadcast_and_dates(self):
        rows = [('E1', 0, 'a', 'kg:1', 'sent_to', '2010-01-02'),                       # a writes to Epstein, after plea
                ('E2', 0, 'kg:1', 'a', 'sent_to', '2005-01-02'),                       # Epstein to a, before
                *[('E3', 0, 'kg:1', p, 'cc_to', '2011-01-01') for p in 'abcde']]      # mass mail
        out = self.run_signals(rows)
        self.assertEqual((out['a']['direct_messages'], out['a']['direct_after_2008'], out['a']['wrote_to_epstein']), (2, 1, 1))
        self.assertEqual(out['a']['broadcast_messages'], 1)
        self.assertEqual(out['b']['direct_messages'], 0)

    def test_copies_count_once(self):
        rows = [('E1', 0, 'a', 'kg:1', 'sent_to', '2010-01-02'), ('E9', 3, 'a', 'kg:1', 'sent_to', '2010-01-02 10:00')]
        self.assertEqual(self.run_signals(rows)['a']['direct_messages'], 1)

    def test_bad_dates_are_undated(self):
        out = self.run_signals([('E1', 0, 'a', 'kg:1', 'sent_to', '2031-01-01')])
        self.assertEqual((out['a']['direct_messages'], out['a']['direct_after_2008']), (1, 0))


class Withheld(unittest.TestCase):
    def test_mostly_withheld_left_out_unless_public(self):
        self.assertTrue(build_leads.mostly_withheld((30, 100), False))
        self.assertFalse(build_leads.mostly_withheld((30, 100), True))
        self.assertFalse(build_leads.mostly_withheld((5, 6), False))       # too few to say
        self.assertFalse(build_leads.mostly_withheld((30, 1000), False))   # small share

    def test_removals_are_not_scored(self):
        self.assertNotIn('removed_or_altered_docs', build_leads.WEIGHTS)


if __name__ == '__main__':
    unittest.main()
