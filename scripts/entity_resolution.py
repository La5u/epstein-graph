"""Existing-node lookup shared by NER typing and entity graph aggregation."""
from collections import defaultdict
import json
from pathlib import Path
import re

from build_companies import normal_name
from build_contacts import contact_id
from extract_companies import SUFFIXES
from build_communications import ocr_repair, WEBMAIL_DOMAINS

ROOT = Path(__file__).resolve().parents[1]
FIRST_NAMES_PATH = ROOT / 'config/entity_first_names.json'
FIRST_NAMES = set(json.loads(FIRST_NAMES_PATH.read_text()))
COMPANIES = {'organization', 'shell_company'}


def company_key(value):
    """Company extractor suffix vocabulary, without discarding meaningful nouns."""
    words = normal_name(value).split()
    legal = set(SUFFIXES.values()) - {'trust', 'foundation', 'holdings', 'fund'}
    while len(words) > 1 and words[-1] in legal:
        words.pop()
    return ' '.join(words)


def names(node):
    yield node['name']
    for field in ('original_names', 'aliases'):
        values = node.get(field) or []
        if isinstance(values, str):
            try:
                values = json.loads(values)
            except ValueError:
                values = [values]
            if isinstance(values, str):
                values = [values]
        yield from (v for v in values if isinstance(v, str))


def read_nodes(out):
    nodes = {}
    # Only layers built before this one. graph_entities.json / graph.json are written after it
    # (build_contacts, analyze_graph) and contain its own nodes: reading them made every build
    # rerun the multi-hour entity steps and fed this layer's aliases back into itself.
    for filename in ('normalized_entities.json', 'company_nodes.json', 'place_nodes.json',
                     'email_place_nodes.json', 'communication_nodes.json', 'contact_nodes.json',
                     'victim_nodes.json', 'vehicle_nodes.json'):
        path = out / filename
        if not path.exists():
            continue
        payload = json.loads(path.read_text())
        for node in payload.get('nodes', []) if isinstance(payload, dict) else payload:
            if (str(node['id']).startswith('entity_')
                    or node.get('source') == 'Full-text entity mentions; NER is unverified'):
                continue
            identity = node['id']
            if identity in nodes:
                previous = nodes[identity]
                previous['aliases'] = sorted(set(names(previous)) | set(names(node)))
            else:
                nodes[identity] = dict(node)
    return nodes


def metadata(node):
    value = node.get('metadata') or {}
    return json.loads(value) if isinstance(value, str) else value


def email_address(value):
    """Recognize literal addresses and glued OCR @ readings, before URL parsing."""
    value = value.strip(' <>\"\'.,;:')
    if '@' in value:
        return value if re.fullmatch(r'[^\s@]+@[^\s@]+\.[A-Za-z]{2,}', value) else None
    for domain in sorted(WEBMAIL_DOMAINS, key=len, reverse=True):
        match = re.fullmatch(r'([\w.+%-]+)[®eg]' + re.escape(domain), value, re.I)
        if match:
            return match[1] + '@' + domain
    match = re.fullmatch(r'([^\s®]+)®([\w.-]+\.[A-Za-z]{2,})', value)
    return match[1] + '@' + match[2] if match else None


# Deliberately require a separator reading before a known provider for e/g;
# an arbitrary website ending in gmail.com is not automatically an address.
EMAIL_LIKE = re.compile(r'[\w.+%-]+(?:@|®)[\w.-]+\.[A-Za-z]{2,}|[\w.+%-]+[eg](?:'
                        + '|'.join(re.escape(d) for d in sorted(WEBMAIL_DOMAINS, key=len, reverse=True)) + r')(?![\w.-])', re.I)


def canonical_place(value, distinct_city=False):
    key = normal_name(value)
    if key in {'us', 'usa', 'u s', 'u s a', 'united states', 'united states of america', 'ny united states'}:
        return 'United States'
    if key == 'new york':
        return 'New York'
    if key == 'manhattan':
        return 'Manhattan'
    if key in {'new york city', 'nyc'}:
        return 'New York City' if distinct_city else 'New York'
    return value


def person_kind(value, kind, known=False):
    tokens = value.split()
    if not known and tokens and tokens[0].casefold() in FIRST_NAMES:
        if len(tokens) == 1 and value.isupper():
            return 'person_weak'
        if kind in {'FAC', 'GPE', 'ORG'} and 2 <= len(tokens) <= 3 and all(t.istitle() for t in tokens):
            return 'PERSON'
    return kind


