"""Document-text-only header checks, independent of graph identities and reviews.

The persistent HeaderCache is versioned on this module. Keep every parsing rule
and helper used by its checks here; evaluate builder/review exemptions separately.
"""
import re
from array import array
from collections import defaultdict
from email.utils import getaddresses
from functools import lru_cache
from pathlib import Path


EMAIL = re.compile(r"^[^\s<>@,;:\[\]()]+@[^\s<>@,;:\[\]()]+\.[^\s<>@,;:\[\]()]+$")

RECIPIENT_SPLIT = re.compile(r'[,;](?![^<]*>)(?=(?:[^"]*"[^"]*")*[^"]*$)')

PLACEHOLDER_DOMAINS = {'example.com', 'example.org', 'example.net'}

PLACEHOLDER_LOCALS = {'unknown', 'redacted', 'none', 'noreply-redacted'}

INLINE_IMAGE_LOCAL = re.compile(r'\.(?:png|jpe?g|gif|bmp|tiff?|emz|wmz)$')


def valid_email(value):
    return _valid_email(value) if isinstance(value, str) else None


@lru_cache(maxsize=1 << 18)
def _valid_email(value):  # memoized: ~5M calls per build on far fewer distinct strings
    # Keep email normalization here too: cached document checks must depend
    # only on the code in this module, never on builder/contact rules.
    value = value.strip()
    if value.count('@') != 1 or re.search(r'\s', value):
        return None
    local, domain = value.rsplit('@', 1)
    value = local + '@' + domain.casefold() if local and domain else None
    if not value or not EMAIL.fullmatch(value):
        return None
    local, domain = value.casefold().rsplit('@', 1)
    # Exact placeholders only: substring checks dropped real addresses such as
    # unredacted@... or ...@notexample.com.
    if domain in PLACEHOLDER_DOMAINS or local in PLACEHOLDER_LOCALS or 'redacted' in re.split(r'[._+-]', local) or domain.split('.')[0] == 'redacted':
        return None
    if INLINE_IMAGE_LOCAL.search(local):
        return None  # "image001.png@01D0778D.2D49CD...": an inline image's Content-ID, not a mailbox
    return value


def parse_recipient(value):
    # The merged table mixes strings with structured [name, email] pairs.
    if isinstance(value, list) and len(value) == 2:
        name, address = value
        email = valid_email(address)
        if email:
            return [(name if isinstance(name, str) else '', email)]
        return parse_recipient(name) if isinstance(name, str) else []
    if not isinstance(value, str):
        return []
    return list(_parse_recipient(value))


@lru_cache(maxsize=1 << 17)
def _parse_recipient(value):
    """Cache immutable results; callers may freely change their returned list."""
    if not value.strip():
        return ()
    # Split on , or ; outside <...> and quotes; getaddresses alone ignores ';'
    # and truncates bare multi-word names ("Bob Jones" -> "Bob").
    pieces = [p.strip() for p in RECIPIENT_SPLIT.split(value) if p.strip()]
    parsed = [(name, email) for piece in pieces if '@' in piece
              for name, address in getaddresses([piece]) if (email := valid_email(address))]
    if not parsed:
        # Name-only lists: ';' always separates recipients; ',' only when every
        # piece is multi-word (otherwise it may be "Last, First").
        named = [p.strip('"').strip() for p in pieces]
        if len(named) > 1 and (';' in value or all(len(p.split()) >= 2 for p in named)):
            return tuple((p, None) for p in named)
        return ((value.strip(), None),)
    # Keep multi-word name-only recipients listed beside addresses. Single
    # tokens may be halves of "Last, First", so they are not kept.
    parsed += [(piece.strip('"').strip(), None) for piece in pieces
               if '@' not in piece and '<' not in piece and len(piece.split()) >= 2]
    return tuple(parsed)

NAME_PARTICLES = {'de', 'la', 'da', 'di', 'du', 'le', 'van', 'von', 'del', 'el', 'al', 'bin', 'ben', 'st'}

