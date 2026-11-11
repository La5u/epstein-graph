"""Build-only repairs for stored extraction results.

Keep these out of the extraction resume signature: old mention databases need
the same checks as new ones, without repeating a multi-hour corpus scan.
Rejection reasons mean hidden evidence, never removal from the full graph.
"""
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from rapidfuzz.distance import Levenshtein


FILE = re.compile(r'(?<!\w)[\w%+~.-]+\.(?:jpe?g|png|gif|bmp|tiff?|svg|webp|ico|heic|pdf|docx?|xlsx?|pptx?|'
                  r'html?|em[fl]|wm[fz]|emz|txt|rtf|csv|tsv|ics|zip|rar|7z|gz|tgz|xml|json|css|js|'
                  r'wav|mp3|mp4|mov|m4v|avi|mpg|wmv|ogg|exe|dll|bat|ps1|py|aff4|p7s|wpd)(?!\w|\.[A-Za-z0-9])', re.I)
SIGNATURE = re.compile(r'\s*\[signature:\s*[^\]]*\]\s*', re.I)
FORMS = set('co company inc incorporated corp corporation ltd limited llc lp llp pllc pa pc plc ag sa trust foundation holdings fund gmbh nv bv lllp anstalt stiftung establishment sarl spa srl pty kg ab oy as aps sas bvba kft'.split())
GENERIC = set('a an the of on in at by for from to and or with without as is are was were be been being not no yes all any each other such this that these those its it their our your my his her can may shall will must would should could only also neither nor while when where which who whom whose if then than into under over against between through during before after above below within out up down off so do does did have has had having use used section article paragraph clause schedule exhibit appendix page number purpose business material adverse effect subsidiary balance bind contains deduction void products product new york access underlying first second third general partner partnership board directors corporation company limited trust fund foundation holdings pursuant agreement memorandum terms principal summary information investment investments investor investors interests assets amount value payment contribution distribution capital tax income net right rights liability obligations respect related affiliates member members provided including means defined behalf hereby therein thereof thereto hereunder hereof foregoing following concerning certain applicable'.split())
# These describe a class of business or a clause, rather than distinguish a
# company. Apply only when *every* non-form token belongs to this vocabulary.
GENERIC |= set('because manager management aviation cayman services service servicing technology technologies '
               'shipping freight scholarship sciences science storage stores store solutions source '
               'strategies strategy studio studios trading trade wholesale club insurance securities '
               'financial finance international global equity private public mutual money market '
               'small large cap growth value fixed bond debt municipal exempt offshore onshore feeder '
               'master alternative aggregate annual return returns future performance committee fiscal '
               'executive released parties gross asset values currency liquidity maximum warranty '
               'warranties obligations warranty restriction restrictions provision provisions '
               'formation operation operations termination duration liability protect protector '
               'settlement settlor shareholder shares units unit subscriptions subscription '
               'authorization accounting administrators administrator incorporated incorporation '
               'incorporating incorporated deduction deductions limitations limitation summary '
               'registered registration distributions employer employee ordinary qualified '
               'report reports interests agreement amended restated successor specified surety '
               'holding group total other top beginning ending date copies copy name names '
               'record records account held directly bank banking branch branches country state '
               'foreign domestic benefit benefits pension retirement irrevocable revocable '
               'three two one third second first fourth sole signatory authorized signature '
               'available applicable unlimited special purpose acquisition industry industries '
               'construction development realty properties property resource resources '
               'professional associates association partners directors officer officers '
               'person persons incorporated grantor grantors trustees trustee insurance '
               'trusts incorporated establishing establishment general limited liability '
               'companys companies funds foundations businesses yours thereof herein'.split())
