"""Stage 3 of the company layer: reviewed full-text companies become graph nodes.

Inputs: output/company_candidates.csv and output/.build/company_mentions.db
(extract_companies.py), the cached model verdicts in
output/.build/company_review_<model>.jsonl (classify_companies.py; Sol, or Terra where Sol has none, decides,
Luna is the second reader; where Sol has no verdict, Luna and DeepSeek must
agree), config/company_review.json (manual overrides) and
the KG mentions in output/comention_graph.db (build_comentions.py).

Every reviewed candidate becomes a node; relevance and label quality filter the viewer only.
Spellings folded into it (glue, OCR, fragments) and candidates with the same
reviewed name are one node. epstein_controlled -> shell_company, else
organization; a candidate matching a KG organization adds its documents there.

Links: documents naming a company and a KG entity or another company,
scored like build_comentions (documents naming few entities weigh more, lift
vs chance; near-duplicate copies from dedupe_documents.py count once). They are reading leads, not evidence of a relationship.
A link needs --min-shared documents, or --min-shared-public when the other end is
a KG public figure (Epstein, Leon Black, ...). Each
company keeps its strongest --per-company links; the full table is in
output/company_graph.db (entities, mentions, pairs; same mentions schema as
comention_graph.db so build_documents can list the documents behind a link).

Survivors: a company whose name contains a configured survivor name is dropped.

Writes output/company_nodes.json, output/company_edges.json, output/company_graph.db.
"""
import argparse
import csv
import hashlib
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from rapidfuzz.distance import Levenshtein

import corpus
from corpus_index import cluster_pair_counts, doc_clusters, page_map
from entity_build_rules import clean_company_name, company_label_reason, company_tail, surname_legal_firm

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output'
CANDIDATES = OUT / 'company_candidates.csv'
MENTIONS = OUT / '.build/company_mentions.db'
REVIEW = ROOT / 'config/company_review.json'
PRIMARY, SECOND, THIRD = ('gpt-6.1-sol', 'gpt-6.1-terra'), 'gpt-6-luna', 'deepseek'  # Terra answers what Sol has not
KEEP_RELEVANCE = ('epstein_controlled', 'network')
DROP_KINDS = ('generic', 'junk')
MEANING = ('Both names appear in the same EFTA documents (company name found by its legal form, e.g. "JEGE, LLC"). '
           'A reading lead, NOT evidence of a relationship, ownership or wrongdoing.')
SAMPLE_LIMIT = 8


def verdicts(models):
    out = {}
    for model in reversed((models,) if isinstance(models, str) else models):  # the first model wins
        path = OUT / f'.build/company_review_{model}.jsonl'
        if path.exists():
            for line in path.read_text().splitlines():
                item = json.loads(line)
                out[item['key']] = {**item, '_model': model}  # the latest answer wins
    return out


FORMS = {'company': 'co', 'incorporated': 'inc', 'corporation': 'corp', 'limited': 'ltd', 'and': '&', 'street': 'st'}
LEGAL = {'co', 'inc', 'corp', 'ltd', 'llc', 'lp', 'llp', 'pllc', 'pa', 'pc', 'plc', 'ag', 'sa'}


@lru_cache(maxsize=32768)
def normal_name(name):
    """Spelling-insensitive name: "The C.O.U.Q. Foundation, Inc." = "COUQ Foundation Incorporated"."""
    name = re.sub(r'\b8(?=\s+(?:co\b|company\b))', '&', name, flags=re.I)
    text = name.casefold().replace("'s ", ' ').replace("’s ", ' ')
    text = re.sub(r'\b([a-z])\.(?=[a-z]\.)', r'\1', text)  # c.o.u.q. -> couq.
    text = re.sub(r'\b([a-z])\.', r'\1', text)
    words = [FORMS.get(w, w) for w in re.findall(r'[a-z0-9&]+', text)]
    while words and words[0] == 'the':
        words.pop(0)
    return ' '.join(words)


def base_of(normal):
    words = normal.split()
    while len(words) > 1 and words[-1] in LEGAL:
        words.pop()
    return ' '.join(words)


def company_id(name):
    return 'company_' + hashlib.sha1(normal_name(name).encode()).hexdigest()[:16]


def words(text):
    return re.findall(r'[a-z0-9]+', text.casefold())


def contains_name(company, name):
    a, b = words(company), words(name)
    return len(b) >= 2 and any(a[i:i + len(b)] == b for i in range(len(a) - len(b) + 1))


