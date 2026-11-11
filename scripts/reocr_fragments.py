"""Recover recipients lost to split webmail addresses by re-reading the official scans.

    .venv/bin/python scripts/reocr_fragments.py [--workers 4] [--apply]

join_split_addresses() folds a domain fragment ("mail.com") into the recipient
before it. Two cases stay unidentified: the fragment opens the list, or the
item before it is too garbled to resolve ("Von: Jeffre E stein", "*eevacation").
For each such recipient this finds the corpus header line holding the
fragment (with the line before it, where the start of a wrapped address sits),
reads that page from the justice.gov PDF with Tesseract and RapidOCR, and
takes the address in the matching lines whose domain ends in the fragment.

A repair is accepted only if
  - both OCR engines read an address there, and they resolve to one identity;
  - that address is already a node of the communication graph (nothing new is
    invented from OCR alone);
  - every corpus line of the document that could be this header agrees.
--third-engine settles the cases where the two engines disagreed: the saved
report's pages are read again with OnnxTR (docTR detection + PARSeq
recognition on ONNX, a third independent model family). A repair is then
accepted if two engines read one known address and no engine reads another.

--luna settles what is still disputed with a vision model: a band around the
header row (located with RapidOCR boxes) is cropped from each page and
GPT-6-Luna transcribes the address on the To/Cc line without being shown any
candidate (data/private/ocr_review/luna_crop_prompt.txt; one JSON answer per
crop in luna_crop_out/). Redacted or partly hidden addresses count as not
legible; text under redaction boxes is never used. A repair is accepted if
Luna reads exactly one known address, at least one OCR engine reads the same
identity, and every other engine reading is that address, a garble of it
(within two characters, with a name glued in front, or its cut-off end) or
nothing.

Recipients where no OCR engine read a known address (no_address_read; mostly
redactions) get the same Luna crop. With no engine to agree, a Luna reading of
one known identity is accepted only if the corpus window holds a garble of its
local part, or the engines' row for the fragment carries no other header label
(a "From:" row means the fragment belongs to another field).

Accepted repairs are written to config/recipient_repairs.json with --apply
(pseudonymous IDs only); build_communications adds them as recipients of that
message. Raw lines and per-engine readings go to
data/private/ocr_review/fragment_reocr.json for review.
"""
import argparse
import json
import os
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from pathlib import Path
from queue import Queue

from rapidfuzz import fuzz

from build_communications import (Builder, OCR_AT_SIGN, domain_fragment, join_split_addresses,
                                  parse_recipient, valid_email)
from build_contacts import contact_id, contact_key
import corpus as corpus_source
import emails as email_source
from corpus import document_urls
from normalize_graph import registry_aliases
import reocr_full
import reocr_participants

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'data/external/reocr'
PRIVATE = ROOT / 'data/private/ocr_review/fragment_reocr.json'
CONFIG = ROOT / 'config/recipient_repairs.json'
ADDRESS = re.compile(r'[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+')
ROLES = {'to': 'sent_to', 'cc': 'cc_to'}


def unresolved_fragments(builder):
    """[(record_id, efta, role, prev_text, fragment)] the builder cannot attribute."""
    found = []
    with closing(email_source.headers()) as db:
        for record_id, efta, to_raw, cc_raw in db.execute('SELECT id,doc_id,to_json,cc_json FROM headers WHERE noise=0'):
            for role, raw in (('to', to_raw), ('cc', cc_raw)):
                try:
                    items = json.loads(raw or '[]')
                except (ValueError, TypeError):
                    continue
                if not isinstance(items, list):
                    continue
                pairs = [pair for item in items for pair in parse_recipient(item)]
                for k, (name, address) in enumerate(pairs):
                    if address is not None or not domain_fragment(name):
                        continue
                    if k == 0:
                        found.append((record_id, efta, role, '', name))
                        continue
                    prev, prev_address = pairs[k - 1]
                    if prev_address is not None:
                        continue
                    joined = join_split_addresses(pairs[k - 1:k + 1])
                    if joined and joined[0][1]:
                        continue  # rejoined into an address
                    if builder.label_identity(prev) is None:
                        found.append((record_id, efta, role, prev, name))
    return found


