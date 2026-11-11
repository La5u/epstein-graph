"""Contact-backed header names. No corpus reads, evolving spelling chains or public labels.

The spelling index is frozen. Context is projected through the resolver's existing
identity map once, before proposing any new links. Documents and correspondents
are sets: repeated OCR copies cannot meet a threshold by inflating record counts.
"""
from bisect import bisect_left
from functools import lru_cache
import re
from collections import Counter, defaultdict
from itertools import combinations

from header_text import header_junk_reason


ASSOCIATIONS = {'header_name_association', 'header_label_association', 'upstream_resolved_header_identity'}
COMMUNICATIONS = {'sent_to', 'cc_to'}
LOCATION_PREFIX = re.compile(r'^(?:nyc|ny|london|paris)\s+(?:[-–:]\s*)?', re.I)
TITLE_PREFIX = re.compile(r'^(?:governor|gov\.?|senator|sen\.?|ambassador|professor|prof\.?)\s+', re.I)
ROLE_TAIL = re.compile(r'\s+(?:secretary|co-founder)\s*$', re.I)
CREDENTIAL_TAIL = re.compile(r'(?:\s+(?:m\.?\s*d\.?|ph\.?\s*d\.?|esq\.?|cpa|mba|jd))+[.,\s]*$', re.I)
FIELD_TAIL = re.compile(r'(?:\s+|(?<=[a-z])(?=[A-Z]))(?:Subject|Cc|Bcc)\s*:?[\s]*$')
ID_TAIL = re.compile(r'(?:\s*-\s*(?:[a-z]|[0-9]{1,3})|\s*\[dead\])\s*$', re.I)
GIVEN_ID = re.compile(r"^([a-z][a-z'’]+)-[a-z]\s+", re.I)
IDENTITY_SUFFIXES = {'jr', 'sr', 'ii', 'iii', 'iv', 'llc', 'llp', 'pllc', 'pc', 'pa', 'inc', 'ltd', 'corp'}


def sender_signature(text, sender, full):
    """A complete signature inside this sender's own message, before any reply.

    Mere page co-occurrence is insufficient. Require an explicit From slot,
    a Subject boundary, a standalone full name, and signature furniture.
    """
    from header_text import HEADER_FIELD, BODY_AFTER_FIELD, word_key
    lines = [line.strip().lstrip('>|').strip() for line in text.splitlines() if line.strip()]
    own, target = word_key(sender), word_key(full)
    active = subject = False
    for n, line in enumerate(lines):
        field = HEADER_FIELD.match(line)
        if field and field[1].casefold() == 'from':
            value = field[2].strip() or (lines[n + 1] if n + 1 < len(lines) else '')
            active, subject = own in word_key(value), False
            continue
        if BODY_AFTER_FIELD.match(line):
            active = False
        if not active:
            continue
        if field:
            subject |= field[1].casefold() == 'subject'
            continue
        if subject and word_key(line) == target:
            before = ' '.join(lines[max(0, n - 2):n])
            after = ' '.join(lines[n + 1:n + 4])
            if (re.search(r'\b(?:regards|sincerely|thanks|thank you|best wishes|best|cheers)\W*$', before, re.I)
                    or re.match(r'(?:tel\b|fax\b|cell\b|mobile\b|https?://|www\.|\d+\s+\w)|'
                                r'.*\b(?:LLC|LLP|Inc\.?|Associates|Corporation)\b', after, re.I)):
                return True
    return False


def decorated_name(label):
    """Return a stripped spelling and an auditable rule; never edit the raw node."""
    if GIVEN_ID.search(label):
        return GIVEN_ID.sub(r'\1 ', label, count=1), 'id_suffix'
    for pattern, rule in ((CREDENTIAL_TAIL, 'credential_tail'), (FIELD_TAIL, 'header_field_tail'),
                          (LOCATION_PREFIX, 'location_prefix'), (TITLE_PREFIX, 'title_prefix'),
                          (ROLE_TAIL, 'role_tail'), (ID_TAIL, 'id_suffix')):
        core = pattern.sub('', label, count=1).strip(' ,;')
        if core != label and core:
            return core, rule
    return label, None


