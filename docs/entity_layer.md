# Full-text entity layer

All reviewed company nodes, including incidental and rejected/generic labels, remain in the full graph. `relevance=incidental` is hidden by the viewer's **Show incidental organizations** switch (off initially); rejected labels use `label_quality=not_a_name`. Survivor exclusions still apply. Company edge caps affect the viewer export only.

`extract_entities.py` scans all pages using three processes by default. Each process holds one small spaCy model and reads pages through `corpus.connect`; batches are bounded. The done table makes interrupted scans resumable. Changes to extractor/resolver code, label rules, first names or the gazetteer reset its progress. Changed existing-node inputs are only logged (they change on most builds; a reset would cost a 4–5 h scan). `--limit-pages` processes at most that many pending pages. Requirements are the existing `spacy` / `en_core_web_sm` installation and system `libpsl` (offline public-suffix normalization). Install the matching English model with `.venv/bin/python -m spacy download en_core_web_sm` if it is missing.

```sh
# Isolated validation: all unredacted candidates stay private.
.venv/bin/python scripts/extract_entities.py --workers 2 \
  --sample-docs output/entity_pilot/sample_docs.json \
  --output data/private/entity_pilot/entity_mentions.db \
  --private-output data/private/entity_pilot/entity_spellings.db
.venv/bin/python scripts/build_entities.py \
  --mentions data/private/entity_pilot/entity_mentions.db \
  --private data/private/entity_pilot/entity_spellings.db --output data/private/entity_pilot
.venv/bin/python scripts/publish_entity_contacts.py \
  --source data/private/entity_pilot --output output/entity_pilot --private data/private/entity_pilot

# Usual incremental build includes extraction, resolution and publication.
# Existing raw caches are migrated without resetting extraction progress.
.venv/bin/python scripts/build.py -j 1
```

The mention database is `data/private/entity_mentions.db` (0600; moved from output/.build without re-extraction): `mentions`, `counts` (a view with occurrences/pages/documents), `done`, and the extractor signature. A non-gazetteer key needs two distinct documents to become a full-graph node. Weaker candidates and single-document mentions stay in this database. Gazetteer candidates retain their canonical label/subtype. NER labels remain available in metadata; they are unverified extraction signals. Unknown words have no inferred person identity and use participant nodes flagged `not_a_name`.

`--sample-docs` accepts a JSON array or one document ID per line and requires an output DB different from the resumable corpus DB. It extracts every page of those documents and uses a separate private spelling DB (either `--private-output` or an isolated default under `data/private`). Sample IDs are part of the resume signature. Each run writes a sibling `.metrics.json` with processed pages, characters, elapsed seconds and characters/s, including worker startup and DB writes.

`config/entity_label_rules.json` holds the form-label vocabulary, lowercase function words, a small leftover-label list and geographic code mappings. Short nongazetteer spans, form labels, unit fragments, digit/unit spans and common dictionary phrases without source Title Case are hidden as `not_a_name`. A short state/country code can only fold to one existing location; it cannot create a visible node. Explicit US/USA/U.S. variants canonicalize to United States; NYC/New York City canonicalize to New York unless the existing place layer distinguishes the city. Manhattan remains distinct. Bare legal suffixes and nonletter/symbol noise are discarded before mention storage, with a second defense in the builder for older DBs. OCR-damaged capitalized tokens are retained and can be hidden.

The compact `config/entity_first_names.json` list corrects 2–3 Title Case FAC/GPE/ORG spans to people when the first token is a given name and no existing place/organization or gazetteer entry matches. Single all-caps given names become hidden `person_weak` evidence. Markup and coded report headings are hidden.

Email-like spans are detected before website and NER matching, including OCR @ readings (`®`, `e`/`g` glued before a known mail provider). Shared `build_communications.ocr_repair` mailbox keys and existing merged email IDs resolve them to retained email nodes; unresolved email spans are hidden. Their public spellings are HMAC labels.

Uncurated PERSON, person_weak and unknown-word values use exactly the header-participant HMAC namespace. Their raw spellings stay in `data/private/entity_spellings.db` (mode 0600), and `private_contacts.py` includes them in its encrypted reveal data. Public samples therefore show pseudonyms. Known KG names can retain their public labels. Survivor policy is applied before writing graph nodes/evidence.

