import unittest

from node_cleanup import base_name, fold_own_websites, numbered, numbered_families, squeeze

VOCABULARY = {'model_makers': {squeeze('Bell'): {'entity_type': 'vehicle', 'vehicle_type': 'aircraft'}},
              'aliases': {squeeze('Jet Blue'): 'JetBlue Airways', squeeze('Air France'): 'Air France'},
              'series_words': {'trust', 'fund'},
              'words': frozenset({'bill', 'point', 'june', 'laura', 'virginia', 'phillips'})}


def org(identity, name, **kw):
    return {'id': identity, 'name': name, 'entity_type': 'organization', **kw}


class NumberedLabelTests(unittest.TestCase):
    def test_parsing(self):
        for label, stem in [('Jet Blue #65', 'Jet Blue'), ("Rule 45's", 'Rule'), ('Bell 407/430', 'Bell'),
                            ('KATHERINE 48,944.00', 'KATHERINE'), ('mark248236', 'mark'), ('Point - 15', 'Point')]:
            self.assertEqual(numbered(label)[0], stem, label)
        for label in ('JetBlue Airways Corporation', '767 N.Y.S.2d 228', 'Bill Gates', '3M'):
            self.assertIsNone(numbered(label), label)
        self.assertEqual(base_name('The Delta Air Lines, Inc.'), 'deltaairlines')

    def test_families(self):
        entities = [org('jb', 'JetBlue Airways Corporation'), org(1, 'Jet Blue #65'), org(2, 'JetBlue 704'),
                    org(3, 'AIR FRANCE 1047'), org(4, 'Air France 22'), org(5, 'Bell 407 and'), org(6, 'Bell 407'), org(7, 'Bell 429'),
                    org(8, 'Bill 7'), org(9, 'Laura 1'), org(10, 'Acme Widgets 5'), org(11, 'Acme Widgets 9'),
                    org(12, 'Zork 1'), org(13, 'Zork 2'), org(14, 'Zork 3'), org(15, 'Caterpillar Trust 2'),
                    org(16, 'Caterpillar Trust 3'), org(17, 'Caterpillar Trust 4'), org(18, 'Phillips 66'), org(19, 'Phillips 66 Company'),
                    org(20, 'Quux 12,500.00'), org('aw', 'Acme Widgets, Inc.'), org('junk', 'Bi l l'), org(21, 'Bi ll 5')]
        resolutions = {}
        out = {e['id']: e for e in numbered_families(entities, VOCABULARY, resolutions)}
        self.assertEqual(resolutions[1], ('jb', 'numbered_reference_of_organization'))
        self.assertEqual(resolutions[2][0], 'jb')
        self.assertEqual({out[3]['name'], out[4]['name']} & {'Air France'}, {'Air France'})
        self.assertEqual((out[7]['entity_type'], out[7]['vehicle_type'], out[7]['name']), ('vehicle', 'aircraft', 'Bell 429'))
        self.assertEqual(resolutions[10], ('aw', 'numbered_reference_of_organization'))
        for hidden in (8, 9, 12, 13, 14, 20):  # ordinary word/name stem, a 3-number family, an amount
            self.assertEqual(out[hidden].get('label_quality'), 'not_a_name', out[hidden]['name'])
        for kept in (15, 16, 17, 18):  # a numbered series of trusts; a company named with a number
            self.assertFalse(out[kept].get('label_quality'), out[kept]['name'])
            self.assertNotIn(kept, resolutions)
        self.assertNotEqual(resolutions.get(21, (None,))[0], 'junk')

    def test_own_website(self):
        nodes = [org('o', 'White & Case LLP'), {'id': 'w', 'name': 'whitecase.com', 'entity_type': 'website'}]
        entities, edges, fold = fold_own_websites(nodes, [{'source_entity_id': 'w', 'target_entity_id': 'o', 'relationship_type': 'website_of'}])
        self.assertEqual((fold, entities[0]['websites'], edges), ({'w': 'o'}, ['whitecase.com'], []))


if __name__ == '__main__':
    unittest.main()
