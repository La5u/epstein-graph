"""Conservative entity-label cleanup shared by extraction and graph building."""
import json
from pathlib import Path
import re

RULES_PATH = Path(__file__).resolve().parents[1] / 'config/entity_label_rules.json'
RULES = json.loads(RULES_PATH.read_text())


def is_markup(value):
    return bool(re.fullmatch(r'</?[A-Za-z][\w:-]*(?:\s+[^<>]*)?\s*/?>', value.strip()))


def cannot_be_name(value):
    """Drop only bare legal suffixes and spans dominated by nonletter noise.

    Unicode letters count as letters. Garbled capitalized words are preserved;
    hiding uncertain labels is the builder's job.
    """
    if is_markup(value):
        return False  # Retain explicit tags as hidden markup evidence.
    compact = ''.join(c for c in value if not c.isspace())
    suffix = value.strip().casefold().replace('.', '')
    if suffix in RULES['legal_suffixes']:
        return True
    letters = sum(c.isalpha() for c in compact)
    # Digits alone are not names; a digit-heavy product/model label can be one.
    symbols = sum(not c.isalnum() for c in compact)
    # An uppercase letter can be an OCR-damaged proper name. Retain it for
    # hiding in the builder, even when punctuation overwhelms the word.
    return not letters or symbols >= len(compact) / 2 and not any(c.isupper() for c in compact)


def dictionary_words():
    path = Path('/usr/share/dict/words')
    words = path.read_text().splitlines() if path.exists() else []
    # Keep proper names such as May and Rose out of the common-word heuristic.
    proper = {w.casefold() for w in words if w[:1].isupper()}
    return {w for w in words if w == w.casefold()} - proper


def short_label(value):
    return len(''.join(c for c in value if c.isalnum())) <= 3


def label_reason(value, dictionary):
    """Return a hiding reason, never a deletion decision."""
    if re.search(r'<!?/?[A-Za-z][^>]*>|\bDOCTYPE\b', value, re.I) or value.casefold() in {'html', 'body', 'head', 'div', 'span', 'table', 'tbody', 'tr', 'td', 'script', 'stylesheet'}:
        return 'markup'
    if re.search(r'\b[A-Z]{1,8}\d{4,}\b', value) and re.search(r'\b(?:report|statement|summary|schedule)\b', value, re.I):
        return 'document_heading'
    words = re.findall(r"[^\W\d_]+(?:['’][^\W\d_]+)?", value, re.UNICODE)
    folded = [re.sub(r"['’]s$", '', w.casefold()) for w in words]
    if words and words[0] in RULES['lowercase_function_words']:
        return 'lowercase_function_word'
    if short_label(value):
        return 'short_span'
    compact = ''.join(c for c in value if not c.isspace())
    if sum(not c.isalnum() for c in compact) >= len(compact) / 2:
        return 'symbol_noise'
    if folded and all(w in RULES['form_label_words'] for w in folded) and not any(c.isdigit() for c in value):
        return 'form_label'
    if value.casefold() in {s.casefold() for s in RULES['non_name_labels']}:
        return 'configured_label'
    if re.fullmatch(r'(?:suite|ste|apt|apartment|floor|fl|unit|room|rm)\.?\s+#?\s*[\w-]+', value, re.I):
        return 'unit_fragment'
    if re.fullmatch(r'\d+(?:st|nd|rd|th)?\s+(?:floor|fl)\.?', value, re.I):
        return 'unit_fragment'
    if re.search(r'(?<!\w)\d+(?:\.\d+)?\s*(?:vdc|vac|v|ma|a|w|kw|hz|khz|mhz|ghz|kb|mb|gb|tb|mm|cm|kg|mg|ml)\b', value, re.I):
        return 'digits_units'
    if len(value) > 100:
        return 'long_span'
    if words and all(w.casefold() in dictionary for w in words):
        if len(words) == 1 or not value.istitle():
            return 'dictionary_words'
    return None
