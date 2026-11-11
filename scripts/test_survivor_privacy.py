import json
import tempfile
import unittest
from pathlib import Path

import survivor_privacy as sp


class SurvivorPrivacyTests(unittest.TestCase):
    def run_protect(self, entities, relationships=()):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'victims.json'
            config.write_text(json.dumps({'names': ['Anna Quistrel']}))
            survivors = Path(tmp) / 'survivors.json'
            survivors.write_text(json.dumps({'names': ['Carol Ward']}))
            return sp.protect(list(entities), list(relationships), b'k', config=config, survivor_config=survivors,
                              registry=Path(tmp) / 'absent', words=frozenset({'ward'}))

    def test_named_survivor_keeps_node_under_pseudonym(self):
        people = [{'id': 'victim_anna_quistrel', 'name': 'Anna Quistrel', 'entity_type': 'survivor', 'note': 'Anna Quistrel said'},
                  {'id': 7, 'name': 'Other', 'entity_type': 'person', 'note': 'Quistrel v. Other; Carol Ward'}]
        edges = [{'id': 1, 'source_entity_id': 'victim_anna_quistrel', 'target_entity_id': 7, 'relationship_type': 'x'}]
        entities, relationships, reveal, _, _ = self.run_protect(people, edges)
        self.assertEqual(len(entities), 2)
        text = json.dumps([entities, relationships])
        self.assertNotIn('Quistrel', text)
        self.assertNotIn('quistrel', text)
        self.assertNotIn('Carol Ward', text)
        new_id = entities[0]['id']
        self.assertEqual(relationships[0]['source_entity_id'], new_id)
        self.assertEqual(reveal[new_id]['value'], 'Anna Quistrel')
        self.assertTrue(entities[0]['name'].startswith('Survivor '))

    def test_common_surname_of_someone_else_is_kept(self):
        entities, *_ = self.run_protect([{'id': 7, 'name': 'Bob Ward', 'entity_type': 'person'}])
        self.assertEqual(entities[0]['name'], 'Bob Ward')

    def test_pseudonymous_designator_is_left_alone(self):
        entities, _, reveal, _, _ = self.run_protect([{'id': 'victim_jane_doe_2', 'name': 'Jane Doe #2', 'entity_type': 'survivor'}])
        self.assertEqual((entities[0]['id'], reveal), ('victim_jane_doe_2', {}))

    def test_public_record_names_are_scrubbed_in_full_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'c.json'
            config.write_text(json.dumps({'names': []}))
            extra = Path(tmp) / 'extra.json'
            extra.write_text(json.dumps({'people': [{'name': 'Ava Arden'}]}))
            nodes = [{'id': 1, 'name': 'Ava Arden', 'entity_type': 'organization'}, {'id': 2, 'name': 'Elizabeth Arden', 'entity_type': 'organization'}]
            out, *_ = sp.protect(nodes, [], b'k', config=config, survivor_config=config, registry=Path(tmp) / 'a', words=frozenset(), public_record=extra)
        self.assertEqual([e['name'] for e in out], [sp.WITHHELD, 'Elizabeth Arden'])

    def test_contacts(self):
        scrub = sp.scrubber(['Quistrel'])
        contacts = {'email_a': 'x@y.z', 'email_b': 'anna.quistrel@y.z', 'email_c': 'c@y.z'}
        self.assertEqual(sp.public_contacts(contacts, scrub, {'email_c'}), {'email_a': 'x@y.z'})

    def test_mailbox_spelling_a_name_is_not_public(self):
        forms = sp.mailbox_forms(['Anna Quistrel'])
        contacts = {'email_a': 'anna.quistrel@y.z', 'email_b': 'aquistrel@y.z', 'email_c': 'other@y.z'}
        self.assertEqual(sp.public_contacts(contacts, sp.scrubber([]), set(), forms), {'email_c': 'other@y.z'})


if __name__ == '__main__':
    unittest.main()
