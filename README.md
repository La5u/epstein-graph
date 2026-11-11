> **Public copy.** This repository holds the code of a document-discovery tool for the public Epstein
> files (DOJ EFTA release). It contains no corpus data and no build output. To protect privacy (GDPR):
> reviewed records about individuals in `config/` (identity merges, node and label reviews, contact owners,
> survivor lists, ...) ship as empty templates; names, email addresses and phone numbers of private people
> in tests, comments and documentation have been replaced by invented ones. Appearing in these documents
> does not imply wrongdoing. Privacy or removal requests: lasushin@proton.me.

# Corpus and graph analysis starter

## Audit fixes and interpretation

The viewer is a document-discovery tool, not a verified relationship or culpability map. The audit fixes enforce cluster-wide identity exclusions, preserve legitimate short surnames and valid `.co`/`.cm` domains, remove contact-bearing review notes from exported merge rules, and share survivor exclusions across discovery analyses. Exact-name search no longer substitutes a partial match when the exact entity is filtered out. Private viewer/evidence pages are written securely and evidence caches track their inputs.

`config/contact_key_fingerprint.json` pins the local pseudonym namespace without exposing the secret. Restore the matching private key when moving the project; do not delete the pin to silence a mismatch, because reviewed pseudonymous IDs would no longer refer to the same records. Previously exported copies can still contain raw review notes: delete or privately archive those copies rather than publishing them. Pseudonymization is not a guarantee of anonymity.

Current counts belong to each build: consult `output/summary.json` and the layer summaries rather than historical examples below. The involvement summary records input stamps and search parameters; `involvement.db.document_reasons` retains all document-level ranking reasons for verification. These reasons remain heuristic categories, not findings about conduct.

## Documents behind every link and node

`scripts/build_documents.py` (build step `build_documents`) writes `output/documents.db` and `output/documents_summary.json`: for each link, every EFTA document behind it (email headers, co-mentions, survivor-designator documents, emails mentioning a place, EFTA numbers cited in the record), plus non-PDF records (Epstein Exposed flight pages, Jmail emails, Epstein Exposed place/person pages). A link with none of these gets pages whose text names both endpoints, marked "full text names both (fallback)": a reading lead only. Flight-log PDFs cannot be matched to individual flights (handwritten OCR, ~2% match rate), so flights link to their Epstein Exposed record. The viewer shows the first documents of each link and node (with page anchors where known); after unlocking, "All documents" opens the node's private page in `data/private/node_docs/`, listing every link's documents (lists render when opened).

Addresses from email bodies: OCR misreadings of one street are merged (same house number and suffix, small edit distance on long street names), an address node needs at least two linked identities, and addresses that repeat under one name/company line are labelled "(signature)" rather than removed.

Survivor designators: every "Jane Doe N" / "John Doe N" / "Minor Victim-N" in an Epstein-related document becomes a node (`#`/`No.` optional). Numbers restart per matter (CVRA case, 2008 civil suits, draft indictment, estate claims), so they are labels, not a count, and one number can denote different people; each node lists `pages_by_context`.

## Status and layout

- `data/raw/`: structured entity/relationship exports, `knowledge_graph.db`, persons registry, upstream README/license, pinned source commit, and v5.2 release manifest.
- `output/summary.json`: validated graph counts and relationship-type breakdown.
- `output/graph.json`: normalized NetworkX node-link MultiDiGraph, preserving original IDs/records, parallel relationships, dates, weights, and metadata.
- `output/entity_merges.json`: audit trail for 27 exact name/type merge groups (606 raw entities → 546 normalized entities). Casefold, Unicode NFC, and whitespace normalization are applied; surname-only, middle-name and alias variants remain separate pending identity review.
- `output/normalized_entities.json` and `output/normalized_relationships.json`: normalized original KG. All 2,302 original relationship records are retained.
- `output/graph_entities.json` and `output/graph_relationships.json`: combined KG, pseudonymous contacts and bounded communication slice used by analysis and viewer. Raw files are never modified.
- `output/entity_connectivity.csv`: unweighted unique-neighbor counts and relationship-record counts. These measure topology, not wrongdoing.
- `data/raw/full_text_corpus.db`: available only after the large corpus download/decompression finishes.

## Epstein Exposed API coverage pass

```bash
# Read-only: fresh exports, OpenAPI inventory, and six bounded detail samples.
.venv/bin/python scripts/audit_epsteinexposed_api.py --probe-details
# Compare existing person/organization exports against normalized graph entities.
.venv/bin/python scripts/reconcile_api_entities.py
```

Reports: `output/epsteinexposed_api_audit.md` / `.json` and `output/api_entity_coverage.md` / `.json`. Full API responses, hashes, field-level diffs and candidate identities stay under `data/private/` with restrictive permissions. Neither command installs new exports, merges identities, changes legal-status markers or alters the map. The API pass uses a fixed endpoint allowlist, bounded response sizes and retries, and request spacing; it never calls paid endpoints or crawls response links.

