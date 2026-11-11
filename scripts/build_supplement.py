"""Text the corpus does not have, as extra pages for the extraction steps.

    .venv/bin/python scripts/build_supplement.py

Sources (upstream v5.2 release, scripts/download_metadata.py):
  image_analysis_clean.db     text visible in photographed pages, signs and documents
  non_efta_image_analysis.db  the same for images outside the EFTA numbering
  handwriting_transcriptions.db  transcribed FBI 302s and AUSA proffer notes (the
                              corpus OCR of these pages is mostly a Bates stamp)
Only text read off the image is used, never the model's description of the
people in it (a description can guess who someone resembles). Audio/video
transcripts and DS8 spreadsheets are already in the corpus ([TRANSCRIPT] pages).

Writes data/supplement/pages.db: pages(id, efta_number, page_number, text_content,
source), ids from corpus_index.SUPPLEMENT_BASE so they never collide with corpus
page ids. Real names: kept under data/, never in output/.
"""
import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from supplement import SUPPLEMENT, SUPPLEMENT_BASE

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / 'data/raw'
# Answers meaning "nothing legible" rather than text.
NO_TEXT = re.compile(r'^\W*(?:there (?:is|are) no (?:visible |legible |readable )?text|no (?:visible |legible )?text'
                     r'|the text (?:is|on .{0,40} is) not (?:legible|readable|visible)|none\b|n/?a\b)', re.I)
MIN_LETTERS = 12


# Short answers about the image rather than text in it.
FILLER = re.compile(r'not (?:legible|readable|clearly visible)|obscuring any text|\[any visible text\]|too (?:small|blurry)'
                    r'|no (?:other )?(?:visible |legible |readable )?text', re.I)
PREAMBLE = re.compile(r'^(?:the )?(?:visible )?text (?:in|on|visible in) the image (?:reads|is as follows|includes)?[^:\n]{0,60}:\s*', re.I)


def image_text(value):
    """The text an image analysis read: no code fences, preambles or 'no text' answers."""
    text = PREAMBLE.sub('', re.sub(r'```\w*', ' ', value or '').strip())
    if NO_TEXT.match(text) or (len(text) < 200 and FILLER.search(text)) or sum(c.isalpha() for c in text) < MIN_LETTERS:
        return None
    return text


def read(path, query):
    if not path.exists():
        return []
    with closing(sqlite3.connect(f'file:{path}?mode=ro&immutable=1', uri=True)) as db:
        return db.execute(query).fetchall()


def main():
    pages = defaultdict(list)  # (efta, page, source) -> texts
    for name, source in (('image_analysis_clean.db', 'image_text'), ('non_efta_image_analysis.db', 'image_text')):
        for efta, page, text in read(RAW / name, 'SELECT efta_number, page_number, text_content FROM images ORDER BY 1, 2'):
            if efta and (text := image_text(text)):
                pages[efta, page or 1, source].append(text)
    for efta, page, text in read(RAW / 'handwriting_transcriptions.db',
                                 'SELECT efta_number, page_number, transcript FROM transcriptions ORDER BY 1, 2'):
        if efta and text and text.strip():
            pages[efta, page or 1, 'handwriting'].append(text.strip())
    SUPPLEMENT.parent.mkdir(parents=True, exist_ok=True)
    partial = SUPPLEMENT.with_suffix('.partial.db')
    partial.unlink(missing_ok=True)
    with closing(sqlite3.connect(partial)) as db:
        db.execute('CREATE TABLE pages(id INTEGER PRIMARY KEY, efta_number TEXT, page_number INTEGER, text_content TEXT, source TEXT)')
        db.executemany('INSERT INTO pages VALUES (?,?,?,?,?)',
                       [(SUPPLEMENT_BASE + i, efta, page, '\n\n'.join(texts), source)
                        for i, ((efta, page, source), texts) in enumerate(sorted(pages.items()))])
        db.execute('CREATE INDEX pages_efta ON pages(efta_number, page_number)')
        db.commit()
        summary = dict(db.execute('SELECT source, count(*) FROM pages GROUP BY source').fetchall())
        summary['documents'] = db.execute('SELECT count(DISTINCT efta_number) FROM pages').fetchone()[0]
        summary['characters'] = db.execute('SELECT sum(length(text_content)) FROM pages').fetchone()[0]
    partial.replace(SUPPLEMENT)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