NICKNAMES = {'bill': 'william', 'will': 'william', 'bob': 'robert', 'rob': 'robert', 'larry': 'lawrence',
             'jim': 'james', 'jimmy': 'james', 'mike': 'michael', 'dick': 'richard', 'rick': 'richard',
             'rich': 'richard', 'tom': 'thomas', 'ted': 'edward', 'ed': 'edward', 'les': 'leslie',
             'alex': 'alexander', 'andy': 'andrew', 'dan': 'daniel', 'dave': 'david', 'steve': 'steven',
             'jeff': 'jeffrey', 'joe': 'joseph', 'ken': 'kenneth', 'chuck': 'charles', 'charlie': 'charles',
             'peggy': 'margaret', 'kathy': 'kathryn', 'kate': 'kathryn', 'liz': 'elizabeth', 'jon': 'jonathan'}

DICTIONARY = Path('/usr/share/dict/words')

HUNSPELL = Path('/usr/share/hunspell/en_US.dic')

# Words of a sentence, never of a name ("Hope you are well", "I think").
# Not may, might, will, us: Bruce Mdo, Will Long, US.
SENTENCE_WORDS = {'you', 'your', 'yours', 'we', 'our', 'are', 'is', 'was', 'were', 'am', 'be', 'been', 'being', 'has', 'have', 'had',
                  'do', 'does', 'did', 'not', 'this', 'that', 'these', 'those', 'they', 'them', 'their', 'he', 'she', 'his', 'her',
                  'him', 'it', 'its', 'im', 'ill', 'ive', 'can', 'could', 'would', 'should', 'shall', 'must', 'there', 'here', 'what',
                  'when', 'where', 'why', 'how', 'who', 'which', 'if', 'but', 'so', 'because', 'just', 'please', 'let', 'lets', 'got',
                  'get', 'going', 'went', 'go'}

# Capitalized in word lists, but not names on their own or next to plain words.
NOT_NAME_WORDS = {'at', 'care', 'case', 'darling', 'day', 'federal', 'friend', 'glass', 'good', 'house', 'in', 'internet', 'lie', 'little', 'me',
                  'more', 'news', 'nice', 'no', 'now', 'oh', 'on', 'or', 'port', 'post', 'right', 'set', 'sir', 'tell', 'thanksgiving'}

NICKNAME_WORDS = {'woody', 'wally', 'hawk', 'stew', 'vicars', 'bee', 'sneaky'}  # single dictionary words used as a name in the headers

FRAGMENT_START = re.compile(r"(?:(?:of|for|from|by|with|and|the|at|as|to|or|but|in|on|my)\s|(?:As|I|My|A|Of)\s+[a-z])")

FRAGMENT_PHRASE = re.compile(r"[ai]\s+\S+\s+\S")

TITLE_SMALL_WORDS = NAME_PARTICLES | {'a', 'an', 'and', 'at', 'for', 'in', 'of', 'on', 'or', 'the', 'to'}

ENDINGS = (('s', ''), ('es', ''), ('ed', ''), ('ed', 'e'), ('ing', ''), ('ing', 'e'), ('ion', 'e'), ('ation', 'e'), ('ation', 'ate'),
           ('ly', ''), ('ment', ''), ('ness', ''), ('ies', 'y'))


@lru_cache(maxsize=1)
def dictionary_words():
    """(common words, proper nouns, everyday stems), case-folded; empty without a system word list.
    The system list calls "here", "go" and "for" proper nouns too; hunspell's list
    (Mark, Rose, Hope, but not those) decides for the words it knows."""
    if not DICTIONARY.exists():
        return frozenset(), frozenset(), frozenset()
    words = DICTIONARY.read_text(errors='ignore').split()
    hunspell = [w.split('/')[0] for w in HUNSPELL.read_text(errors='ignore').split('\n')[1:]] if HUNSPELL.exists() else []
    everyday = frozenset(w for w in hunspell if w.islower())
    return (frozenset(w for w in words if w.islower()) | everyday,
            frozenset(w.casefold() for w in hunspell if w[:1].isupper()) | frozenset(
                w.casefold() for w in words if w[:1].isupper() and w.casefold() not in everyday),
            everyday)