# Industry, geography and abbreviation words: a name made only of these plus a
# legal form ("Atlantic Corp.", "Coffee Inc.") identifies no particular firm.
GENERIC.add('abbr')
STUB_WORDS = set('abbr research exchange markets communications entertainment energy education consultants '
               'electronics aircraft transportation leasing support health jet press street division title '
               'protection tech house newspapers publishing airlines motor oil steel venture supply airways electric '
               'payments dynamics productions equipment laboratories emergency car power sales wireless tile prime '
               'designs homes healthcare land residential senior engineering design sportswear transfer coffee '
               'telecommunications facilities developers petroleum chemicals ballet park broadcasting publishers '
               'maintenance industrial retail view select realtors broad pharmaceutical pharmaceuticals agency '
               'portfolio wealth funding series corporate computer telephone machines furniture tobacco hotels '
               'cooling machinery network electronic limousine community air cargo toys food foods data dealers mall '
               'magazine machine collection towing tours transport clearing art fin mgmt ins mfg grp assoc servs '
               'indus intl distrib mortg instl pvt sys techs tr res ill gp '
               'delaware florida usvi us uk ny nyc ca canada island california boston ohio texas canadian european '
               'deutschland atlantic pacific australia australian bermuda dubai fla mass america american atlanta '
               'north south east west northwest southeast northeast southwest'.split())
# Measured against reviewer findings: these words more often sit inside real firm names.
GENERIC -= {'international', 'one', 'equity', 'capital', 'global', 'public', 'principal', 'financial'}
SENTENCE = re.compile(
    r'\b(?:shall|will|must|would|should|could)\s+(?:not\s+)?(?:be|have|do|provide|include|apply|remain|constitute|require|receive|pay|make)\b|'
    r'\b(?:cannot|hereby|thereof|pursuant)\b|'
    r'\b(?:of|on|in|to|by|for|with|against)\s+(?:the|an?|any|each|such|this|our|your)\s+(?:(?:underlying|access|initial|exempted|onshore|offshore|feeder|first)\s+){0,3}(?:company|corporation|fund|trust|limited)\b|'
    r'\b(?:is|are|was|were|has|have)\s+(?:not|been|a|an|the)\b|'
    r'\b(?:motion for|cause of action|no recourse|as defined|private placement memorandum|'
    r'see section|subject to|power to|provided that|your deduction|no assurance|'
    r'representations and warranties|recipient of this|undersigned agrees|'
    r'authorized by|tax consequences|liquidation value|prospective limited|'
    r'conducted by|applicable box|issues update|sec charges|fincen fines|'
    r'under fire|order granting|court costs|case no|which incorporated|'
    r'amended and restated|confidential memorandum)\b|'
    r'^(?:section|article)\s+(?:\d+|[IVX]+|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|fourteenth)\b|'
    r'^(?:trying to|send a|sent from my|(?:best|kind|warm) regards)\b|'
    r'\b(?:cv attached|everyone who|who works for|more fun)\b|'
    r'^(?:mr|mrs|ms|dr)\.?\s+\w+\s+(?:back|is|was|has|will)\b|'
    r'^(?:become a|select and|sign and|consult its|assure that|invest in|investing in|'
    r'effect of|initial closing of|valuation of|examinations of|risks related to|'
    r'regulatory programs|private offering memorandum|principal payment|'
    r'periodic review of|kyc party contact|grantor of|affiliates of|country or state)\b', re.I)


def clean_label(value):
    """Signature metadata is not part of a place's name."""
    return ' '.join(SIGNATURE.sub(' ', value).split())


# A greeting or sign-off read as an entity ("Hello Lesley", "Best Regards",
# "Dear Sir/Madam"). "HI" in capitals (Hawaii), "Hi-Tech" and "Best Buy" stay.
GREETING = re.compile(r'^(?:(?i:hello|hey|dear|greetings|good\s+(?:morning|afternoon|evening|day)|thanks|thank\s+you|'
                      r'best\s+(?:wishes|regards)|kind\s+regards|warm\s+regards|regards)|Hi|hi)\s+(?!(?i:inc|llc|ltd|co|corp|america)\b)\S')


