"""
rechtspraak_core.py
====================
Kernlogica voor de "Uitsprakenoverzicht -> PDF's" tool.

Wat dit doet:
  1. Leest een L&S-uitsprakenoverzicht (PDF) en haalt alle ECLI's eruit,
     samen met de titel/partijnamen die in het overzicht achter de ECLI staan.
  2. Haalt per ECLI de officiele uitspraak op via de Open Data-webservice
     van de Rechtspraak (https://data.rechtspraak.nl/uitspraken/content?id=ECLI...).
  3. Rendert die uitspraak naar een nette, doorzoekbare PDF (volledige tekst
     + metadata: instantie, datum, zaaknummer, rechtsgebied, bron-URL).
  4. Slaat het bestand op als  YYYYMMDD_Instantie_Titel.pdf  in de door de
     gebruiker ingestelde map.

Bewuste keuze: we renderen uit de open data (XML) i.p.v. een screenshot van
de website. Rechtspraak.nl is een JavaScript-app (de "deel als PDF"-knop draait
in de browser); de open data bevat exact dezelfde, gezaghebbende tekst en is
veel betrouwbaarder om geautomatiseerd op te halen. Het resultaat is een PDF
met selecteerbare tekst i.p.v. een plaatje.

Geen externe netwerk-dependencies: HTTP via urllib (stdlib), XML via
xml.etree (stdlib). Enkel pypdf (PDF lezen) en reportlab (PDF schrijven) nodig.
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import html
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Configuratie (wordt opgeslagen als JSON en is later aan te passen)
# ---------------------------------------------------------------------------

APP_NAME = "RechtspraakDownloader"


def default_config_path() -> Path:
    """Platform-afhankelijke locatie voor het configuratiebestand."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
    return Path(base) / APP_NAME / "config.json"


def default_output_dir() -> str:
    return str(Path(os.path.expanduser("~")) / "Documents" / "Uitspraken")


@dataclass
class Config:
    # Map waarin de PDF's worden opgeslagen (in de instellingen aan te passen).
    output_dir: str = field(default_factory=default_output_dir)
    # Waarmee de "/" in partijnamen wordt vervangen (mag niet in een bestandsnaam).
    slash_replacement: str = "-"
    # Ook ECLI's meenemen die in het overzicht GEEN hyperlink hebben (alleen
    # als tekst genoemd). Standaard uit: dan worden alleen gelinkte ECLI's
    # geexporteerd.
    include_unlinked: bool = False
    # Het _verwerkingsrapport.txt genereren?
    generate_report: bool = True
    # Bestaand bestand met dezelfde naam overschrijven?
    overwrite_existing: bool = False
    # Spaties in de bestandsnaam vervangen door underscores?
    spaces_to_underscores: bool = False
    # Pauze (seconden) tussen verzoeken aan de server (max toegestaan: 10/sec).
    request_delay: float = 1.0

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        path = path or default_config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cfg = cls()
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
            return cfg
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return cls()

    def save(self, path: Optional[Path] = None) -> None:
        path = path or default_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False),
                        encoding="utf-8")


# ---------------------------------------------------------------------------
# Stap 1: ECLI's + titels uit het overzicht-PDF halen
# ---------------------------------------------------------------------------

_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11,
    "december": 12,
}

_ECLI_RE = r"ECLI:NL:[A-Z]+:\d{4}:[A-Z0-9]+"

# Een "hoofd"-uitspraak in het overzicht ziet er zo uit:
#   <Instantie> <dag maand jaar>, <ECLI>[, <titel/partijen>].
# De datum direct gevolgd door ", ECLI" is het betrouwbare anker.
_MAIN_RE = re.compile(
    r"(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(\d{4}),\s*(" + _ECLI_RE + r")"
    r"(?:,\s*([^.\n]*?)\.)?",
    re.IGNORECASE,
)

# Publieke aliassen (gebruikt door rechtspraak_sources.py).
ECLI_RE = re.compile(_ECLI_RE)
MAIN_RE = _MAIN_RE
MONTHS = _MONTHS

