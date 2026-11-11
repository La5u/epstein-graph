"""Report potential node-quality problems in the built viewer; never reject a build.

Reads only the compact gzip/base64 viewer payload, not either combined graph.
Public reports contain counts and opaque references. Decrypted samples are written
only to data/private/audit/node_audit.json, with mode 0600.
"""
import argparse
import base64
import gzip
import hashlib
import json
import mmap
import os
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from place_normalization import place_label_reason

ROOT = Path(__file__).resolve().parents[1]
PERSON_TYPES = {'person', 'participant', 'survivor'}
CONTACT_TYPES = {'contact', 'email', 'phone'}
PLACE_TYPES = {'place', 'property', 'location', 'airport'}
HIDDEN_QUALITY = {'not_a_name', 'drop', 'boilerplate', 'ocr_garbage', 'redaction_or_placeholder'}
SECTIONS = (
    'same_word_set_names', 'middle_name_initial_subsets', 'one_ocr_edit_same_first_name',
    'space_insensitive_duplicates', 'cross_type_exact_duplicates', 'ocr_skeleton_duplicates',
    'isolated_nodes_by_type', 'long_labels_by_type', 'file_names_as_entities',
    'email_addresses_non_contact', 'mangled_emails_in_person_labels', 'invalid_websites',
    'message_content_id_contacts', 'leading_noise', 'lowercase_punct_person_labels', 'phone_duplicates',
    'place_label_quality',
)
HONORIFICS = {'mr', 'mrs', 'ms', 'dr', 'jr', 'sr', 'esq'}
FILE = re.compile(r'\.(?:jpe?g|png|gif|bmp|tiff?|pdf|docx?|xlsx?|pptx?|html?|em[fl]|wmz|emz)(?:\b|$)', re.I)
EMAIL = re.compile(r'[\w.!%+\-]+@[a-z0-9.-]+\.[a-z]{2,}', re.I)
NOISE = re.compile(r'^(?:esq\.?|mr\.?|mrs\.?|ms\.?|to|from|see|yes\.)\s*[:;,\-]?\s+', re.I)
SIGNATURE = re.compile(r'\s*\[signature:\s*[^\]]*\]', re.I)
DOMAIN_WORDS = set('the this your with value summary total beginning ending presented income safety on manage managel by information office company name home page file image content type text application'.split())
MAIL_BASES = {'gmail', 'googlemail', 'yahoo', 'hotmail', 'outlook', 'aol', 'icloud'}


def words(value):
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().casefold()
    return re.findall(r'[a-z0-9]+', value)


def name_words(value):
    return tuple(w for w in words(value) if w not in HONORIFICS)


def compact(value):
    return ''.join(words(value))


def skeleton(value):
    value = compact(value)
    for old, new in (('rn', 'm'), ('vv', 'w'), ('cl', 'd'), ('l', 'i'), ('1', 'i'), ('0', 'o'), ('5', 's'), ('8', 'b'), ('6', 'g')):
        value = value.replace(old, new)
    return value


