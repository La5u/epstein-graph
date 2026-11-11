"""Airport spellings resolve to one OurAirports identity, never by guessing."""
import csv
import tempfile
import unittest
from pathlib import Path

from airports import Airports

COLUMNS = ['id', 'ident', 'type', 'name', 'latitude_deg', 'longitude_deg', 'elevation_ft', 'continent', 'iso_country',
           'iso_region', 'municipality', 'scheduled_service', 'icao_code', 'iata_code', 'gps_code', 'local_code',
           'home_link', 'wikipedia_link', 'keywords']
ROWS = [
    dict(ident='KSFO', type='large_airport', name='San Francisco International Airport', iso_country='US', iso_region='US-CA',
         municipality='San Francisco', icao_code='KSFO', iata_code='SFO', gps_code='KSFO', local_code='SFO'),
    dict(ident='WSSS', type='large_airport', name='Singapore Changi Airport', iso_country='SG', iso_region='SG-04',
         municipality='Singapore', icao_code='WSSS', iata_code='SIN', gps_code='WSSS'),
    dict(ident='K27K', type='small_airport', name='Georgetown-Scott County Regional Airport', iso_country='US',
         iso_region='US-KY', municipality='Georgetown', gps_code='K27K', local_code='27K'),
    dict(ident='KPBI', type='large_airport', name='Renamed International Airport', iso_country='US', iso_region='US-FL',
         municipality='West Palm Beach', icao_code='KDJT', iata_code='DJT', gps_code='KDJT', local_code='DJT'),
    dict(ident='DNAA', type='large_airport', name='Nnamdi Azikiwe International Airport', iso_country='NG',
         iso_region='NG-FC', municipality='Abuja', icao_code='DNAA', iata_code='ABV', gps_code='DNAA'),
    dict(ident='XAAA', type='small_airport', name='Twin Field', iso_country='US', iso_region='US-TX', iata_code='TWN'),
    dict(ident='XBBB', type='small_airport', name='Twin Field', iso_country='US', iso_region='US-OK', iata_code='TWN'),
    dict(ident='XOLD', type='closed', name='Old Field', iso_country='US', iso_region='US-TX', iata_code='SIN'),
]


class AirportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        path = Path(cls.directory.name) / 'airports.csv'
        with path.open('w', newline='') as f:
            writer = csv.DictWriter(f, COLUMNS)
            writer.writeheader()
            for row in ROWS:
                writer.writerow({**dict.fromkeys(COLUMNS, ''), 'latitude_deg': '1', 'longitude_deg': '2', **row})
        cls.airports = Airports(path)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_code_name_and_city_spellings_meet(self):
        for spelling in ('SFO', 'SFO Airport', 'KSFO Airport', 'SFO International, CA', 'San Francisco International, CA',
                         'San Francisco Intl Airport, CA'):
            with self.subTest(spelling):
                self.assertEqual(self.airports.resolve(spelling)[0], 'KSFO')
        self.assertEqual(self.airports.resolve('WSSS Airport'), ('WSSS', 'code'))
        self.assertEqual(self.airports.resolve('27K Airport'), ('K27K', 'code'))  # US local code
        self.assertEqual(self.airports.resolve('PBI'), ('KPBI', 'code'))          # old code of a renamed field
        self.assertEqual(self.airports.resolve('Abuja, Nigeria'), ('DNAA', 'city'))

    def test_ambiguous_wrong_region_and_junk_stay_unresolved(self):
        self.assertEqual(self.airports.resolve('TWN'), (None, None))  # two open fields share it
        self.assertEqual(self.airports.resolve('Twin Field'), (None, None))
        self.assertEqual(self.airports.resolve('Twin Field, TX'), ('XAAA', 'name'))
        self.assertEqual(self.airports.resolve('San Francisco International, NY'), (None, None))
        for junk in ('SIMULATOR', 'Unknown', 'TEB, AHN'):
            self.assertEqual(self.airports.resolve(junk), (None, None))

    def test_open_field_wins_over_closed_code(self):
        self.assertEqual(self.airports.resolve('SIN'), ('WSSS', 'code'))

    def test_missing_reference_file_resolves_nothing(self):
        self.assertFalse(Airports(Path(self.directory.name) / 'missing.csv'))
        self.assertEqual(Airports(Path(self.directory.name) / 'missing.csv').resolve('SFO'), (None, None))


if __name__ == '__main__':
    unittest.main()
