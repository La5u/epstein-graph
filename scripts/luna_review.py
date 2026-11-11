"""PRIVATE, local-only: GPT-6-Luna review of the viewer's header names and duplicates.

    .venv/bin/python scripts/luna_review.py classify [--model gpt-6-luna] [--limit N]
    .venv/bin/python scripts/luna_review.py merges   [--model gpt-6-luna] [--limit N]
    .venv/bin/python scripts/luna_review.py apply    # write accepted verdicts to config/

classify  every viewer participant (an email-header name that matched no graph
          person): person, organization, role_or_group or boilerplate.
merges    candidate pairs of viewer nodes that may be one identity (same surname,
          first initial or nickname, an address spelling a name, a first-name-only
          header written on an address), each with its evidence: labels, record
          counts, header names on the address, shared neighbours. Luna answers
          same / different / unsure.
apply     verdicts on which both model runs agree at high confidence:
          organization -> config/node_review.json ('ok_entity': drawn as an
          organization); 'same' -> config/identity_merges.json (IDs and a generic
          reason only).

Real labels are sent to the model (`sol -m <model>`) and the raw answers stay
in data/private/luna_review/; config/ only ever gets IDs and fixed codes.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import numpy as np
from rapidfuzz import fuzz, process

from build_communications import FUNCTION_WORDS, NICKNAMES, edit_distance, header_name_words, legal_form, name_skeleton_words
from header_context_resolution import decorated_name
from header_text import header_junk_reason

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output'
PRIVATE = ROOT / 'data/private'
REVIEW = PRIVATE / 'luna_review'
BATCH = 40


def private_write(path, text):
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)


DEEPSEEK = 'opencode-go/deepseek-v4.1-flash'  # unlimited plan (2026-10-30)


def run_name(model):
    """Answer-file name of a model: opencode-go/glm-5.3 -> glm-5.3."""
    return model.rsplit('/', 1)[-1]


def ask(prompt, model, effort='low'):
    """JSON objects from the model's answer, one per line."""
    if model == 'deepseek' or model.startswith(('opencode/', 'opencode-go/')):  # hosted model, no tools; a call can hang
        try:
            result = subprocess.run(['opencode', 'run', '--pure', '-m', DEEPSEEK if model == 'deepseek' else model],
                                    input=prompt, capture_output=True, text=True, timeout=900)  # 20 pairs take ~4-5 min
        except subprocess.TimeoutExpired:
            return []
    else:
        result = subprocess.run(['sol', '-m', model, '-e', effort], input=prompt, capture_output=True, text=True, timeout=1800)
    if result.returncode == 75:
        raise SystemExit('sol: usage limit reached')
    out = []
    for line in result.stdout.splitlines():
        line = line.strip().strip(',')
        if line.startswith('{'):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def load_view():
    """(nodes {id: entity}, labels {id: real label}, neighbours {id: Counter(other id: records)})."""
    entities = json.loads((OUT / 'graph_entities.json').read_text())
    relationships = json.loads((OUT / 'graph_relationships.json').read_text())
    lookup = {}
    for name in ('contact_lookup.json', 'address_lookup.json'):
        path = PRIVATE / name
        if path.exists():
            lookup.update({k: v['value'] for k, v in json.loads(path.read_text()).items()})
    nodes = {e['id']: e for e in entities}
    from ocr_consensus import Consensus
    scan_spellings = Consensus()
    labels = {i: scan_spellings.correct(lookup.get(i, e['name'])) for i, e in nodes.items()}
    neighbours = defaultdict(Counter)
    for r in relationships:
        a, b = r['source_entity_id'], r['target_entity_id']
        if a != b:
            neighbours[a][b] += r.get('weight') or 1
            neighbours[b][a] += r.get('weight') or 1
    return nodes, labels, neighbours