def candidate_lines(pages, prev, fragment, role):
    """[(page, two-line window, score)] corpus lines that may hold this header."""
    needle = fragment.strip().strip('<>,;"\'').casefold()
    probe = f'{prev} {fragment}'.casefold()
    out = []
    for page, text in pages:
        lines = (text or '').splitlines()
        for i, line in enumerate(lines):
            if needle not in line.casefold():
                continue
            window = (lines[i - 1] + ' ' if i else '') + line
            score = fuzz.partial_ratio(probe, window.casefold()) if prev else 0
            score += 30 * bool(re.match(rf'\s*{role}\s*[:;]', window, re.I) or re.match(rf'\s*{role}\s*[:;]', line, re.I))
            out.append((page, window.strip(), score))
    return out


def engine_addresses(lines, window, fragment):
    """Addresses whose domain ends in the fragment, from the engine lines best matching the window."""
    lines = [line.strip() for line in lines if line.strip()]
    if not lines:
        return set(), ''
    spans = [' '.join(lines[i:i + n]) for n in (1, 2) for i in range(len(lines) - n + 1)]
    best = max(spans, key=lambda s: fuzz.partial_ratio(window.casefold(), s.casefold()) + fuzz.ratio(window.casefold(), s.casefold()) / 4)
    tail = fragment.strip().strip('<>,;"\'').casefold()
    found = set()
    for raw in ADDRESS.findall(OCR_AT_SIGN.sub('@', re.sub(r'\s*@\s*', '@', best))):
        email = valid_email(raw.strip('.,;:<>()[]'))
        if email and email.rsplit('@', 1)[1].casefold().endswith(tail):
            found.add(email)
    return found, best


_onnxtr = None


def onnxtr_rows(job):
    """OnnxTR text of one page as visual rows (runs in a worker)."""
    global _onnxtr
    import tempfile
    import subprocess
    pdf, page = job
    if _onnxtr is None:
        import onnxruntime
        from onnxtr.models import EngineConfig, ocr_predictor
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads, options.inter_op_num_threads = 2, 1
        config = EngineConfig(session_options=options)
        _onnxtr = ocr_predictor(det_arch='db_resnet50', reco_arch='parseq', assume_straight_pages=True,
                                det_engine_cfg=config, reco_engine_cfg=config)
    from onnxtr.io import DocumentFile
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(['pdftoppm', '-r', '300', '-f', str(page), '-l', str(page), '-png', pdf, f'{tmp}/p'], check=True)
        result = _onnxtr(DocumentFile.from_images([str(next(Path(tmp).glob('p*.png')))]))
    # Word boxes (relative corners) as RapidOCR-style pixel quads, grouped into visual rows the same way.
    boxes = [[[(x * 1000, y * 1000) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))], word.value, 1.0]
             for block in result.pages[0].blocks for line in block.lines for word in line.words
             for (x0, y0), (x1, y1) in [word.geometry]]
    return reocr_participants.rapid_rows(boxes)


def write_config(repairs):
    CONFIG.write_text(json.dumps({
        '_doc': 'Recipients recovered from the official scans where a split webmail address left only its domain '
                '("mail.com") in the header metadata. Written by scripts/reocr_fragments.py. engines = the OCR passes '
                'that read this address on the matching header line: either Tesseract and RapidOCR together, or two '
                'of Tesseract/RapidOCR/OnnxTR with none reading another address. The address is already a graph '
                'node and all candidate lines of the document agree. record = email_metadata.db emails_merged id; '
                'identity = pseudonymous address read; build_communications adds it as a recipient of that message.',
        'repairs': sorted(repairs, key=lambda r: (r['record'], r['role'], r['identity']))}, indent=1) + '\n')
    print(f'{len(repairs)} repairs -> {CONFIG.relative_to(ROOT)}')


