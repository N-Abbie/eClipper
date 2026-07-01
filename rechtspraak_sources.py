"""
rechtspraak_sources.py
======================
Detecteert ECLI's (met en zonder hyperlink) in verschillende bronbestanden en
levert Entry-objecten op voor rechtspraak_core.process_entries.

Ondersteunde bronnen:
  - .pdf            (link-annotaties + tekst; bij een L&S-uitsprakenoverzicht
                     ook de samenvatting + de "Samenvatting door ..."-attributie)
  - .eml            (e-mail; links uit <a href> en uit platte tekst)
  - .msg            (Outlook; vereist het optionele pakket 'extract-msg')
  - .html / .htm    (links uit <a href> + zichtbare tekst)
  - .txt / .md      (ECLI's; "met link" = ECLI die in een rechtspraak-URL staat)

Onderscheid met/zonder link:
  - "met link"   = de ECLI is het doel van een hyperlink (PDF-annotatie of href).
  - "zonder link"= de ECLI komt alleen als platte tekst voor.
Standaard exporteert de tool alleen ECLI's met link; met de optie
include_unlinked worden ook de losse tekst-ECLI's meegenomen.

Alleen stdlib + pypdf. extract-msg is optioneel en wordt lui geimporteerd.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from rechtspraak_core import Entry, ECLI_RE, MAIN_RE, MONTHS

SUPPORTED_EXT = {".pdf", ".eml", ".msg", ".html", ".htm", ".txt", ".md"}

# Een uitspraak-"entry"-regel begint met een datum gevolgd door een ECLI
# (bv. "Rechtbank Oost-Brabant 21 april 2026, ECLI:NL:RBOBR:2026:3262").
# Voetnoten ("1ECLI:NL:..., L&S 2025-3/G19.") hebben GEEN datum vooraf en
# worden hierdoor niet als entry gezien.
_DATE_ECLI = re.compile(
    r"\d{1,2}\s+(?:" + "|".join(MONTHS) + r")\s+\d{4},?\s*ECLI:NL:",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+")


@dataclass
class HarvestResult:
    entries: list = field(default_factory=list)   # list[Entry], ontdubbeld op ECLI
    n_linked: int = 0
    n_unlinked: int = 0
    is_ls_overview: bool = False
    summaries_found: int = 0
    sources: list = field(default_factory=list)   # bestandsnamen
    errors: list = field(default_factory=list)     # (bestandsnaam, melding)


# ---------------------------------------------------------------------------
# Hulpfuncties
# ---------------------------------------------------------------------------

def _ecli_from_url(url: str):
    m = ECLI_RE.search(url or "")
    return m.group(0).upper() if m else None


def _eclis_in_text(text: str) -> set:
    return {m.group(0).upper() for m in ECLI_RE.finditer(text or "")}


def _main_title_date(text: str) -> dict:
    """ECLI -> (titel, YYYYMMDD) op basis van de 'datum, ECLI, titel.'-patronen."""
    norm = re.sub(r"\s+", " ", text or "")
    out: dict = {}
    for m in MAIN_RE.finditer(norm):
        day, month, year, ecli, title = m.groups()
        ecli = ecli.upper()
        if ecli not in out:
            out[ecli] = ((title or "").strip(),
                         f"{int(year):04d}{MONTHS[month.lower()]:02d}{int(day):02d}")
    return out


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def _pdf_linked_eclis(reader) -> set:
    """ECLI's die het doel zijn van een hyperlink-annotatie."""
    out: set = set()
    for page in reader.pages:
        annots = page.get("/Annots")
        if not annots:
            continue
        try:
            annots = annots.get_object()
        except Exception:
            pass
        for a in annots:
            try:
                obj = a.get_object()
            except Exception:
                continue
            if obj.get("/Subtype") != "/Link":
                continue
            action = obj.get("/A")
            if not action:
                continue
            try:
                action = action.get_object()
            except Exception:
                pass
            uri = action.get("/URI")
            e = _ecli_from_url(str(uri)) if uri else None
            if e:
                out.add(e)
    return out


