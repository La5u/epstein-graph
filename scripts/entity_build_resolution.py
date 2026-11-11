"""Resolution of stored extraction labels; deliberately outside scan signatures.

All approximate repairs require a unique independently existing full company.
Legal form, trust/fund identifiers and digits remain meaningful. No person
fuzzy matching occurs here. Raw private aliases are never written to outputs.
"""
from collections import defaultdict
from functools import lru_cache
import re
from urllib.parse import urlsplit

from bounded_json import items
from build_companies import normal_name
from build_contacts import contact_id
from entity_labels import dictionary_words, label_reason
from build_communications import ocr_repair
from entity_resolution import ExistingIndex, COMPANIES, FIRST_NAMES, company_key, email_address, names, metadata
from entity_build_rules import (clean_company_name, company_label_reason, company_tail,
                                has_company_form, text_reason, FILE, GENERIC, noise_prefix,
                                company_spelling_key)


ORG_WORDS = set('bank university school college institute association museum council department '
                'air airlines airways capital partners group management research hospital church '
                'agency authority hotel aviation securities media studio studios'.split())

# The extractor's implementation/signature stays untouched. These pure lookup
# functions are repeatedly called for the same labels during a build.
company_key = lru_cache(maxsize=32768)(company_key)


@lru_cache(maxsize=32768)
def org_signal(value):
    return has_company_form(value) or bool(set(normal_name(value).split()) & ORG_WORDS)


def website_domain(value):
    if email_address(value) or '@' in value or FILE.search(value):
        return None
    explicit = bool(re.match(r'^(?:https?://|www\.)', value, re.I))
    if not explicit and not re.fullmatch(r'[\w-]+(?:\.[\w-]+)+', value):
        return None
    host = urlsplit(value if '://' in value else 'https://' + value).hostname
    if host and re.fullmatch(r'(?:[a-z0-9-]+\.)+[a-z]{2,}', host, re.I):
        # Use the extractor's offline PSL validator; dotted personal names with
        # an unknown suffix are not URLs. Importing it does not run extraction.
        from extract_entities import domain_key
        return host.casefold().removeprefix('www.') if domain_key(host) else None
    return None


def wrong_type(value):
    """Unambiguous machine formats, not guessed person names."""
    if email_address(value):
        return 'email', {}
    if website_domain(value):
        return 'website', {}
    if re.fullmatch(r'(?:G(?:II|III|IV|V|VI|[2456]50|[2456]00)|CL60|B737|C560)', value):
        return 'vehicle', {'vehicle_type': 'aircraft'}
    if re.fullmatch(r'[A-Z]{2}\s+\d{5}(?:-\d{4})?', value):
        return 'location', {'place_type': 'postal_code', 'label_quality': 'not_a_name',
                            'label_quality_reason': 'postal_code_fragment'}
    return None, {}


def token_ocr_key(value):
    result = []
    for word in normal_name(value).split():
        # Constrain vocabulary repair to these organization words. Arbitrary
        # similar surnames/brands are never one-edit matches.
        skeleton = word.replace('rn', 'm').replace('l', 'i').replace('1', 'i')
        for canonical, variants in {
            'deutsche': {'deutsche', 'deutche', 'deutshe', 'deutscbe', 'deutscne', 'deuische', 'deutschebank'},
            'financial': {'financial', 'financiai', 'financia1', 'financiat', 'financlal', 'financla1', 'financiai'},
            'stearns': {'stearns', 'steams', 'stearns', 'steams'},
        }.items():
            if skeleton in {v.replace('rn', 'm').replace('l', 'i').replace('1', 'i') for v in variants}:
                word = canonical
                break
        result.append(word)
    return ' '.join(result)


