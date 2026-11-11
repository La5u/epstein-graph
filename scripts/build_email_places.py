"""Places mentioned in email bodies -> graph nodes linked to the emails' senders/recipients.

    .venv/bin/python scripts/build_email_places.py [--min-docs 25] [--classify]

Input: data/private/email_places.db (scripts/extract_places.py).
  named places  spaCy GPE/LOC/FAC mentions in >= --min-docs documents, folded by
                spelling, then classified once by GPT-6-Luna (`sol -m gpt-6-luna`)
                into real place / not, a canonical name and a type; verdicts are
                cached in data/private/place_classification.json (--classify adds
                new candidates; without it only cached verdicts are used).
  addresses     street addresses in >= MIN_ADDRESS_DOCS documents, normalized
                (Ave -> Avenue, units and localities preserved). They can be private homes, so they
                are pseudonymous here (address_<HMAC>); real values go to
                data/private/address_lookup.json for the private viewer only.
Edges: place -- sender/recipient identities of the emails that mention it
(email_metadata evidence), >= MIN_SHARED_DOCS shared documents, the top
MAX_PER_PLACE per place, only identities shown in the viewer.
Writes output/email_place_nodes.json, output/email_place_edges.json.
"""
import argparse
import hashlib
import hmac
import json
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

import corpus as corpus_source
from build_contacts import contact_key
from fts_cache import cached
from normalize_graph import name_key
from place_normalization import clean_place_text, place_key, place_label, place_annotations, clean_place_node, aliases_for_place, place_label_reason

