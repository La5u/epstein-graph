"""Resolve flight-log airport spellings to one airport via OurAirports.

Reference: data/external/ourairports/airports.csv
(https://davidmegginson.github.io/ourairports-data/airports.csv, public domain).
Flight logs mix IATA codes ("SFO"), ICAO codes ("WSSS"), FAA local codes
("27K") and written names ("San Francisco International, CA"). Rules, in order,
each requiring exactly one airport:
  code      the whole endpoint, or its leading token, is a code
            (ICAO/GPS/ident, then IATA, then a US local code, then K+code for
            a US ident; closed fields only when nothing open has the code)
  name      written name equals the OurAirports name (ignoring "Airport",
            "Intl"), narrowed by a trailing state or country when given
  city      "Abuja, Nigeria", "Columbus, OH": the one large airport serving
            that city in that country/state
Reviewed spellings (REVIEWED) are checked first.
Anything else (multi-code fields like "TEB, AHN", "SIMULATOR") is unresolved.
"""
import csv
import math
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data/external/ourairports/airports.csv'
AIRFIELDS = {'large_airport', 'medium_airport', 'small_airport', 'seaplane_base', 'heliport', 'closed'}
COUNTRIES = {'usvi': 'VI', 'uk': 'GB', 'france': 'FR', 'canada': 'CA', 'spain': 'ES', 'italy': 'IT', 'portugal': 'PT',
             'morocco': 'MA', 'nigeria': 'NG', 'ghana': 'GH', 'rwanda': 'RW', 'mozambique': 'MZ', 'south africa': 'ZA',
             'st maarten': 'SX'}


# Reviewed log spellings whose official name or code differs (renamed fields,
# retired ICAO codes FAJS -> FAOR and WRRR -> WADD). Keys are name_key forms.
REVIEWED = {
    'dulles international': 'KIAD', 'aspen pitkin county': 'KASE', 'madrid barajas': 'LEMD',
    'montreal trudeau international': 'CYUL', 'baltimore washington international': 'KBWI',
    'tf green international': 'KPVD', 'dayton international': 'KDAY', 'rochester international': 'KROC',
    'barnstable municipal': 'KHYA', 'ciampino': 'LIRA', 'johannesburg': 'FAOR', 'fajs': 'FAOR',
    'wrrr': 'WADD', 'santa monica': 'KSMO', 'boston logan international': 'KBOS',
}


def name_key(text):
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()  # Côte -> Cote
    text = re.sub(r'\bairport\b', ' ', text.casefold().replace('.', ''))
    text = re.sub(r'\bintl\b', 'international', text)
    return ' '.join(re.findall(r"[a-z0-9]+", text.replace("'", '')))


def distance_km(a, b):
    (lat1, lng1), (lat2, lng2) = (map(math.radians, p) for p in (a, b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


class Airports:
    def __init__(self, path=SOURCE):
        self.rows = {}
        self.codes = {kind: defaultdict(set) for kind in ('icao', 'iata', 'local', 'us_ident')}
        self.names, self.cities = defaultdict(set), defaultdict(set)
        if not Path(path).exists():
            return
        with open(path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                if row['type'] not in AIRFIELDS:
                    continue
                ident = row['ident']
                self.rows[ident] = row
                for column in ('ident', 'icao_code', 'gps_code'):
                    if len(row[column]) == 4:
                        self.codes['icao'][row[column]].add(ident)
                if row['iata_code']:
                    self.codes['iata'][row['iata_code']].add(ident)
                if row['local_code'] and row['iso_country'] == 'US':
                    self.codes['local'][row['local_code']].add(ident)
                # US ICAO idents are K + the FAA code; keeps old codes of renamed
                # fields reachable (PBI -> KPBI after the 2026 DJT re-coding).
                if row['iso_country'] == 'US' and re.fullmatch(r'K[A-Z0-9]{3}', ident):
                    self.codes['us_ident'][ident[1:]].add(ident)
                if row['type'] != 'closed':
                    self.names[name_key(row['name'])].add(ident)
                if row['type'] == 'large_airport' and row['municipality']:
                    for area in (row['iso_country'], row['iso_region']):
                        self.cities[(row['municipality'].casefold(), area)].add(ident)

    def __bool__(self):
        return bool(self.rows)

    def by_code(self, code):
        for kind in (('icao',) if len(code) == 4 else ()) + ('iata', 'local', 'us_ident'):
            found = self.codes[kind].get(code, set())
            open_ = {i for i in found if self.rows[i]['type'] != 'closed'}
            found = open_ or found
            if len(found) == 1:
                return next(iter(found))
            if found:
                return None  # ambiguous: never guess between airports
        return None

    def resolve(self, endpoint):
        """(ident, rule) or (None, None)."""
        text = re.sub(r'\s+Airport\b', '', endpoint.strip())
        head, _, tail = text.partition(',')
        reviewed = REVIEWED.get(name_key(head))
        if reviewed in self.rows:
            return reviewed, 'reviewed'
        if re.fullmatch(r'[A-Z0-9]{3,4}', head.strip()) and not tail.strip():
            ident = self.by_code(head.strip())
            return (ident, 'code') if ident else (None, None)
        lead = re.match(r'^([A-Z]{3,4}) [A-Z][a-z]', head)
        if lead and (ident := self.by_code(lead[1])):
            return ident, 'code'
        where = tail.strip().split(',')[-1].strip() if tail.strip() else ''
        region = (f'US-{where}' if re.fullmatch(r'[A-Z]{2}', where) and where not in ('UK',)
                  else COUNTRIES.get(where.casefold().replace('.', '')))
        found = self.names.get(name_key(head), set())
        if region:
            found = {i for i in found if region in (self.rows[i]['iso_region'], self.rows[i]['iso_country'])}
        if len(found) == 1:
            return next(iter(found)), 'name'
        if region:
            found = self.cities.get((head.strip().casefold(), region), set())
            if len(found) == 1:
                return next(iter(found)), 'city'
        return None, None

    def position(self, ident):
        row = self.rows[ident]
        return float(row['latitude_deg']), float(row['longitude_deg'])
