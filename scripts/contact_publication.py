"""Fail-closed publication policy for contact values (GDPR review 2026-11-10).

Only attested organisation contacts and contacts of people actually named in
this public viewer may be plaintext. All other labels use a separate local
HMAC key. No key or private lookup is part of a public artifact.
"""
from collections import Counter
from contextlib import closing
from functools import lru_cache
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
CONTACT_KINDS = ('email_', 'phone_')
EMAIL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+'-]{0,63}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,4}\.[A-Za-z]{2,24}")
# Include the address forms OCR leaves in ordinary metadata, not only @.
OCR_EMAIL = re.compile(
    r"(?<![\w.%+'-])([A-Za-z0-9][A-Za-z0-9_%+'-]{0,63}(?:[ \t]*\.[ \t\r\n]*[A-Za-z0-9_%+'-]{1,63}){0,8}"
    r"[ \t\r\n]*(?:@|©|®|\(a\))[ \t\r\n]*"
    r"[A-Za-z0-9-]{1,63}(?:[ \t\r\n]*\.[ \t\r\n]*[A-Za-z0-9-]{1,63}){1,5})(?![\w-])",re.I)
# Bare digits require a telephone cue, so document IDs remain intact.
SPACE = r'(?:\s|\\+[nrt])'
SEP = r'(?:[\s()./-]|\\+[nrt])'
EXT = rf'(?:{SPACE}*(?:ext(?:ension)?\.?|x|#){SPACE}*\d{{1,8}})?'
NANP = (rf'(?<!\w)(?:\+1{SEP}*|1{SEP}+)?'
        rf'(?:\(?[2-9]\d{{2}}\){SEP}*|[2-9]\d{{2}}{SEP}+)'
        rf'\d{{3}}{SEP}*\d{{4}}{EXT}(?!\d)')
PHONE = re.compile(rf'{NANP}|(?<!\w)\+[1-9](?:{SEP}*\d){{6,14}}{EXT}(?!\d)'
                   rf'|(?<!\w)0\d{{1,4}}(?:{SEP}+\d{{1,5}}){{2,5}}{EXT}(?!\d)', re.I)
PHONE_CUE = re.compile(
    r'''(?<!\w)(?!(?-i:phone:[0-9a-f]{12})(?!\w))'''
    r'''(?P<prefix>(?:(?:home|work|office|cell)[ _-]*)?(?:tel(?:ephone)?|phone|fax|mobile|switchboard|cell)'''
    r'''(?:[ \t_-]*(?:no\.?|number))?(?![a-z_])'''
    r'''(?:[ \t:;=()"'\\./-]|&quot;|&apos;|\\+[nrt]){0,16})'''
    rf'(?P<number>\+?[\d(](?:{SEP}|\d){{6,26}}\d){EXT}(?!\d)',re.I)


def phone_text(value):
    """Do not treat a short date as a national phone number."""
    digits=re.sub(r'\D','',re.split(r'(?:ext(?:ension)?\.?|x|#)',value,flags=re.I)[0])
    return 7<=len(digits)<=15 and (not value.startswith('0') or len(digits) in (10,11))


def role_contacts():
    """Explicit published service contacts (including this site's GDPR inbox)."""
    path = ROOT / 'config/public_contacts.json'
    return set(json.loads(path.read_text()).get('organisational_role_contacts', {})) if path.exists() else set()


def publication_key(private=None):
    if private is None:
        import viewer_secret
        private = viewer_secret.PRIVATE
    path = Path(private) / 'public_contact_hmac.key'
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(os.urandom(32))
    path.chmod(0o600)
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError('Public contact HMAC key must contain 32 bytes')
    return key


def hash_label(value, kind, key):
    from contact_resolution import email_key, phone_key
    normalized = '@'.join(email_key(value)) if kind == 'email' else phone_key(value)
    return kind + ':' + hmac.new(key, (kind + '\0' + normalized).encode(), hashlib.sha256).hexdigest()[:12]