ROOT = Path(__file__).resolve().parents[1]
MENTIONS = ROOT / 'data/private/email_places.db'
VERDICTS = ROOT / 'data/private/place_classification.json'
ADDRESSES = ROOT / 'data/private/address_lookup.json'
MIN_ADDRESS_DOCS, MIN_SHARED_DOCS, MAX_PER_PLACE, BATCH = 5, 3, 15, 150
MIN_ADDRESS_PEOPLE = 2  # identities linked to an address node
NOT_PLACES = {'sent', 'sat', 'sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'event', 'island', 'epstein', 'delta', 'verizon'}
STREET_WORDS = {'st': 'street', 'ave': 'avenue', 'rd': 'road', 'blvd': 'boulevard', 'dr': 'drive', 'ln': 'lane', 'pl': 'place',
                'ct': 'court', 'hwy': 'highway', 'pkwy': 'parkway', 'sq': 'square', 'n': 'north', 's': 'south', 'e': 'east', 'w': 'west'}


def address_key(text):
    """Exact, conservative street normalization; never discard a unit or city.

    Expand street suffixes only in suffix position (not St John), and directions
    only next to the house number or suffix. No spelling/number guessing.
    """
    text = clean_place_text(text).casefold().replace('.', '')
    words = re.findall(r'[a-z0-9]+(?:-[a-z0-9]+)*|#', text)
    if not words:
        return ''
    suffixes = {k: v for k, v in STREET_WORDS.items() if k not in ('n', 's', 'e', 'w')}
    suffixes.update({v: v for v in suffixes.values()})
    suffixes.update({'way': 'way', 'terrace': 'terrace', 'ter': 'terrace'})
    # The first token is the house number; a suffix must follow a street name.
    end = next((i for i in range(2, len(words)) if words[i] in suffixes), None)
    for i in range(1, end if end is not None else len(words)):
        words[i] = re.sub(r'^(\d+)(?:st|nd|rd|th)$', r'\1', words[i])
    if end is None and len(words) >= 3:
        words[1] = {'n': 'north', 's': 'south', 'e': 'east', 'w': 'west'}.get(words[1], words[1])
    if end is not None:
        words[end] = suffixes[words[end]]
        if end > 2 and words[1] == 'fast':
            words[1] = 'east'  # 'E' read as 'F' next to the house number ('401 Fast Las Olas Blvd')
        for i in (1, end + 1):
            if i < len(words):
                words[i] = {'n': 'north', 's': 'south', 'e': 'east', 'w': 'west',
                            'ne': 'northeast', 'nw': 'northwest', 'se': 'southeast', 'sw': 'southwest'}.get(words[i], words[i])
    # Unit identifiers are retained, including #; equivalent suite/apt spellings
    # are safe, but apartment and suite are deliberately different kinds.
    words = [{'ste': 'suite', 'apt': 'apartment'}.get(w, w) for w in words]
    return ' '.join(words)


def canonical_address(key):
    # Reviewed spelling equivalence, restricted to this house number/street.
    # Keep all trailing locality/unit tokens; never extend this to 6300 etc.
    return re.sub(r'^6100 (?:redhook|red hook)(?: quarters?)? street(?= |$)',
                  '6100 redhook street', key)


SUFFIX_WORDS = {'street', 'avenue', 'road', 'boulevard', 'drive', 'lane', 'place', 'court', 'highway', 'parkway',
                'square', 'way', 'terrace'}
DIRECTIONS = {'north', 'south', 'east', 'west', 'northeast', 'northwest', 'southeast', 'southwest'}
UNIT_WORDS = {'suite', 'apartment', 'unit', 'floor', 'fl', 'room', 'rm', '#', 'no', 'ste', 'apt', 'penthouse', 'ph'}
US_STATES = set('al ak az ar ca co ct de dc fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv nh nj nm ny nc '
                'nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy pr vi usa'.split())


SPELLED_NUMBERS = {'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'}
ORDINAL_STREETS = {'first', 'second', 'third', 'fourth', 'fifth', 'sixth', 'seventh', 'eighth', 'ninth', 'tenth', 'eleventh', 'twelfth'}


def split_address(key):
    """(street, locality) of a normalized key; street is None without a suffix.

    street: house number through the street suffix (and a trailing direction).
    locality: the remaining words minus units, zip codes and US state codes.
    """
    words = key.split()
    end = next((i for i in range(2, len(words)) if words[i] in SUFFIX_WORDS), None)
    if end is None or not words[0][:1].isdigit() or not words[0].strip('0')[:1].isalnum():
        return None, ''  # '00 meet dr' is a time ('11:00 Meet Dr …'), not a house number
    if end + 1 < len(words) and words[end + 1] in DIRECTIONS:
        end += 1
    locality, skip = [], False
    for word in words[end + 1:]:
        if skip and (any(c.isdigit() for c in word) or len(word) == 1 or word in SPELLED_NUMBERS):  # 'suite one'
            skip = False
            continue
        skip = word in UNIT_WORDS
        if skip or any(c.isdigit() for c in word) or word in US_STATES:
            continue
        locality.append(word)
    return ' '.join(words[:end + 1]), ' '.join(locality)


def ocr_fold(street):
    """House number O->0, l/I->1; street words rn->m and digits read as letters.
    Only used to bring OCR variants of one street together, never as a label."""
    number, *rest = street.split()
    if re.fullmatch(r'[0-9oil]+', number) and re.search(r'\d', number):
        number = number.translate(str.maketrans('oil', '011'))
    rest = [w.replace('rn', 'm').translate(str.maketrans('01', 'ol')) if re.search(r'[a-z]', w) else w for w in rest]
    return ' '.join([number, *rest])


def fuzzy_street_merge(folded, weight, parts):
    """{fold: keys} with OCR misreadings of one street joined ('2029 stiedin ct',
    '2029 stierhn ct' -> '2029 stierlin court').

    Only streets with the same house number and suffix are compared, on the
    street name without spaces ('ponce deleon' = 'ponce de leon'). Joined: equal,
    one edit apart for names of 7+ letters, or two edits when
    one spelling has 3x the documents. Short names never join (Green/Greene, Water/Baker),
    except a misread numbered street ('filth' -> 'fifth') or a cut-off ending ('fa' ->
    'fay'), one edit from a spelling with 3x the documents.
    The joined group keeps the fold of its most-documented member.
    """
    from rapidfuzz.distance import Levenshtein
    docs = {fold: sum(weight.get(k, 0) for k in keys) for fold, keys in folded.items()}
    by_street = defaultdict(list)
    for fold in folded:
        words = fold.split()
        suffix = next((w for w in reversed(words) if w in SUFFIX_WORDS), '')
        name = ''.join(w for w in words[1:] if w not in SUFFIX_WORDS and w not in DIRECTIONS)
        by_street[(words[0], suffix)].append((fold, name))
    parent = {fold: fold for fold in folded}

    def root(fold):
        while parent[fold] != fold:
            fold = parent[fold]
        return fold
    for group in by_street.values():
        for i, (a, name_a) in enumerate(group):
            for b, name_b in group[i + 1:]:
                shortest = min(len(name_a), len(name_b))
                distance = Levenshtein.distance(name_a, name_b, score_cutoff=2)
                ratio = max(docs[a], docs[b]) / max(min(docs[a], docs[b]), 1)
                # Short names join only as a misread numbered street ('filth' / 'fifth') or a cut-off
                # ending ('fa' / 'fay'), and only onto the spelling with 3x the documents.
                heavier = name_a if docs[a] >= docs[b] else name_b
                short_ok = distance == 1 and ratio >= 3 and (heavier in ORDINAL_STREETS or (
                    1 <= shortest <= 2 and (name_a.startswith(name_b) or name_b.startswith(name_a))))
                if distance == 0 or (distance == 1 and shortest >= 7) or (distance == 2 and shortest >= 7 and ratio >= 3) or short_ok:
                    ra, rb = root(a), root(b)
                    if ra != rb:
                        keep, drop = sorted((ra, rb), key=lambda f: (-docs[f], f))
                        parent[drop] = keep
                        docs[keep] += docs[drop]
    merged = defaultdict(list)
    for fold, keys in folded.items():
        merged[root(fold)].extend(keys)
    return merged


def building_groups(keys, weight):
    """{key: group key}: one group per building.

    Same house number and street (after OCR folding) is one building; units,
    suites and zip codes never split it. Localities still do: a street seen
    with two or more different localities keeps them apart, and spellings
    without a locality then stay on their own (never guessed into one).
    A suffix-less key ('500 fifth') joins the one street it abbreviates.
    weight: {key: documents}, to name each group after its best-attested spelling.
    """
    parts = {k: split_address(k) for k in keys}
    folded = defaultdict(list)
    for k, (street, _) in parts.items():
        if street:
            folded[ocr_fold(street)].append(k)
    folded = fuzzy_street_merge(folded, weight, parts)
    streets_by_bare = defaultdict(set)  # '500 fifth' -> {'500 fifth avenue'}
    for fold in folded:
        words = fold.split()
        bare = words[:-1] if words[-1] in DIRECTIONS else words
        streets_by_bare[' '.join(bare[:-1])].add(fold)
    groups = {}
    for fold, members in folded.items():
        # Name the group after a clean spelling (one OCR folding leaves unchanged).
        best = max(members, key=lambda k: (ocr_fold(parts[k][0]) == parts[k][0], weight.get(k, 0), k))
        street = parts[best][0]
        localities = {parts[k][1] for k in members if parts[k][1]}
        for k in members:
            locality = parts[k][1] or (next(iter(localities)) if len(localities) == 1 else '')
            groups[k] = f'{street}, {locality}' if locality else street
    for k, (street, _) in parts.items():
        if street is None:
            bare = ' '.join(re.sub(r'^(\d+)(?:st|nd|rd|th)$', r'\1', w) for w in k.split())  # '9 east 71st'
            match = streets_by_bare.get(ocr_fold(bare), set()) if k[:1].isdigit() else set()
            targets = {groups[m] for fold in match for m in folded[fold]}
            groups[k] = next(iter(targets)) if len(match) == 1 and len(targets) == 1 else k
    return groups


def address_id(key, secret):
    return 'address_' + hmac.new(secret, ('address\0' + key).encode(), hashlib.sha256).hexdigest()[:24]


def public_address_fields(node):
    """Only explicit public fields, never prose or email-derived spellings."""
    metadata = node.get('metadata') or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    for value in (node.get('address'), metadata.get('address') if isinstance(metadata, dict) else None):
        if isinstance(value, str):
            yield value


def street_prefix(label):
    """A comma delimits international streets; otherwise require a known suffix.

    This is an index boundary, not permission to discard query locality/units.
    """
    head = canonical_address(address_key(label.split(',', 1)[0]))
    if ',' in label and not re.search(r'\b(?:suite|apartment|unit|floor|room)\b|#', head):
        return head
    match = re.match(r'^(\d+[a-z0-9-]* .+? (?:street|avenue|road|boulevard|drive|lane|place|court|highway|parkway|square|way|terrace)(?: (?:north|south|east|west|northeast|northwest|southeast|southwest))?)(?: |$)', head)
    return match[1] if match else None


class PublicPlaces:
    """Set-valued indexes; aliases are internal and never added to output nodes."""
    def __init__(self, records, curated=()):
        self.nodes = {}
        self.names, self.addresses = defaultdict(set), defaultdict(set)
        self.streets = defaultdict(set)
        self.incomplete_streets = defaultdict(set)
        records = list(records)
        for node in records:
            if node['entity_type'] not in ('location', 'property'):
                continue
            self.nodes.setdefault(node['id'], node)
            for label in [node['name']] + (node.get('original_names') or []):
                self.names[place_key(label)].add(node['id'])
                for alias in aliases_for_place(label):
                    self.names[place_key(alias)].add(node['id'])
                self.add_address(label, node['id'])
            for label in public_address_fields(node):
                self.add_address(label, node['id'])
        # Same predicates as build_places, but never its first-match fallback:
        # require a unique normalized identity across ALL matching graph names.
        graph = [n for n in records if n['entity_type'] in ('location', 'property')
                 and isinstance(n['id'], int)]
        for loc in curated:
            own = name_key(loc['name'])
            address = name_key(loc.get('address') or '')
            matches = {n['id'] for n in graph if own and name_key(n['name']) and
                       (name_key(n['name']) == own or name_key(n['name']) in address or
                        re.match(re.escape(own) + r'\b', name_key(n['name'])))}
            if not matches:
                fallback = 'place_' + loc['slug']
                matches = {fallback} if fallback in self.nodes else set()
            if len(matches) == 1:
                for label in public_address_fields(loc):
                    self.add_address(label, next(iter(matches)))

    def add_address(self, label, node_id):
        if not re.match(r'^\s*\d+\s', label):
            return
        key = canonical_address(address_key(label))
        self.addresses[key].add(node_id)
        base = street_prefix(label)
        if base:
            self.streets[base].add(key)
            words = base.split()
            if words[-1] in SUFFIX_WORDS:
                self.incomplete_streets[' '.join(words[:-1])].add(base)

    def address_candidates(self, label):
        key = canonical_address(address_key(label))
        variants = self.streets.get(key, set())
        # Complete only a whole suffix-less street, never a locality/unit prefix.
        if split_address(key)[0] is None:
            bare = ' '.join(re.sub(r'^(\d+)(?:st|nd|rd|th)$', r'\1', w) for w in key.split())
            completions = self.incomplete_streets.get(bare, set())
            if len(completions) == 1:
                return self.address_candidates(next(iter(completions)))
            if completions:
                return set().union(*(self.addresses[v] for base in completions for v in self.streets[base])), 'ambiguous'
        candidates = set(self.addresses.get(key, set()))
        for variant in variants:
            candidates.update(self.addresses[variant])
        # Even two different explicit localities for one ID are not permission
        # to silently collapse them. Exact qualified queries still work.
        qualified = variants - {key}
        has_units = any(re.search(r'\b(?:suite|apartment|unit|floor|room)\b|#', v) for v in qualified)
        # Variants naming one locality ('palm beach', 'palm beach fl 33480 …') are one place.
        localities = sorted({split_address(v)[1] for v in qualified}, key=len)
        one_locality = len(localities) <= 1 or all(l.startswith(localities[0]) for l in localities[1:]) and localities[0]
        if len(candidates) == 1 and (len(qualified) <= 1 or one_locality) and not has_units:
            return candidates, 'exact' if key in self.addresses else 'street_only_prefix'
        return candidates, 'ambiguous' if candidates else 'unmatched'

    def resolve(self, label):
        if re.match(r'^\s*\d+\s', label):
            matches, rule = self.address_candidates(label)
            if rule == 'ambiguous':
                return None
        else:
            matches = self.names.get(place_key(label), set())
        return next(iter(matches)) if len(matches) == 1 else None


def add_addresses(addr_docs, addr_spellings, public, secret, place_docs, nodes, summary=None, review=None):
    """Union all aliases before applying thresholds or computing edge counts.

    Public places match per spelling, exactly as before. Other spellings join
    their building (building_groups); a building whose spellings match exactly
    one public place joins that place.
    """
    canonical_of = {key: canonical_address(key) for key in addr_docs}
    weight = Counter()
    for key, canonical in canonical_of.items():
        weight[canonical] += len(addr_docs[key])
    building = building_groups(set(canonical_of.values()), weight)
    public_of, building_public = {}, defaultdict(set)
    for key, canonical in canonical_of.items():
        public_of[key] = public.resolve(canonical)
        if public_of[key] is not None:
            building_public[building[canonical]].add(public_of[key])
    grouped, spellings, aliases = defaultdict(set), defaultdict(Counter), defaultdict(set)
    for key, found in addr_docs.items():
        canonical = canonical_of[key]
        target = public_of[key]
        candidates, rule = public.address_candidates(canonical)
        if summary is not None:
            summary[rule + '_address_keys'] += 1
        if review is not None and rule == 'ambiguous':
            review.append({'address': canonical, 'spellings': sorted(addr_spellings[key]),
                           'candidate_ids': sorted(candidates, key=str), 'documents': len(found),
                           'reason': 'multiple_public_ids_or_explicit_address_variants'})
        if target is None:
            shared = building_public[building[canonical]]
            if len(shared) == 1 and rule != 'ambiguous':
                target = next(iter(shared))
                if summary is not None:
                    summary['joined_public_building'] += 1
        if target is None:
            target = address_id(building[canonical], secret)
            if summary is not None and building[canonical] != canonical:
                summary['merged_into_building'] += 1
        grouped[target].update(found)
        spellings[target].update(addr_spellings[key])
        aliases[target].add(address_id(key, secret))
    lookup = {}
    for node_id, found in grouped.items():
        key = node_id
        # Include named-place evidence already assigned to the same identity.
        found = found | place_docs.get(node_id, set())
        if len(found) < MIN_ADDRESS_DOCS:
            continue
        place_docs[node_id].update(found)
        if node_id in public.nodes:
            node = nodes.setdefault(node_id, dict(public.nodes[node_id]))
        else:
            node = nodes.setdefault(node_id, {'id': node_id, 'name': 'Address_' + node_id.split('_', 1)[1],
                'entity_type': 'location', 'place_type': 'address',
                'source': 'Street address in email bodies; pseudonymous value (real value in the private viewer only)'})
        previous = {item['id']: item for item in node.get('merged_address_aliases', [])}
        previous.update({alias: {'id': alias, 'rule': 'exact_normalized_or_reviewed_address'} for alias in aliases[key]})
        node['merged_address_aliases'] = sorted(previous.values(), key=lambda item: item['id'])
        lookup[node_id] = {'value': clean_place_text(public.nodes[node_id]['name'] if node_id in public.nodes else spellings[key].most_common(1)[0][0]),
                           'metadata': {'signature_annotations': sorted({a for text in spellings[key] for a in place_annotations(text)})},
                           'spellings': sorted(spellings[key]), 'documents': len(place_docs[node_id])}
        reasons = {place_label_reason(text) for text in spellings[key]}
        if node_id not in public.nodes and reasons and None not in reasons:
            node.update(label_quality='not_a_name', suspected_parse_artifact=True,
                        place_quality_reason=sorted(reasons)[0])
    if summary is not None:
        summary['public_address_targets_kept'] = sum(k in public.nodes for k in lookup)
        summary['private_address_targets_kept'] = sum(k not in public.nodes for k in lookup)
        summary['address_aliases_kept'] = sum(len(aliases[k]) for k in lookup)
    return lookup


def signature_block(spellings, docs, corpus, sample=30, seen_lines=None):
    """(share, line): how often the address follows the same line of text.

    An address in a signature or letterhead repeats under one company or person
    line ('HBRK Associates Inc.', 'Michelin House'); an address in a message body
    does not. Reads up to `sample` documents (evenly spread, deterministic).
    """
    docs = sorted(docs)
    picked = docs[::max(1, len(docs) // sample)][:sample]
    lines, seen = Counter(), 0
    for efta in picked:
        text = ' '.join(t or '' for (t,) in cached(corpus, 'SELECT text FROM pages WHERE doc_id=? ORDER BY page', (efta,)))
        at = next((i for i in (text.find(s) for s in spellings) if i >= 0), -1)
        if at < 0:
            continue
        seen += 1
        before = [part for part in re.split(r'\s{2,}|\n|>', text[max(0, at - 200):at]) if re.search(r'[A-Za-z]{2}', part)]
        if before:
            lines[' '.join(re.sub(r'[^\w&\'. -]', ' ', before[-1]).split())[:60]] += 1
    if seen_lines is not None:
        seen_lines.update(line.casefold() for line in lines)
    if seen < 3 or not lines:
        return 0.0, ''
    line, count = lines.most_common(1)[0]
    if len(line.split()) > 8 and 'regist' not in line.casefold():
        return 0.0, ''  # a repeated sentence (a forwarded thread), not a name/company line
    return round(count / seen, 2), line


SIGNATURE_SHARE = 0.4


def signature_number_merges(lookup, docs, signature, lines, ratio=20):
    """{member: target}: same street words, a signature line also seen above the
    target (lines: {node: set of lines}), and a house number
    one OCR slip from the target's (one edit, or its tail: '013' in '2013'), with
    the target documented `ratio` times more. Different buildings on one street
    (340 / 345 Park Avenue) sit under different signature lines and stay apart."""
    from rapidfuzz.distance import Levenshtein
    streets = defaultdict(list)
    for node_id, entry in lookup.items():
        street, _ = split_address(address_key(entry['value']))
        if street:
            number, rest = street.split(' ', 1)
            streets[rest].append((len(docs.get(node_id, ())), number, node_id))
    merges = {}
    for members in streets.values():
        top_docs, top_number, top = max(members)
        for n, number, node_id in members:
            share, line = signature.get(node_id, (0.0, ''))
            if node_id != top and share >= SIGNATURE_SHARE and line.casefold() in lines.get(top, ()) \
                    and top_docs >= ratio * max(n, 1) \
                    and (Levenshtein.distance(number, top_number) <= 1 or top_number.endswith(number)):
                merges[node_id] = top
    return merges


def classify(candidates):
    """[(key, display, docs)] -> {key: {'place': bool, 'name': str, 'type': str}} via Luna."""
    lines = '\n'.join(f'{key}\t{display}\t{docs}' for key, display, docs in candidates)
    prompt = ('Below are strings that a named-entity tagger marked as places in emails (Epstein files). Columns: key, most common '
              'spelling, number of emails. For each, decide if it is a real geographic place or named facility (city, country, '
              'state/region, island, neighbourhood, airport, building, hotel, venue, street) and not a person, company, product, '
              'time zone, court district or boilerplate. Output ONLY JSON lines in input order: {"key": ..., "place": true|false, '
              '"name": "<canonical English name, e.g. New York City / Paris / United States>", "type": "city|country|region|island|'
              'neighbourhood|airport|building|venue|street|other"}. For "New York" decide city vs state from typical email usage '
              '(usually the city).\n\n' + lines)
    result = subprocess.run(['sol', '-m', 'gpt-6-luna', '-e', 'low'], input=prompt, capture_output=True, text=True, timeout=1800)
    if result.returncode == 75:  # usage limit: stop rather than record empty verdicts
        raise SystemExit('sol: usage limit reached; rerun later or classify data/private/place_candidates.tsv elsewhere')
    out = {}
    for line in result.stdout.splitlines():
        line = line.strip().strip(',')
        if line.startswith('{'):
            try:
                item = json.loads(line)
                out[item['key']] = {'place': bool(item.get('place')), 'name': item.get('name') or '', 'type': item.get('type') or 'other'}
            except (ValueError, KeyError):
                continue
    return out


def normalized_verdicts(verdicts):
    """Keep old cached classifications usable after key normalization.

    Conflicting aliases do not invent a decision; an exact cached key wins.
    """
    out = dict(verdicts)
    grouped = defaultdict(list)
    for key, verdict in verdicts.items():
        grouped[place_key(key)].append(verdict)
    for key, values in grouped.items():
        signatures = {(v.get('place'), place_key(v.get('name') or ''), v.get('type')) for v in values}
        if key not in out and len(signatures) == 1:
            out[key] = values[0]
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--min-docs', type=int, default=25)
    parser.add_argument('--classify', action='store_true', help='ask Luna about uncached candidates')
    parser.add_argument('--export-candidates', action='store_true',
                        help='write uncached candidates to data/private/place_candidates.tsv (for classification elsewhere) and stop')
    args = parser.parse_args()
    out = ROOT / 'output'
    db = sqlite3.connect(f'file:{MENTIONS}?mode=ro', uri=True)
    docs, spellings, addr_docs, addr_spellings = defaultdict(set), defaultdict(Counter), defaultdict(set), defaultdict(Counter)
    for efta, kind, text, n in db.execute('SELECT efta, kind, text, n FROM mentions'):
        if kind == 'STREET':
            key = address_key(text)
            addr_docs[key].add(efta)
            addr_spellings[key][text] += n
        else:
            key = place_key(text)
            if len(key) >= 3 and key not in NOT_PLACES and not key.isdigit():
                docs[key].add(efta)
                spellings[key][text] += n
    db.close()

    # Named places: fold spellings, drop obvious first names, classify the rest once.
    entities = json.loads((out / 'normalized_entities.json').read_text())
    first_names = Counter(name_key(e['name']).split()[0] for e in entities if e['entity_type'] == 'person' and name_key(e['name']))
    candidates = [(k, spellings[k].most_common(1)[0][0], len(v)) for k, v in docs.items()
                  if len(v) >= args.min_docs and not (' ' not in k and first_names[k] >= 2)]
    verdicts = normalized_verdicts(json.loads(VERDICTS.read_text())) if VERDICTS.exists() else {}
    todo = sorted((c for c in candidates if c[0] not in verdicts), key=lambda c: -c[2])
    if args.export_candidates:
        path = ROOT / 'data/private/place_candidates.tsv'
        path.write_text('key\tspelling\temails\n' + ''.join(f'{k}\t{d}\t{n}\n' for k, d, n in todo))
        path.chmod(0o600)
        print(f'{len(todo)} uncached candidates -> {path}')
        return
    if args.classify:
        for i in range(0, len(todo), BATCH):
            verdicts.update(classify(todo[i:i + BATCH]))
            VERDICTS.write_text(json.dumps(verdicts, indent=1, ensure_ascii=False) + '\n')
            VERDICTS.chmod(0o600)
            print(f'classified {min(i + BATCH, len(todo))}/{len(todo)}', flush=True)
    records = list(entities)
    if (out / 'place_nodes.json').exists():
        records.extend(json.loads((out / 'place_nodes.json').read_text()))
    curated_path = ROOT / 'data/external/epsteinexposed/locations.json'
    curated = json.loads(curated_path.read_text())['data'] if curated_path.exists() else []
    public = PublicPlaces(records, curated)
    place_docs, nodes = defaultdict(set), {}
    canonical_labels = {}
    for key, display, _ in candidates:
        verdict = verdicts.get(key)
        if not verdict or not verdict['place'] or not verdict['name']:
            continue
        canonical = place_label(verdict['name'])
        canonical_key = place_key(canonical)
        # Reuse one spelling for case/state variants, even without a public node.
        canonical = canonical_labels.setdefault(canonical_key, canonical)
        node_id = public.resolve(canonical)
        if re.match(r'^\s*\d+\s', canonical):
            # Address-like classifier results follow the same privacy and identity
            # path as STREET mentions; never export classifier-provided addresses.
            addr_key = address_key(canonical)
            addr_docs[addr_key].update(docs[key])
            addr_spellings[addr_key].update(spellings[key])
            continue
        if node_id is None:
            # Do not mint a duplicate public name when that name is ambiguous.
            if public.names.get(place_key(canonical)):
                continue
            node_id = 'place_' + re.sub(r'[^a-z0-9]+', '_', canonical_key).strip('_')
        place_docs[node_id] |= docs[key]
        if node_id not in public.nodes:
            node = nodes.setdefault(node_id, {'id': node_id, 'name': canonical, 'entity_type': 'location', 'place_type': verdict['type'],
                                              'email_spellings': [], 'source': 'Places named in email bodies (spaCy NER, classified by GPT-6-Luna); mentions are not visits'})
            node['email_spellings'].append(clean_place_text(display))
            nodes[node_id] = clean_place_node(node)

    # Public addresses reuse known labels; unmatched ones remain pseudonymous.
    merge_summary, review = Counter(), []
    lookup = add_addresses(addr_docs, addr_spellings, public, contact_key(), place_docs, nodes, merge_summary, review)
    review_path = ROOT / 'data/private/address_merge_review.json'
    review_path.touch(mode=0o600, exist_ok=True)
    review_path.chmod(0o600)
    review_path.write_text(json.dumps(review, indent=1, ensure_ascii=False) + '\n')

    # Signature lines first: an address under the same line as a far more frequent
    # one on the same street, with an OCR-damaged house number, is that address
    # ('2023 Costa Del Mar Road' and '013 …' under "Deepak Chopra MD" = 2013).
    corpus = corpus_source.connect() if corpus_source.PATH.exists() else None
    lines = defaultdict(set)  # every name/company line seen above each address
    signature = {node_id: signature_block(lookup[node_id]['spellings'], place_docs[node_id], corpus, seen_lines=lines[node_id])
                 for node_id in lookup if corpus is not None}
    if corpus is not None:
        corpus.close()
    for member, target in signature_number_merges(lookup, place_docs, signature, lines).items():
        place_docs[target] |= place_docs.pop(member)
        lookup[target]['spellings'] = sorted(set(lookup[target]['spellings']) | set(lookup.pop(member)['spellings']))
        lookup[target]['documents'] = len(place_docs[target])
        aliases = nodes[target].setdefault('merged_address_aliases', [])
        aliases += [{**a, 'rule': 'ocr_house_number_same_signature'} for a in nodes.pop(member).get('merged_address_aliases', [])]
        merge_summary['ocr_house_number_same_signature'] += 1

    # Link each place to the senders/recipients of the emails that mention it.
    wanted = set().union(*place_docs.values()) if place_docs else set()
    shown = {n['id'] for n in json.loads((out / 'communication_nodes.json').read_text())}
    people = {e['id'] for e in entities}
    endpoints = defaultdict(set)
    graph = sqlite3.connect(f'file:{out / "communication_graph.db"}?mode=ro', uri=True)
    for efta, a, b in graph.execute("SELECT efta_number, canonical_source, canonical_target FROM evidence WHERE relationship_type IN ('sent_to','cc_to') AND efta_number IS NOT NULL"):
        if efta in wanted:
            endpoints[efta].update((a, b))
    graph.close()

    def view_id(identity):
        if identity.startswith('kg:'):
            return int(identity[3:]) if int(identity[3:]) in people else None
        return identity if identity in shown else None
    edges, edge_documents = [], {}
    for place, found in place_docs.items():
        shared = defaultdict(list)
        for efta in sorted(found):
            for identity in {view_id(v) for v in endpoints.get(efta, ())} - {None}:
                shared[identity].append(efta)
        for identity, refs in sorted(shared.items(), key=lambda item: -len(item[1]))[:MAX_PER_PLACE]:
            if len(refs) >= MIN_SHARED_DOCS:
                edge_documents[f'email_place_{identity}_{place}'] = refs
                edges.append({'id': f'email_place_{identity}_{place}', 'source_entity_id': identity, 'target_entity_id': place,
                              'relationship_type': 'mentioned_place_in_email', 'weight': len(refs), 'date_first': None, 'date_last': None,
                              'metadata': {'record_count': len(refs), 'sample_documents': [{'efta_number': e} for e in refs[:5]],
                                           'meaning': 'Emails sent or received by this identity mention the place; a mention is not a visit, ownership or residence.'}})
    # An address is kept only when the emails tie it to at least two identities.
    people = defaultdict(set)
    for edge in edges:
        people[edge['target_entity_id']].add(edge['source_entity_id'])
    dropped = {node_id for node_id, node in nodes.items()
               if node.get('place_type') == 'address' and len(people[node_id]) < MIN_ADDRESS_PEOPLE}
    edges = [e for e in edges if e['target_entity_id'] not in dropped]
    linked = {e['target_entity_id'] for e in edges}
    signatures = 0
    for node_id, found in place_docs.items():
        if node_id in nodes:
            nodes[node_id]['email_document_count'] = len(found)
        if node_id in linked and node_id in signature:
            # Labelled, not removed: a letterhead address still ties people to an office.
            share, line = signature[node_id]
            if share >= SIGNATURE_SHARE:
                signatures += 1
                lookup[node_id].setdefault('metadata', {})['signature_block'] = line
                nodes[node_id]['signature_block_share'] = share
                nodes[node_id]['note'] = (f'Signature or letterhead address: in {share:.0%} of sampled emails it follows '
                                          'the same name/company line. Mostly boilerplate, not a place anyone visited.')
    ADDRESSES.write_text(json.dumps(lookup, indent=1, ensure_ascii=False) + '\n')
    ADDRESSES.chmod(0o600)
    # Weak/unlinked evidence stays in the full graph, hidden by default.
    for node_id, node in nodes.items():
        if node_id in dropped:
            node.update(label_quality='not_a_name', suspected_parse_artifact=True, place_quality_reason='weak_address_evidence')
    place_nodes = [clean_place_node(n) for n in nodes.values()]
    (out / 'email_place_nodes.json').write_text(json.dumps(place_nodes, indent=2, ensure_ascii=False) + '\n')
    (out / 'email_place_edges.json').write_text(json.dumps(edges, indent=2) + '\n')
    # Every email behind each edge, for the document index (build_documents.py).
    (out / 'email_place_documents.json').write_text(json.dumps({e['id']: edge_documents[e['id']] for e in edges}) + '\n')
    print(json.dumps({'named_candidates': len(candidates), 'classified': sum(1 for c in candidates if c[0] in verdicts),
                      'named_places_kept': sum(1 for n in place_nodes if n.get('place_type') != 'address'),
                      'addresses_kept': sum(1 for n in place_nodes if n.get('place_type') == 'address'),
                      'addresses_dropped_fewer_than_two_people': len(dropped), 'signature_addresses': signatures,
                      'edges': len(edges), 'address_merges': dict(merge_summary),
                      'ambiguous_address_reviews': len(review)}, indent=2))


if __name__ == '__main__':
    main()