def everyday_word(word):
    """In the everyday list, directly or by a regular ending ("requested", "Commodities"); not "Wally" or "Rober"."""
    everyday = dictionary_words()[2]
    return word in everyday or any(word.endswith(end) and word[:-len(end)] + stem in everyday for end, stem in ENDINGS)


def title_case(label):
    """"Dept of Neurology", "SEAPORT HOTEL": how an organization is written, not a sentence."""
    words = re.findall(r"[^\W\d_][^\W_]*", re.sub(r"['\u2019]s\b", '', label))
    return len(words) > 1 and words[-1][0].isupper() and all(w[0].isupper() or w.casefold() in TITLE_SMALL_WORDS for w in words)


def sentence_text(label):
    """Header text with a sentence word ("Bank has received your request", "I think"),
    or lowercase words around a name ("Per Mr. Fraguela's request"). Uppercase IT or AM are not sentence words."""
    words = re.findall(r"[^\W\d_]+", re.sub(r"['\u2019](?:s|t|re|ll|ve|d|m)\b", '', label))
    if len(words) < 2 or title_case(label) and not label.isupper():
        return False
    if re.match(r"(?:per|good \w+)\s+(?:mr|mrs|ms|dr)\b", label.casefold()):
        return True  # "Per Mr. Fraguela's request", "Good evening Mr. Kahn"
    if re.search(r"n['\u2019]t\b|['\u2019](?:re|ll|ve)\b|\bI['\u2019]m\b", label) or (words[0] == 'I' and words[1].islower()):
        return True
    if any(w.casefold() in SENTENCE_WORDS and (not w.isupper() or label.isupper()) for w in words):
        return True
    common = dictionary_words()[0]
    plain = [w for w in label.split()[1:] if w.islower() and w.isalpha() and w in common and w not in TITLE_SMALL_WORDS]
    return len(words) >= 3 and len(plain) >= 2 and words[0].casefold() in common and not name_word(words[0]) and len(plain) * 2 >= len(words) - 1


def fragment_text(label):
    """A line cut out of a sentence: "as soy", "i spoke to sue", "As discussed", "My best".
    A lowercase "a" or "i" may be an initial ("a sferrazza"), so those need a longer phrase."""
    return bool(FRAGMENT_START.match(label) or FRAGMENT_PHRASE.match(label))


def plain_text(label, name_words=frozenset()):
    """Every word of a label is everyday English and it reads as text, not a name or an organization."""
    if fragment_text(label):
        return True
    common = dictionary_words()[0]
    words = re.findall(r"[^\W\d_]+", re.sub(r"['\u2019](?:s|t|re|ll|ve|d|m)\b", '', label.casefold()))
    if not words or re.search(r"\w\.(?:com|org|net|edu|html?)\b", label.casefold()) or words[0] in NICKNAME_WORDS:
        return False
    if sentence_text(label) and sum(w not in common for w in words) * 3 <= len(words):
        return True
    if not all(w in common and (len(w) > 1 or w in 'ai') and not name_word(w, name_words) for w in words):
        return False
    return everyday_word(words[0]) if len(words) == 1 else not title_case(label)


def name_word(word, name_words=frozenset()):
    """A word that can be (part of) a name: a proper noun, nickname or known person's name word."""
    word = word.casefold()
    return word not in NOT_NAME_WORDS and (word in dictionary_words()[1] or word in name_words or word in NICKNAMES or word in NICKNAMES.values())

HEADER_FIELD = re.compile(r'^[\s>|]*(from|to|cc|bcc|sent|date|subject|attachments?|importance|reply-to|'
                          r'return-path|message-id|in-reply-to|mime-version|content-type|content-transfer-encoding)\s*[:;.]\s*(.*)$', re.I)

