import json
import tempfile
import unittest
from pathlib import Path

from build_contacts import contact_id
from reocr_fragments import luna_alone

KEY = b'test key'


class LunaAloneTest(unittest.TestCase):
    """Luna's reading of a fragment no OCR engine read (reocr_fragments.py --luna)."""

    def verdict(self, answer, corpus='To: jeevacation(ii;gmail.com', engine_line='From: Paul Morris', engine_addresses=()):
        known = {contact_id('jeevacation@gmail.com', 'email', KEY)}
        reading = {'page': 1, 'corpus': corpus,
                   **{name: {'line': engine_line, 'addresses': list(engine_addresses), 'known': []} for name in ('tesseract', 'rapidocr')}}
        entry = {'record_id': 7, 'role': 'to', 'fragment': 'gmail.com', 'readings': [reading]}
        with tempfile.TemporaryDirectory() as directory:
            if answer is not None:
                (Path(directory) / '7_to_0.json').write_text(json.dumps(answer))
            return luna_alone(entry, Path(directory), KEY, known, lambda i: i)

    def test_corroborated_by_garbled_corpus_text(self):
        self.assertEqual(self.verdict({'addresses': ['jeevacation@gmail.com'], 'legible': True}), 'accepted')

    def test_missing_answer_stays_unscored(self):
        self.assertEqual(self.verdict(None), 'no_address_read')

    def test_engine_reading_another_address_contradicts(self):
        self.assertEqual(self.verdict({'addresses': ['jeevacation@gmail.com'], 'legible': True},
                                      engine_addresses=['jsmith@gmail.com']), 'luna_contradicted')

    def test_fragment_on_another_header_row_is_rejected(self):
        # The redacted From address's domain, while Luna read the To line.
        self.assertEqual(self.verdict({'addresses': ['jeevacation@gmail.com'], 'legible': True},
                                      corpus='To: gmail.com>', engine_line='From: @gmail.com>'), 'luna_uncorroborated')

    def test_unknown_or_illegible(self):
        self.assertEqual(self.verdict({'addresses': ['someone@gmail.com'], 'legible': True}), 'luna_unknown_address')
        self.assertEqual(self.verdict({'addresses': [], 'legible': False}), 'luna_not_legible')


if __name__ == '__main__':
    unittest.main()
