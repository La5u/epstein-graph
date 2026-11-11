"""Bounded, build-only evidence for ambiguous company and website labels.

No combined graph is read and no extraction signature depends on this module.
Counts retain the legal form: occurrences of a bare word do not attest a firm.
"""
from collections import defaultdict
from contextlib import closing
import csv
import json
from itertools import islice, product
from pathlib import Path
import re
import sqlite3

from bounded_json import items
from entity_build_rules import FORMS, GENERIC, STUB_WORDS, company_tokens, has_company_form, text_reason, website_reason

ROOT = Path(__file__).resolve().parents[1]
MIN_LEGAL_DOCUMENTS = 3
# Pages read per full-text batch. Short names ('bare.com' -> 'bare com') match
# millions of pages; reading them all took hours. Unattested within the cap = junk.
MAX_SCANNED_PAGES = 1500


class DocumentOverlap:
    """Bounded, read-only support from a completed predecessor document index.

    Never use this in a step that produces the index. A truncated evidence set
    cannot authorize a merge; common labels are deliberately left unresolved.
    """
    def __init__(self, path, limit=1024):
        self.limit = limit
        self.db = sqlite3.connect(f'file:{path}?mode=ro', uri=True) if path.exists() else None
        if self.db:
            self.db.execute('PRAGMA cache_size=-2000')
        self.cache = {}

    def documents(self, identity):
        if identity not in self.cache:
            rows = list(self.db.execute('SELECT efta FROM node_documents WHERE node_id=? LIMIT ?',
                                       (str(identity), self.limit + 1))) if self.db else []
            if len(self.cache) >= 256:
                self.cache.pop(next(iter(self.cache)))
            self.cache[identity] = None if len(rows) > self.limit else {r[0] for r in rows if r[0]}
        return self.cache[identity]

    def supports(self, source, target):
        a, b = self.documents(source), self.documents(target)
        shared = len(a & b) if a and b else 0
        # The bare brand usually has the larger document set (it is the common
        # short form), so containment is judged against the smaller side.
        return shared >= 2 and shared >= .8 * min(len(a), len(b))

    def close(self):
        if self.db:
            self.db.close()


def full_key(value):
    from build_companies import normal_name
    return normal_name(value)


