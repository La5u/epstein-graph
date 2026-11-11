"""Re-OCR pages behind OCR-variant email merges and cross-verify them.

    .venv/bin/python scripts/reocr_pilot.py [--docs 40] [--dpi 300]

For a random sample of documents whose corpus text produced an address that
was merged as an OCR variant, download the official PDF (justice.gov URL from
VERIFICATION_URLS), read its embedded text layer (pdftotext), render pages
and OCR them again with Tesseract, then compare the addresses each pass reads
(HMAC IDs only; no raw addresses are written):
  confirmed  fresh OCR reads the canonical address the variant was merged into
  same_garble fresh OCR reproduces the garbled variant (damage is in the scan)
  neither    fresh OCR reads neither
PDFs and images stay under data/external/reocr/ (git-ignored).

justice.gov shows an "Are you 18 or older?" page before Epstein files. With the
user's authorization, the script answers it once in an isolated headless
Chromium (the same click a person makes) and reuses that session's cookies.
"""
import argparse
import json
import random
import re
import sqlite3
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from build_communications import valid_email
from build_contacts import contact_id, contact_key
from corpus import document_urls

ROOT = Path(__file__).resolve().parents[1]
EMAIL = re.compile(r'[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+')


def age_verified_cookies(sample_url):
    """Click 'Yes' on the justice.gov age page once; return a Cookie header."""
    import shutil
    from playwright.sync_api import sync_playwright
    executable = shutil.which('chromium-browser') or shutil.which('chromium')
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=executable, headless=True)
        page = browser.new_page(user_agent='Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36')
        page.goto('https://www.justice.gov/age-verify?destination=' + sample_url.split('justice.gov', 1)[1], wait_until='domcontentloaded')
        page.click('#age-button-yes')
        page.wait_for_timeout(2000)
        cookies = page.context.cookies('https://www.justice.gov')
        browser.close()
    return '; '.join(f"{c['name']}={c['value']}" for c in cookies)


def addresses(text, key):
    # Tesseract often brackets the at-sign: "name(@gmail.com", "name@)gmail.com".
    text = re.sub(r'\(?@\)?', '@', text or '')
    return {contact_id(v, 'email', key) for raw in EMAIL.findall(text or '') if (v := valid_email(raw.strip('.,;:<>()[]')))}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--docs', type=int, default=40)
    parser.add_argument('--dpi', type=int, default=300)
    parser.add_argument('--max-pages', type=int, default=3)
    args = parser.parse_args()
    key = contact_key()
    db = sqlite3.connect(f'file:{ROOT / "output/communication_graph.db"}?mode=ro', uri=True)
    variants = dict(db.execute("SELECT member_id, canonical_id FROM identity_map WHERE rule='ocr_variant_of_more_frequent_address'"))
    resolved = dict(db.execute('SELECT member_id, canonical_id FROM identity_map'))
    # canonical_id may be a person (kg:N) after chaining; compare at the email level too.
    docs = {}
    for efta, source, target in db.execute("SELECT efta_number, source_id, target_id FROM evidence WHERE source_db='email_metadata.db' AND efta_number IS NOT NULL"):
        for member in (source, target):
            if member in variants:
                docs.setdefault(efta, set()).add(member)
    random.seed(7)
    sample = random.sample(sorted(docs), min(args.docs, len(docs)))
    urls = document_urls(sample)
    # Every address ID that resolves to the same identity as each variant.
    same_identity = {}
    for member, canonical in resolved.items():  # any rule, incl. reviewed merges
        same_identity.setdefault(canonical, set()).add(member)
    work = ROOT / 'data/external/reocr'
    work.mkdir(parents=True, exist_ok=True)
    results, outcome = [], Counter()
    first = next((urls[e] for e in sample if urls.get(e, '').startswith('https://www.justice.gov/')), None)
    cookie = age_verified_cookies(first) if first else ''
    for efta in sample:
        url = urls.get(efta)
        if not url or not url.startswith('https://www.justice.gov/'):
            outcome['no_official_url'] += 1
            continue
        pdf = work / f'{efta}.pdf'
        if not pdf.exists():
            done = subprocess.run(['curl', '-sL', '--fail', '--max-time', '120', '-A', 'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36',
                                   '-H', f'Cookie: {cookie}', '-o', str(pdf), url])
            if done.returncode or not pdf.exists() or pdf.read_bytes()[:4] != b'%PDF':
                pdf.unlink(missing_ok=True)
                outcome['download_failed'] += 1
                continue
        layer = subprocess.run(['pdftotext', '-l', str(args.max_pages), str(pdf), '-'], capture_output=True, text=True).stdout
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['pdftoppm', '-r', str(args.dpi), '-l', str(args.max_pages), '-png', str(pdf), f'{tmp}/p'], check=True)
            fresh = ''.join(subprocess.run(['tesseract', str(png), '-', '--psm', '6'], capture_output=True, text=True).stdout
                            for png in sorted(Path(tmp).glob('p*.png')))
        layer_ids, fresh_ids = addresses(layer, key), addresses(fresh, key)
        for member in docs[efta]:
            canonical = variants[member]
            family = same_identity.get(canonical, set()) | {canonical}
            correct = (family - {member}) & fresh_ids
            if correct and member not in fresh_ids:
                verdict = 'confirmed'
            elif member in fresh_ids:
                verdict = 'same_garble'
            else:
                verdict = 'neither'
            outcome[verdict] += 1
            results.append({'efta_number': efta, 'variant_id': member, 'canonical_id': canonical, 'verdict': verdict,
                            'pdf_text_layer_has_variant': member in layer_ids, 'pdf_text_layer_has_correct': bool((family - {member}) & layer_ids)})
    summary = {'documents_sampled': len(sample), 'outcomes': dict(outcome),
               'text_layer_matches_corpus_garble': sum(r['pdf_text_layer_has_variant'] for r in results),
               'note': 'confirmed = an independent Tesseract pass reads the merged-into address on the official scan.'}
    (ROOT / 'output/reocr_pilot.json').write_text(json.dumps({'summary': summary, 'results': results}, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
