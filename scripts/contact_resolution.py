"""Build-only contact ownership. Raw values stay private; all decisions use IDs.

Original contact nodes and mention rows are retained. Canonicals are a display
fold, with explicit rules; an ambiguous name/domain is never an owner.
"""
from collections import Counter, defaultdict
import json
import re
import sqlite3
from contextlib import closing

from bounded_json import items
from build_communications import ocr_repair, WEBMAIL_DOMAINS, label_quality, never_merge_pairs, nickname_root, AUTOMATED_LOCAL, GENERIC_TLDS, edit_distance
from build_contacts import normalize_contact
from normalize_graph import name_key


def email_key(value):
    value = re.sub(r'\s+', '', value.casefold()).replace('©', '@').replace('(a)', '@').replace('®', '@')
    value = re.sub(r'^(?:rnailto|rnaitto|rnaiito|rnalito|mailt0)[:;]?', '', value)
    if '@' not in value:
        from entity_resolution import email_address
        value = email_address(value) or value
    if '@' not in value:
        return value, ''
    return ocr_repair(value)


def phone_key(value):
    # Extensions identify a line: preserve them and never fold two extensions
    # or an extension into a switchboard without separate ownership evidence.
    value=re.sub(r'\\+[nrt]',' ',value).replace('/',' ')
    value=re.sub(r'^(\+\d{1,3})\s*\(0\)',r'\1',value.strip())
    parts = re.split(r'\s*(?:ext(?:ension)?\.?|x|#)\s*(?=\d)', value, maxsplit=1, flags=re.I)
    base = normalize_contact(parts[0], 'phone')
    return (base or parts[0]) + ('x' + re.sub(r'\D', '', parts[1]) if len(parts) == 2 else '')


def skeleton(value):
    return re.sub(r'[^a-z0-9]', '', value.casefold().replace('rn', 'm').translate(str.maketrans({'l': 'i', '1': 'i', '0': 'o'})))


def registered_domain(domain):
    parts = domain.split('.')
    keep = 3 if len(parts) > 2 and parts[-2] in {'co', 'com', 'org', 'ac', 'gov', 'edu', 'net'} and len(parts[-1]) == 2 else 2
    return '.'.join(parts[-keep:])


def corporate(domain):
    if not re.fullmatch(r'(?:[a-z0-9-]+\.)+[a-z]{2,}',domain):
        return False
    root = registered_domain(domain)
    return root not in WEBMAIL_DOMAINS and root.split('.')[0] not in {
        'gmail', 'googlemail', 'yahoo', 'hotmail', 'aol', 'icloud', 'outlook', 'comcast',
        'verizon', 'msn', 'mac', 'me', 'att', 'earthlink', 'live', 'mail', 'email', 'protonmail', 'proton', 'gmx'}


def broken_tld_variants(values, counts):
    """Repair a rare broken TLD beside the same independently known mailbox.

    A real .com/.net or country-domain pair never merges on frequency. The
    mailbox and domain stem must agree, and all dominant candidates must name
    the same repaired domain. This also reaches header-only spellings.
    """
    groups = defaultdict(list)
    for i,spec in sorted(values.items()):
        if spec['kind'] != 'email': continue
        local,domain = email_key(spec['value'])
        if '.' not in domain: continue
        base,tld = domain.rsplit('.',1)
        groups[local,skeleton(base)].append((i,domain,tld))
    out = {}
    for group in groups.values():
        for i,domain,tld in group:
            n=counts.get(i,1)
            if n>3 or tld in GENERIC_TLDS or len(tld)==2: continue
            hits=[(j,target) for j,target,ending in group if j!=i and
                  (ending in GENERIC_TLDS or len(ending)==2) and counts.get(j,1)>=10*max(n,1)
                  and (edit_distance(tld,ending,2)<=(2 if len(ending)==2 or len(tld)>=4 else 1)
                       or tld.startswith(ending) and len(tld)-len(ending)<=3)]
            if len({target for _,target in hits})==1:
                out[i]=min(hits,key=lambda x:(-counts.get(x[0],1),x[0]))
    return out