# Instantiecodes -> leesbare naam. Alleen een fallback; normaal levert de
# Open Data-API de instantienaam aan.
COURT_CODES = {
    "HR": "Hoge Raad", "PHR": "Parket bij de Hoge Raad",
    "RVS": "Raad van State", "CRVB": "Centrale Raad van Beroep",
    "CBB": "College van Beroep voor het bedrijfsleven",
    "RBAMS": "Rechtbank Amsterdam", "RBDHA": "Rechtbank Den Haag",
    "RBROT": "Rechtbank Rotterdam", "RBOBR": "Rechtbank Oost-Brabant",
    "RBMNE": "Rechtbank Midden-Nederland", "RBNHO": "Rechtbank Noord-Holland",
    "RBNNE": "Rechtbank Noord-Nederland", "RBGEL": "Rechtbank Gelderland",
    "RBOVE": "Rechtbank Overijssel", "RBLIM": "Rechtbank Limburg",
    "RBZWB": "Rechtbank Zeeland-West-Brabant",
    "GHSHE": "Gerechtshof 's-Hertogenbosch",
    "GHARL": "Gerechtshof Arnhem-Leeuwarden",
    "GHAMS": "Gerechtshof Amsterdam", "GHDHA": "Gerechtshof Den Haag",
    "GHSGR": "Gerechtshof 's-Gravenhage",
    "OGEAC": "Gerecht in eerste aanleg van Curacao",
    "OGEAA": "Gerecht in eerste aanleg van Aruba",
    "OGEABES": "Gerecht in eerste aanleg van Bonaire, Sint Eustatius en Saba",
    "OGHACMB": "Gemeenschappelijk Hof van Justitie",
}


@dataclass
class Entry:
    ecli: str
    linked: bool = False          # had deze ECLI een hyperlink in het overzicht?
    overview_title: str = ""      # titel zoals in het overzicht (kan leeg zijn)
    overview_date: str = ""       # YYYYMMDD uit het overzicht (fallback)
    attribution: str = ""         # "Naam, Kantoor" van de samenvatter (L&S)
    # Samenvatting uit het overzicht, als lijst van alinea's; elke alinea is
    # een lijst van runs (tekst, vet, cursief). Leeg als er geen samenvatting is.
    summary_paras: list = field(default_factory=list)
    source_name: str = ""         # bestandsnaam van de bron (voor het rapport)