The first pass found identical person, flight, location and organization exports. The index advertised 83 locations but returned 84. Sample organization profiles had empty document/person arrays despite nonzero counts; Beller's document/connection samples were also empty. These are retrieval-coverage limitations, not proof of no evidence. Candidate matching is lexical and unreviewed, preserves legal suffixes and bank divisions, and excludes configured survivor identities. Source descriptions and statuses remain upstream assertions, not verified findings.

## Run

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# Bring all outputs up to date after downloads or edits: communications,
# full-text co-mentions, analysis, viewer, audits and tests. Incremental: only
# steps whose inputs changed rerun (a no-op takes under a second; a full
# rebuild about 2 minutes).
scripts/rebuild_all.sh
scripts/rebuild_all.sh -n                    # explain what would run, and why
scripts/rebuild_all.sh --force               # rebuild everything
scripts/rebuild_all.sh --force involvement   # rerun one step (plus whatever it changes)
scripts/rebuild_all.sh visualize_graph       # only that step and what it needs
# Resumable large download, about 4.1 GB compressed plus decompressed storage:
.venv/bin/python scripts/download_corpus.py > output/corpus_download.log 2>&1
```

Build speed: the header-name matcher uses rapidfuzz's C Levenshtein and indexes candidates by first letter, header endpoints are memoized, and FTS lookups run on parallel read-only connections. The page→document map (3M pages) is cached in `output/corpus_page_map.npz` and rebuilt automatically when the corpus file changes. `rebuild_all.sh` runs `scripts/build.py`, which runs independent steps in parallel (`-j`, default 3) and skips up-to-date ones. Each step runs under `scripts/_trace.py`, which records every project file it reads (data, config and the scripts it imports), lists, checks for or writes, so dependencies are never listed by hand. Fingerprints in `output/.build/state.json` are content hashes (size and mtime for files over 256 MB, such as the raw databases), so touching a file does nothing, and a step whose rerun produces identical outputs does not trigger later steps. The viewer's force-directed layout is cached in `output/graph_layout_cache.json` and recomputed only when the graph changes. Outputs are identical to the slower build.

If the download is already running, do not launch a second copy. Check:

```bash
tail -n 10 output/corpus_download.log
ps -p "$(<output/corpus_download.pid)"
```

The downloader checks published sizes and GitHub asset digests when available, runs SQLite quick_check, and writes `output/corpus_schema.json` on completion. It retains the original download parts. The manifest pins the URLs and digests used.

## Identity merges and parse artifacts

Same-entity variants are folded into one node, and every merge is recorded on it (`merged_identities` / `original_entity_ids`):

- **Reviewed merges** are listed in `config/identity_merges.json`, each with a confidence and a reason. Examples: Jeffrey E. Epstein → Jeffrey Epstein; DARREN INDYKE / Indyke → Dfoken K. Indyke; flight-log misspellings (Myhrbold → Myhrvold); surname-only labels with one possible person (Groff, Wexner…). `kept_separate` documents deliberate non-merges: Trump, Clinton, Maxwell, Black, Epstein and Dubin surname labels are ambiguous, and same-flight passengers are different people.
- **OCR variants of email addresses** are merged automatically: `@grnail`, `.corn`, `leevacation@`… become the most frequent spelling (≥10× more records). Raw values are compared in memory only; `output/` stays pseudonymous.
- **Single-person addresses** are merged automatically: an address whose header display names match exactly one graph person (≥20 records) folds into that person. A shared or assistant-run mailbox would also match this rule, so check the recorded rule before relying on it.
- **Header-name variants** are merged automatically: a participant (a name-only From/To/Cc label) becomes a graph person when the cleaned name matches exactly one person. The rules:
  - **exact:** identical once honorifics, credentials (MD, PhD) and short OCR tails ("Lesley Groff a") are removed;
  - **reordered:** surname and given name swapped ("Epstein Jeffrey", "Pritzker, Tom");
  - **given-name variant:** initial, short form or nickname with the same surname, ignoring middle names ("Rich Kahn", "L Black", "Mark L. Epstein");
  - **OCR spelling:** one character off for names of 10+ letters, two for 14+ ("Jefffrey Epstein", "Katarina Vellorin").

  Names that could be two different people stay separate, as do same-surname people with different first names (Cynthia vs Alfredo Rodriguez). Each merge is recorded as `header_name_variant:<rule>` and marked probable.
- `communication_graph.db` keeps raw endpoint IDs in `evidence` and canonical IDs in `canonical_source/target` and `edges`; `identity_map` lists every merge.
- `scripts/find_duplicates.py` writes `output/duplicate_candidates.csv` (initial/spelling/surname-only/alias candidates, plus shared neighbours, shared documents and a same-flight conflict flag) for further review.
- **Parse artifacts:** 73 upstream "persons" are flight-log route strings, FOIA record IDs or `Passenger (n)` placeholders. They are flagged `suspected_parse_artifact` and hidden by default (**Hide suspected parse artifacts**). Flight-log "Last, First First" labels are repaired (Mmifomobel, Nadia Nadia → Ntado Mmifomobel).

## Header-name quality, node audit and re-OCR

**Why so many odd participants:** DOJ's email pages are low-resolution scans (often 96 dpi). The header parser therefore also picked up greetings, sign-offs, mail-client fields and OCR noise as "names".

- **Rules** (`label_quality` in `build_communications.py`, applied to the full database). These are dropped:
  - boilerplate words: regards, untitled, attachment, reply, subject, sent from…;
  - quote headers ("On Fri");
  - mostly-function-word phrases ("where are you");
  - two-letter fragment runs ("oog e a en ar");
  - stroke noise ("III", "MINIII");
  - 1–2 letter labels;
  - mail-client field names ("date-last-viewed").

  A dictionary-based rule was tried and rejected, because it dropped first names such as Larry and Sarah. Role and group labels ("Assistant to Jeffrey Epstein", "Office") are kept but flagged.
- **Recovery before dropping:** garbled labels that are still recognisable as exactly one graph person are linked instead of dropped. This catches OCR that lost ligatures (ff, fi, pt) and read in/rn as m, e.g. "je rey eps em" → Jeffrey Epstein and "dee ak cho ra" → Deepak Chopra.
- **Node-by-node audit:** every non-contact node in the viewer was reviewed by parallel Sol reviewers, in two passes of about 1,200 nodes, and spot-checked. Verdicts live in `config/node_review.json`, keyed by pseudonymous ID with no raw names. Junk is dropped, variants are merged into the named person, and roles/groups are flagged. **Hide parse artifacts & role/group labels** (on by default) hides the flagged nodes.
- **Re-OCR cross-check** (`scripts/reocr_pilot.py`): downloads official PDFs, re-OCRs them with Tesseract at 300 dpi, and checks the OCR-variant address merges. justice.gov shows an "I am 18+" page first; with the user's authorization, the script clicks it once in an isolated headless Chromium. Pilot of 60 documents: a fresh pass read the merged-into address in 65 of 68 cases (96%); 2 scans were too damaged and 1 was unclear. DOJ's embedded text layer reproduced the corpus garble in 61 of 68.
- **Full two-engine re-OCR** (`scripts/reocr_full.py`, needs `requirements-dev.txt`): every OCR-variant address (6,349) checked on one official scan that contains it (4,770 documents, reused where one covers several variants). Up to 8 pages per document, pages holding the variant first, are read three ways: DOJ's text layer, Tesseract, and **RapidOCR** (PaddleOCR models on ONNX, an independent engine). Each pass gets the pilot's verdict; `output/reocr_full.json` reports per-engine counts and Tesseract/RapidOCR agreement. Resumable (`data/external/reocr/full_results.jsonl`); a full run takes many hours, so start it detached and follow `output/reocr_full.log`. Most "neither" verdicts are a third, differently garbled spelling on the scan itself.

## Full-text co-mentions and new leads

```bash
.venv/bin/python scripts/build_comentions.py          # ~40 s over 2.9M pages
.venv/bin/python scripts/cooccur.py "Leon Black" "Jes Staley" --near 50   # same-page proximity
.venv/bin/python scripts/cooccur.py "Leon Black" "Jes Staley" --document  # cross-page document evidence
```

`build_comentions.py` exact-phrase searches the corpus FTS5 index for every graph person, organization, shell company, property, location and aircraft tail number, plus unambiguous aliases from `persons_registry.json`. Victim-labelled people are never searched. Garbled flight-log labels (`Warren ?`, airport strings, FOIA record IDs), single-word names and initial-only names are skipped as too ambiguous. Two entities are linked when they appear in at least 3 of the same EFTA documents.

- `output/new_connections.csv`: ranked pairs that have **no** KG, header or contact link (including via a name variant). Columns: shared documents, a specificity score (documents naming many entities, like manifests and contact lists, count less), lift over chance, and sample EFTA numbers. Current build: 6,593 pairs, 5,621 not otherwise linked.
- `output/comention_graph.db`: every entity→document mention (`mentions`), search phrase hit counts (`search_terms`) and all pairs (`pairs`). Example: `SELECT efta_number FROM mentions WHERE entity_id=? INTERSECT SELECT efta_number FROM mentions WHERE entity_id=?`.
- `output/identity_candidates.csv`: name-compatible variants that co-occur trivially (`Jeffrey E. Epstein` / `Jeffrey Epstein`, `DARREN INDYKE` / `Dfoken K. Indyke`, `Jean-Luc Brunel` / `Jean Luc Brunel`). They are excluded from leads and left unmerged pending review. Document overlap alone never establishes an identity candidate or suppresses a pair.
- The viewer shows the top 600 pairs as dashed amber `co_mentioned_in_documents` edges, with sample documents linked to justice.gov where the corpus has a PDF URL (`--view-edges` changes the count).

**A co-mention is a reading lead, not a relationship.** Names share court filings, news clippings, calendars and contact books. Exact phrases miss misspellings and nicknames, and over-match common names and OCR noise. Read the documents (`cooccur.py`) before drawing any conclusion.

## Victim-evidence proximity and who-was-where (no victim identities)

```bash
.venv/bin/python scripts/involvement.py   # ~1 min; also run by rebuild_all.sh
```

- `output/involvement_ranking.csv`: one row per named person, with these counts:
  - documents that are FBI 302s/reports, police reports or grand-jury material;
  - sworn depositions;
  - pages where the name is within 25 words of victim-context terms (victim, minor, massage, recruit…);
  - documents carrying FOIA privacy codes `(b)(6)`/`(b)(7)(C)`.
  Each count also comes as an enrichment ratio against the person's overall document count. There is also a redaction-density ratio, built from redaction counts only. `professional_role_likely` flags lawyers, prosecutors and journalists, who appear in this evidence because of their jobs.
- `output/presence_timeline.csv`: dated events per person. Flight-log dates, plus schedule mentions: the name within 6 words of appt/lunch/dinner/meeting in a dated calendar or email document. These are `schedule_document_mention` records dated by the document, not extracted appointments: the event could be on a different day, cancelled, hypothetical, or unrelated. Flight samples preserve their relationship IDs and available provenance, but remain incomplete.
- **Same-date mention edges:** people with evidence dated on at least 2 shared days. The viewer shows the top pairs as dashed purple `same_day_document_mentions` edges (legacy viewers used `same_day_presence`). Epstein and staff who write the schedules (assistants present on more than 15% of all days) are excluded from pairing; frequent visitors are kept. Being scheduled on the same day doesn't mean two people met.
- `output/involvement.db`: the ranking, events and co-presence pairs, for SQL.

Redacted text is never read; only document types, redaction counts and visible names are used. Survivors are excluded from every output: upstream victim labels, `victim_of` sources, plus `config/survivors.json` (extendable). Appearing in victim evidence is a reason to read the documents, not a finding.

## Visual graph

Since the GDPR review of 2026-11-10, `scripts/build.py` keeps working caches, reviews and logs under
`data/private/build_workspace/output/`, and exports audited contact-safe artifacts to `output/`.
Manual pipeline commands should run from `data/private/build_workspace/`; edit source files in this checkout and use
build.py to publish. Organisation contacts and contacts of already publicly named people can be plaintext
(`config/public_contacts.json`, named-person switch default ON); other contacts use stable HMAC labels. Password unlock
retains real values. See [the publication policy](docs/PIPELINE.md#811-contact-publication-after-the-gdpr-review-2026-11-10).

Open `output/graph.html` in your browser (offline, no server required):

```bash
xdg-open output/graph.html
```

The viewer is plain HTML/JS with no dependencies, built from `viewer/`: `renderer.js` (WebGL2 dots and lines, dashes, arrows, labels on a 2D canvas, pan/zoom/drag, and the live layout in a web worker), `graph_logic.js` (filters, counts, paths, search; unit-tested in Node), `app.js` (controls and details panel) and the `viewer.html` template. `visualize_graph.py` inlines them, so edits need no build step, just a rerun. Controls sit in the left panel (Find, Show, Hide, Layout); notes on what dots, lines and flags mean are under **About this graph**. **Animate layout** runs a force layout on the largest connected group only. `scripts/bench_viewer.py` measures pan/zoom frame rates (headless, real GPU).

Location nodes start hidden by default; click **location** (or **All types**) to restore them. Property nodes remain enabled. 

Search by name, filter relationship types, zoom/drag, and click nodes or edges to inspect upstream metadata. Clicking a node lists its visible connections (click one to jump to it); clicking an edge shows sample source documents. **Shortest path** finds the fewest-hop route between two names over the currently visible links. Hide Deceased first to route around Jeffrey Epstein's hub. **Only co-mentions not otherwise linked** keeps just the new full-text leads. The layout is precomputed in Python, so the page is interactive in about 0.6 s instead of about 17 s. Color identifies entity type; **dot radius increases from 7 to 34 px** with unique connected entities (undirected, excluding self-loops and repeated relationships). The shared logarithmic size scale updates with relationship filters and excludes status-hidden nodes/edges. Entity-type focus retains faded direct neighbors and does not change the count scale. Hover or inspect a node for its connection count. Size and line width do not represent confidence. Rebuild with:

```bash
.venv/bin/python scripts/build.py -j 2 visualize_graph
```

## Layout and investigation view controls

The layout is precomputed in Python: the main connected component uses a spring layout, and isolated nodes and small components are packed in rows below it. The page opens **frozen**, so it is responsive immediately (about 0.6 s). Dragging moves one dot. **Animate layout** (off by default) runs a force layout with size-aware collision in a background worker; click **Freeze layout** to stop it, and a circle-separation pass with a 12 px gap removes any overlaps. The **Spacing** slider sets its link length. Labels are drawn largest-dot first and skipped where they would overlap; hover or select a dot to always see its name.

Additional reversible filters (all off by default):

- **Hide isolated nodes**: removes nodes with zero unique neighbors under current relationship/status/layer filters.
- **Hide unresolved header labels**: removes pseudonymous `participant` labels, not identified people.
- **Hide co-mention links**: removes only `shares_exported_efta_reference`, not sender/recipient or other typed relationships.

These controls express view preferences, not proof that hidden material is irrelevant.

A real-browser smoke test uses an isolated headless Chromium instance, never your personal Firefox/Chromium profile:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/browser_smoke.py
```