def one_edit(a, b):
    """Exactly one insertion, deletion or substitution, in linear time."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = edits = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if len(a) == len(b):
            i += 1
        j += 1
    return edits + (j < len(b)) == 1


def middle_subset(a, b):
    """Same endpoints; inner tokens may be omitted or expanded from initials."""
    if a == b or len(a) < 2 or len(b) < 2 or (a[0], a[-1]) != (b[0], b[-1]):
        return False
    a, b = a[1:-1], b[1:-1]
    if len(a) > len(b):
        a, b = b, a
    pos = 0
    for token in a:
        while pos < len(b) and not (token == b[pos] or min(len(token), len(b[pos])) == 1 and token[0] == b[pos][0]):
            pos += 1
        if pos == len(b):
            return False
        pos += 1
    return True


def common_domain_words():
    path = Path('/usr/share/dict/words')
    if not path.is_file():
        return set()
    with path.open() as stream:
        words = {line.strip() for line in stream}
    proper = {word.casefold() for word in words if word[:1].isupper()}
    return {word for word in words if word.islower()} - proper


def website_reason(value, dictionary=()):
    """Review leads, not a claim that a domain is unregistered."""
    raw = value.strip().casefold()
    try:
        parsed = urlsplit(raw if '://' in raw else '//' + raw)
        host = (parsed.hostname or '').removeprefix('www.')
    except ValueError:
        return 'malformed_domain'
    if not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}', host) or '@' in raw:
        return 'malformed_domain'
    labels = host.split('.')
    base = labels[-2]
    if host in {'drive.goo', 'drive.goog', 'drive.google'} or labels[-1] in {'co', 'cm', 'con', 'corn', 'c0m', 'om'}:
        return 'fragment_or_ocr_suffix'
    if len(base) < 3 or base in DOMAIN_WORDS or base in dictionary and not re.match(r'^(?:https?://|www\.)', raw):
        return 'fragment_or_common_word_domain'
    if base not in MAIL_BASES and (base in {'innail', 'qmail', 'umail', 'thsmail', 'gmad'} or any(one_edit(base.replace('rn', 'm'), p) for p in MAIL_BASES if len(p) >= 5)):
        return 'mail_provider_lookalike'
    return None


def message_id(value):
    local, sep, domain = value.casefold().strip('<>').partition('@')
    if not sep:
        return False
    return bool(re.search(r'\.(?:prod|outlook|mail|local|internal)\b', domain) and re.search(r'\d{5,}', local)
                or re.fullmatch(r'(?:[a-f0-9]{16,}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})', local)
                or re.match(r'(?:image\d*|att\d*|part\d*|content-id|message-id|ii_|f_)[._-]', local)
                or re.fullmatch(r'[a-f0-9.\-]{20,}', local))


def _json_block(html, pattern, default=None):
    match = re.search(pattern, html, re.S)
    return json.loads(match.group(1)) if match else default


def read_viewer(path, password_path):
    """Compact viewer nodes plus degrees. No lazy detail chunks are inflated."""
    with Path(path).open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as html:
        match = re.search(rb'<script\b[^>]*\bid=["\']viewer-data["\'][^>]*>(.*?)</script>', html, re.S)
        if not match:
            raise ValueError('viewer-data payload missing')
        payload = json.loads(gzip.decompress(base64.b64decode(match.group(1))))
        degree = Counter()
        for edge in payload.get('edges', []):
            degree[edge['from']] += 1
            degree[edge['to']] += 1
        nodes = []
        detail_nodes = {}
        for node in payload['nodes']:
            record = node.get('record', {})
            nodes.append({'id': node['id'], 'label': node['label'], 'real': None,
                          'type': record.get('entity_type', 'unknown'),
                          'sub': record.get('subtype') or record.get('place_type') or record.get('category'),
                          'deg': degree[node['id']], 'rec': record})
            if 'detail' in node:
                detail_nodes[node['detail']] = nodes[-1]
        # Decode one node-detail chunk at a time. Edges and document tables are
        # discarded immediately; merged members are needed to track changed IDs.
        chunk_size = payload.get('detailChunkSize', 256)
        for match_detail in re.finditer(rb'<script\b[^>]*\bid=["\']detail-(\d+)["\'][^>]*>(.*?)</script>', html, re.S):
            start = int(match_detail[1]) * chunk_size
            if start > max(detail_nodes, default=-1):
                break
            records = json.loads(gzip.decompress(base64.b64decode(match_detail[2])))
            for offset, record in enumerate(records):
                node = detail_nodes.get(start + offset)
                if node is not None:
                    node['rec'] = {k: record[k] for k in (
                        'label_quality', 'suspected_parse_artifact', 'relevance',
                        'merged_identities', 'merged_address_aliases', 'original_entity_ids',
                        'original_names', 'aliases', 'place_quality_reason',
                    ) if k in record} | node['rec']
            del records
        del payload, degree
        public = _json_block(html, rb'const PUBLIC_REVEAL\s*=\s*(\{.*?\});', {})
        secret = _json_block(html, rb'const SECRET\s*=\s*(\{.*?\}|null);')
    reveal, decrypted = {}, False
    warnings = []
    if secret and Path(password_path).is_file():
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
            password = Path(password_path).read_text().strip()
            key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=base64.b64decode(secret['salt']), iterations=secret['iterations']).derive(password.encode())
            plain = AESGCM(key).decrypt(base64.b64decode(secret['iv']), base64.b64decode(secret['data']), None)
            if secret.get('compression') == 'gzip':
                plain = gzip.decompress(plain)
            reveal = json.loads(plain).get('reveal', {})
            decrypted = True
        except Exception:
            # Exception messages can contain private input. Keep the warning fixed.
            warnings.append('viewer reveal could not be decrypted; pseudonymous labels audited')
    for node in nodes:
        value = reveal.get(str(node['id']), public.get(str(node['id'])))
        node['real'] = value.get('value') if isinstance(value, dict) else value
        if isinstance(node['real'], str) and re.fullmatch(r'(?:email|phone):[0-9a-f]{12}', node['real']):
            node['real'] = None  # a public HMAC label is not a decrypted identity
        # These labels never leave memory except in the private report.
        for member in node['rec'].get('merged_identities', []):
            value = reveal.get(str(member.get('id')), public.get(str(member.get('id'))))
            if value:
                member['value'] = value.get('value') if isinstance(value, dict) else value
    return nodes, decrypted, warnings


def audit(nodes, dictionary=()):
    """Return bounded examples as node indexes, so privacy is decided at export."""
    nodes.sort(key=lambda n: str(n['id']))
    pair_sections = {'middle_name_initial_subsets', 'one_ocr_edit_same_first_name'}
    group_sections = {'same_word_set_names', 'space_insensitive_duplicates', 'cross_type_exact_duplicates', 'ocr_skeleton_duplicates', 'phone_duplicates'}
    sections = {key: {'count': 0, 'unit': 'pairs' if key in pair_sections else 'groups' if key in group_sections else 'nodes',
                      'examples': []} for key in SECTIONS}

    def add(section, indexes, reason=None, by_type=False):
        result = sections[section]
        result['count'] += 1
        if by_type:
            counts = result.setdefault('by_type', {})
            kind = nodes[indexes[0]]['type']
            counts[kind] = counts.get(kind, 0) + 1
        if len(result['examples']) < 20:
            result['examples'].append({'nodes': indexes, **({'reason': reason} if reason else {})})

    groups = {k: defaultdict(list) for k in ('wordset', 'space', 'exact', 'skeleton', 'phone')}
    middles, firsts = defaultdict(list), defaultdict(dict)
    for i, node in enumerate(nodes):
        label = (node.get('real') or node['label']).strip()
        clean = SIGNATURE.sub('', label).strip()
        kind = node['type']
        person = kind in PERSON_TYPES
        tokens = name_words(clean)
        if kind in PLACE_TYPES:
            reason = place_label_reason(clean)
            if reason:
                add('place_label_quality', [i], reason)
        if person and len(tokens) >= 2 and len(tokens) <= 8:
            groups['wordset'][tuple(sorted(set(tokens)))].append(i)
            middles[tokens[0], tokens[-1]].append((i, tokens))
            tail = ''.join(tokens[1:])
            if 3 <= len(tail) <= 160:
                firsts[tokens[0]].setdefault(tail, []).append(i)
        groups['exact'][' '.join(words(clean))].append(i)
        if kind not in CONTACT_TYPES:
            key = compact(clean)
            if len(key) >= 4:
                groups['space'][key].append(i)
                groups['skeleton'][skeleton(clean)].append(i)
        if node.get('deg', 0) == 0:
            add('isolated_nodes_by_type', [i], by_type=True)
        is_address = node.get('sub') == 'address' or kind in {'place', 'property', 'location'} and bool(re.match(r'^\d+\s', clean))
        if len(clean.split()) > 5 and not is_address:
            add('long_labels_by_type', [i], by_type=True)
        if FILE.search(clean) and kind not in CONTACT_TYPES:
            add('file_names_as_entities', [i])
        if EMAIL.search(clean) and kind not in CONTACT_TYPES:
            add('email_addresses_non_contact', [i])
        if person and (re.search(r'[@®]|www\.|\.(?:com|net|org)\b|\bmailto:', clean, re.I)
                       or re.search(r'[a-z0-9][eg](?:gmail|yahoo|hotmail|aol)\b', clean, re.I)):
            add('mangled_emails_in_person_labels', [i])
        if kind == 'website':
            reason = website_reason(clean, dictionary)
            if reason:
                add('invalid_websites', [i], reason)
        if kind in CONTACT_TYPES and message_id(clean):
            add('message_content_id_contacts', [i])
        if NOISE.match(clean):
            add('leading_noise', [i])
        if person and clean and (not clean[0].isalpha() or clean[0].islower()):
            add('lowercase_punct_person_labels', [i])
        if kind in CONTACT_TYPES and '@' not in clean and not re.match(r'^(?:Email|Phone)_', clean, re.I):
            digits = re.sub(r'\D', '', clean)
            if 7 <= len(digits) <= 15:
                groups['phone'][digits[-10:]].append(i)

    for key, section in (('wordset', 'same_word_set_names'), ('space', 'space_insensitive_duplicates'), ('exact', 'cross_type_exact_duplicates'), ('skeleton', 'ocr_skeleton_duplicates'), ('phone', 'phone_duplicates')):
        for indexes in groups[key].values():
            if len(indexes) < 2:
                continue
            if key == 'exact' and len({nodes[i]['type'] for i in indexes}) < 2:
                continue
            add(section, indexes)
    for group in middles.values():
        for pos, (i, a) in enumerate(group):
            for j, b in group[pos + 1:]:
                if middle_subset(a, b):
                    add('middle_name_initial_subsets', [i, j])
    # Deletion signatures find edit-distance-one candidates without an all-pairs scan.
    for tails in firsts.values():
        deleted = defaultdict(set)
        for tail in sorted(tails, key=lambda t: (len(t), t)):
            candidates = set(deleted.get(tail, ()))
            for pos in range(len(tail)):
                variant = tail[:pos] + tail[pos + 1:]
                if variant in tails:
                    candidates.add(variant)
                candidates.update(deleted.get(variant, ()))
            for other in sorted(candidates):
                if one_edit(tail, other):
                    for i in tails[tail]:
                        for j in tails[other]:
                            add('one_ocr_edit_same_first_name', [i, j])
            for pos in range(len(tail)):
                deleted[tail[:pos] + tail[pos + 1:]].add(tail)
    return sections


def report(nodes, sections, private=False):
    def example_node(index):
        node = nodes[index]
        opaque = 'Node_' + hashlib.sha256(str(node['id']).encode()).hexdigest()[:16]
        return {'id': node['id'] if private else opaque,
                'label': (node.get('real') or node['label']) if private else opaque, 'type': node['type']}
    return {key: {**value, 'examples': [{**item, 'nodes': [example_node(i) for i in item['nodes'][:20]]} for item in value['examples']]}
            for key, value in sections.items()}


def identity_key(value):
    return str(value).removeprefix('kg:')


def label_key(value):
    # Keep spaces: space-insensitive matching can confuse two different names.
    return ' '.join(words(SIGNATURE.sub('', value or '')))


PLACE_CLASS = r'place|airport|zip|address|street|location|state_zip|po_box'


def place_kind(finding):
    """Sub-bucket for place classes of the evaluation set; None for other classes."""
    value = str(finding.get('class', '')).casefold()
    if not re.search(PLACE_CLASS, value):
        return None
    if re.search(r'zip', value):
        return 'zip_code'
    if re.search(r'dup|variant|typo', value):
        return 'duplicate'
    if re.search(r'bad_label|address_as|address_text|street_address|ocr_error_address|po_box|mailing', value):
        return 'address_label'
    if re.search(r'junk|title_as|placeholder|not_a|surname|first_name', value):
        return 'not_a_place'
    return 'wrong_type'


def finding_theme(finding):
    value = str(finding.get('class', '')).casefold()
    if place_kind(finding):
        return 'places'
    if re.search(r'dup|variant|same|word_order|ocr_family', value):
        return 'duplicates'
    if re.search(r'email|website|domain|url|contact|message_id', value):
        return 'email_website'
    if re.search(r'wrong_type|typed|as_person|as_org|as_participant', value):
        return 'wrong_type'
    if re.search(r'prefix|suffix|leading|trailing|noise|title|honorific', value):
        return 'prefix_suffix_noise'
    if re.search(r'glue|compound|combined|split', value):
        return 'glued_compound'
    if re.search(r'generic|boilerplate|stub|fund_term|trust_term', value):
        return 'boilerplate_stub'
    return 'junk_fragments'


def requested_actions(finding):
    """Extract only explicit actions. Free-form reviewer prose is not executable."""
    fix = str(finding.get('fix', '')).casefold()
    actions = set()
    if re.search(r'\bmerge\b|\bdedupe\b|\bdedup\b', fix):
        actions.add('merge')
    if re.search(r'\bhide\b|\bremove\b|\bdrop\b', fix):
        actions.add('hide')
    if re.search(r'\bretype\b|->\s*(?:place|organization|person|contact|website)', fix):
        actions.add('retype')
    if re.search(r'\bstrip\b|\brelabel\b|\bnormalize\b|\bcorrect\b|\breorder\b', fix):
        actions.add('label')
    return actions


def evaluation(nodes, findings, baseline=(), identity_map=None):
    """Score findings using observable evidence, with explicit unknown outcomes.

    An absent viewer node alone is never proof of a fix: export thresholds can
    change. ID/member matches precede unique label/member-label matches. Mixed
    instructions and arbitrary relabels remain unverified rather than inflating
    recall. Low-confidence findings are reported separately from high confidence.
    This function never changes the graph, including survivor nodes.
    """
    by_id, by_label = defaultdict(set), defaultdict(set)
    current = {identity_key(n['id']): n for n in nodes}
    old = {identity_key(n['id']): n for n in baseline}
    for node in nodes:
        owner = identity_key(node['id'])
        by_id[owner].add(owner)
        rec = node.get('rec') or {}
        members = [*rec.get('merged_identities', []), *rec.get('merged_address_aliases', [])]
        for member in members:
            if 'id' in member:
                by_id[identity_key(member['id'])].add(owner)
            for field in ('value', 'name', 'label'):
                if member.get(field):
                    by_label[label_key(member[field])].add(owner)
        for member in rec.get('original_entity_ids', []):
            by_id[identity_key(member)].add(owner)
        labels = [node.get('real') or node['label']]
        for field in ('original_names', 'aliases'):
            values = rec.get(field) or []
            if isinstance(values, str):
                try:
                    values = json.loads(values)
                except ValueError:
                    values = [values]
                if isinstance(values, str):
                    values = [values]
            labels.extend(v for v in values if isinstance(v, str))
        for value in labels:
            key = label_key(value)
            if key:
                by_label[key].add(owner)
    # The viewer caps merged-member details. The small communication identity map
    # supplies all members without loading either combined graph or contact lookup.
    identity_map = identity_map or {}
    for member, item in identity_map.items():
        target = identity_key(item['canonical'])
        seen = set()
        while target not in current and target in identity_map and target not in seen:
            seen.add(target)
            target = identity_key(identity_map[target]['canonical'])
        if target in current:
            by_id[identity_key(member)].add(target)

    results = []
    buckets = {'by_theme': {}, 'by_class': {}, 'by_confidence': {}, 'by_place_kind': {}}
    for index, finding in enumerate(findings):
        ids = list(dict.fromkeys(identity_key(i) for i in finding.get('good', finding.get('ids', []))))
        labels = finding.get('labels') or []
        aligned = len(labels) == len(finding.get('ids', []))
        label_of = {identity_key(i): label for i, label in zip(finding.get('ids', []), labels)} if aligned else {}
        actions = requested_actions(finding)
        duplicate = 'merge' in actions or finding_theme(finding) == 'duplicates'
        matched, observations = {}, []
        for member in ids:
            previous = old.get(member, {})
            label = label_of.get(member) or previous.get('real') or previous.get('label')
            matches = by_id.get(member, set())
            method = 'id_or_member'
            if not matches and label:
                matches = by_label.get(label_key(label), set())
                method = 'label_or_member_label'
            if len(matches) == 1:
                matched[member] = next(iter(matches))
            observations.append({'id': member, 'old_label': label, 'matches': sorted(matches),
                                 'match_method': method, 'old_type': previous.get('type')})
        targets = set(matched.values())
        merged_group = duplicate and len(ids) > 1 and len(matched) == len(ids) and len(targets) == 1
        fix = str(finding.get('fix', ''))
        # A target explicitly named after "into"/"to" is useful for singleton
        # duplicate findings; do not guess a target from arbitrary prose.
        target_match = re.search(r'\b(?:merge(?:\s+.+?)?\s+into|relabel(?:\s+.+?)?\s+to)\s+[\'"]?(.+?)(?:[\'"]?(?:;|\n)|$)', fix, re.I)
        target_label = label_key(target_match[1].strip(" '\".")) if target_match else None
        for observation in observations:
            member = observation['id']
            owner = matched.get(member)
            if owner is None:
                observation['status'] = 'unverifiable'
                continue
            node = current[owner]
            rec = node.get('rec') or {}
            label = node.get('real') or node['label']
            hidden = bool(rec.get('suspected_parse_artifact') or rec.get('label_quality') in HIDDEN_QUALITY)
            changed = label.strip() != observation['old_label'].strip() if observation['old_label'] else False
            observation.update(current_id=node['id'], current_label=label, current_type=node['type'])
            proof = None
            if duplicate and (merged_group or owner != member and member in by_id):
                proof = 'merged'
            elif duplicate and target_label and label_key(label) == target_label and changed:
                proof = 'merged_or_canonical_label'
            # Survivor changes other than duplicate consolidation are never fixes.
            protected = node['type'] == 'survivor' or observation['old_type'] == 'survivor'
            if not proof and not protected:
                if hidden and 'hide' in actions:  # a hidden duplicate is still unmerged
                    proof = 'hidden'
                elif 'retype' in actions:
                    type_aliases = {'place': PLACE_TYPES, 'location': PLACE_TYPES,
                                    'organization': {'organization', 'shell_company'},
                                    'contact': CONTACT_TYPES, 'email': CONTACT_TYPES,
                                    'website': {'website'}, 'person': {'person'}}
                    requested = re.search(r'\b(?:retype(?:\s+\w+)?\s+(?:to|as)|->)\s*(place|location|organization|contact|email|website|person)\b', fix, re.I)
                    if requested and node['type'] in type_aliases[requested[1].casefold()]:
                        if not observation['old_type'] or observation['old_type'] not in type_aliases[requested[1].casefold()]:
                            proof = 'retyped'
                if not proof and changed and 'label' in actions:
                    before, after = label_key(observation['old_label']), label_key(label)
                    if target_label == after or after and (before.endswith(' ' + after) or before.startswith(after + ' ')
                            or before == after and re.search(r'normalize|case|punct|signature|annotation', fix, re.I)):
                        proof = 'label_stripped'
            observation['status'] = 'resolved' if proof else 'changed_unverified' if changed else 'untouched'
            if proof:
                observation['evidence'] = proof
            if protected and not proof and (hidden or changed):
                observation['protected_survivor'] = True
        # A mixed instruction can assign different fixes to different labels.
        # Preserve the observations, but don't certify an arbitrary branch as
        # the requested fix for every node in a grouped finding.
        mixed = len(actions) > 1 and bool(re.search(r';|\b(?:except|others|but)\b|\([^)]*\b(?:hide|merge|strip|retype)\b', fix, re.I))
        if mixed:
            for observation in observations:
                if observation['status'] == 'resolved':
                    observation['status'] = 'changed_unverified'
                    observation['needs_fix_scope_review'] = True
        counts = Counter(item['status'] for item in observations)
        if not ids or not actions:
            outcome = 'review_only'
        elif counts['resolved'] == len(ids):
            outcome = 'resolved'
        elif counts['resolved']:
            outcome = 'partial'
        elif counts['unverifiable'] == len(ids):
            outcome = 'unverifiable'
        else:
            outcome = 'unresolved'
        result = {'finding': index, 'class': finding.get('class', 'unknown'),
                  'theme': finding_theme(finding), 'confidence': finding.get('confidence', 'unknown'),
                  'status': outcome, 'actions': sorted(actions), 'nodes': observations,
                  'fix': fix, 'labels': labels}
        results.append(result)
        groups = [('by_theme', result['theme']), ('by_class', result['class']), ('by_confidence', result['confidence'])]
        if place_kind(finding):
            groups.append(('by_place_kind', place_kind(finding)))
        for group, key in groups:
            bucket = buckets[group].setdefault(key, {'findings': Counter(), 'nodes': defaultdict(set)})
            bucket['findings'][outcome] += 1
            for observation in observations:
                bucket['nodes'][observation['status']].add(observation['id'])
    for group in buckets.values():
        for key, bucket in group.items():
            bucket['findings'] = dict(bucket['findings'])
            # Nodes can occur in several findings with different requested fixes.
            bucket['nodes'] = {status: len(ids) for status, ids in bucket['nodes'].items()}
    counts = Counter(result['status'] for result in results)
    return {'count': len(results), 'unit': 'findings', 'findings': dict(counts), **buckets,
            'method': 'Missing viewer nodes are unverifiable, not automatically resolved. Theme buckets are heuristic; classes are reviewer supplied. Node counts overlap across findings.',
            'examples': results[:20], 'results': results}


def evaluation_report(value, private=False):
    """Public evaluation contains counts only, never IDs, fixes or labels."""
    return {key: item for key, item in value.items() if private or key not in {'examples', 'results'}}


def write_report(path, value, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o755)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, 'w') as stream:
        if private:
            os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--viewer', type=Path, default=root / 'output/graph.html')
    args = parser.parse_args(argv)
    status = {'viewer': 'output/graph.html', 'nodes': 0, 'decrypted': False, 'warnings': []}
    nodes = []
    scored = None
    try:
        nodes, status['decrypted'], status['warnings'] = read_viewer(args.viewer, root / 'data/private/viewer_password')
        status['nodes'] = len(nodes)
        sections = audit(nodes, common_domain_words())
    except Exception:
        status['warnings'].append('viewer audit unavailable; build continues')
        sections = {key: {'count': 0, 'examples': []} for key in SECTIONS}
    try:
        findings_path = root / 'data/private/audit/full_read/all_findings.json'
        if findings_path.is_file():
            # The snapshot the reviewers read (frozen); viewer_nodes.json is overwritten by every dump,
            # which would make fixed types and labels look untouched.
            baseline_path = root / 'data/private/audit/full_read/reviewed_snapshot.json'
            if not baseline_path.is_file():
                baseline_path = root / 'data/private/audit/viewer_nodes.json'
            baseline = json.loads(baseline_path.read_text()) if baseline_path.is_file() else []
            mapping_path = root / 'output/identity_map.json'
            mapping = json.loads(mapping_path.read_text()) if mapping_path.is_file() else {}
            scored = evaluation(nodes, json.loads(findings_path.read_text()), baseline, mapping)
    except Exception:
        status['warnings'].append('review evaluation unavailable; build continues')
    try:
        write_report(root / 'output/node_audit.json', {**status, 'sections': report(nodes, sections),
            **({'evaluation': evaluation_report(scored)} if scored is not None else {})})
    except OSError:
        status['warnings'].append('public report could not be written; build continues')
    # Never leave stale private samples when a later build has no password.
    private_path = root / 'data/private/audit/node_audit.json'
    try:
        if status['decrypted']:
            write_report(private_path, {**status, 'sections': report(nodes, sections, private=True),
                **({'evaluation': evaluation_report(scored, private=True)} if scored is not None else {})}, private=True)
        elif private_path.exists():
            private_path.unlink()
    except OSError:
        status['warnings'].append('private report could not be updated; build continues')
    for key, value in sections.items():
        print(f'{key}: {value["count"]}')
    if scored is not None:
        print('evaluation: ' + ', '.join(f'{key}={count}' for key, count in sorted(scored['findings'].items())))
    for warning in status['warnings']:
        print('audit_nodes: ' + warning)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