def text_reason(value):
    value = clean_label(value)
    if FILE.search(value):
        return 'file_name'
    if GREETING.match(value) or re.fullmatch(r'(?i:best|kind|warm)\s+regards|regards|thanks|thank\s+you', value):
        return 'salutation'
    junk = entity_junk_reason(value)
    if junk:
        return junk
    if SENTENCE.search(value):
        return 'sentence_fragment'
    return None


def clean_company_name(value):
    """Remove syntactic lead-ins; never remove an arbitrary first name/word."""
    value = clean_label(value)
    # OCR reads the copyright circle as O/0, often glued to its year. A date
    # followed by a trust remains meaningful ("2007 Family Trust").
    value = re.sub(r'^[O0©]\s*((?:19|20)\d{2})\s+(?!.+\btrust\.?$)', r'\1 ', value)
    if re.match(r'^(?:19|20)\d{2}\s+(?:(?:N|S|E|W|North|South|East|West)\.?\s+)?'
                r'(?:\d+(?:st|nd|rd|th)?|[A-Z][A-Za-z\'-]+)\s+'
                r'(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Drive|Dr\.?|Lane|Ln\.?|Way|Place|Pl\.?|Boulevard|Blvd\.?)\b', value, re.I):
        return value  # a street number can happen to look like a year
    previous = None
    while previous != value:
        previous = value
        value = re.sub(r'^copyright\s*(?:©|\(c\)|[c0])?\s*(?:(?:19|20)\d{2}[-–]?\s*)+', '', value, flags=re.I)
        value = re.sub(r'^(?:(?:see|yes|legal name|account title|filer name|esq)\.?\s*[:,-]?\s+|'
                       r'(?:copyright\s*(?:©|\(c\)|c[\W_]*)?\s*|©\s*)?(?:19|20)\d{2}\s+(?!.+\btrust\.?$)|'
                       r'copyright\s*(?:©|\(c\)|c[\W_]*)?\s+|[ID]\s+(?=(?:19|20)\d{2}\b)|I\s+(?=(?-i:[A-Z][A-Z.]{2,}\b)))', '', value, flags=re.I)
    value = re.sub(r'^(?:copyright\s*(?:©|\(c\)|[c0])?\s*(?:(?:19|20)\d{2}[-–]?\s*)+)', '', value, flags=re.I)
    # Unambiguous syntactic phrases can be stripped even without an anchor.
    lead = (r'(?:(?:defendants?|plaintiffs?|respondents?|claimants?)(?:\(s\))?\s*[:,.-]?|'
            r'(?:third[- ]party\s+(?:defendants?|respondents?|claimants?))|'
            r'(?:as\s+)?(?:ceo|cfo|cio|chairman|president|vice president|vice chairman|'
            r'chief (?:executive|financial|operating|product) officer|board member|director|trustees?)\s+of|'
            r'trust agreement\s+(?:of|for)|ttees\s+for|attorneys?\s+for\s+(?:plaintiffs?|defendants?)|'
            r'wire\s+(?:transfer|instructions)')
    # Amount residue: "000 CEMEX Inc.", "$5,000 Acme LLC", "0 2015 X". A leading-zero number is never a name.
    lead += (r'|(?:\$\s*)?0\d*(?:,\d{3})*(?:\s+(?:19|20)\d{2})?(?=\s+[A-Za-z])|\$\s*[\d,.]+(?=\s+[A-Za-z])|\d{1,3}(?:,\d{3})+(?=\s+[A-Za-z])'
             r'|a\s+(?:division|unit|subsidiary|member)\s+of|although|alternate\s+name|company\s+name|examinations\s+of|cf\.|certified\s+copy|corporate\s+defendants'
             r'|chief\s+(?:product|technology|revenue|marketing|legal|investment)\s+officer'
             )
    lead += (r'|co-(?:founders?|chair\w*|head|chief)(?:\s+of(?:\s+the)?)?'
             r'|(?:chair(?:man|person|woman)?|head|partner|principal|owner|founder)\s+of(?:\s+the)?'
             r'|(?:board\s+and\s+ceo|board\s+chairman)\s+of'
             r'|(?:creditor|closing|closes|includes|including|banking specialist(?:\s+\d+)?|'
             r'attorneys\s*for|by and between|bus\.?\s*name|call\s+\d+|comes now plaintiff|[A-Za-z]+-based)'
             # Table headers and prose before a firm: "Principal Payment CIT GROUP INC",
             # "OPERATED 8Y DELTA AIR LINES INC", "Neither FMR LLC", "ACH To Regions Bank".
             r'|principal\s+payment|operated\s+[b8]y|neither|ach\s+to|cleared|manufacturer\s*:?')
    lead += ')'
    previous = None
    while previous != value:
        previous = value
        value = re.sub(r'^' + lead + r'\s+', '', value, flags=re.I)
        value = re.sub(r'^(?:mr\.?|mrs\.?|dr\.?|esq\.?)\s+', '', value, flags=re.I)
        value = re.sub(r'^Ms\.\s+', '', value)  # MS Dealer Service is a firm.
    return value.strip().rstrip('&').strip()


