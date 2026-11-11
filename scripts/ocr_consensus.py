"""Private, exact spelling aliases from independently re-read source lines.

No extraction dependency: import this only in build/resolution or private reveal
steps. A missing file is an empty map. Never use this to remove a raw node, change
its HMAC, or publish the raw spellings. Pass corrected values to matching only.
"""
from difflib import SequenceMatcher
import json
from pathlib import Path
import re

PATH = Path(__file__).resolve().parents[1] / 'data/private/ocr_consensus.json'
OCR_ENGINES = frozenset({'doj', 'tesseract', 'rapidocr', 'onnxtr'})


def spelling_key(value):
    """Case/whitespace only; punctuation, word order and mailbox dots matter."""
    return ' '.join(value.split()).casefold()


EMAIL = re.compile(r'[^@\s]{2,}@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}', re.I)


def plausible_reading(raw, target, min_ratio=0.78):
    """A re-read must be the SAME span: similar letters/digits and length.

    Projection onto the engine line can pick a neighbouring or partial span
    ('appaloosa management lp' -> 'Appaloosa', 'nosa inc.' -> 'C'); those are
    not readings of this spelling. An address only becomes a complete address.
    """
    a, b = (re.sub(r'[^a-z0-9@]', '', v.casefold()) for v in (raw, target))
    if not a or not b or not 0.8 <= len(b) / len(a) <= 1.25:
        return False
    if ('@' in raw or '@' in target) and not EMAIL.fullmatch(target.strip()):
        return False
    digits = [re.sub(r'\D', '', v) for v in (raw, target)]
    if all(digits) and digits[0] != digits[1]:
        return False  # 'WAWW 15 LLC' is not 'WAWW17LLC'
    last = [re.findall(r'[a-z0-9]{2,}', v.casefold()) for v in (raw, target)]
    if '@' not in target and last[0] and last[1] and SequenceMatcher(None, last[0][-1], last[1][-1]).ratio() < 0.5 \
            and not ''.join(last[1]).endswith(last[0][-1]):
        return False  # 'Millennium USA LP' is not 'Millennium Funds'
    return SequenceMatcher(None, a, b).ratio() >= min_ratio


class Consensus:
    def __init__(self, path=None):
        self.aliases = {}
        path = Path(path) if path is not None else PATH  # module PATH read at call time (tests repoint it)
        if not path.exists():
            return
        payload = json.loads(path.read_text())
        if payload.get('version') != 1:
            raise ValueError('Unsupported OCR consensus version')
        candidates, conflicts = {}, set()
        for raw, evidence in payload.get('variants', {}).items():
            target = evidence.get('consensus')
            pages = evidence.get('pages', [])
            # Two independent non-LLM engines must agree ON EACH page. An
            # engine union across pages (or two Tesseract runs) is not a vote.
            if not isinstance(target, str) or not target.strip() or not pages:
                continue
            if any(len(set(p.get('engines', [])) & OCR_ENGINES) < 2
                   or not isinstance(p.get('page_id'), int) for p in pages):
                continue
            key = spelling_key(raw)
            if key in candidates and spelling_key(candidates[key]) != spelling_key(target):
                conflicts.add(key)
            candidates[key] = target
        # Chaining would invent a correction never read by two engines for
        # this raw spelling. Cycles and chains are deliberately not followed.
        for key, target in candidates.items():
            # Implausible readings still count above as chain/cycle evidence.
            if (key not in conflicts and key != spelling_key(target)
                    and spelling_key(target) not in candidates and plausible_reading(key, target)):
                self.aliases[key] = target

    def correct(self, spelling):
        return self.aliases.get(spelling_key(spelling), spelling) if isinstance(spelling, str) else spelling

    def evidence(self, spelling):
        """Whether this exact spelling has a usable scan-backed correction."""
        return spelling_key(spelling) in self.aliases

    def pairs(self, labels):
        """(raw id, existing target id), only with one literal target spelling.

        The caller must run these through its usual never-merge/review guards.
        The raw labels/IDs remain intact. No targets are created or chosen by
        similarity, and targets are not themselves corrected aliases.
        """
        by_label = {}
        for identity, value in labels.items():
            if isinstance(value, str) and not self.evidence(value):
                by_label.setdefault(spelling_key(value), set()).add(identity)
        for identity, value in labels.items():
            if isinstance(value, str) and self.evidence(value):
                targets = by_label.get(spelling_key(self.correct(value)), set()) - {identity}
                if len(targets) == 1:
                    yield identity, next(iter(targets))