OCR_LOOKALIKES = frozenset((x, y) for g in ('il1', 'o0', 'uvy', 'mn', 'bd', 'ce', 'fr', 'gq', 'zs') for x in g for y in g)
OCR_LIGATURES = defaultdict(list)
for _left, _right in (('rn', 'm'), ('cl', 'd'), ('vv', 'w'), ('li', 'h')):
    OCR_LIGATURES[_left[-1], _right[-1]].append((_left, _right))
    OCR_LIGATURES[_right[-1], _left[-1]].append((_right, _left))
OCR_LIGATURES = dict(OCR_LIGATURES)


@lru_cache(maxsize=1 << 18)
def ocr_cost(a, b, limit=float('inf')):
    """Weighted character OCR alignment, including multi-character ligatures.

    Generic substitutions cost 1, missing characters .9, visual confusions .2.
    This score is ONLY usable with a shared, non-conflicting mailbox.
    Above `limit` the exact value does not matter: returns inf as soon as it is
    certain (a ligature skips at most one row, so two rows over the limit end it).
    """
    previous2, previous = None, [.9 * j for j in range(len(b) + 1)]
    above = min(previous) > limit
    for i, x in enumerate(a, 1):
        row = [.9 * i]
        for j, y in enumerate(b, 1):
            cost = 0 if x == y else .2 if (x, y) in OCR_LOOKALIKES else 1
            best = min(previous[j] + .9, row[j - 1] + .9, previous[j - 1] + cost)
            for left, right in OCR_LIGATURES.get((x, y), ()):
                if i >= len(left) and j >= len(right) and a[i-len(left):i] == left and b[j-len(right):j] == right:
                    best = min(best, (previous if len(left) == 1 else previous2)[j-len(right)] + .2)
            row.append(best)
        over = min(row) > limit
        if over and above:
            return float('inf')
        above, previous2, previous = over, previous, row
    return previous[-1]


def quoted_handle_documents(pairs):
    """Bounded FTS check for handles whose full name is only in quoted headers.

    Batch many name pairs in each query. Read at most 40 hits per batch, 6 pages
    per document, and 200k characters per page. No body-only name co-occurrence.
    Missing/redacted evidence yields no link; query limits can only lose recall.
    """
    import corpus
    if not pairs or not corpus.PATH.exists():
        return {}
    def header(label):
        name = r'\s+'.join(map(re.escape, label.split()))
        return re.compile(r'(?:^(?:from|to|cc):\s*|^on[^\n]{0,150},\s*)' + name + r'(?=\s|[<>,]|$)', re.I | re.M)
    found = defaultdict(set)
    with corpus.connect() as db:
        db.execute('PRAGMA cache_size=-8000')
        for start in range(0, len(pairs), 12):
            batch = pairs[start:start + 12]
            query = ' OR '.join('("' + a.replace('"', '""') + '" AND "' + b.replace('"', '""') + '")'
                                for _, _, a, b in batch)
            # search_text is streaming and uncached: do not put private query
            # names into the public output/.build FTS query cache.
            cursor = corpus.search_text(db, query)
            hits = dict.fromkeys(doc for doc, _ in cursor.fetchmany(40))
            cursor.close()
            patterns = [(i, t, header(a), header(b)) for i, t, a, b in batch]
            for doc in hits:
                rows = db.execute('SELECT substr(text,1,200000) FROM pages WHERE doc_id=? ORDER BY page LIMIT 7', (doc,)).fetchall()
                if len(rows) > 6:
                    continue
                text = '\n'.join(row[0] or '' for row in rows)
                if len(re.findall(r'^(?:from|to|cc):', text, re.I | re.M)) > 20:
                    continue
                for i, t, own, full in patterns:
                    if own.search(text) and full.search(text):
                        found[i, t].add(doc)
    return found


