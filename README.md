# Uitspraken-export — uitsprakenoverzicht → losse PDF's (met voorblad)

Een programma dat een bron met ECLI-uitspraken inleest (een L&S-kwartaaloverzicht,
een ander PDF met links naar rechtspraak.nl, of een e-mail), elke uitspraak ophaalt
bij de Rechtspraak en als losse, doorzoekbare PDF opslaat —  met een **voorblad**
dat de basisgegevens en (bij het L&S-overzicht) de **samenvatting** bevat.

Bestandsnaam: **`YYYYMMDD_Instantie_Titel.pdf`**.

Volgende keer hoef je alleen de nieuwe bron te kiezen (of erop te slepen) en op
**Start export** te klikken.

---

## 1. Wat het doet

1. **Detecteert ECLI's** in de bron — zowel met hyperlink (`rechtspraak.nl`) als
   los als tekst. Bij het inladen zie je meteen hoeveel er zijn gevonden
   (*met link* en *zonder link*).
2. **Haalt per ECLI de uitspraak op** via de **Open Data-webservice** van de
   Rechtspraak (`data.rechtspraak.nl`).
3. Maakt van elke uitspraak een **doorzoekbare PDF** met:
   - een **voorblad**: naam, instantie, datum uitspraak, **datum publicatie**,
     zaaknummer, rechtsgebied, ECLI, bronlink, en — als de bron een
     L&S-uitsprakenoverzicht is — de **samenvatting** (met behoud van vet/cursief)
     plus *"Samenvatting door [advocaat, kantoor]"*;
   - daarna de **volledige uitspraaktekst** + officiële inhoudsindicatie.
4. Slaat die op als `YYYYMMDD_Instantie_Titel.pdf` in je gekozen map.
5. Schrijft (optioneel) een `_verwerkingsrapport.txt`.

Het standaardlettertype is **Verdana 10** (op Windows automatisch aanwezig; op
andere systemen valt het terug op DejaVuSans en anders Helvetica).

> **Waarom de Open Data-service en niet de "deel → PDF"-knop van de website?**
> De website is een JavaScript-app; die deelknop draait in de browser en is lastig
> betrouwbaar na te bootsen. De Open Data-service levert exact dezelfde, *officiële*
> tekst als data. Daardoor krijg je een **echte tekst-PDF** (doorzoekbaar,
> kopieerbaar) in plaats van een schermafdruk, en breekt het niet als de website
> verandert.

---

## 2. Welke bronnen kun je gebruiken?

| Type            | Extensie            | Bijzonderheden                                    |
|-----------------|---------------------|---------------------------------------------------|
| L&S-overzicht   | `.pdf`              | Volledig: voorblad **met** samenvatting + auteur  |
| Ander PDF       | `.pdf`              | Voorblad met basisgegevens; **geen** samenvatting |
| E-mail          | `.eml`              | Links + tekst-ECLI's; werkt out-of-the-box        |
| Outlook-mail    | `.msg`              | Vereist `extract-msg` (zie §6) — of bewaar als `.eml`/`.pdf` |
| Webpagina       | `.html` / `.htm`    | Links + zichtbare tekst                            |
| Platte tekst    | `.txt` / `.md`      | "Met link" = ECLI die in een rechtspraak-URL staat |

Je kunt **meerdere bestanden tegelijk** toevoegen; dezelfde uitspraak in twee
bronnen wordt één keer geëxporteerd.

---

## 3. Met of zonder link — wat wordt geëxporteerd?

- **Standaard**: elke uitspraak waar een **hyperlink** naar wijst.
- **Vinkje "Ook ECLI's zonder hyperlink meenemen"** (standaard uit): dan worden
  óók ECLI's geëxporteerd die alleen als **tekst** (zonder link) in de bron staan
  — bijvoorbeeld een uitspraak die terloops wordt genoemd.

> In het L&S-kwartaaloverzicht zijn doorgaans álle ECLI's gelinkt (ook de
> "vervolg op"-verwijzingen in de voetnoten). Die worden dus standaard meegenomen;
> de voetnoot-uitspraken krijgen een voorblad met alleen basisgegevens.

---

## 4. Eenmalige installatie