Results and screenshot: `output/browser_smoke.json`, `output/graph_smoke.png`. The dot-overlap measurement is for the settled/frozen view at a specified viewport/data snapshot, not every possible label, zoom or drag state. The post-audit full-view smoke result has 2,273 visible dots, zero dot overlaps and zero JavaScript errors; deceased/isolated filters, programmatic focus links, and freezing are verified. See the generated report and current test log for the latest results.

## Legal-status markers and de-emphasis

Node badges distinguish `[CONV]` (reported conviction), `[CUST]` (reported custody), `[INV]` (reported investigation), and `[DEAD]` (reported deceased). Above the graph, **Hide: CONV / CUST / INV / Deceased** are independent checkboxes. Check one to hide matching people **and all their incident edges**; uncheck to restore them. All are off by default. Connection sizes recalculate without hidden nodes. Search respects status filters rather than silently restoring hidden people. The former status-fading control has been removed.

This is deliberately **incomplete**, not a current legal-status database. The upstream export supplies conviction labels for Ghislaine Maxwell and Harvey Weinstein, but no usable custody or investigation markers. It supplies deceased markers for Jeffrey Epstein and Jean-Luc Brunel; other deaths are not assumed or comprehensively researched. The source does not give status dates. For example, Epstein's upstream `legal_status` says only `deceased`, so the importer does not invent a conviction flag from it. Missing markers, `unknown`, and `no_action` do not mean cleared or exonerated. A conviction is not proof of current imprisonment; allegations, associations and `person_type` labels never generate markers.