def _pdf_styled_pages(reader):
    """
    Reconstrueert per pagina de regels met stijl-informatie.
    Geeft list[list[line]] terug; line = (y, runs) met
    runs = list[(bold, italic, size, text)].
    """
    pages = []
    for page in reader.pages:
        runs = []

        def visit(text, cm, tm, fontDict, fontSize):
            if not text or not text.strip():
                return
            base = str(fontDict.get("/BaseFont", "")) if fontDict else ""
            bold = "bold" in base.lower()
            ital = "italic" in base.lower() or "oblique" in base.lower()
            runs.append((round(tm[5], 1), tm[4], bold, ital, round(fontSize, 1), text))

        try:
            page.extract_text(visitor_text=visit)
        except Exception:
            pages.append([])
            continue

        runs.sort(key=lambda r: (-r[0], r[1]))
        lines = []
        cur, cy = [], None
        for y, x, b, i, s, t in runs:
            if cy is None:
                cur, cy = [(b, i, s, t)], y
            elif abs(y - cy) <= 2.5:
                cur.append((b, i, s, t))
            else:
                lines.append((cy, cur))
                cur, cy = [(b, i, s, t)], y
        if cur:
            lines.append((cy, cur))
        pages.append(lines)
    return pages


def _line_text(line) -> str:
    return "".join(t for _, _, _, t in line[1])


def _line_runs(line):
    """Aaneengesloten runs met dezelfde stijl samenvoegen -> [(text, bold, italic)]."""
    merged = []
    for b, i, _s, t in line[1]:
        if merged and merged[-1][1] == b and merged[-1][2] == i:
            merged[-1][0] += t
        else:
            merged.append([t, b, i])
    return [tuple(r) for r in merged]


def _max_size(line) -> float:
    sizes = [s for _, _, s, t in line[1] if t.strip()]
    return max(sizes) if sizes else 0.0


def _all_bold(line) -> bool:
    runs = [r for r in line[1] if r[3].strip()]
    return bool(runs) and all(r[0] for r in runs)


def _all_ital(line) -> bool:
    runs = [r for r in line[1] if r[3].strip()]
    return bool(runs) and all(r[1] for r in runs)


def _upper_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def _is_category(line, txt: str) -> bool:
    return (_all_bold(line) and _upper_ratio(txt) >= 0.6
            and len(txt) >= 5 and not ECLI_RE.search(txt))


def _parse_ls(pages) -> dict:
    """
    Parseert een L&S-uitsprakenoverzicht.
    Geeft dict ecli -> (attribution, summary_paras) terug, waarbij
    summary_paras = list[list[(text, bold, italic)]] (alinea's van runs),
    met de identificerende eerste regel weggelaten.
    """
    # Vlakke lijst met paginanummer erbij (voor alinea-detectie per pagina).
    flat = []  # (page_idx, y, line)
    for pi, lines in enumerate(pages):
        for ln in lines:
            flat.append((pi, ln[0], ln))

    result: dict = {}
    cur_attr = ""
    n = len(flat)
    i = 0
    while i < n:
        _pi, _y, ln = flat[i]
        txt = _line_text(ln).strip()
        if not txt:
            i += 1
            continue

        # Rubriekkop + attributie (de attributie staat op de regel eronder).
        if _is_category(ln, txt):
            j = i + 1
            while j < n and not _line_text(flat[j][2]).strip():
                j += 1
            if j < n and _all_ital(flat[j][2]) and "," in _line_text(flat[j][2]):
                cur_attr = re.sub(r"\s+", " ", _line_text(flat[j][2]).strip())
                i = j + 1
                continue
            i += 1
            continue

        # Begin van een uitspraak (identificerende regel met datum + ECLI).
        if _DATE_ECLI.search(txt):
            m = ECLI_RE.search(txt)
            ecli = m.group(0).upper() if m else None

            k = i + 1
            # Eventuele doorgelopen (cursieve) partijnaam overslaan.
            skipped = 0
            while k < n and skipped < 3:
                lk = flat[k][2]
                tk = _line_text(lk).strip()
                if not tk:
                    k += 1
                    continue
                if _all_ital(lk) and not _DATE_ECLI.search(tk):
                    k += 1
                    skipped += 1
                    continue
                break

            paras = []
            cur_runs = []
            prev_pi, prev_y = None, None
            while k < n:
                kpi, ky, lk = flat[k]
                tk = _line_text(lk).strip()
                if not tk:
                    k += 1
                    continue
                # Stop bij de volgende uitspraak of rubriek.
                if _DATE_ECLI.search(tk) or _is_category(lk, tk):
                    break
                # Voetnoten (kleiner lettertype) overslaan.
                if _max_size(lk) and _max_size(lk) < 10.0:
                    k += 1
                    continue
                # Losse paginanummers overslaan.
                if re.fullmatch(r"\d{1,3}", tk):
                    k += 1
                    continue

                # Alinea-einde detecteren op basis van een grote y-sprong
                # binnen dezelfde pagina.
                if (prev_pi == kpi and prev_y is not None
                        and (prev_y - ky) > 20 and cur_runs):
                    paras.append(cur_runs)
                    cur_runs = []

                runs = _line_runs(lk)
                if cur_runs:
                    cur_runs.append((" ", False, False))
                cur_runs.extend(runs)
                prev_pi, prev_y = kpi, ky
                k += 1

            if cur_runs:
                paras.append(cur_runs)
            if ecli and paras:
                result[ecli] = (cur_attr, paras)
            i = k
            continue

        i += 1
    return result


