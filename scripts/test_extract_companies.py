import unittest

from extract_companies import consolidate, extract, name_key


class ExtractTests(unittest.TestCase):
    def test_spellings_share_one_key(self):
        keys = {name_key(*s.rsplit(' ', 1)) for s in ('SOUTHERN FINANCIAL, LLC', 'Southern Financial L.L.C.', 'The Southern Financial LLC')}
        self.assertEqual(keys, {'southern financial llc'})
        self.assertEqual(name_key('Gratitude America', 'Limited'), name_key('GRATITUDE AMERICA', 'LTD.'))
        self.assertNotEqual(name_key('JEGE', 'Inc.'), name_key('JEGE', 'LLC'))  # two legal entities
        self.assertIsNone(name_key('The', 'Company'))

    def test_extract_finds_names_and_cuts_sentences(self):
        text = 'Paid to JEGE, LLC on 3/1. Valuation Statement. Southern Trust Company was wired by HBRK Associates Inc. today'
        keys = [k for k, _ in extract(text)]
        self.assertIn('jege llc', keys)
        self.assertIn('southern trust co', keys)
        self.assertIn('hbrk associates inc', keys)
        self.assertFalse(any(k.startswith('valuation') for k in keys))
        self.assertEqual(extract('no legal names here at all'), [])

    def test_consolidate(self):
        pages = {
            'darian k keldry pllc': set(range(100)),
            'copyright of darian k keldry pllc': set(range(30)),  # glue
            'securities llc': set(range(200, 220)),
            'j p morgan securities llc': set(range(200, 230)),    # securities llc is its fragment
            'deutsche bank trust': set(range(300, 700)),
            'deutsche rank trust': set(range(700, 710)),          # OCR variant
            'crw 2007 llc': set(range(800, 900)),
            'crw 2009 llc': set(range(900, 905)),                 # other entity, digits differ
            'underlying fund': set(range(1000, 1050)),           # generic
            'plan d llc': set(range(1100, 1150)),                 # in the KG
        }
        status = consolidate(pages, ['Plan D, LLC', 'Deutsche Bank'])
        self.assertEqual(status['copyright of darian k keldry pllc'], ('glue', 'darian k keldry pllc'))
        self.assertEqual(status['securities llc'], ('fragment', 'j p morgan securities llc'))
        self.assertEqual(status['deutsche rank trust'], ('ocr', 'deutsche bank trust'))
        self.assertEqual(status['crw 2009 llc'][0], 'candidate')
        self.assertEqual(status['underlying fund'][0], 'generic')
        self.assertEqual(status['plan d llc'], ('kg', 'Plan D, LLC'))
        self.assertEqual(status['deutsche bank trust'][0], 'candidate')  # "Deutsche Bank" is a bank, not this trust


if __name__ == '__main__':
    unittest.main()