def resolve_contacts(db, lookup, entities, identity_map, private, protected, root):
    """Add a transitive, guarded resolutions table and update nodes.canonical.

    Signature names need >=3 documents and 95% agreement. Initial+surname
    needs a known domain or signature; full names must be unique. A corporate
    domain uses an existing organisation, never an invented company node.
    """
    from entity_resolution import company_key
    nodes = {str(e['id']): e for e in entities}
    company_file = root / 'output/company_nodes.json'
    if company_file.exists():
        nodes.update((str(e['id']), e) for e in items(company_file))
    comm_file = root / 'output/communication_nodes.json'
    if comm_file.exists():
        nodes.update((str(e['id']), e) for e in items(comm_file))
    # The entity layer already contains reviewed agencies, universities and
    # brands. Omitting it made their domains impossible to resolve here.
    entity_file = root / 'output/entity_nodes.json'
    if entity_file.exists():
        for e in items(entity_file):
            if e['entity_type'] in {'organization', 'shell_company'}:
                nodes.setdefault(str(e['id']), e)
    values = {i: {**v, 'kind': i.split('_',1)[0] if i.startswith(('email_','phone_')) else v['kind']}
              for i,v in {**lookup, **private}.items()
              if i.startswith(('email_','phone_')) or v.get('kind') in {'email','phone'}}
    mapping, rules, orgs = {}, {}, {}
    alias_rules = dict(db.execute('SELECT member,rule FROM aliases')) if db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='aliases'").fetchone() else {}
    for i, canonical in db.execute('SELECT id,canonical FROM nodes WHERE id != canonical'):
        mapping[i] = str(canonical)
        rules[i] = 'header_identity' if i in identity_map else alias_rules.get(i,'reviewed_contact_owner')
    for i, spec in identity_map.items():
        target = str(spec['canonical']).removeprefix('kg:')
        if i != target:
            mapping[i], rules[i] = target, 'header_identity'
    protected_ids = {str(e['id']) for e in entities if e['entity_type'] == 'survivor' or name_key(e['name']) in protected}
    protected_ids |= {i for i,v in lookup.items() if v.get('kind') == 'participant' and name_key(v['value']) in protected}
    def final(i):
        i = str(i).removeprefix('kg:')
        seen = set()
        while i in mapping and i not in seen:
            seen.add(i)
            i = mapping[i]
        return i
    blocked = [(str(a).removeprefix('kg:'), str(b).removeprefix('kg:')) for a,b in never_merge_pairs()]
    def merge(i, target, rule, override=False):
        target = final(target)
        if (final(i) != i and not override or i == target or final(i) == target
                or target in protected_ids or i in protected_ids or final(i) in protected_ids):
            return False
        previous, previous_rule = mapping.get(i), rules.get(i)
        mapping[i], rules[i] = target, rule
        if any(final(a) == final(b) for a,b in blocked):
            if previous is None:
                del mapping[i], rules[i]
            else:
                mapping[i], rules[i] = previous, previous_rule
            return False
        return True

    # Join exact normalised values across header, NER and full-text namespaces.
    # A known owner wins; conflicting owners stay separate. Resolve through
    # existing aliases first, so selecting a representative cannot form a cycle.
    same = defaultdict(list)
    for i,v in values.items():
        normalized = email_key(v['value']) if v['kind']=='email' else phone_key(v['value'])
        same[v['kind'],normalized].append(i)
    for (kind,_),ids in same.items():
        if len(ids)<2: continue
        finals={final(i) for i in ids}
        owners={i for i in finals if i.startswith('participant_') or nodes.get(i,{}).get('entity_type') in {'person','organization','shell_company'}}
        if len(owners)>1: continue
        candidates=owners or finals
        target=min(candidates,key=lambda i:(not i.startswith(kind+'_'),-values.get(i,{}).get('occurrences',0),i))
        for i in sorted(ids): merge(i,target,'same_contact_value')
    frequencies={i:max(1,v.get('occurrences',0)) for i,v in values.items()}
    if 'documents' in {r[1] for r in db.execute('PRAGMA table_info(nodes)')}:
        for i,n in db.execute('SELECT id,documents FROM nodes'):
            frequencies[i]=max(frequencies.get(i,1),n or 0)
    repaired_domains={}
    for i,(target,domain) in broken_tld_variants(values,frequencies).items():
        if merge(i,target,'ocr_tld_same_mailbox') or final(i)==final(target):
            repaired_domains[i]=domain
    # Full names and nicknames are indexed by final identity, so many OCR
    # aliases of one person do not make an otherwise unique match ambiguous.
    full, initial, nick = defaultdict(set), defaultdict(set), defaultdict(set)
    names = {i:e['name'] for i,e in nodes.items() if e['entity_type'] in {'person','participant'}}
    for i,v in lookup.items():
        if v.get('kind') == 'participant' and (v.get('occurrences', 0) >= 3 or final(i) in nodes):
            # communication_nodes carries HeaderLabel_<id>, not the name.
            # Index the private spelling, as for the full-text namespace.
            names[i] = v['value']
    names.update({i:v['value'] for i,v in private.items() if v.get('kind') == 'participant'})
    for i,name in names.items():
        if name_key(name) in protected or final(i) in protected_ids:
            continue
        words = re.findall(r'[a-z]+', name.casefold())
        if label_quality(name) != 'ok' or not 2 <= len(words) <= 4 or min(len(words[0]),len(words[-1])) < 2:
            continue
        target = final(i)
        full[skeleton(words[0] + words[-1])].add(target)
        full[skeleton(''.join(words))].add(target)
        if len(words[-1]) >= 5:
            initial[skeleton(words[0][0] + words[-1])].add(target)
        for word in (words[0], words[-1]):
            nick[nickname_root(word)].add(target)
    # Restrict one-word nickname completion to unique names actually written
    # beside the same contact; a bare first name alone is not evidence.
    signatures = defaultdict(Counter)
    direct_headers = defaultdict(Counter)
    for value, person, documents in db.execute('SELECT value,identity,documents FROM names'):
        signatures[final(value)][final(person)] += documents
    # Header display names are also co-occurrence evidence, including bare
    # nicknames which the full-text signature parser deliberately does not emit.
    comm_db = root / 'output/communication_graph.db'
    if comm_db.exists():
        with closing(sqlite3.connect(f'file:{comm_db}?mode=ro', uri=True)) as headers:
            for a,b,n in headers.execute("SELECT source_id,target_id,count(DISTINCT efta_number) FROM evidence "
                                         "WHERE relationship_type='header_label_association' GROUP BY source_id,target_id"):
                contact, person = (a,b) if a in values else (b,a)
                if contact not in values:
                    continue
                direct_headers[contact][str(person).removeprefix('kg:')] += n
                if n < 3: continue
                owner, person = final(contact), final(person)
                signatures[owner][person] += n
                if owner in names:
                    signatures[owner][owner] += n
    nickname_targets = defaultdict(set)
    for value, counts in signatures.items():
        for person in counts:
            label = names.get(person, lookup.get(person, private.get(person, {})).get('value',''))
            word = name_key(label)
            candidates = nick.get(nickname_root(word), set()) if len(word.split()) == 1 else set()
            nickname_targets[person].update(candidates & set(counts))
    for person, candidates in sorted(nickname_targets.items()):
        if len(candidates) == 1:
            merge(person, next(iter(candidates)), 'nickname_signature_full_name')
    known_domains = defaultdict(set)
    for i,v in values.items():
        if v['kind'] == 'email' and final(i) != i:
            known_domains[final(i)].add(email_key(v['value'])[1])
    # A shared domain does not authorize a person match. Try person evidence
    # first; unresolved corporate mailboxes may then fold into the domain org.
    for i,v in sorted(values.items()):
        if final(i) != i:
            continue
        counts = Counter()
        for person,n in signatures.get(i, {}).items():
            counts[final(person)] += n
        best = sorted(counts, key=lambda x:(-counts[x],x))
        if best and counts[best[0]] >= 3 and counts[best[0]] >= .95 * sum(counts.values()):
            target = best[0]
            if target in names and len(name_key(names[target]).split()) >= 2 and label_quality(names[target]) == 'ok':
                merge(i,target,'signature_consensus')
        if final(i) != i or v['kind'] != 'email':
            continue
        local,domain = email_key(v['value'])
        candidates = full.get(skeleton(local), set())
        rule = 'unique_full_name_localpart'
        if len(candidates) != 1:
            candidates = {p for p in initial.get(skeleton(local),set()) if domain in known_domains[p] or p in counts}
            rule = 'initial_surname_known_domain'
        if len(candidates) == 1:
            target = next(iter(candidates))
            if not counts or set(counts) == {target}:
                merge(i,target,rule)
    brands, domains, reviewed_domains = defaultdict(set), defaultdict(set), {}
    # Reviewed domain aliases cover abbreviations (db.com, aa.com) which
    # cannot be derived safely from an organisation's written name.
    policy = root / 'config/public_contacts.json'
    if policy.exists():
        for domain, spec in json.loads(policy.read_text()).get('organisation_domains', {}).items():
            target = str(spec['canonical'])
            if (target in nodes and nodes[target]['entity_type'] in {'organization','shell_company'}
                    and corporate(domain) and spec.get('document')):
                domains[registered_domain(domain)].add(target)
                reviewed_domains[registered_domain(domain)] = {target}
    gazetteer = root / 'config/entity_gazetteer.json'
    aliases = json.loads(gazetteer.read_text()) if gazetteer.exists() else {}
    for i,e in nodes.items():
        if e['entity_type'] not in {'organization','shell_company'} or e.get('label_quality') == 'not_a_name':
            continue
        extra = aliases.get(e['name'], {}).get('aliases', [])
        for name in [e['name'],*e.get('original_names',[]),*extra]:
            brand = re.sub(r'[^a-z0-9]','',company_key(name))
            if len(brand) >= 4:
                brands[brand].add(i)
            # Some domains retain the legal form (elysllc.com). Removing it
            # from both available keys made these exact matches disappear.
            legal_brand = re.sub(r'[^a-z0-9]','',name.casefold())
            if len(legal_brand) >= 4:
                brands[legal_brand].add(i)
        meta = e.get('metadata') or {}
        if isinstance(meta,str):
            try: meta=json.loads(meta)
            except ValueError: meta={}
        for field in ('domain','website','url'):
            host = re.sub(r'^https?://|^www\.', '', str(meta.get(field,''))).split('/')[0].casefold()
            if re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}',host):
                domains[registered_domain(host)].add(i)
    for i,v in values.items():
        if v['kind'] == 'email':
            local,domain=email_key(v['value'])
            target=final(i)
            if target in nodes and nodes[target]['entity_type'] in {'organization','shell_company'} and corporate(domain):
                domains[registered_domain(domain)].add(target)
    def contact_organisation(identity):
        return (identity.startswith(('email_','phone_','participant_'))
                and nodes.get(identity,{}).get('entity_type') in {'organization','shell_company'})
    owner_contacts=defaultdict(set)
    for i,v in values.items():
        if v['kind']=='email' and contact_organisation(final(i)):
            owner_contacts[final(i)].add(i)
    for i,v in sorted(values.items()):
        if v['kind'] != 'email':
            continue
        local,domain = email_key(v['value']); domain=registered_domain(repaired_domains.get(i,domain))
        if not corporate(domain):
            continue
        domain_candidates=domains.get(domain,set())
        brand_candidates=brands.get(re.sub(r'[^a-z0-9]','',domain.split('.')[0]),set())
        # Header role mailboxes can themselves be typed as organisations.
        # Prefer a unique, fully named company over that contact surrogate;
        # conflicting named domain/brand owners remain ambiguous.
        named={final(c) for c in domain_candidates | brand_candidates if not contact_organisation(c)}
        candidates=reviewed_domains.get(domain) or named or domain_candidates or brand_candidates
        if len(candidates) == 1:
            target=next(iter(candidates)); orgs[i]=target
            role = bool(AUTOMATED_LOCAL.match(local) or local.split('+',1)[0] in {'press','office'})
            # Defer folding the surrogate's own ID until all its mailboxes
            # agree, so an agency/shared contact cannot merge two businesses.
            if not contact_organisation(i):
                merge(i,target,'organisation_role' if role else 'organisation_domain',
                      override=role or contact_organisation(final(i)))
    for owner,members in sorted(owner_contacts.items()):
        proposed={orgs.get(i) for i in members}
        if len(proposed)==1 and None not in proposed:
            target=next(iter(proposed))
            if not contact_organisation(target):
                merge(owner,target,'organisation_domain_owner')
    orgs={i:final(owner) for i,owner in orgs.items()}
    # Strong same-page evidence for main/switchboard numbers only. Office,
    # mobile, home, direct, fax and extension lines stay private by default.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='mentions'").fetchone():
        phone_org=defaultdict(Counter)
        page_org=defaultdict(set)
        for i,efta,page in db.execute("SELECT m.id,m.efta,m.page FROM mentions m JOIN nodes n ON n.id=m.id WHERE n.kind='email'"):
            if i in orgs: page_org[efta,page].add(orgs[i])
        totals=Counter()
        for i,efta,page,label in db.execute("SELECT m.id,m.efta,m.page,m.label FROM mentions m JOIN nodes n ON n.id=m.id WHERE n.kind='phone'"):
            totals[i]+=1
            if label in {'main','switchboard'} and 'x' not in values.get(i,{}).get('value','') and len(page_org[efta,page]) == 1:
                phone_org[i][next(iter(page_org[efta,page]))]+=1
        for i,counts in phone_org.items():
            org,n=counts.most_common(1)[0]
            if n >= 3 and n >= .95*totals[i] and merge(i,org,'organisation_switchboard'):
                orgs[i]=org
    db.execute('CREATE TABLE resolutions(member TEXT PRIMARY KEY, canonical TEXT, rule TEXT, organisation TEXT)')
    # Store header-only contacts too: publication and viewer folds must use the
    # same evidence even when an address never occurs in full-text extraction.
    all_ids=set(values) | {i for i in mapping if i.startswith('participant_')}
    rows=[(i,final(i),rules.get(i,'unresolved'),orgs.get(i)) for i in sorted(all_ids)]
    db.executemany('INSERT INTO resolutions VALUES (?,?,?,?)',rows)
    # Public-name eligibility is separate from merging: a weak OCR or
    # shared-mailbox merge must not authorize revealing a staff address.
    # Compare the ORIGINAL header label with the owner's actual name, before
    # following merges; co-recipients and assistants cannot attest the owner.
    def person_name(identity):
        return names.get(identity, '')
    def same_person(a,b):
        a,b = re.findall(r'[a-z]+', a.casefold()), re.findall(r'[a-z]+', b.casefold())
        return (len(a)>=2 and len(b)>=2 and skeleton(a[-1])==skeleton(b[-1])
                and nickname_root(a[0])==nickname_root(b[0]))
    attested = {}
    # A parser can associate a full name with a suffix of an OCR-split
    # mailbox hundreds of times. Repetition alone is not independent proof:
    # compare with that owner's strongest mailbox on the same domain.
    strongest_header = defaultdict(int)
    header_mailboxes = defaultdict(dict)
    header_domains = defaultdict(dict)
    for i, counts in direct_headers.items():
        if i not in values or values[i]['kind'] != 'email': continue
        c = final(i)
        matching = sum(n for p,n in counts.items() if same_person(person_name(p),person_name(c)))
        local,domain = email_key(values[i]['value'])
        strongest_header[c,domain] = max(strongest_header[c,domain],matching)
        header_mailboxes[c,domain][local] = max(header_mailboxes[c,domain].get(local,0),matching)
        # Retain literal domains here: rn/m normalization can erase the
        # independently observed spelling needed to detect a bad variant.
        raw_domain = values[i]['value'].casefold().rsplit('@',1)[-1]
        header_domains[c,local][raw_domain] = max(header_domains[c,local].get(raw_domain,0),matching)
    reviewed = root / 'config/contact_owners.json'
    reviewed_ids = set(json.loads(reviewed.read_text()).get('owners', {})) if reviewed.exists() else set()
    for i,c,rule,_ in rows:
        name = person_name(c)
        if not name or label_quality(name) != 'ok': continue
        counts = direct_headers.get(i, {})
        matching = sum(n for p,n in counts.items() if same_person(person_name(p),name))
        if i in values and values[i]['kind']=='email':
            raw = values[i]['value'].casefold()
            local,domain = email_key(raw)
            # Repairing a domain is useful for folding; it does not prove
            # ownership of a different literal domain in a public export.
            if (raw.count('@') != 1 or raw.rsplit('@',1)[-1] != domain
                    or raw.split('@',1)[0].replace('.','') != local):
                continue
            if matching < .05*strongest_header[c,domain]:
                continue
            if any(n>matching and (skeleton(local)==skeleton(other) or edit_distance(local,other,2)<=2)
                   for other,n in header_mailboxes[c,domain].items() if other!=local):
                continue
            if any(n and len(other)>len(local) and other.endswith(local)
                   for other,n in header_mailboxes[c,domain].items()):
                continue
            if domain not in WEBMAIL_DOMAINS and any(n and (skeleton(domain)==skeleton(other) or edit_distance(domain,other,2)<=2)
                   for other,n in header_domains[c,local].items() if other!=domain):
                continue
            words = re.findall(r'[a-z]+',name.casefold())
            forms = {words[0]+words[-1],words[0][0]+words[-1]} if len(words)>=2 else set()
            if any(local!=form and skeleton(local)==skeleton(form) for form in forms):
                continue
            if (len(words)>=2 and len(local)>1 and local[0]!=words[0][0]
                    and skeleton(local[0])==skeleton(words[0][0]) and local[1]==words[-1][0]):
                continue
        if matching >= 3 and matching >= .95*sum(counts.values()):
            attested[i] = (c,'direct_header_name')
        elif i in reviewed_ids:
            attested[i] = (c,rule)
    # Header metadata can be linked to the wrong source/name (including an
    # unrelated person's identical full name). Require the literal mailbox
    # in linked corpus evidence too; masked pages cannot attest an address.
    import corpus
    try:
        source = corpus.connect(root/'data/raw/full_text_corpus.db')
    except sqlite3.OperationalError:
        source = None
    headers = sqlite3.connect(f'file:{comm_db}?mode=ro',uri=True) if comm_db.exists() else None
    try:
        for i in list(attested):
            if i not in values or values[i]['kind']!='email': continue
            pattern = re.compile(r"(?<![\w.%+'-])"+re.escape(values[i]['value']).replace('@',r'[@©®]')+r"(?![\w.-])",re.I)
            pages = list(db.execute('SELECT efta,page FROM mentions WHERE id=? ORDER BY header DESC,efta,page LIMIT 10',(i,)))
            found = False
            if source is not None:
                for doc,page in pages:
                    row = source.execute('SELECT text FROM pages WHERE doc_id=? AND page=?',(doc,page)).fetchone()
                    if row and pattern.search(row[0] or ''):
                        found = True; break
                if not found and headers is not None:
                    docs = headers.execute("SELECT DISTINCT efta_number FROM evidence WHERE relationship_type='header_label_association' "
                                           'AND (source_id=? OR target_id=?) ORDER BY efta_number LIMIT 20',(i,i))
                    for doc, in docs:
                        if any(pattern.search(text or '') for text, in source.execute('SELECT text FROM pages WHERE doc_id=?',(doc,))):
                            found = True; break
            if not found: del attested[i]
    finally:
        if source is not None: source.close()
        if headers is not None: headers.close()
    # Case/dot normalisation can preserve independently attested mailboxes.
    # OCR look-alikes and edit distance are merge clues, not publication proof:
    # jane1 and jane2 may be two real people even if the merger folded them.
    proven = defaultdict(set)
    for i,(c,_) in attested.items():
        if i in values and values[i]['kind']=='email':
            local,domain=email_key(values[i]['value'])
            proven[c].add((local,domain))
    for i,c,_,_ in rows:
        if i in attested or i not in values or values[i]['kind']!='email': continue
        local,domain=email_key(values[i]['value'])
        raw = values[i]['value'].casefold()
        if (raw.count('@') == 1 and raw.rsplit('@',1)[-1] == domain
                and raw.split('@',1)[0].replace('.','') == local and (local,domain) in proven[c]):
            attested[i] = (c,'normalised_attested_mailbox')
    db.execute('CREATE TABLE person_contact_evidence(member TEXT PRIMARY KEY, owner TEXT, rule TEXT)')
    db.executemany('INSERT INTO person_contact_evidence VALUES (?,?,?)',
                   [(i,c,r) for i,(c,r) in sorted(attested.items())])
    db.executemany('UPDATE nodes SET canonical=? WHERE id=?',[(canonical,i) for i,canonical,_,_ in rows])
    # An extracted spelling may be only a candidate in the NER lookup or
    # outside the header viewer slice. Retain its canonical as an actual node
    # in the full contact data as well as the raw member nodes.
    missing = sorted({c for _,c,_,_ in rows if c.startswith(('participant_','email_','phone_')) or c in values}
                     - {i for i, in db.execute('SELECT id FROM nodes')})
    if missing:
        db.executemany('INSERT INTO nodes(id,kind,canonical,in_headers,service_line,documents,pages,body_pages,labels) '
                       'VALUES (?,?,?,0,0,0,0,0,?)',
                       [(i,values[i]['kind'] if i in values else i.split('_',1)[0],i,json.dumps({'label_quality':'ok'}) if i.startswith('participant_') else None)
                        for i in missing])
    # Re-aggregate page evidence under the new canonicals; original mentions
    # remain untouched and each distinct document still counts once.
    db.executescript("""
      CREATE TEMP TABLE resolved_mentions AS
        SELECT DISTINCT n.canonical id,m.efta,m.page FROM mentions m JOIN nodes n ON n.id=m.id;
      CREATE INDEX resolved_page ON resolved_mentions(efta,page,id);
      CREATE TEMP TABLE eligible_pages AS SELECT efta,page,count(*) n FROM resolved_mentions
        GROUP BY efta,page HAVING count(*) BETWEEN 2 AND 40;
      DROP TABLE pairs;
      CREATE TABLE pairs AS SELECT a.id a,b.id b,count(DISTINCT a.efta) shared_documents,
        count(*) shared_pages,round(sum(1.0/(p.n-1)),3) specificity,min(a.efta) sample_efta
        FROM eligible_pages p JOIN resolved_mentions a USING(efta,page)
        JOIN resolved_mentions b ON b.efta=a.efta AND b.page=a.page AND a.id<b.id
        GROUP BY a.id,b.id;
      CREATE INDEX pairs_a ON pairs(a); CREATE INDEX pairs_b ON pairs(b);
      DROP TABLE resolved_mentions; DROP TABLE eligible_pages;
    """)
    return {'by_rule': dict(Counter(rule for i,c,rule,_ in rows if i != c)),
            'retained_nodes':dict(db.execute('SELECT kind,count(*) FROM nodes GROUP BY kind')),
            'remaining_contacts':dict(db.execute("SELECT n.kind,count(DISTINCT n.canonical) FROM nodes n JOIN nodes t ON t.id=n.canonical AND t.kind=n.kind WHERE n.kind IN ('email','phone') GROUP BY n.kind"))}


