import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import luna_review
import ocr_consensus as _ocr_consensus
_ocr_consensus.PATH = _ocr_consensus.PATH.with_name('nonexistent_ocr_consensus_for_tests.json')  # tests never read the live re-OCR output


class AgreedTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(luna_review, 'REVIEW', self.dir))
        for model, verdict in [('a', 'same'), ('b', 'same'), ('c', 'different')]:
            (self.dir / f'merges_{model}.json').write_text(json.dumps({'1|2': {'verdict': verdict, 'confidence': 'high'}}))

    def test_named_models_must_all_agree(self):
        self.assertEqual(set(luna_review.agreed('merges', 'verdict', ['a', 'b'])), {'1|2'})
        self.assertEqual(luna_review.agreed('merges', 'verdict', ['a', 'b', 'c']), {})
        self.assertEqual(luna_review.agreed('merges', 'verdict'), {})  # every answer file

    def test_duplicate_or_missing_model_is_refused(self):
        with self.assertRaises(SystemExit):
            luna_review.agreed('merges', 'verdict', ['a', 'a'])
        with self.assertRaises(SystemExit):
            luna_review.agreed('merges', 'verdict', ['a', 'b', 'typo'])


class CandidatePairTests(unittest.TestCase):
    def test_exact_cross_type_names_and_credentials_are_not_starved(self):
        named = {0: ('Zelda Hargrave', 'person'), 20: ('Zelda Hargrave', 'organization')}
        named.update({n: ('Zelda Hargrave ' + suffix, 'participant') for n, suffix in enumerate(
            ('MD', 'M.D.', 'PhD', 'CPA', 'Esq.', 'MD PhD', 'MBA', 'JD'), 1)})
        pairs = self.pairs(named)
        self.assertIn(frozenset((0, 20)), pairs)
        for n in range(1, 9):
            self.assertIn(frozenset((0, n)), pairs)

    def test_old_reviews_do_not_suppress_changed_labels(self):
        labels = {1: 'Zelda Hargrave', 'p': 'Hargrave Zelda'}
        signature = luna_review.review_label_signature('1|p', labels)
        self.assertTrue(luna_review.current_review('1|p', {'label_signature': signature}, labels))
        self.assertFalse(luna_review.current_review('1|p', {'verdict': 'different'}, labels))
        self.assertFalse(luna_review.current_review('1|p', {'label_signature': signature}, {**labels, 'p': 'Other Person'}))
        self.assertFalse(luna_review.current_review('1|missing', {}, labels))

    def pairs(self, named):
        nodes = {i: {'entity_type': kind} for i, (_, kind) in named.items()}
        labels = {i: label for i, (label, _) in named.items()}
        return {frozenset((a, b)): why for a, b, why in luna_review.candidate_pairs(nodes, labels, {})}

    def test_short_form_of_the_first_name(self):
        pairs = self.pairs({1: ('Rudolph Tanzi', 'participant'), 2: ('Rudy Tanzi', 'participant'),
                            3: ('Lesley Groff', 'person'), 4: ('leslie groff', 'participant'),
                            5: ('Mary Tanzi', 'participant')})
        self.assertIn(frozenset((1, 2)), pairs)
        self.assertIn(frozenset((3, 4)), pairs)
        self.assertNotIn(frozenset((1, 5)), pairs)

    def test_companies_do_not_pair_on_a_shared_last_word(self):
        pairs = self.pairs({1: ('Judah Investment Trust', 'organization'), 2: ('Jupiter Trust', 'organization')})
        self.assertNotIn(frozenset((1, 2)), pairs)

    def test_clear_name_pairs_survive_a_crowded_node_cap(self):
        named = {0: ('Zelda Hargrave', 'person'), 99: ('Zelda Hargraave', 'participant'),
                 100: ('Hargrave. Zelda', 'participant')}
        named.update({n: (f'Zelda Hargrave {n}', 'participant') for n in range(1, 10)})
        nodes = {i: {'entity_type': kind} for i, (_, kind) in named.items()}
        labels = {i: label for i, (label, _) in named.items()}
        pairs = luna_review.candidate_pairs(nodes, labels, {})
        self.assertEqual(pairs[0][2], 'same name words, different order or punctuation')
        reasons = {frozenset((a, b)): why for a, b, why in pairs}
        self.assertEqual(reasons[frozenset((0, 99))], 'same first name, one-edit surname')
        self.assertIn(frozenset((0, 100)), reasons)

    def test_structural_variants_are_not_capped_at_six(self):
        named = {n: ('Hargrave Zelda' if n % 2 else 'Zelda Hargrave', 'participant') for n in range(10)}
        self.assertEqual(len(self.pairs(named)), 45)

    def test_similarity_matrices_have_bounded_rows(self):
        original = luna_review.process.cdist
        sizes = []
        def bounded(a, b, **kwargs):
            sizes.append(len(a))
            return original(a, b, **kwargs)
        with patch.object(luna_review.process, 'cdist', side_effect=bounded):
            self.pairs({i: (f'Zelda Otherword{i}', 'participant') for i in range(130)})
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), 128)

    def test_quick_structural_report_skips_similarity_matrices(self):
        nodes = {1: {'entity_type': 'person'}, 2: {'entity_type': 'participant'}}
        with patch.object(luna_review.process, 'cdist', side_effect=AssertionError('matrix in quick report')):
            pairs = luna_review.candidate_pairs(nodes, {1: 'Zelda Hargrave', 2: 'Zelda Hargraave'}, {}, include_spelling=False)
        self.assertEqual(pairs, [(1, 2, 'same first name, one-edit surname')])