class ExistingIndex:
    def __init__(self, existing, gazetteer, key=None, lookup=None):
        self.existing = existing
        self.exact, self.company, self.tails = (defaultdict(set) for _ in range(3))
        self.primary, self.primary_company, self.full_names = (defaultdict(set) for _ in range(3))
        self.emails = defaultdict(set)
        self.tail_names = defaultdict(set)
        self.full_labels = {}
        for identity, node in existing.items():
            self.primary[normal_name(node['name'])].add(identity)
            if node['entity_type'] in COMPANIES:
                self.primary_company[company_key(node['name'])].add(identity)
            for value in names(node):
                normalized = normal_name(value)
                self.exact[normalized].add(identity)
                if node['entity_type'] in COMPANIES:
                    self.company[company_key(value)].add(identity)
                tokens = normalized.split()
                if node['entity_type'] in COMPANIES | {'location'}:
                    self.full_names[normalized].add(identity)
                    self.full_labels.setdefault(normalized, value)
                    for start in range(1, len(tokens) - 2):
                        tail = ' '.join(tokens[start:])
                        self.tails[tail].add(identity)
                        self.tail_names[tail].add(normalized)
        self.gaz_names = {canonical: [canonical, *spec.get('aliases', [])] for canonical, spec in gazetteer.items()}
        self.aliases = {normal_name(alias): canonical for canonical, spec in gazetteer.items()
                        for alias in [canonical, *spec.get('aliases', [])]}
        self.distinct_city = bool(self.locations('New York City'))
        if key is not None:
            # Public merged identity records map HMAC IDs to retained email nodes.
            owners = {i:i for i,n in existing.items() if n['entity_type'] == 'email'}
            for identity, node in existing.items():
                if node['entity_type'] == 'email':
                    for member in node.get('merged_identities', []):
                        owners[member['id']] = identity
            for member in list(owners):
                seen, target = set(), owners[member]
                while target in owners and owners[target] != target and target not in seen:
                    seen.add(target)
                    target = owners[target]
                owners[member] = target
            self.email_owners, self.key = owners, key
            lookup = lookup or ROOT / 'data/private/contact_lookup.json'
            if owners and lookup.exists():
                for identity, item in json.loads(lookup.read_text()).items():
                    target = owners.get(identity)
                    if target is not None and item.get('kind') == 'email':
                        for value in [item['value'], *item.get('raw_spellings', [])]:
                            address = email_address(value)
                            if address:
                                self.emails[ocr_repair(address)].add(target)

    def locations(self, value):
        return {i for i in self.exact.get(normal_name(value), ()) if self.existing[i]['entity_type'] == 'location'}

    def hits(self, value):
        canonical = self.aliases.get(normal_name(value), value)
        # A legal entity's own name/key wins over another node's original-name
        # alias (the KG can list subsidiaries under their parent bank).
        hits = set(self.primary.get(normal_name(canonical), ()))
        if not hits:
            hits = set(self.primary_company.get(company_key(canonical), ()))
        if not hits:
            hits = set(self.exact.get(normal_name(canonical), ()))
        if not hits:
            hits = set(self.company.get(company_key(canonical), ()))
        if not hits:
            for alias in self.gaz_names.get(canonical, ()):
                hits |= self.primary.get(normal_name(alias), set()) | self.primary_company.get(company_key(alias), set())
            if not hits:
                for alias in self.gaz_names.get(canonical, ()):
                    hits |= self.exact.get(normal_name(alias), set()) | self.company.get(company_key(alias), set())
        return hits

    def choose(self, hits):
        if len(hits) == 1:
            return next(iter(hits))
        if (hits and all(self.existing[i]['entity_type'] in COMPANIES for i in hits)
                and len({company_key(self.aliases.get(normal_name(self.existing[i]['name']), self.existing[i]['name'])) for i in hits}) == 1):
            # Equivalent company keys can already have multiple legal-form nodes.
            # Reuse the KG identity, otherwise the most evidenced company. Exact
            # legal-name matches were attempted first, so Inc/LLC stay distinct.
            return min(hits, key=lambda i: (not isinstance(i, int),
                       -metadata(self.existing[i]).get('document_count', 0), str(i)))
        return None

    def email_hits(self, value):
        address = email_address(value)
        if not address:
            return set()
        repaired = ocr_repair(address)
        # Prefer the actual canonical mailbox node to legacy contact nodes for
        # the same repaired mailbox. Loading all contact layers can expose both.
        for candidate in ('@'.join(repaired), address, address.casefold()):
            target = self.email_owners.get(contact_id(candidate, 'email', self.key))
            if target is not None:
                return {target}
        return set(self.emails.get(repaired, ()))
