"""Dataset-agnostic clean-up of extracted graph nodes for a viewer slice.

No imports from this project: input and output are plain dicts
({'id', 'name', 'entity_type', ...} nodes and {'source_entity_id',
'target_entity_id', 'relationship_type'} links), so another graph (another
release, another corpus) can reuse it. Dataset knowledge lives in a JSON
vocabulary (config/node_vocabulary.json), never in code.

Numbered labels. Text extraction turns "Jet Blue #65", "Point 15", "Rule 45's",
"Bill 7", "LexisNexis 2007" or "Bell 407" into organizations: a name followed
by a page, flight, model, docket, year or amount. Per name stem:
  * the vocabulary names the stem as a model maker -> one aircraft/vehicle node
    per model ("Bell 407");
  * the stem is an existing organization (exact name without legal form, or a
    vocabulary alias) -> the numbered labels fold into it;
  * the vocabulary gives the stem a canonical name but no node exists -> the
    family collapses into one node with that name ("Air France");
  * a series word ends the stem ("Caterpillar Trust 2", "Fund III") -> kept;
  * three or more different numbers and none of the above -> hidden
    ("numbered_reference"); hiding never deletes.
Nothing here merges two differently named entities.

Text checks shared with the build: text_junk_reason (dates, dated headings,
citations) and sentence_break_domain ("good.by" from "good. By").
"""
from collections import defaultdict
import json
from pathlib import Path
import re

DEFAULT_VOCABULARY = Path(__file__).resolve().parents[1] / 'config/node_vocabulary.json'
LEGAL_FORMS = {'inc', 'incorporated', 'corp', 'corporation', 'co', 'company', 'llc', 'llp', 'lp', 'ltd', 'limited',
               'plc', 'sa', 'ag', 'gmbh', 'nv', 'bv', 'sarl', 'sas', 'srl', 'pa', 'pc', 'pllc', 'the'}
NUMBERED = re.compile(r"^(?P<stem>[^\W\d_](?:[^\d#:]*?[^\W\d_])?)\.?\s*(?:[-–#:,/]\s*|\s+|(?=\d))"
                      r"(?P<number>[+-]?\d[\d.,:/-]*(?:\s+\d[\d.,:/-]*)*)(?:'s)?\s*$")
MIN_FAMILY = 3


AMOUNT = re.compile(r'\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2}')


def load_vocabulary(path=DEFAULT_VOCABULARY):
    data = json.loads(Path(path).read_text()) if path and Path(path).exists() else {}
    words = Path(data.get('word_list', '/usr/share/dict/words'))
    return {'model_makers': {squeeze(k): v for k, v in data.get('model_makers', {}).items()},
            'aliases': {squeeze(k): v for k, v in data.get('aliases', {}).items()},
            'series_words': {w.casefold() for w in data.get('series_words', [])},
            # Ordinary words and common names (any case): "Point", "June", "Laura", "Virginia".
            'words': frozenset(w.casefold() for w in words.read_text().split()) if words.exists() else frozenset()}


def clean_label(value):
    """A target spelling, not OCR debris: no stray symbols or split letters ("Bi l l", "AIR FRANCE &=")."""
    tokens = str(value).split()
    return (bool(re.fullmatch(r"[\w&.,'’() -]+", str(value))) and not re.search(r'[&,(-]\s*$|^\W', str(value))
            and all(len(t) > 1 or t in '&' for t in tokens) and str(value).count('(') == str(value).count(')'))


def squeeze(value):
    return re.sub(r'[^0-9a-z]', '', str(value).casefold())


def base_name(value):
    """Name without legal form or punctuation: 'Delta Air Lines, Inc.' -> 'deltaairlines'."""
    words = re.findall(r'[0-9a-z&]+', str(value).casefold().replace('.', ''))
    while len(words) > 1 and words[-1] in LEGAL_FORMS:
        words.pop()
    while len(words) > 1 and words[0] == 'the':
        words.pop(0)
    return squeeze(' '.join(words))


MONTHS = r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?'
COMMON_ENDINGS = {'com', 'net', 'org', 'gov', 'edu', 'mil', 'int', 'co.uk', 'org.uk', 'com.au', 'us', 'io'}
TWO_LETTER_WORDS = {'by', 'is', 'do', 'so', 'to', 'be', 'my', 'at', 'me', 'no', 'it', 'in', 'am', 'an', 'go', 'he', 'we', 'or', 'of', 'on', 'up'}


def text_junk_reason(value):
    """Labels that are a date, a dated heading or a citation, never an entity."""
    value = str(value).strip()
    # "Sun, Nov 15, 2009", "Feb 18-25": email and calendar date lines.
    if (re.match(r'(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+' + MONTHS + r'(?:\s|$|[\d,=])', value, re.I)
            or re.fullmatch(MONTHS + r'\s+\d{1,2}(?:\s*-\s*\d{1,2})?(?:,?\s+\d{4})?', value, re.I)):
        return 'date_text'
    # "7/2/2020 - Maxwell", "Chicago Tribune (9/18": a dated heading or clipping line.
    if re.match(r'\d{1,2}/\d{1,2}(?:/\d{2,4})?\s+(?:-\s+)?\S', value) or re.search(r'\(\d{1,2}/\d{1,2}(?:/\d{2,4})?\)?$', value):
        return 'dated_heading'
    # Case-reporter citations and preprint ids ("767 N.Y.S.2d 228", "arXiv:1403.3289").
    if re.search(r'\bN\.\s?Y\.\s?S\.\s?\d?[dn]\b|\bF\.\s?(?:Supp\.\s?)?\d[dn]\b|^arXiv:\s?\d', value, re.I):
        return 'citation'
    return None


