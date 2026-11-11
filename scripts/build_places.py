"""Places: curated locations and flight-log airports, linked to the people who flew there.

    .venv/bin/python scripts/build_places.py

Inputs (data/external/epsteinexposed/, from https://epsteinexposed.com/api/v2/export,
"Public Interest - Attribution Requested"):
  locations.json  curated places (properties, airports, institutions, hotels...)
  flights.json    flight logs: date, origin, destination, passenger names
Writes output/place_nodes.json, output/place_edges.json (merged into the
viewer by build_contacts) and output/flight_index.json. A curated place that already exists in the graph
(normalized_entities.json) reuses that node. Flight endpoints are folded onto
one airport per code/name (scripts/airports.py, OurAirports
reference data); passengers are matched to graph people with the
header-name matcher. Person-place edges need >= MIN_FLIGHTS flights.
Ownership, residence and use, which the source states only in prose, come
from reviewed links in config/place_links.json, each quoting its basis.
"""
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from airports import Airports, distance_km
from build_communications import PersonMatcher
from normalize_graph import name_key
from place_normalization import place_key, clean_place_node, aliases_for_place

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data/external/epsteinexposed'
MIN_FLIGHTS = 2
ATTRIBUTION = 'Epstein Exposed (epsteinexposed.com) export; Public Interest - Attribution Requested'
CURATED_AIRPORT_KM = 5  # a curated airport this close to the resolved one is the same field
# Flight-log spellings of one airport -> the curated location's name (or a canonical label).
AIRPORT_ALIASES = {
    'teterboro': 'Teterboro Airport', 'teb': 'Teterboro Airport',
    'palm beach international': 'Palm Beach International Airport', 'pbi': 'Palm Beach International Airport',
    'tist': 'Cyril E. King Airport (St. Thomas)', 'stt': 'Cyril E. King Airport (St. Thomas)', 'cyril e king': 'Cyril E. King Airport (St. Thomas)',
    'saf': 'Santa Fe Municipal Airport', 'santa fe municipal': 'Santa Fe Municipal Airport',
    'john glenn columbus international': 'Port Columbus International Airport', 'cmh': 'Port Columbus International Airport',
    'le bourget': 'Paris Le Bourget Airport', 'lfpb': 'Paris Le Bourget Airport',
    'laurence g hanscom field': 'Hanscom Field (Bedford, MA)', 'hanscom field': 'Hanscom Field (Bedford, MA)', 'bed': 'Hanscom Field (Bedford, MA)',
    'ewr': 'Newark Liberty International Airport', 'newark liberty international': 'Newark Liberty International Airport',
    'london luton': 'London Luton Airport', 'ltn': 'London Luton Airport',
    'john f kennedy international': 'John F. Kennedy International Airport', 'jfk': 'John F. Kennedy International Airport',
    'albuquerque international sunport': 'Albuquerque International Sunport', 'abq': 'Albuquerque International Sunport',
    'little st james island': 'Little St. James Island',
    'lga': 'LaGuardia Airport', 'la guardia': 'LaGuardia Airport', 'laguardia': 'LaGuardia Airport',
    'mia': 'Miami International Airport', 'miami international': 'Miami International Airport',
}


def endpoint_key(text):
    """'Teterboro Airport, NJ' -> 'teterboro'; 'TIST Airport' -> 'tist'."""
    text = re.sub(r',\s*[A-Z]{2}$|,\s*(?:USVI|UK|France|NM|FL|NY|NJ|MA|OH|DC|CA)\b.*$', '', text.strip())
    text = re.sub(r'\b(?:airport|intl)\b', '', text, flags=re.I)
    return ' '.join(re.findall(r'[a-z0-9]+', text.casefold().replace('.', '')))