Je hebt **Python 3.10 of nieuwer** nodig
([python.org](https://www.python.org/downloads/); vink *"Add Python to PATH"* aan).

Open een terminal/PowerShell in de map met deze bestanden en draai:

```
pip install -r requirements.txt
```

---

## 5. Gebruiken

### Grafisch (aanraders)

```
python rechtspraak_downloader.py
```

1. **Voeg bronbestand(en) toe** (knop *Bestanden toevoegen* — of sleep ze in het
   venster als `tkinterdnd2` is geïnstalleerd, of sleep ze op het programma-icoon).
2. Je ziet meteen: *"Gedetecteerd: X met link · Y zonder link · Z met samenvatting"*
   en hoeveel er worden geëxporteerd.
3. Kies de **opslagmap** en eventueel de opties.
4. Klik **Start export**.

### Slepen

Sleep een of meer bronbestanden (PDF, `.eml`, `.msg`, `.html`, `.txt`) op het
programma (of op het `.exe`-icoon na het inpakken, zie §7). Het venster opent dan
met die bestanden al ingeladen.

### Opdrachtregel (server / zonder venster)

```
python rechtspraak_downloader.py --run overzicht.pdf
python rechtspraak_downloader.py --run a.pdf b.eml --out "D:/Uitspraken" --include-unlinked
```

Opties: `--run` (direct verwerken), `--out MAP`, `--include-unlinked`,
`--no-report`, `--overwrite`, `--underscores`, `--slash X`, `--delay SEC`,
`--save-config`, `--gui`.

---

## 6. Knoppen / instellingen

- **Ook ECLI's zonder hyperlink meenemen** — zie §3 (standaard uit).
- **Verwerkingsrapport genereren** — schrijf `_verwerkingsrapport.txt` (standaard aan).
- **Bestaande bestanden overschrijven** — anders worden al bestaande PDF's overgeslagen.
- **Spaties → underscores** in de bestandsnaam.
- **"/" in partijnamen vervangen door** — standaard `-` (mag niet in een bestandsnaam).
- **Pauze tussen verzoeken** — beleefdheidslimiet (max. 10/sec; standaard 1,0 sec).

Instellingen worden onthouden voor de volgende keer.

---

## 7. Optionele extra's

- **`.msg` (Outlook) lezen:** `pip install extract-msg`. Zonder dit pakket geeft de
  tool een nette melding; je kunt de mail dan opslaan als `.eml` of `.pdf`.
- **Slepen ín het venster:** `pip install tkinterdnd2`. Zonder dit werkt slepen op
  het `.exe`-icoon en de knop *Bestanden toevoegen* gewoon.

### Inpakken tot één `.exe` (Windows)

```
pip install pyinstaller
pyinstaller --onefile --windowed --name "UitsprakenExport" rechtspraak_downloader.py
```

Wil je slepen ín het venster meebundelen, installeer dan eerst `tkinterdnd2`
(PyInstaller pakt het dan mee). De `.exe` verschijnt in de map `dist`. Daarna kun
je bronbestanden direct op het `.exe`-icoon slepen.

---

## 8. Bestanden in dit pakket

| Bestand                       | Rol                                                        |
|-------------------------------|------------------------------------------------------------|
| `rechtspraak_downloader.py`   | Het programma (venster + opdrachtregel). **Dit start je.** |
| `rechtspraak_sources.py`      | Detecteert ECLI's + samenvattingen in de bronbestanden.    |
| `rechtspraak_core.py`         | Haalt uitspraken op en maakt de PDF's (voorblad + tekst).  |
| `requirements.txt`            | De benodigde pakketten.                                    |
| `LEESMIJ.md`                  | Deze uitleg.                                               |

---

## 9. Aandachtspunten / beperkingen

- **Samenvatting** wordt alleen overgenomen uit een **L&S-uitsprakenoverzicht**
  (herkend aan de opmaak). Bij andere PDF's/e-mails staat op het voorblad alleen
  de basisinformatie. Vet en cursief uit het overzicht worden zo goed mogelijk
  overgenomen; sommige korte samenvattingen staan in het overzicht zélf volledig
  vet — dat wordt dan ook vet weergegeven.
- **Voetnoten** in de samenvatting worden niet als losse noten gereconstrueerd. De
  "vervolg op"-uitspraken waar de voetnoten naar verwijzen zijn doorgaans wél
  gelinkt en worden als aparte PDF's geëxporteerd.
- **Gepseudonimiseerde** uitspraken (`[verzoeker]` e.d.) hebben vaak geen
  bruikbare titel in het overzicht; die bestanden krijgen een naam op basis van het
  zaaknummer of de ECLI en worden in het rapport vermeld.
- Soms publiceert de Rechtspraak alleen metadata (geen volledige tekst). De PDF
  bevat dan het voorblad + een verwijzing naar de bronlink.