PARTY_FIELDS = {'from', 'to', 'cc', 'bcc'}


def word_key(text):
    return f" {re.sub(r'[^0-9a-z]+', ' ', text.casefold()).strip()} "


def header_block_text(text):
    """The party lines of a document's message headers, as word_key text. A redacted address
    leaves "To:" empty and the source parser read the next line of the body as the recipient
    ("To:" | "come back"): lines after a party field count only when another header field
    closes the block, or when they carry address syntax."""
    lines, out, i = text.split('\n'), [], 0
    while i < len(lines):
        field = HEADER_FIELD.match(lines[i])
        if not field or field[1].casefold() not in PARTY_FIELDS:
            i += 1
            continue
        out.append(field[2])
        following, j, closed = [], i + 1, False
        while j < len(lines) and j - i <= 12:
            if (next_field := HEADER_FIELD.match(lines[j])):
                closed = next_field[1].casefold() not in PARTY_FIELDS or next_field[1].casefold() in ('cc', 'bcc')
                break
            following.append(lines[j].strip())
            j += 1
        previous = field[2].strip()
        for line in following:
            if not (closed or re.search(r'[@<>]', line) or line.startswith(';') or previous.endswith((',', ';'))):
                break
            out.append(line)
            previous = line
        i = j
    return word_key(' '.join(out))

BODY_AFTER_FIELD = re.compile(
    r'^(?:On\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b|'
    r'On\b.*\bwrote\s*:|[>\s]*wrote\s*:|'
    r'The information (?:contained|in this)|This (?:e-?mail|message)|'
    r'Confidential(?:ity)?\b|Disclaimer\b|[-_]{4,})', re.I)


@lru_cache(maxsize=1 << 17)
def header_body_marker(line):
    """Body evidence that does not require every word to be correctly spelled."""
    return bool(BODY_AFTER_FIELD.search(line) or len(line) > 78 or len(line.split()) > 10
                or re.search(r'[!?]|\w[.,;:]\s+(?:[a-z]|I\b)|[a-z]{2}[.]$', line)
                or sentence_text(line))


def body_after_bare_field(text):
    """Word-key text of body lines swallowed by an empty From/To/Cc field.

    Look ahead at most eight physical lines. A following header or address
    confirms recipient continuations; body evidence ends that run even if a
    quoted message supplies more headers later. Keep short recipient lists.
    A spelling also attested in a proper header in this document is protected.
    """
    lines = text.splitlines()
    bad, good = set(), set()
    for i, raw in enumerate(lines):
        field = HEADER_FIELD.match(raw)
        if not field or field[1].casefold() not in PARTY_FIELDS:
            continue
        if field[2].strip():
            good.add(word_key(field[2]))
        bare = not field[2].strip() and field[1].casefold() in {'from', 'to', 'cc'}
        following = []
        for j in range(i + 1, min(i + 9, len(lines))):
            line = lines[j]
            line = line.strip().lstrip('>|').strip()
            if not line:
                continue
            if HEADER_FIELD.match(line) or re.search(r'[@<>]', line) and not BODY_AFTER_FIELD.search(line):
                good.update(word_key(s) for s in following)
                break
            following.append(line)
            if header_body_marker(line):
                next_line = next((s.strip() for s in lines[j + 1:i + 9] if s.strip()), '')
                if len(following) == 1 and HEADER_FIELD.match(next_line) and not BODY_AFTER_FIELD.search(line):
                    good.update(word_key(s) for s in following)
                    break
                if bare:
                    # Two preceding short lines are evidence of a recipient list.
                    start = 0 if len(following) <= 2 else len(following) - 1
                    bad.update(word_key(s) for s in following[start:])
                    # The upstream parser can also pick a wrapped body continuation.
                    for tail in lines[j + 1:i + 9]:
                        tail = tail.strip()
                        if HEADER_FIELD.match(tail):
                            break
                        if tail:
                            bad.add(word_key(tail))
                break
    # Recipient parsing splits a body line at commas/semicolons. Match those
    # fragments as whole words, while preserving spellings attested as headers.
    body = '\n'.join(sorted(bad))
    for key in good:
        body = body.replace(key, ' ')
    return body

