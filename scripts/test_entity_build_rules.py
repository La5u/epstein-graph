import unittest

from entity_build_rules import (clean_label, clean_company_name, company_label_reason,
                                company_tail, website_reason, website_ocr_target, text_reason)


class BuildLabelRuleTests(unittest.TestCase):
    def test_dates_dated_headings_and_citations_are_not_entities(self):
        for value, reason in [('Sun, Nov 15, 2009', 'date_text'), ('Feb 18-25', 'date_text'),
                              ('7/2/2020 - Maxwell', 'dated_heading'), ('Chicago Tribune (9/18', 'dated_heading'),
                              ('767 N.Y.S.2d 228', 'citation'), ('arXiv:1403.3289', 'citation')]:
            self.assertEqual(text_reason(value), reason, value)
        for value in ('Sunday Times', 'Sun Microsystems', 'Nov Holdings', 'March of Dimes', 'May Department Stores', 'Chicago Tribune'):
            self.assertIsNone(text_reason(value), value)

    def test_sentence_breaks_read_as_domains(self):
        for domain in ('good.by', 'well.today', 'will.be', 'mother.now'):
            self.assertEqual(website_reason(domain, (domain,)), 'sentence_break_domain', domain)
        for domain in ('amazon.com', 'spiegel.de', 'bit.ly', 'justice.gov'):
            self.assertIsNone(website_reason(domain, (domain,)), domain)
        self.assertIsNone(website_reason('good.by', ('https://good.by',)))
        self.assertIsNone(website_reason('good.by', ('good.by',), attested=True))

    def test_greetings_are_salutations_not_entities(self):
        from entity_build_rules import text_reason
        for label in ['Hello Lesley', 'Dear Sir/Madam', 'Hello Barnaby', 'Dear EarthLink Subscriber']:
            self.assertEqual(text_reason(label), 'salutation', label)
        for label in ['HI LP', 'Hi-Tech Concrete', 'Best Buy Co', 'Thanks Inc', 'Good Morning America']:
            self.assertNotEqual(text_reason(label), 'salutation', label)

    def test_company_requires_distinctive_name_not_legal_prose(self):
        hidden = ['While the Company', 'Section 15 of the LLC', 'Purpose and Business of the Company',
                  'Material Adverse Effect on the Company', 'Subsidiary of the Company', 'Bind the Trust',
                  'Balance of Trust', 'Can LP', 'Contains Limited', 'YOUR DEDUCTION IS NOT LIMITED',
                  'WILL BE VOID AB', 'C INC', 'T LLC', 'ZE Inc.', 'New York Inc.', 'Products Inc.',
                  'Paul G. Alpha 6 Motion for Limited', 'Article FIRST of the Insurance Trust',
                  'THERE CAN BE NO ASSURANCE THAT THE FUND']
        hidden += [prefix + ' the Example Fund' for prefix in (
            'Become A', 'Select And', 'Sign And', 'Consult Its', 'Assure That', 'Invest In', 'Investing In',
            'Effect Of', 'Initial Closing Of', 'Valuation Of', 'Examinations Of', 'Risks Related To',
            'Regulatory Programs', 'Private Offering Memorandum', 'Principal Payment', 'Periodic Review Of',
            'KYC Party Contact', 'Grantor Of', 'Affiliates Of', 'Country Or State')]
        for name in hidden:
            with self.subTest(name=name):
                self.assertIsNotNone(company_label_reason(name))
        for name in ['School of Earth and Space Exploration', 'Acme Products Inc.', 'AB Capital LLC',
                     'J.E.G.E. Inc.', 'The 1953 Trust', 'Bank of the West Inc.',
                     'John D. and Catherine T. Alpha Foundation', 'Reich & Tang Asset Management L.P.']:
            with self.subTest(name=name):
                self.assertIsNone(company_label_reason(name))

    def test_syntactic_company_leadins(self):
        for raw, clean in [('See Acme Inc.', 'Acme Inc.'), ('Yes. A.C.M.E. Inc.', 'A.C.M.E. Inc.'),
                           ('I ACME Inc', 'ACME Inc'), ('Legal Name Acme & Co., Inc', 'Acme & Co., Inc'),
                           ('2009 Acme Limited', 'Acme Limited'), ('D 2016 Acme Corporation', 'Acme Corporation'),
                           ('Copyright (c) 2013 Acme Ltd.', 'Acme Ltd.'), ('Esq. Alpha Beta LLP', 'Alpha Beta LLP')]:
            self.assertEqual(clean_company_name(raw), clean)
        self.assertEqual(clean_company_name('I Management LLC'), 'I Management LLC')
        self.assertEqual(clean_company_name('3M Company'), '3M Company')
        self.assertEqual(clean_company_name('2009 Family Trust'), '2009 Family Trust')
        self.assertEqual(clean_company_name('Account Title Acme LLC'), 'Acme LLC')
        for raw, clean in [('Principal Payment CIT GROUP INC', 'CIT GROUP INC'), ('OPERATED 8Y DELTA AIR LINES INC', 'DELTA AIR LINES INC'),
                           ('Neither FMR LLC', 'FMR LLC'), ('Cleared Southern Financial LLC', 'Southern Financial LLC'),
                           ('Make-A-Wish Foundation', 'Make-A-Wish Foundation')]:
            self.assertEqual(clean_company_name(raw), clean)
        self.assertEqual(clean_company_name('Filer Name Acme LLC'), 'Acme LLC')

    def test_person_or_title_prefix_requires_one_existing_full_target(self):
        lookup = lambda name: {'firm'} if name == 'Acme Corporation' else set()
        zlookup = lambda name: {'z'} if name == 'Zentrix Corporation' else set()
        self.assertEqual(company_tail('Joel I. Alpha Zentrix Corporation', zlookup), ('Zentrix Corporation', {'z'}))
        for name in ['Jane Alpha Partner Acme Corporation',
                     'President of Acme Corporation']:
            self.assertEqual(company_tail(name, lookup), ('Acme Corporation', {'firm'}))
        for name in ['Palm Beach Acme Corporation', 'Alpha Beta Corporation']:
            self.assertFalse(company_tail(name, lookup)[1])
        # A person-named firm must not fold into a generic dictionary-word stub.
        generic = lambda name: {'stub'} if name == 'Landscaping LLC' else set()
        self.assertFalse(company_tail('Thomas J. Costello Landscaping LLC', generic)[1])
        self.assertFalse(company_tail('Jane Alpha Partner Acme Corporation', lambda _: {'a', 'b'})[1])

    def test_filename_and_signature_are_separate_classes(self):
        for name in ['image003.jpg', 'Inline-Images: EWLarge2.jpg', 'Statement.pdf', 'TEXT.htm', 'photo.JPG',
                     'attachment.docx', 'dataset.csv', 'meeting.ics', 'archive.zip', 'logo.svg']:
            self.assertEqual(company_label_reason(name), 'file_name')
        self.assertEqual(clean_label('1755 N Brown Rd [signature: image001.png]'), '1755 N Brown Rd')
        self.assertIsNone(company_label_reason('1755 N Brown Rd [signature: image001.png]'))
        self.assertIsNone(company_label_reason('Jpg Productions Inc.'))
        self.assertIsNone(text_reason('images.jpg.com'))
        self.assertIsNone(text_reason('Will Alpha'))
        for text in ['Trying to find a cab I', 'Send a followup to the dealer', 'Jane Alpha cv attached',
                     'Jane Everyone Who Works For Him', 'upenn more fun georgtown smarter peopl',
                     'Mr Alpha back in Palm Beach']:
            self.assertEqual(text_reason(text), 'sentence_fragment')

    def test_websites_need_url_context_for_dictionary_words(self):
        hidden = ['ail.com', 'al.com', 'l.com', 'e.com', 'i.com', 'il.com', 'le.com', 'gle.com',
                  'gmaii.com', 'gmall.com', 'innail.com', 'qmail.com', 'umail.com', 'acationpgrnail.com',
                  'epsteinueevacationftmail.com', 'jcevacationesmail.com', 'jeevacationftmail.com',
                  'thsmail.com', 'feetr4mail.com', 'teettigmarl.com', 'drive.goo', 'drive.goog', 'drive.google',
                  'the.gov', 'with.gov', 'this.com', 'your.com', 'value.net', 'summary.net', 'total.net',
                  'beginning.net', 'ending.net', 'presented.net', 'income.net', 'safety.net', 'on.net',
                  'on.org', 'manage.com', 'managel.com']
        for domain in hidden:
            with self.subTest(domain=domain):
                self.assertIsNotNone(website_reason(domain))
        self.assertIsNone(website_reason('value.net', ['https://value.net/a']))
        self.assertIsNone(website_reason('summary.net', ['www.summary.net']))
        self.assertEqual(website_reason('summary.net', ['x@summary.net']), 'website_in_email')
        self.assertEqual(website_reason('gmall.com', ['https://gmall.com']), 'mail_provider_lookalike')
        self.assertIsNone(website_reason('example.com', ['x@example.com', 'https://example.com']))
        self.assertIsNone(website_reason('gmail.com'))
        self.assertEqual(website_reason('un.org', ['https://un.org']), 'short_domain_label')
        self.assertEqual(website_reason('sample.com', (), {'sample'}), 'bare_dictionary_domain')

    def test_ocr_domain_fold_needs_dominant_unambiguous_lookalike(self):
        counts = {'flightaware.com': 1771, 'flightawarc.com': 443, 'tlightaware.com': 409}
        self.assertEqual(website_ocr_target('flightawarc.com', counts), 'flightaware.com')
        self.assertEqual(website_ocr_target('tlightaware.com', counts), 'flightaware.com')
        for counts in [{'longname.com': 6, 'longnamc.com': 3},
                       {'longname.com': 20, 'longnamc.com': 3, 'longnamc.org': 50},
                       {'longname.com': 20, 'longnamc.com': 3, 'longnamf.com': 20}]:
            result = website_ocr_target('longnamc.com', counts)
            # f/c is not in the OCR substitution vocabulary; no ambiguity here.
            self.assertEqual(result, 'longnamc.com' if counts['longname.com'] == 6 else 'longname.com')
        self.assertEqual(website_ocr_target('longnamc.com', {'longnamc.com': 2, 'longname.com': 20, 'longnamc.net': 30}), 'longname.com')
        self.assertEqual(website_ocr_target('longnamc.com', {'longnamc.com': 2, 'longname.com': 20, 'longnanc.com': 20}), 'longnamc.com')