class ContextNames:
    # K=3 independent non-hub correspondents. At least 60% of the smaller
    # neighborhood must overlap. A rival with >= half the overlap vetoes it.
    MIN_CORRESPONDENTS = 3
    MAX_HUB_NEIGHBORS = 100
    MAX_DOC_PEOPLE = 20

    def __init__(self, db, names, counts, reviewed, final, words, nickname, quality, forbidden=(), mailbox_root=None,
                 mailbox_values=None, mailbox_owner=None):
        self.names, self.counts, self.final = names, counts, final
        self.mailbox_owner = mailbox_owner or final
        self.words, self.nickname = words, nickname
        self.forms = {i: words(label) for i, label in names.items()}
        self.refs = {}
        self.exact, self.tokens, self.handles = defaultdict(set), defaultdict(set), defaultdict(set)
        self.by_initial = defaultdict(set)
        self.by_first, self.by_last = defaultdict(set), defaultdict(set)
        self.by_root = defaultdict(set)
        self.root_of = {i: final(i) for i in names}
        forbidden = set(forbidden) | {'airlines', 'customer', 'service', 'vacation', 'beach'} | IDENTITY_SUFFIXES
        for i, form in self.forms.items():
            if (not 2 <= len(form) <= 4 or min(len(form[0]), len(form[-1])) < 2
                    or not re.fullmatch(r"[\w\s.'’,-]+", names[i])
                    or quality(names[i]) != 'ok' or header_junk_reason(names[i]) or any(w in forbidden for w in form)
                    or reviewed.get(i, {}).get('verdict') not in (None, 'ok_person', 'variant_of_known', 'fix_label')
                    or decorated_name(names[i])[1]):
                continue
            self.refs[i] = form
            self.by_root[self.root_of[i]].add(i)
            self.exact[form].add(i)
            self.tokens[nickname(form[0])].add(i)
            self.tokens[form[-1]].add(i)
            self.by_initial[form[0][0], form[-1]].add(i)
            self.by_first[nickname(form[0])].add(i)
            self.by_last[form[-1]].add(i)
            handles = {''.join(form), ''.join(w[0] for w in form), form[0][0] + form[-1],
                       ''.join(w[0] for w in form[:-1]) + form[-1], form[0] + form[-1]}
            for handle in handles:
                self.handles[handle].add(i)
        # Two compatible FULL names separately listed in the same recipient
        # field are evidence of two people (e.g. a nickname and its expansion).
        # Retain message context only for roots which can have such a rival.
        # Sorted surnames per initial: a candidate known only by its initial
        # can only be a 'partial' match, whose surname starts with ours.
        self.initial_lasts = defaultdict(list)
        for initial, last in sorted(self.by_initial):
            self.initial_lasts[initial].append(last)
        surnames, firsts = defaultdict(list), defaultdict(list)
        for i, form in self.refs.items():
            surnames[form[-1]].append(i)
            firsts[nickname(form[0])].append(i)
        competing = set()
        for ids in (*surnames.values(), *firsts.values()):
            for n, i in enumerate(ids):
                for j in ids[n + 1:]:
                    a, b = self.refs[i], self.refs[j]
                    if (a != b and (self.compatible(a, b) or self.compatible(b, a) or self.visual_spelling(a, b))
                            and self.root_of[i] != self.root_of[j]):
                        competing.add(frozenset((self.root_of[i], self.root_of[j])))
        competing_roots = set().union(*competing) if competing else set()
        recipient_fields = defaultdict(set)
        self.addresses, self.docs, self.peers = defaultdict(Counter), defaultdict(set), defaultdict(set)
        self.doc_people = defaultdict(set)
        self.address_people = defaultdict(set)
        self.mailbox_names = defaultdict(Counter)
        # Raw identities on the existing DB may have been resolved out of the
        # snapshot; the caller's final() includes those established aliases too.
        columns = {r[1] for r in db.execute('PRAGMA table_info(evidence)')}
        document = 'efta_number' if 'efta_number' in columns else 'NULL'
        provenance = ',source_db,source_table,source_record_id,message_index' if {
            'source_db', 'source_table', 'source_record_id', 'message_index'} <= columns else ''
        roots = set(self.root_of.values())
        root_cache = {}
        def root(i):
            if i not in root_cache:
                root_cache[i] = final(i)
            return root_cache[i]
        mailbox_root = mailbox_root or (lambda i: i)
        for raw_a, raw_b, kind, doc, *record in db.execute(f'SELECT source_id,target_id,relationship_type,{document}{provenance} FROM evidence'):
            a, b = root(raw_a), root(raw_b)
            if kind in ASSOCIATIONS:
                # Upstream identity links point email -> name; parsed display
                # name associations point name -> email. Retain actual mailbox
                # families, even after both addresses resolve to one owner.
                person, address = ((b, raw_a) if raw_a.startswith('email_') and not raw_b.startswith('email_')
                                   else (a, raw_b))
                if person in roots and address.startswith('email_'):
                    address = mailbox_root(address)
                    self.addresses[person][address] += 1
                    self.address_people[address].add(person)
                    self.mailbox_names[address][person] += 1
            if kind in COMMUNICATIONS:
                if a != b:
                    self.peers[a].add(b)
                    self.peers[b].add(a)
                if record and b in competing_roots:
                    recipient_fields[(a, kind, doc, *record)].add(b)
            if doc and kind in COMMUNICATIONS | ASSOCIATIONS:
                self.doc_people[doc].update((a, b))
                for i in (a, b):
                    if i in roots:
                        self.docs[i].add(doc)
        self.colisted = {pair for people in recipient_fields.values() for a, b in combinations(people, 2)
                         if (pair := frozenset((a, b))) in competing}
        # "Given I brand" is an OCR vertical-bar signature when that long last
        # token is the actual mailbox domain, not a fuller personal surname.
        # This only withholds an anchor; it never deletes or relabels the node.
        for i, form in list(self.refs.items()):
            if len(form) >= 3 and 'i' in form[1:-1] and len(form[-1]) >= 7:
                if any(form[-1] == (mailbox_values or {}).get(address, '').rsplit('@', 1)[-1].split('.')[0].casefold()
                       for address in self.addresses[self.root_of[i]]):
                    del self.refs[i]
        self.hubs = {i for i, peers in self.peers.items() if len(peers) > self.MAX_HUB_NEIGHBORS}
        for peers in self.peers.values():
            peers.difference_update(self.hubs)
        self.last_details = {}
        self.candidate_cache = {}
        self.quoted = {}
        self.spelling_headers = {}
        self.signatures = {}

    def visual_spelling(self, a, b):
        """Only a visual confusion/ligature, or one lost letter in a long word.

        This produces candidates, never a spelling-only identity decision.
        Whole distinct middle names, initials and generations remain significant.
        """
        if len(a) != len(b) or not 2 <= len(a) <= 4 or a[1:-1] != b[1:-1]:
            return False
        if a[-1] == b[-1] and min(len(a[-1]), len(a[0]), len(b[0])) >= 5:
            x, y = a[0], b[0]
        elif self.nickname(a[0]) == self.nickname(b[0]) and min(len(a[-1]), len(b[-1])) >= 6:
            x, y = a[-1], b[-1]
        else:
            return False
        if x == y:
            return False
        # No generic substitution/transposition. At .2 per visual substitution,
        # .9 for a missing character, .95 admits at most one ordinary deletion.
        return abs(len(x) - len(y)) <= 1 and ocr_cost(x, y, .95) <= .95

    def attest_spellings(self, read_document):
        """Read at most three existing shared documents per visual candidate.

        No FTS scan. Both spellings must actually occur in party slots, not
        signatures or body lists. The caller bounds each document to six pages.
        """
        from header_text import nonparty_header_text, word_key
        from functools import lru_cache

        @lru_cache(maxsize=64)
        def keys(doc):
            text = read_document(doc)
            return (text, word_key(text), nonparty_header_text(text)) if text else ('', '', '')

        for i in self.names:
            if self.root_of[i] != i:
                continue
            hits, _ = self.candidates(i)
            for target, kind in hits.items():
                if self.mailbox_ok(i, target) and kind != 'source_shape':
                    continue
                # The complete signature can attest a KG name with no header
                # evidence at all. Partial/visual candidates still have to pass
                # this same-message check; a bare given name is never guessed.
                docs = self.docs[i] if kind in ('partial', 'visual', 'source_shape') else set()
                for doc in sorted(docs)[:3]:
                    if len(self.doc_people[doc]) > self.MAX_DOC_PEOPLE:
                        continue
                    source, text, nonparty = keys(doc)
                    if sender_signature(source, self.names[i], self.names[target]):
                        self.signatures.setdefault((i, target), set()).add(doc)
                    # Check full word runs, including punctuation normalization.
                    own, other = word_key(self.names[i]), word_key(self.names[target])
                    if (kind == 'visual' and doc in self.docs[target]
                            and own in text and other in text and own not in nonparty and other not in nonparty):
                        self.spelling_headers.setdefault((i, target), set()).add(doc)

    def compatible(self, own, other):
        if own == other:
            return 'exact'
        if len(own) == 1:
            text = own[0]
            if len(text) >= 3 and (self.nickname(text) == self.nickname(other[0]) or text == other[-1]):
                return 'partial'
            if text in {''.join(other), ''.join(w[0] for w in other), other[0][0] + other[-1],
                        ''.join(w[0] for w in other[:-1]) + other[-1], other[0] + other[-1]}:
                return 'handle'
        if 2 <= len(own) <= len(other):
            # Missing middle words/initials are allowed; all supplied words must
            # align in order and contradictory middle initials never disappear.
            first = (self.nickname(own[0]) == self.nickname(other[0])
                     or len(own[0]) == 1 and own[0] == other[0][0]
                     or len(own[0]) >= 3 and other[0].startswith(own[0]))
            last = (own[-1] == other[-1] or len(own[-1]) >= 1 and other[-1].startswith(own[-1]))
            middle = not own[1:-1] or (len(own[1:-1]) == len(other[1:-1]) and all(
                x == y or len(x) == 1 and y.startswith(x) for x, y in zip(own[1:-1], other[1:-1])))
            if first and last and middle:
                return 'partial'
        return None

    def mailbox_ok(self, a, b):
        own, other = self.addresses[a], self.addresses[b]
        if not own:
            return False
        shared = own.keys() & other.keys()
        # Shared/assistant mailboxes must not choose between compatible people.
        safe = {address for address in shared if
                sum(self.mailbox_names[address][i] for i in {a, b}) >= .9 * sum(self.mailbox_names[address].values())}
        # A previously established owner also attests a second physical mailbox.
        # Do not confuse this with replacing every mailbox by a person key: an
        # unrelated stray name on a hub's address still cannot claim that hub's
        # mailbox as evidence for a different full-name identity.
        safe.update(address for address in own if self.mailbox_owner(address) == b)
        return bool(safe) and sum(own[x] for x in safe) >= .9 * sum(own.values())

    def candidates(self, identity):
        # The index is frozen after __init__ and each identity is asked up to three times.
        if identity not in self.candidate_cache:
            self.candidate_cache[identity] = self.find_candidates(identity)
        return self.candidate_cache[identity]

    def find_candidates(self, identity):
        core, decoration = decorated_name(self.names[identity])
        own = self.words(core)
        if (not own or len(own) > 4 or own[-1] in IDENTITY_SUFFIXES
                or re.search(r'@|\band\b|&|\?', core, re.I)
                or re.search(r'\w-\s*$', core)):  # 'Given-' is a cut 'Given-M Surname', not a bare name
            return {}, decoration
        options = set(self.exact.get(own, ()))
        if len(own) == 1:
            options.update(self.tokens.get(self.nickname(own[0]), ()))
            options.update(self.handles.get(own[0], ()))
        else:
            lasts = self.initial_lasts.get(own[0][0], ())
            k = bisect_left(lasts, own[-1])
            while k < len(lasts) and lasts[k].startswith(own[-1]):
                options.update(self.by_initial[own[0][0], lasts[k]])
                k += 1
            options.update(self.by_first.get(self.nickname(own[0]), ()))
            options.update(self.by_last.get(own[-1], ()))
        # Larger OCR distances are explored only among names read on the same
        # mailbox, not the whole spelling index.
        root = self.root_of[identity]
        mailbox_roots = {p for a in self.addresses[root] for p in self.address_people[a]}
        mailbox_roots.update(self.mailbox_owner(address) for address in self.addresses[root])
        options.update(i for root in mailbox_roots for i in self.by_root[root])
        hits = {}
        for i in options:
            if i not in self.refs:
                continue
            target = self.root_of[i]
            if target == root or target not in self.names:
                continue
            other = self.refs[i]
            kind = self.compatible(own, other)
            if kind is None and self.visual_spelling(own, other):
                kind = 'visual'
            if kind is None and len(own) == len(other) >= 2 and own[1:] == other[1:]:
                # A cut at the LEFT edge of the given name needs a signature;
                # "Anna" and "Brianna" cannot match from neighborhood alone.
                if len(own[0]) >= 3 and 0 < len(other[0]) - len(own[0]) <= 3 and other[0].endswith(own[0]):
                    kind = 'source_shape'
            if kind is None and target in mailbox_roots and len(own) >= 2:
                if len(own) == len(other) and any(len(x) == len(y) == 1 and x != y for x, y in zip(own, other)):
                    continue  # explicit given/middle initials are identities, not OCR noise
                a, b = ''.join(own), ''.join(other)
                if (min(len(a), len(b)) >= 8 and abs(len(a)-len(b)) <= 5
                        and (own[0][0] == other[0][0] or own[-1] == other[-1])
                        and ocr_cost(a, b, limit := min(3.2, .23 * max(len(a), len(b)))) <= limit):
                    kind = 'ocr'
            if kind:
                # Same canonical reference can be attested by several spellings.
                priority = {'exact': 0, 'partial': 1, 'handle': 2, 'visual': 3, 'source_shape': 4, 'ocr': 5}
                if target not in hits or priority[kind] < priority[hits[target]]:
                    hits[target] = kind
        return hits, decoration

    def propose(self, identity):
        hits, decoration = self.candidates(identity)
        a = self.root_of[identity]
        support = {}
        signatures = {self.forms[b] for b in hits}
        for b, kind in hits.items():
            shared_address = self.mailbox_ok(a, b)
            if frozenset((a, b)) in self.colisted and not shared_address:
                continue
            if self.addresses[a] and self.addresses[b] and not shared_address:
                continue  # conflicting mailboxes outweigh shared pages/neighbors
            docs = {d for d in self.docs[a] & self.docs[b] if len(self.doc_people[d]) <= self.MAX_DOC_PEOPLE}
            peers = (self.peers[a] & self.peers[b]) - {a, b}
            if shared_address and kind != 'source_shape':
                score, rule = 3, 'mailbox_ocr' if kind in ('ocr', 'visual') else 'mailbox'
            elif (signed := self.signatures.get((identity, b))):
                docs = docs | signed
                score, rule = 2, 'sender_signature'
            elif kind == 'visual':
                docs = self.spelling_headers.get((identity, b), set())
                if not docs:
                    continue
                # An OCR spelling never absorbs a graph person or a stronger
                # reference. Repetition of damaged text alone cannot pick a root.
                if not b.startswith('kg:') and self.counts[b] < 5 * max(self.counts[identity], 1):
                    continue
                score, rule = 2, 'header_spelling'
            elif kind == 'ocr':
                continue
            elif kind == 'source_shape':
                continue
            elif decoration and kind == 'exact':
                score, rule = 2, decoration
            elif (quoted := self.quoted.get((identity, b))):
                docs = docs | quoted
                score, rule = 2, 'quoted_header'
            elif docs and (len(self.words(decorated_name(self.names[identity])[0])) > 1
                           or len(docs) >= 2 and len(docs) >= .8 * len(self.docs[a])
                           or len(signatures) == 1
                           or kind == 'handle' and len(''.join(self.words(self.names[identity]))) >= 3):
                score, rule = 2, 'document'
            elif len(peers) >= self.MIN_CORRESPONDENTS and len(peers) >= .6 * min(len(self.peers[a]), len(self.peers[b])):
                score, rule = 1, 'correspondents'
            else:
                continue
            support[b] = dict(score=score, rule=rule, kind=kind, documents=len(docs),
                              correspondents=len(peers), shared_mailboxes=len(self.addresses[a].keys() & self.addresses[b].keys()),
                              sample_documents=sorted(docs)[:5], sample_correspondents=sorted(peers)[:5],
                              sample_mailboxes=sorted(self.addresses[a].keys() & self.addresses[b].keys())[:5],
                              sample_owned_mailboxes=sorted(address for address in self.addresses[a] if self.mailbox_owner(address) == b)[:5])
        if not support:
            return None
        top_score = max(s['score'] for s in support.values())
        best = {b: s for b, s in support.items() if s['score'] == top_score}
        if top_score == 1 and len(best) > 1:
            most = max(s['correspondents'] for s in best.values())
            best = {b: s for b, s in best.items() if s['correspondents'] >= .5 * most}
        # Punctuation-only duplicates are one candidate, but two distinct KG
        # people with the same name remain separate. Never use frequency to
        # break a tie between different spellings/people.
        if len(best) > 1 and len({self.forms[b] for b in best}) == 1:
            kg = {b for b in best if b.startswith('kg:')}
            if len(kg) <= 1:
                chosen = next(iter(kg)) if kg else max(best, key=lambda b: (self.counts[b], b))
                best = {chosen: best[chosen]}
        if len(best) != 1:
            return None
        b, detail = next(iter(best.items()))
        if (not decoration and self.forms[identity] == self.forms[b] and not b.startswith('kg:')
                and self.counts[b] <= self.counts[identity]):
            return None
        # Even a weakly supported competitor can invalidate a bare-name expansion.
        if detail['score'] == 1 and any(len((self.peers[a] & self.peers[c]) - {a, c}) >= .5 * detail['correspondents']
                                        for c in hits if c != b):
            return None
        self.last_details[identity] = dict(target=b, **detail)
        return b, 'header_context:' + detail['rule']