def company_tokens(value):
    """Tokens with legal-form and OCR COMPANY fragments normalized."""
    # Dotted legal forms and initials are squeezed before checking distinctiveness.
    squeezed = value.casefold()
    # Dotted abbreviations ("Mach.", "Assoc.") are not distinctive names.
    squeezed = re.sub(r"\b(?:mach|assoc|ins|ind|indus|tech|techs|servs|intl|grp|comm|fin|instl)\.(?=\s|$)", ' abbr ', squeezed)
    squeezed = re.sub(r"\b(?:ass'n|commc'ns)", ' abbr ', squeezed)
    squeezed = re.sub(r'\b([a-z])\.(?=[a-z]\.)', r'\1', squeezed)
    # OCR COMPANY variants are legal prose, not distinctive proper names.
    squeezed = re.sub(r'\b(?:co[i1l]?[amnp]{2,4}(?:ny|iiy)|cornpany|cornpanys|cornpames|'
                       r'compaiiy|compauy|conipany|conlpany|trusr|trjst|trustcompany)\b', 'company', squeezed)
    return re.findall(r'[a-z0-9]+', squeezed)


def company_label_reason(value, attested=False):
    reason = text_reason(value)
    if reason:
        return reason
    clause = re.search(r'\b(?:and|or)\s+(?:the\s+)?(?:incorporated|foundation|limited|trust)\.?$', value, re.I)
    if clause:
        prefix = re.findall(r'[A-Za-z]+', value[:clause.start()].casefold())
        named_trust = (clause[0].casefold() == 'and trust' and len(prefix) >= 2
                       and any(w not in GENERIC | FORMS for w in prefix)
                       and not set(prefix) & {'mr', 'mrs', 'ms', 'dr'})
        if not named_trust:
            return 'company_clause_fragment'
    if re.match(r'^(?:hi|hello|dear)\s+\w+[.!]\s+(?:trust|company|inc|llc)\b', value, re.I):
        return 'company_clause_fragment'
    tokens = company_tokens(value)
    if tokens[:3] == ['financial', 'trust', 'company'] and all(t in FORMS for t in tokens[3:]):
        return None  # An independently established named firm in this corpus.
    while tokens and tokens[-1] in FORMS:
        tokens.pop()
    stubby = len(tokens) == 1 and tokens[0] in STUB_WORDS
    # M&T, AT&T: letters joined by an ampersand are one distinctive token.
    joined = bool(re.search(r'\b[a-z]+\s*&\s*[a-z]+\b', value, re.I))
    distinctive = [w for w in tokens if w not in GENERIC and not w.isdigit()]
    if attested or joined:
        return None
    if stubby:
        return 'generic_company'
    if not distinctive and re.fullmatch(r'(?:the\s+)?(?:19|20)\d{2}\s+trust', value, re.I):
        return None  # dated/numbered trusts have a distinctive identifier
    if not distinctive:
        return 'generic_company'
    if all(len(w) <= 1 for w in distinctive):
        return 'company_initial_fragment'
    if len(distinctive) == 1 and distinctive[0] == 'ze':
        return 'company_ocr_fragment'
    return None