def third_engine(args, key, known, final):
    """Re-read the engine disagreements of the saved report with OnnxTR (2-of-3 vote)."""
    report = json.loads(PRIVATE.read_text())
    disputed = [e for e in report if e['verdict'] == 'engines_disagree']
    jobs = sorted({(e['efta_number'], r['page']) for e in disputed for r in e['readings']})
    print(f'{len(disputed)} disagreements; {len(jobs)} pages for OnnxTR', flush=True)
    rows = {}
    with ProcessPoolExecutor(args.workers) as pool:
        futures = {job: pool.submit(onnxtr_rows, (str(WORK / f'{job[0]}.pdf'), job[1])) for job in jobs if (WORK / f'{job[0]}.pdf').exists()}
        for n, (job, future) in enumerate(futures.items(), 1):
            rows[job] = future.result()
            if n % 25 == 0:
                print(f'{n}/{len(futures)} pages read', flush=True)
    outcomes = Counter()
    for entry in disputed:
        targets, verdict = set(), None
        for reading in entry['readings']:
            if (entry['efta_number'], reading['page']) not in rows:
                verdict = 'download_failed'
                break
            found, line = engine_addresses(rows[entry['efta_number'], reading['page']], reading['corpus'], entry['fragment'])
            ids = {contact_id(e, 'email', key) for e in found}
            reading['onnxtr'] = {'line': line, 'addresses': sorted(found), 'known': sorted(i for i in ids if i in known)}
            # An engine that reads an unknown address counts as reading something else.
            votes = [({final(i) for i in reading[name]['known']}, bool(reading[name]['addresses']))
                     for name in ('tesseract', 'rapidocr', 'onnxtr')]
            read = set().union(*(v for v, _ in votes))
            if len(read) != 1 or any(saw and not v for v, saw in votes) or sum(bool(v) for v, _ in votes) < 2:
                verdict = 'still_disputed'
                break
            targets |= read
        entry['verdict'] = verdict or ('accepted' if len(targets) == 1 else 'ambiguous_lines')
        entry['engines'] = 'two_of_three'
        outcomes[entry['verdict']] += 1
    PRIVATE.write_text(json.dumps(report, indent=1, ensure_ascii=False) + '\n')
    print(json.dumps(dict(outcomes), indent=1))
    return report


def agrees(engine_address, luna_address, target, key, final):
    """An engine reading consistent with Luna's: same identity, a name glued in front, a near-miss,
    or the cut-off end of it ("JEE@" for "LESLEY.JEE@")."""
    from rapidfuzz.distance import Levenshtein
    e, luna = engine_address.casefold(), luna_address.casefold()
    return (final(contact_id(engine_address, 'email', key)) == target or e.endswith(luna) or Levenshtein.distance(e, luna) <= 2
            or (len(e) >= 8 and Levenshtein.distance(e, luna[-len(e):]) <= 2))


HEADER_LABEL = re.compile(r'\s*(from|to|cc|bcc|sent|date|subject|reply-?to)\s*[:;]', re.I)


def luna_alone(entry, answers, key, known, final):
    """Verdict for a fragment no OCR engine read: Luna's reading of the last (unread) window, checked
    against the corpus text; earlier windows were read by both engines and must name the same identity."""
    *earlier, reading = entry['readings']
    n = len(earlier)
    path = answers / f"{entry['record_id']}_{entry['role']}_{n}.json"
    if not path.exists():
        return 'no_address_read'  # not transcribed yet; scored on a later --luna run
    answer = json.loads(path.read_text())
    read = sorted({e for a in answer.get('addresses', ()) for m in ADDRESS.findall(a) if (e := valid_email(m))}
                  if answer.get('legible') else ())
    ids = {contact_id(e, 'email', key) for e in read}
    reading['luna'] = {'addresses': read, 'known': sorted(i for i in ids if i in known)}
    if not read:
        return 'luna_not_legible'
    targets = {final(i) for i in ids if i in known}
    if len(targets) != 1 or len(reading['luna']['known']) != len(ids):
        return 'luna_unknown_address'
    if any({final(i) for name in ('tesseract', 'rapidocr') for i in r[name]['known']} != targets for r in earlier):
        return 'luna_contradicted'
    # An engine that read some other (unknown) address on this row contradicts Luna.
    if not all(agrees(e, read[0], next(iter(targets)), key, final) for name in ('tesseract', 'rapidocr') for e in reading[name]['addresses']):
        return 'luna_contradicted'
    squash = lambda s: re.sub(r'[^0-9a-z]', '', s.casefold())
    corpus = squash(reading['corpus'])
    garbled = any(len(local := squash(e.split('@')[0])) >= 4 and fuzz.partial_ratio(local, corpus) >= 80 for e in read)
    rows = [reading[name]['line'] for name in ('tesseract', 'rapidocr')]
    fragment = squash(entry['fragment'])
    on_own_row = any(fragment in squash(row) for row in rows) and not any(
        (m := HEADER_LABEL.match(row)) and m.group(1).casefold() != entry['role'] for row in rows)
    return 'accepted' if garbled or on_own_row else 'luna_uncorroborated'


