import unittest
from unittest.mock import patch

import extract_places
from place_normalization import clean_place_node, clean_place_text, place_annotations, place_key, place_label, place_label_reason, aliases_for_place


class PlaceNormalizationTests(unittest.TestCase):
    def test_case_state_suffix_and_placeholder(self):
        self.assertEqual(place_key('WEST PALM BEACH FL'), place_key('West Palm Beach'))
        self.assertEqual(place_key('New York Na'), place_key('New York'))
        self.assertEqual(place_key('Albany, N.Y.'), place_key('Albany'))
        self.assertNotEqual(place_key('New York'), place_key('York'))
        self.assertEqual(place_label('12 Main Street NE'), '12 Main Street NE')
        self.assertEqual(place_label('12 Main Street, NY'), '12 Main Street, NY')
        for text in ('Avenue', 'NY', 'ST', 'N.A', 'n.y.', 'Road'):
            self.assertIsNotNone(place_label_reason(text), text)
            node = clean_place_node(dict(id='kept', name=text))
            self.assertEqual(node['id'], 'kept')
            self.assertEqual(node['label_quality'], 'not_a_name')
        for text in ('St Louis', 'New York', 'Avenue Foch', 'Indiana'):
            self.assertIsNone(place_label_reason(text))

    def test_annotations_live_in_metadata(self):
        text = '12 Main Street [signature: image001.png] [signature: Room 1-211]'
        self.assertEqual(clean_place_text(text), '12 Main Street')
        self.assertEqual(place_annotations(text), ['image001.png', 'Room 1-211'])
        self.assertEqual(clean_place_node(dict(id=1, name=text))['place_annotations'], place_annotations(text))

    def test_non_places_zip_codes_and_false_street_matches(self):
        for value in ('Client', 'cursor', 'Eastern Time', 'Eastern Standard Time', 'Refunds',
                      'MA 02109', 'CA 90017', '10007', '10007-1234', '00 Meet Dr',
                      '0000 Dear Dr', '807 Arrives St', '943 Departs St'):
            self.assertIsNotNone(place_label_reason(value), value)
            node = clean_place_node(dict(id='retained', name=value, entity_type='location'))
            self.assertEqual(node['label_quality'], 'not_a_name')
            self.assertEqual(node['id'], 'retained')
        for value in ('2013 Costa Del Mar Road', 'Downing Street', '12 Main Street', 'St. Thomas'):
            self.assertIsNone(place_label_reason(value), value)
        survivor = dict(id='protected', name='Client', entity_type='survivor')
        self.assertEqual(clean_place_node(survivor), survivor)

    def test_aliases_preserve_island_and_annex_distinctions(self):
        for canonical, alias in [('LaGuardia Airport', 'LGA Airport'),
                                 ('Miami International Airport', 'Miami International'),
                                 ('Cyril E. King Airport (St. Thomas)', 'Cyril E King'),
                                 ('Metropolitan Correctional Center (MCC New York)', 'MCC New York'),
                                 ('St. Thomas', 'St Thomas')]:
            self.assertIn(alias, aliases_for_place(canonical))
            self.assertIn(canonical, aliases_for_place(alias))
            self.assertIn(alias, clean_place_node(dict(id=1, name=canonical))['aliases'])
        self.assertNotIn('St Thomas', aliases_for_place('Cyril E King'))
        self.assertEqual(aliases_for_place('Metropolitan Correctional Center Annex'), [])

    def test_extractor_stops_emitting_bare_words_and_codes(self):
        class Entity:
            label_ = 'GPE'
            def __init__(self, text):
                self.text = text
        class Doc:
            ents = [Entity(s) for s in ('Avenue', 'NY', 'ST', 'N.A', 'Client', 'Eastern Time', 'MA 02109', 'Boston', 'St Louis')]
        class NLP:
            def pipe(self, texts, **kwargs):
                return [Doc() for _ in texts]
        class Corpus:
            def execute(self, *args):
                return [('Boston St Louis',)]
        with patch.multiple(extract_places, _nlp=NLP(), _corpus=Corpus()):
            _, rows = extract_places.extract(['doc'])
        self.assertEqual({r[2] for r in rows}, {'Boston', 'St Louis'})

class AddressFragmentTests(unittest.TestCase):
    def test_streetless_addresses_and_bare_units_are_not_places(self):
        from place_normalization import place_label_reason
        self.assertEqual(place_label_reason('13 The St'), 'address_without_street_name')
        self.assertEqual(place_label_reason('Suite 400'), 'unit_without_address')
        self.assertIsNone(place_label_reason('13 Main Street'))