def viewer_folds(entities, relationships, out):
    """Fold the viewer slice, retaining every full-data node and its provenance."""
    path=out/'contact_graph.db'
    if not path.exists(): return entities,relationships
    with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='resolutions'").fetchone(): return entities,relationships
        mapping={i:(c,r) for i,c,r in db.execute('SELECT member,canonical,rule FROM resolutions WHERE member!=canonical')}
    by_id={str(e['id']):dict(e) for e in entities}
    visible_ids = set(by_id)
    for e in entities:
        visible_ids.update(str(m['id']) for m in e.get('merged_identities',[]) if 'id' in m)
    wanted={c for i,(c,r) in mapping.items() if i in visible_ids and c not in by_id}
    if wanted:
        for e in items(out/'graph_entities.json'):
            if str(e['id']) in wanted: by_id[str(e['id'])]=dict(e)
    for i,(c,r) in mapping.items():
        if i in visible_ids and c not in by_id and c.startswith('participant_'):
            by_id[c]={'id':c,'name':'HeaderLabel_'+c.split('_',1)[1],'entity_type':'participant'}
    # Previously merged role mailboxes can move from a display-name identity
    # to their organisation without merging that person's own node into it.
    moved = []
    for i,e in by_id.items():
        keep = []
        for m in e.get('merged_identities',[]):
            target,rule = mapping.get(str(m.get('id')), (i,None))
            if target != i and target in by_id and str(m.get('id','')).startswith(('email_','phone_')):
                moved.append((target,{**m,'rule':rule}))
            else:
                keep.append(m)
        if 'merged_identities' in e: e['merged_identities']=keep
    for target,member in moved:
        members=by_id[target].setdefault('merged_identities',[])
        if not any(m.get('id')==member.get('id') for m in members): members.append(member)
    resolutions={by_id[i]['id']:(by_id[c]['id'],r) for i,(c,r) in mapping.items() if i in by_id and c in by_id}
    from viewer_label_payload import fold_viewer_resolutions
    folded, edges = fold_viewer_resolutions(list(by_id.values()),relationships,resolutions)
    inferred = {'unique_full_name_localpart','initial_surname_known_domain','signature_consensus',
                'organisation_domain','organisation_role','organisation_domain_owner','organisation_switchboard','nickname_signature_full_name',
                'ocr_edit','ocr_confusables','ocr_digit','ocr_space_split','ocr_variant','ocr_tld_same_mailbox','missing_country_code'}
    for node in folded:
        for member in node.get('merged_identities',[]):
            if member.get('rule') in inferred:
                member['confidence']='probable'
            elif member.get('rule') == 'reviewed_contact_owner':
                member['confidence']='reviewed'
    return folded, edges