class BuildIndex(ExistingIndex):
    def __init__(self, existing, gazetteer=None, key=None, lookup=None, evidence=None, airports=None, document_overlap=None):
        # The extractor's index loads a 394 MB lookup as JSON. Stream it here;
        # keep only aliases belonging to the small set of existing graph nodes.
        super().__init__(existing, gazetteer or {})
        self.evidence = evidence
        self.document_overlap = document_overlap
        self.given_names = set(FIRST_NAMES)
        for node in existing.values():
            tokens = normal_name(node['name']).split()
            if (node['entity_type'] == 'person' and len(tokens) >= 2 and not org_signal(node['name'])
                    and (isinstance(node['id'], int) or str(node['id']).isdigit())):
                self.given_names.add(tokens[0])
        self.airports = airports
        self.airport_targets = defaultdict(set)
        self.city_rows = defaultdict(list)
        self.airport_labels = defaultdict(set)
        if airports:
            from airports import name_key
            for ident, row in airports.rows.items():
                if row['type'] == 'closed':
                    continue
                self.city_rows[row['municipality'].casefold()].append(ident)
                # Official names often include International/Airport; ignore
                # these descriptors, never a person's given/surname tokens.
                label = re.sub(r'\binternational\b', '', name_key(row['name']))
                self.airport_labels[' '.join(label.split())].add(ident)
            for identity, node in existing.items():
                if node['entity_type'] in {'location', 'airport'}:
                    code = node.get('icao') or node.get('airport_ident')
                    if code:
                        self.airport_targets[code].add(identity)
                    for value in names(node):
                        ident, _ = airports.resolve(value)
                        if ident:
                            self.airport_targets[ident].add(identity)
        self.key = key
        self.email_owners = {}
        self.owners = {i: i for i in existing}
        for identity, node in existing.items():
            for member in node.get('merged_identities', []):
                self.owners[member['id']] = identity
        for member in list(self.owners):
            target, seen = self.owners[member], {member}
            while target in self.owners and self.owners[target] != target and target not in seen:
                seen.add(target)
                target = self.owners[target]
            self.owners[member] = target
            if existing.get(target, {}).get('entity_type') == 'email':
                self.email_owners[member] = target
        for identity, node in existing.items():
            if node['entity_type'] == 'email' and email_address(node['name']):
                self.emails[ocr_repair(node['name'])].add(identity)
        if lookup and lookup.exists():
            for identity, item in items(lookup):
                target = self.owners.get(identity)
                if target is None:
                    continue
                for value in [item['value'], *item.get('raw_spellings', [])]:
                    if item.get('kind') == 'email' and email_address(value):
                        self.emails[ocr_repair(email_address(value))].add(target)
                    elif item.get('kind') == 'participant':
                        self.exact[normal_name(value)].add(target)
        self.common_words = dictionary_words()
        self.plain_words = frozenset(self.common_words)  # without proper-name homographs (Post, Bailey)
        # Company-tail completion must also guard dictionary words that happen
        # to be surnames (Read -> Bread). Person labeling uses a different set.
        from pathlib import Path
        lexicon = Path('/usr/share/dict/words')
        if lexicon.exists():
            self.common_words |= {w for w in lexicon.read_text().splitlines() if w.islower()}
        self.suffixes, self.ocr_companies = defaultdict(set), defaultdict(set)
        self.spaced_companies = defaultdict(set)
        self.legal_spellings = defaultdict(set)
        for identity, node in existing.items():
            if node['entity_type'] not in COMPANIES or node.get('label_quality') == 'not_a_name':
                continue
            value = node['name']
            if clean_company_name(value) != value or self.company_reason(value) or wrong_type(value)[0]:
                continue
            base = company_key(value)
            self.legal_spellings[company_spelling_key(value)].add(identity)
            self.spaced_companies[normal_name(value).replace(' ', '')].add(identity)
            # Index suffixes starting inside the first word or at later words.
            # This also handles a dropped J.P., with a separate rule below.
            for start in range(1, min(5, max(1, len(base.split()[0]) - 2))):
                tail = base[start:]
                # At most four leading letters lost; the surviving first word is long and not a
                # dictionary word ("Read & Co." must not complete to Bread, "RAM" to OrganiGram).
                first = tail.split()[0]
                if (len(tail) >= 6 and tail[0].isalpha() and first not in GENERIC and len(first) >= 4
                        and first not in self.common_words):
                    self.suffixes[tail].add(identity)
            if token_ocr_key(value) == normal_name(value):
                self.ocr_companies[token_ocr_key(value)].add(identity)

    def stub_reason(self, value, dictionary):
        """Build-time hiding reason for an organization/participant label that is
        a lone common word, a 1-3 letter span, markup or form text.

        Applies the entity-layer label rules to nodes created by earlier layers.
        A given name, a reference-backed brand, anything with a legal form (the
        company rules own those) and a word with a matching website are kept.
        """
        reason = label_reason(value, dictionary)
        if reason is None or has_company_form(value):
            return None
        words = normal_name(value).split()
        if reason == 'short_span' and (value.casefold() in {n.casefold() for n in self.given_names}
                                       or len(words) == 1 and value[:1].isupper() and value[1:].islower()):
            return None
        if reason == 'dictionary_words':
            word = value.casefold()
            if len(words) != 1 or value.isupper() or normal_name(value) in self.aliases:
                return None
            if self.evidence and any(word + '.' + tld in self.evidence.domains for tld in ('com', 'org', 'net')):
                return None
        return reason

    def company_reason(self, value):
        return company_label_reason(value, bool(self.evidence and self.evidence.company_attested(value)))

    def person_shape(self, value):
        tokens = normal_name(value).split()
        proper = lambda token: bool(re.fullmatch(r"(?:[A-Z]{1,3}\.?|[A-Z][a-z]+(?:[A-Z][a-z]+)*|[A-Z]{2,})(?:[-'][A-Za-z]+)?", token))
        return (bool(tokens) and not org_signal(value) and
                (tokens[0] in self.given_names or 2 <= len(value.split()) <= 3 and all(proper(t) for t in value.split())))

    def organization_reference(self, identity, value):
        node = self.existing[identity]
        if node.get('label_quality') == 'not_a_name' or self.company_reason(node['name']):
            return False
        if not str(identity).startswith('entity_') or normal_name(value) in self.aliases:
            return True
        # A NER guess is weaker than a reviewed legal entity. A frequent,
        # distinctive organization spelling can serve as a brand reference;
        # rare names, ordinary words, and header acronyms cannot.
        words = set(normal_name(value).split())
        if metadata(node).get('document_count', 0) < 20 or value.isupper() and len(value) <= 5:
            return False
        if len(words) == 1:
            return bool(self.evidence and any(value.casefold() + '.' + suffix in self.evidence.domains
                                              for suffix in ('com', 'org', 'net')))
        institution = words & {'institute', 'university', 'college', 'museum', 'hospital', 'church'}
        return bool(institution or org_signal(value) and not words <= self.common_words)

    def cross_type(self, value, source_type, exclude=()):
        """Exact existing organization/place, otherwise a reference-backed place.

        No approximate person-to-company matching. A person-shaped label never
        resolves to an organization merely because an extracted company agrees.
        """
        excluded = set(exclude)
        if source_type not in {'person', 'participant', 'location', 'airport', 'property', *COMPANIES}:
            return None, None, {}, None
        detected, fields = wrong_type(value)
        if detected:
            return None, detected, fields, 'machine_format'
        exact = set(self.exact.get(normal_name(value), ())) - excluded
        places = {i for i in exact if self.existing[i]['entity_type'] in {'location', 'airport', 'property'}
                  and (not str(i).startswith('entity_') or self.existing[i].get('place_type')
                       or normal_name(value) in self.aliases)}
        target = self.choose(places)
        if target is not None:
            if source_type in {'location', 'airport', 'property'}:
                peers = places | {i for i in excluded if i in self.existing and
                                  normal_name(self.existing[i]['name']) == normal_name(value)}
                if self.choose(peers) in excluded:
                    return None, None, {}, None
            return target, 'location', {}, 'exact_existing_place'
        if source_type in {'person', 'participant'} and not self.person_shape(value):
            canonical = self.aliases.get(normal_name(value), value)
            company_hits = self.primary_company.get(company_key(canonical), set()) if not has_company_form(value) else set()
            firms = {i for i in (exact | self.hits(value) | company_hits) - excluded
                     if self.existing[i]['entity_type'] in COMPANIES
                     and self.organization_reference(i, value)
                     and (not str(i).startswith('entity_') or org_signal(value) or normal_name(value) in self.aliases
                          or len(normal_name(value).split()) == 1
                          or not set(normal_name(value).split()) & self.common_words)}
            target = self.choose(firms)
            if target is not None:
                return target, self.existing[target]['entity_type'], {}, 'exact_existing_organization'
        elif source_type == 'participant' and not has_company_form(value):
            # "Delta Air Lines" (mail header) beside "Delta Air Lines, Inc."
            # (company layer): capitalized words look like a person, but a
            # unique legal entity whose name is exactly this label plus a legal
            # form identifies the sender. The label must end in an ordinary
            # noun (Express, Hardware, Jets): "Fgan Hsigan" beside "Fred
            # Haddad, P.A." stays a person even when the given name is unlisted.
            words = normal_name(value).split()
            if (len(words) >= 2 and words[0] not in self.given_names
                    and (words[-1] in self.plain_words or words[-1] in ORG_WORDS)):
                firms = {i for i in set(self.primary_company.get(company_key(value), ())) - excluded
                         if self.existing[i]['entity_type'] in COMPANIES
                         and has_company_form(self.existing[i]['name'])
                         and normal_name(self.existing[i]['name']) != normal_name(value)
                         and self.organization_reference(i, self.existing[i]['name'])}
                if len(firms) == 1:
                    target = next(iter(firms))
                    return target, self.existing[target]['entity_type'], {}, 'legal_form_base_organization'
        return self.place_type(value, source_type, excluded)

    def place_type(self, value, source_type=None, exclude=()):
        if not self.airports or has_company_form(value) and not re.search(r'\s[A-Z]{2}$', value):
            return None, None, {}, None
        if value.isupper() and len(re.sub(r'[^A-Z]', '', value)) <= 5:
            return None, None, {}, None
        from airports import name_key
        ident, rule = self.airports.resolve(value)
        # Bare 3-letter person initials/company acronyms are not airport proof.
        if rule == 'code':
            # "EYW West Palm Beach, FL": a leading code counts only when the
            # words after it name that airport's own city or airport name.
            rest = re.sub(r'^[A-Z]{3,4}\s+', '', value.partition(',')[0]).casefold()
            row = self.airports.rows[ident] if ident else None
            if not (row and ' ' in value.strip() and rest and
                    re.search(r'\b' + re.escape(rest) + r'\b', (row['municipality'] or '').casefold() + ' | ' + row['name'].casefold())):
                ident = None
        candidates = self.airport_labels.get(name_key(value), set())
        if len(name_key(value).split()) < 2 and not re.search(r'\bairport\b', value, re.I):
            ident, candidates = None, set()
        if ident is None and len(candidates) == 1:
            ident = next(iter(candidates))
        if ident:
            fields = {'place_type': 'airport', 'icao': ident}
            targets = self.airport_targets.get(ident, set())
            # NER may have made both the short and the official spelling into
            # locations. They all resolved to this exact reference airport;
            # prefer the place layer's airport over another NER fragment.
            target = min(targets, key=lambda i: (
                self.existing[i].get('place_type') != 'airport',
                not bool(self.existing[i].get('icao') or self.existing[i].get('airport_ident')),
                str(i).startswith('entity_'), not isinstance(i, int), str(i))) if targets else None
            return target if target not in exclude else None, 'location', fields, 'airport_reference_name'
        head = value.strip()
        region = None
        match = re.fullmatch(r'(.+?)[, ]+([A-Z]{2})', head)
        if match:
            head, region = match.groups()
        candidates = self.city_rows.get(head.casefold(), ())
        if region:
            candidates = [i for i in candidates if self.airports.rows[i]['iso_region'].endswith('-' + region)]
        # Keep given names and ambiguous single-word person labels. Multiword
        # municipalities need a large-airport reference (e.g. Charlotte Amalie).
        if self.person_shape(head):
            candidates = [i for i in candidates if self.airports.rows[i]['type'] == 'large_airport']
        elif len(head.split()) == 1 and (head.casefold() in FIRST_NAMES or source_type in {'person', 'participant'}):
            candidates = []
        areas = {self.airports.rows[i]['iso_region'] for i in candidates}
        if len(areas) == 1:
            return None, 'location', {'place_type': 'city', 'region': next(iter(areas))}, 'airport_reference_municipality'
        return None, None, {}, None

    def hits(self, value):
        hits = super().hits(value)
        if not hits and hasattr(self, 'spaced_companies'):
            hits = set(self.spaced_companies.get(normal_name(value).replace(' ', ''), ()))
        if not hits and hasattr(self, 'legal_spellings'):
            hits = set(self.legal_spellings.get(company_spelling_key(value), ()))
        # A person label mistyped by NER is not a company. Never remove Trust,
        # Foundation, Fund, or another organizational word to match a person.
        exact = set(self.primary.get(normal_name(value), ())) | set(self.exact.get(normal_name(value), ()))
        firms = {i for i in exact if self.existing[i]['entity_type'] in COMPANIES
                 and self.existing[i].get('label_quality') != 'not_a_name'}
        if firms and not self.person_shape(value):
            primary = firms & self.primary.get(normal_name(value), set())
            preferred = {i for i in hits if self.existing[i]['entity_type'] in COMPANIES}
            return primary or preferred or firms
        if not org_signal(value) and len(normal_name(value).split()) >= 2:
            persons = {i for i in exact if self.existing[i]['entity_type'] in {'person', 'participant', 'survivor'}
                       and self.existing[i].get('label_quality') != 'not_a_name'}
            if persons:
                return persons
        if org_signal(value):
            firms = {i for i in hits if self.existing[i]['entity_type'] in COMPANIES}
            base = company_key(value)
            form = normal_name(value)[len(base):].strip()
            if form:
                firms = {i for i in firms if not (other_form := normal_name(self.existing[i]['name'])[len(company_key(self.existing[i]['name'])):].strip()) or form == other_form
                         or company_spelling_key(value) == company_spelling_key(self.existing[i]['name'])}
            if firms:
                return firms
            hits = {i for i in hits if self.existing[i]['entity_type'] not in COMPANIES}
        places = {i for i in exact if self.existing[i]['entity_type'] in {'location', 'airport', 'property'}}
        if places and not org_signal(value):
            return places
        return hits

    def choose(self, hits):
        target = super().choose(hits)
        if target is not None:
            return target
        if hits and len({normal_name(self.existing[i]['name']) for i in hits}) == 1:
            kinds = {self.existing[i]['entity_type'] for i in hits}
            if kinds <= {'person', 'participant'} or (len(kinds) > 1 and kinds <= {'location', 'airport', 'property'}):
                return min(hits, key=lambda i: (not isinstance(i, int), self.existing[i]['entity_type'] == 'participant', str(i)))
        return None

    def email_hits(self, value):
        if self.key is None:
            address = email_address(value)
            return set(self.emails.get(ocr_repair(address), ())) if address else set()
        return super().email_hits(value)

    def company_repair(self, value, exclude=()):
        """Return (clean spelling, unique target, reason), frozen references only."""
        clean = clean_company_name(value)
        excluded = set(exclude)

        def targets(label):
            hits = set(self.hits(label)) - excluded
            if not hits:
                # An extracted bare brand's own primary key used to mask the
                # independently existing formed spelling after self-exclusion.
                if normal_name(label)[len(company_key(label)):].split() in ([], ['co']):
                    candidates = set(self.primary_company.get(company_key(label), ())) - excluded
                    label_form = normal_name(label)[len(company_key(label)):].strip()
                    brand = company_key(label).split()
                    # "Company" and "Corporation" can be different successor firms
                    # (General Motors Company/Corporation): an explicit form only
                    # completes to a privately-held form of the same brand.
                    candidates = {i for i in candidates if self.organization_reference(i, label)
                                  and (not label_form or set(normal_name(self.existing[i]['name'])[len(company_key(self.existing[i]['name'])):].split())
                                       <= {'co', 'inc', 'llc', 'ltd', 'lp', 'llp', 'pllc', 'pa', 'pc'})
                                  and not any(w.isdigit() for w in brand) and not set(brand) <= GENERIC}
                    # Mentions of a short form and its formal spelling seldom share
                    # a page. A multi-word brand with exactly one formed spelling
                    # is unambiguous; a one-word brand or several legal forms
                    # (Inc vs LLC) need shared documents.
                    if len(candidates) == 1 and (len(brand) >= 2 or '&' in label):
                        hits = candidates
                    else:
                        hits = {i for i in candidates
                                if any(self.document_overlap and self.document_overlap.supports(source, i)
                                       for source in excluded)}
                hits |= self.legal_spellings.get(company_spelling_key(label), set()) - excluded
            return {i for i in hits
                    if (self.existing[i]['entity_type'] not in COMPANIES or not self.company_reason(self.existing[i]['name']))
                    and (len(normal_name(label).split()) >= 2
                         or self.existing[i]['entity_type'] not in {'person', 'participant', 'survivor'})}

        hits = targets(clean)
        target = self.choose(hits)
        if target is not None and self.company_reason(clean) and self.existing[target]['entity_type'] in COMPANIES \
                and normal_name(self.existing[target]['name']) != normal_name(clean):
            # Generic stubs differing only by legal form ("I LLC" / "LLC") stay separate hidden nodes.
            target = None
        if target is not None:
            if company_spelling_key(clean) == company_spelling_key(self.existing[target]['name']):
                # Exact/equivalent duplicates always point in one direction.
                # Otherwise two retained nodes would nominate each other and
                # the viewer's cycle guard would keep both.
                peers = {i for i in excluded if i in self.existing and
                         self.existing[i]['entity_type'] in COMPANIES and
                         company_spelling_key(self.existing[i]['name']) == company_spelling_key(clean)}
                rank = lambda i: (not isinstance(i, int), str(i).startswith('entity_'),
                                  str(i).startswith('participant_'), -metadata(self.existing[i]).get('document_count', 0), str(i))
                if peers and min(peers | {target}, key=rank) in peers:
                    return clean, None, None
            return clean, target, 'company_clean_or_exact'
        if (self.evidence and self.evidence.company_attested(value)
                and re.match(r'^(?:FAO|FBO)\s', value)):
            return clean, None, None
        tail, hits = company_tail(clean, targets, self.company_reason)
        target = self.choose(hits)
        if target is not None:
            return tail, target, 'company_noise_prefix'
        # A digit standing in for an ampersand must match a real full spelling.
        amp = re.sub(r'(?<=\s)[68](?=\s)', '&', clean)
        variants = targets(amp) if amp != clean else set()
        variants |= self.ocr_companies.get(token_ocr_key(clean), set()) - excluded
        target = self.choose(variants)
        if target is not None:
            return self.existing[target]['name'], target, 'company_ocr_word'
        base = company_key(clean)
        if not self.company_reason(clean) and len(base) >= 6:
            hits = self.suffixes.get(base, set()) - excluded
            if base.startswith('morgan '):
                for prefix in ('j p ', 'jp '):
                    hits |= self.primary_company.get(prefix + base, set()) - excluded
            # Suffix completion must preserve the legal form if one is present.
            form = normal_name(clean)[len(base):].strip()
            hits = {i for i in hits if not form or normal_name(self.existing[i]['name']).endswith(' ' + form)}
            # If Northern and Southern both exist, THERN is deliberately unresolved.
            target = self.choose(hits)
            if target is not None:
                return self.existing[target]['name'], target, 'company_unique_left_fragment'
        return clean, None, None