def _harvest_pdf(path: str) -> HarvestResult:
    from pypdf import PdfReader
    name = Path(path).name
    reader = PdfReader(path)

    flat_text = "\n".join(p.extract_text() or "" for p in reader.pages)
    linked = _pdf_linked_eclis(reader)
    text_eclis = _eclis_in_text(flat_text)
    all_eclis = linked | text_eclis

    titles = _main_title_date(flat_text)

    is_ls = "uitsprakenoverzicht" in flat_text.lower()
    ls = {}
    if is_ls:
        try:
            ls = _parse_ls(_pdf_styled_pages(reader))
        except Exception:
            ls = {}

    entries = []
    summaries = 0
    for e in sorted(all_eclis):
        title, date = titles.get(e, ("", ""))
        attr, paras = ls.get(e, ("", []))
        if paras:
            summaries += 1
        entries.append(Entry(
            ecli=e, linked=(e in linked),
            overview_title=title, overview_date=date,
            attribution=attr, summary_paras=paras, source_name=name,
        ))

    return HarvestResult(
        entries=entries,
        n_linked=len(linked),
        n_unlinked=len(all_eclis - linked),
        is_ls_overview=is_ls,
        summaries_found=summaries,
        sources=[name],
    )


# ---------------------------------------------------------------------------
# HTML / e-mail / tekst
# ---------------------------------------------------------------------------

class _LinkTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.hrefs.append(v)

    def handle_data(self, data):
        if data:
            self._text.append(data)

    @property
    def text(self):
        return " ".join(self._text)


def _parse_html(html_text: str):
    """Geeft (linked_eclis, zichtbare_tekst) terug uit een HTML-fragment."""
    p = _LinkTextParser()
    try:
        p.feed(html_text or "")
    except Exception:
        pass
    linked = set()
    for h in p.hrefs:
        e = _ecli_from_url(h)
        if e:
            linked.add(e)
    return linked, p.text


def _linked_from_plaintext(text: str) -> set:
    """ECLI's die in een (rechtspraak-)URL in platte tekst staan = 'met link'."""
    out = set()
    for m in _URL_RE.finditer(text or ""):
        e = _ecli_from_url(m.group(0))
        if e:
            out.add(e)
    return out


def _harvest_eml(path: str):
    import email
    from email import policy
    with open(path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)

    linked, alltext = set(), []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "text/html":
            try:
                l, t = _parse_html(part.get_content())
                linked |= l
                alltext.append(t)
            except Exception:
                pass
        elif ctype == "text/plain":
            try:
                alltext.append(part.get_content())
            except Exception:
                pass
    text = "\n".join(alltext)
    linked |= _linked_from_plaintext(text)
    return linked, text


