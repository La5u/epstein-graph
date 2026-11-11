"""Every email address and phone number written in the full text, as graph nodes.

Input: data/private/fulltext_contacts.db (extract_contacts.py; raw values),
data/private/contact_lookup.json (header identities' values), output/identity_map.json.

Each distinct value is a node with the pipeline's pseudonymous ID
(build_contacts.contact_id: the same value in the headers has the same ID, so
known addresses attach to their identity and its merges). An email that is
only an OCR variant of a known address (build_communications.ocr_repair key)
becomes an alias of it, as does a value misread from a much more frequent one
(fuzzy_variants: one-edit email local parts, phones missing +1, one misread digit
in a phone's area code or exchange). Nothing is filtered: service lines (toll-free numbers)
and values in one document are nodes too, flagged so the viewer can rank them.

Links: values written on the same page (contact lists, signatures, letterheads),
counted per distinct document; pages listing more than --max-per-page values
(distribution lists, directories) give no pairs. A page pair is a reading lead,
not proof the values belong together.

Names: the name written next to a value (extract_contacts.name_near) links it
to that person: a KG person (name or original names), a header name (same label
in the header data), else a new name node (participant_<hmac of the name>,
label_quality flag from build_communications). Survivor names are never linked
or added. A value whose named documents are >= 80% one identity (>= 2 documents)
gets that identity as likely owner (nodes.owner); owners confirmed by
review_contact_owners.py (config/contact_owners.json) become the value's canonical.

Writes output/contact_graph.db (pseudonymous: nodes, aliases, mentions, pairs,
names) and data/private/fulltext_contact_lookup.json ({id: {kind, value}} for
values and names not in the header lookup; 0600).
"""
import argparse
import json
import os
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

import hashlib
import hmac

import re

from build_communications import edit_distance, label_quality, ocr_repair
from build_contacts import contact_id, contact_key
from contact_resolution import email_key, phone_key, skeleton, resolve_contacts, spaced_address_variants
from normalize_graph import name_key
from victim_policy import victim_names

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data/private/fulltext_contacts.db'
LOOKUP = ROOT / 'data/private/contact_lookup.json'
PRIVATE_LOOKUP = ROOT / 'data/private/fulltext_contact_lookup.json'
OUT = ROOT / 'output/contact_graph.db'
TOLL_FREE = ('+1800', '+1833', '+1844', '+1855', '+1866', '+1877', '+1888')


def view_id(identity):
    """Canonical IDs as the viewer and identity_map write them: graph entities as their number."""
    identity = str(identity)
    return identity[3:] if identity.startswith('kg:') else identity


def value_canonical(identity, rules, owners, identity_map):
    """The node a value belongs to: its merge target, or (OCR variant) the repaired address's.
    A reviewed owner (config/contact_owners.json, review_contact_owners.py) of the value or of
    the value it is a misreading of wins, followed through identity merges like any target."""
    target = rules[identity].split(':', 1)[1] if identity in rules else identity
    target = owners.get(identity) or owners.get(target) or target
    return view_id(identity_map.get(target, {}).get('canonical', target))


def resolver(lookup, key):
    """value -> (id, rule): the header identity for a known value, else a new ID."""
    exact, folded, repaired = {}, {}, {}
    for identity, item in lookup.items():
        if item.get('kind') == 'phone' and not identity.startswith('phone_'):
            repaired.setdefault(('phone', phone_key(item['value'])),identity)
            continue
        # NER spellings historically used kind=participant even for email_
        # IDs. Their IDs encode the repaired mailbox; keep each raw spelling
        # as a member instead of treating the raw OCR string as an exact key.
        if (identity.startswith('email_') and item.get('kind') == 'participant'
                or item.get('kind') == 'email' and not identity.startswith('email_')):
            repaired.setdefault(email_key(item['value']), identity)
            continue
        if item.get('kind') not in ('email', 'phone'):
            continue
        exact[item['value']] = identity
        if item['kind'] == 'email':
            folded.setdefault(item['value'].casefold(), identity)
            repaired.setdefault(email_key(item['value']), identity)
        else:
            repaired.setdefault(('phone', phone_key(item['value'])), identity)

    def resolve(kind, value):
        if value in exact:
            return exact[value], 'header_value'
        if kind == 'email':
            if value.casefold() in folded:
                return folded[value.casefold()], 'header_value_case'
            identity = contact_id(value.casefold(), kind, key)
            target = repaired.get(email_key(value))
            return identity, ('ocr_variant:' + target) if target and target != identity else 'fulltext'
        identity = contact_id(value, kind, key)
        target = repaired.get(('phone', phone_key(value)))
        return identity, ('ocr_variant:' + target) if target and target != identity else 'fulltext'
    return resolve