def main():
    out = ROOT / 'output'
    locations = json.loads((SOURCE / 'locations.json').read_text())['data']
    flights = json.loads((SOURCE / 'flights.json').read_text())['data']
    entities = json.loads((out / 'normalized_entities.json').read_text())
    graph_places = defaultdict(set)
    for e in entities:
        if e['entity_type'] in ('location', 'property'):
            for label in [e['name'], *aliases_for_place(e['name'])]:
                graph_places[place_key(label)].add(e['id'])

    nodes, place_of, slug_of = {}, {}, {}
    for loc in locations:
        own = place_key(loc['name'])
        matches = graph_places.get(own, set()) or {i for key, ids in graph_places.items() for i in ids
            if own and key and (key in place_key(loc.get('address') or '') or re.match(re.escape(own) + r'\b', key))}
        existing = next(iter(matches)) if len(matches) == 1 else None
        node_id = existing if existing is not None else f"place_{loc['slug']}"
        place_of[name_key(loc['name'])] = node_id
        slug_of[loc['slug']] = node_id
        if existing is None:
            nodes[node_id] = {'id': node_id, 'name': loc['name'], 'entity_type': 'property' if loc['type'] == 'property' else 'location',
                              'place_type': loc['type'], 'address': loc.get('address'), 'lat': loc.get('lat'), 'lng': loc.get('lng'),
                              'description': loc.get('description'), 'source': ATTRIBUTION}
        for label in [loc['name'], *aliases_for_place(loc['name'])]:
            graph_places[place_key(label)].add(node_id)

    airports = Airports()
    curated_airports = [(place_of[name_key(loc['name'])], (loc['lat'], loc['lng'])) for loc in locations
                        if loc['type'] == 'airport' and loc.get('lat') is not None and loc.get('lng') is not None]

    def airport(endpoint):
        key = endpoint_key(endpoint)
        name = AIRPORT_ALIASES.get(key) or next((v for k, v in AIRPORT_ALIASES.items() if len(k) > 3 and k in key), None)
        if name and name_key(name) in place_of:
            return place_of[name_key(name)]
        # One node per OurAirports airport, whichever code or name the log used.
        ident, rule = airports.resolve(name or endpoint)
        if ident:
            here = airports.position(ident)
            near = [node_id for node_id, at in curated_airports if distance_km(here, at) <= CURATED_AIRPORT_KM]
            node_id = near[0] if len(near) == 1 else 'place_airport_' + ident.casefold()
            if near and node_id not in nodes:
                return node_id  # curated place that reuses an existing graph node
            if node_id not in nodes:
                row = airports.rows[ident]
                nodes[node_id] = {'id': node_id, 'name': row['name'], 'entity_type': 'location', 'place_type': 'airport',
                                  'icao': ident, 'iata': row['iata_code'] or None, 'municipality': row['municipality'] or None,
                                  'lat': here[0], 'lng': here[1], 'flight_log_spellings': [],
                                  'source': ATTRIBUTION + '; airport identity from OurAirports (public domain)'}
            node = nodes[node_id]
            node.setdefault('flight_log_spellings', [])
            if endpoint not in node['flight_log_spellings']:
                node['flight_log_spellings'].append(endpoint)
            node.setdefault('airport_match_rules', {})[endpoint] = rule
            return node_id
        label = name or endpoint.strip()
        node_id = 'place_airport_' + re.sub(r'[^a-z0-9]+', '_', (name or key).casefold()).strip('_')
        nodes.setdefault(node_id, {'id': node_id, 'name': label, 'entity_type': 'location', 'place_type': 'airport',
                                   'flight_log_spellings': [], 'source': ATTRIBUTION})
        if endpoint not in nodes[node_id]['flight_log_spellings']:
            nodes[node_id]['flight_log_spellings'].append(endpoint)
        return node_id

    matcher = PersonMatcher(entities)
    people = {name_key(e['name']): e['id'] for e in entities if e['entity_type'] == 'person' and not e.get('suspected_parse_artifact')}
    visits = defaultdict(list)
    unmatched = Counter()
    flight_index = {}
    for flight in flights:
        ends = {airport(flight[k]) for k in ('origin', 'destination') if flight.get(k)}
        aboard = set()
        for passenger in flight['passenger_names']:
            person = people.get(name_key(passenger)) or matcher.match(passenger)[0]
            if person is None:
                unmatched[passenger] += 1
            else:
                aboard.add(person)
        flight_index[flight['id']] = {'date': flight.get('date'), 'aircraft': flight.get('aircraft'),
                                      'origin': flight.get('origin'), 'destination': flight.get('destination'),
                                      'people': sorted(aboard, key=str), 'places': sorted(ends, key=str)}
        for person in aboard:
            for place in ends:
                visits[(person, place)].append((flight['date'], flight['id']))
    edges = []
    for (person, place), trips in sorted(visits.items(), key=lambda item: str(item[0])):
        if len(trips) < MIN_FLIGHTS:
            continue
        dates = sorted(d for d, _ in trips if d)
        edges.append({'id': f'flew_{person}_{place}', 'source_entity_id': person, 'target_entity_id': place,
                      'relationship_type': 'flew_to_or_from', 'weight': len(trips),
                      'date_first': dates[0] if dates else None, 'date_last': dates[-1] if dates else None,
                      'metadata': {'record_count': len(trips), 'flight_ids': [f for _, f in trips][:50], 'source': ATTRIBUTION,
                                   'meaning': 'Passenger on flight-log legs departing or arriving at this place; logs can be incomplete or misspelled.'}})
    edges += reviewed_links(slug_of, entities, matcher)
    place_nodes = prepared_place_nodes(nodes.values(), entities)
    (out / 'place_nodes.json').write_text(json.dumps(place_nodes, indent=2, ensure_ascii=False) + '\n')
    (out / 'place_edges.json').write_text(json.dumps(edges, indent=2) + '\n')
    # Matched passengers and airports per flight, for the document index (build_documents.py).
    (out / 'flight_index.json').write_text(json.dumps(flight_index) + '\n')
    print(json.dumps({'place_nodes': len(place_nodes), 'reused_graph_places': sorted({v for v in place_of.values() if isinstance(v, int)}),
                      'person_place_edges': len(edges), 'passenger_legs_unmatched': sum(unmatched.values()),
                      'top_unmatched_passengers': unmatched.most_common(10)}, indent=2, ensure_ascii=False))