Click a marked person to see `legal_status_evidence`, including source link, upstream record ID, and date availability. Counts and warnings are in `output/legal_status_summary.json`. Additional reviewed, dated assertions can be supplied in `config/legal_status_annotations.json`:

```json
{"annotations": [{"entity_id": 123, "status": "in_custody", "source_url": "https://official-source.example/record", "as_of": "2025-01-01", "note": "Dated source statement; not a live custody check."}]}
```

The ID must identify an existing canonical person. Supported statuses are `convicted`, `in_custody`, `under_investigation`, and `deceased`. Use authoritative sources, distinguish historical claims from current status, and rebuild analysis/viewer after changes. The example is a schema illustration, not an actual status assertion. One reviewed annotation is loaded: Jeffrey Epstein's 30 June 2008 Florida guilty plea (DOJ OPR report). Maxwell's custody facility, Brunel's French charges and Alfredo Rodríguez's 2010 obstruction conviction are not loaded, because I couldn't verify an official source for them.

## Survivor nodes

Survivors are a separate `survivor` entity type (red, distinct from `person`), so participant ↔ survivor links can be read directly in the viewer. Config files, scripts and upstream fields keep their `victim` names (`config/victims.json`, `person_type: victim`); only the viewer label says survivor. The `victim_of` edges (red, shown as `survivor_of`) are the upstream survivor-perpetrator allegation records, carrying `abuse_type`, date range and corroboration notes; an allegation recorded upstream is not a proven offence.