def viewer_references(out, labels, gazetteer=None):
    """Select canonical references without loading the combined graph.

    Unlinked organizations may be absent from the viewer while their linked
    participant duplicates remain. Only references matching viewer spellings
    or gazetteer aliases are retained from the streamed layer files.
    """
    wanted = {normal_name(v) for v in labels}
    for canonical, spec in (gazetteer or {}).items():
        if any(normal_name(v) in wanted for v in [canonical, *spec.get('aliases', [])]):
            wanted.add(normal_name(canonical))
    bases = {company_key(v) for v in wanted}
    legal_spellings = {company_spelling_key(v) for v in wanted}
    references = {}
    for filename in ('normalized_entities.json', 'company_nodes.json', 'place_nodes.json',
                     'email_place_nodes.json', 'entity_nodes.json'):
        path = out / filename
        if not path.exists():
            continue
        for node in items(path):
            if node['entity_type'] not in COMPANIES | {'location', 'airport', 'property'} or node.get('label_quality') == 'not_a_name':
                continue
            if any(normal_name(v) in wanted or node['entity_type'] in COMPANIES and
                   (company_key(v) in bases or company_spelling_key(v) in legal_spellings) for v in names(node)):
                references.setdefault(node['id'], node)
    return references


def website_org_links(nodes):
    """Exact domain/base-name equality is an association, never an identity."""
    nodes = list(nodes)
    firms = defaultdict(set)
    for node in nodes:
        if (node['entity_type'] in COMPANIES and node.get('label_quality') != 'not_a_name'
                and not text_reason(node['name'])):
            # Digits stay: "Point 15" / "Bill 7" are not point.com / bill.com.
            base = re.sub(r'[^a-z0-9]', '', company_key(node['name']))
            if len(base) >= 4:
                firms[base].add(node['id'])
    links = []
    for node in nodes:
        if node['entity_type'] != 'website' or node.get('label_quality') == 'not_a_name':
            continue
        host = website_domain(node['name'])
        if not host:
            continue
        parts = host.split('.')
        base = parts[-3] if len(parts) >= 3 and '.'.join(parts[-2:]) in {'co.uk', 'org.uk', 'com.au'} else parts[-2]
        for target in sorted(firms.get(base, ()), key=str):
            links.append({'id': f'website_of_{node["id"]}_{target}', 'source_entity_id': node['id'],
                          'target_entity_id': target, 'relationship_type': 'website_of', 'weight': 1,
                          'metadata': {'rule': 'exact domain label and squeezed company base; not verified ownership'}})
    return links