def visibility_reason(name, kind, evidence=None, explicit_rejection=False):
    attested = bool(evidence and evidence.company_attested(name))
    reason = company_label_reason(name, attested)
    if reason:
        return reason
    if explicit_rejection:
        return 'reviewed_rejection'
    weak_rejection = company_label_reason(name) in {'generic_company', 'company_initial_fragment'}
    from entity_build_rules import GENERIC, FORMS
    body = normal_name(name).split()
    while body and body[-1] in FORMS:
        body.pop()
    if len(body) >= 2 and set(body) <= GENERIC | {'&'}:
        weak_rejection = False
    trusted = bool(evidence and normal_name(name) in evidence.trusted)
    initials = bool(re.search(r'^[A-Z]{1,3}\s*&\s*[A-Z]{1,3}\b', name))
    if kind in DROP_KINDS and not (surname_legal_firm(name) or trusted or attested and (weak_rejection or initials)):
        return 'reviewed_rejection'
    return None


DICTIONARY = Path('/usr/share/dict/words')  # optional; without it one-word names are never searched bare
PERSONAL_FORMS = {'pa', 'pc', 'pllc', 'chartered'}  # professional corporations, often one person's practice
MAX_FORMLESS_RATIO = 3  # the bare name may at most triple a company's documents (else it is a common phrase)


def formless_phrases(groups, people, places, given_names, dictionary, evidence=None):
    """{node id: bare name} to search without the legal form ("Gratitude America" for
    "Gratitude America, Ltd."). Skipped: names that are a person ("Dfoken K. Indyke PLLC",
    "Tceco Hsigan, P.A."), a KG person or place, shared by two nodes ("JEGE, Inc." /
    "JEGE, LLC"), end in "&" ("Allen & Company"), or one word that is a dictionary word
    ("Maple, Inc.") or shorter than 4 characters."""
    bases, forms = defaultdict(list), defaultdict(set)
    for node_id, g in groups.items():
        if company_label_reason(g['name'], bool(evidence and evidence.company_attested(g['name']))):
            continue  # keep extracted evidence, but do not search prose as a bare name
        normal = normal_name(g['name'])
        base = base_of(normal)
        if base != normal:
            bases[base].append(node_id)
            forms[base].update(normal.split()[len(base.split()):])
    taken = {normal_name(g['name']) for g in groups.values()}
    out = {}
    for base, ids in bases.items():
        words = base.split()
        if len(ids) > 1 or base in taken or words[-1] in ('&', 'and') or base in people or base in places:
            continue
        if any(contains_name(base, person) for person in people):
            continue
        if len(words) == 1:
            if len(base) < 4 or base in dictionary:
                continue
        elif words[0] in given_names and len(words) <= 3 or len(words) == 3 and len(words[1]) == 1:
            continue  # "Fgan Hsigan", "Bruce E. Reinhart": the person, not the firm
        elif len(words) == 2 and '&' not in words and forms[base] & PERSONAL_FORMS:
            continue  # "Tceco Hsigan, P.A.": a one-lawyer practice is named after the person
        out[ids[0]] = base
    return out


LOOKALIKE_LETTERS = [set(g) for g in ('od0q', 'il1j', 'b8', 'cg', 'ec', 'nm', 'uv', 'rn', 'sz5')]


def misread(a, b, limit):
    """Whether base name b reads as an OCR misreading of a: within `limit` edits (two only
    as same-length substitutions); short words (<= 3 letters, mostly initials) may only
    differ by look-alike letters ("LOB" / "LDB", not "LB" / "NC" or "DIF" / "VIF"); a
    different word count only as spacing ("BlueStar" / "Blue Star")."""
    if Levenshtein.distance(a, b, score_cutoff=limit) > limit or (len(a) != len(b) and Levenshtein.distance(a, b) > 1):
        return False
    wa, wb = a.split(), b.split()
    if len(wa) != len(wb):
        return a.replace(' ', '') == b.replace(' ', '')
    for x, y in zip(wa, wb):
        if x != y and min(len(x), len(y)) <= 3 and not (
                len(x) == len(y) and all(c == d or any(c in g and d in g for g in LOOKALIKE_LETTERS) for c, d in zip(x, y))):
            return False
    return True