- **Who is typed a victim** comes only from curated structured lists — `config/victims.json`, `config/survivors.json`, `persons_registry.json` entries with `category: victim`, and upstream `person_type: victim` or `victim_of` sources. No corpus search, OCR or redaction recovery is used to identify anyone; adding a name only force-types a node that already exists, never de-anonymizes one.
- `config/victims.json` is the "all victim names" list: `names` force-types a matching graph node, and `extra_nodes` can add a standalone victim node for a curated name with no upstream graph node (currently Sbarep Mbacerir, a registry-labelled alleged Nygard victim, isolated with its EFTA reference).
- **Censored upstream designators** (`scripts/build_victims.py`) promote the pseudonyms the corpus actually uses — `Jane Doe #N`, `John Doe #N`, `Minor Victim-N`, `Anonymous Victim` — to victim nodes, and link each one to the graph people it shares EFTA documents with (`victim_document_co_mention`, light-red dashed; a same-document reading lead, not a relationship). Case-citation forms ("Doherty v. John Doe No. 22") are dropped, designators must appear in at least `--min-docs` documents (default 2) and touch the Epstein document set, and links need `--min-shared` shared documents (default 3), capped at `--per-victim`/`--view-edges`. Outputs: `output/victim_nodes.json`, `output/victim_edges.json`, `output/victim_summary.json`. Only the censored pseudonym is used — no redacted text is read and no pseudonym is resolved to a real identity.
- `scripts/victim_policy.py` resolves the curated set; `scripts/build_contacts.py` applies the type (recording `original_entity_type`) and adds the standalone nodes. Survivors stay excluded from co-mention search and involvement rankings as before; the red type is a curated list entry or censored designator, not a corpus-derived identity or proof of any claim.