def extract_text_from_pdf(pdf_path: str) -> str:
    """Volledige tekst uit een PDF (alle pagina's)."""
    from pypdf import PdfReader  # lazy import zodat de module zonder pypdf laadt
    reader = PdfReader(pdf_path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# Het detecteren van ECLI's (met/zonder link), titels en samenvattingen zit nu
# in rechtspraak_sources.py (harvest_file / harvest_files). Die levert Entry-
# objecten op die hieronder door process_entries worden verwerkt.


# ---------------------------------------------------------------------------
# Stap 2: uitspraak ophalen via de Open Data-webservice
# ---------------------------------------------------------------------------

CONTENT_URL = "https://data.rechtspraak.nl/uitspraken/content?id={ecli}"
DEEPLINK_URL = "https://uitspraken.rechtspraak.nl/details?id={ecli}"

_UA = (f"{APP_NAME}/1.0 (kwartaal-export L&S uitsprakenoverzicht; "
       "python-urllib)")


def fetch_ecli_xml(ecli: str, timeout: int = 30, retries: int = 3) -> bytes:
    """Haalt de ruwe XML van een uitspraak op. Werpt urllib.error bij falen."""
    url = CONTENT_URL.format(ecli=urllib.parse.quote(ecli, safe=":"))
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": _UA, "Accept": "application/xml"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            # Bij 404 heeft opnieuw proberen geen zin.
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                raise
            time.sleep(1.5 * attempt)
    assert last_err is not None
    raise last_err


# urllib.parse wordt door fetch_ecli_xml gebruikt:
import urllib.parse  # noqa: E402  (bewust onderaan om import-volgorde leesbaar te houden)


# ---------------------------------------------------------------------------
# Stap 3a: XML ontleden (metadata + tekst)
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    """Naam van een XML-tag zonder namespace-prefix."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


# Block-elementen waarvan we de tekst als 1 alinea/koptekst overnemen.
_LEAF_BLOCKS = {"para", "title", "bridgehead", "nr"}
_HEADING_BLOCKS = {"title", "bridgehead", "nr"}


@dataclass
class Ruling:
    ecli: str
    found: bool = False           # bestaat de ECLI in de index?
    has_body: bool = False        # is de volledige tekst gepubliceerd?
    court: str = ""               # instantie (dcterms:creator)
    date: str = ""                # YYYYMMDD (dcterms:date) = uitspraakdatum
    date_published: str = ""      # YYYYMMDD (dcterms:issued) = publicatiedatum
    title_meta: str = ""          # dcterms:title (bevat zelden partijnamen)
    zaaknummer: str = ""
    subject: str = ""             # rechtsgebied
    abstract: str = ""            # inhoudsindicatie
    doc_type: str = "Uitspraak"   # Uitspraak of Conclusie
    blocks: list[tuple[str, str]] = field(default_factory=list)  # (kind, tekst)


def _normspace(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _walk_body(el, out: list[tuple[str, str]]) -> None:
    ln = _local(el.tag)
    if ln in _LEAF_BLOCKS:
        text = _normspace("".join(el.itertext()))
        if text:
            kind = "heading" if ln in _HEADING_BLOCKS else "para"
            out.append((kind, text))
        return  # niet verder afdalen: itertext() pakte de kinderen al mee
    for child in el:
        _walk_body(child, out)


def _merge_numbered_headings(blocks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Voegt een los nummer-kopje samen met de erop volgende kop/alinea."""
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(blocks):
        kind, text = blocks[i]
        if (kind == "heading" and re.fullmatch(r"\d+[.)]?", text)
                and i + 1 < len(blocks)):
            nkind, ntext = blocks[i + 1]
            out.append((nkind, f"{text}  {ntext}"))
            i += 2
            continue
        out.append((kind, text))
        i += 1
    return out


def parse_ruling(ecli: str, xml_bytes: bytes) -> Ruling:
    """Ontleedt de Open Data-XML naar een Ruling-object."""
    r = Ruling(ecli=ecli)
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return r  # found blijft False

    r.found = True

    # ---- Metadata (dcterms / rdf), op local-name gematcht ----
    dates: list[str] = []
    for el in root.iter():
        ln = _local(el.tag)
        val = _normspace(el.text or "")
        if ln == "creator" and val and not r.court:
            r.court = val
        elif ln == "date" and re.match(r"\d{4}-\d{2}-\d{2}", val):
            dates.append(val[:10])
        elif ln == "issued" and re.match(r"\d{4}-\d{2}-\d{2}", val) and not r.date_published:
            r.date_published = val[:10].replace("-", "")
        elif ln == "zaaknummer" and val and not r.zaaknummer:
            r.zaaknummer = val
        elif ln == "title" and val and not r.title_meta:
            r.title_meta = val
        elif ln == "subject" and val and not r.subject:
            r.subject = val
    if dates:
        # dcterms:date = uitspraakdatum; neem de vroegste t.o.v. publicatie-ruis.
        d = sorted(dates)[0]
        r.date = d.replace("-", "")

    # ---- Inhoudsindicatie (abstract) ----
    for el in root.iter():
        if _local(el.tag) in ("abstract", "inhoudsindicatie"):
            txt = _normspace("".join(el.itertext()))
            if txt:
                r.abstract = txt
                break

    # ---- Tekst van de uitspraak / conclusie ----
    body_el = None
    for el in root.iter():
        ln = _local(el.tag)
        if ln in ("uitspraak", "conclusie"):
            body_el = el
            r.doc_type = "Conclusie" if ln == "conclusie" else "Uitspraak"
            break
    if body_el is not None:
        blocks: list[tuple[str, str]] = []
        _walk_body(body_el, blocks)
        blocks = _merge_numbered_headings(blocks)
        # "Geen tekst gepubliceerd"-melding herkennen (alleen metadata aanwezig).
        joined = " ".join(t for _, t in blocks).lower()
        meaningful = [b for b in blocks if len(b[1]) > 2]
        if meaningful and "wordt voor de uitspraak gepubliceerd" not in joined:
            r.blocks = meaningful
            r.has_body = True

    return r


# ---------------------------------------------------------------------------
# Stap 3b: bestandsnaam samenstellen
# ---------------------------------------------------------------------------

_ILLEGAL = r'[<>:"/\\|?*\x00-\x1f]'


def _sanitize(part: str, slash_replacement: str = "-") -> str:
    part = part.replace("/", slash_replacement)
    part = re.sub(_ILLEGAL, "", part)
    part = _normspace(part).rstrip(" .")  # geen punt/spatie aan het eind (Windows)
    return part


def choose_title(entry: Entry, ruling: Ruling) -> tuple[str, str]:
    """
    Bepaalt de 'Titel' voor de bestandsnaam.

    Volgorde:
      1. Titel/partijnamen uit het overzicht (indien aanwezig).
      2. Anders: zaaknummer (betrouwbaar, uniek, herleidbaar).
      3. Anders: het nummergedeelte van de ECLI.

    Retourneert (titel, bron) waarbij bron = 'overzicht' | 'zaaknummer' | 'ecli'.
    De meeste uitspraken zonder overzichttitel zijn gepseudonimiseerd
    ([verzoeker]/[verweerder]), dus echte partijnamen zijn dan niet beschikbaar;
    daarom valt de tool terug op het zaaknummer.
    """
    if entry.overview_title:
        return entry.overview_title, "overzicht"
    if ruling.zaaknummer:
        return ruling.zaaknummer, "zaaknummer"
    return entry.ecli.split(":")[-1], "ecli"


def build_filename(entry: Entry, ruling: Ruling, cfg: Config) -> tuple[str, str]:
    """Bouwt 'YYYYMMDD_Instantie_Titel.pdf'. Retourneert (bestandsnaam, titelbron)."""
    code = entry.ecli.split(":")[2] if entry.ecli.count(":") >= 2 else ""
    date = ruling.date or entry.overview_date or "00000000"
    court = ruling.court or COURT_CODES.get(code, code) or "Onbekend"
    title, source = choose_title(entry, ruling)

    court_s = _sanitize(court, cfg.slash_replacement) or "Onbekend"
    title_s = _sanitize(title, cfg.slash_replacement) or "zonder-titel"
    # Lengte begrenzen (Windows-padlimiet).
    title_s = title_s[:120].rstrip(" .")

    name = f"{date}_{court_s}_{title_s}"
    if cfg.spaces_to_underscores:
        name = name.replace(" ", "_")
    name = name[:180].rstrip(" .")
    return name + ".pdf", source


# ---------------------------------------------------------------------------
# Stap 3c: PDF renderen met reportlab
# ---------------------------------------------------------------------------

# --- Lettertype: Verdana, met nette fallback ------------------------------
# Het gewenste standaardlettertype is Verdana. Op Windows is dat standaard
# aanwezig (verdana.ttf). Op andere systemen valt de tool terug op DejaVuSans
# en uiteindelijk op het ingebouwde Helvetica, zodat er altijd iets rendert.
_FONT_CACHE: dict = {}


def _font_dirs() -> list[str]:
    dirs: list[str] = []
    if sys.platform.startswith("win"):
        win = os.environ.get("WINDIR", r"C:\Windows")
        dirs.append(os.path.join(win, "Fonts"))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    elif sys.platform == "darwin":
        dirs += ["/Library/Fonts", os.path.expanduser("~/Library/Fonts"),
                 "/System/Library/Fonts/Supplemental"]
    else:
        dirs += ["/usr/share/fonts/truetype/msttcorefonts",
                 "/usr/share/fonts/truetype/dejavu",
                 "/usr/share/fonts", "/usr/local/share/fonts",
                 os.path.expanduser("~/.fonts")]
    return dirs


def _find_font(*names: str) -> Optional[str]:
    """Zoekt (hoofdletterongevoelig) een fontbestand in de bekende mappen."""
    want = [n.lower() for n in names]
    for d in _font_dirs():
        try:
            files = os.listdir(d)
        except OSError:
            continue
        low = {f.lower(): os.path.join(d, f) for f in files}
        for w in want:
            if w in low:
                return low[w]
    return None


def _resolve_font() -> dict:
    """
    Registreert een lettertypefamilie en geeft de namen terug:
    {'family','bold','italic','bolditalic'}. Resultaat wordt gecachet.
    """
    if _FONT_CACHE:
        return _FONT_CACHE
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        ("Verdana", ("verdana.ttf", "verdanab.ttf", "verdanai.ttf", "verdanaz.ttf")),
        ("DejaVuSans", ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf",
                        "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique.ttf")),
    ]
    for fam, (rn, bn, iN, zn) in candidates:
        reg = _find_font(rn)
        if not reg:
            continue
        try:
            bold = _find_font(bn) or reg
            ital = _find_font(iN) or reg
            bi = _find_font(zn) or bold
            pdfmetrics.registerFont(TTFont(fam, reg))
            pdfmetrics.registerFont(TTFont(fam + "-Bold", bold))
            pdfmetrics.registerFont(TTFont(fam + "-Italic", ital))
            pdfmetrics.registerFont(TTFont(fam + "-BoldItalic", bi))
            pdfmetrics.registerFontFamily(
                fam, normal=fam, bold=fam + "-Bold",
                italic=fam + "-Italic", boldItalic=fam + "-BoldItalic")
            _FONT_CACHE.update(family=fam, bold=fam + "-Bold",
                               italic=fam + "-Italic", bolditalic=fam + "-BoldItalic")
            return _FONT_CACHE
        except Exception:
            continue

    # Ingebouwde fallback (geen TTF nodig).
    _FONT_CACHE.update(family="Helvetica", bold="Helvetica-Bold",
                       italic="Helvetica-Oblique", bolditalic="Helvetica-BoldOblique")
    return _FONT_CACHE


