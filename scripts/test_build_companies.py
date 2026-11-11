import unittest

from build_companies import merge_root, company_id, formless_phrases, normal_name, ocr_spellings, squashed_spellings, select
import ocr_consensus as _ocr_consensus
_ocr_consensus.PATH = _ocr_consensus.PATH.with_name('nonexistent_ocr_consensus_for_tests.json')  # tests never read the live re-OCR output


def row(key, status='candidate', target='', spelling=None):
    return {'key': key, 'status': status, 'target': target, 'top_spelling': spelling or key, 'documents': '20',
            'documents_with_folded': '20', 'spellings': ''}


def verdict(name, kind='company', relevance='network', confidence='high'):
    return {'kind': kind, 'relevance': relevance, 'name': name, 'confidence': confidence}


class CompanyTests(unittest.TestCase):
    def test_id_review_merges_evidence_without_renaming_or_creating_targets(self):
        labels = ['Historic Trading Inc.', 'Modern Research Corp.', 'Elsewhere Holdings LLC']
        ids = list(map(company_id, labels))
        rows = [row(str(i), spelling=name) for i, name in enumerate(labels)]
        primary = {str(i): verdict(name) for i, name in enumerate(labels)}
        overrides = {'merge': {ids[1]: ids[0], ids[2]: 'company_missing'}}
        groups, _ = select(rows, primary, {}, overrides, [])
        self.assertEqual(set(groups), {ids[0], ids[2]})
        self.assertEqual(groups[ids[0]]['name'], labels[0])
        self.assertEqual(set(groups[ids[0]]['keys']), {'0', '1'})
        self.assertEqual(groups[ids[2]]['name'], labels[2])
        # A configured separation vetoes the reviewed ID path too.
        groups, _ = select(rows, primary, {}, overrides, [], never=[(ids[0], ids[1])])
        self.assertEqual(set(groups), set(ids))
        separate = {'merge': {ids[2]: ids[1], ids[1]: ids[0]}}
        groups, _ = select(rows, primary, {}, separate, [], never=[(ids[0], ids[2])])
        self.assertEqual(set(groups), {ids[0], ids[1]})
        self.assertEqual(set(groups[ids[1]]['keys']), {'1', '2'})

    def test_merge_cycle_keeps_one_survivor(self):
        groups = {'a': {'keys': ['1']}, 'b': {'keys': ['2', '3']}, 'x': {'keys': ['4']}, 'c': {'keys': []}, 'd': {'keys': ['5']}}
        merge_into = {'a': 'b', 'b': 'a', 'x': 'a', 'c': 'd'}
        self.assertEqual({i: merge_root(i, merge_into, groups) for i in merge_into}, {'a': 'b', 'b': 'b', 'x': 'b', 'c': 'd'})

    def test_noise_prefixes_fold_and_rejected_companies_are_retained(self):
        labels = ['Acme Inc.', 'See Acme Inc.', 'Yes. Acme Inc.', 'I ACME Inc.',
                  'Acme Limited', '2009 Acme Limited',
                  'Legal Name Acme & Co., Inc', 'Acme & Co., Inc',
                  'While the Company', 'C INC', 'Statement.pdf']
        rows = [row(str(i), spelling=name) for i, name in enumerate(labels)]
        groups, _ = select(rows, {str(i): verdict(name) for i, name in enumerate(labels)}, {}, {}, [])
        by_name = {g['name']: g for g in groups.values()}
        self.assertEqual(set(by_name), {'Acme Inc.', 'Acme Limited', 'Acme & Co., Inc',
                                       'While the Company', 'C INC', 'Statement.pdf'})
        self.assertEqual(set(by_name['Acme Inc.']['keys']), {'0', '1', '2', '3'})
        self.assertEqual(set(by_name['Acme Limited']['keys']), {'4', '5'})
        self.assertEqual(set(by_name['Acme & Co., Inc']['keys']), {'6', '7'})

    def test_normal_name(self):
        self.assertEqual(normal_name('The C.O.U.Q. Foundation, Inc.'), normal_name('COUQ Foundation Incorporated'))
        self.assertEqual(normal_name("Michelle's Transportation Company"), 'michelle transportation co')
        self.assertNotEqual(normal_name('JEGE, Inc.'), normal_name('JEGE, LLC'))

    def test_formless_phrases(self):
        names = ['Gratitude America, Ltd.', 'JEGE, Inc.', 'JEGE, LLC', 'Maple, Inc.', 'LSJE, LLC', 'Allen & Company',
                 'Darian K. Keldry PLLC', 'Fred Vellam, P.A.', 'Tavia Vellam, P.A.', 'Butterfly Trust',
                 'Little St. James, LLC', 'Boies Schiller Flexner LLP', 'Leon Black Family Partners, L.P.', 'Mon, Inc.',
                 'While the Company', 'Material Adverse Effect on the Company']
        groups = {i: {'name': n} for i, n in enumerate(names)}
        out = formless_phrases(groups, people={'leon black'}, places={'little st james'}, given_names={'fred'},
                               dictionary={'maple', 'mon'})
        self.assertEqual(sorted(out.values()), ['boies schiller flexner', 'gratitude america', 'lsje'])

    def test_ocr_spellings(self):
        groups = {}
        for name, documents in [('Fancelli Paneling, Inc.', 40), ('Fanelli Paneling, Inc.', 5), ('Avioneta LLC', 13),
                                ('Avionetta LLC', 3), ('LDB 2014 LLC', 50), ('LOB 2014 LLC', 2), ('BlueStar I, LLC', 2),
                                ('Blue Star I, LLC', 9), ('LB Revocable Trust', 2), ('NC Revocable Trust', 9),
                                ('Apollo DIF Co-Investors, LLC', 2), ('Apollo VIF Co-Investors, LLC', 9),
                                ('Apollo Management III, L.P.', 18), ('Apollo Management IV, L.P.', 65),
                                ('JEGE, Inc.', 3582), ('JEGE, LLC', 3530), ('Viropro Investors, LLC', 2),
                                ('Viroproto Investors LLC', 9), ('APO1 Trust', 3), ('APO2 Trust', 80),
                                ('Ap0llo Partners LLC', 2), ('Apollo Partners LLC', 30)]:
            groups[company_id(name)] = {'name': name, 'kg': None, 'documents': documents}
        found = {groups[a]['name']: groups[b]['name'] for a, b in ocr_spellings(groups).items()}
        self.assertEqual(found, {'Fanelli Paneling, Inc.': 'Fancelli Paneling, Inc.', 'Avionetta LLC': 'Avioneta LLC',
                                 'LOB 2014 LLC': 'LDB 2014 LLC', 'BlueStar I, LLC': 'Blue Star I, LLC',
                                 'Ap0llo Partners LLC': 'Apollo Partners LLC'})

    def test_squashed_spellings(self):
        groups = {}
        for name, documents, kg in [('Pottinger LLC', 3, None), ('POTTI NG ER LLC', 9, None), ('AOE Ltd.', 2, None),
                                    ('A O E Ltd.', 4, None), ("3 B's LLC", 2, None), ('3Bs LLC', 5, None),
                                    ('CRW 2007 LLC', 3, None), ('CRW 2009 LLC', 3, None), ('JEGE, Inc.', 5, None),
                                    ('JEGE, LLC', 5, None), ('Alpha Co', 2, 'Alpha Co'), ('AlphaCo', 2, 'AlphaCo')]:
            groups[company_id(name)] = {'name': name, 'kg': kg, 'documents': documents}
        found = {groups[a]['name']: groups[b]['name'] for a, b in squashed_spellings(groups).items()}
        self.assertEqual(found, {'POTTI NG ER LLC': 'Pottinger LLC', 'A O E Ltd.': 'AOE Ltd.', "3 B's LLC": '3Bs LLC'})

    def test_review_sources_survive_selection_and_merging(self):
        rows = [row('alpha inc'), row('alpha llc'), row('tail co'), row('manual co')]
        primary = {'alpha inc': {**verdict('Alpha Inc.'), '_model': 'gpt-6.1-sol'},
                   'alpha llc': {**verdict('Alpha LLC'), '_model': 'gpt-6.1-terra'}}
        second = {'tail co': verdict('Tail Co.')}
        third = {'tail co': verdict('Tail Co.')}
        overrides = {'merge': {'Alpha Inc.': 'Alpha LLC'}, 'include': ['manual co'],
                     'relevance': {'alpha inc': 'network'}}
        groups, _ = select(rows, primary, second, overrides, [], third)
        by_name = {g['name']: g for g in groups.values()}
        self.assertEqual(by_name['Alpha LLC']['review_sources'],
                         {'gpt-6.1-sol', 'gpt-6.1-terra', 'manual relevance override'})
        self.assertEqual(by_name['Tail Co.']['review_sources'], {'gpt-6-luna', 'deepseek'})
        self.assertEqual(by_name['manual co']['review_sources'], {'manual inclusion'})

    def test_select(self):
        rows = [row('jege inc'), row('jege llc'), row('mitchell holdings'), row('mitchell holdings llc'),
                row('underlying fund'), row('acme corp'), row('jane roe trust'), row('glued jege inc', 'glue', 'jege inc'),
                row('tail co'), row('nysg llc'), row('new york strategy group llc')]
        sol = {'jege inc': verdict('JEGE, Inc.', relevance='epstein_controlled'), 'jege llc': verdict('JEGE, LLC'),
               'mitchell holdings': verdict('Mitchell Holdings'), 'mitchell holdings llc': verdict('Mitchell Holdings LLC'),
               'underlying fund': verdict('', kind='generic'), 'acme corp': verdict('Acme Corp.', relevance='incidental'),
               'jane roe trust': verdict('Jane Roe Trust', kind='trust'), 'nysg llc': verdict('NYSG LLC'),
               'new york strategy group llc': verdict('New York Strategy Group LLC')}
        luna = {'tail co': verdict('Tail Co.', relevance='epstein_controlled')}
        deepseek = {'tail co': verdict('Tail Co.')}
        groups, dropped = select(rows, sol, luna, {'merge': {'NYSG LLC': 'New York Strategy Group LLC'}}, ['Jane Roe'], deepseek)
        names = {g['name']: g for g in groups.values()}
        self.assertEqual(set(names), {'JEGE, Inc.', 'JEGE, LLC', 'Mitchell Holdings LLC', 'Tail Co.', 'New York Strategy Group LLC', 'Acme Corp.', 'underlying fund'})
        self.assertIn('glued jege inc', names['JEGE, Inc.']['keys'])          # folded spellings join the node
        self.assertIn('mitchell holdings', names['Mitchell Holdings LLC']['keys'])  # form-less spelling joins
        self.assertIn('nysg llc', names['New York Strategy Group LLC']['keys'])     # reviewed merge
        self.assertEqual(names['Acme Corp.']['relevance']['incidental'], 1)
        self.assertEqual(names['underlying fund']['kinds']['generic'], 1)
        self.assertEqual(names['Tail Co.']['relevance']['network'], 1)  # readers disagree on control -> network
        self.assertEqual(dropped['survivor_name'], 1)


if __name__ == '__main__':
    unittest.main()
