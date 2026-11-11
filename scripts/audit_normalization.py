"""Quantify conservative contact normalization and unresolved variants.

Only pseudonymous candidate IDs and aggregate counts are exported. Candidates
are NOT automatically merged: casing of email local parts and phone country
context can be significant. No raw contacts or reverse maps are written.
"""
import json
from collections import Counter, defaultdict
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from build_contacts import contact_id, contact_key, normalize_contact
import emails as email_source
from build_communications import parse_recipient, parse_sender, valid_email

ROOT = Path(__file__).resolve().parents[1]


def summarize(emails, phones, key):
    groups = {'email_local_case': defaultdict(set), 'email_idna_domain': defaultdict(set),
              'phone_plus_prefix': defaultdict(set), 'phone_us_prefix': defaultdict(set)}
    for email in emails:
        normalized = normalize_contact(email, 'email')
        if normalized is None:
            continue
        identity = contact_id(normalized, 'email', key)
        local, domain = normalized.rsplit('@', 1)
        groups['email_local_case'][normalized.casefold()].add(identity)
        try:
            ascii_domain = domain.encode('idna').decode('ascii').casefold()
            groups['email_idna_domain'][(local, ascii_domain)].add(identity)
        except UnicodeError:
            pass
    for phone in phones:
        value = normalize_contact(phone, 'phone')
        if value is None:
            continue
        identity = contact_id(value, 'phone', key)
        digits = value.lstrip('+')
        groups['phone_plus_prefix'][digits].add(identity)
        if len(digits) == 10:
            groups['phone_us_prefix'][digits].add(identity)
        elif len(digits) == 11 and digits.startswith('1'):
            groups['phone_us_prefix'][digits[1:]].add(identity)
    candidates = {}
    for kind, entries in groups.items():
        candidates[kind] = sorted([sorted(ids) for ids in entries.values() if len(ids) > 1])
    return candidates


def collect_contacts(external=None):
    """Raw email/phone strings from every source, with occurrence counts."""
    @lru_cache(maxsize=131072)
    def string_recipients(value):
        return tuple(parse_recipient(value))

    raw = ROOT / 'data/raw'
    export = json.loads((raw / 'extracted_entities_filtered.json').read_text())
    emails, phones = Counter(), Counter()
    for row in export['emails']:
        emails[row['entity_value']] += 1
    for row in export['phones']:
        phones[row['entity_value']] += 1
    with closing(email_source.headers()) as db:
        # Parse each distinct header once, retaining its full occurrence weight.
        for name, sender, count in db.execute('SELECT sender_name,sender_email,count(*) FROM headers WHERE noise=0 GROUP BY sender_name,sender_email'):
            if (value := valid_email(parse_sender(name, sender)[1])):
                emails[value] += count
        recipient_counts = Counter()
        for column in ('to_json', 'cc_json'):
            recipient_counts.update(dict(db.execute(f'SELECT {column},count(*) FROM headers WHERE noise=0 GROUP BY {column}')))
        for raw_value, count in recipient_counts.items():
            try:
                items = json.loads(raw_value or '[]')
            except (ValueError, TypeError):
                continue
            if not isinstance(items, list):
                continue
            for item in items:
                parsed = string_recipients(item) if isinstance(item, str) else parse_recipient(item)
                for _, address in parsed:
                    if address:
                        emails[address] += count
    with closing(email_source.parties()) as db:
        for raw_email, count in db.execute('SELECT address, count(*) FROM parties GROUP BY address'):
            if (value := valid_email(raw_email)):
                emails[value] += count
    for *_, sender, to_list, cc_list, _ in email_source.external() if external is None else external:
        for value in [sender, *to_list, *cc_list]:
            parsed = string_recipients(value) if isinstance(value, str) else parse_recipient(value)
            for _, address in parsed:
                if address:
                    emails[address] += 1
    return export, emails, phones


def main():
    out = ROOT / 'output'
    key = contact_key()
    export, emails, phones = collect_contacts()
    candidates = summarize(emails, phones, key)
    result = {
        'normalization_policy': {
            'names': 'Unicode NFC + casefold + whitespace collapse within entity type. Exact labels are not verified identities; surname/middle-name/alias variants remain separate.',
            'emails': 'Trim outer whitespace, casefold domains, preserve local-part case. No plus-tag removal or provider-specific dot rules. Not complete mailbox identity resolution.',
            'phones': 'Strip formatting, keep explicit plus, accept 7–15 ASCII digits. No guessed region/country code; no extension guessing. Not full E.164 identity resolution.',
        },
        'email_inventory_normalized_unique': len({normalize_contact(e, 'email') for e in emails if normalize_contact(e, 'email')}),
        'phone_export_rows': len(export['phones']),
        'phone_normalized_unique': len({normalize_contact(p, 'phone') for p in phones if normalize_contact(p, 'phone')}),
        'candidate_group_counts': {kind: len(groups) for kind, groups in candidates.items()},
        'candidate_groups_pseudonymous': candidates,
        'warning': 'Review candidates, do not automatically merge. US-prefix candidates assume a region that has NOT been established; local-part case variants are not proven equivalent. Groups can overlap. Pseudonyms remain sensitive.',
    }
    (out / 'normalization_audit.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in ('candidate_groups_pseudonymous',)}, indent=2))


if __name__ == '__main__':
    main()