def prepared_place_nodes(nodes, entities):
    """Expose safe aliases to downstream resolution without editing the KG.

    A repeated KG ID is an alias supplement, not a second identity. The entity
    resolver combines these names with the earlier normalized node. Suffix-less
    street aliases require a unique full street and locality in the entire index.
    """
    from build_email_places import PublicPlaces, public_address_fields
    from place_normalization import STREET_WORDS
    nodes = {n['id']: clean_place_node(n) for n in nodes}
    existing = {n['id']: n for n in entities if n['entity_type'] in ('location', 'property')}
    public = PublicPlaces([*existing.values(), *nodes.values()])
    for node in existing.values():
        aliases = aliases_for_place(node['name'])
        if aliases:
            nodes.setdefault(node['id'], clean_place_node(node))
    for bare in public.incomplete_streets:
        target = public.resolve(bare)
        if target is None:
            continue
        node = nodes.setdefault(target, clean_place_node(public.nodes[target]))
        aliases = node.get('aliases') or []
        node['aliases'] = sorted(set(aliases) | {bare})
    for original in public.nodes.values():
        for label in [original['name'], *(original.get('original_names') or []), *public_address_fields(original)]:
            head = label.split(',', 1)[0].strip()
            if not re.match(r'^\d+\s', head):
                continue
            bare = re.sub(r'\s+(?:' + '|'.join(sorted(STREET_WORDS)) + r')\.?$', '', head, flags=re.I)
            if bare == head or public.resolve(bare) != original['id']:
                continue
            node = nodes.setdefault(original['id'], clean_place_node(original))
            node['aliases'] = sorted(set(node.get('aliases') or []) | {bare})
    return list(nodes.values())


def reviewed_links(slug_of, entities, matcher):
    """Place -> person/company edges from config/place_links.json, minus ones the graph already has."""
    path = ROOT / 'config/place_links.json'
    if not path.exists():
        return []
    named = {name_key(e['name']): e['id'] for e in entities
             if e['entity_type'] in ('person', 'organization', 'shell_company') and not e.get('suspected_parse_artifact')}
    existing = {(r['source_entity_id'], r['target_entity_id'], r['relationship_type'])
                for r in json.loads((ROOT / 'output/normalized_relationships.json').read_text())}
    edges = []
    for link in json.loads(path.read_text())['links']:
        place, who = slug_of.get(link['place']), named.get(name_key(link['person'])) or matcher.match(link['person'])[0]
        if place is None or who is None:
            print(f"place link skipped, not in graph: {link['place']} / {link['person']}", flush=True)
            continue
        kind = link['relationship']
        if {(place, who, kind), (who, place, kind)} & existing:
            continue
        edges.append({'id': f'{kind}_{place}_{who}', 'source_entity_id': place, 'target_entity_id': who,
                      'relationship_type': kind, 'weight': 1, 'date_first': link.get('date_first'), 'date_last': link.get('date_last'),
                      'metadata': {'record_count': 1, 'basis': link['basis'], 'source': ATTRIBUTION + ' (location description; reviewed link)',
                                   'meaning': 'Stated in the curated place description; not independently verified.'}})
    return edges


if __name__ == '__main__':
    main()
