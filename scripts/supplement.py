"""Extra pages beside the corpus (scripts/build_supplement.py: text read off images,
handwriting transcriptions). Page ids start at SUPPLEMENT_BASE so they never collide
with corpus page ids."""
import sqlite3
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPPLEMENT = ROOT / 'data/supplement/pages.db'
SUPPLEMENT_BASE = 100_000_000


def supplement_pages(path=SUPPLEMENT):
    """[(id, efta, page, text)] of the extra pages, [] before build_supplement ran."""
    if not path.exists():
        return []
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
        return db.execute('SELECT id, efta_number, page_number, text_content FROM pages ORDER BY id').fetchall()
