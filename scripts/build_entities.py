"""Aggregate entity mentions and score complete co-mention evidence on disk.

Only the viewer edge export is capped. Single-document non-gazetteer mentions
remain in the extraction database. Configured survivors are excluded.
"""
import argparse
from collections import Counter, defaultdict
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from build_companies import normal_name, contains_name
from build_contacts import contact_key
from build_communications import Builder, strip_name_tail, family_ocr_key, edit_distance, domain_fragment, legal_form, WEBMAIL_DOMAINS, OCR_CONFUSABLE
from normalize_graph import name_key, registry_aliases
from survivor_policy import survivor_ids
from entity_labels import RULES, cannot_be_name, dictionary_words, label_reason, short_label
from entity_resolution import read_nodes, ExistingIndex, canonical_place, person_kind, email_address, names, COMPANIES, FIRST_NAMES
from entity_build_rules import (clean_label, clean_company_name, company_label_reason, company_tail,
                                text_reason, website_reason, website_ocr_target, has_company_form)
from entity_build_resolution import BuildIndex, wrong_type, website_domain, website_org_links

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output'


HEADING_WORDS = {'relationship', 'request', 'ia', 'report', 'statement', 'summary', 'account', 'information'}


def build_label_reason(name):
    """Builder-only boilerplate and known OCR domain remnants, including URLs."""
    if re.match(r'^(?:(?:kind|best|warm|warmest) regards|sent from my (?:iphone|ipad))\b', name, re.I):
        return 'correspondence_boilerplate'
    # A public suffix alone cannot distinguish these known fragments from real
    # registrations. Do not hide arbitrary short domains (db.com, un.org, ...).
    if name.casefold() in {'ail.com', 'by.net', 'the.net'} or (domain_fragment(name) and name.casefold() not in {*WEBMAIL_DOMAINS, 'mail.com'}):
        return 'domain_fragment'
    return None


def existing_head_hits(name, index):
    """Trim only to a complete existing name; never invent a shortened node."""
    base = re.sub(r"['’]s$", '', name, flags=re.I)
    if base != name and (hits := index.hits(base)):
        return hits, 'possessive'
    cut = strip_name_tail(base)
    if cut and (hits := index.hits(cut[0])):
        return hits, 'existing_name_tail'
    words = base.split()
    for end in range(len(words) - 1, 1, -1):
        if not set(normal_name(' '.join(words[end:])).split()) <= HEADING_WORDS:
            break
        if hits := index.hits(' '.join(words[:end])):
            return hits, 'existing_heading_tail'
    return set(), None


class OrganizationSpellings:
    """Frozen OCR references, bucketed without changing distance/tie rules."""
    def __init__(self, references):
        self.plain, self.family = defaultdict(set), defaultdict(set)
        for reference in references:
            group = (len(reference.split()), legal_form(reference)[1])
            self.plain[*group, len(reference)].add(reference)
            self.family[*group, len(family_ocr_key(reference))].add(reference)

    def candidates(self, normalized):
        group = (len(normalized.split()), legal_form(normalized)[1])
        hits = set()
        for buckets, length in ((self.plain, len(normalized)), (self.family, len(family_ocr_key(normalized)))):
            for size in range(length - 2, length + 3):
                hits.update(buckets.get((*group, size), ()))
        return hits


def organization_ocr_hits(name, index, references):
    normalized = normal_name(name)
    if len(normalized) < 12 or len(normalized.split()) < 2:
        return set()
    best, hits = 3, set()
    for reference in references.candidates(normalized) if isinstance(references, OrganizationSpellings) else references:
        if len(reference.split()) != len(normalized.split()):
            continue
        if legal_form(normalized)[1] != legal_form(reference)[1]:
            continue
        distance = min(edit_distance(normalized, reference, 2),
                       edit_distance(family_ocr_key(normalized), family_ocr_key(reference), 2))
        if distance <= 2:
            targets = {i for i in index.hits(reference) if index.existing[i]['entity_type'] in COMPANIES
                       and index.existing[i].get('label_quality') != 'not_a_name'}
            if distance < best:
                best, hits = distance, targets
            elif distance == best:
                hits |= targets
    return hits


def pair_samples(db, a, b, document_count):
    """Same eight cluster samples, starting with the less frequent endpoint.

    CROSS JOIN keeps SQLite from choosing the popular name as the outer loop.
    Crowded documents remain eligible here, just as in the original query.
    """
    if document_count(b) < document_count(a):
        a, b = b, a
    return [r[0] for r in db.execute('''SELECT min(x.efta_number)
        FROM clustered_mentions x CROSS JOIN clustered_mentions y
        ON y.efta_number=x.efta_number AND y.entity_id=?
        WHERE x.entity_id=? GROUP BY x.cluster ORDER BY x.cluster LIMIT 8''', (b, a))]


COMPANY_TLDS = frozenset({'llc', 'llp', 'inc', 'co', 'ltd', 'corp', 'lp'})
SQUASH_FAMILIES = {'organization': 'org', 'shell_company': 'org', 'location': 'place', 'property': 'place', 'airport': 'place'}