# Digits OCR confuses (8/3/6/0/9, 1/7, 5/6, 2/5).
CONFUSABLE_DIGITS = [set(group) for group in ('08369', '17', '56', '38', '25')]


def fuzzy_variants(documents):
    """{(kind, value): (target value, rule)} for misread values of a much more frequent one.

    documents: {(kind, value): distinct documents} (emails case-folded).
      ocr_edit             email: local part (after ocr_repair) one edit from a local part
                           (>= 6 letters; two edits >= 8) at the same repaired domain with
                           >= 10x the documents, the rule header addresses already follow.
      missing_country_code phone: "+3407752525" beside "+13407752525".
      ocr_digit            phone in <= 3 documents, one confusable digit (8/3, 1/7...) off a
                           number in >= 20x as many (area code) or >= 50x (exchange); never in
                           the last four digits, which tell lines of one office apart (...0361 / ...0381).
    """
    out = {}
    by_domain = defaultdict(list)
    for (kind, value), n in documents.items():
        if kind == 'email' and '@' in value:
            local, domain = ocr_repair(value)
            by_domain[domain].append((n, local, value))
    for items in by_domain.values():
        items.sort(key=lambda item: (-item[0], item[2]))
        seen = set()
        for n, local, value in items:
            if local not in seen:
                limit = 2 if len(local) >= 8 else 1 if len(local) >= 6 else 0
                for n2, local2, value2 in items if limit else ():
                    if n2 < 10 * max(n, 1):
                        break
                    if local2 != local and abs(len(local2) - len(local)) <= limit and edit_distance(local, local2, limit) <= limit:
                        out['email', value] = (value2, 'ocr_edit')
                        break
            seen.add(local)
    phones = {value: n for (kind, value), n in documents.items() if kind == 'phone'}
    masked = defaultdict(list)
    for value, n in phones.items():
        if value.startswith('+1') and len(value) == 12:
            for i in range(2, 8):
                masked[value[:i] + '?' + value[i + 1:]].append((n, value))
    for value, n in phones.items():
        if re.fullmatch(r'\+[2-9]\d\d[2-9]\d{6}', value) and '+1' + value[1:] in phones:
            out['phone', value] = ('+1' + value[1:], 'missing_country_code')
        elif n <= 3 and value.startswith('+1') and len(value) == 12:
            best = max(((n2, v2) for i in range(2, 8) for n2, v2 in masked[value[:i] + '?' + value[i + 1:]]
                        if v2 != value and n2 >= (20 if i < 5 else 50) * n  # exchange misreads need more weight
                        and any(value[i] in g and v2[i] in g for g in CONFUSABLE_DIGITS)), default=None)
            if best:
                out['phone', value] = (best[1], 'ocr_digit')
    # Exact mailbox semantics (case, Gmail dots/tags, repaired @/domain)
    # and phone formatting do not need a frequency imbalance.
    exact_groups = defaultdict(list)
    for (kind, value), n in documents.items():
        normalized = email_key(value) if kind == 'email' else phone_key(value)
        exact_groups[kind, normalized].append((n, value))
    for (kind, normalized), group in exact_groups.items():
        def fidelity(value):
            if kind == 'email':
                return int('@' in value and value.rsplit('@',1)[1].casefold() == normalized[1])
            return int(value == normalized)
        group.sort(key=lambda x: (-x[0], -fidelity(x[1]), x[1]))
        target = group[0][1]
        for _, value in group[1:]:
            out.setdefault((kind, value), (target, 'mailbox_normalization' if kind == 'email' else 'phone_format'))
    # Check all requested look-alikes in local parts and domains, with a
    # dominant attested target. Dot loss and insertion are included. Domains
    # are compared only alongside the same mailbox, never as global renames.
    groups = defaultdict(list)
    for (kind, value), n in documents.items():
        if kind == 'email':
            local, domain = email_key(value)
            if len(local) >= 6:
                groups[skeleton(local), skeleton(domain)].append((n, value))
        elif kind == 'phone':
            groups['phone', phone_key(value)].append((n, value))
    for group in groups.values():
        group.sort(key=lambda x: (-x[0], x[1]))
        n2, target = group[0]
        for n, value in group[1:]:
            if n2 >= 10 * max(n, 1):
                kind = 'email' if '@' in value else 'phone'
                out.setdefault((kind,value),(target,'ocr_confusables' if kind == 'email' else 'phone_format'))
    return out


