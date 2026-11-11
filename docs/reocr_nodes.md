# Scan-backed node spelling consensus

`scripts/reocr_nodes.py` reads the small decrypted viewer snapshot, never either
combined graph JSON. It selects OCR-looking labels, one/two-edit neighbours,
word-order/punctuation variants, malformed contacts, and labels longer than five
words (excluding address labels). Selection is a review queue, not a merge rule.
`--names` requests substring matches including clean controls; `--all-nodes`
checks every node in a separately curated input list.

```sh
.venv/bin/python -B scripts/reocr_nodes.py --pilot --limit 40 --cached-only
.venv/bin/python -B scripts/reocr_nodes.py --limit 200 --kinds participant email company
.venv/bin/python -B scripts/reocr_nodes.py --names 'Invented Rne' --pages 3 --workers 2
.venv/bin/python -B scripts/reocr_nodes.py --plan-only --limit 40
```

Participants/emails use indexed raw communication evidence and member IDs;
company and entity keys use their mention databases. KG people can also use the
co-mention index. Communication citations to inner Bates pages are located in
their parent document with a bounded indexed predecessor lookup. Every selected
page must contain a close raw-spelling anchor; fuzzy similarity to a different
clause does not qualify. At most one page per document, in up to three documents,
is chosen per node. Cached PDFs are preferred. The page limit for finding a
communication line within a document is 80; direct mention page IDs have no such
limit. No extraction signatures or corpus tables change.

The page is rendered in grayscale, capped at 2200 pixels. A Tesseract TSV pass
locates the matching row. Its bounded line crop is re-read by Tesseract and
RapidOCR, and compared with the matching DOJ text-layer line. Character alignment
projects the raw mention into each reading without offering candidate names to
an engine. Spacing around email dots/@ is normalized, but different spellings do
not receive approximate votes. A correction must differ from the *corpus span*,
not just from the viewer's canonical punctuation. A confident unchanged or
different page blocks a conflicting global alias.

`--onnxtr` adds recognition-only PARSeq on disagreement crops, avoiding docTR's
large page detector. `--luna` asks the vision model to transcribe disagreement
crops without candidate spellings; it is review evidence only and can never
provide the second automatic vote. These options are optional, not needed for
the three default readers. The tool never attempts to recover covered text.

Fresh spawned workers process one page each, default three concurrent workers.
There are at most that many pending futures. Workers and subprocess descendants
are polled against 580 MiB, and the whole OCR pool plus parent against 1400 MiB;
over-limit jobs stop and can be retried with one worker. PDF downloads are
sequential, rate limited, atomic, and validate only their four-byte header. URLs
come from `corpus.document_urls`; the existing headless age-gate helper is used
only if a successful HTTP response is not a PDF. Network errors do not launch a
browser. `--cached-only` performs no network calls.

Private outputs (mode 0600; new directories 0700):

- `data/private/ocr_review/node_reocr.json`: accumulated per-node/page raw
  readings, crop paths, corpus page IDs, votes, agreement, failures, and metrics.
- `data/private/ocr_review/node_pages/`: atomic page/line caches and crops. PDF
  size/mtime, DPI and algorithm version invalidate the cache. Successful lines
  survive interrupted runs; failed reads retry. Repeated node/page checks replace
  earlier checks without double-counting.
- `data/private/ocr_consensus.json`: version 1, `variants` maps raw spelling to
  `{consensus, engines, pages}`. Every page has at least two independent non-LLM
  engines. Conflicting confident pages are withheld. This file is the *only*
  interchange input builds need.

`scripts/ocr_consensus.py` is the standalone stdlib build reader. It validates
per-page engine evidence and refuses ambiguous case-normalized aliases, chains,
and cycles. `correct(value)` is an exact case/whitespace lookup, not a fuzzy
resolver. `pairs(id_to_raw_label)` proposes an alias only if exactly one existing
ID has the literal target spelling. Callers must preserve raw IDs/spellings and
apply their existing separation/review guards. Do not filter or delete raw nodes
or recompute HMACs from corrected names. No hook belongs in either extractor.

## Hooks for the lead

The five files below were deliberately **not edited** by Task C. These are the
exact integration snippets; keep the private spelling file out of public JSON.
The build tracer records the consensus file when the reader loads it, so this
does not need a new extraction dependency or a `build.py` change.

