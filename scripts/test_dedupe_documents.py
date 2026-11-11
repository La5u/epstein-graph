import unittest
from itertools import combinations

import numpy as np

from corpus_index import cluster_pair_counts
from dedupe_documents import (DATES, K, SAMPLES, Features, bottom_dates, candidate_pairs, center_pairs, components,
                              containment_links, date_hashes, date_inside, drop_common, minhash,
                              normalize, shingle_hashes, similar)

LETTER = ('Dear Jane, the shipment of twelve crates left the warehouse on Tuesday morning and should reach the '
          'harbour office before the end of the week, please confirm the invoice and the customs forms with Roe')


def features(texts, dates=None):
    hashes = [shingle_hashes(normalize(t).encode().split()) for t in texts]
    sig = np.array([minhash(h)[0] for h in hashes])
    date_rows = np.zeros((len(texts), DATES), dtype=np.uint32)
    for i, text in enumerate(dates or []):
        found = date_hashes(text)
        date_rows[i, :len(found)] = found
    return Features(sig, np.array([len(h) for h in hashes]), np.zeros(len(texts), dtype=np.uint64), date_rows)


class NormalizeTests(unittest.TestCase):
    def test_production_stamps_removed(self):
        for stamp in ('EFTA00001623', 'EFTA_R1_00912174', 'E FTA_R1_00912174', 'SDNY_GM_00012345', 'SONY GM_00012345',
                      'DB-SDNY-0012345', 'JPM-SDNY-00012345', 'DOJ-OGR-00012345', 'HOUSE_OVERSIGHT_012345'):
            self.assertEqual(normalize(f'Re: lunch {stamp}'), 're lunch', stamp)

    def test_other_numbers_kept(self):
        self.assertEqual(normalize('Call 561 655 1234, account 12345678'), 'call 561 655 1234 account 12345678')

    def test_copies_with_different_stamps_hash_alike(self):
        a = shingle_hashes(normalize(LETTER + ' EFTA00000001').encode().split())
        b = shingle_hashes(normalize(LETTER + ' EFTA_R1_00000999').encode().split())
        self.assertTrue(np.array_equal(a, b))

    def test_drop_common(self):
        hashes = np.array([1, 5, 9, 12], dtype=np.uint64)
        self.assertEqual(drop_common(hashes, np.array([5, 12], dtype=np.uint64)).tolist(), [1, 9])
        self.assertEqual(drop_common(hashes, np.empty(0, dtype=np.uint64)).tolist(), [1, 5, 9, 12])


class DateTests(unittest.TestCase):
    def test_formats_agree(self):
        self.assertEqual(date_hashes('11/1/2013'), date_hashes('Nov 1, 2013'))
        self.assertEqual(date_hashes('Nov 1, 2013'), date_hashes('1 November 2013'))
        self.assertEqual(date_hashes('13/45/2013 and 0/3/12'), [])

    def test_bottom_dates_keeps_smallest_distinct(self):
        out = bottom_dates(3, np.array([0, 0, 2, 0]), np.array([5, 3, 7, 5], dtype=np.uint32))
        self.assertEqual(out[0, :3].tolist(), [3, 5, 0])
        self.assertEqual(out[1].tolist(), [0] * DATES)
        self.assertEqual(out[2, 0], 7)


class SimilarityTests(unittest.TestCase):
    def test_ocr_variant_matches(self):
        f = features([LETTER, LETTER.replace('warehouse', 'warehousc')])
        self.assertTrue(similar(f, np.array([0]), np.array([1]), 0.6)[0])

    def test_short_email_inside_long_thread_is_not_a_copy(self):
        thread = ' '.join(f'reply {i} about item {i * 7} sent from office {i * 3}' for i in range(30))
        f = features([LETTER, thread + ' ' + LETTER])
        self.assertFalse(similar(f, np.array([0]), np.array([1]), 0.6)[0])

    def test_same_template_different_dates_is_not_a_copy(self):
        texts = [LETTER, LETTER]
        f = features(texts, dates=['For the Period 9/1/12 to 9/28/12', 'For the Period 11/1/13 to 11/29/13'])
        self.assertFalse(similar(f, np.array([0]), np.array([1]), 0.8)[0])
        f = features(texts, dates=['On Jul 30, 2015 wrote', 'Sent 7/30/2015; On Jul 29, 2015 wrote'])
        self.assertTrue(similar(f, np.array([0]), np.array([1]), 0.8)[0])

    def test_center_clustering_breaks_chains(self):
        # Three fake signatures: 0~1 and 1~2 pass 0.8, 0~2 does not.
        sig = np.zeros((3, K), dtype=np.uint32)
        sig[1, :8] = 1
        sig[2, :8] = 1
        sig[2, 8:16] = 2
        f = Features(sig, np.array([100, 99, 98]), np.zeros(3, dtype=np.uint64), np.zeros((3, DATES), dtype=np.uint32))
        members, centers = center_pairs(f, np.array([0, 1]), np.array([1, 2]), 3, 0.8)
        self.assertEqual(dict(zip(members.tolist(), centers.tolist())), {1: 0})  # 2 starts its own cluster


    def test_bucket_members_unmatched_by_the_first_anchor_still_pair(self):
        # Same signatures; dates split the bucket into {0} and {1, 2, 3}: 1-2, 1-3 must still be found.
        n = 4
        sig = np.zeros((n, K), dtype=np.uint32)
        dates = np.zeros((n, DATES), dtype=np.uint32)
        dates[:, 0] = [11, 22, 22, 22]
        f = Features(sig, np.full(n, 50), np.zeros(n, dtype=np.uint64), dates)
        a, b = candidate_pairs(f, 0.8)
        self.assertEqual({tuple(sorted(p)) for p in zip(a.tolist(), b.tolist())}, {(1, 2), (1, 3)})