def luna_vote(key, known, final):
    """Score Luna's transcriptions of the still-disputed crops (see --luna)."""
    report = json.loads(PRIVATE.read_text())
    answers = PRIVATE.parent / 'luna_crop_out'
    outcomes = Counter()
    for entry in report:
        if entry['verdict'] == 'no_address_read':
            entry['verdict'] = luna_alone(entry, answers, key, known, final)
            if entry['verdict'] == 'accepted':
                entry['engines'] = 'luna_corroborated'
            outcomes[entry['verdict']] += 1
            continue
        if entry['verdict'] != 'still_disputed':
            continue
        targets, verdict = set(), None
        for n, reading in enumerate(entry['readings']):
            path = answers / f"{entry['record_id']}_{entry['role']}_{n}.json"
            answer = json.loads(path.read_text()) if path.exists() else {}
            read = sorted({e for a in answer.get('addresses', ()) if (e := valid_email(a))}) if answer.get('legible') else []
            reading['luna'] = {'addresses': read, 'known': sorted(i for e in read if (i := contact_id(e, 'email', key)) in known)}
            if len(read) != 1 or not reading['luna']['known']:
                verdict = 'luna_not_legible' if not read else 'luna_unknown_address'
                break
            target = final(reading['luna']['known'][0])

            def consistent(name):
                return all(agrees(e, read[0], target, key, final) for e in reading[name]['addresses'])
            engines = ('tesseract', 'rapidocr', 'onnxtr')
            if not all(consistent(name) for name in engines) or not any(target in {final(i) for i in reading[name]['known']} for name in engines):
                verdict = 'luna_contradicted'
                break
            targets.add(target)
        entry['verdict'] = verdict or ('accepted' if len(targets) == 1 else 'ambiguous_lines')
        if entry['verdict'] == 'accepted':
            entry['engines'] = 'luna_vote'
        outcomes[entry['verdict']] += 1
    PRIVATE.write_text(json.dumps(report, indent=1, ensure_ascii=False) + '\n')
    print(json.dumps(dict(outcomes), indent=1))
    return report