def ocr_spellings(groups, merged=(), evidence=None):
    """{node: node it is a misreading of}: "Fancelli Paneling, Inc." / "Fanelli Paneling, Inc.",
    "Avionetta LLC" / "Avioneta LLC". Names one edit apart (two from 12 letters, at least 8),
    with the same legal form and the same numbers and numerals ("Apollo Management III" /
    "IV", "116" / "118 East 65th St." stay apart; digits inside words too when both have some: "APO1" /
    "APO2", not "Apol1o" / "Apollo"), see misread(); never two KG companies. The node with
    fewer documents moves; candidates already folded OCR spellings, this catches the rest."""
    numeral = re.compile(r'^(?:\d+|[ivx]+)$')
    entries = []
    for i, g in groups.items():
        if i in merged or company_label_reason(g['name'], bool(evidence and evidence.company_attested(g['name']))):
            continue
        normal = normal_name(g['name'])
        base = base_of(normal)
        entries.append((i, base, normal[len(base):].strip(), [w for w in base.split() if numeral.match(w)],
                        re.findall(r'\d+', base)))
    by_length = defaultdict(list)
    for entry in entries:
        by_length[len(entry[1])].append(entry)
    out = {}
    for i, base, form, numbers, digits in entries:
        limit = 2 if len(base) >= 12 else 1 if len(base) >= 8 else 0
        found = set()
        for length in range(len(base) - limit, len(base) + limit + 1) if limit else ():
            for j, base2, form2, numbers2, digits2 in by_length.get(length, ()):
                if j != i and form2 == form and numbers2 == numbers and base2 != base \
                        and not (digits and digits2 and digits != digits2) \
                        and not (groups[i]['kg'] and groups[j]['kg']) and misread(base, base2, limit):
                    found.add(j)
        if len(found) == 1:
            j = found.pop()
            rank = lambda k: (bool(groups[k]['kg']), groups[k].get('documents', 0), groups[k]['name'])
            if rank(j) > rank(i) and out.get(j) != i:
                out[i] = j
    return out


def squashed_spellings(groups, merged=(), evidence=None):
    """{node: node}: spacing/punctuation OCR variants with identical letters and digits,
    "POTTI NG ER LLC" / "Pottinger LLC", "A O E Ltd." / "AOE Ltd.", "3 B's LLC" / "3Bs LLC".
    Never two KG companies; the KG node, then the spelling with fewer split fragments wins."""
    by_key, junk = defaultdict(list), set()
    for i, g in groups.items():
        if i in merged:
            continue
        if company_label_reason(g['name'], bool(evidence and evidence.company_attested(g['name']))):
            junk.add(i)  # e.g. "A O E Ltd." (initial fragments) may move, never receive
        key = re.sub(r'[^0-9a-z&]', '', g['name'].casefold())
        if len(re.sub(r'[^a-z]', '', key)) >= 3:
            by_key[key].append(i)
    out = {}
    for ids in by_key.values():
        clean = [i for i in ids if i not in junk]
        if len(ids) < 2 or not clean or sum(bool(groups[i]['kg']) for i in ids) > 1:
            continue
        rank = lambda k: (not groups[k]['kg'], sum(len(w) <= 2 for w in re.findall(r'[A-Za-z]+', groups[k]['name'])),
                          -groups[k].get('documents', 0), groups[k]['name'])
        best = min(clean, key=rank)
        out.update({i: best for i in ids if i != best})
    return out


def merge_root(i, merge_into, groups):
    """End of i's merge chain. Independent rules can point two names at each
    other; every member of such a cycle gets the same survivor, the group with
    the most keys, so no group is merged into one that already left."""
    path, seen = [], set()
    while i in merge_into and i not in seen:
        seen.add(i)
        path.append(i)
        i = merge_into[i]
    if i not in seen:
        return i
    cycle = path[path.index(i):]
    return min((c for c in cycle if c in groups), key=lambda c: (-len(groups[c]['keys']), str(c)), default=i)