class SupersetTests(unittest.TestCase):
    def test_minhash_samples_are_members(self):
        hashes = shingle_hashes(normalize(LETTER).encode().split())
        sig, sample = minhash(hashes)
        self.assertTrue(set(sample.tolist()) <= set(hashes.tolist()))
        self.assertEqual(minhash(np.empty(0, dtype=np.uint64))[1].tolist(), [0] * SAMPLES)

    def test_date_inside(self):
        none, five = np.zeros((1, DATES), np.uint32), np.zeros((1, DATES), np.uint32)
        five[0, :2] = [5, 9]
        other = np.zeros((1, DATES), np.uint32)
        other[0, 0] = 7
        self.assertTrue(date_inside(none, five)[0])
        self.assertTrue(date_inside(five, five)[0])
        self.assertFalse(date_inside(other, five)[0])  # five's dates are all known and 7 is not one
        full = np.arange(1, DATES + 1, dtype=np.uint32)[None, :]
        late = np.zeros((1, DATES), np.uint32)
        late[0, 0] = 99
        self.assertTrue(date_inside(late, full)[0])  # beyond the sample of a document with many dates

    @staticmethod
    def corpus(contains, shingles):
        """Documents i with SAMPLES samples each (i * 100 + k); contains[j] = documents whose samples j holds."""
        n = len(shingles)
        sample = np.array([[i * 100 + k + 1 for k in range(SAMPLES)] for i in range(n)], dtype=np.uint64)
        docs, hashes = [], []
        for j in range(n):
            for i in {j} | set(contains.get(j, ())):
                docs += [j] * SAMPLES
                hashes += sample[i].tolist()
        return Features(np.zeros((n, K), np.uint32), np.array(shingles), np.zeros(n, np.uint64),
                        np.zeros((n, DATES), np.uint32), sample, np.array(docs), np.array(hashes, dtype=np.uint64))

    def test_subset_joins_the_largest_top_level_container(self):
        # 0 is inside 1 and 2; 1 is inside 3. 0 must join 2 (1 is not top level).
        f = self.corpus({1: [0], 2: [0], 3: [1]}, [30, 100, 200, 300])
        labels, canonical = components(4, np.empty(0, np.int64), np.empty(0, np.int64), f.shingles)
        a, b = containment_links(f, labels, canonical)
        self.assertEqual(dict(zip(a.tolist(), b.tolist())), {0: 2, 1: 3})

    def test_small_or_partial_overlap_is_not_contained(self):
        f = self.corpus({1: [0]}, [10, 100])  # 0 is below MIN_CONTAINED
        labels, canonical = components(2, np.empty(0, np.int64), np.empty(0, np.int64), f.shingles)
        self.assertEqual(len(containment_links(f, labels, canonical)[0]), 0)
        f = self.corpus({}, [30, 100])
        f.post_docs = np.r_[f.post_docs, [1] * 8]  # 1 holds half of 0's samples
        f.post_hashes = np.r_[f.post_hashes, f.sample[0, :8]]
        labels, canonical = components(2, np.empty(0, np.int64), np.empty(0, np.int64), f.shingles)
        self.assertEqual(len(containment_links(f, labels, canonical)[0]), 0)


class ClusterPairCountTests(unittest.TestCase):
    def test_copies_count_once_with_best_copy(self):
        doc_members = {'d1': [1, 2], 'd1copy': [1, 2, 3], 'd2': [1, 2], 'd3': [2, 3]}
        cluster = {'d1': 'c1', 'd1copy': 'c1', 'd2': 'c2', 'd3': 'c3'}
        pairs = lambda members: combinations(sorted(members), 2)
        shared, weighted, samples = cluster_pair_counts(doc_members, cluster.get, pairs, max_members=10)
        self.assertEqual(shared[1, 2], 2)           # c1 and c2, not three documents
        self.assertEqual(weighted[1, 2], 2.0)       # weight from the 2-entity copy d1
        self.assertEqual(sorted(samples[1, 2]), [(2, 'd1'), (2, 'd2')])
        self.assertEqual(shared[2, 3], 2)           # d1copy (c1) and d3 (c3)
        self.assertEqual(shared[1, 3], 1)

    def test_identity_clusters_match_plain_counting(self):
        doc_members = {0: [1, 2, 3], 1: [1, 2]}
        shared, weighted, _ = cluster_pair_counts(doc_members, lambda d: d, lambda m: combinations(m, 2), max_members=2)
        self.assertEqual(shared, {(1, 2): 2, (1, 3): 1, (2, 3): 1})
        self.assertEqual(weighted, {(1, 2): 1.0})   # the 3-entity document is above max_members


if __name__ == '__main__':
    unittest.main()