@lru_cache(maxsize=32768)
def has_company_form(value):
    squeezed = re.sub(r'\b([a-z])\.(?=[a-z]\.)', r'\1', value.casefold())
    tokens = re.findall(r'[a-z0-9]+', squeezed)
    return bool(tokens and tokens[-1] in FORMS)


@lru_cache(maxsize=32768)
def company_spelling_key(value):
    """Equivalent written legal forms, without dropping the form or brand.

    Build-only: extraction IDs and resume signatures keep their original keys.
    Inc/LLC, parent/subsidiary words, fund series and all numbers stay distinct.
    """
    from build_companies import normal_name
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode()
    value = re.sub(r'\bpvt\.?\s+(?=ltd\b|limited\b)', 'private ', value, flags=re.I)
    value = re.sub(r'\bpublic\s+limited\s+company\b', 'plc', value, flags=re.I)
    value = re.sub(r'\blimited\s+liability\s+company\b', 'llc', value, flags=re.I)
    value = re.sub(r'\bnational\s+association\b', 'NA', value, flags=re.I)
    value = re.sub(r'\bs\.?\s*a\.?\s*r\.?\s*l\.?$', 'SARL', value, flags=re.I)
    return normal_name(value)


@lru_cache(maxsize=1)
def _surname_common_words():
    from entity_labels import dictionary_words
    return dictionary_words()


@lru_cache(maxsize=1)
def _surname_proper_words():
    from pathlib import Path
    path = Path('/usr/share/dict/words')
    return {w.casefold() for w in path.read_text().splitlines() if w.istitle()} if path.exists() else set()


def surname_legal_firm(value):
    """A distinctive surname with a practice/company form is not a generic stub.

    A model's low-confidence 'generic' verdict supplies no contrary evidence.
    This deliberately keeps plausible firms visible rather than guessing which
    surname is registered. Sentence/file/generic-word checks still take priority.
    """
    return (bool(re.fullmatch(r"(?:[A-Z](?:['’][A-Z])?[a-z]+(?:[A-Z][a-z]+)*(?:[-'’][A-Z]?[a-z]+)*|[A-Z]{5,})\s+(?:LLP|LLC|PLLC|P\.?A\.?|P\.?C\.?)", value))
            and value.split()[0].casefold() not in _surname_common_words()
            and (not value.split()[0].isupper() or value.split()[0].casefold() in _surname_proper_words())
            and company_label_reason(value) is None)