class LabelEvidence:
    def __init__(self, trusted=(), legal_counts=None, domains=(), geographic=()):
        self.trusted = {full_key(s) for s in trusted}
        self.legal_counts = legal_counts or {}
        self.domains = set(domains)
        self.geographic = {full_key(s) for s in geographic}

    @classmethod
    def project(cls, root=ROOT, include_counts=True):
        trusted = []
        geographic = []
        kg = root / 'data/raw/knowledge_graph_entities.json'
        if kg.exists():
            for node in items(kg):
                if node['entity_type'] in {'organization', 'shell_company'}:
                    trusted.extend([node['name'], *(node.get('aliases') or [])])
                elif node['entity_type'] in {'location', 'airport', 'property'}:
                    geographic.append(node['name'])
        gaz = root / 'config/entity_gazetteer.json'
        if gaz.exists():
            for name, spec in json.loads(gaz.read_text()).items():
                if spec['type'] in {'organization', 'shell_company'}:
                    trusted.extend([name, *spec.get('aliases', [])])
                elif spec['type'] in {'location', 'place'}:
                    geographic.extend([name, *spec.get('aliases', [])])
        airports = root / 'data/external/ourairports/airports.csv'
        if airports.exists():
            with airports.open(newline='') as stream:
                geographic.extend(row['municipality'] for row in csv.DictReader(stream) if row['municipality'])
        review = root / 'config/company_review.json'
        if review.exists():
            spec = json.loads(review.read_text())
            trusted.extend(spec.get('include', []))
            trusted.extend(spec.get('rename', {}).values())
            for alias, canonical in spec.get('merge', {}).items():
                if not alias.startswith('company_'):  # ID-only reviews are pointers, not attested names.
                    trusted.extend((alias, canonical))
            trusted.extend(n['name'] for n in spec.get('seeds', []))
            excluded = {full_key(s) for s in spec.get('exclude', [])}
            trusted = [s for s in trusted if full_key(s) not in excluded]
        counts = {}
        path = root / 'output/.build/company_mentions.db'
        if include_counts and path.exists():
            with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
                db.executescript('PRAGMA temp_store=FILE; PRAGMA cache_size=-16000;')
                for spelling, count in db.execute('SELECT spelling,count(DISTINCT efta) FROM mentions GROUP BY spelling HAVING count(DISTINCT efta)>=3'):
                    k = full_key(spelling)
                    # Never sum spelling counts: one document can contain many spellings.
                    counts[k] = max(counts.get(k, 0), count)
        return cls(trusted, counts, geographic=geographic)

    def company_attested(self, value, documents=None):
        k = full_key(value)
        if k in self.trusted:
            return True
        if re.search(r'^[A-Z]{1,3}\s*&\s*[A-Z]{1,3}\b', value) and not text_reason(value):
            return True
        if (self.legal_counts.get(k, 0) if documents is None else documents) < MIN_LEGAL_DOCUMENTS or not has_company_form(value) or text_reason(value):
            return False
        words = k.split()
        count = self.legal_counts.get(k, 0) if documents is None else documents
        if count >= 20:
            # A frequent complete legal name can use a legal-form word as its
            # brand, or name a state's statutory insurance fund. These need
            # stronger evidence than ordinary distinctive firms.
            if re.fullmatch(r'The [A-Z][a-z]+,? (?:Inc\.?|LLC|Corp\.?|Ltd\.?)', value):
                return True
            if k.endswith(' fund') and any(k.startswith(p + ' state ') for p in self.geographic):
                return True
        while words and words[-1] in FORMS:
            words.pop()
        words = [w for w in words if w != 'the']
        if not any(w not in FORMS for w in company_tokens(value)):
            return False
        if all(w in FORMS | {'abbr'} for w in company_tokens(value)):
            return False
        if len(words) == 1 and words[0] in STUB_WORDS | {'aviation', 'cayman', 'services', 'service', 'management', 'bank',
                    'offshore', 'onshore', 'feeder', 'master', 'insurance'}:
            # Multi-letter corporate initials need much stronger repetition
            # than an ordinary name; a single letter never establishes one.
            initial = re.match(r'^(?:[A-Z](?:\.[A-Z]){1,3}\.?|[A-Z]{2,3})\s', value)
            return bool(initial and count >= 20)
        if len(words) == 1 and words[0] in GENERIC and k.split()[-1] in {'llc', 'lp', 'llp', 'lllp', 'pllc', 'pc', 'pa', 'co', 'corp', 'ltd', 'inc'}:
            return False
        # Repetition of class descriptions is not legal-name evidence. Bare
        # trusts/funds need a curated identity; their surrounding prose repeats.
        if k.split()[-1] in {'trust', 'fund', 'holdings'}:
            return False
        if set(words) <= set('first second third fourth foreign domestic general limited business consenting'.split()):
            return False
        # Repeated clauses and form fields are not independently named firms.
        prose = set('a an of on in at by for from to and or with as if because while '
                    'neither manager administrator member grantor account name '
                    'summary section agreement memorandum under underlying '
                    'consenting sole members trustees directors partner'.split())
        return bool(words) and not set(words) & prose and any(len(w) >= 3 and w.isalpha() for w in words)

    def prepare_companies(self, values, path=None):
        """Fill gaps where reviewed spellings differ from the extractor's key.

        FTS is only a candidate locator; exact text (including the legal form)
        and distinct document IDs establish the three-document threshold.
        """
        import corpus
        from entity_build_rules import company_label_reason
        wanted = sorted({v for v in values if company_label_reason(v) in {'generic_company', 'company_initial_fragment'}
                         and not self.company_attested(v) and self.company_attested(v, MIN_LEGAL_DOCUMENTS)})
        path = path or corpus.PATH
        if not wanted or not path.exists():
            return
        with closing(corpus.connect(path)) as db:
            db.execute('PRAGMA cache_size=-16000')
            for start in range(0, len(wanted), 32):
                batch = wanted[start:start + 32]
                patterns = {}
                phrases = set()
                documents = defaultdict(set)
                for value in batch:
                    tokens = re.findall(r'[\w]+|&', value)
                    alternatives = {'insurance': ('insurance', 'ins'), 'company': ('company', 'co')}
                    parts = [r'(?:and|&)' if t.casefold() in {'and', '&'} else
                             '(?:' + '|'.join(alternatives[t.casefold()]) + ')' if t.casefold() in alternatives else re.escape(t) for t in tokens]
                    patterns[value] = re.compile(r'(?<!\w)' + r'[\s.,]*'.join(parts) + r'(?!\w)', re.I)
                    for variant in product(*(alternatives.get(t.casefold(), (t,)) for t in tokens)):
                        phrases.add('"' + ' '.join(re.findall(r'\w+', ' '.join(variant))) + '"')
                query = ' OR '.join(sorted(phrases))
                for doc, text in islice(corpus.search_text(db, query), MAX_SCANNED_PAGES):
                    for value, pattern in patterns.items():
                        if len(documents[value]) < MIN_LEGAL_DOCUMENTS and pattern.search(text or ''):
                            documents[value].add(doc)
                    if all(len(d) >= MIN_LEGAL_DOCUMENTS for d in documents.values()) and len(documents) == len(batch):
                        break
                for value, docs in documents.items():
                    if len(docs) >= MIN_LEGAL_DOCUMENTS:
                        self.legal_counts[full_key(value)] = len(docs)

    def prepare_reference_brands(self, references, path=None):
        """Independent URL/mailbox evidence for ambiguous single-token NER brands."""
        labels = set()
        for node in references.values():
            meta = node.get('metadata') or {}
            meta = json.loads(meta) if isinstance(meta, str) else meta
            if (str(node['id']).startswith('entity_') and node['entity_type'] in {'organization', 'shell_company'}
                    and meta.get('document_count', 0) >= 20 and re.fullmatch(r'[A-Za-z]{4,}', node['name'])):
                labels.add(node['name'].casefold())
        self.prepare_domains((name + '.' + suffix for name in labels for suffix in ('com', 'org', 'net')), path, force=True)

    def prepare_domains(self, domains, path=None, force=False):
        """One FTS pass per batch, retaining hosts only from literal URL/mailboxes.

        A plain OCR token does not attest a host. Provider garbles remain junk
        even when a damaged address happens to contain them.
        """
        import corpus
        from entity_labels import dictionary_words
        dictionary = dictionary_words()
        # Only plausible hosts are searched: seen in >=2 documents (when counts
        # are given) with a label of >=2 characters incl. a letter. '0.ac' or a gmail garble
        # ('2inail.com') stays junk; 24k candidates took hours of full-text reads.
        counts = domains if isinstance(domains, dict) else None
        wanted = sorted({d for d in domains if website_reason(d, dictionary=dictionary, attested=True) is None
                         and (counts is None or counts[d] >= 2) and re.fullmatch(r'(?=.*[a-z])[a-z0-9-]{2,}', d.rpartition('.')[0].rpartition('.')[2], re.I)
                         and (force or website_reason(d, dictionary=dictionary)
                         in {'short_domain_label', 'bare_dictionary_domain'})} - self.domains)
        path = path or corpus.PATH
        if not wanted or not path.exists():
            return
        host_pattern = re.compile(r'(?:https?://|\bwww\.|[\w.+%-]+@)((?:[a-z0-9-]+\.)+[a-z]{2,})(?![\w-]|\.[a-z0-9])', re.I)
        with closing(corpus.connect(path)) as db:
            db.execute('PRAGMA cache_size=-16000')
            for start in range(0, len(wanted), 32):
                remaining = set(wanted[start:start + 32])
                query = ' OR '.join('"' + d.replace('"', '""') + '"' for d in sorted(remaining))
                for _, text in islice(corpus.search_text(db, query), MAX_SCANNED_PAGES):
                    for match in host_pattern.finditer(text or ''):
                        host = match[1].casefold().removeprefix('www.')
                        labels = host.split('.')
                        for offset in range(len(labels) - 1):
                            domain = '.'.join(labels[offset:])
                            if domain in remaining:
                                self.domains.add(domain)
                                remaining.remove(domain)
                    if not remaining:
                        break