def named_public_ids(entities, labels, verdicts):
    """Use the names decision layer's result; never make new public-name decisions."""
    import public_names
    shown = public_names.shown_names(entities, labels, verdicts)
    out = set()
    for e in entities:
        i = str(e['id'])
        if e.get('entity_type') not in public_names.PEOPLE or public_names.withheld((verdicts or {}).get(i)):
            continue
        # apply() has already withheld private people; accept actual displayed
        # names or the shown_names reveal of a reviewed pseudonymous person.
        if i in shown or not public_names.PSEUDONYM.match(str(e['name'])) and not re.match(r'^Person [0-9a-f]+$', e['name']):
            out.add(i)
    return out


def publish(entities, values, *, named_ids=(), denied=(), out=None, private=None, kinds=None):
    """Return (id -> safe value/hash, per-class counts). Missing evidence hashes.

    An organisation domain is eligible even when its employee has a person
    canonical. Personal providers and unresolved domains are never company
    evidence. The one config switch controls the named-person class only.
    """
    out = Path(out or ROOT / 'output')
    key = publication_key(private)
    policy = ROOT / 'config/public_contacts.json'
    allow_people = json.loads(policy.read_text()).get('named_public_people',True) if policy.exists() else True
    explicit_roles = {v.casefold() for v in role_contacts()}
    named_ids, denied = set(map(str,named_ids)), set(map(str,denied))
    owners, organisations, person_evidence = {}, set(), {}
    path = out / 'contact_graph.db'
    if path.exists():
        with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='resolutions'").fetchone():
                for i,c,org in db.execute('SELECT member,canonical,organisation FROM resolutions'):
                    owners[i]=c
                    if org: organisations.add(i)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='person_contact_evidence'").fetchone():
                person_evidence = dict(db.execute('SELECT member,owner FROM person_contact_evidence'))
    # A contact folded by the existing header resolver belongs to that node,
    # even in small fixtures or installations without a full-text database.
    for e in entities:
        for m in e.get('merged_identities',[]):
            if str(m.get('id','')).startswith(CONTACT_KINDS):
                owners.setdefault(str(m['id']),str(e['id']))
    def kind_of(i):
        return (kinds or {}).get(i) or (i.split('_',1)[0] if i.startswith(CONTACT_KINDS) else None)
    denied_hashes={hash_label(values[i],kind_of(i),key) for i in denied if i in values and kind_of(i) in {'email','phone'}}
    result, classes = {}, Counter()
    for i,value in sorted(values.items()):
        kind=kind_of(i)
        if kind not in {'email','phone'}:
            continue
        category = 'hashed'
        # NER can retain a whole OCR span under an email_ ID. Approving the
        # final domain must never publish another address embedded in that
        # span. Only one syntactically complete contact can be plaintext.
        personal_mailbox = True
        if kind == 'email':
            from build_communications import MAIL_SERVER, non_mailbox_reason, AUTOMATED_LOCAL
            domain = value.rsplit('@',1)[-1].casefold()
            standalone = (EMAIL.fullmatch(value) is not None and not non_mailbox_reason(value)
                          and not MAIL_SERVER.match(domain)
                          # Marketplace aliases route to private sellers;
                          # the service domain does not make them staff.
                          and not domain.endswith('.craigslist.org'))
            local = value.split('@',1)[0].casefold()
            personal_mailbox = not (AUTOMATED_LOCAL.match(local) or re.search(r'assistant|office|staff|admin|secretary',local))
        else:
            from contact_resolution import phone_key
            standalone = re.fullmatch(r'\+?[0-9]{7,15}(?:x[0-9]{1,8})?', phone_key(value)) is not None
        digest=hash_label(value,kind,key)
        if i not in denied and digest not in denied_hashes and standalone:
            if i in organisations or value.casefold() in explicit_roles:
                category = 'plaintext_org'
            elif (allow_people and personal_mailbox and owners.get(i,i) in named_ids
                  and person_evidence.get(i) == owners.get(i,i)):
                category = 'plaintext_named_public_figure'
        result[i] = digest if category == 'hashed' else value
        classes[category] += 1
    return result, {c:classes[c] for c in ('plaintext_org','plaintext_named_public_figure','hashed')}