def noise_prefix(prefix, tail=''):
    """Evidence for a prefix attached to an independently existing company name."""
    if not prefix.strip():
        return True
    if re.fullmatch(r'[1-9]\d*', prefix):
        return False  # building numbers and numbered trusts are meaningful
    if re.search(r'\d', prefix) and re.search(r'\b(?:trust|foundation|fund)\.?$', tail, re.I):
        return False  # a dated trust is a separate identity
    words = re.findall(r'[A-Za-z]+', prefix)
    folded = {w.casefold() for w in words}
    function_words = set('a an the of on in at by for from to and or with as is are was were '
                         'this that yes see legal name copyright esq because while both between '
                         'call buy cf co fbo fao total company'.split())
    if folded and folded <= function_words:
        return True
    if re.search(r'\b(?:esq|partner|president|ceo|cfo|chairman|attorney|counsel|director)\b', prefix, re.I):
        return True
    if re.fullmatch(r'(?:(?:name\s+of\s+(?:the\s+)?)?(?:company|entity|employer|originator)|'
                    r'(?:contact|account)\s+name|(?:dbab\s+)?account\s+name|'
                    r'page\s+\d+\s+of\s+\d+|operated\s+(?:by|8y)|operating\s+agreement\s+of|'
                    r'(?:irs\s+)?forms?\s+(?:w[- ]?[23]|109[69])|'
                    r'(?:intervenor|regarding|regarding\s+the|visit|contact|employer)|'
                    r'liability\s+statement|distribution[-:]?|'
                    r'private\s+client\s+services\s+for\s+acct)', prefix, re.I):
        return True
    if re.fullmatch(r'[\d\s$€£,.%()-]+(?:\s*(?:SHRS|SHARES))?', prefix, re.I):
        return True
    if prefix in {'P', 'C', 'GO', 'Go', 'GT', 'Flight', 'Current', 'Cryan', 'Closing', 'Closes', 'Buy', 'Sell'}:
        return True
    if re.fullmatch(r'[A-Z]{2,5}', prefix) and False:
        return True
    if re.fullmatch(r'(?:(?:USA|U\.S\.A\.|United States|United Kingdom)|'
                    r'(?:To|From|Total|Hi|Hello|Dear|Bonjour|Good morning)|'
                    r'(?:on behalf of|c/o|CO|FBO|FAO|Wire Transfer|Wire Instructions))', prefix):
        return True
    # A sign-off/name plus a printed affiliation delimiter.
    if re.fullmatch(r"[\w .,'’&-]+\s+[I|]", prefix):
        return True
    # A person's full name with a middle initial ("Joel I. Klein News Corp").
    if not re.fullmatch(r'[A-Z][a-z]+\s+[A-Z]\.\s+[A-Z][a-z]+\s*', prefix):
        return False
    if re.search(r'\b(?:trust|foundation|fund)\.?$', tail, re.I):
        return False  # a person's name distinguishes their trust
    # "Thomas J. Costello Landscaping LLC" is a firm named for a person; folding it into a
    # generic "Landscaping LLC" would be wrong. Only a proper-name tail may follow a person.
    words = [w for w in re.findall(r"[A-Za-z']+", tail) if w.casefold() not in FORMS]
    if any(w.casefold() in {'esquire', 'esq', 'pllc'} for w in words):
        return False
    from entity_labels import dictionary_words
    common = dictionary_words()
    return bool(words) and sum(len(w) for w in words) >= 4 and not all(w.casefold() in common for w in words)