SUBJECT_START = re.compile(r'^(?:re|fw|fwd)\s*:', re.I)

HEADER_DATE = re.compile(r'^(?:(?:mon|tue|wed|thu|fri|sat|sun)\w*\b.*\d|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b)', re.I)

PRINTED_FIELD = re.compile(r'^(from|to|cc|bcc|sent|date|subject)\s*[:;.]?$', re.I)

PAGE_FURNITURE = re.compile(r'^(?:EFTA[\d_]|Case\s+\d|Document\s+\d|Filed\s+\d|Page\s+\d+\s+of\s+\d+|Exhibit\s+\w{1,3}\b)', re.I)

BODY_FORM = re.compile(r'^(?:(?:toll[ -]*free|international|local)\s+)?(?:dial[ -]*in\s+(?:number|instructions)|'
                       r'(?:participant|conference|access)\s+(?:code|passcode)|'
                       r'(?:account|routing|aba|fax|telephone|phone|mobile|skype)\s+(?:name|number|no\.?))(?:\s*[:#]|\s*$)', re.I)


@lru_cache(maxsize=1 << 17)
def party_body_marker(line):
    """Clear body/subject evidence, allowing addresses and long lists of short names."""
    if (SUBJECT_START.match(line) or BODY_FORM.match(line) or BODY_AFTER_FIELD.search(line)
            or re.match(r'<[!?]|</?[a-z][\w:-]*(?:\s[^<>]*)?>', line, re.I)):
        return True
    if re.search(r'[@<>]', line) or not header_body_marker(line):
        return False
    parties = parse_recipient(line)
    return len(parties) < 2 or any(header_body_marker(name) for name, _ in parties)


def attest_party(good, line):
    """Protect both a party line and the individual names in its address/list syntax."""
    good.add(word_key(line))
    good.update(word_key(name) for name, _ in parse_recipient(line) if name)


def detached_header_parties(lines):
    """Party values of printed headers whose labels precede the entire value column.

    Require From, Sent/Date and Subject in a run of bare labels, then a date
    in the values. The date separates the sender from the recipient slots;
    a reply/forward prefix explicitly starts the subject (including wraps).
    """
    good, i = set(), 0
    while i < len(lines):
        fields, j = [], i
        while j < len(lines) and (field := PRINTED_FIELD.fullmatch(lines[j])):
            fields.append(field[1].casefold())
            j += 1
        if not {'from', 'subject'} <= set(fields) or not {'sent', 'date'} & set(fields):
            i += 1
            continue
        values = []
        for line in lines[j:j + 12]:
            if HEADER_FIELD.match(line):
                break
            if not PAGE_FURNITURE.match(line):
                values.append(line)
        date = next((n for n, line in enumerate(values) if HEADER_DATE.match(line)), None)
        if date is not None:
            for line in values[:date]:
                attest_party(good, line)
            slots = sum(f in PARTY_FIELDS for f in fields[fields.index('sent' if 'sent' in fields else 'date') + 1:])
            for line in values[date + 1:date + 1 + slots]:
                if party_body_marker(line):
                    break
                attest_party(good, line)
        i = j
    return good