def repairs_from(report, final):
    repairs = []
    for entry in report:
        if entry['verdict'] != 'accepted':
            continue
        names = {'two_of_three': ('tesseract', 'rapidocr', 'onnxtr'), 'luna_vote': ('luna', 'tesseract', 'rapidocr', 'onnxtr'),
                 'luna_corroborated': ('luna', 'tesseract', 'rapidocr')}.get(entry.get('engines'), ('tesseract', 'rapidocr'))
        reads = {name: {i for r in entry['readings'] for i in r.get(name, {}).get('known', ())} for name in names}
        if entry.get('engines') == 'luna_vote':  # Luna fixes the identity; agreeing engines are listed
            luna = {final(i) for i in reads['luna']}
            reads = {name: {i for i in ids if final(i) in luna} for name, ids in reads.items()}
        target = set.intersection(*({final(i) for i in ids} for ids in reads.values() if ids))
        assert len(target) == 1, entry['record_id']
        votes = Counter(i for ids in reads.values() for i in ids if final(i) in target)
        identity = min(votes, key=lambda i: (-votes[i], i))  # engines may read two OCR variants of one address
        repairs.append({'record': entry['record_id'], 'role': entry['role'], 'identity': identity,
                        'efta_number': entry['efta_number'], 'engines': sorted(n for n, ids in reads.items() if ids)})
    return repairs


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--pause', type=float, default=0.3)
    parser.add_argument('--apply', action='store_true', help='write accepted repairs to config/recipient_repairs.json')
    parser.add_argument('--third-engine', action='store_true', help='settle the saved report\'s disagreements with OnnxTR')
    parser.add_argument('--luna', action='store_true', help='score Luna transcriptions of the still-disputed crops')
    args = parser.parse_args()
    key = contact_key()
    graph = sqlite3.connect(f'file:{ROOT / "output/communication_graph.db"}?mode=ro', uri=True)
    canonical = dict(graph.execute('SELECT member_id, canonical_id FROM identity_map'))
    known = {row[0] for row in graph.execute('SELECT id FROM nodes')} | set(canonical)

    def final(identity):
        seen = set()
        while identity in canonical and identity not in seen:
            seen.add(identity)
            identity = canonical[identity]
        return identity

    if args.third_engine or args.luna:
        report = third_engine(args, key, known, final) if args.third_engine else luna_vote(key, known, final)
        if args.apply:
            write_config(repairs_from(report, final))
        return
    entities = json.loads((ROOT / 'output/normalized_entities.json').read_text())
    builder = Builder(key, entities, registry_aliases(entities, json.loads((ROOT / 'data/raw/persons_registry.json').read_text())))
    cases = unresolved_fragments(builder)
    print(f'{len(cases)} unresolved fragment recipients', flush=True)

    corpus = corpus_source.connect()
    urls = document_urls({c[1] for c in cases})
    plans = {}  # case -> [(page, window)]
    for case in cases:
        record_id, efta, role, prev, fragment = case
        if efta not in urls:
            continue
        pages = list(corpus.execute('SELECT page, text FROM pages WHERE doc_id=? ORDER BY page', (efta,)))
        lines = candidate_lines(pages, prev, fragment, role)
        if lines:
            top = max(score for _, _, score in lines)
            # Every equally good candidate is read; they must all agree.
            plans[case] = [(page + 1, window) for page, window, score in lines if score >= top - 5][:4]
    jobs = sorted({(case[1], page) for case, plan in plans.items() for page, _ in plan})
    print(f'{len(plans)} located in the corpus; {len(jobs)} pages to read', flush=True)

    WORK.mkdir(parents=True, exist_ok=True)
    todo = sorted({e for e, _ in jobs}, key=lambda e: (not (WORK / f'{e}.pdf').exists(), e))
    ready = Queue(maxsize=args.workers * 4)
    threading.Thread(target=reocr_full.downloader, args=(todo, urls, ready, args.pause), daemon=True).start()
    pages_of = defaultdict(list)
    for job in jobs:
        pages_of[job[0]].append(job)
    texts, futures, failed = {}, {}, 0
    with ProcessPoolExecutor(args.workers) as pool:
        while (item := ready.get()) is not None:
            if isinstance(item, Exception):
                raise RuntimeError('downloader failed') from item
            efta, ok = item
            failed += not ok
            for job in pages_of[efta] if ok else ():
                futures[job] = pool.submit(reocr_participants.read_page, (str(WORK / f'{efta}.pdf'), job[1]))
        print(f'downloads done, {failed} failed', flush=True)
        for n, (job, future) in enumerate(futures.items(), 1):
            texts[job] = future.result()
            if n % 50 == 0:
                print(f'{n}/{len(futures)} pages read', flush=True)

    report, outcomes = [], Counter()
    for case in cases:
        record_id, efta, role, prev, fragment = case
        entry = {'record_id': record_id, 'efta_number': efta, 'role': role, 'prev': prev, 'fragment': fragment, 'readings': []}
        identities = set()
        verdict = 'not_located' if case not in plans else None
        for page, window in plans.get(case, ()):
            if (efta, page) not in texts:
                verdict = 'download_failed'
                break
            reading = {'page': page, 'corpus': window}
            per_engine = {}
            for name in ('tesseract', 'rapidocr'):
                found, line = engine_addresses(texts[efta, page][name], window, fragment)
                ids = {contact_id(e, 'email', key) for e in found}
                per_engine[name] = {final(i) for i in ids if i in known}
                reading[name] = {'line': line, 'addresses': sorted(found), 'known': sorted(i for i in ids if i in known)}
            entry['readings'].append(reading)
            agreed = per_engine['tesseract'] & per_engine['rapidocr']
            if len(agreed) != 1:
                verdict = 'engines_disagree' if per_engine['tesseract'] or per_engine['rapidocr'] else 'no_address_read'
                break
            identities |= agreed
        if verdict is None:
            verdict = 'accepted' if len(identities) == 1 else 'ambiguous_lines'
        entry['verdict'] = verdict
        outcomes[verdict] += 1
        report.append(entry)

    PRIVATE.parent.mkdir(parents=True, exist_ok=True)
    PRIVATE.write_text(json.dumps(report, indent=1, ensure_ascii=False) + '\n')
    os.chmod(PRIVATE, 0o600)
    print(json.dumps(dict(outcomes), indent=1))
    repairs = repairs_from(report, final)
    print('accepted identities:', dict(Counter(final(r['identity']) for r in repairs).most_common(15)))
    if args.apply:
        write_config(repairs)


if __name__ == '__main__':
    main()