def squash(text):
    return re.sub(r'[^0-9a-z]', '', str(text).casefold())


def squash_merges(db, existing, protected, summary):
    """Spacing/punctuation OCR variants share one node: "TD Bank" = "TDBank",
    "Chase& Co" = existing "Chase & Co.", "DONALDTRUMP" = person Donald Trump,
    website "coleman.llp" = "COLEMAN LLP". Letters and digits must be identical.
    """
    known = defaultdict(set)
    for identity, node in existing.items():
        family = SQUASH_FAMILIES.get(node['entity_type']) or ('person' if node['entity_type'] == 'person' else None)
        if family and node.get('label_quality') != 'not_a_name':
            for value in names(node):
                key = squash(value)
                if len(key) >= 4:
                    known[family, key].add(identity)
    groups = defaultdict(list)
    for identity, name, entity_type, payload in db.execute('SELECT id,name,entity_type,metadata FROM entities'):
        if identity in existing:
            continue
        family = SQUASH_FAMILIES.get(entity_type)
        if entity_type == 'website':
            family = 'org' if name.rpartition('.')[2] in COMPANY_TLDS else None
        key, meta = squash(name), json.loads(payload)
        if family and len(key) >= 4 and re.search('[a-z]', key) and not meta.get('label_quality'):
            groups[family, key].append((identity, name, entity_type, meta))
    moves = []
    for (family, key), members in groups.items():
        targets = {t for t in known.get((family, key), ()) if t not in protected}
        if not targets and family == 'org' and len(key) >= 8:
            # Only a glued spelling can be a person ("Danielsiad"); "Ralph Lauren"
            # the company keeps its own node.
            if all(not re.search(r'\s', name) for _, name, _, _ in members):
                targets = {t for t in known.get(('person', key), ()) if t not in protected}
        if len(targets) == 1:
            target = next(iter(targets))
            node = dict(existing[target])
            meta = node.get('metadata') or {}
            node['metadata'] = json.loads(meta) if isinstance(meta, str) else dict(meta)
            db.execute('INSERT OR IGNORE INTO entities VALUES (?,?,?,?,?)',
                       (target, node['name'], node['entity_type'], 0, json.dumps(node)))
            reason = 'squashed_existing_variant'
        elif len(targets) > 1 or len(members) < 2:
            continue
        else:
            target = min(members, key=lambda m: (m[2] == 'website', ' ' not in m[1].strip(),
                                                 -(m[3].get('metadata') or {}).get('document_count', 0), str(m[0])))[0]
            reason = 'squashed_spelling_variant'
        target_type = db.execute('SELECT entity_type FROM entities WHERE id=?', (target,)).fetchone()[0]
        for identity, name, _, _ in members:
            if identity == target:
                continue
            for (k,) in db.execute('SELECT key FROM key_nodes WHERE entity_id=?', (identity,)).fetchall():
                db.execute('INSERT OR REPLACE INTO folds VALUES (?,?,?,?,?)', (k, name, target, target_type, reason))
            moves.append((target, identity))
            summary[reason] += 1
    db.executemany('UPDATE key_nodes SET entity_id=? WHERE entity_id=?', moves)


def person_ocr_key(name):
    """Spelling key that ignores OCR look-alikes and spacing: "lean Luc Brunel" =
    "Jean-Luc Brunel", "Mariana Idikowslca" = "Mcegupa Idortodor"."""
    text = ''.join(re.findall(r'[a-z0-9]+', name.casefold()))
    return family_ocr_key(text).replace('k', 'lc').replace('w', 'vv').translate(OCR_CONFUSABLE)


def person_ocr_eligible(value):
    """Two words or more, a last word of >=3 letters and no digits: initials and
    numbers ("David LI" / "DAVID H", "Exhibit 6" / "Exhibit B") are not look-alikes."""
    words = value.split()
    return (len(words) >= 2 and len(re.sub(r'[^A-Za-z]', '', words[-1])) >= 3 and not re.search(r'\d', value)
            and len(person_ocr_key(value)) >= 8)


def middle_initials(value):
    words = value.split()
    return tuple(w[0].casefold() for w in words[1:-1] if len(re.sub(r'[^A-Za-z]', '', w)) == 1)


def person_group_key(value):
    """person_ocr_key without middle initials: "Stephen R. Adorcefoc" = "Stephen Adorcefoc"."""
    words = value.split()
    return person_ocr_key(' '.join([words[0], *(w for w in words[1:-1] if len(re.sub(r'[^A-Za-z]', '', w)) != 1), words[-1]]))