def nonparty_header_text(text):
    """Text after message headers with no attestation in a party slot.

    Unlike the eight-line bare-field check, this covers subjects, attachments,
    printed value columns and the rest of a message body. A closed header or
    address syntax confirms wrapped recipients; body evidence ends their run.
    Preserve short lists after bare fields and spellings attested in any proper
    party header of the document. Without headers, reject only explicit form labels.
    """
    lines = [s.strip().lstrip('>|').strip() for s in text.splitlines() if s.strip()]
    good = detached_header_parties(lines)
    start = next((i for i, line in enumerate(lines) if HEADER_FIELD.match(line)), None)
    if start is None:
        if not good:
            forms = [s for s in lines if BODY_FORM.match(s)]
            return word_key('\n'.join(forms)) if forms else ''
        start = 0
    for i, line in enumerate(lines):
        field = HEADER_FIELD.match(line)
        if not field or field[1].casefold() not in PARTY_FIELDS:
            continue
        value = field[2].strip()
        if value and not party_body_marker(value):
            attest_party(good, value)
        following, closed = [], False
        for tail in lines[i + 1:i + 13]:
            if HEADER_FIELD.match(tail):
                closed = True
                break
            if party_body_marker(tail):
                break
            following.append(tail)
        previous = value
        unclosed_list = (not value and 1 < len(following) <= 3 and all(
            2 <= len(s.split()) <= 4 and title_case(s) and not header_junk_reason(s) for s in following))
        for tail in following:
            if (closed or re.search(r'[@<>]', tail) or tail.startswith(';') or previous.endswith((',', ';'))
                    or unclosed_list or not value and not previous and not header_junk_reason(tail)):
                attest_party(good, tail)
                previous = tail
            else:
                break
    # Reply headers are also real party slots, including a separately printed
    # "wrote:" line. Their message bodies must never become party continuations.
    for party in reply_parties(lines):
        attest_party(good, party)
    forms = [s for s in lines[:start] if BODY_FORM.match(s)]
    return nonparty_values(forms + lines[start:], good)


def reply_parties(lines):
    """Names in date-led quoted replies, with at most three physical lines.

    A date/time is required; ordinary prose beginning with On is not a header.
    A split marker may follow a redacted address, but never intervening body text.
    """
    for i, line in enumerate(lines):
        if not re.match(r'^On\s+\S', line, re.I) or not re.search(r'\d.*\d', line):
            continue
        joined = line
        for tail in lines[i + 1:i + 3]:
            if re.search(r'\bwrote\s*:', joined, re.I):
                break
            if not re.fullmatch(r'[<>\s]*wrote\s*:|[<>\s]+', tail, re.I):
                break
            joined += ' ' + tail
        match = re.match(r'^On\b.*(?:\bat\s+\d[^,]*,|\b[AP]M\s*,?)\s*(.+?)\s*(?:[<>]\s*)?wrote\s*:', joined, re.I)
        if match and not HEADER_FIELD.match(match[1]):
            yield match[1].strip(' <>')


# Syntactic classes, not a list of rejected people. Apply in resolution too so
# older extraction databases retain their nodes but receive a hidden quality.
SALUTATION = re.compile(r'^(?:hello|hey|hi|dear|bonjour|hola|ciao|greetings|'
                         r'good\s+(?:morning|afternoon|evening|day)|morning|'
                         r'best\s+(?:wishes|regards)|kind\s+regards|thanks|thx|thxs|xo|xoxo)\b', re.I)
HEADER_LABEL = re.compile(r'^(?:subject|from|to|cc|bcc|sent|date|importance|'
                          r'returnpath|messageid|inreplyto|mimeversion|contenttype|contenttransferencoding|'
                          r'x(?:eudora|apple|microsoft).*signature|date(?:lastviewed|received))$')
ROLE_LINE = re.compile(r'^(?:(?:assistant|executive|first|senior|vice|managing|group|global|'
                       r'chief|deputy)\s+)*(?:vice\s+president|president|director|manager|'
                       r'legal\s+officer|office\s+manager|associate\s+warden|victim\s+specialist)'
                       r'(?:\s+of\s+(?:operations|properties))?$', re.I)
FORM_LINE = re.compile(r'^(?:file\s*(?:date\s*time\s*utc|hash\s*(?:type|value)|number|size)|'
                       r'(?:bank|guest|formatted|filer)\s+name|'
                       r'(?:flight|order)\s+(?:status|time)|form\s+of\s+payment|'
                       r'airline\s+ticket\s+number|unique\s+identifier|'
                       r'(?:view\s+order\s+summary|email\s+unsubscribe|begin\s+pgp\s+signature))$', re.I)


