import unittest

import networkx as nx
import numpy as np

import fastlayout


class FastLayoutTest(unittest.TestCase):
    def test_layout_like_networkx(self):
        graph = nx.connected_caveman_graph(6, 8)  # six tight cliques joined in a ring
        positions = fastlayout.spring_layout(graph, seed=42, iterations=150, k=3 / np.sqrt(len(graph)))
        self.assertIsNotNone(positions, 'needs a C compiler')
        self.assertEqual(set(positions), set(graph))
        coords = np.array(list(positions.values()))
        self.assertAlmostEqual(np.abs(coords).max(), 1.0)  # rescaled like networkx
        np.testing.assert_allclose(coords.mean(axis=0), 0, atol=1e-9)
        def mean_distance(pairs):
            return np.mean([np.linalg.norm(positions[a] - positions[b]) for a, b in pairs])
        same = [(a, b) for a, b in graph.edges() if a // 8 == b // 8]
        other = [(a, b) for a in range(0, 48, 8) for b in range(0, 48, 8) if a < b]
        self.assertLess(mean_distance(same) * 2, mean_distance(other))  # cliques stay together
        again = fastlayout.spring_layout(graph, seed=42, iterations=150, k=3 / np.sqrt(len(graph)), threads=1)
        np.testing.assert_allclose(coords, np.array(list(again.values())))  # seeded and thread-count independent


if __name__ == '__main__':
    unittest.main()