def _runs_to_markup(runs, esc) -> str:
    """Zet een lijst runs (tekst, vet, cursief) om naar reportlab-markup."""
    parts = []
    for text, bold, ital in runs:
        s = esc(text)
        if bold and ital:
            s = f"<b><i>{s}</i></b>"
        elif bold:
            s = f"<b>{s}</b>"
        elif ital:
            s = f"<i>{s}</i>"
        parts.append(s)
    return "".join(parts)


def render_pdf(entry: Entry, ruling: Ruling, out_path: str) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
        PageBreak,
    )

    F = _resolve_font()
    fam = F["family"]

    base = ParagraphStyle("base", fontName=fam, fontSize=10, leading=14)
    title = ParagraphStyle("title", parent=base, fontName=F["bold"],
                           fontSize=14, leading=18, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=base, fontSize=9,
                         textColor=colors.HexColor("#555555"), spaceAfter=8)
    body = ParagraphStyle("body", parent=base, fontSize=10, leading=14,
                          alignment=TA_JUSTIFY, spaceAfter=6)
    leadin = ParagraphStyle("leadin", parent=base, fontSize=10, leading=14,
                            spaceAfter=4)
    head = ParagraphStyle("head", parent=base, fontName=F["bold"], fontSize=11,
                          leading=14, spaceBefore=10, spaceAfter=4)
    attribst = ParagraphStyle("attrib", parent=base, fontName=F["italic"],
                              fontSize=9.5, textColor=colors.HexColor("#444444"),
                              spaceBefore=10)
    meta_lbl = ParagraphStyle("ml", parent=base, fontSize=9,
                              textColor=colors.HexColor("#666666"))
    meta_val = ParagraphStyle("mv", parent=base, fontSize=9)
    note = ParagraphStyle("note", parent=base, fontSize=10,
                          textColor=colors.HexColor("#8a1f11"), spaceBefore=6)

    def esc(s: str) -> str:
        return html.escape(s or "", quote=False)

    code = entry.ecli.split(":")[2] if entry.ecli.count(":") >= 2 else ""
    court = ruling.court or COURT_CODES.get(code, code) or "Onbekende instantie"
    du = ruling.date or entry.overview_date or ""
    du_nl = f"{du[6:8]}-{du[4:6]}-{du[0:4]}" if len(du) == 8 else du
    dp = ruling.date_published or ""
    dp_nl = f"{dp[6:8]}-{dp[4:6]}-{dp[0:4]}" if len(dp) == 8 else dp
    name_title, _src = choose_title(entry, ruling)
    deeplink = DEEPLINK_URL.format(ecli=entry.ecli)

    story = []

    # ---------------- VOORBLAD ----------------
    story.append(Paragraph(esc(name_title), title))
    story.append(Paragraph(esc(entry.ecli), sub))

    rows = [
        [Paragraph("Instantie", meta_lbl), Paragraph(esc(court), meta_val)],
        [Paragraph("Datum uitspraak", meta_lbl), Paragraph(esc(du_nl), meta_val)],
    ]
    if dp_nl:
        rows.append([Paragraph("Datum publicatie", meta_lbl),
                     Paragraph(esc(dp_nl), meta_val)])
    if ruling.zaaknummer:
        rows.append([Paragraph("Zaaknummer", meta_lbl),
                     Paragraph(esc(ruling.zaaknummer), meta_val)])
    if ruling.subject:
        rows.append([Paragraph("Rechtsgebied", meta_lbl),
                     Paragraph(esc(ruling.subject), meta_val)])
    rows.append([Paragraph("ECLI", meta_lbl), Paragraph(esc(entry.ecli), meta_val)])
    rows.append([Paragraph("Bron", meta_lbl),
                 Paragraph(f'<a href="{esc(deeplink)}">{esc(deeplink)}</a>', meta_val)])

    tbl = Table(rows, colWidths=[3.4 * cm, None])
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=0.6,
                            color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 8))

    if entry.summary_paras:
        story.append(Paragraph("Samenvatting", head))
        for idx, para in enumerate(entry.summary_paras):
            mk = _runs_to_markup(para, esc)
            story.append(Paragraph(mk, leadin if idx == 0 else body))
        if entry.attribution:
            story.append(Paragraph("Samenvatting door " + esc(entry.attribution),
                                   attribst))
    elif ruling.abstract:
        # Geen samenvatting uit het overzicht: toon tenminste de officiele
        # inhoudsindicatie van de Rechtspraak op het voorblad.
        story.append(Paragraph("Inhoudsindicatie", head))
        story.append(Paragraph(esc(ruling.abstract), body))

    story.append(PageBreak())

    # ---------------- UITSPRAAKTEKST ----------------
    story.append(Paragraph(esc(f"{court} — {ruling.doc_type}"), title))
    story.append(Paragraph(esc(entry.ecli), sub))

    # Inhoudsindicatie hier alleen tonen als die niet al op het voorblad stond.
    if ruling.abstract and entry.summary_paras:
        story.append(Paragraph("Inhoudsindicatie", head))
        story.append(Paragraph(esc(ruling.abstract), body))

    if ruling.has_body:
        story.append(Paragraph(ruling.doc_type, head))
        for kind, text in ruling.blocks:
            story.append(Paragraph(esc(text), head if kind == "heading" else body))
    else:
        story.append(Paragraph(
            "Let op: van deze ECLI is via Open Data van de Rechtspraak geen "
            "volledige uitspraaktekst beschikbaar (mogelijk alleen metadata "
            "gepubliceerd). Raadpleeg de uitspraak via de bron-URL op het voorblad.",
            note))

    def _footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(fam, 7.5)
        canvas.setFillColor(colors.HexColor("#999999"))
        canvas.drawString(2 * cm, 1.2 * cm, entry.ecli)
        canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"pagina {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=1.8 * cm, bottomMargin=1.8 * cm,
        title=f"{entry.ecli}", author="Rechtspraak.nl Open Data",
    )
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)


