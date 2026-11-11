"""Survivors stay in the viewer as nodes, but never under their name in clear.

Every survivor node with a real name gets a pseudonym and an id that does not
spell the name. The name is only in the encrypted reveal table, so it needs the
viewer password; a public build (VIEWER=public) has no such table at all.
Every other string in the viewer is scrubbed of the same names: merged spellings,
source records, notes, case captions ("Roe v. Maxwell"). Emails and phones
that belong to a survivor or spell a survivor's name leave the plain-text table.
"""
import hashlib
import hmac
import json
import os
import re
from pathlib import Path

from victim_policy import entity_names, victim_ids

ROOT = Path(__file__).resolve().parents[1]
ANONYMOUS = re.compile(r'^(?:jane doe|john doe|minor victim|anonymous victim|headerlabel_|participant_|email_|phone_|[0-9a-f]{20,})', re.I)
WITHHELD = '[name withheld]'


def word_list(path='/usr/share/dict/words'):
    path = Path(path)
    return frozenset(w.casefold() for w in path.read_text().split()) if path.exists() else frozenset()


def configured_names(paths):
    names = []
    for path in paths:
        names += json.loads(Path(path).read_text()).get('names', [])
    return [n for n in names if not ANONYMOUS.match(n)]


def name_terms(names, words):
    """Full names plus surnames too rare to be someone else's (Vellorin, not Ward)."""
    terms = {' '.join(str(n).replace('(', ' ').replace(')', ' ').split()) for n in names}
    for name in list(terms):
        parts = re.findall(r"[^\W\d_][\w'-]+", name)
        if parts and parts[-1].casefold() not in words:
            terms.add(parts[-1])
        if len(parts) >= 2:
            terms.add(f'{parts[-1]}, {" ".join(parts[:-1])}')
    return sorted((t for t in terms if len(t) > 3), key=len, reverse=True)


def scrubber(terms, ids=None):
    """Function replacing the names everywhere in a JSON-like value; ids maps exact old ids to new ones."""
    ids = ids or {}
    pattern = re.compile(r'(?<![\w])(?:' + '|'.join(re.escape(t).replace(r'\ ', r'\s+') for t in terms) + r')(?![\w])', re.I) if terms else None

    def text(value):
        if value in ids:
            return ids[value]
        return pattern.sub(WITHHELD, value) if pattern else value

    def walk(value):
        if isinstance(value, str):
            return text(value)
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, dict):
            return {(text(k) if isinstance(k, str) else k): (ids.get(v, v) if k in ('id', 'source_entity_id', 'target_entity_id') and isinstance(v, str) else walk(v))
                    for k, v in value.items()}
        return value
    return walk


def contact_ids(entity):
    merged = entity.get('merged_identities')
    return {str(m.get('id')) for m in merged if isinstance(m, dict)} if isinstance(merged, list) else set()


def protect(entities, relationships, key, config=ROOT / 'config/victims.json', survivor_config=ROOT / 'config/survivors.json',
            registry=ROOT / 'data/raw/persons_registry.json', words=None, public_record=ROOT / 'config/survivors_public_record.json'):
    """Returns (entities, relationships, {new id: {'value': real name}}, scrub, private contact ids)."""
    words = word_list() if words is None else words
    survivors = victim_ids(entities, relationships, config=config, survivor_config=survivor_config, registry=registry)
    named = [e for e in entities if (e['id'] in survivors or e.get('entity_type') == 'survivor') and not ANONYMOUS.match(str(e['name']))]
    names, ids, reveal, private = configured_names([config, survivor_config]), {}, {}, set()
    for e in named:
        names += [n for n in entity_names(e) if not ANONYMOUS.match(n)]
        digest = hmac.new(key, str(e['id']).encode(), hashlib.sha256).hexdigest()[:8]
        node_id = e['id'] if isinstance(e['id'], int) else f'survivor_{digest}'
        if node_id != e['id']:
            ids[e['id']] = node_id
        reveal[str(node_id)] = {'value': e['name'], 'pseudonym': f'Survivor {digest[:6]}'}
        private |= contact_ids(e)
    # Names from web research, full names only: a surname alone could be anyone (Elizabeth Arden).
    extra = [p['name'] for p in json.loads(Path(public_record).read_text())['people']] if Path(public_record).exists() else []
    scrub = scrubber(sorted({*name_terms(names, words), *extra}, key=len, reverse=True), ids)
    pseudonyms = {info_id: info['pseudonym'] for info_id, info in reveal.items()}
    out = []
    for e in scrub(entities):
        if str(e['id']) in pseudonyms:
            e = {**e, 'name': pseudonyms[str(e['id'])], 'normalized_name': pseudonyms[str(e['id'])].casefold()}
            e.pop('aliases', None), e.pop('original_names', None)
        out.append(e)
    return out, scrub(relationships), {i: {'value': v['value']} for i, v in reveal.items()}, scrub, private


def mailbox_forms(names):
    """Local parts that spell a name: zelda.hargrave, zhargrave, hargrave.zelda..."""
    forms = set()
    for name in names:
        words = re.findall(r'[a-z]+', str(name).casefold())
        if len(words) >= 2:
            first, last = words[0], words[-1]
            forms |= {first + last, f'{first}.{last}', f'{first}_{last}', f'{first}-{last}', first[0] + last, f'{first[0]}.{last}', last + first, f'{last}.{first}'}
    return forms


def survivor_mailbox_forms(paths=(ROOT / 'config/victims.json', ROOT / 'config/survivors.json'), public_record=ROOT / 'config/survivors_public_record.json'):
    extra = [p['name'] for p in json.loads(Path(public_record).read_text())['people']] if Path(public_record).exists() else []
    return mailbox_forms([*configured_names(paths), *extra])


def public_contacts(contacts, scrub, private, forms=()):
    """Plain-text contacts minus survivors' own and any spelling a survivor's name (they stay behind the password)."""
    return {i: v for i, v in contacts.items() if i not in private and scrub(v) == v and v.split('@')[0].casefold() not in forms}


def load_key(path=ROOT / 'data/private/contact_hmac.key'):
    path = Path(path)
    return path.read_bytes() if path.exists() else os.urandom(32)