@lru_cache(maxsize=1 << 17)
def header_junk_reason(label):
    """A fixed reason for unambiguous non-name syntax; None for uncertain labels.

    Keep bare names, unfamiliar acronyms, organizations and shared mailboxes.
    In particular, a dictionary word or a missing surname alone is not junk.
    """
    value = label.strip(' \t\r\n.,;:<>\"')
    compact = re.sub(r'[^a-z]', '', value.casefold())
    if HEADER_LABEL.fullmatch(compact) or re.match(r'^importance\s*[: ]\s*(?:normal|low|high)$', value, re.I):
        return 'header_field'
    if SALUTATION.match(value):
        return 'salutation_or_signoff'
    if ROLE_LINE.fullmatch(value):
        return 'role_title'
    if FORM_LINE.fullmatch(value) or BODY_FORM.match(value):
        return 'form_field'
    if re.match(r'^(?:sent\s+from\s+my|depuis\s+mon|gesendet\s+von\s+meinem)\b', value, re.I):
        return 'device_signature'
    if re.fullmatch(r'(?:monda|tues(?:da)?|wednesda|thur(?:s(?:da)?)?|frida|saturda|sunda)'
                    r'(?:\s+(?:jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*))?'
                    r'|(?:jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)', value, re.I):
        return 'date_fragment'
    if re.match(r'^(?:x[ms][dil]|xmlns)\s*=|^cipher\s*=', value, re.I) or value.casefold() in {'arial', 'tahoma', 'helvetica neue'}:
        return 'markup_or_font'
    if re.fullmatch(r'(?:i?mailto?[aeinl]?|mail\s+(?:com|corn)(?:\s+and)?)', value, re.I):
        return 'address_delimiter'
    return None


def nonparty_values(lines, good):
    """Remove attested party spellings in a stable order, retaining wrapped body text."""
    body = word_key('\n'.join(lines))
    return remove_party_keys(body, sorted(good, key=lambda s: (-len(s), s)))


PARTY_REPLACE_SCAN_LIMIT = 2_000_000


def remove_party_keys(body, keys):
    """Apply ordered replacements without scanning a long document per party.

    word_key produces one space between words. Index starting words and unlink
    each matching run, so later keys can also match across earlier removals.
    str.replace consumes both boundary spaces: skip an immediately adjacent
    occurrence of the same key, even though it shares no name words.
    """
    if len(body) * len(keys) < PARTY_REPLACE_SCAN_LIMIT:
        for key in keys:
            body = body.replace(key, ' ')
        return body
    words = body.split()
    first_words = {key.split()[0] for key in keys if key.strip()}
    positions = defaultdict(list)
    for i, word in enumerate(words):
        if word in first_words:
            positions[word].append(i)
    n = len(words)
    after = array('i', range(1, n + 1))
    before = array('i', range(-1, n - 1))
    head = 0
    for key in keys:
        parts = key.split()
        if not parts:
            # Empty word_key is two spaces; normalized nonempty body has none.
            if not words:
                return ' '
            continue
        blocked = -1
        for start in positions.get(parts[0], ()):
            if after[start] < 0 or start == blocked:
                continue
            end = start
            for part in parts[1:]:
                end = after[end]
                if end == n or words[end] != part:
                    break
            else:
                previous, following = before[start], after[end]
                if previous < 0:
                    head = following
                else:
                    after[previous] = following
                if following < n:
                    before[following] = previous
                blocked = following
                current = start
                while current != following:
                    following_word = after[current]
                    after[current] = -1
                    current = following_word
    kept, current = [], head
    while current < n:
        kept.append(words[current])
        current = after[current]
    return ' ' + ' '.join(kept) + ' ' if kept else (' ' if words else body)
