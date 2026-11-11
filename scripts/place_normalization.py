"""Small, shared place-label rules; private annotations never form identity keys."""
import re

US_STATE_CODES = set('al ak az ar ca co ct de dc fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy pr vi'.split())
STREET_WORDS = set('street st avenue ave road rd boulevard blvd drive dr lane ln place pl court ct highway hwy parkway pkwy square sq terrace ter way mews gardens'.split())
ANNOTATION = re.compile(r'\s*\[signature:\s*([^\]]*)\]\s*', re.I)
NON_PLACES = {'client', 'cursor', 'refunds', 'relationship', 'respectfully', 'event', 'esq',
              'ecrit', 'rhode island registration number'}
TIME_ZONE = re.compile(r'^(?:(?:eastern|central|mountain|pacific|atlantic|greenwich mean)\s+(?:(?:standard|daylight)\s+)?time|utc|gmt)$', re.I)
# Attested airport/institution names, not fuzzy matches on arbitrary proper names.
# In particular St Thomas (the island) is never an alias of its airport, and an
# Annex remains a separate facility despite sharing a postal address.
PLACE_ALIAS_GROUPS = (
    ('LaGuardia Airport', 'La Guardia Airport', 'LGA Airport', 'LGA', 'LaGuardia'),
    ('Miami International Airport', 'Miami International', 'MIA Airport', 'MIA'),
    ('Cyril E. King Airport (St. Thomas)', 'Cyril E King Airport', 'Cyril E King', 'TIST', 'STT', 'TIST/STT'),
    ('Metropolitan Correctional Center (MCC New York)', 'Metropolitan Correctional Center, New York', 'MCC New York'),
    ('St. Thomas', 'St Thomas', 'Saint Thomas'),
)


def aliases_for_place(text):
    key = place_key(text)
    for group in PLACE_ALIAS_GROUPS:
        if key in {place_key(value) for value in group}:
            return list(group)
    return []


def place_annotations(text):
    return [m.group(1).strip() for m in ANNOTATION.finditer(text)]


def clean_place_text(text):
    return ' '.join(ANNOTATION.sub(' ', text).split())


def place_label(text):
    """Drop only a trailing state/NA qualifier; preserve the source's spelling."""
    text = clean_place_text(text).strip()
    # NE is also Nebraska; on a numbered street it can be a direction.
    # Address normalization preserves all unit/locality/direction qualifiers.
    if re.match(r'^\d+\s', text):
        return text
    match = re.search(r'[,\s]+([A-Za-z.]+)$', text)
    if match and (match[1].casefold().replace('.', '') in US_STATE_CODES or match[1].casefold().replace('.', '') == 'na'):
        text = text[:match.start()].rstrip(' ,')
    return text


def place_key(text):
    text = place_label(text).casefold().strip()
    text = re.sub(r'^the\s+', '', text)
    text = re.sub(r'\s+(?:sent|destination|registration number:?)$', '', text)
    # Dots in abbreviations are punctuation, not a different place identity.
    words = re.findall(r"[a-z0-9'-]+", text.replace('.', ''))
    return ' '.join(words).strip(" .'-")


def place_label_reason(text):
    key = place_key(text)
    squeezed = re.sub(r'[^a-z0-9]', '', key)
    if not key or squeezed in {'na', 'none', 'null'}:
        return 'place_placeholder'
    if key in STREET_WORDS:
        return 'bare_street_word'
    if squeezed in US_STATE_CODES:
        return 'bare_state_code'
    if key in NON_PLACES:
        return 'non_place_term'
    if TIME_ZONE.fullmatch(clean_place_text(text).strip()):
        return 'time_zone'
    if re.fullmatch(r'(?:(?:' + '|'.join(sorted(US_STATE_CODES)) + r')\s+)?\d{5}(?:-\d{4})?', key):
        return 'postal_code'
    if re.fullmatch(r'0+\s+.+', key) or re.match(r'^\d+\s+(?:(?:go\s+)?see|meet|dear|hello|arrives?|departs?)\b', key):
        return 'itinerary_or_greeting_address'
    if re.fullmatch(r'\d+\s+the\s+(?:st|street)', key):
        return 'address_without_street_name'
    if re.fullmatch(r'(?:suite|apt|apartment|unit|floor|room)\s+[a-z0-9-]+', key):
        return 'unit_without_address'
    return None


def clean_place_node(node):
    """Retain the node and its annotations, marking noise for viewer filtering."""
    node = dict(node)
    if node.get('entity_type') == 'survivor':
        return node
    value = node['name']
    node['name'] = place_label(value)
    aliases = aliases_for_place(node['name'])
    if aliases:
        previous = node.get('aliases') or []
        if isinstance(previous, str):
            previous = [previous]
        node['aliases'] = sorted(set(previous) | set(aliases) - {node['name']})
    annotations = place_annotations(value)
    if annotations:
        node['place_annotations'] = list(dict.fromkeys([*(node.get('place_annotations') or []), *annotations]))
    reason = place_label_reason(node['name'])
    if reason and node.get('entity_type') != 'survivor':
        node.update(label_quality='not_a_name', suspected_parse_artifact=True, place_quality_reason=reason)
    return node