def _harvest_msg(path: str):
    try:
        import extract_msg  # optioneel
    except ImportError:
        raise RuntimeError(
            "Voor .msg-bestanden is het pakket 'extract-msg' nodig "
            "(pip install extract-msg). Of bewaar de e-mail als .eml of .pdf "
            "en sleep die naar het programma."
        )
    m = extract_msg.Message(path)
    linked, alltext = set(), []
    html_body = getattr(m, "htmlBody", None)
    if html_body:
        if isinstance(html_body, bytes):
            html_body = html_body.decode("utf-8", "ignore")
        l, t = _parse_html(html_body)
        linked |= l
        alltext.append(t)
    if getattr(m, "body", None):
        alltext.append(m.body)
    try:
        m.close()
    except Exception:
        pass
    text = "\n".join(alltext)
    linked |= _linked_from_plaintext(text)
    return linked, text


def _harvest_textlike(path: str, is_html: bool):
    raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    if is_html:
        linked, text = _parse_html(raw)
        linked |= _linked_from_plaintext(text)
        return linked, text
    return _linked_from_plaintext(raw), raw


# ---------------------------------------------------------------------------
# Publieke API
# ---------------------------------------------------------------------------

def harvest_file(path: str) -> HarvestResult:
    """Detecteert ECLI's in een enkel bestand en levert een HarvestResult op."""
    ext = Path(path).suffix.lower()
    name = Path(path).name

    if ext == ".pdf":
        return _harvest_pdf(path)

    if ext == ".eml":
        linked, text = _harvest_eml(path)
    elif ext == ".msg":
        linked, text = _harvest_msg(path)
    elif ext in (".html", ".htm"):
        linked, text = _harvest_textlike(path, is_html=True)
    elif ext in (".txt", ".md"):
        linked, text = _harvest_textlike(path, is_html=False)
    else:
        raise RuntimeError(f"Niet-ondersteund bestandstype: {ext or '(geen)'}")

    text_eclis = _eclis_in_text(text)
    all_eclis = linked | text_eclis
    titles = _main_title_date(text)  # zelden aanwezig buiten PDF, maar schaadt niet

    entries = []
    for e in sorted(all_eclis):
        title, date = titles.get(e, ("", ""))
        entries.append(Entry(ecli=e, linked=(e in linked),
                             overview_title=title, overview_date=date,
                             source_name=name))

    return HarvestResult(
        entries=entries,
        n_linked=len(linked),
        n_unlinked=len(all_eclis - linked),
        is_ls_overview=False,
        summaries_found=0,
        sources=[name],
    )


def harvest_files(paths) -> HarvestResult:
    """
    Detecteert ECLI's in meerdere bestanden en voegt het resultaat samen
    (ontdubbeld op ECLI). Een ECLI geldt als 'met link' zodra hij in minstens
    een bron gelinkt was; titel/samenvatting worden overgenomen van de bron
    die ze heeft.
    """
    merged: dict = {}
    sources: list = []
    errors: list = []
    is_ls = False

    for p in paths:
        try:
            r = harvest_file(p)
        except Exception as ex:
            errors.append((Path(p).name, str(ex)))
            continue
        sources.extend(r.sources)
        is_ls = is_ls or r.is_ls_overview
        for e in r.entries:
            if e.ecli in merged:
                m = merged[e.ecli]
                m.linked = m.linked or e.linked
                if not m.overview_title and e.overview_title:
                    m.overview_title = e.overview_title
                if not m.overview_date and e.overview_date:
                    m.overview_date = e.overview_date
                if not m.summary_paras and e.summary_paras:
                    m.summary_paras = e.summary_paras
                    m.attribution = e.attribution
            else:
                merged[e.ecli] = e

    entries = list(merged.values())
    return HarvestResult(
        entries=entries,
        n_linked=sum(1 for e in entries if e.linked),
        n_unlinked=sum(1 for e in entries if not e.linked),
        is_ls_overview=is_ls,
        summaries_found=sum(1 for e in entries if e.summary_paras),
        sources=sources,
        errors=errors,
    )