Rebuild the victim layer with the rest of the chain (`scripts/rebuild_all.sh`); or on its own after `build_comentions.py`:

```bash
.venv/bin/python scripts/build_victims.py --view-edges 800
```

## Pseudonymous contact layer

The viewer includes email and phone type buttons. Contacts use local HMAC-SHA256 IDs; raw contact values and reverse mappings are not exported. The key is `data/private/contact_hmac.key` (mode 0600, ignored by git). Keep it private and backed up: deleting/changing it changes every contact ID. Pseudonyms and EFTA references can still be sensitive; do not treat them as anonymous or publish them indiscriminately. Raw downloaded source files remain private under `data/raw/`.

- The entity-occurrence export contributes 2,497 distinct conservative contact keys: 357 emails and 2,140 phone numbers. The communication metadata below contributes additional email addresses.
- 40 entity-export contacts (17 email, 23 phone) have shared-reference links and appear in the viewer, adding 52 co-mention edges.
- All pseudonymized **entity-export** contacts are in `output/contact_nodes.json`; coverage and counts are in `output/contact_summary.json`. Communication contacts are indexed separately in SQLite.
- Edges named `shares_exported_efta_reference` mean only a matching name label and contact appeared under the same exported EFTA reference. They do **not** establish ownership, communication, identity, or wrongdoing. References are incomplete.
- Name matching is exact normalized label matching, not verified identity resolution. Single-word person labels and ambiguous label matches are excluded.
- Email domain case is normalized; local-part case is preserved. Phone formatting is removed conservatively, with no guessed country codes or extensions. Deduplication may consequently leave equivalent contacts separate.

Rebuild both analysis and viewer with the existing commands; each refreshes the contact layer automatically. Tests: `.venv/bin/python -m unittest discover -s scripts -p 'test_*.py'`.

## Real emails, phone numbers and header names (password unlock)

There is one viewer, `output/graph.html`. It shows pseudonyms until you enter the viewer password, which decrypts the real email addresses, phone numbers, header names and street addresses in that page only (reload to hide them again). Clicking a revealed dot also shows `pseudonym`, `possible_duplicates` and a link to its private document list.

```bash
.venv/bin/python scripts/viewer_secret.py --set-password   # once; prompts, saves data/private/viewer_password (0600)
.venv/bin/python scripts/visualize_graph.py --no-rebuild    # re-encrypt into output/graph.html (seconds)
```