def select(rows, primary, second, overrides, survivor_names, third=None, evidence=None, never=()):
    third = third or {}
    """[(node name, kind, relevance, [keys], agreement)] for the candidates that become or extend nodes."""
    status = {r['key']: (r['status'], r['target']) for r in rows}

    def root(key, seen=()):
        kind, target = status.get(key, ('candidate', ''))
        return root(target, (*seen, key)) if kind in ('glue', 'fragment', 'ocr') and target in status and target not in seen else key
    folded = defaultdict(list)
    for r in rows:
        folded[root(r['key'])].append(r['key'])
    include, exclude, rename = set(overrides.get('include', [])), set(overrides.get('exclude', [])), overrides.get('rename', {})
    relevance_fix = overrides.get('relevance', {})
    groups = {}
    dropped = Counter()
    for r in rows:
        key = r['key']
        if r['status'] not in ('candidate', 'kg'):
            continue
        v = primary.get(key)
        if not v and key in second and key in third:
            # Preserve reviewed candidates; only agreement can establish Epstein control.
            a, b = second[key], third[key]
            if a and b:
                relevance = 'epstein_controlled' if a['relevance'] == b['relevance'] == 'epstein_controlled' else ('network' if any(x['relevance'] in KEEP_RELEVANCE for x in (a, b)) else 'incidental')
                v = {**b, 'relevance': relevance, 'name': b.get('name') or a.get('name'), 'confidence': 'medium',
                     '_models': [a.get('_model', SECOND), b.get('_model', THIRD)]}
        v = v or second.get(key) or third.get(key)
        if v and key in relevance_fix:
            v = {**v, 'relevance': relevance_fix[key], 'confidence': 'high'}
        if key not in include:
            if not v:
                continue
        v = v or {'kind': 'company', 'relevance': 'network', 'name': r['top_spelling']}
        if key in exclude:
            v = {**v, 'kind': 'junk'}  # reviewed rejection hides the label; preserve its evidence
        name = clean_company_name(rename.get(key) or (r['target'] if r['status'] == 'kg' else v.get('name')) or r['top_spelling'])
        if any(contains_name(name, s) for s in survivor_names):
            dropped['survivor_name'] += 1
            continue
        other = second.get(key)
        agree = bool(other) and other['kind'] not in DROP_KINDS and other['relevance'] in KEEP_RELEVANCE
        group = groups.setdefault(company_id(name), {'name': name, 'kinds': Counter(), 'relevance': Counter(), 'keys': [],
                                                     'agree': True, 'kg': r['target'] if r['status'] == 'kg' else None})
        group.setdefault('review_sources', set()).update(v.get('_models', [v.get('_model', 'manual inclusion')]))
        if key in include:
            group['review_sources'].add('manual inclusion')
        if key in relevance_fix and v.get('_model') != 'manual inclusion':
            group['review_sources'].add('manual relevance override')
        group['kinds'][v['kind']] += 1
        group['explicit_rejection'] = group.get('explicit_rejection', False) or key in exclude
        group['relevance'][v['relevance']] += 1
        group['keys'] += folded[key]
        group['agree'] &= agree
        group['documents'] = group.get('documents', 0) + int(r.get('documents') or 0)
    # "Mitchell Holdings" -> "Mitchell Holdings LLC" when exactly one formed spelling shares the base;
    # "JEGE, Inc." and "JEGE, LLC" (both formed) stay two entities.
    by_base = defaultdict(list)
    for node_id, g in groups.items():
        by_base[base_of(normal_name(g['name']))].append(node_id)
    merge_into = {}
    for ids in by_base.values():
        formed = [i for i in ids if normal_name(groups[i]['name']) != base_of(normal_name(groups[i]['name']))]
        bare = [i for i in ids if i not in formed]
        if len(formed) == 1:
            merge_into.update({i: formed[0] for i in bare})
    # Reviewed merges accept names (legacy) or two existing opaque company IDs.
    # Missing ID targets never rename a group or create a new company.
    names = {normal_name(g['name']): i for i, g in groups.items()}
    for alias, canonical in overrides.get('merge', {}).items():
        if alias.startswith('company_'):
            if alias in groups and canonical in groups and alias != canonical:
                previous = merge_into.get(alias)
                merge_into[alias] = canonical
                if any(merge_root(a, merge_into, groups) == merge_root(b, merge_into, groups) for a, b in never):
                    if previous is None:
                        del merge_into[alias]
                    else:
                        merge_into[alias] = previous
            continue
        a, c = names.get(normal_name(alias)), names.get(normal_name(canonical))
        if a and c and a != c:
            merge_into[a] = c
        elif a and not c:
            groups[a]['name'] = canonical  # rename; may also join a KG node of that name
            groups[a]['kg'] = groups[a]['kg'] or canonical
    # Strip person/title and prose prefixes only against an independently
    # attested complete company name. Ambiguous suffixes stay separate.
    by_name = defaultdict(set)
    for i, g in groups.items():
        if not company_label_reason(g['name'], bool(evidence and evidence.company_attested(g['name']))):
            by_name[normal_name(g['name'])].add(i)
    for i, g in groups.items():
        if i not in merge_into:
            if evidence and evidence.company_attested(g['name']) and re.match(r'^(?:FAO|FBO)\s', g['name']):
                continue
            _, hits = company_tail(g['name'], lambda tail: by_name.get(normal_name(tail), ()),
                                   lambda s: company_label_reason(s, bool(evidence and evidence.company_attested(s))))
            if len(hits) == 1 and i not in hits:
                merge_into[i] = next(iter(hits))
    from ocr_consensus import Consensus  # spellings re-read from the scans by >=2 OCR engines
    for member, target in Consensus().pairs({i: g['name'] for i, g in groups.items()}):
        if member not in merge_into:
            merge_into[member] = target
    merge_into.update(ocr_spellings(groups, merge_into, evidence))  # after reviewed merges, never against them
    merge_into.update(squashed_spellings(groups, merge_into, evidence))
    for i in list(merge_into):
        target = merge_root(i, merge_into, groups)
        if target == i or i not in groups:
            continue
        g, t = groups.pop(i), groups[target]
        t.setdefault('review_sources', set()).update(g.get('review_sources', set()))
        t['kinds'].update(g['kinds'])
        t['relevance'].update(g['relevance'])
        t['keys'] += g['keys']
        t['agree'] &= g['agree']
        t['kg'] = t['kg'] or g['kg']
    return groups, dropped