def person_ocr_merges(db, existing, protected, person_names, summary):
    """OCR spellings of one person created as separate nodes. A rarer spelling
    joins the single existing named person with the same key, else a spelling
    with >=3x its documents. Different real given names (Anna/Anne) stay apart."""
    groups = defaultdict(list)
    for identity, node in existing.items():
        if node['entity_type'] == 'person' and identity not in protected and node.get('label_quality') != 'not_a_name':
            for value in names(node):
                if person_ocr_eligible(value):
                    groups[person_group_key(value)].append((identity, value, None, False))
    for identity, value in person_names.items():
        found = db.execute('SELECT document_count,metadata FROM entities WHERE id=?', (identity,)).fetchone()
        if found and person_ocr_eligible(value):
            hidden = bool(json.loads(found[1]).get('label_quality'))  # may move, never receive
            groups[person_group_key(value)].append((identity, value, found[0], hidden))
    moves = []
    for members in groups.values():
        if len({m[0] for m in members}) < 2:
            continue
        known = {m[0] for m in members if m[2] is None}
        if len(known) > 1:
            continue
        if known:
            target = next(iter(known))
            target_name, floor = next(m[1] for m in members if m[0] == target), float('inf')
        else:
            visible = [m for m in members if not m[3]]
            if not visible:
                continue
            target, target_name, documents, _ = max(visible, key=lambda m: (m[2], str(m[0])))
            floor = documents / 3
        target_type = existing[target]['entity_type'] if target in existing else 'person'
        if target in existing:
            node = dict(existing[target])
            meta = node.get('metadata') or {}
            node['metadata'] = json.loads(meta) if isinstance(meta, str) else dict(meta)
            db.execute('INSERT OR IGNORE INTO entities VALUES (?,?,?,?,?)',
                       (target, node['name'], node['entity_type'], 0, json.dumps(node)))
        first = target_name.split()[0].casefold()
        for identity, value, documents, hidden in members:
            if identity == target or documents is None or documents > floor and not hidden:
                continue
            given = value.split()[0].casefold()
            if given != first and (given in FIRST_NAMES and first in FIRST_NAMES
                                   or min(len(re.sub(r'[^a-z]', '', given)), len(re.sub(r'[^a-z]', '', first))) <= 1):
                continue  # Anna/Anne, J./L. Ackerman: different people
            if middle_initials(value) and middle_initials(target_name) and middle_initials(value) != middle_initials(target_name):
                continue  # Stephen R. / Stephen P. Alexander
            for (k,) in db.execute('SELECT key FROM key_nodes WHERE entity_id=?', (identity,)).fetchall():
                db.execute('INSERT OR REPLACE INTO folds VALUES (?,?,?,?,?)',
                           (k, 'HeaderLabel_' + hashlib.sha256(k.encode()).hexdigest()[:20], target, target_type, 'person_ocr_variant'))
            moves.append((target, identity))
            summary['person_ocr_variants'] += 1
    db.executemany('UPDATE key_nodes SET entity_id=? WHERE entity_id=?', moves)


def key_forms(wanted, db, websites):
    """One scan instead of a query per key: {key: (kind, spelling, subtype)} with
    the preferred form, and {website key: its spellings}."""
    best, rank, spellings = {}, {}, defaultdict(set)
    for k, kind, spelling, subtype, gazetteer, n in db.execute(
            'SELECT key,kind,spelling,subtype,max(gazetteer),sum(n) FROM extracted.mentions GROUP BY key,kind,spelling,subtype'):
        if k in websites:
            spellings[k].add(spelling)
        if k not in wanted:
            continue
        order = (-gazetteer, kind == 'unknown_word', -n, kind, spelling, subtype is not None, subtype or '')
        if k not in rank or order < rank[k]:
            rank[k], best[k] = order, (kind, spelling, subtype)
    return best, spellings