`entity_resolution.py` indexes all existing names, original names and aliases from normalized KG, company, place, communication/contact, victim and vehicle inputs. It never reads the combined `graph_entities.json`/`graph.json`: those are written later in the build and contain this layer's own nodes. It uses the company extractor’s legal-form vocabulary, case/punctuation/leading-The normalization, and gazetteer aliases. An existing node’s own name/company key wins over aliases under a different node. Exact legal forms stay distinct; a bare equivalent company key reuses the KG node or the most evidenced existing company. Unrelated ambiguous aliases remain unresolved. Left-truncated tails require at least three words and full-name evidence on the same page; exact full-page anchors are recovered even when NER misses them. The `folds` table records the key, safe span, target/type and rule.

`build_entities.py` groups and scores on disk in SQLite. `output/entity_graph.db` retains all qualifying nodes, mentions, and all co-mention pairs, including pairs omitted from the viewer. Copies count once when any individual copy names both endpoints; pair specificity uses the least crowded such copy. Only documents naming at most 40 entities (`--max-entities-per-doc`, the same cap as the other co-mention layers) form pairs; larger ones (indexes, contact dumps, one with 64k entities) would yield ~9.6 billion pairs. Lift uses the corpus deduplication table's distinct-document population. Missing deduplication data falls back to distinct observed documents. Co-mention is a reading lead, never a relationship claim.

`entity_nodes.json` contains all qualifying nodes; `entity_edges.json` is bounded by `--view-edges` and `--per-node`. The combined graph includes the nodes, and `documents.db` imports their node evidence directly through SQL (no per-node full-text name search). The viewer (`visualize_graph.viewer_slice`) embeds only entity-layer nodes that are an endpoint of an exported link; the rest stay in the full data. Neo4j's default export includes the new graph types; `--full` additionally imports every pair from `entity_graph.db`.

Pilot details, fold counts by target type, 40 fold examples and all newly created visible nodes are in `output/entity_pilot_report.md` and `output/entity_pilot_report.json`, including hidden-node counts and measured characters/s. The 600-document pilot uses 300 documents from EFTA01420000–EFTA01430000 and 300 seeded random documents outside that range. Sample IDs, selection details, isolated graph/mention DBs and logs are under `output/entity_pilot/`.

Contact ownership and publication (GDPR review 2026-11-10): the full-text contact builder retains every contact
node/mention and records ownership/OCR canonicals in `contact_graph.db.resolutions`. The viewer folds those contacts
into their person/organisation, preserving merged identities. Organisation contacts and, by default, contacts of people
already named by the public-names decisions may be plaintext; `config/public_contacts.json` controls the latter class.
Named-person publication also requires independent ownership evidence in `person_contact_evidence`; a merge alone
cannot expose an assistant's mailbox or an unverified OCR variant. The resolver reads existing entity organisations too.
Unresolved/private values use stable `email:<12 hex>` / `phone:<12 hex>` HMAC labels. The separate 0600 key remains
in data/private. `--public` follows the same policy; password unlock keeps real contact values in the encrypted SECRET.

The unredacted aggregation is staged at `data/private/entity_layer/`. `publish_entity_contacts.py` produces the public
entity JSON/SQLite files, hashing incidental literal and OCR contact text (including keys/spans) without removing nodes,
mentions or pairs. Keys containing contact text become distinct opaque HMAC keys, preserving uniqueness and joins.
Original labels and affected fields are in `data/private/entity_contact_lookup.json` and the encrypted viewer reveal;
unlock restores them, including lazily opened metadata. The SQLite publication uses secure_delete so overwritten contact
text does not remain in free page space. Direct extraction/build commands must use the private output paths shown above.
The build orchestrator now runs all steps from `data/private/build_workspace/`; the relative paths in manual commands
refer to that workspace. It keeps raw query/company caches and review snapshots there, exports sanitized artifacts to
the checkout's output/, and audits the release before publishing. No node or evidence row is removed by that boundary.
