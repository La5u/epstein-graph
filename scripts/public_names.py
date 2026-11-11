"""Which real names the public viewer shows: public figures only.

data/private/public_figures.json holds the reviewed verdicts ({node id: {public,
qid, confidence, why}}; scripts/public_figure_candidates.py builds the review
input from Wikidata people and the node's own context). In the viewer:
  * a pseudonymous node (email participant, extracted name) whose verdict is
    public with high or medium confidence is named in clear;
  * a node shown under its real name without such a verdict, or the upstream
    public_figure flag, gets a pseudonym; its name moves to the encrypted table,
    so the password view still shows every name;
  * a one-word name that is the surname of a named public figure stays (the
    upstream "Dubin" is Glenn or Eva Dubin's record); other one-word names get a
    pseudonym but are not scrubbed from text (a word like "Black" is everywhere).
Full names of pseudonymized people are scrubbed from every other string.
"""
import hashlib
import hmac
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERDICTS = ROOT / 'data/private/public_figures.json'
PSEUDONYM = re.compile(r'^(?:[A-Za-z]+_)?[0-9a-f]{20,}$|^(?:HeaderLabel|Email|Phone|Participant|Person|Survivor)[_ ][0-9a-f]+$', re.I)
PEOPLE = ('person', 'participant')


def load_verdicts(path=VERDICTS):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else None


def is_public(verdict):
    return bool(verdict and verdict.get('public') and verdict.get('confidence') in ('high', 'medium'))


def name_key(name):
    return ' '.join(re.findall(r'[a-z]+', re.sub(r'\[[^\]]*\]', ' ', str(name).casefold())))


def public_name_keys(verdicts):
    """Names of verified public figures: every node spelled exactly so is that person's
    record too (OCR copies), so one cannot be named while another hides behind a pseudonym."""
    return {name_key(v['name']) for v in (verdicts or {}).values() if is_public(v) and v.get('name') and len(name_key(v['name']).split()) > 1}


NOT_A_PERSON = re.compile(r'not a (?:person|name|human)|organi[sz]ation|^(?:company|bank|airline|law firm|place|airport)\b(?! (?:staff|employee|contact))|place name|mailbox|sign-off|greeting|boilerplate|form field|job title|label', re.I)


def private_name_terms(verdicts, kept):
    """Full names of reviewed people who stay private, for scrubbing other strings ("subpoena to X",
    "X I Some Firm LLP"). Not labels the review found are no person, nor part of a name still shown."""
    keys = public_name_keys(verdicts) | {name_key(n) for n in kept}
    shown = [f' {k} ' for k in keys]
    terms = set()
    for v in (verdicts or {}).values():
        key = name_key(v.get('name') or '')
        if (not is_public(v) and len(key.split()) > 1 and key not in keys
                and not NOT_A_PERSON.search(f"{v.get('role') or ''} | {v.get('why') or ''}")
                and not any(f' {key} ' in k for k in shown)):
            terms.add(v['name'])
    return terms


def withheld(verdict):
    """Owner decision: password view only, even with the upstream public_figure flag."""
    return bool(verdict and verdict.get('withhold'))


def flagged_public(entity):
    metadata = entity.get('metadata')
    try:
        metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
    except ValueError:
        return False
    return isinstance(metadata, dict) and metadata.get('public_figure') is True


def apply(entities, verdicts, key, scrubber):
    """Pseudonyms for real-named people who are not public figures.
    Returns (entities, {id: {'value': name}} for the password view, scrub)."""
    if verdicts is None:  # no review yet: leave the viewer as it was
        return entities, {}, lambda value: value
    hidden, full_names = {}, []
    named = [e for e in entities if e.get('entity_type') in PEOPLE and not PSEUDONYM.match(str(e['name']))]
    public_surnames = {e['name'].split()[-1].casefold() for e in named
                       if len(e['name'].split()) > 1 and not withheld(verdicts.get(str(e['id'])))
                       and (is_public(verdicts.get(str(e['id']))) or flagged_public(e))}
    keys = public_name_keys(verdicts)
    out = []
    for e in entities:
        verdict = verdicts.get(str(e['id']))
        if name_key(e['name']) in keys:
            verdict = {'public': True, 'confidence': 'high'}
        if e.get('entity_type') not in PEOPLE:
            out.append(e)
        elif PSEUDONYM.match(str(e['name'])):
            out.append(e)
        elif not withheld(verdict) and (is_public(verdict) or flagged_public(e)) or (len(e['name'].split()) == 1 and e['name'].casefold() in public_surnames):
            out.append(e)
        else:
            digest = hmac.new(key, str(e['id']).encode(), hashlib.sha256).hexdigest()[:6]
            hidden[str(e['id'])] = {'value': e['name']}
            if len(e['name'].split()) > 1:
                full_names.append(e['name'])
                if withheld(verdict):  # rare surnames are cut too ("Ms. X" in a summary)
                    full_names.append(e['name'].split()[-1])
                full_names += [n for n in e.get('original_names') or [] if isinstance(n, str) and len(n.split()) > 1]
            slim = {**e, 'name': f'Person {digest}', 'normalized_name': f'person {digest}'}
            for field in ('aliases', 'original_names'):
                slim.pop(field, None)
            out.append(slim)
    keep = {e['name'].casefold() for e in named if str(e['id']) not in hidden}
    terms = sorted({n for n in full_names if n.casefold() not in keep} | private_name_terms(verdicts, keep), key=len, reverse=True)
    scrub = scrubber(terms) if terms else (lambda value: value)
    cleaned = scrub(out)
    for before, after in zip(out, cleaned):  # the password view keeps the original of any label a name was cut from
        if before['name'] != after['name'] and str(before['id']) not in hidden:
            hidden[str(before['id'])] = {'value': before['name']}
    return cleaned, hidden, scrub


def shown_names(entities, labels, verdicts):
    """{id: real name} of pseudonymous people verified as public figures (shown without the password)."""
    if not verdicts:
        return {}
    keys = public_name_keys(verdicts)
    return {str(e['id']): labels[str(e['id'])] for e in entities
            if e.get('entity_type') in PEOPLE and PSEUDONYM.match(str(e['name'])) and str(e['id']) in labels
            and (is_public(verdicts.get(str(e['id']))) or name_key(labels[str(e['id'])]) in keys)}
