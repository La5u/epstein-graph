import json
import unittest

import public_names as pn
import survivor_privacy as sp


class PublicNamesTests(unittest.TestCase):
    verdicts = {'1': {'public': True, 'confidence': 'high'}, '2': {'public': False, 'confidence': 'high'},
                'participant_aaaaaaaaaaaaaaaaaaaaaaaa': {'public': True, 'confidence': 'medium'},
                'participant_bbbbbbbbbbbbbbbbbbbbbbbb': {'public': True, 'confidence': 'low'}}

    def test_private_person_gets_pseudonym_and_password_entry(self):
        nodes = [{'id': 1, 'name': 'Famous Banker', 'entity_type': 'person'},
                 {'id': 2, 'name': 'Quiet Neighbour', 'entity_type': 'person', 'original_names': ['Quiet Neighbour']},
                 {'id': 3, 'name': 'Org', 'entity_type': 'organization', 'note': 'met Quiet Neighbour'}]
        out, hidden, _ = pn.apply(nodes, self.verdicts, b'k', sp.scrubber)
        self.assertEqual(out[0]['name'], 'Famous Banker')
        self.assertTrue(out[1]['name'].startswith('Person '))
        self.assertEqual(hidden, {'2': {'value': 'Quiet Neighbour'}})
        self.assertNotIn('Quiet Neighbour', json.dumps(out))

    def test_upstream_public_flag_and_surname_of_public_figure_stay(self):
        nodes = [{'id': 5, 'name': 'Ann Banker', 'entity_type': 'person', 'metadata': json.dumps({'public_figure': True})},
                 {'id': 6, 'name': 'Banker', 'entity_type': 'person'}, {'id': 7, 'name': 'Black', 'entity_type': 'person'}]
        out, hidden, _ = pn.apply(nodes, {}, b'k', sp.scrubber)
        self.assertEqual([e['name'] for e in out[:2]], ['Ann Banker', 'Banker'])
        self.assertEqual(list(hidden), ['7'])

    def test_public_participants_are_named_only_with_confidence(self):
        nodes = [{'id': 'participant_aaaaaaaaaaaaaaaaaaaaaaaa', 'name': 'aaaaaaaaaaaaaaaaaaaaaaaa', 'entity_type': 'participant'},
                 {'id': 'participant_bbbbbbbbbbbbbbbbbbbbbbbb', 'name': 'bbbbbbbbbbbbbbbbbbbbbbbb', 'entity_type': 'participant'}]
        labels = {n['id']: 'Real ' + n['id'][-1] for n in nodes}
        self.assertEqual(pn.shown_names(nodes, labels, self.verdicts), {'participant_aaaaaaaaaaaaaaaaaaaaaaaa': 'Real a'})

    def test_without_review_nothing_changes(self):
        nodes = [{'id': 2, 'name': 'Quiet Neighbour', 'entity_type': 'person'}]
        self.assertEqual(pn.apply(nodes, None, b'k', sp.scrubber)[0], nodes)

    def test_same_name_as_a_verified_public_figure_is_named(self):
        verdicts = {'9': {'public': True, 'confidence': 'high', 'name': 'Famous Banker'}, '10': {'public': False, 'confidence': 'high', 'name': 'Famous Banker'}}
        out, hidden, _ = pn.apply([{'id': 10, 'name': 'Famous Banker', 'entity_type': 'person'}], verdicts, b'k', sp.scrubber)
        self.assertEqual((out[0]['name'], hidden), ('Famous Banker', {}))
        node = {'id': 'participant_cccccccccccccccccccccccc', 'name': 'cccccccccccccccccccccccc', 'entity_type': 'participant'}
        self.assertEqual(pn.shown_names([node], {node['id']: 'famous banker'}, verdicts), {node['id']: 'famous banker'})

    def test_private_participant_is_scrubbed_but_not_labels_or_parts_of_shown_names(self):
        verdicts = {'p1': {'public': False, 'confidence': 'high', 'role': 'bank staff', 'name': 'Quiet Clerk'},
                    'p2': {'public': False, 'confidence': 'high', 'role': 'airline, not a person', 'name': 'Air Example'},
                    'p3': {'public': False, 'confidence': 'high', 'name': 'Jean Luc'},
                    '1': {'public': True, 'confidence': 'high', 'name': 'Jean Luc Famous'}}
        nodes = [{'id': 1, 'name': 'Jean Luc Famous', 'entity_type': 'person'},
                 {'id': 'c', 'name': 'Quiet Clerk I Air Example LLP', 'entity_type': 'organization'}]
        out, hidden, _ = pn.apply(nodes, verdicts, b'k', sp.scrubber)
        self.assertEqual(out[0]['name'], 'Jean Luc Famous')
        self.assertEqual(out[1]['name'], f'{sp.WITHHELD} I Air Example LLP')
        self.assertEqual(hidden, {'c': {'value': 'Quiet Clerk I Air Example LLP'}})

    def test_withheld_overrides_the_upstream_flag(self):
        verdicts = {'5': {'public': False, 'confidence': 'medium', 'withhold': True, 'name': 'Rare Zzyzx'}}
        nodes = [{'id': 5, 'name': 'Rare Zzyzx', 'entity_type': 'person', 'metadata': {'public_figure': True}},
                 {'id': 6, 'name': 'Zzyzx', 'entity_type': 'person'}, {'id': 'o', 'name': 'Ms. Zzyzx Firm', 'entity_type': 'organization'}]
        out, hidden, _ = pn.apply(nodes, verdicts, b'k', sp.scrubber)
        self.assertTrue(out[0]['name'].startswith('Person '))
        self.assertTrue(out[1]['name'].startswith('Person '))
        self.assertEqual(hidden['5'], {'value': 'Rare Zzyzx'})
        self.assertIn(sp.WITHHELD, out[2]['name'])


if __name__ == '__main__':
    unittest.main()