def spaced_address_variants(source, documents, lookup, corpus_db):
    """Recover an extracted tail from an address split inside a word by OCR.

    Require the complete known address, with inter-letter whitespace, on every
    sampled source page of a value in at most three documents. Ordinary prose
    is never joined to invent an address; only an independently known mailbox
    can supply the missing letters. A disagreeing page vetoes the global fold.
    """
    suffixes = defaultdict(set)
    targets = {}
    for identity,spec in lookup.items():
        if spec.get('kind') != 'email' and not identity.startswith('email_'):
            continue
        local, domain = email_key(spec['value'])
        if not 4 <= len(local) <= 64:
            continue
        target = spec['value'].casefold()
        targets.setdefault((local,domain),target)
        for start in range(1,len(local)-1):
            suffixes[local[start:],domain].add((local,domain))
    candidates = {}
    for (kind,value),n in documents.items():
        if kind != 'email' or n > 3:
            continue
        key = email_key(value)
        hits = suffixes.get(key,set())
        if key not in targets and 0 < len(hits) <= 20:
            candidates[value] = hits
    pages = defaultdict(set)
    for value,doc,page in source.execute("SELECT value,efta,page FROM mentions WHERE kind='email'"):
        value = value.casefold()
        if value in candidates:
            pages[value].add((doc,page))
    patterns = {}
    def pattern(key):
        if key not in patterns:
            local,domain = key
            local_pattern = r'[\s.]*'.join(map(re.escape, local.replace('.','')))
            domain_pattern = r'\s*'.join(map(re.escape,domain))
            patterns[key] = re.compile(r'(?<![\w])'+local_pattern+r'\s*(?:@|©|®|\(a\))\s*'+domain_pattern+r'(?![\w.])',re.I)
        return patterns[key]
    out = {}
    for value,hits in sorted(candidates.items()):
        observed = set(hits)
        # Also bound repeated-page noise in a long contact-directory document.
        if not pages[value] or len(pages[value]) > 6:
            continue
        for doc,page in sorted(pages[value]):
            row = corpus_db.execute('SELECT text FROM pages WHERE doc_id=? AND page=? LIMIT 1',(doc,page)).fetchone()
            text = row[0] if row and row[0] else ''
            observed &= {key for key in observed if (m:=pattern(key).search(text)) and re.search(r'\s',m.group())}
            if not observed:
                break
        if len(observed) == 1:
            out['email',value] = (targets[next(iter(observed))],'ocr_space_split')
    return out
