# Agent rules for this repo

## Searching (the tree is ~39 GB: data/raw 30G, output 4.8G)
- Use `rg`, never `grep -R` / `grep -r`. rg uses all cores and skips binary files and git-ignored paths.
- Search many IDs or names in ONE pass: `rg -n -e id1 -e id2 -e id3 PATHS`, or put them in a file and use
  `rg -n -F -f ids.txt PATHS`. Never loop one search per ID over the tree.
- `data/` and `output/` are git-ignored, so `rg` from the repo root does not search them. Name them as paths when
  you need them (`rg -n foo output config scripts`); explicit paths are searched.
- Pick the narrowest paths. Code and settings: `scripts config viewer`. Build results: `output`. Don't rg `data/raw`
  (30G, mostly SQLite): query the corpus through its full-text index, `data/raw/full_text_corpus.db`
  (`scripts/corpus.py`, `scripts/adapters/efta.py`), with many names per query.
- Add `-l`, `-m N` or `--max-filesize` when you only need to know where something is.
