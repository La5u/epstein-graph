"""Regression checks for document-only parsing and ordered party removal."""
import random
import unittest
from unittest.mock import patch

import header_text


class HeaderTextTests(unittest.TestCase):
    def test_non_names_have_syntactic_reasons_without_rejecting_bare_people(self):
        cases = {
            'Su bject': 'header_field', 'Importance Normal': 'header_field',
            'Message-ID': 'header_field', 'X-Eudora-Signature': 'header_field',
            'Hello Zelda': 'salutation_or_signoff', 'Good afternoon Mr. Hargrave': 'salutation_or_signoff',
            'Best wishes Zelda': 'salutation_or_signoff', 'Assistant Vice President': 'role_title',
            'Director of Properties': 'role_title', 'FileHashType': 'form_field',
            'Flight Time': 'form_field', 'Bank Name': 'form_field',
            'depuis mon iPhone': 'device_signature', 'Sent from my iPad': 'device_signature',
            'Tuesda': 'date_fragment', 'April Monday': 'date_fragment',
            'Helvetica Neue': 'markup_or_font', 'cipher=OTHER': 'markup_or_font',
            'mail com': 'address_delimiter', 'mailtoa': 'address_delimiter',
        }
        for label, reason in cases.items():
            with self.subTest(label=label):
                self.assertEqual(header_text.header_junk_reason(label), reason)
        for label in ('May', 'Low', 'Sun', 'Normal', 'Zelda', 'Nerla Nerla', 'KB',
                      'Hope Rose', 'George Best', 'HelloFresh', 'Mail Delivery Subsystem',
                      'Sony Concierge', 'Woody Allen Assistant', 'Zelda Hargrave MD'):
            self.assertIsNone(header_text.header_junk_reason(label), label)

    def test_bare_field_does_not_attest_a_whole_signature(self):
        text = 'From: Actual Sender\nSubject: Update\nTo:\nZelda Hargrave\nNY Office\nAssistant Vice President\nwww.example.test'
        bad = header_text.nonparty_header_text(text)
        self.assertNotIn(header_text.word_key('Zelda Hargrave'), bad)
        for label in ('NY Office', 'Assistant Vice President'):
            self.assertIn(header_text.word_key(label), bad)

    def test_split_reply_header_attests_only_the_name(self):
        lines = ['On Oct 6, 2015, at 10:17 AM, Zelda Hargrave <', '> wrote:', 'Best wishes Zelda']
        self.assertEqual(list(header_text.reply_parties(lines)), ['Zelda Hargrave'])
        text = 'From: Actual Sender\nSubject: Update\nTo:\n' + '\n'.join(lines)
        bad = header_text.nonparty_header_text(text)
        self.assertNotIn(header_text.word_key('Zelda Hargrave'), bad)
        self.assertIn(header_text.word_key('Best wishes Zelda'), bad)
        self.assertEqual(list(header_text.reply_parties(['On a clear day Zelda wrote:'])), [])

    def test_nonparty_header_fields_close_a_party_slot(self):
        text = 'From: Zelda Hargrave\nTo: Ramon Other\nReply-To: reply@example.test\nImportance: Normal\nBody text'
        bad = header_text.nonparty_header_text(text)
        self.assertNotIn(header_text.word_key('Ramon Other'), bad)
        self.assertIn(header_text.word_key('reply@example.test'), bad)

    def test_cached_recipients_return_independent_lists(self):
        value = 'First Person; Second Person <second@example.test>'
        expected = header_text.parse_recipient(value)
        header_text.parse_recipient(value).clear()
        self.assertEqual(header_text.parse_recipient(value), expected)
        self.assertEqual(header_text.parse_recipient(['First Person', None]), [('First Person', None)])

    def test_indexed_removal_matches_ordered_replace(self):
        rng = random.Random(0)
        cases = [
            (' a a a ', [' a ']),  # adjacent matches share a consumed space
            (' a b c ', [' b ', ' a c ']),  # later key spans an earlier removal
            (' a b c ', [' a b ', ' b c ']),  # overlapping keys use key order
            (' a b c ', [' b c ', ' a b ']),
            ('  ', ['  ']),
            (' a b ', [' a b ', '  ']),
        ]
        for _ in range(1000):
            body = ' ' + ' '.join(rng.choices(['a', 'b', 'c'], k=rng.randrange(40))) + ' '
            keys = [' ' + ' '.join(rng.choices(['a', 'b', 'c'], k=rng.randrange(5))) + ' '
                    for _ in range(10)]
            cases.append((body, keys))
        for body, keys in cases:
            expected = body
            for key in keys:
                expected = expected.replace(key, ' ')
            for threshold in (0, 10**9):
                with patch.object(header_text, 'PARTY_REPLACE_SCAN_LIMIT', threshold):
                    self.assertEqual(header_text.remove_party_keys(body, keys), expected, (body, keys))


if __name__ == '__main__':
    unittest.main()
