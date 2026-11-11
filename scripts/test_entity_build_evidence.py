import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from adapters.efta import create
from build_companies import visibility_reason
from entity_build_evidence import LabelEvidence, MIN_LEGAL_DOCUMENTS, full_key
from entity_build_rules import company_label_reason, website_reason


class LabelEvidenceTests(unittest.TestCase):
    def test_attestation_keeps_full_legal_form_and_blocks_prose(self):
        counts = {full_key(s): n for s, n in [('Community Bank Inc.', 3), ('Total S.A.', 4),
                  ('Investor AB', 6), ('York Foundation', 7), ('Aviation Co.', 2),
                  ('Because the Fund', 1000), ('I LLC', 200), ('COIAPANY INC', 100)]}
        evidence = LabelEvidence(trusted=['U.S. Trust Company'], legal_counts=counts)
        self.assertEqual(MIN_LEGAL_DOCUMENTS, 3)
        for value in ('Community Bank, Inc.', 'Total S.A.', 'Investor AB', 'York Foundation', 'U.S. Trust Company'):
            self.assertTrue(evidence.company_attested(value), value)
            self.assertIsNone(company_label_reason(value, evidence.company_attested(value)))
            self.assertIsNone(visibility_reason(value, 'company', evidence))
        for value in ('Total S.A.', 'Investor AB', 'York Foundation', 'U.S. Trust Company'):
            self.assertIsNone(visibility_reason(value, 'generic', evidence))
        for value in ('Community Bank LLC', 'Aviation Co.', 'Because the Fund', 'I LLC', 'COIAPANY INC'):
            self.assertFalse(evidence.company_attested(value), value)
        self.assertEqual(company_label_reason('Purpose and Business of the Company', True), 'sentence_fragment')

    def test_ampersand_initial_names_and_surname_firms(self):
        for value in ('AT&T Services', 'AT&T Investment Management', 'M&T Bank Corporation', 'A&B Services', 'AT & T Techs. Inc.'):
            self.assertIsNone(company_label_reason(value))
        for value in ('Frankel LLP', 'Coleman LLC', 'Connolly LLP', 'Alpha PLLC', 'Bell P.A.'):
            self.assertIsNone(visibility_reason(value, 'generic'), value)
            self.assertIsNone(visibility_reason(value, 'junk'), value)
            self.assertEqual(visibility_reason(value, 'junk', explicit_rejection=True), 'reviewed_rejection')
        for value in ('Because the Fund', 'Services Inc.', 'C INC'):
            self.assertIsNotNone(visibility_reason(value, 'generic'), value)
        for value in ('Landscaping LLC', 'Index LLC', 'NOVICE PLLC', 'SJE LLC', 'TIONS LLC', 'EPORTERS P.C.', 'FrIANCLAL LLC'):
            self.assertEqual(visibility_reason(value, 'junk'), 'reviewed_rejection', value)
        self.assertIsNone(company_label_reason('Bitcoin Savings and Trust'))
        self.assertIsNone(company_label_reason('Belford. Fund'))
        from entity_build_rules import text_reason
        self.assertEqual(text_reason('Verstuurd'), 'correspondence_boilerplate')
        self.assertEqual(website_reason('ail.co'), 'mail_provider_fragment')

    def test_project_evidence_counts_distinct_documents_per_spelling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'output/.build').mkdir(parents=True)
            (root/'config').mkdir()
            (root/'config/company_review.json').write_text(json.dumps({
                'include': ['Example Trust'], 'exclude': ['Reject Trust'],
                'merge': {'Legacy Trust': 'Canonical Trust', 'company_old': 'company_target'}}))
            with sqlite3.connect(root/'output/.build/company_mentions.db') as db:
                db.execute('CREATE TABLE mentions(key,spelling,efta)')
                db.executemany('INSERT INTO mentions VALUES (?,?,?)',
                    [('k', 'Community Bank Inc.', 'a')]*20 + [('k', 'Community Bank Inc.', d) for d in ('b', 'c')]
                    + [('k', 'Services Inc.', 'a')]*100)
            evidence = LabelEvidence.project(root)
            self.assertTrue(evidence.company_attested('Example Trust'))
            self.assertTrue(evidence.company_attested('Community Bank, Inc.'))
            self.assertFalse(evidence.company_attested('Community Bank LLC'))
            self.assertFalse(evidence.company_attested('Services Inc.'))
            self.assertTrue(evidence.company_attested('Legacy Trust'))
            self.assertTrue(evidence.company_attested('Canonical Trust'))
            self.assertFalse(evidence.company_attested('company_old'))
            self.assertFalse(evidence.company_attested('company_target'))
            early = LabelEvidence.project(root, include_counts=False)
            self.assertEqual(early.legal_counts, {})
            self.assertTrue(early.company_attested('Example Trust'))

    def test_real_urls_and_email_domains_rescue_weak_hosts_but_not_mail_garbles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'corpus.db'
            with sqlite3.connect(path) as db:
                create(db, [('a', 1, 'Write to user@rr.com, http://www.w3.org/ or https://match.com/search.'),
                            ('b', 1, 'Contact user@partners.org. Bare total.net and junk@gmall.com.')])
            evidence = LabelEvidence()
            evidence.prepare_domains(['rr.com', 'w3.org', 'match.com', 'partners.org', 'total.net', 'gmall.com'], path)
            self.assertEqual(evidence.domains, {'rr.com', 'w3.org', 'match.com', 'partners.org'})
            self.assertIsNone(website_reason('rr.com', attested=True))
            self.assertIsNone(website_reason('match.com', dictionary={'match'}, attested=True))
            self.assertEqual(website_reason('gmall.com', attested=True), 'mail_provider_lookalike')
            self.assertEqual(website_reason('example.com', ['user@example.com'], attested=True), 'website_in_email')

    def test_corpus_fills_legal_spelling_gap_and_count_does_not_rescue_classes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'corpus.db'
            with sqlite3.connect(path) as db:
                create(db, [(d, 1, 'Branch Banking and Trust Co.; First State Ins. Co.') for d in ('a', 'b', 'c')])
            evidence = LabelEvidence()
            evidence.prepare_companies(['Branch Banking and Trust Company', 'First State Insurance Company'], path)
            self.assertTrue(evidence.company_attested('Branch Banking and Trust Company'))
            self.assertTrue(evidence.company_attested('First State Insurance Company'))
            for name in ('Foreign Corporation', 'Sponsor and the Access Fund', 'FIRST COMPANY', 'Delaware Business Trust', 'General Partner and the Special Limited'):
                self.assertFalse(evidence.company_attested(name, documents=1000), name)
            self.assertFalse(evidence.company_attested('Growth LLC', documents=1000))
            self.assertFalse(evidence.company_attested('U.S. Trust Company', documents=19))
            self.assertTrue(evidence.company_attested('U.S. Trust Company', documents=20))
        evidence = LabelEvidence(geographic=['New York'])
        self.assertTrue(evidence.company_attested('New York State Insurance Fund', documents=20))
        self.assertFalse(evidence.company_attested('New York State Insurance Fund', documents=19))
        self.assertTrue(evidence.company_attested('The Limited, Inc.', documents=20))
        self.assertFalse(evidence.company_attested('The Limited, Inc.', documents=19))
        for value in ('PARTNERSHIP LLLP', "Commc'ns Inc.", 'U So. Co.'):
            self.assertFalse(evidence.company_attested(value, documents=100), value)
        evidence = LabelEvidence(legal_counts={full_key('Management Acquisition Limited'): 100})
        self.assertEqual(visibility_reason('Management Acquisition Limited', 'junk', evidence), 'reviewed_rejection')

    def test_ner_brands_need_independent_domain_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'corpus.db'
            with sqlite3.connect(path) as db:
                create(db, [('a', 1, 'See https://gulfstream.com/support. Marilia sent the note.')])
            references = {i: {'id': i, 'name': name, 'entity_type': 'organization', 'metadata': {'document_count': 100}}
                          for i, name in [('entity_g', 'Gulfstream'), ('entity_m', 'Marilia')]}
            evidence = LabelEvidence()
            evidence.prepare_reference_brands(references, path)
            self.assertEqual(evidence.domains, {'gulfstream.com'})


if __name__ == '__main__':
    unittest.main()