class StubAndPrefixTests(unittest.TestCase):
    def test_industry_geography_and_abbreviation_stubs_hidden(self):
        for raw in ('Atlantic Corp.', 'Coffee Inc.', 'Assoc. Inc.', 'Mach. Inc.', 'Air Inc', 'Delaware LLC',
                    "Ass'n Inc.", 'Aircraft Company'):
            self.assertIsNotNone(company_label_reason(raw), raw)
        for raw in ('M&T Bank Corporation', 'Acme Coffee Inc.', 'Bank of the West Inc.'):
            self.assertIsNone(company_label_reason(raw), raw)

    def test_amount_division_and_role_prefixes(self):
        for raw, expected in [('000 CEMEX Inc.', 'CEMEX Inc.'), ('02015 Acme Aerospace Corporation', 'Acme Aerospace Corporation'),
                              ('0 2015 Acme Aerospace Corporation', 'Acme Aerospace Corporation'),
                              ('A Division of Acme Inc.', 'Acme Inc.'), ('Co-Founder of Acme Inc.', 'Acme Inc.'),
                              ('Closing Acme Bank AG', 'Acme Bank AG'), ('Creditor Acme Group LLC', 'Acme Group LLC'),
                              ('California-based First Foundation', 'First Foundation')]:
            self.assertEqual(clean_company_name(raw), expected)
        for raw in ('1031 Exchange Services, LLC', '13 Ace Entertainment Holdings Inc.', '112 Main Street LLC'):
            self.assertEqual(clean_company_name(raw), raw)

    def test_document_headings_and_unit_fragments_are_not_entities(self):
        from entity_build_rules import entity_junk_reason
        for raw in ('10/05/2012 Page 1 of 20', '2nd Floor East', 'ABA Routing', 'Total Before Tax',
                    'Motion for Summary Judgment', 'Net Asset Value'):
            self.assertTrue(entity_junk_reason(raw), raw)
        for raw in ('Floor Plans Inc.', 'Total Bank', 'Summary Judgment Partners LLC'):
            self.assertIsNone(entity_junk_reason(raw), raw)

    def test_copyright_circle_and_equivalent_legal_forms(self):
        from entity_build_rules import company_spelling_key
        self.assertEqual(clean_company_name('O2015 Acme Inc.'), 'Acme Inc.')
        self.assertEqual(clean_company_name('O 2007 Family Trust'), '2007 Family Trust')
        self.assertEqual(company_spelling_key('Acme Pvt Ltd'), company_spelling_key('Acme Private Limited'))
        self.assertNotEqual(company_spelling_key('Acme Inc.'), company_spelling_key('Acme LLC'))


if __name__ == '__main__':
    unittest.main()