def entity_junk_reason(value):
    """Build-only tests for machine fields and boilerplate in older NER DBs."""
    if re.search(r'\b(?:content[- ](?:type|transfer[- ]encoding)|mime[- ]version|dkim[- ]signature|'
                 r'x[- ](?:mailer|originating|received)|filehash(?:type|iype)|msonormal|'
                 r'esmtps?|smtps?|msgreply|inline[- ]images|xmlns|xsd=|doctype)\b', value, re.I):
        return 'email_metadata'
    if re.search(r'(?:\b(?:font[- ](?:family|size|weight)|text[- ](?:align|decoration)|'
                 r'border[- ](?:collapse|width)|mso[-:]\w+|colspan|rowspan|dir|style)\s*[:=]|'
                 r'^</?\w|^BEGIN:(?:VALARM|VEVENT)|^(?:DTSTART|DTEND|TRIGGER|ACTION|PRODID):)', value, re.I):
        return 'markup_or_calendar'
    if re.fullmatch(r'(?:I{4,}|l{5,}|[Xx]{5,}|[?_.=-]{3,})', value):
        return 'ocr_garbage_run'
    if re.fullmatch(r'(?:\d{1,2}[/.-]){2}\d{2,4}\s+page\s+\d+\s+of\s+\d+', value, re.I):
        return 'page_date_heading'
    if re.fullmatch(r'\d+(?:st|nd|rd|th)\s+floor(?:\s+(?:east|west|office))?|'
                    r'(?:mon|tue|wed|thu|fri|sat|sun),?\s+\d{1,2}\s+[a-z]{3}\s+\d{4}|'
                    r'\d{1,2}:\d{2}\s+(?:montag|dienstag|mittwoch|donnerstag|freitag)', value, re.I):
        return 'date_or_unit_fragment'
    if re.fullmatch(r'(?:attachment\s+file\s+type|aba\s+routing|'
                    r'(?:asset\s+account|consolidated\s+financial)\s+statements?|'
                    r'(?:change\s+in\s+investment|net\s+asset)\s+value|'
                    r'(?:annual\s+percentage|interest)\s+(?:yield|rate)|'
                    r'(?:business|private\s+(?:access|business))\s+checking|'
                    r'(?:daily\s+)?ending\s+balance|(?:calculation\s+of|\w+\s+criteria)\s+balance|'
                    r'(?:portfolio\s+activity|premium\s+adjustments)\s*(?:detail)?|'
                    r'(?:case|census)\s+data(?::\s*(?:messages|calendar\s+items))?|'
                    r'(?:settle\s+(?:counter\s+currency|selection\s+method)|'
                    r'settled\s+(?:foreign\s+exchange\s+contracts|securities\s+purchased))|'
                    r'qty/symbol/security|pgp\s+key\s+id|rsa-sha\d+|'
                    r'(?:irs\s+circular\s+230)(?:\s+notice\s+requirement(?::\s*this)?)?|'
                    r'\d+\s+list|(?:tal|total)\s+(?:before\s+tax|checks|value|'
                    r'concentrated\s*&\s*other\s+equity|deposits\s*&\s*credits|'
                    r'fees,?\s+charges\s*&\s*other\s+withdrawals|inflows\s*&\s*outflows|'
                    r'payments\s*&\s*transfers))', value, re.I):
        return 'document_field_or_financial_heading'
    if re.search(r'\b(?:amendment\s+and\s+restatement\s+to|notice\s+of\s+(?:appointment|contract\s+terms|incorporated|limited)|'
                 r'motion\s+for\s+(?:partial\s+)?summary\s+judgment|'
                 r'views\s+of\s+the\s+authors|is\s+committed\s+to\s+maintaining|'
                 r'will\s+(?:no\s+longer\s+be|call)|'
                 r'you\s+for\s+booking|your\s+(?:priced\s+)?portfolio\s+holdings)\b', value, re.I):
        return 'document_boilerplate'
    if re.fullmatch(r'(?:\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d{1,2}[- ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[- ,]+\d{2,4})(?:\s+\d{1,2}:\d{2}.*)?', value, re.I):
        return 'date_text'
    if re.match(r'^(?:envoy[eé] de mon|(?:gesendet|verstuurd|verzonden)(?:\s*:|$)|peut [eê]tre(?:\s|$)|'
                r'ce message (?:et|a)|veuillez (?:trouver|noter)|bonjour\s*[,!]|'
                r'confidentiality notice|delivery status notification|do not reply\b|'
                r'download attachment\b|contact us\s*[•|]|dear .* subscriber\b)', value, re.I):
        return 'correspondence_boilerplate'
    if re.fullmatch(r'(?:print name|departure terminal|destination city|direct (?:fax|phone)|'
                    r'current relationship overview|daily (?:activities|deposit|portfolio) report|'
                    r'ending balance|file under seal|page\s+\d+|customer (?:care|service)|'
                    r'client service team|confidential treatment requested)', value, re.I):
        return 'form_heading'
    if re.fullmatch(r'American Express Travel\s+[A-Z0-9X]{5,}', value, re.I):
        return 'booking_reference'
    # Dates, dated headings, citations: dataset-agnostic, shared in node_cleanup.
    from node_cleanup import text_junk_reason
    junk = text_junk_reason(value)
    if junk:
        return junk
    return None


def company_tail(value, lookup, reason=company_label_reason):
    """Unique independently attested company suffix; lookup returns target IDs."""
    original = value
    value = clean_company_name(value)
    if value != original and not reason(value):
        hits = set(lookup(value))
        if len(hits) == 1:
            return value, hits
    tokens = value.split()
    matches = []
    for start in range(1, len(tokens) - 1):
        tail = ' '.join(tokens[start:])
        prefix = ' '.join(tokens[:start])
        ticker = {'CSCO': 'cisco', 'AAPL': 'apple', 'MSFT': 'microsoft', 'TSO': 'tesoro',
                  'GOOG': 'google', 'GOOGL': 'google', 'AMZN': 'amazon', 'FB': 'facebook'}
        ticker_prefix = prefix in ticker and tail.casefold().startswith(ticker[prefix] + ' ')
        if (noise_prefix(prefix, tail) or ticker_prefix) and not reason(tail):
            hits = set(lookup(tail))
            if hits:
                matches.append((tail, hits))
    identities = set().union(*(hits for _, hits in matches)) if matches else set()
    return (matches[0][0], identities) if len(identities) == 1 else (value, set())