def header_names(ids):
    """{address or name id: Counter(label id: records)}: which names were written
    with which addresses in parsed headers (both directions)."""
    found = defaultdict(Counter)
    db_path = OUT / 'communication_graph.db'
    with closing(sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)) as db:
        final = dict(db.execute('SELECT member_id, canonical_id FROM identity_map'))
        view = lambda x: int(x[3:]) if x.startswith('kg:') else x
        for name, address, n in db.execute("""SELECT source_id, target_id, count(*) FROM evidence WHERE relationship_type IN
                ('header_label_association', 'header_name_association', 'upstream_resolved_header_identity') GROUP BY 1, 2"""):
            name, address = view(final.get(name, name)), view(final.get(address, address))
            if address in ids:
                found[address][name] += n
            if name in ids:
                found[name][address] += n
    return found


def describe(node_id, nodes, labels, neighbours, headers):
    e = nodes[node_id]
    top = ', '.join(f'{labels.get(o, o)} ({n})' for o, n in neighbours[node_id].most_common(5))
    said = ', '.join(f'{labels.get(o, o)} ({n})' for o, n in headers.get(node_id, Counter()).most_common(4))
    return (f'{labels[node_id]!r} [{e["entity_type"]}; {sum(neighbours[node_id].values())} records]'
            + (f'; written together in headers with: {said}' if said else '') + (f'; top links: {top}' if top else ''))


# ---- classify --------------------------------------------------------------------
def classify(args):
    nodes, labels, neighbours = load_view()
    ids = [i for i, e in nodes.items() if e['entity_type'] == 'participant']
    headers = header_names(set(ids))
    if args.not_person_by:  # second opinion only where it can change something
        other = json.loads((REVIEW / f'classify_{args.not_person_by}.json').read_text())
        ids = [i for i in ids if i in other and not (other[i].get('kind') == 'person' and other[i].get('confidence') == 'high')]
    # Names classified in an earlier run keep their answer (only new names are sent).
    path = REVIEW / f'classify_{run_name(args.model)}.json'
    earlier = json.loads(path.read_text()) if path.exists() else {}
    items = [(i, describe(i, nodes, labels, neighbours, headers)) for i in ids if i not in earlier][:args.limit or None]
    print(len(ids), 'names,', len(items), 'new', flush=True)
    prompt = ('Each line is a name taken from the From/To/Cc header of emails in the Epstein files, with the addresses it was '
              'written with and its strongest links. Decide what it names: "person" (an individual, even a first name or '
              'initials), "organization" (company, shop, bank, newspaper, school, government office, brand, service account), '
              '"role_or_group" (an office, team, family, assistant desk, mailing list) or "boilerplate" (header junk, OCR '
              'noise, a sentence). Answer ONLY JSON lines in input order: {"id": ..., "kind": ..., "confidence": "high|low"}.\n\n')
    results = {**earlier, **run_batches(prompt, items, args.model)}
    private_write(path, json.dumps(results, indent=1, ensure_ascii=False) + '\n')
    print(json.dumps(Counter((r.get('kind'), r.get('confidence')) for r in results.values()).most_common(), indent=1))


def run_batches(prompt, items, model):
    # DeepSeek: unlimited but slow on long prompts, so smaller batches. Each opencode run takes
    # about 485 MB, so no more calls at once than Sol gets.
    size, workers = (20, 6) if model == 'deepseek' or model.startswith('opencode') else (BATCH, 6)
    batches = [items[i:i + size] for i in range(0, len(items), size)]
    def one(batch):
        return ask(prompt + '\n'.join(f'{i}\t{text}' for i, text in batch), model)
    results = {}
    with ThreadPoolExecutor(workers) as pool:
        for answers in pool.map(one, batches):
            for answer in answers:
                if 'id' in answer:
                    results[str(answer['id'])] = answer
    return results


# ---- merges ----------------------------------------------------------------------
LOOKALIKE = [set(group) for group in ('il1|!jt', 'o0dq', 'ec', 'sz5', 'b8h', 'gq9', 'uv', 'nh', 'a@')]
MULTI = {('rn', 'm'), ('vv', 'w'), ('cl', 'd'), ('li', 'h'), ('ii', 'u')}