def lexicon(path='/usr/share/dict/words'):
    """Every lowercase word of a word list, names' homographs included (good, well, point)."""
    if path not in _LEXICONS:
        _LEXICONS[path] = frozenset(w for w in Path(path).read_text().split() if w.islower()) if Path(path).exists() else frozenset()
    return _LEXICONS[path]


_LEXICONS = {}


def sentence_break_domain(domain, words=None):
    """"good. By", "well. Today", "will. Be": a sentence break read as a bare URL.
    An English word followed by an uncommon ending that is itself a word."""
    labels = str(domain).casefold().removeprefix('www.').split('.')
    if len(labels) < 2:
        return False
    two = '.'.join(labels[-2:])
    base, ending = (labels[-3], two) if len(labels) >= 3 and two in COMMON_ENDINGS else (labels[-2], labels[-1])
    words = lexicon() if words is None else words
    return (ending not in COMMON_ENDINGS and base in words
            and (len(ending) >= 3 and ending in words or ending in TWO_LETTER_WORDS))


def numbered(name):
    """(stem, number) of 'Stem 123' / 'Stem #12' / 'Stem 4/430' labels, else None."""
    match = NUMBERED.match(str(name).strip())
    if not match or len(re.sub(r'[^A-Za-z]', '', match['stem'])) < 2:
        return None
    return match['stem'].strip(' .-'), match['number']


def numbered_families(entities, vocabulary, resolutions, types=('organization',)):
    """Returns updated entities; adds {source id: (target id, rule)} to resolutions."""
    entities = [dict(e) for e in entities]
    by_base, all_bases = defaultdict(list), set()
    for e in entities:
        if not e.get('label_quality') and not numbered(e['name']):
            all_bases.add(base_name(e['name']))
        if e['entity_type'] in types and not e.get('label_quality') and not numbered(e['name']) and clean_label(e['name']):
            by_base[base_name(e['name'])].append(e)
    # "Phillips 66 Company" exists: "Phillips 66" is that company, not a page of "Phillips".
    named = {b for b in all_bases if re.search(r'\d', b)}
    pick = lambda found: max(found, key=lambda e: (len(e.get('documents') or ()) if isinstance(e.get('documents'), (list, set)) else
                                                   (e.get('documents') or {}).get('n', 0), str(e['id'])))
    families = defaultdict(list)
    for e in entities:
        if e['entity_type'] in types and not e.get('label_quality') and e['id'] not in resolutions:
            found = numbered(e['name'])
            if found:
                families[squeeze(found[0])].append((e, *found))
    renamed, models = {}, defaultdict(list)
    for key, members in families.items():
        stem = members[0][1]
        last = re.findall(r'[a-z]+', stem.casefold())[-1:]
        maker = vocabulary['model_makers'].get(key)
        alias = vocabulary['aliases'].get(key)
        words = re.findall(r'[a-z0-9&]+', stem.casefold())
        # A single ordinary word or name is never itself the organization ("Card", "June", "Point").
        common = len(words) == 1 and words[0] in vocabulary['words']
        targets = by_base.get(base_name(alias), []) if alias else []
        targets = targets or ([] if common else by_base.get(key, []))
        if maker:
            for e, stem, number in members:
                label = f'{stem} {number}'.strip()
                e.update(name=label[:1].upper() + label[1:], entity_type=maker.get('entity_type', 'vehicle'),
                         original_entity_type=e['entity_type'], label_rule='numbered_model',
                         **{k: v for k, v in maker.items() if k != 'entity_type'})
                models[squeeze(label)].append(e)
        elif targets:
            target = pick(targets)
            for e, *_ in members:
                resolutions[e['id']] = (target['id'], 'numbered_reference_of_organization')
        elif alias:
            target = pick([e for e, *_ in members])
            renamed[target['id']] = alias
            for e, *_ in members:
                if e is not target:
                    resolutions[e['id']] = (target['id'], 'numbered_reference_of_organization')
        elif last and last[0] in vocabulary['series_words']:
            continue
        else:
            family = len({squeeze(number) for _, _, number in members}) >= MIN_FAMILY
            for e, _, number in members:
                if base_name(e['name']) in named:
                    continue
                if family or common or AMOUNT.fullmatch(number.strip()):
                    e.update(label_quality='not_a_name', label_quality_reason='numbered_reference')
    for found in models.values():
        target = pick(found)
        for e in found:
            if e is not target:
                resolutions[e['id']] = (target['id'], 'same_model')
    for e in entities:
        if e['id'] in renamed:
            e.update(original_names=[e['name']], name=renamed[e['id']])
    return entities


def fold_own_websites(entities, relationships, website='website', owners=('organization',)):
    """A website whose only partner is the organization it belongs to
    (website_of) becomes a 'websites' line on it instead of a second dot."""
    nodes = {e['id']: e for e in entities}
    partners, owned = defaultdict(set), defaultdict(set)
    for r in relationships:
        s, t = r['source_entity_id'], r['target_entity_id']
        if s == t or s not in nodes or t not in nodes:
            continue
        partners[s].add(t)
        partners[t].add(s)
        if r['relationship_type'] == 'website_of':
            owned[s].add(t)
    fold = {w: next(iter(found)) for w, found in partners.items()
            if nodes[w]['entity_type'] == website and len(found) == 1 and found == owned[w]
            and nodes[next(iter(found))]['entity_type'] in owners}
    for site, owner in sorted(fold.items(), key=str):
        nodes[owner] = {**nodes[owner], 'websites': [*nodes[owner].get('websites', []), nodes[site]['name']]}
    return ([nodes[e['id']] for e in entities if e['id'] not in fold],
            [r for r in relationships if r['source_entity_id'] not in fold and r['target_entity_id'] not in fold], fold)