MAIL_PROVIDERS = {'gmail.com', 'googlemail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'aol.com', 'icloud.com', 'mail.com'}
MAIL_GARBLES = {'innail', 'qmail', 'umail', 'thsmail', 'gmad'}
DOMAIN_WORDS = set('the this your with value summary total beginning ending presented income safety on manage managel by'.split())


def website_reason(domain, spellings=(), dictionary=(), attested=False):
    """A bare OCR token is weaker evidence than an explicit URL.

    Spellings, not the normalized key, preserve URL/email context. Mailbox-like
    spellings never establish a website, even if they contain http/www.
    """
    domain = domain.casefold().strip().removeprefix('www.')
    spellings = tuple(spellings) or (domain,)
    email_like = lambda s: bool(re.search(r'[@®]|\bmailto\s*:', s, re.I))
    usable = [s for s in spellings if not email_like(s)]
    if not usable:
        return 'website_in_email'
    explicit = any(re.match(r'^(?:https?://|www\.)', s.strip(), re.I) for s in usable)
    labels = domain.split('.')
    base = labels[-3] if len(labels) >= 3 and '.'.join(labels[-2:]) in {'co.uk', 'org.uk', 'com.au'} else labels[-2] if len(labels) > 1 else domain
    if domain in MAIL_PROVIDERS:
        return None
    if any(p != domain and p.endswith(domain) for p in MAIL_PROVIDERS | {'google.com'}):
        return 'mail_provider_fragment'
    provider_bases = {p.split('.')[0] for p in MAIL_PROVIDERS}
    if base not in provider_bases and len(base) <= 3 and any(p.endswith(base) for p in provider_bases):
        return 'mail_provider_fragment'
    provider_garble = base in MAIL_GARBLES or bool(re.search(r'gmar[l1i]$', base))
    provider_garble |= any(Levenshtein.distance(base.replace('rn', 'm'), p.split('.')[0], score_cutoff=1) <= 1
                         for p in MAIL_PROVIDERS if len(p.split('.')[0]) >= 5)
    if provider_garble or re.search(r'(?:mail|rnail|nail)$', base) and not (explicit or attested):
        return 'mail_provider_lookalike'
    if domain in {'drive.goo', 'drive.goog', 'drive.google'}:
        return 'truncated_domain'
    if attested:
        return None
    if len(base) < 3:
        return 'short_domain_label'
    if not explicit and (base in DOMAIN_WORDS or base in dictionary):
        return 'bare_dictionary_domain'
    from node_cleanup import sentence_break_domain
    if not explicit and sentence_break_domain(domain):
        return 'sentence_break_domain'
    return None



def website_ocr_target(domain, counts):
    """Rare one-letter OCR substitutions fold only to a dominant same-TLD host."""
    if '.' not in domain:
        return domain
    base, suffix = domain.rsplit('.', 1)
    if len(base) < 8:
        return domain
    hits = [other for other, count in counts.items() if other != domain and count >= 5
            and count >= 3 * counts.get(domain, 0) and other.rsplit('.', 1)[-1] == suffix
            and len(other.rsplit('.', 1)[0]) == len(base)
            and Levenshtein.distance(base, other.rsplit('.', 1)[0], score_cutoff=1) == 1
            and any({a, b} <= set(group) for a, b in zip(base, other.rsplit('.', 1)[0]) if a != b
                    for group in ('ce', 'ft', 'il1', '0o', 'nm', 'uv'))]
    return hits[0] if len(hits) == 1 else domain