def ocr_similarity(a, b):
    """0..1: aligned letters score 1 when equal, 0.6 when OCR look-alikes (e/c,
    i/l/1/j, o/0, and rn/m-style pairs), 0 otherwise; divided by the longer length."""
    a, b = re.sub(r'[^a-z0-9]', '', a.casefold()), re.sub(r'[^a-z0-9]', '', b.casefold())
    if not a or not b:
        return 0.0
    def sub(x, y):
        return 0.0 if x == y else 0.4 if any(x in g and y in g for g in LOOKALIKE) else 1.0
    # Weighted edit distance; a two-letter misread (rn/m) costs like one look-alike.
    d = [[0.0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = float(i)
    for j in range(len(b) + 1):
        d[0][j] = float(j)
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + sub(a[i - 1], b[j - 1]))
            for long, short in MULTI:
                for x, y in ((long, short), (short, long)):
                    if a[max(0, i - len(x)):i] == x and b[max(0, j - len(y)):j] == y:
                        d[i][j] = min(d[i][j], d[i - len(x)][j - len(y)] + 0.4)
    return max(0.0, 1 - d[-1][-1] / max(len(a), len(b)))


NOT_NAME_WORDS = {'good', 'morning', 'afternoon', 'evening', 'night', 'day', 'hi', 'hello', 'dear', 'best', 'wishes',
                  'check', 'know', 'sounds', 'miss', 'got', 'tried', 'call', 'thanks', 'regards', 'yet', 'per', 'name',
                  'executive', 'assistant', 'vice', 'president', 'managing', 'director', 'office', 'wealth', 'management',
                  'banking', 'private', 'express', 'retail'}


def candidate_pairs(nodes, labels, neighbours, *, include_spelling=True):
    """Strong structural pairs first; at most six generic candidates per node.

    Clear one-edit / reordered pairs cannot be starved by generic candidates.
    Similarity matrices are bounded to 128 rows, including on a large snapshot.
    """
    kinds = ('person', 'participant', 'email', 'organization', 'shell_company')
    ids = [i for i, e in nodes.items() if e['entity_type'] in kinds and not e.get('suspected_parse_artifact')]
    words = {i: name_skeleton_words(labels[i].split('@')[0] if nodes[i]['entity_type'] == 'email' else labels[i]) for i in ids}
    first_name = lambda w: NICKNAMES.get(w, w)
    by_last, by_first = defaultdict(list), defaultdict(list)
    for i in ids:
        if nodes[i]['entity_type'] != 'email' and words[i]:
            # Companies share their "surname" (Trust, LLC, Advisors) with unrelated companies.
            if len(words[i]) >= 2 and len(words[i][-1]) >= 3 and nodes[i]['entity_type'] in ('person', 'participant'):
                by_last[words[i][-1]].append(i)
            by_first[first_name(words[i][0])].append(i)
    pairs = {}
    def add(a, b, why, score):
        key = tuple(sorted((a, b), key=str))
        if a != b and (key not in pairs or pairs[key][1] < score):
            pairs[key] = (why, score)
    # Mistyped people and names emitted by another layer still need comparison.
    # These are review candidates only: cross-type equality never asserts identity.
    literal = {i: header_name_words(labels[i]) for i in ids if nodes[i]['entity_type'] != 'email'
               and not header_junk_reason(labels[i])}
    bags, firsts = defaultdict(list), defaultdict(list)
    for i, w in literal.items():
        if len(w) >= 2:
            bags[tuple(sorted(w))].append(i)
            firsts[w[0]].append(i)
    for group in bags.values():
        for x, a in enumerate(group):
            for b in group[x + 1:]:
                add(a, b, 'same name words, different order or punctuation', 6)
    exact = defaultdict(list)
    for i, w in literal.items():
        exact[w].append(i)
    for i in literal:
        core, rule = decorated_name(labels[i])
        if rule:
            for j in exact.get(header_name_words(core), ()):
                add(i, j, 'complete name with header decoration or credentials', 5.5)
    for group in firsts.values():
        for x, a in enumerate(group):
            for b in group[x + 1:]:
                if min(len(literal[a][-1]), len(literal[b][-1])) >= 6 and edit_distance(literal[a][-1], literal[b][-1], 1) == 1:
                    add(a, b, 'same first name, one-edit surname', 5.5)
    for group in by_last.values():
        for x in range(len(group)):
            for y in range(x + 1, len(group)):
                a, b = group[x], group[y]
                fa, fb = first_name(words[a][0]), first_name(words[b][0])
                if fa == fb or fa[0] == fb[0] and min(len(fa), len(fb)) == 1 or fa.startswith(fb) or fb.startswith(fa):
                    add(a, b, 'same surname, compatible first name', 3)
                elif min(len(fa), len(fb)) >= 3 and fa[:3] == fb[:3]:  # Rudy/Rudolph, Lesley/Leslie, Gini/Ginevra
                    add(a, b, 'same surname, short form or spelling of the first name', 2.5)
    for i in ids:
        if nodes[i]['entity_type'] == 'participant' and len(words[i]) == 1 and len(words[i][0]) >= 3:
            for j in by_first.get(first_name(words[i][0]), []):  # "Irina" and an Irina in the graph
                if j != i and len(words[j]) >= 2:
                    add(i, j, 'first-name-only header name', 1 + (j in neighbours.get(i, {})))
        if nodes[i]['entity_type'] == 'email':
            local = ''.join(name_skeleton_words(labels[i].split('@')[0]))
            for j in ids:
                w = words[j]
                if nodes[j]['entity_type'] != 'email' and len(w) >= 2 and len(w[-1]) >= 4 and w[-1] in local \
                        and (w[0][0] == local[0] or local.startswith(w[-1]) or w[0] in local):
                    add(i, j, 'address spells the name', 3)
                elif nodes[j]['entity_type'] != 'email' and len(w) == 1 and len(w[0]) >= 4 and local.startswith(w[0]):
                    add(i, j, 'address starts with the header name', 2)
    # A phrase or longer label that starts with a full name: "Sigrid S. McCawley - Boies",
    # "EBATIS MRUHASOG I INTERIOR DESIGN" (initials not counted). Plain spelling scores miss
    # these: the tail outweighs the name.
    def name_words(i):
        return tuple(w for w in name_skeleton_words(labels[i]) if len(w) > 1)
    full_names = {}
    for j in ids:
        w = name_words(j)
        plain = set(re.findall(r'[a-z]+', labels[j].casefold()))
        if nodes[j]['entity_type'] in ('person', 'participant') and len(w) >= 2 and labels[j][:1].isupper() \
                and not plain & (FUNCTION_WORDS | NOT_NAME_WORDS):  # "Good morning", "On The", "Check in"
            if w not in full_names or sum(neighbours.get(j, {}).values()) > sum(neighbours.get(full_names[w], {}).values()):
                full_names[w] = j
    for i, e in nodes.items():
        if e['entity_type'] in ('participant', 'phrase') and not e.get('suspected_parse_artifact'):
            w = name_words(i)
            for n in range(len(w) - 1, 1, -1):
                if (j := full_names.get(w[:n])) is not None and j != i:
                    add(i, j, 'full name plus extra words', 3.5)
                    break
    # Companies written with and without their legal form ("The McClatchy Company" /
    # "The McClatchy Company, LLC"); two different forms (Inc / LLC) stay apart on purpose.
    by_base = defaultdict(list)
    for i in ids:
        if nodes[i]['entity_type'] in ('organization', 'shell_company', 'participant'):
            base, form = legal_form(labels[i])
            if base:
                by_base[base].append((i, form))
    for group in by_base.values():
        for x in range(len(group)):
            for y in range(x + 1, len(group)):
                (a, fa), (b, fb) = group[x], group[y]
                if not fa or not fb or fa == fb:
                    add(a, b, 'same company name, legal form missing or respelled', 3.5)
    # All pairs of names, scored in bounded chunks; the slow OCR
    # similarity only runs on pairs a plain edit-distance screen lets through.
    named = [i for i in ids if nodes[i]['entity_type'] != 'email']
    texts = [labels[i].casefold() for i in named]
    for start in range(0, len(texts) if include_spelling else 0, 128):
        chunk = texts[start:start + 128]
        similar = process.cdist(chunk, texts, scorer=fuzz.token_sort_ratio, score_cutoff=88, dtype=np.uint8, workers=1)
        screen = process.cdist(chunk, texts, scorer=fuzz.ratio, score_cutoff=60, dtype=np.uint8, workers=1)
        for x, y in zip(*np.nonzero((similar > 0) | (screen > 0))):
            if start + x >= y:
                continue
            a, b = named[start + x], named[y]
            if similar[x, y]:
                add(a, b, 'similar spelling', 2)
            elif abs(len(labels[a]) - len(labels[b])) <= 3 and min(len(labels[a]), len(labels[b])) >= 7 \
                    and ocr_similarity(labels[a], labels[b]) >= 0.85:
                add(a, b, 'OCR look-alike spelling', 2)
    per_node, chosen = Counter(), []
    for (a, b), (why, score) in sorted(pairs.items(), key=lambda item: (-item[1][1], tuple(map(str, item[0])))):
        if score >= 5 or per_node[a] < 6 and per_node[b] < 6:
            per_node[a] += 1
            per_node[b] += 1
            chosen.append((a, b, why))
    return chosen


MERGE_PROMPT = ('Each line is a pair of nodes from a graph built from the Epstein files (email headers, flight logs, documents). '
                'Decide whether A and B are the same real-world identity: a person, a person and their own email address, or one '
                'company (its name with and without the legal form, a respelling, or a header name that is the company\'s name). '
                'Say "same" only with positive evidence: header names written on the address, a name that can only be this '
                'person or company, the same contacts other than Jeffrey Epstein (nearly everyone shares him), or for a first name '
                'alone a full name that clearly out-shares every other person with that first name. Namesakes, family members '
                '(Mr./Mrs.), shared/assistant mailboxes, a person and a company named after them (Jeff Koons / Jeff Koons LLC), '
                'two legal forms (Inc / LLC) and related companies (parent, subsidiary, fund) are "different". Otherwise "unsure". Answer ONLY JSON lines in input order: {"id": "<A>|<B>", "verdict": '
                '"same|different|unsure", "confidence": "high|low", "reason": "<= 15 words"}.\n\n')


def review_label_signature(key, labels):
    """Bind a cached verdict to its labels as well as IDs. Missing/stale IDs fail closed."""
    ids = [node_key(i) for i in key.split('|')]
    if len(ids) != 2 or any(i not in labels for i in ids):
        return None
    text = json.dumps([(str(i), labels[i].strip().casefold()) for i in sorted(ids, key=str)], ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def current_review(key, answer, labels):
    signature = review_label_signature(key, labels)
    return signature is not None and answer.get('label_signature') == signature


def merges(args):
    nodes, labels, neighbours = load_view()
    pairs = candidate_pairs(nodes, labels, neighbours)[:args.limit or None]
    if args.same_by:  # second opinion on the pairs another model called the same
        other = json.loads((REVIEW / f'merges_{args.same_by}.json').read_text())
        pairs = [p for p in pairs if other.get(f'{p[0]}|{p[1]}', {}).get('verdict') == 'same']
    if args.undecided_by:  # only pairs another model was not sure about
        other = json.loads((REVIEW / f'merges_{args.undecided_by}.json').read_text())
        pairs = [p for p in pairs if not (other.get(f'{p[0]}|{p[1]}', {}).get('confidence') == 'high'
                                          and current_review(f'{p[0]}|{p[1]}', other[f'{p[0]}|{p[1]}'], labels)
                                          and other[f'{p[0]}|{p[1]}'].get('verdict') in ('same', 'different'))]
    # Old files have no label signature: retain their separation vetoes, but
    # re-review these pairs rather than trusting a verdict on a previous label.
    path = REVIEW / f'merges_{run_name(args.model)}.json'
    earlier = json.loads(path.read_text()) if path.exists() else {}
    # Answers from before companies were allowed (COMPANY_PROMPT) called every company pair "different".
    known = {k: v for k, v in earlier.items() if v.get('verdict') and current_review(k, v, labels)
             and not (args.recheck_unsure and v['verdict'] == 'unsure')
             and not (args.recheck_companies and is_company_pair(k, nodes) and not v.get(COMPANY_PROMPT))}
    print(len(pairs), 'pairs,', sum(f'{a}|{b}' in known for a, b, _ in pairs), 'already judged', flush=True)
    pairs_all, pairs = pairs, [(a, b, why) for a, b, why in pairs if f'{a}|{b}' not in known]
    global LABELS
    LABELS = labels
    headers = header_names(set(nodes))
    items = [(f'{a}|{b}', f'({pair_evidence(a, b, why, nodes, labels, neighbours, headers)}) '
                          f'A: {describe(a, nodes, labels, neighbours, headers)} || B: {describe(b, nodes, labels, neighbours, headers)}')
             for a, b, why in pairs]
    prompt = MERGE_PROMPT
    results = {**earlier, **{k: {**v, COMPANY_PROMPT: True, 'label_signature': review_label_signature(k, labels)}
                            for k, v in run_batches(prompt, items, args.model).items()}}
    for a, b, why in pairs_all:
        results.setdefault(f'{a}|{b}', {})['candidate_rule'] = why
    private_write(path, json.dumps(results, indent=1, ensure_ascii=False) + '\n')
    print(len(pairs), 'new pairs;', json.dumps(Counter((r.get('verdict'), r.get('confidence')) for r in results.values()).most_common()))


# ---- apply -----------------------------------------------------------------------
COMPANY_PROMPT = 'companies_allowed'  # set on answers given with the prompt that allows company merges
COMPANY_TYPES = ('organization', 'shell_company')


def node_key(text):
    return int(text) if re.fullmatch(r'\d+', text) else text


def is_company_pair(key, nodes):
    return any(nodes.get(node_key(x), {}).get('entity_type') in COMPANY_TYPES for x in key.split('|'))


def agreed(kind, field, models=None):
    """{id: answer} where every model run (classify_*.json / merges_*.json, or only
    those of `models`) gave the same `field` value at high confidence. Two models must
    have answered."""
    if models:
        if len(set(models)) != len(models):
            raise SystemExit(f'--models lists a model twice: {" ".join(models)}')
        paths = [REVIEW / f'{kind}_{m}.json' for m in models]
        missing = [p.name for p in paths if not p.exists()]
        if missing:
            raise SystemExit(f'no answers for --models: {", ".join(missing)} missing in {REVIEW}')
    else:
        paths = sorted(REVIEW.glob(f'{kind}_*.json'))
    runs = [json.loads(p.read_text()) for p in paths]
    if len(runs) < 2:
        raise SystemExit(f'need {kind} answers from two models (e.g. --model gpt-6-luna and --model gpt-6.1-sol)')
    out = {}
    for key, answer in runs[0].items():
        answers = [run.get(key, {}) for run in runs]
        if all(a.get('confidence') == 'high' and a.get(field) == answer.get(field) for a in answers):
            out[key] = answer
    return out


LABELS = {}


def first_name_rivals(a, b, nodes, neighbours, headers):
    """(evidence of the full name, [(rival, evidence)]) for a first-name-only label and
    a full name, or None when the pair is not one. Evidence: common contacts plus headers
    written together. Rivals are other full names with that first name (nicknames
    folded: "Robert" and Bob Chaganl), not the full name's own unmerged spellings
    ("Smifo De" for Smifo Dmoh)."""
    short, full = (a, b) if len(name_skeleton_words(LABELS[a])) == 1 else (b, a)
    first_name = lambda w: NICKNAMES.get(w, w)
    short_words, full_words = name_skeleton_words(LABELS[short]), name_skeleton_words(LABELS[full])
    if len(short_words) != 1 or len(full_words) < 2 or first_name(full_words[0]) != first_name(short_words[0]):
        return None
    first = first_name(short_words[0])
    def same_surname(words):  # "Dema" / "De", "Clapp" / "Clapp czli"
        x, y = sorted((words[1], full_words[1]), key=len)
        return len(x) >= 2 and y.startswith(x)
    rivals = [i for i in nodes if i not in (short, full) and nodes[i]['entity_type'] in ('person', 'participant')
              and len(w := name_skeleton_words(LABELS[i])) >= 2 and first_name(w[0]) == first and not same_surname(w)]
    def score(i):
        return len(set(neighbours[short]) & set(neighbours[i])) + headers.get(short, Counter()).get(i, 0)
    return score(full), sorted(((i, score(i)) for i in rivals), key=lambda r: -r[1])


def first_name_owner(a, b, nodes, neighbours, headers):
    """A first-name-only label belongs to the full name it shares the most with. The
    model's pick needs some evidence, and no rival may have as much ("Larry": Visoski,
    Summers, Morrison)."""
    found = first_name_rivals(a, b, nodes, neighbours, headers)
    return bool(found) and found[0] >= 1 and all(n < found[0] for _, n in found[1])


def pair_evidence(a, b, why, nodes, labels, neighbours, headers):
    """The contacts both share, by name, and for a first name the full names competing
    for it: "1 shared link" is mostly just Epstein."""
    shared = sorted(set(neighbours[a]) & set(neighbours[b]), key=lambda o: -min(neighbours[a][o], neighbours[b][o]))
    text = f'{why}; {len(shared)} shared links' + (f" ({', '.join(str(labels.get(o, o)) for o in shared[:6])})" if shared else '')
    found = why == 'first-name-only header name' and first_name_rivals(a, b, nodes, neighbours, headers)
    if found:
        own, rivals = found
        text += (f'; first-name evidence for the full name: {own}; other people with this first name: {len(rivals)}'
                 + (f" (best: {', '.join(f'{labels.get(i, i)} {n}' for i, n in rivals[:3])})" if rivals else ''))
    return text


def apply(args):
    classified = agreed('classify', 'kind', args.classify_models)
    path = ROOT / 'config/node_review.json'
    review = json.loads(path.read_text())
    changed = Counter()
    for identity, answer in classified.items():
        if not identity.startswith('participant_'):
            continue
        # A model's "boilerplate" is never applied: the boilerplate verdict drops the name, and
        # a junk-looking label can be a misread name (user, 2026-10-28).
        verdict = {'organization': 'ok_entity', 'role_or_group': 'group_or_role'}.get(answer.get('kind'))
        current = review['participants'].get(identity, {}).get('verdict')
        # Fill unreviewed names; over a reviewed one only to mark a company.
        if verdict and verdict != current and (current is None or verdict == 'ok_entity'):
            review['participants'][identity] = {'verdict': verdict, 'by': 'luna'}
            changed[verdict] += 1
    path.write_text(json.dumps(review, indent=1, ensure_ascii=False) + '\n')
    config = ROOT / 'config/identity_merges.json'
    merged = json.loads(config.read_text())
    blocked = {frozenset(map(str, pair)) for pair in merged.get('never_merge', []) if isinstance(pair, list)}
    nodes, labels, neighbours = load_view()
    added = skipped = skipped_veto = 0
    global LABELS
    LABELS = labels
    headers = header_names(set(nodes))
    # Agreed pairs form clusters ("Terje Rod-Larsen" and five misspellings); each cluster
    # merges into one canonical. Nodes already merged join through their canonical.
    owner = {m: item['canonical'] for item in merged['merges'] for m in item['members']}
    parent = {}
    def root(x):
        x = owner.get(x, x)
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    reasons = {}
    # Any reviewer's high-confidence "different" vetoes a pair, also one outside --models
    # (Claude rejected bare first names that Sol and Terra both merged); on a company pair
    # only an answer given with the company prompt counts.
    vetoed = {key for path in REVIEW.glob('merges_*.json') for key, answer in json.loads(path.read_text()).items()
              if answer.get('verdict') == 'different' and answer.get('confidence') == 'high'
              and (answer.get(COMPANY_PROMPT) or not is_company_pair(key, nodes))}
    for key, answer in agreed('merges', 'verdict', args.models).items():
        if answer.get('verdict') != 'same':
            continue
        if key in vetoed:
            skipped_veto += 1
            continue
        a, b = (node_key(x) for x in key.split('|'))
        if a not in nodes or b not in nodes:
            continue  # merged or gone since the review
        if answer.get('candidate_rule') == 'first-name-only header name' and not first_name_owner(a, b, nodes, neighbours, headers):
            skipped += 1
            continue
        if frozenset((str(a), str(b))) in blocked:
            continue
        parent[root(a)] = root(b)
        reasons.setdefault(a, answer.get('candidate_rule', 'candidate'))
        reasons.setdefault(b, answer.get('candidate_rule', 'candidate'))
    clusters = defaultdict(set)
    for x in list(parent):
        clusters[root(x)].add(x)
    # Canonical: a graph person, else a full name (two words of 2+ letters, initials not
    # counted) over an address over a lone first name, then the spelling with more records:
    # OCR gaps add words ("Ste hanie Remington"), so word count alone would pick the misreading.
    def fullness(x):
        if nodes.get(x, {}).get('entity_type') == 'email':
            return 1.5
        return 2 if len([w for w in str(labels.get(x, '')).split() if len(w.strip('.')) >= 2]) >= 2 else 1
    # Company-layer nodes (build_companies) join the graph after identity resolution; a header
    # name merged into one folds into it there (build_contacts.apply_identity_map).
    rank = lambda x: (isinstance(x, int), str(x).startswith('company_'), fullness(x), sum(neighbours[x].values()) if x in neighbours else 0)
    for members in clusters.values():
        if len(members) < 2 or sum(isinstance(x, int) for x in members) > 1:
            continue  # never fold one graph person into another
        canonical = max(members, key=rank)
        new = sorted((m for m in members if m != canonical and m not in owner), key=str)
        if not new:
            continue
        merged['merges'].append({'canonical': canonical, 'members': new, 'confidence': 'probable',
                                 'reason': f"model review ({', '.join(sorted({reasons.get(m, 'candidate') for m in new}))}); "
                                           'evidence kept in data/private/luna_review/'})
        added += len(new)
    config.write_text(json.dumps(merged, indent=1, ensure_ascii=False) + '\n')
    print(json.dumps({'review_verdicts_changed': changed, 'merges_added': added,
                      'first_name_merges_skipped_not_dominant': skipped, 'merges_vetoed': skipped_veto}))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('mode', choices=('classify', 'merges', 'apply'))
    parser.add_argument('--models', nargs='+', help='apply: answer files that must agree (default: all merges_*.json)')
    parser.add_argument('--model', default='gpt-6-luna', help="sol model, deepseek (opencode-go DeepSeek 4.1), or opencode[-go]/<model> (answers in <kind>_<name after the slash>.json)")
    parser.add_argument('--classify-models', nargs='+', help='apply: classify answer files that must agree (default: all classify_*.json)')
    parser.add_argument('--not-person-by', help='classify: only send names this model did not call a person at high confidence')
    parser.add_argument('--same-by', help='merges: only send pairs this model answered same')
    parser.add_argument('--recheck-unsure', action='store_true', help='merges: ask again about pairs this model answered unsure')
    parser.add_argument('--recheck-companies', action='store_true',
                        help='merges: ask again about company pairs answered before companies were allowed')
    parser.add_argument('--undecided-by', help='merges: only send pairs this model did not answer same/different at high confidence')
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    {'classify': classify, 'merges': merges, 'apply': apply}[args.mode](args)


if __name__ == '__main__':
    main()