The real values are embedded **encrypted** (PBKDF2-SHA256, 600,000 iterations → AES-256-GCM; decrypted with the browser's WebCrypto), and only for dots in the viewer. Anyone who has the HTML file can try passwords offline, so use a long password and treat the file as private unless that risk is acceptable. `VIEWER=public scripts/rebuild_all.sh` (or `visualize_graph.py --public`) builds a copy with no encrypted names at all. Without a password file the viewer simply has no unlock box. Document-page links point to `data/private/node_docs/`, so they only work on this machine.

`scripts/private_contacts.py` (run by `rebuild_all.sh` unless `VIEWER=public`) writes the reveal table and the review files **only** to git-ignored `data/private/` (mode 0600):

- `viewer_secret.json`: plaintext reveal table and document pages that `visualize_graph.py` encrypts into the viewer.
- `contact_duplicates.csv`: possible duplicate groups side by side, with occurrence counts, raw spellings and whether each is in the viewer. Rules: header names with the same words ignoring order and punctuation (`Smith, John` / `John Smith.`); same address ignoring case; Gmail ignores dots and `+tags`; same address without `+tag`; punycode domain; phone with/without `+`; US number with/without leading `1` (assumed); same last 10 digits.
- `contact_lookup.json`: every pseudonym ID → real value, top raw spellings, occurrence count. It covers every email, phone and header-name participant, including all 188,496 participants in `communication_graph.db`, so IDs in SQL results can be looked up there.

The groups are candidates and are never merged automatically. Do not share these files.

## Message-level communication metadata

Both v5.2 archives are downloaded, SHA-256 verified, decompressed, and SQLite-validated: `data/raw/communications.db` and `data/raw/email_metadata.db`. Schemas are saved in `output/metadata_schemas.json`.

```bash
.venv/bin/python scripts/download_metadata.py
# Full metadata build; streams records rather than loading message text:
.venv/bin/python scripts/build_communications.py --view-edges 1500
.venv/bin/python scripts/analyze_graph.py
.venv/bin/python scripts/visualize_graph.py
```

The build reads all 2,123,219 non-noise rows in `emails_merged`, including recipient strings and structured `[name, email]` pairs. `communications.db` supplies 63 additional directed pair/types absent from merged, with 65 supporting evidence records from 55 messages. Sender addresses embedded in the `sender` header (`Name <user@host>`) are recovered when `sender_email` is empty (94,431 records). Recipient strings are split on both `,` and `;`, and multi-word name-only recipients listed beside addresses are kept. Registry aliases that map one-to-one to a graph person resolve header names. Counts for overlapping pairs are **not** added together; the supplemental policy repairs distinct-pair coverage, not complete message counts. Other overlapping versions (`emails`, `emails_gap`, aggregated pair tables) are not blindly added together. This creates:

- `output/communication_graph.db`: complete derived node, aggregated edge, degree and source-evidence tables. 340,495 directed pair/type edges; 155,795 nodes with edges.
- `output/communication_summary.json`: coverage, parse failures and viewer limits.
- `output/communication_nodes.json` / `communication_edges.json`: **bounded viewer slice**, not the full network. The default is 1,500 highest-record-count sender/recipient pairs plus associated identity links. Increase `--view-edges` and rebuild to expand it; large offline layouts can become slow.

The post-audit combined viewer contains 2,370 nodes and 9,217 relationship records, including 600 full-text co-mention edges; check `output/summary.json` for current counts. Dot sizes and `entity_connectivity.csv` describe this displayed slice, not full-network centrality (the CSV is the unfiltered slice; interactive size also respects relationship/status filters). Communication node details also provide full metadata neighbor counts where available.

Relationship meanings:

- `sent_to` / `cc_to`: parsed header sender → recipient. Arrows are enabled only for these types. Counts are **source message records**, not deduplicated real-world messages; repeated quoted/copied emails can inflate counts.
- `header_label_association`: a known graph label and email address appear together in a parsed header. Exact label matching does not verify account ownership.
- `upstream_resolved_header_identity`: canonical resolver label matches a known graph label, confidence ≥0.9, non-fuzzy method. This is still an upstream identity assertion. Importantly, `email_participants.resolved_entity_id` actually joins `resolved_identities.id`, despite its schema comment suggesting KG IDs; the importer handles the real join.
- `participant` nodes are pseudonymous **unresolved name-only header labels**, not identified people or email accounts. Conservatively rejects malformed labels, very long strings, unsupported punctuation and explicit redaction placeholders. OCR/header extraction errors remain possible.

885,036 non-noise merged message records have usable sender/recipient edges. Missing or malformed endpoints are counted, not invented. Email headers do not establish ownership of phone numbers or physical addresses mentioned in bodies. Phone links still come only from the limited co-mention layer.

Example full-network query (no raw contact values stored):

```sql
SELECT source_id, target_id, relationship_type, record_count
FROM edges
WHERE relationship_type = 'sent_to'
ORDER BY record_count DESC LIMIT 20;
```

Run this against `output/communication_graph.db`. `evidence` retains the source DB/table/row ID, EFTA reference, message index and normalized date for every exported edge record. Subjects, body text, raw header labels and contact addresses are not copied into this DB.

## External email dumps (leaked; labelled separately)

Put published email files under `data/external/emails/<source_name>/` (`*.eml` or `*.mbox`, any subfolders). Example: `data/external/emails/ddosecrets_yahoo_public/`. Then rerun `scripts/rebuild_all.sh`. Only headers (From/To/Cc/Date/Message-ID) are read. Messages are deduplicated by Message-ID, and evidence rows carry `source_db = external:<source_name>`, so leaked material stays distinguishable from official releases. Files are used exactly as published: redaction placeholders never become nodes, and nothing is de-redacted.

**Imported so far:** the Jmail export (`data/external/jmail/emails-slim.ndjson.gz`, SHA-256 checked against Jmail's manifest), limited to Epstein's leaked Yahoo account (`yahoo_2`, non-promotional). That's 5,547 records and 676 sender→recipient pairs not present in the official data. Excluded: the DOJ volumes and House Oversight emails (already in `email_metadata.db`), promotional mail, and Ehud Barak's hacked mailbox. The DDoSecrets Yahoo files are the same mailbox, so they are not imported again (no shared message IDs to deduplicate on). Where a name is visible but its address is redacted (`Name <████>`), the name still links; the hidden part is never recovered.

## Contact normalization coverage

Run `.venv/bin/python scripts/audit_normalization.py` to write `output/normalization_audit.json`. It inventories structured email headers from both metadata databases plus the entity-export contacts, using the same local HMAC key. Only pseudonyms and aggregate counts are emitted.

The current inventory has 21,381 conservative email keys. Formatting normalization reduces 2,238 phone records to 2,140 distinct phone keys. **This is not complete entity/contact resolution.** Names normalize case/Unicode/whitespace, but aliases/middle names/surnames are not guessed. Email domain case is normalized; local-part case is preserved; plus tags and provider-specific dot behavior are not stripped. Phone formatting is stripped, but plus signs are preserved, and countries/extensions are not guessed.

Review-only candidate groups: **307** email local-part case groups, **7** phone plus-prefix groups, **29** possible US country-prefix groups; **0** Unicode/punycode-domain groups. Groups can overlap. These potential duplicates remain unmerged pending mailbox/country context; automatically collapsing them could create false connections. The JSON lists candidate groups by pseudonymous IDs for review, not raw values. Treat it as sensitive.

Why the earlier communication pairs were missing: the first importer used only `emails_merged` for communication edges and used `communications.db` for name associations. It also handled recipient strings but skipped structured `[name,email]` arrays. Both limitations are corrected. The 63 supplemental directed pair/types were absent from the merged source itself; normalization alone could not recover those records. A later fix recovered 94,431 sender addresses embedded in the merged `sender` string. Those records had previously produced no edges, adding about 20,000 linked message records and 5,000 directed pairs. Splitting name-only recipient lists ("Alice Smith; Bob Jones") added about 41,000 more pairs, mostly between pseudonymous header labels.

## Coverage and consistency audit

```bash
.venv/bin/python -B scripts/audit_coverage.py
```

Reports: `output/audit_coverage.md` and `.json`. The initial pre-fix findings are retained as `output/audit_coverage_before_fixes.md` and `.json`.

Checks compare KG SQLite vs JSON, normalized vs original nodes/edges, actual structured participants in `communications.db`, merged email headers, original/gap metadata location keys, extracted contact inventories, full derived SQLite, the viewer slice, and bounded corpus reference lookups. These are **same-project overlapping sources**, not independent external verification, identity adjudication or proof of complete real-world coverage.

The audit caught/fixed dropped structured recipient arrays, 67 missing directed email pair/types and calendar-invalid dates. After rebuilding: all **590/590** conservative valid-address directed pair/types from `communications.db` are present; merged-header replay has zero per-pair count disagreements; zero dangling viewer edges; zero retained invalid calendar dates. All 606 original entity IDs and 2,302 relationships match the KG DB exports. All 2,497 normalized entity-export contacts are retained. Both 100-reference deterministic corpus samples resolve, but these are not random or comprehensive citation checks.

Remaining limitations are recorded, not hidden:

- 16,656 original `emails` keys and 8,644 `emails_gap` keys are absent from merged under exact `(efta_number,message_index)` matching. Indices/extraction versions may differ; these are reconciliation candidates, **not a proven count of missing messages**.
- 43.3% excess header/date signatures suggest substantial repetition; this is not a deduplicated real-world email rate.
- Missing/malformed headers, unresolved labels, incomplete contact refs and parser errors remain. Structured email metadata cannot measure phone/body-mention coverage.
- Viewer still shows a bounded slice; absence from the canvas is not absence from SQLite or the source corpus.
- Current legal statuses are not independently verified.

## Explore locally

```python
import json
import networkx as nx
from pathlib import Path

g = nx.node_link_graph(json.loads(Path('output/graph.json').read_text()))
# Canonical IDs are keys; original IDs/records remain in attributes.
# Inspect edge metadata before interpreting paths.
print(g.number_of_nodes(), g.number_of_edges())
```

Use the corpus SQLite pages as the text/citation base, and structured exports for entity analysis. Inspect the actual corpus schema before assuming document/page join keys. FTS is retrieval, not the analytical dataset. No raw-text NER or victim de-redaction is performed here.

Upstream curation is not independent verification. Preserve source metadata and distinguish assertions, co-mentions, communications, and travel. Relationship weights use different units and must not be interpreted as confidence or combined into one generic strength score. Edge direction is retained as supplied; do not infer causal direction from it. Some upstream records lack page citations.

Neo4j is not installed; this starter follows the NetworkX + SQLite approach. The JSON multigraph can later be mapped to a graph database without discarding provenance. Keep local data private and review the upstream license before redistribution.