# ---------------------------------------------------------------------------
# Stap 4: alles aan elkaar knopen
# ---------------------------------------------------------------------------

@dataclass
class Result:
    ecli: str
    status: str            # 'ok' | 'metadata-only' | 'not-found' | 'error' | 'skipped'
    filename: str = ""
    title_source: str = "" # 'overzicht' | 'zaaknummer' | 'ecli'
    linked: bool = True    # had de ECLI een hyperlink in de bron?
    has_summary: bool = False
    message: str = ""


def process_entries(
    entries: list[Entry],
    cfg: Config,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
) -> list[Result]:
    """
    Verwerkt een lijst Entry-objecten (afkomstig uit rechtspraak_sources.harvest):
    haalt per ECLI de uitspraak op, rendert een PDF met voorblad en bewaart die.

    Welke entries worden verwerkt hangt af van cfg.include_unlinked:
      - False (standaard): alleen entries met een hyperlink (entry.linked).
      - True             : ook ECLI's die alleen als tekst voorkwamen.

    progress_cb(done, total, ecli)  -> voor een voortgangsbalk
    log_cb(regel)                   -> voor een logvenster
    """
    def log(msg: str) -> None:
        if log_cb:
            log_cb(msg)

    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    work = [e for e in entries if (cfg.include_unlinked or e.linked)]
    total = len(work)
    log(f"{total} uitspraak/uitspraken te verwerken.")

    results: list[Result] = []
    for i, entry in enumerate(work, start=1):
        if progress_cb:
            progress_cb(i - 1, total, entry.ecli)
        tag = "" if entry.linked else " (zonder link)"
        log(f"[{i}/{total}] {entry.ecli}{tag} ...")

        try:
            xml_bytes = fetch_ecli_xml(entry.ecli)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                results.append(Result(entry.ecli, "not-found", linked=entry.linked,
                                      message="ECLI niet gevonden (404)"))
                log("    niet gevonden (404).")
            else:
                results.append(Result(entry.ecli, "error", linked=entry.linked,
                                      message=f"HTTP {e.code}"))
                log(f"    HTTP-fout {e.code}.")
            time.sleep(cfg.request_delay)
            continue
        except Exception as e:  # netwerk/time-out/etc.
            results.append(Result(entry.ecli, "error", linked=entry.linked,
                                  message=str(e)))
            log(f"    fout bij ophalen: {e}")
            time.sleep(cfg.request_delay)
            continue

        ruling = parse_ruling(entry.ecli, xml_bytes)
        if not ruling.found:
            results.append(Result(entry.ecli, "not-found", linked=entry.linked,
                                  message="Geen geldige XML"))
            log("    geen geldige XML.")
            time.sleep(cfg.request_delay)
            continue

        filename, source = build_filename(entry, ruling, cfg)
        target = out_dir / filename
        has_sum = bool(entry.summary_paras)

        if target.exists() and not cfg.overwrite_existing:
            results.append(Result(entry.ecli, "skipped", filename, source,
                                  entry.linked, has_sum, "Bestaat al"))
            log(f"    overgeslagen (bestaat al): {filename}")
            time.sleep(cfg.request_delay)
            continue

        try:
            render_pdf(entry, ruling, str(target))
        except Exception as e:
            results.append(Result(entry.ecli, "error", filename, source,
                                  entry.linked, has_sum, f"PDF-fout: {e}"))
            log(f"    fout bij PDF maken: {e}")
            time.sleep(cfg.request_delay)
            continue

        status = "ok" if ruling.has_body else "metadata-only"
        results.append(Result(entry.ecli, status, filename, source,
                              entry.linked, has_sum))
        flag = "" if source == "overzicht" else f"  [titel uit {source}]"
        extra = "" if ruling.has_body else "  [alleen metadata]"
        summ = "  [met samenvatting]" if has_sum else ""
        log(f"    opgeslagen: {filename}{flag}{extra}{summ}")

        time.sleep(cfg.request_delay)

    if progress_cb:
        progress_cb(total, total, "")
    return results