def resolve_company_groups(groups, existing, key=None, lookup=None, evidence=None, airports=None):
    """Repair reviewed company labels against a frozen set of full names.

    Preserve each group's extraction keys and evidence. Retyped addresses keep
    their existing contact ID when available; no private person alias is emitted.
    Imports stay local because the extraction index also uses normal_name here.
    """
    from entity_build_resolution import BuildIndex, wrong_type, website_domain
    from build_communications import ocr_repair
    index_nodes = dict(existing)
    for identity, group in groups.items():
        index_nodes.setdefault(identity, {'id': identity, 'name': group['name'], 'entity_type': 'organization'})
    index = BuildIndex(index_nodes, key=key, lookup=lookup, evidence=evidence, airports=airports)
    result = {}
    for identity, group in groups.items():
        group = {**group}
        name, target, rule = index.company_repair(group['name'], exclude={identity})
        kind, fields = wrong_type(name)
        place_target, place_kind, place_fields, place_rule = index.cross_type(name, 'organization', exclude={identity})
        if place_kind == 'location':
            kind, fields, target, rule = place_kind, place_fields, place_target, place_rule
        if kind == 'email':
            hits = index.email_hits(name)
            target = index.choose(hits)
            name = '@'.join(ocr_repair(name))
            rule = 'company_email_contact'
        elif kind == 'website':
            name = website_domain(name)
            hits = {i for i in index.hits(name) if index_nodes[i]['entity_type'] == 'website'}
            target = index.choose(hits)
            rule = 'company_website'
        group['name'] = name
        if target is not None:
            identity = target
            node = dict(index_nodes[target])
            # A private alias matching a person reuses the person's safe label.
            group['name'] = node['name']
            group['existing_node'] = node
            group['resolution_rule'] = rule
        elif kind:
            group['existing_node'] = {'id': identity, 'name': name, 'entity_type': kind, **fields}
            group['resolution_rule'] = rule or 'company_wrong_type'
        if identity not in result:
            result[identity] = group
        else:
            retained = result[identity]
            for field in ('kinds', 'relevance'):
                retained[field].update(group[field])
            retained['keys'] += group['keys']
            retained.setdefault('review_sources', set()).update(group.get('review_sources', ()))
            retained['agree'] &= group['agree']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--per-company', type=int, default=6, help='strongest links kept per company')
    parser.add_argument('--view-edges', type=int, default=800, help='company links exported to the viewer')
    parser.add_argument('--min-shared', type=int, default=3)
    parser.add_argument('--min-shared-public', type=int, default=1, help='shared documents needed for a link to a KG public figure')
    parser.add_argument('--max-entities-per-doc', type=int, default=40)
    args = parser.parse_args()
    started = time.time()
    rows = list(csv.DictReader(open(CANDIDATES)))
    overrides = json.loads(REVIEW.read_text()) if REVIEW.exists() else {}
    survivor_names = json.loads((ROOT / 'config/survivors.json').read_text())['names']
    entities = json.loads((OUT / 'normalized_entities.json').read_text())
    by_name = {e['name']: e for e in entities}
    public = {e['id'] for e in entities if json.loads(e['metadata'] or '{}').get('public_figure')}
    from entity_build_evidence import LabelEvidence
    from airports import Airports
    evidence = LabelEvidence.project()
    from build_communications import never_merge_pairs
    groups, dropped = select(rows, verdicts(PRIMARY), verdicts(SECOND), overrides, survivor_names,
                             verdicts(THIRD), evidence, never_merge_pairs())
    evidence.prepare_companies(g['name'] for g in groups.values())
    from bounded_json import items
    from build_contacts import contact_key
    existing = {node['id']: node for node in entities}
    for filename in ('communication_nodes.json', 'contact_nodes.json', 'place_nodes.json', 'vehicle_nodes.json'):
        if (OUT / filename).exists():
            for node in items(OUT / filename):
                existing.setdefault(node['id'], node)
    groups = resolve_company_groups(groups, existing, contact_key(), ROOT / 'data/private/contact_lookup.json', evidence, Airports())

    # Documents per company node (all folded spellings).
    key_node = {k: node for node, g in groups.items() for k in g['keys']}
    company_docs = defaultdict(set)
    with closing(sqlite3.connect(f'file:{MENTIONS}?mode=ro', uri=True)) as db:
        for key, efta in db.execute('SELECT key, efta FROM mentions WHERE efta IS NOT NULL'):
            if key in key_node:
                company_docs[key_node[key]].add(efta)
    # Curated seeds: names written without a legal form ("Carbyne", "Clipper Realty"), found by FTS phrase.
    seeds = overrides.get('seeds', [])
    if seeds:
        db = corpus.connect()
        page_doc, doc_names = page_map()
        by_normal = {normal_name(g['name']): i for i, g in groups.items()}
        for seed in seeds:
            docs = set()
            for phrase in seed['phrases']:
                pages = corpus.search(db, '"' + ' '.join(re.findall(r'\w+', phrase)) + '"')
                docs |= {doc_names[d] for d in page_doc[pages].tolist() if d >= 0}
            node_id = by_normal.get(normal_name(seed['name'])) or by_normal.get(normal_name(seed.get('join', ''))) \
                or company_id(seed['name'])
            if node_id not in groups:
                groups[node_id] = {'name': seed['name'], 'kinds': Counter({seed.get('kind', 'company'): 1}),
                                   'relevance': Counter({seed.get('relevance', 'network'): 1}), 'keys': [], 'agree': True, 'kg': None}
            groups[node_id].setdefault('review_sources', set()).add('curated seed')
            groups[node_id]['name'] = seed['name']
            groups[node_id]['keys'] += [f'seed: {p}' for p in seed['phrases']]
            company_docs[node_id] |= docs
        db.close()
    # The same companies written without their legal form, where the bare name is specific enough.
    people = {normal_name(e['name']) for e in entities if e['entity_type'] == 'person'}
    registry = ROOT / 'data/raw/persons_registry.json'
    registry_names = [p['name'] for p in json.loads(registry.read_text())] if registry.exists() else []
    given = {n.split()[0] for n in map(normal_name, registry_names + [e['name'] for e in entities if e['entity_type'] == 'person'])
             if len(n.split()) >= 2 and n.split()[0].isalpha() and len(n.split()[0]) > 1}
    places = {normal_name(e['name']) for e in entities if e['entity_type'] in ('location', 'property', 'aircraft')}
    dictionary = {w.casefold() for w in DICTIONARY.read_text(errors='ignore').split()} if DICTIONARY.exists() else set()
    phrases = formless_phrases(groups, people, places, given, dictionary, evidence)
    if phrases:
        page_doc, doc_names = page_map()
        items = sorted(phrases.items(), key=lambda item: (str(item[0]), item[1]))  # KG ids are int, others str
        added = 0
        for (node_id, base), pages in zip(items, corpus.parallel_search(['"' + base + '"' for _, base in items])):
            found = {doc_names[d] for d in page_doc[pages].tolist() if d >= 0}
            have = company_docs[node_id]
            if have and len(found | have) <= MAX_FORMLESS_RATIO * len(have):
                groups[node_id]['formless'] = (base, len(found - have))
                added += len(found - have)
                have |= found
        print(f'{sum("formless" in g for g in groups.values())} of {len(phrases)} bare names searched add {added} documents', flush=True)
    nodes, kg_docs = [], {}
    hidden_rules = Counter()
    for node_id, g in groups.items():
        attested = evidence.company_attested(g['name'])
        reason = company_label_reason(g['name'], attested)
        if 'existing_node' in g:
            known = g['existing_node']
            # Email, website, vehicle and existing person/place targets are not
            # judged with company distinctiveness rules.
            if known['entity_type'] not in ('organization', 'shell_company'):
                reason = known.get('label_quality_reason')
            meta = known.get('metadata') or {}
            meta = json.loads(meta) if isinstance(meta, str) else dict(meta)
            meta.setdefault('document_count', len(company_docs.get(node_id, ())))
            nodes.append({**known, 'metadata': meta,
                          **({'label_quality': 'not_a_name', 'label_quality_reason': reason} if reason else {}),
                          'company_resolution_rule': g.get('resolution_rule')})
            kg_docs[node_id] = company_docs.pop(node_id, set())
            continue
        if reason:
            hidden_rules[reason] += 1
        if g['kg'] and g['kg'] in by_name:
            known = by_name[g['kg']]
            relevance = next((r for r in KEEP_RELEVANCE if g['relevance'][r]), g['relevance'].most_common(1)[0][0])
            meta = known.get('metadata') or {}
            meta = json.loads(meta) if isinstance(meta, str) else dict(meta)
            meta.update({'relevance': relevance, 'document_count': len(company_docs[node_id])})
            nodes.append({**known, 'relevance': relevance, 'metadata': meta,
                          **({'label_quality': 'not_a_name', 'label_quality_reason': reason} if reason else {})})
            kg_docs[known['id']] = company_docs.pop(node_id, set())  # extends the KG node
            continue
        kind = g['kinds'].most_common(1)[0][0]
        reason = visibility_reason(g['name'], kind, evidence, g.get('explicit_rejection', False))
        relevance = next((r for r in KEEP_RELEVANCE if g['relevance'][r]), g['relevance'].most_common(1)[0][0])
        nodes.append({'id': node_id, 'name': g['name'], 'entity_type': 'shell_company' if relevance == 'epstein_controlled' else 'organization',
                      'relevance': relevance, **({'label_quality': 'not_a_name', 'label_quality_reason': reason} if reason else {}),
                      'source': 'full-text legal name (scripts/extract_companies.py)',
                      'metadata': {'company_kind': kind, 'relevance': relevance, 'second_reader_agrees': g['agree'],
                                   'review_sources': sorted(g.get('review_sources', {'curated seed'})),
                                   'document_count': len(company_docs[node_id]), 'spellings': sorted(set(g['keys']))[:12],
                                   **({'without_legal_form': {'phrase': g['formless'][0], 'documents_added': g['formless'][1]}}
                                      if 'formless' in g else {})}})
    print(f'{len(nodes)} company nodes, {len(kg_docs)} KG organizations extended, dropped {dict(dropped)} '
          f'({time.time() - started:.1f}s)', flush=True)

    # KG entity documents (comention layer), restricted to documents naming a company.
    wanted = set().union(*company_docs.values(), *kg_docs.values()) if company_docs or kg_docs else set()
    _, doc_names = page_map()
    cluster = doc_clusters(doc_names)
    cluster_of = dict(zip(doc_names, cluster.tolist()))
    total_docs = len(set(cluster_of.values()))
    entity_docs, extended_clusters = defaultdict(set), defaultdict(set)
    with closing(sqlite3.connect(f'file:{OUT / "comention_graph.db"}?mode=ro', uri=True)) as db:
        for entity_id, efta in db.execute('SELECT entity_id, efta_number FROM mentions'):
            if efta in wanted:
                entity_docs[entity_id].add(efta)
            if entity_id in kg_docs:
                extended_clusters[entity_id].add(cluster_of.get(efta, efta))
    for entity_id, docs in kg_docs.items():
        entity_docs[entity_id] |= docs
        extended_clusters[entity_id] |= {cluster_of.get(d, d) for d in docs}
    doc_members = defaultdict(list)
    for owner in (company_docs, entity_docs):
        for x, docs in owner.items():
            for d in docs:
                doc_members[d].append(x)
    # Distinct-document counts for lift: companies use their own, KG entities their full comention count
    # (extended organizations: the union of their comention and company documents).
    with closing(sqlite3.connect(f'file:{OUT / "comention_graph.db"}?mode=ro', uri=True)) as db:
        full_count = dict(db.execute('SELECT id, distinct_documents FROM entities'))
    distinct = lambda docs: len({cluster_of.get(d, d) for d in docs})
    size = {**{x: distinct(d) for x, d in company_docs.items()},
            **{x: max(full_count.get(x, 0), distinct(d)) for x, d in entity_docs.items()},
            **{x: len(c) for x, c in extended_clusters.items()}}

    def company_pairs(members):
        for c in members:
            if isinstance(c, str):
                for other in members:
                    if other != c and not (isinstance(other, str) and other < c):  # company pairs once
                        yield c, other
    shared, weighted, samples = cluster_pair_counts(
        doc_members, lambda d: cluster_of.get(d, d), company_pairs, args.max_entities_per_doc)
    pairs = []
    for (a, b), count in shared.items():
        if count < (args.min_shared_public if a in public or b in public else args.min_shared):
            continue
        lift = count * total_docs / (size[a] * size[b])
        pairs.append({'a': a, 'b': b, 'shared': count, 'specificity': round(weighted[a, b], 3), 'lift': round(lift, 2),
                      'sample': [d for _, d in sorted(samples[a, b])[:SAMPLE_LIMIT]]})
    score = lambda p: p['specificity'] * min(p['lift'], 10) ** 0.5
    pairs.sort(key=lambda p: (-score(p), str(p['a']), str(p['b'])))
    kept, per = [], Counter()
    for p in pairs:
        if p['lift'] < 1 or p['specificity'] <= 0:
            continue
        ends = [x for x in (p['a'], p['b']) if isinstance(x, str)]
        if all(per[x] >= args.per_company for x in ends):
            continue
        for x in ends:
            per[x] += 1
        kept.append(p)
    print(f'{len(pairs)} pairs with >= {args.min_shared} shared documents ({args.min_shared_public} with a public figure), {len(kept)} kept '
          f'({time.time() - started:.1f}s)', flush=True)

    partial = OUT / 'company_graph.partial.db'
    partial.unlink(missing_ok=True)
    with closing(sqlite3.connect(partial)) as db:
        db.executescript('''
            CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT, entity_type TEXT, document_count INTEGER, metadata TEXT);
            CREATE TABLE mentions(entity_id TEXT, efta_number TEXT);
            CREATE TABLE pairs(a TEXT, b TEXT, shared_documents INTEGER, specificity_score REAL, lift REAL, kept INTEGER, sample_eftas TEXT);
        ''')
        db.executemany('INSERT INTO entities VALUES (?,?,?,?,?)',
                       [(n['id'], n['name'], n['entity_type'], n['metadata']['document_count'], json.dumps(n['metadata'])) for n in nodes])
        db.executemany('INSERT INTO mentions VALUES (?,?)', ((c, d) for owner in (company_docs, kg_docs) for c, docs in owner.items() for d in docs))
        kept_ids = {id(p) for p in kept}
        db.executemany('INSERT INTO pairs VALUES (?,?,?,?,?,?,?)',
                       [(str(p['a']), str(p['b']), p['shared'], p['specificity'], p['lift'], id(p) in kept_ids, json.dumps(p['sample'])) for p in pairs])
        db.executescript('CREATE INDEX mentions_entity ON mentions(entity_id, efta_number); CREATE INDEX mentions_efta ON mentions(efta_number);')
        db.commit()
    partial.replace(OUT / 'company_graph.db')

    # Every Epstein-controlled or curated company gets its two strongest links into the view; the rest by score.
    seeded = {i for i, g in groups.items() if any(k.startswith('seed: ') for k in g['keys'])}
    shells = {n['id'] for n in nodes if n['entity_type'] == 'shell_company' or n['id'] in seeded}  # + curated seeds
    sure, count = [], Counter()
    for p in kept:
        ends = [x for x in (p['a'], p['b']) if x in shells and count[x] < 2]
        if ends:
            sure.append(p)
            count.update(ends)
    chosen = {id(p) for p in sure}
    view = sure + [p for p in kept if id(p) not in chosen][:max(0, args.view_edges - len(sure))]
    view.sort(key=lambda p: -score(p))
    urls = corpus.document_urls(sorted({d for p in view for d in p['sample']}))
    edges = [{'id': f"company_{p['a']}_{p['b']}", 'source_entity_id': p['a'], 'target_entity_id': p['b'],
              'relationship_type': 'co_mentioned_in_documents', 'weight': p['shared'], 'date_first': None, 'date_last': None,
              'metadata': {'meaning': MEANING, 'new_link': True, 'layer': 'companies', 'shared_documents': p['shared'],
                           'specificity_score': p['specificity'], 'lift_vs_chance': p['lift'],
                           'documents_mentioning_source': size[p['a']], 'documents_mentioning_target': size[p['b']],
                           'sample_documents': [{'efta_number': d, **({'pdf_url': urls[d]} if d in urls else {})} for d in p['sample']],
                           'complete_evidence': 'output/company_graph.db (mentions, pairs)'}} for p in view]
    linked = {x for e in edges for x in (e['source_entity_id'], e['target_entity_id'])}
    (OUT / 'company_nodes.json').write_text(json.dumps(nodes, indent=2, ensure_ascii=False) + '\n')
    (OUT / 'company_edges.json').write_text(json.dumps(edges, indent=2, ensure_ascii=False) + '\n')
    summary = {'company_nodes': len(nodes), 'company_nodes_in_view': sum(n['id'] in linked for n in nodes),
               'kg_organizations_extended': len(kg_docs), 'pairs': len(pairs), 'view_edges': len(edges),
               'dropped': dict(dropped), 'hidden_label_rules': dict(hidden_rules), 'types': dict(Counter(n['entity_type'] for n in nodes)),
               'seconds': round(time.time() - started, 1)}
    (OUT / 'company_summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