def name_resolver(lookup, entities, key):
    """name -> identity: KG person, header name (participant label), else a new name ID;
    tries "Surname First" as "First Surname" too."""
    people = {}
    for e in entities:
        if e['entity_type'] in ('person', 'survivor'):
            for n in (e['name'], *e.get('original_names', [])):
                people.setdefault(name_key(n), f"kg:{e['id']}")
    labels = {}
    for identity, item in lookup.items():
        if item.get('kind') == 'participant':
            labels.setdefault(name_key(item['value']), identity)

    def resolve(name):
        k = name_key(name)
        words = k.split()
        for candidate in (k, ' '.join(words[1:] + words[:1]) if len(words) == 2 else None):
            if candidate in people:
                return people[candidate], 'kg_person'
            if candidate in labels:
                return labels[candidate], 'header_name'
        return 'participant_' + hmac.new(key, ('fulltext_name\0' + k).encode(), hashlib.sha256).hexdigest()[:24], 'fulltext_name'
    return resolve


def person_test(lookup, entities, company_db=ROOT / 'output/company_graph.db', min_count=10):
    """name -> whether it reads as a person's name: its first word (after initials) is a known
    first name: of a KG person or registry person, or of >= min_count two/three-word header
    names unless a company name starts with it ("Google", "Delta", "Grand")."""
    firsts = Counter()
    for item in lookup.values():
        if item.get('kind') == 'participant':
            words = name_key(item['value']).split()
            if 2 <= len(words) <= 3 and words[0].isalpha() and len(words[0]) > 1:
                firsts[words[0]] += 1
    company_firsts = set()
    if company_db.exists():
        with closing(sqlite3.connect(f'file:{company_db}?mode=ro', uri=True)) as db:
            company_firsts = {name_key(n).split()[0].strip(',.') for n, in db.execute('SELECT name FROM entities') if n.split()}
    registry = ROOT / 'data/raw/persons_registry.json'
    known = [e['name'] for e in entities if e['entity_type'] in ('person', 'survivor')]
    known += [p['name'] for p in json.loads(registry.read_text())] if registry.exists() else []
    given = {w for w, n in firsts.items() if n >= min_count} - company_firsts
    given |= {name_key(n).split()[0] for n in known if len(name_key(n).split()) >= 2}

    def test(name):
        words = [w.strip('.') for w in name_key(name).split()]
        return next((w for w in words if len(w) > 1), '') in given
    return test