In `build_communications.resolve_identities`, after the reviewed `curated` loop
has populated `mapping`, before automatic address-family rules. The existing
`GuardedMerges` applies separation vetoes and keeps raw communication nodes:

```python
from ocr_consensus import Consensus
labels = {**(entity_names or {}), **(participant_labels or {}), **email_values}
for member, target in Consensus().pairs(labels):
    if member not in mapping:
        mapping[member] = (target, 'ocr_scan_consensus', 'confirmed')
```

In `build_companies.select`, after the reviewed merges and before
`merge_into.update(ocr_spellings(...))`. Use the existing alias/evidence
bookkeeping; do not add a raw-node deletion or discard mention keys:

```python
from ocr_consensus import Consensus
for member, target in Consensus().pairs({i: g['name'] for i, g in groups.items()}):
    if member not in merge_into:
        merge_into[member] = target
```

In `build_entities`, construct one reader outside the aggregation loop; apply the
last line after `original_name = name` and before `hits = index.hits(name)`. Keep
`k`, `spelling`, private raw values, and existing privacy/type handling intact:

```python
from ocr_consensus import Consensus
scan_spellings = Consensus()
name = scan_spellings.correct(name)
```

In `private_contacts.main`, immediately after `lookup = build_lookup(key)`, so
corrections also apply to cached lookups while original `raw_spellings` survive:

```python
from ocr_consensus import Consensus
scan_spellings = Consensus()
for entry in lookup.values():
    if entry['kind'] in ('participant', 'email'):
        entry['value'] = scan_spellings.correct(entry['value'])
```

In `luna_review.load_view`, immediately after constructing `labels`. This
corrects review text without changing IDs or making review labels public:

```python
from ocr_consensus import Consensus
scan_spellings = Consensus()
labels = {i: scan_spellings.correct(value) for i, value in labels.items()}
```

## Pilot, 2026-11-07

Shell downloads were unavailable (`curl: Could not resolve host: www.justice.gov`).
From a seeded 160-node candidate plan, 42 had cached source pages; the first 40
became `data/private/ocr_review/pilot_nodes.json`. Unavailable family examples
remain in `node_pilot_selection.json`, including the Johanaoff/Johananoff names.
The cached cohort includes Cotrin, Martinez and garbled-mailbox examples. This
is a cache-availability sample, not an error-rate estimate for the full corpus.

Across all 27,313 snapshot nodes, selection flagged 9,513 candidates. Overlapping
reasons: 5,128 OCR-looking, 4,402 near one/two-edit spellings, 719 word-order or
punctuation variants, 1,545 long non-address labels, and 337 garbled contacts.
Counts and private pilot selection evidence are in
`data/private/ocr_review/node_suspicion_counts.json` and `node_pilot_selection.json`.

```sh
.venv/bin/python -B scripts/reocr_nodes.py \
  --nodes-from data/private/ocr_review/pilot_nodes.json --all-nodes \
  --limit 40 --cached-only --pages 2 --workers 2
```

The run checked 40 nodes (16 participants, 10 companies, 7 organizations, 5
people, 2 emails), 66 node/page checks on 64 distinct pages. There were 46
unchanged votes, 4 corrected votes, 8 disagreements and 8 unmatched crops.
Thirty-eight nodes had at least one matched crop.
At least two engines agreed on 50/66 checks (75.8%; 86.2% of matched checks);
all three agreed on 14. Two raw spelling variants received usable aliases,
supported by three pages; another corrected page already had the correct
viewer spelling, so added no alias. Both exported repairs are read independently
by Tesseract and RapidOCR. Raw spellings are in the private report only.
Worker peak RSS was 228.8 MiB; the pilot used two workers and no rebuild.

The final full unittest discovery run passed 419 tests in 13.5 seconds, with a
measured 634.8 MiB aggregate RSS peak (23 focused tests for this tool). Tests cover every selection rule, line
anchoring, voting, private output, cache invalidation, indexed provenance and
inner Bates citations.

The optional OnnxTR/Luna paths and downloads still need a network-enabled or
explicit disagreement pilot. Task C exports alias evidence; the lead must add
the hooks and run the rebuild before graph-level merge effects can be measured.