def write_report(results: list[Result], out_dir: str) -> str:
    """Schrijft een overzicht van de run naar _verwerkingsrapport.txt."""
    lines = ["Verwerkingsrapport - Uitspraken-export", "=" * 44, ""]
    ok = [r for r in results if r.status == "ok"]
    meta = [r for r in results if r.status == "metadata-only"]
    nf = [r for r in results if r.status == "not-found"]
    err = [r for r in results if r.status == "error"]
    skip = [r for r in results if r.status == "skipped"]
    done = [r for r in results if r.status in ("ok", "metadata-only")]
    with_sum = [r for r in done if r.has_summary]
    unlinked = [r for r in done if not r.linked]
    fallback = [r for r in done if r.title_source != "overzicht"]

    lines.append(f"Totaal verwerkt : {len(results)}")
    lines.append(f"  Opgeslagen (volledige tekst) : {len(ok)}")
    lines.append(f"  Opgeslagen (alleen metadata) : {len(meta)}")
    lines.append(f"  Waarvan met samenvatting     : {len(with_sum)}")
    lines.append(f"  Waarvan zonder hyperlink     : {len(unlinked)}")
    lines.append(f"  Overgeslagen (bestond al)    : {len(skip)}")
    lines.append(f"  Niet gevonden                : {len(nf)}")
    lines.append(f"  Fouten                       : {len(err)}")
    lines.append("")
    if unlinked:
        lines.append("Meegenomen ZONDER hyperlink (alleen als tekst genoemd):")
        for r in unlinked:
            lines.append(f"  - {r.ecli}  -> {r.filename}")
        lines.append("")
    if fallback:
        lines.append("Bestanden waarvan de titel NIET uit het overzicht kwam")
        lines.append("(controleer/hernoem evt. handmatig - vaak gepseudonimiseerd):")
        for r in fallback:
            lines.append(f"  - {r.filename}   (titel uit {r.title_source})")
        lines.append("")
    if meta:
        lines.append("Alleen metadata beschikbaar (geen volledige tekst):")
        for r in meta:
            lines.append(f"  - {r.ecli}  -> {r.filename}")
        lines.append("")
    if nf or err:
        lines.append("Niet gelukt:")
        for r in nf + err:
            lines.append(f"  - {r.ecli}  ({r.status}: {r.message})")
        lines.append("")

    path = Path(out_dir) / "_verwerkingsrapport.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)