def compound_people(names, final, words, refs, excluded=(), forbidden=(), common_words=()):
    """Keep a compound label and link each uniquely resolvable constituent.

    A shared surname is supplied to the first given name only when written on
    the second. No organization names, inference from a bare pair or survivor
    overlays. Links describe the label's text, never identity equivalence.
    """
    exact = defaultdict(set)
    excluded = set(excluded)
    for i in refs:
        if i in excluded or final(i) in excluded:
            continue
        exact[words(names[i])].add(final(i))
    result = {}
    for i, label in names.items():
        if i in excluded or final(i) != i:
            continue
        parts = re.split(r'\s+(?:and|&)\s+', label, flags=re.I)
        if len(parts) != 2:
            continue
        left, right = map(words, parts)
        if len(left) == 1 and 2 <= len(right) <= 3:
            left = left + right[1:]
        if (not (2 <= len(left) <= 4 and 2 <= len(right) <= 4)
                or any(w in forbidden or not w.isalpha() for w in left + right)
                or any(p[0] in common_words and p[0] not in {'mom', 'dad'} for p in (left, right))):
            continue
        targets = [exact.get(p, set()) - {i} for p in (left, right)]
        resolved = {next(iter(t)) for t in targets if len(t) == 1}
        # Two explicit full names are still a compound when the individuals do
        # not exist elsewhere. Do not invent their identities or merge the pair.
        if resolved or len(parts[0].split()) >= 2 and len(parts[1].split()) >= 2:
            result[i] = resolved
    return result