def text_scrubber(public_values, *, key=None):
    """Last boundary for metadata, aliases, tooltips and search strings.

    Raw contacts not explicitly approved above are hashed even when incidental
    prose contains them. Encrypt the private payload before using this walker.
    """
    key = key or publication_key()
    safe = {v.casefold() for v in (*public_values.values(), *role_contacts()) if not re.match(r'^(email|phone):[a-f0-9]{12}$',v)}
    def replacement(match,kind):
        value=match.group()
        if kind=='phone' and not phone_text(value): return value
        return value if value.casefold() in safe else hash_label(value,kind,key)
    def phone_cue(match):
        prefix,number=match['prefix'],match['number']
        if not phone_text(number) or number.casefold() in safe: return match[0]
        masked=hash_label(number,'phone',key)
        # Preserve numeric JSON properties, nested JSON escaping, CSV doubled
        # quotes and XML entities when metadata is serialized.
        quoted=re.search(r'(?P<escape>\\*)(?P<quote>"+|&quot;|&apos;)\s*:\s*$',prefix)
        if quoted:
            quote=quoted['escape']+quoted['quote']
            masked=quote+masked+quote
        return prefix+masked
    @lru_cache(maxsize=32768)
    def cached_text(value):
        original=value
        # Metadata cells and release files can themselves be JSON. Scrub
        # their strings/telephone fields by type, preserving numeric layout
        # coordinates such as 600.5671875 (which resemble dotted phones).
        if value.lstrip().startswith(('{','[')):
            try:
                structured=json.loads(value)
            except (ValueError,RecursionError):
                pass
            else:
                clean=walk(structured)
                return original if clean==structured else json.dumps(clean,ensure_ascii=False)
        if r'\u00' in value.casefold():
            value=re.sub(r'\\+u00(a9|ae|40|a0|20)',lambda m:{'a9':'©','ae':'®','40':'@','a0':'\u00a0','20':' '}[m[1].lower()],value,flags=re.I)
        decoded=value
        # Most exported strings are IDs, field names or official URLs. Avoid
        # expensive address regex backtracking on long strings without @.
        # Chained OCR spans can leave "<hash>©gmail.com" after one pass.
        # Each replacement removes an address marker; stop at a fixed point.
        while any(marker in value for marker in ('@','©','®','(a)','(A)')):
            before=value
            if '@' in value:
                value=EMAIL.sub(lambda m:replacement(m,'email'),value)
            value=OCR_EMAIL.sub(lambda m:replacement(m,'email'),value)
            if value==before: break
        if '+' in value or re.search(r'\d[\s()./-]\d',value):
            value=PHONE.sub(lambda m:replacement(m,'phone'),value)
        if re.search(r'tel|phone|fax|mobile|switchboard|cell',value,re.I):
            value=PHONE_CUE.sub(phone_cue,value)
        return original if value==decoded else value
    def text(value):
        # Release/audit callers also pass whole JSON files and 1 MB chunks.
        # Retaining both originals and scrubbed copies in the string cache
        # can exhaust the 15 GB machine. Cache only small repeated cells.
        return cached_text(value) if len(value)<=16384 else cached_text.__wrapped__(value)
    def walk(value):
        if isinstance(value,str): return text(value)
        if isinstance(value,list): return [walk(v) for v in value]
        if isinstance(value,dict):
            out={}
            for k,v in value.items():
                if (isinstance(k,str) and re.sub(r'[\W_]+','',k).casefold() in {
                        'tel','telno','telnumber','telephone','telephoneno','telephonenumber','phone','phoneno','phonenumber',
                        'fax','faxno','faxnumber','mobile','mobileno','mobilenumber','cell','cellphone','cellnumber','switchboard',
                        'homephone','officephone','workphone'}
                        and isinstance(v,(str,int)) and str(v).isdigit() and 7<=len(str(v))<=15):
                    v=hash_label(str(v),'phone',key)
                out[text(k) if isinstance(k,str) else k]=walk(v)
            return out
        return value
    return walk
