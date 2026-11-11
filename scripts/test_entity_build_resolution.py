import json
from pathlib import Path
import tempfile
import unittest

from bounded_json import items
from entity_build_resolution import BuildIndex, wrong_type, website_org_links, viewer_references
from entity_build_rules import company_label_reason, clean_company_name, entity_junk_reason
from build_companies import resolve_company_groups
from collections import Counter
from types import SimpleNamespace


def node(identity, name, kind='organization'):
    return {'id': identity, 'name': name, 'entity_type': kind}


class BuildResolutionTests(unittest.TestCase):
    def test_exact_brands_override_wrong_person_type_but_person_shape_is_guarded(self):
        nodes = {1: node(1, 'Gulfstream'), 2: node(2, 'Jane Alpha'),
                 'p': node('p', 'Gulfstream', 'person'), 'j': node('j', 'Jane Alpha', 'person'),
                 3: {**node(3, 'Example Airlines'), 'aliases': ['Example Air']}}
        index = BuildIndex(nodes)
        self.assertEqual(index.hits('Gulfstream'), {1})
        self.assertEqual(index.cross_type('Gulfstream', 'person', {'p'})[0], 1)
        self.assertEqual(index.cross_type('Example Air', 'participant')[0], 3)
        self.assertIsNone(index.cross_type('Jane Alpha', 'person', {'j'})[0])
        self.assertIsNone(index.cross_type('Gulfstrean', 'participant')[0])
        self.assertIsNone(index.cross_type('Gulfstream', 'survivor')[0])
        index = BuildIndex({1: node(1, 'Fabrice Aidan'), 'p': node('p', 'Fabrice Aidan', 'person')})
        self.assertIsNone(index.cross_type('Fabrice Aidan', 'person', {'p'})[0])
        for value in ('Elaine McVellon', 'AJ Discala', 'FABRICE AIDAN'):
            self.assertTrue(index.person_shape(value), value)

    def test_header_label_joins_its_unique_legal_entity(self):
        index = BuildIndex({1: node(1, 'Delta Air Lines, Inc.'), 2: node(2, 'David Veyss CPA, PLLC'), 6: node(6, 'Fred Vellam, P.A.'),
                            3: node(3, 'Blue Star Jets, LLC'), 4: node(4, 'Blue Star Jets LLP'),
                            5: node(5, 'American Express Company')})
        self.assertEqual(index.cross_type('Delta Air Lines', 'participant')[0], 1)
        self.assertEqual(index.cross_type('American Express', 'participant')[0], 5)
        self.assertIsNone(index.cross_type('David Veyss', 'participant')[0])
        self.assertIsNone(index.cross_type('Fred Vellam', 'participant')[0])
        self.assertIsNone(index.cross_type('Blue Star Jets', 'participant')[0])

    def test_numbered_personal_trusts_dictionary_tails_and_attested_fao_brand_stay_distinct(self):
        from entity_build_evidence import LabelEvidence, full_key
        evidence = LabelEvidence(legal_counts={full_key('FAO Schwarz Inc.'): 3})
        index = BuildIndex({1: node(1, 'Owners Corp.'), 2: node(2, 'Life Insurance Trust'),
                            3: node(3, 'IOTA Trust'), 4: node(4, 'Bread & Company'),
                            5: node(5, 'Schwarz Inc.'), 6: node(6, 'Acme Inc.')}, evidence=evidence)
        for raw in ('66 Owners Corp.', '1992 Life Insurance Trust', 'Charles D. Barnett IOTA Trust', 'Read & Co.', 'FAO Schwarz Inc.'):
            self.assertIsNone(index.company_repair(raw)[1], raw)
        self.assertEqual(index.company_repair('FAO Acme Inc.')[1], 6)
        for raw in ('1960 East 5th Street, LLC', '2001 Main Street LLC'):
            self.assertEqual(clean_company_name(raw), raw)

    def test_ner_acronym_and_single_given_name_do_not_establish_company_identity(self):
        index = BuildIndex({'entity_ocr': node('entity_ocr', 'MRKD'),
                            'p': node('p', 'Max', 'participant')})
        self.assertIsNone(index.cross_type('MRKD', 'participant')[0])
        self.assertIsNone(index.company_repair('MAX')[1])
        from entity_build_evidence import LabelEvidence
        index = BuildIndex({'entity_brand': {**node('entity_brand', 'Gulfstream'), 'metadata': {'document_count': 125}},
                            'participant_bad': node('participant_bad', 'Gulfstream Aerospace', 'person'),
                            'entity_name': {**node('entity_name', 'Jess'), 'metadata': {'document_count': 2}}},
                           evidence=LabelEvidence(domains={'gulfstream.com'}))
        self.assertEqual(index.cross_type('Gulfstream', 'person')[0], 'entity_brand')
        self.assertIsNone(index.cross_type('Jess', 'participant')[0])
        index = BuildIndex({'entity_name': {**node('entity_name', 'Marilia'), 'metadata': {'document_count': 200}},
                            'entity_opaque': {**node('entity_opaque', 'LSJ GSJ czi'), 'metadata': {'document_count': 200}}})
        self.assertIsNone(index.cross_type('Marilia', 'participant')[0])
        self.assertIsNone(index.cross_type('LSJ GSJ czi', 'participant')[0])

    def test_bare_brand_reuses_formed_company_even_when_source_person_is_primary(self):
        index = BuildIndex({1: node(1, 'ExampleAir LLC'), 'p': node('p', 'ExampleAir', 'person')})
        self.assertEqual(index.cross_type('ExampleAir', 'person', {'p'})[0], 1)

    def test_viewer_reference_stream_keeps_only_matching_layers_and_legal_bases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'company_nodes.json').write_text(json.dumps([node(1, 'ExampleAir LLC'), node(2, 'Unrelated LLC')]))
            (root/'entity_nodes.json').write_text(json.dumps([node('entity_brand', 'ExampleBrand')]))
            (root/'graph_entities.json').write_text('must not be opened')
            references = viewer_references(root, ['ExampleAir', 'ExampleBrand'])
            self.assertEqual(set(references), {1, 'entity_brand'})

    def test_airport_names_municipalities_and_provinces_not_person_initials(self):
        rows = {'ZZZZ': {'name': 'Charles de Example International Airport', 'type': 'large_airport',
                          'municipality': 'Charlotte Example', 'iso_region': 'CA-AB'},
                'AAAA': {'name': 'Austin Airport', 'type': 'small_airport',
                          'municipality': 'Austin', 'iso_region': 'US-TX'},
                'BBBB': {'name': 'Example City Airport', 'type': 'small_airport',
                          'municipality': 'Exampleville', 'iso_region': 'US-OR'}}
        airports = SimpleNamespace(rows=rows, resolve=lambda _: (None, None))
        index = BuildIndex({1: {**node(1, 'Example International Airport', 'location'), 'icao': 'ZZZZ'}}, airports=airports)
        result = index.cross_type('Charles de Example', 'person')
        self.assertEqual(result[:2], (1, 'location'))
        self.assertEqual(result[2]['place_type'], 'airport')
        self.assertEqual(index.cross_type('Charlotte Example AB', 'person')[1], 'location')
        self.assertEqual(index.cross_type('Exampleville', 'organization')[1], 'location')
        for value in ('Austin', 'ABC', 'Exampleville Inc.', 'Charlotte Example XY'):
            self.assertIsNone(index.cross_type(value, 'person')[1], value)

    def test_streams_objects_arrays_and_chunk_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.json'
            for payload in ({'é': {'x': '\"\\\n' * 40}, 'empty': []}, [123456789, 'é', {}, []], [], {}):
                path.write_text(json.dumps(payload, ensure_ascii=False))
                expected = list(payload.items()) if isinstance(payload, dict) else payload
                for size in (1, 3, 13):
                    self.assertEqual(list(items(path, size)), expected)
            for invalid in ('{"a":1,}', '[1,]', '[1] extra', '{"a":'):
                path.write_text(invalid)
                with self.assertRaises(ValueError):
                    list(items(path, 2))

    def test_cross_type_person_duplicates_preserve_legal_entities(self):
        nodes = {1: node(1, 'Jane Alpha', 'person'), 'p': node('p', 'JANE ALPHA', 'participant'),
                 'o': node('o', 'Jane Alpha'), 't': node('t', 'Jane Alpha Trust')}
        index = BuildIndex(nodes)
        self.assertEqual(index.choose(index.hits('Jane Alpha')), 1)
        self.assertEqual(index.choose(index.hits('Jane Alpha Trust')), 't')
        self.assertEqual(index.company_repair('Jane Alpha', exclude={'o'})[1], 1)

    def test_legal_forms_are_not_interchangeable(self):
        index = BuildIndex({1: node(1, 'Acme Inc.'), 2: node(2, 'Acme LLC')})
        self.assertEqual(index.hits('Acme LLC'), {2})
        self.assertEqual(index.hits('Acme Incorporated'), {1})
        self.assertFalse(index.hits('Acme AG'))
        self.assertIsNone(index.choose(index.hits('Acme')) if not index.hits('Acme') else None)

    def test_syntax_prefixes_and_dangling_ampersand(self):
        cases = [('Defendant Acme Inc.', 'Acme Inc.'), ('Plaintiffs Acme Inc.', 'Acme Inc.'),
                 ('Respondent Acme Inc.', 'Acme Inc.'), ('Claimant Acme Inc.', 'Acme Inc.'),
                 ('As CEO of Acme Inc.', 'Acme Inc.'), ('Trustee of The Acme Trust', 'The Acme Trust'),
                 ('Trust Agreement for The Acme Trust', 'The Acme Trust'),
                 ('FBO Acme Inc.', 'FBO Acme Inc.'), ('FAO Acme Inc.', 'FAO Acme Inc.'),
                 ('Wire Transfer Acme Inc.', 'Acme Inc.'), ('Acme Partners &', 'Acme Partners'),
                 ('Copyright 0 2016 Acme Inc.', 'Acme Inc.'), ('Mr. Acme Trust', 'Acme Trust')]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(clean_company_name(raw), expected)
        for name in ('HI Capital Partners LLC', 'MS Dealer Service Corp.', 'Hi-Tech Concrete Pumping, LLC'):
            self.assertEqual(clean_company_name(name), name)
            self.assertIsNone(company_label_reason(name))

    def test_anchor_required_for_number_country_ticker_and_person_prefix(self):
        index = BuildIndex({1: node(1, 'Cisco Systems Inc.'), 2: node(2, 'Acme Industries LLC'),
                            3: node(3, 'Interiors LLC'), 4: node(4, 'Banco do Brasil S.A.')})
        for raw, target in [('CSCO Cisco Systems Inc.', 1), ('14000 SHRS Cisco Systems Inc.', 1),
                            ('000 Acme Industries LLC', 2), ('USA Acme Industries LLC', 2),
                            ('United States Acme Industries LLC', 2),
                            ('Jane Alpha I Acme Industries LLC', 2), ('TO BANCO DO B RASIL S.A.', 4)]:
            with self.subTest(raw=raw):
                self.assertEqual(index.company_repair(raw)[1], target)
        for raw in ('JSC Interiors LLC', 'BP Acme Industries LLC', 'HI Acme Industries LLC',
                    '118 Main Street LLC', 'CSCO Another Systems Inc.'):
            self.assertIsNone(index.company_repair(raw)[1])

    def test_truncation_requires_unique_company_and_preserves_form(self):
        index = BuildIndex({1: node(1, 'Northern Financial LLC'), 2: node(2, 'Deutsche Bank AG'),
                            3: node(3, 'Tourmaline Partners LLC'), 4: node(4, 'J.P. Morgan Securities LLC'),
                            5: node(5, 'Northern Financial Inc.'), 6: node(6, 'Bell Textron Inc.')})
        for raw, target in [('THERN FINANCIAL LLC', 1), ('TSCHE BANK AG', 2),
                            ('urmaline Partners', 3), ('Morgan Securities LLC', 4)]:
            self.assertEqual(index.company_repair(raw)[1], target)
        for raw in ('Textron Inc.', 'Bank AG', 'THERN FINANCIAL AG'):
            self.assertIsNone(index.company_repair(raw)[1])
        ambiguous = BuildIndex({1: node(1, 'Northern Financial LLC'), 2: node(2, 'Southern Financial LLC')})
        self.assertIsNone(ambiguous.company_repair('THERN FINANCIAL LLC')[1])

    def test_ocr_vocabulary_and_ampersand_only_to_existing_spelling(self):
        index = BuildIndex({1: node(1, 'Deutsche Bank AG'), 2: node(2, 'Financial Trust Company Inc.'),
                            3: node(3, 'Bear Stearns LLC'), 4: node(4, 'Wharton & Garrison LLP'),
                            5: node(5, 'Apollo 1 Trust'), 6: node(6, 'Apollo 2 Trust')})
        for raw, target in [('Deutche Bank AG', 1), ('Financlal Trust Company Inc.', 2),
                            ('Bear Steams LLC', 3), ('Wharton 6 Garrison LLP', 4),
                            ('Wharton 8 Garrison LLP', 4)]:
            self.assertEqual(index.company_repair(raw)[1], target)
        self.assertIsNone(index.company_repair('Wharton 6 Unknown LLP')[1])
        self.assertEqual(index.company_repair('Apollo 2 Trust')[1], 6)

    def test_bare_boilerplate_and_company_ocr_are_hidden(self):
        for raw in ('Because the Fund', 'Aviation Co.', 'Cayman LP', 'Manager of the Fund', 'COIAPANY INC',
                    'CORNPANY INC', 'Company Incorporated', 'Limited Liability Company', 'Services Inc.',
                    'FROM THE ACCOUNT HELD DIRECTLY AT THE FUND COMPANY', 'Hi Jeff. Trust',
                    'Dr. Serge Alpha and Incorporated', "Mr. Alpha's and the Foundation"):
            with self.subTest(raw=raw):
                self.assertIsNotNone(company_label_reason(clean_company_name(raw)))
        for raw in ('School of Earth and Space Exploration', 'Financial Trust Company, Inc.',
                    'Acme Aviation Co.', 'Acme Services Inc.', 'The 1953 Trust'):
            self.assertIsNone(company_label_reason(raw))

    def test_machine_fields_dates_french_and_garbage(self):
        for raw in ('Content-Type', 'Content-Type: TEXT/HTML', 'DKIM-Signature', 'X-Mailer',
                    'MsoNormal', 'FileHashType', 'ESMTPS', 'Gesendet:', 'style="font',
                    'font-family: Arial', 'BEGIN:VALARM', 'DTSTART:20160101', 'IIIIII',
                    '20-Nov-2014', '2014-11-20', 'Print Name', 'peut être assurée',
                    'Envoye de mon', 'American Express Travel XXXXXX'):
            with self.subTest(raw=raw):
                self.assertIsNotNone(entity_junk_reason(raw))
        for raw in ('Jean Alpha', 'Smith Travel LLC', 'Calendar Research Institute', 'G550', 'Mso Partners'):
            self.assertIsNone(entity_junk_reason(raw))

    def test_formats_retype_without_inventing_person_identity(self):
        for raw, expected in [('notify@example.com', 'email'), ('www.example.com', 'website'),
                              ('https://example.com/path', 'website'), ('GIV', 'vehicle'),
                              ('G550', 'vehicle'), ('MA 02109', 'location')]:
            self.assertEqual(wrong_type(raw)[0], expected)
        for raw in ('image001.jpg', 'Statement.pdf', 'GIV Partners LLC', 'Example com', 'Jane Alpha'):
            self.assertIsNone(wrong_type(raw)[0])
        self.assertIsNone(wrong_type('prabhuling-c.kottalagi')[0])

    def test_private_aliases_and_email_repairs_are_bounded_and_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            lookup = Path(directory) / 'lookup.json'
            lookup.write_text(json.dumps({'participant_a': {'kind': 'participant', 'value': 'Jane Alpha'},
                                          'email_b': {'kind': 'email', 'value': 'sample@example.com'}}))
            index = BuildIndex({'participant_a': node('participant_a', 'HeaderLabel_a', 'participant'),
                                'email_b': node('email_b', 'Email_b', 'email')}, key=b'fixture', lookup=lookup)
            self.assertEqual(index.hits('Jane Alpha'), {'participant_a'})
            self.assertEqual(index.email_hits('sample@example.com'), {'email_b'})
            self.assertEqual(index.existing['participant_a']['name'], 'HeaderLabel_a')

    def test_company_group_reuses_contact_and_person_evidence(self):
        def group(name, key):
            return {'name': name, 'kinds': Counter(company=1), 'relevance': Counter(network=1),
                    'keys': [key], 'agree': True, 'kg': None}
        groups = {'company_a': group('Jane Alpha', 'a'), 'company_b': group('sample@example.com', 'b'),
                  'company_c': group('G550', 'c')}
        existing = {1: node(1, 'Jane Alpha', 'person'), 'email_x': node('email_x', 'sample@example.com', 'email')}
        result = resolve_company_groups(groups, existing)
        self.assertEqual(set(result), {1, 'email_x', 'company_c'})
        self.assertEqual(result[1]['keys'], ['a'])
        self.assertEqual(result['email_x']['keys'], ['b'])
        self.assertEqual(result['company_c']['existing_node']['entity_type'], 'vehicle')

    def test_website_of_exact_base_links_without_merging(self):
        nodes = [node(1, 'Acme & Alpha LLP'), node(2, 'Bank of Example Corporation'),
                 node(3, 'acmealpha.com', 'website'), node(4, 'bankofexample.com', 'website'),
                 node(5, 'acmealpha-help.com', 'website'), node(6, 'ac.com', 'website'),
                 node(7, 'AC Inc.'), node(8, 'www.acmealpha.co.uk', 'website')]
        links = website_org_links(nodes)
        self.assertEqual({(e['source_entity_id'], e['target_entity_id']) for e in links}, {(3, 1), (4, 2), (8, 1)})
        self.assertTrue(all(e['relationship_type'] == 'website_of' for e in links))
        self.assertEqual(len(nodes), 8)

    def test_website_of_keeps_digits_and_skips_junk_labels(self):
        nodes = [node(1, 'Point 15'), node(2, 'point.com', 'website'), node(3, '7/2/2020 - Maxwell'),
                 node(4, 'maxwell.net', 'website'), node(5, 'Point Capital LLC'), node(6, 'pointcapital.com', 'website')]
        self.assertEqual({(e['source_entity_id'], e['target_entity_id']) for e in website_org_links(nodes)}, {(6, 5)})

    def test_bare_brand_completes_to_one_formed_spelling_only(self):
        index = BuildIndex({'a': node('a', 'Zephyr & Company'), 'b': node('b', 'Zephyr & Company, Inc.'),
                            'c': node('c', 'Orion Systems Company'), 'd': node('d', 'Orion Systems Corporation'),
                            'e': node('e', 'Quill Foundry'), 'f': node('f', 'Quill Foundry LLC'),
                            'g': node('g', 'Quill Foundry Inc.'), 'h': node('h', 'Rusty Company'),
                            'i': node('i', 'Rusty Ltd.')})
        self.assertEqual(index.company_repair('Zephyr & Company', exclude={'a'})[1], 'b')
        # Company vs Corporation can be two successor firms; several legal forms are ambiguous;
        # a one-word brand needs shared documents (none provided).
        self.assertIsNone(index.company_repair('Orion Systems Company', exclude={'c'})[1])
        self.assertIsNone(index.company_repair('Quill Foundry', exclude={'e'})[1])
        self.assertIsNone(index.company_repair('Rusty Company', exclude={'h'})[1])

    def test_equivalent_written_legal_forms_point_one_way(self):
        index = BuildIndex({'a': node('a', 'Acme Holdings Pvt. Ltd.'), 'b': node('b', 'Acme Holdings Private Ltd.')})
        targets = {index.company_repair(v, exclude={i})[1] for i, v in (('a', 'Acme Holdings Pvt. Ltd.'), ('b', 'Acme Holdings Private Ltd.'))}
        self.assertEqual(len(targets - {None}), 1)

    def test_stub_reason_hides_lone_words_but_keeps_names_and_legal_firms(self):
        index = BuildIndex({1: node(1, 'Jay', 'person')})
        words = {'settle', 'credits', 'password'}
        self.assertEqual(index.stub_reason('Settle', words), 'dictionary_words')
        self.assertIsNone(index.stub_reason('Credits Inc.', words))
        self.assertIsNone(index.stub_reason('Jay', words))
        self.assertEqual(index.stub_reason('MZ', words), 'short_span')
        self.assertIsNone(index.stub_reason('Acme Partners', words))

    def test_leading_airport_code_needs_the_airports_own_city(self):
        class Airports:
            rows = {'KPBI': {'municipality': 'West Palm Beach', 'name': 'Palm Beach International Airport', 'type': 'large_airport', 'iso_region': 'US-FL'}}
            def resolve(self, value):
                return ('KPBI', 'code') if value.startswith('PBI ') else (None, None)
        index = BuildIndex({}, airports=Airports())
        self.assertEqual(index.place_type('PBI West Palm Beach, FL, United States')[0:2], (None, 'location'))
        self.assertIsNone(index.place_type('PBI Chicago, IL, United States')[1])


if __name__ == '__main__':
    unittest.main()