def build(mentions, out, existing, survivors, private, key, view_edges=8000, per_node=8, clusters=None, relationships=(),
          max_entities_per_doc=40, evidence=None, airports=None):
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'entity_graph.partial.db'
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    # A throwaway .partial file: no crash journal or fsync. The bigger cache is for the 18M-row index builds.
    db.executescript('''PRAGMA temp_store=FILE; PRAGMA cache_size=-512000; PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE entities(id PRIMARY KEY, name TEXT, entity_type TEXT, document_count INTEGER, metadata TEXT);
        CREATE TABLE key_nodes(key TEXT PRIMARY KEY, entity_id);
        CREATE TABLE folds(key TEXT PRIMARY KEY, span TEXT, entity_id, target_type TEXT, reason TEXT);
        CREATE TABLE mentions(entity_id, efta_number TEXT, PRIMARY KEY(entity_id,efta_number));''')
    db.execute('ATTACH DATABASE ? AS extracted', (str(mentions),))
    gaz = json.loads((ROOT / 'config/entity_gazetteer.json').read_text())
    if evidence:
        evidence.prepare_companies(n['name'] for n in existing.values() if n['entity_type'] in COMPANIES)
    index = BuildIndex(existing, gaz, key, lookup=private.with_name('contact_lookup.json'), evidence=evidence, airports=airports)
    protected = survivor_ids(list(existing.values()), relationships, key=key)
    dictionary = dictionary_words()
    gaz_aliases = set(index.aliases)
    registry = ROOT / 'data/raw/persons_registry.json'
    if registry.exists():
        people = [{**n, 'normalized_name': n.get('normalized_name') or name_key(n['name'])} for n in existing.values() if n['entity_type'] == 'person']
        for i, aliases in registry_aliases(people, json.loads(registry.read_text())).items():
            for alias in aliases:
                index.exact[normal_name(alias)].add(i)
    raw = sqlite3.connect(f'file:{private}?mode=ro', uri=True) if private.exists() else None
    @lru_cache(maxsize=32768)
    def private_name(mention_key, spelling):
        if raw and mention_key.startswith('participant_'):
            found = raw.execute('SELECT spelling FROM spellings WHERE key=? ORDER BY n DESC,spelling LIMIT 1', (mention_key,)).fetchone()
            if found:
                return found[0]
        return spelling
    references = OrganizationSpellings({normal_name(value) for node in existing.values() if node['entity_type'] in COMPANIES and node.get('label_quality') != 'not_a_name'
                  for value in names(node) if len(normal_name(value)) >= 12 and len(normal_name(value).split()) >= 2})
    # Include single-document full names, even private HMAC spellings, as anchors.
    db.execute('CREATE TEMP TABLE existing_keys(key,spelling,tail,entity_id,PRIMARY KEY(key,spelling,tail,entity_id))')
    db.execute('CREATE TEMP TABLE person_anchors(key,spelling,surname,given_name,PRIMARY KEY(key,spelling,surname))')
    db.execute('CREATE TEMP TABLE surname_candidates(key PRIMARY KEY,surname)')
    for mention_key, spelling, mention_kind in db.execute('SELECT DISTINCT key,spelling,kind FROM extracted.mentions'):
        full_name = private_name(mention_key, spelling)
        normalized = normal_name(full_name)
        hits = index.hits(full_name)
        identities = index.full_names.get(normalized, set()) & hits
        for start in range(1, len(normalized.split()) - 2):
            tail = ' '.join(normalized.split()[start:])
            for identity in identities:
                db.execute('INSERT OR IGNORE INTO existing_keys VALUES (?,?,?,?)', (mention_key, spelling, tail, identity))
        if len(normalized.split()) >= 2:
            if person_kind(full_name, mention_kind) == 'PERSON':
                db.execute('INSERT OR IGNORE INTO person_anchors VALUES (?,?,?,?)',
                           (mention_key, spelling, normalized.split()[-1], normalized.split()[0]))
            for identity in hits:
                if existing[identity]['entity_type'] == 'person':
                    db.execute('INSERT OR IGNORE INTO existing_keys VALUES (?,?,?,?)',
                               (mention_key, spelling, normalized.split()[-1], identity))
    private_name.cache_clear()
    db.executescript('''CREATE INDEX existing_keys_tail ON existing_keys(tail,entity_id);
        CREATE INDEX person_anchors_surname ON person_anchors(surname,key,spelling);
        CREATE INDEX person_anchors_given ON person_anchors(given_name,key);''')
    db.execute('CREATE INDEX IF NOT EXISTS extracted.mentions_page ON mentions(doc_id,page_id,key)')
    summary = Counter()
    participant_builder = Builder(key, [])
    website_counts = dict(db.execute('SELECT key,count(DISTINCT doc_id) FROM extracted.mentions WHERE kind="website" GROUP BY key'))
    if evidence:
        evidence.prepare_domains(website_counts)
    website_buckets = defaultdict(dict)
    for domain, count in website_counts.items():
        base, _, suffix = domain.rpartition('.')
        website_buckets[len(base), suffix][domain] = count
    from ocr_consensus import Consensus  # spellings re-read from the scans by >=2 OCR engines
    scan_spellings = Consensus()
    person_names = {}  # new person node -> its (private) spelling, for OCR-variant merging
    eligible = db.execute('SELECT key,count(DISTINCT doc_id),max(gazetteer) FROM extracted.mentions GROUP BY key HAVING count(DISTINCT doc_id)>=2 OR max(gazetteer)=1').fetchall()
    best_form, website_spellings = key_forms({k for k, _, _ in eligible}, db, set(website_counts))
    for k, documents, is_gaz in eligible:
        kind, spelling, subtype = best_form[k]
        name = spelling
        if (k.startswith('participant_') or kind == 'email') and raw:
            found = raw.execute('SELECT spelling FROM spellings WHERE key=? ORDER BY n DESC,spelling LIMIT 1', (k,)).fetchone()
            if found:
                name = found[0]
        if kind == 'gazetteer':
            name = next(n for n in gaz if normal_name(n) == k)
        elif kind == 'website':
            name = k
        # Defense for older extraction DBs too: these keys must not merge into
        # existing nodes or contribute graph mention/pair counts.
        impossible = cannot_be_name(name)
        if impossible:
            summary['hidden_impossible_keys'] += 1
        if any(name_key(name) == name_key(s) or contains_name(name, s) for s in survivors):
            summary['survivors_excluded'] += 1
            continue
        original_name = name
        name = clean_label(scan_spellings.correct(name))
        if kind == 'ORG':
            name = clean_company_name(name)
        detected_type, type_fields = wrong_type(name)
        cross_target, cross_kind, cross_fields, cross_rule = index.cross_type(
            name, 'person' if kind in ('PERSON', 'person_weak', 'unknown_word') else 'organization' if kind == 'ORG' else kind)
        if cross_kind == 'location':
            detected_type, type_fields, kind = cross_kind, cross_fields, 'GPE'
        if detected_type == 'website':
            name, kind = website_domain(name), 'website'
        elif detected_type == 'vehicle':
            kind = 'aircraft_code'
        elif detected_type == 'location' and type_fields.get('place_type') == 'postal_code':
            kind = 'postal_code'
        if kind == 'website':
            base, _, suffix = name.rpartition('.')
            name = website_ocr_target(name, website_buckets[len(base), suffix])
        hits = index.hits(name)
        if cross_target is not None:
            hits = {cross_target}
        elif cross_kind == 'location':
            hits = {i for i in hits if existing[i]['entity_type'] in {'location', 'airport', 'property'}}
        if detected_type in ('website', 'vehicle', 'location'):
            hits = {i for i in hits if existing[i]['entity_type'] == detected_type}
        fold_reason = cross_rule or 'name_or_company_key'
        is_email = kind == 'email' or email_address(name) is not None or '@' in name
        if is_email:
            hits = index.email_hits(name)
            kind = 'email'
            fold_reason = 'ocr_email_variant'
        else:
            if not hits and kind == 'ORG':
                repaired, repair_target, repair_rule = index.company_repair(name)
                if repair_target is not None:
                    name, hits, fold_reason = repaired, {repair_target}, repair_rule
            if not hits:
                hits, tail_reason = existing_head_hits(name, index)
                if hits:
                    fold_reason = tail_reason
            if normal_name(name) == 'state of new york':
                name, hits, kind = 'New York', set(), 'GPE'
            elif normal_name(name) == 'new york jfk':
                airport_hits = index.locations('JFK') | index.locations('John F. Kennedy International Airport')
                if airport_hits:
                    name, hits, kind = 'John F. Kennedy International Airport', airport_hits, 'FAC'
            place_name = canonical_place(name, index.distinct_city)
            place_hits = index.locations(name) or index.locations(place_name)
            if place_hits:
                name = place_name
                hits = place_hits
                kind = 'GPE'
                fold_reason = 'existing_place'
            elif place_name != name or place_name in {'United States', 'New York', 'New York City', 'Manhattan'}:
                name, kind = place_name, 'GPE'
                hits = set()
            if not hits:
                tails = set(index.tails.get(normal_name(name), ()))
                if len(normal_name(name).split()) == 1:
                    tails |= {r[0] for r in db.execute('SELECT DISTINCT entity_id FROM existing_keys WHERE tail=?', (normal_name(name),))}
                hits = {i for i in tails if db.execute('''SELECT 1 FROM extracted.mentions m
                    JOIN extracted.mentions f ON m.doc_id=f.doc_id AND m.page_id=f.page_id
                    JOIN existing_keys e ON e.key=f.key AND e.spelling=f.spelling
                    WHERE m.key=? AND e.entity_id=? AND e.tail=? LIMIT 1''', (k, i, normal_name(name))).fetchone()}
                if hits:
                    fold_reason = 'same_page_surname' if len(normal_name(name).split()) == 1 else 'same_page_left_fragment'
            if not hits and kind not in ('PERSON', 'person_weak', 'website', 'GPE', 'LOC'):
                hits = organization_ocr_hits(name, index, references)
                if hits:
                    fold_reason = 'ocr_organization_variant'
            known = any(existing[i]['entity_type'] in ('location','organization','shell_company') for i in hits)
            kind = person_kind(name, kind, known or normal_name(name) in gaz_aliases)
        exempt = (bool(is_gaz) or normal_name(name) in gaz_aliases or kind == 'website'
                  or not short_label(name) and (cross_target is not None and cross_kind in COMPANIES or cross_kind == 'location'))
        reason = ('impossible_span' if impossible else None) or type_fields.get('label_quality_reason') or text_reason(name) or build_label_reason(name) or ('unresolved_email' if is_email and len(hits) != 1 else 'person_weak' if kind == 'person_weak' else None if exempt or kind == 'aircraft_code' else label_reason(name, dictionary))
        if kind == 'website':
            spellings = website_spellings[k]
            reason = website_reason(name, spellings, dictionary, bool(evidence and name in evidence.domains)) or reason
        else:
            if kind == 'ORG' and has_company_form(name):
                reason = index.company_reason(name) or reason
        if reason == 'short_span':
            # A short code can only reuse an existing geographic identity.
            # Never let e.g. CA fold to an organization/person named CA.
            targets = RULES['geographic_codes'].get(name.replace('.', '').upper(), [])
            hits = set(hits) if targets else set()
            for target in targets:
                hits |= index.locations(target)
            hits = {i for i in hits if existing[i]['entity_type'] == 'location'}
            if len(hits) == 1:
                reason = None
        elif reason:
            hits = set()
        target = index.choose(hits)
        # Canonical place variants share a new identity too, without making short
        # ambiguous geographic codes eligible in the absence of an existing node.
        new_key = name if kind == 'website' else normal_name(name) if kind in ('GPE','LOC','FAC','ORG') and name != original_name else k
        if kind in ('PERSON', 'person_weak') and not k.startswith('participant_'):
            new_key = participant_builder.participant_id(name_key(name))
        identity = target if target is not None else new_key if new_key.startswith('participant_') else 'entity_' + hashlib.sha256(new_key.encode()).hexdigest()[:20]
        if identity in protected:
            continue
        if identity in existing:
            node = dict(existing[identity])
            summary['folded_keys'] += 1
            summary['folded_' + node['entity_type']] += 1
            # Raw personal/email spellings remain in the private input only.
            span = spelling if kind not in ('PERSON', 'person_weak', 'email', 'unknown_word') else ('Email_' if kind == 'email' else 'HeaderLabel_') + hashlib.sha256(k.encode()).hexdigest()[:20]
            db.execute('INSERT INTO folds VALUES (?,?,?,?,?)', (k, span, identity, node['entity_type'], fold_reason))
        else:
            entity_type = 'website' if kind == 'website' else 'email' if kind == 'email' else 'vehicle' if kind == 'aircraft_code' else 'person' if kind in ('PERSON', 'person_weak') else 'location' if kind in ('GPE','LOC','FAC','postal_code') else 'participant' if kind == 'unknown_word' else 'organization'
            safe_name = ('HeaderLabel_' if kind != 'email' else 'Email_') + identity.split('_', 1)[1] if str(identity).startswith('participant_') or kind == 'email' else name
            node = {'id': identity, 'name': safe_name, 'entity_type': entity_type, 'source': 'Full-text entity mentions; NER is unverified'}
            node.update(type_fields)
            if kind == 'unknown_word' or reason:
                node['label_quality'] = 'not_a_name'
                node['label_quality_reason'] = reason or 'unknown_word'
            if kind == 'website' and name != k:
                db.execute('INSERT INTO folds VALUES (?,?,?,?,?)', (k, spelling, identity, entity_type, 'ocr_website_variant'))
                summary['ocr_website_keys'] += 1
            summary['created_' + entity_type] += 1
            if not reason and not is_email and kind != 'website' and len(normal_name(name).split()) == 1:
                db.execute('INSERT INTO surname_candidates VALUES (?,?)', (k, normal_name(name)))
        if subtype and identity not in existing:
            node['subtype'] = subtype
        elif node['entity_type'] == 'organization' and identity not in existing:
            node.setdefault('subtype', 'ner')
        meta = node.get('metadata') or {}
        meta = json.loads(meta) if isinstance(meta, str) else dict(meta)
        meta.update({'entity_mention_kind': kind, 'document_count': documents, 'gazetteer': bool(is_gaz)})
        node['metadata'] = meta
        db.execute('INSERT OR IGNORE INTO entities VALUES (?,?,?,?,?)', (identity, node['name'], node['entity_type'], documents, json.dumps(node)))
        db.execute('INSERT INTO key_nodes VALUES (?,?)', (k, identity))
        if identity not in existing and kind in ('PERSON', 'person_weak'):
            person_names.setdefault(identity, name)
    if raw:
        raw.close()
    # Full people first created in this build can also anchor a surname. Resolve
    # against a frozen set of full names, never against another fragment/fold.
    pending = []
    for fragment_key, surname in db.execute('SELECT key,surname FROM surname_candidates'):
        anchors = list(db.execute('''SELECT DISTINCT k.entity_id,p.given_name,e.document_count FROM extracted.mentions m
            JOIN extracted.mentions f ON m.doc_id=f.doc_id AND m.page_id=f.page_id
            JOIN person_anchors p ON p.key=f.key AND p.spelling=f.spelling AND p.surname=?
            JOIN key_nodes k ON k.key=f.key JOIN entities e ON e.id=k.entity_id
            WHERE m.key=? AND e.entity_type IN ('person','participant') AND k.entity_id !=
                (SELECT entity_id FROM key_nodes WHERE key=?)''', (surname, fragment_key, fragment_key)))
        hits = {r[0] for r in anchors}
        if len(hits) > 1:
            # Full-name OCR variants can already be separate retained header
            # nodes. Only a strongly predominant full spelling can win, and
            # every competing given name must differ by at most one OCR edit.
            ranked = sorted(anchors, key=lambda r: (-r[2], str(r[0]), r[1]))
            top, given, count = ranked[0]
            others = [r for r in ranked if r[0] != top]
            if count >= 3 * sum({i: n for i, _, n in others}.values()) and all(
                    len(given) >= 4 and len(other) >= 4 and
                    edit_distance(family_ocr_key(given), family_ocr_key(other), 1) <= 1
                    for _, other, _ in others):
                hits = {top}
        reason = 'same_page_surname'
        if not hits and len(surname) >= 5 and surname not in FIRST_NAMES and surname not in dictionary:
            # A rare one-word name ("Ariely", "Tancredi") that is the surname or
            # given name of exactly one full-name person anywhere in the corpus.
            owners = {r[0] for r in db.execute('''SELECT DISTINCT k.entity_id FROM person_anchors p
                JOIN key_nodes k ON k.key=p.key JOIN entities e ON e.id=k.entity_id
                WHERE (p.surname=? OR p.given_name=?) AND e.entity_type IN ('person','participant') AND k.entity_id !=
                    (SELECT entity_id FROM key_nodes WHERE key=?)''', (surname, surname, fragment_key))}
            if len(owners) == 1:
                hits, reason = owners, 'unique_name_token'
                summary['unique_name_token_keys'] += 1
        if len(hits) == 1:
            target = next(iter(hits))
            pending.append((target, fragment_key))
            if target in existing:
                target_type = existing[target]['entity_type']
                db.execute('INSERT INTO folds VALUES (?,?,?,?,?)',
                           (fragment_key, 'HeaderLabel_' + hashlib.sha256(fragment_key.encode()).hexdigest()[:20],
                            target, target_type, reason))
                summary['folded_keys'] += 1
                summary['folded_' + target_type] += 1
    db.executemany('UPDATE key_nodes SET entity_id=? WHERE key=?', pending)
    summary['same_page_person_keys'] = len(pending)
    # Both merge passes look keys up by node: without this each lookup scans ~1M rows.
    db.execute('CREATE INDEX key_nodes_entity ON key_nodes(entity_id)')
    squash_merges(db, existing, protected, summary)
    person_ocr_merges(db, existing, protected, person_names, summary)
    db.execute('DROP INDEX key_nodes_entity')
    person_names.clear()
    db.execute('DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM key_nodes)')
    db.executescript('''INSERT OR IGNORE INTO mentions SELECT k.entity_id,m.doc_id FROM extracted.mentions m JOIN key_nodes k USING(key);
        CREATE INDEX mentions_doc ON mentions(efta_number,entity_id);
        UPDATE entities SET document_count=(SELECT count(*) FROM mentions WHERE entity_id=entities.id);
        UPDATE entities SET metadata=json_set(metadata,'$.metadata.document_count',document_count);''')
    # Join near-duplicate IDs in SQLite rather than loading the corpus document map.
    if clusters and clusters.exists():
        db.execute('ATTACH DATABASE ? AS dedupe', (str(clusters),))
        cluster_expr = 'coalesce(d.canonical,m.efta_number)'
        join = 'LEFT JOIN dedupe.docs d ON d.efta_number=m.efta_number'
    else:
        cluster_expr, join = 'm.efta_number', ''
    db.executescript(f'''CREATE TABLE clustered_mentions AS SELECT m.entity_id,m.efta_number,{cluster_expr} AS cluster FROM mentions m {join};
        CREATE INDEX cluster_doc ON clustered_mentions(efta_number,entity_id);
        CREATE TABLE distinct_mentions AS SELECT DISTINCT entity_id,cluster FROM clustered_mentions;
        CREATE INDEX distinct_doc ON distinct_mentions(cluster,entity_id);
        CREATE TABLE doc_sizes AS SELECT efta_number,count(*) AS n FROM mentions GROUP BY efta_number;
        CREATE UNIQUE INDEX sizes_doc ON doc_sizes(efta_number);
        CREATE TABLE frequencies AS SELECT entity_id,count(*) AS n FROM distinct_mentions GROUP BY entity_id;
        CREATE UNIQUE INDEX frequency_entity ON frequencies(entity_id);
        CREATE TABLE pair_clusters AS SELECT a.entity_id AS a,b.entity_id AS b,a.cluster,min(s.n) AS n
          FROM doc_sizes s JOIN clustered_mentions a ON a.efta_number=s.efta_number
          JOIN clustered_mentions b ON b.efta_number=s.efta_number AND a.entity_id<b.entity_id
          WHERE s.n<={int(max_entities_per_doc)} GROUP BY a.entity_id,b.entity_id,a.cluster;
        CREATE TABLE pairs AS SELECT a,b,count(*) AS shared_documents,
          sum(1.0 / (n-1)) AS specificity_score,
          0.0 AS lift,0 AS kept,'[]' AS sample_eftas FROM pair_clusters GROUP BY a,b;
        CREATE INDEX pairs_a ON pairs(a); CREATE INDEX pairs_b ON pairs(b);''')
    total_docs = db.execute('SELECT count(DISTINCT canonical) FROM dedupe.docs' if clusters and clusters.exists()
                            else 'SELECT count(DISTINCT cluster) FROM distinct_mentions').fetchone()[0]
    db.execute('''UPDATE pairs SET lift=shared_documents*?*1.0 /
          ((SELECT n FROM frequencies WHERE entity_id=a)*(SELECT n FROM frequencies WHERE entity_id=b))''', (total_docs,))
    # Specificity, lift and evidence include every pair; cap only the viewer slice.
    # Samples look mentions up by entity: without this index every viewer edge
    # scanned all clustered mentions (hours). Dropped afterwards to save disk.
    db.execute('CREATE INDEX IF NOT EXISTS cluster_entity ON clustered_mentions(entity_id,efta_number,cluster)')
    edges, counts = [], Counter()
    @lru_cache(maxsize=16384)
    def document_count(identity):
        return db.execute('SELECT document_count FROM entities WHERE id=?', (identity,)).fetchone()[0]
    for pair_id,a,b,shared,specificity,lift in db.execute('SELECT rowid,a,b,shared_documents,specificity_score,lift FROM pairs ORDER BY specificity_score*sqrt(min(lift,10)) DESC,a,b'):
        if specificity <= 0 or lift < 1:
            continue
        if len(edges) >= view_edges:
            break
        if counts[a] >= per_node or counts[b] >= per_node:
            continue
        samples = pair_samples(db, a, b, document_count)
        counts.update((a,b))
        db.execute('UPDATE pairs SET kept=1,sample_eftas=? WHERE rowid=?', (json.dumps(samples),pair_id))
        edges.append({'id':f'entity_comention_{a}_{b}', 'source_entity_id':a,'target_entity_id':b,
            'relationship_type':'co_mentioned_in_documents','weight':shared,'date_first':None,'date_last':None,
            'metadata':{'meaning':'Document co-mention is a reading lead, not evidence of a relationship or wrongdoing.',
                'shared_documents':shared,'specificity_score':specificity,'lift_vs_chance':lift,
                'sample_documents':[{'efta_number':d} for d in samples], 'complete_evidence':'output/entity_graph.db'}})
    # Association edges do not merge domains into organizations. Include existing
    # firms even if their names were absent from this layer's qualifying mentions.
    db.execute("DROP INDEX IF EXISTS cluster_entity")
    association_nodes = {i: n for i, n in existing.items() if n['entity_type'] in COMPANIES | {'website'}}
    for (payload,) in db.execute("SELECT metadata FROM entities WHERE entity_type IN ('website','organization','shell_company')"):
        node = json.loads(payload)
        association_nodes[node['id']] = node
    domain_edges = website_org_links(association_nodes.values())
    seen_domain_edges = set()
    for edge in domain_edges:
        if edge['id'] not in seen_domain_edges:
            edges.append(edge)
            seen_domain_edges.add(edge['id'])
    summary['website_of_links'] = len(seen_domain_edges)
    with (out / 'entity_nodes.json').open('w') as f:
        f.write('[\n')
        first = True
        for (payload,) in db.execute('SELECT metadata FROM entities ORDER BY id'):
            if not first:
                f.write(',\n')
            f.write(payload)
            first = False
        f.write('\n]\n')
    (out / 'entity_edges.json').write_text(json.dumps(edges,indent=2)+'\n')
    for field in list(summary):
        if field.startswith('created_'):
            del summary[field]
    for identity, entity_type in db.execute('SELECT id,entity_type FROM entities'):
        if identity not in existing:
            summary['created_' + entity_type] += 1
    summary.update({'nodes':db.execute('SELECT count(*) FROM entities').fetchone()[0],
                    'hidden_nodes':db.execute("SELECT count(*) FROM entities WHERE json_extract(metadata,'$.label_quality')='not_a_name'").fetchone()[0],
                    'pairs':db.execute('SELECT count(*) FROM pairs').fetchone()[0], 'viewer_edges':len(edges)})
    db.commit(); db.close()
    path.replace(out / 'entity_graph.db')
    (out / 'entity_summary.json').write_text(json.dumps(dict(summary),indent=2)+'\n')
    return dict(summary)


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--mentions',type=Path,default=OUT / '.build/entity_mentions.db')
    p.add_argument('--output',type=Path,default=OUT)
    p.add_argument('--private',type=Path,default=ROOT / 'data/private/entity_spellings.db')
    p.add_argument('--view-edges',type=int,default=8000)
    p.add_argument('--per-node',type=int,default=8)
    # Same cap as build_comentions/build_companies: documents naming more entities (indexes, contact
    # dumps; one names 64k) say nothing about any pair and would yield ~9.6 billion pairs.
    p.add_argument('--max-entities-per-doc',type=int,default=40)
    args = p.parse_args()
    from entity_build_evidence import LabelEvidence
    from airports import Airports
    started = time.time()
    summary = build(args.mentions,args.output,read_nodes(OUT),json.loads((ROOT / 'config/survivors.json').read_text())['names'],
                    args.private, contact_key(), args.view_edges,args.per_node,OUT / 'document_clusters.db',
                    json.loads((OUT / 'normalized_relationships.json').read_text()), args.max_entities_per_doc,
                    evidence=LabelEvidence.project(), airports=Airports())
    print(json.dumps({**summary,'seconds':round(time.time()-started,2)},indent=2))


if __name__ == '__main__':
    main()