def with_phone_bases(rows):
    """Retain the base-number node when extraction also discovers an extension.

    The base number is itself written in the source. Keeping it prevents a
    newly parsed extension from deleting an earlier extracted full-data node;
    different extension lines remain distinct and need ownership evidence.
    """
    for row in rows:
        yield row
        kind, value, *rest = row
        if kind == 'phone' and re.fullmatch(r'\+\d+x\d+',value):
            yield (kind,value.split('x',1)[0],*rest)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--max-per-page', type=int, default=40)
    args = parser.parse_args()
    started = time.time()
    key = contact_key()
    lookup = json.loads(LOOKUP.read_text())
    entity_lookup = ROOT / 'data/private/entity_contact_lookup.json'
    if entity_lookup.exists():
        lookup.update(json.loads(entity_lookup.read_text()))
    identity_map = json.loads((ROOT / 'output/identity_map.json').read_text())
    resolve = resolver(lookup, key)
    entities = json.loads((ROOT / 'output/normalized_entities.json').read_text())
    resolve_name = name_resolver(lookup, entities, key)
    survivors = victim_names(config=ROOT / 'config/victims.json', survivor_config=ROOT / 'config/survivors.json',
                             registry=ROOT / 'data/raw/persons_registry.json')
    names, name_ids, name_rules = defaultdict(lambda: [set(), set(), Counter()]), {}, {}

    partial = OUT.with_suffix('.partial.db')
    partial.unlink(missing_ok=True)
    db = sqlite3.connect(partial)
    db.executescript('''
        CREATE TABLE mentions(id TEXT, efta TEXT, page INTEGER, label TEXT, header INTEGER);
        CREATE TABLE aliases(member TEXT PRIMARY KEY, canonical TEXT, rule TEXT);
        CREATE TEMP TABLE page_pairs(a TEXT, b TEXT, efta TEXT, weight REAL);
    ''')
    ids, rules, private, kinds = {}, {}, {}, {}
    owners_path = ROOT / 'config/contact_owners.json'
    reviewed_owners = json.loads(owners_path.read_text()).get('owners', {}) if owners_path.exists() else {}

    def canonical_of(identity):
        return value_canonical(identity, rules, reviewed_owners, identity_map)
    page_values = defaultdict(set)
    with closing(sqlite3.connect(f'file:{SOURCE}?mode=ro', uri=True)) as src:
        documents = Counter()
        for kind, value, n in src.execute('SELECT kind, value, count(DISTINCT efta) FROM mentions GROUP BY kind, value'):
            documents[kind, value.casefold() if kind == 'email' else value] += n
            if kind == 'phone' and 'x' in value:
                documents[kind,value.split('x',1)[0]] += n
        fuzzy = fuzzy_variants(documents)
        import corpus
        with closing(corpus.connect()) as text_db:
            for value, target in spaced_address_variants(src, documents, lookup, text_db).items():
                fuzzy.setdefault(value, target)

        def variant_rule(kind, value):
            """'rule:target id' for a misread value (target chains followed), else None."""
            key_ = (kind, value.casefold() if kind == 'email' else value)
            rule, seen = None, set()
            while key_ in fuzzy and key_ not in seen:
                seen.add(key_)
                target, rule = fuzzy[key_]
                key_ = (kind, target)
            if rule is None:
                return None
            identity, target_rule = resolve(kind, key_[1])
            return rule + ':' + (target_rule.split(':', 1)[1] if target_rule.startswith('ocr_variant:') else identity)
        rows = src.execute('SELECT kind, value, efta, page, label, header, name, name_how FROM mentions ORDER BY efta, page')
        current = None

        def flush(page, values):
            if 2 <= len(values) <= args.max_per_page:
                weight = 1 / (len(values) - 1)
                ordered = sorted(values)
                db.executemany('INSERT INTO page_pairs VALUES (?,?,?,?)',
                               [(a, b, page[0], weight) for i, a in enumerate(ordered) for b in ordered[i + 1:]])

        batch = []
        for kind, value, efta, page, label, header, name, how in with_phone_bases(rows):
            if (kind, value) not in ids:
                identity, rule = resolve(kind, value)
                ids[kind, value] = identity
                kinds[identity] = kind
                if rule != 'header_value' and rule != 'header_value_case' and identity not in lookup:
                    private[identity] = {'kind': kind, 'value': value.casefold() if kind == 'email' else value}
                if rule.startswith('ocr_variant:'):
                    rules[identity] = rule
                elif rule == 'fulltext' and (variant := variant_rule(kind, value)):
                    rules[identity] = variant
            identity = ids[kind, value]
            canonical = canonical_of(identity)
            batch.append((identity, efta, page, label, header))
            if name and name_key(name) not in survivors:
                if name not in name_ids:
                    name_ids[name], name_rules[name] = resolve_name(name)
                person = view_id(identity_map.get(name_ids[name], {}).get('canonical', name_ids[name]))
                if person != canonical:
                    item = names[canonical, person]
                    item[0].add(efta)
                    item[1].add(name)
                    item[2][how] += 1
            if (efta, page) != current:
                if current is not None:
                    flush(current, page_values.pop(current, set()))
                current = (efta, page)
            page_values[current].add(canonical)
            if len(batch) >= 100000:
                db.executemany('INSERT INTO mentions VALUES (?,?,?,?,?)', batch)
                batch = []
        db.executemany('INSERT INTO mentions VALUES (?,?,?,?,?)', batch)
        if current is not None:
            flush(current, page_values.pop(current, set()))
    db.executemany('INSERT INTO aliases VALUES (?,?,?)',
                   [(m, r.split(':', 1)[1], r.split(':', 1)[0]) for m, r in rules.items()])
    print(f'{len(ids):,} values, {len(rules):,} aliases {dict(Counter(r.split(":", 1)[0] for r in rules.values()))} ({time.time() - started:.0f}s)', flush=True)

    db.executescript('''
        CREATE INDEX mentions_id ON mentions(id, efta);
        CREATE INDEX mentions_efta ON mentions(efta);
        CREATE TABLE pairs AS
            SELECT a, b, count(DISTINCT efta) AS shared_documents, count(*) AS shared_pages,
                   round(sum(weight), 3) AS specificity, min(efta) AS sample_efta
            FROM page_pairs GROUP BY a, b;
        DROP TABLE page_pairs;
        CREATE INDEX pairs_a ON pairs(a);
        CREATE INDEX pairs_b ON pairs(b);
    ''')
    db.execute('''CREATE TABLE nodes(id TEXT PRIMARY KEY, kind TEXT, canonical TEXT, in_headers INTEGER,
                  service_line INTEGER, documents INTEGER, pages INTEGER, body_pages INTEGER, labels TEXT)''')
    labels = defaultdict(Counter)
    for identity, label, n in db.execute("SELECT id, label, count(*) FROM mentions WHERE label != '' GROUP BY 1, 2"):
        labels[identity][label] = n
    service = {i for (kind, value), i in ids.items() if kind == 'phone' and value.startswith(TOLL_FREE)}
    counts = {i: (d, p, b) for i, d, p, b in db.execute(
        'SELECT id, count(DISTINCT efta), count(*), sum(header = 0) FROM mentions GROUP BY id')}
    db.executemany('INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)', [
        (i, kinds[i], canonical_of(i), int(i in lookup),
         int(i in service), *counts[i], json.dumps(dict(labels[i].most_common())) if labels[i] else None)
        for i in sorted(counts)])
    # Names written next to values; new names are nodes too (survivor names never reach here).
    db.execute('CREATE TABLE names(value TEXT, identity TEXT, documents INTEGER, mentions INTEGER, how TEXT, rule TEXT, sample_efta TEXT)')
    rule_of = {}
    for name, identity in name_ids.items():
        rule_of.setdefault(view_id(identity_map.get(identity, {}).get('canonical', identity)), name_rules[name])
    db.executemany('INSERT INTO names VALUES (?,?,?,?,?,?,?)', [
        (value, person, len(docs), sum(how.values()), json.dumps(dict(how)), rule_of.get(person, 'fulltext_name'), min(docs))
        for (value, person), (docs, spellings, how) in names.items()])
    new_names = {}
    for name, identity in name_ids.items():
        if name_rules[name] == 'fulltext_name' and identity not in lookup:
            new_names.setdefault(identity, Counter())[name] += 1
    person_like = person_test(lookup, entities)
    def quality(name):
        q = label_quality(name)
        return 'uncertain' if q == 'ok' and not person_like(name) else q  # "Google Calendar", "Adjustment Request"
    db.executemany('INSERT OR IGNORE INTO nodes VALUES (?,?,?,?,?,?,?,?,?)', [
        (identity, 'participant', identity, 0, 0, 0, 0, 0, json.dumps({'label_quality': quality(spellings.most_common(1)[0][0])}))
        for identity, spellings in new_names.items()])
    for identity, spellings in new_names.items():
        private[identity] = {'kind': 'participant', 'value': spellings.most_common(1)[0][0]}
    # Likely owner: one identity names >= 80% of a value's named documents (>= 2).
    db.execute('ALTER TABLE nodes ADD COLUMN owner TEXT')
    by_value = defaultdict(list)
    for value, person, docs in db.execute('SELECT value, identity, documents FROM names'):
        by_value[value].append((docs, person))
    owners = []
    for value, items in by_value.items():
        items.sort(reverse=True)
        total = sum(d for d, _ in items)
        if items[0][0] >= 2 and items[0][0] >= 0.8 * total:
            owners.append((items[0][1], value))
    db.executemany('UPDATE nodes SET owner = ? WHERE id = ? AND kind != "participant"', owners)
    db.executescript('CREATE INDEX nodes_canonical ON nodes(canonical); CREATE INDEX nodes_owner ON nodes(owner); '
                     'CREATE INDEX names_identity ON names(identity); CREATE INDEX names_value ON names(value);')
    resolution_summary = resolve_contacts(db, lookup, entities, identity_map, private, survivors, ROOT)
    summary = {'resolution': resolution_summary, 'values': len(counts), 'by_kind': dict(Counter(kinds[i] for i in counts)),
               'in_headers': sum(i in lookup for i in counts), 'ocr_aliases': len(rules), 'service_lines': len(service),
               'pairs': db.execute('SELECT count(*) FROM pairs').fetchone()[0],
               'name_links': db.execute('SELECT count(*) FROM names').fetchone()[0],
               'name_links_by_rule': dict(db.execute('SELECT rule, count(*) FROM names GROUP BY rule').fetchall()),
               'new_name_nodes': len(new_names), 'values_with_owner': len(owners),
               'seconds': round(time.time() - started)}
    db.execute('CREATE TABLE summary(json TEXT)')
    db.execute('INSERT INTO summary VALUES (?)', (json.dumps(summary),))
    db.commit()
    db.close()
    partial.replace(OUT)
    old_mask = os.umask(0o077)
    try:
        tmp = PRIVATE_LOOKUP.with_suffix('.partial')
        tmp.write_text(json.dumps(private, ensure_ascii=False) + '\n')
        tmp.replace(PRIVATE_LOOKUP)
    finally:
        os.umask(old_mask)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