class FirstNameOwnerTests(unittest.TestCase):
    def owner(self, labels, shared, short='s', full='f'):
        """shared: {id: contacts in common with the short label}."""
        self.enterContext(patch.dict(luna_review.LABELS, labels, clear=True))
        nodes = {i: {'entity_type': 'participant'} for i in labels}
        neighbours = {i: Counter({f'{i}{n}': 1 for n in range(shared.get(i, 0))}) for i in labels}
        neighbours[short] = Counter({f'{i}{n}': 1 for i, k in shared.items() for n in range(k)})
        return luna_review.first_name_owner(short, full, nodes, neighbours, {})

    def test_nickname_target_wins_on_more_shared_contacts(self):
        self.assertTrue(self.owner({'s': 'Robert', 'f': 'Bob Wexfrey', 'r': 'Robert Trivers'}, {'f': 7, 'r': 2}))

    def test_one_shared_contact_is_enough_when_no_rival_has_any(self):
        self.assertTrue(self.owner({'s': 'Frank', 'f': 'Frank Velora', 'r': 'Frank Vamble'}, {'f': 1}))

    def test_own_spellings_are_not_rivals(self):
        self.assertTrue(self.owner({'s': 'Sonam', 'f': 'Sonam Vela', 'r': 'Sonam Ve'}, {'f': 2, 'r': 2}))

    def test_tie_or_no_evidence_blocks(self):
        self.assertFalse(self.owner({'s': 'Lauren', 'f': 'Lauren Cganke', 'r': 'Lauren Veyce'}, {'f': 1, 'r': 1}))
        self.assertFalse(self.owner({'s': 'Doug', 'f': 'Doug Varnelin', 'r': 'Doug Band'}, {'r': 3}))
        self.assertFalse(self.owner({'s': 'Frank', 'f': 'Frank Velora'}, {}))


class PairEvidenceTests(unittest.TestCase):
    def test_names_shared_contacts_and_first_name_rivals(self):
        labels = {'s': 'Gary', 'f': 'Gary Vellor', 'r': 'Gary Noren', 'e': 'Jeffrey Epstein', 'g': 'Lesley Groff'}
        self.enterContext(patch.dict(luna_review.LABELS, labels, clear=True))
        nodes = {i: {'entity_type': 'participant'} for i in labels}
        neighbours = {'s': Counter({'e': 5, 'g': 1}), 'f': Counter({'e': 2, 'g': 3}), 'r': Counter({'e': 9}),
                      'e': Counter(), 'g': Counter()}
        text = luna_review.pair_evidence('s', 'f', 'first-name-only header name', nodes, labels, neighbours, {})
        self.assertIn('2 shared links (Jeffrey Epstein, Lesley Groff)', text)
        self.assertIn('first-name evidence for the full name: 2; other people with this first name: 1 (best: Gary Noren 1)', text)
        text = luna_review.pair_evidence('f', 'r', 'similar spelling', nodes, labels, neighbours, {})
        self.assertNotIn('first-name', text)


if __name__ == '__main__':
    unittest.main()
