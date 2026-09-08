"""
Bus Whisky Canon — MCP-server voor Bonnie.

Deze server leest de kennis-canon uit de GitHub-repo
(https://github.com/dhurkmans716/buswhisky-canon), houdt die in het geheugen,
en biedt Bonnie tools aan om er tijdens telefoon-/WhatsApp-gesprekken in te zoeken.

GitHub blijft de enige bron van waarheid. Deze server schrijft nooit; hij leest alleen.

Configuratie via omgevingsvariabelen (allemaal optioneel, met verstandige defaults):
  CANON_RAW_URL      Raw-URL van het canon-bestand.
                     Default: het main-bestand van de publieke repo.
  CANON_REFRESH_TTL  Aantal seconden dat de canon in geheugen vers blijft (default 300).
  BONNIE_AUTH_TOKEN  Als gezet, moeten tool-aanroepen 'Authorization: Bearer <token>'
                     meesturen. Laat leeg om zonder token te draaien (repo is publiek).
  GITHUB_TOKEN       Optioneel. Alleen nodig als de repo ooit privé wordt of bij
                     GitHub rate limits. Wordt als Bearer meegestuurd bij het ophalen.
  PORT               Poort (default 8080). Railway zet deze automatisch.
"""

from __future__ import annotations

import os
import re
import time
import threading
import json
from typing import Any, Dict, List, Optional
from urllib.request import urlopen, Request

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers, get_http_request
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse

# --------------------------------------------------------------------------- #
# Configuratie
# --------------------------------------------------------------------------- #

CANON_RAW_URL = os.environ.get(
    "CANON_RAW_URL",
    "https://raw.githubusercontent.com/dhurkmans716/buswhisky-canon/main/canon.md",
)
REFRESH_TTL_SECONDS = int(os.environ.get("CANON_REFRESH_TTL", "300"))
BONNIE_AUTH_TOKEN = os.environ.get("BONNIE_AUTH_TOKEN", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "8080"))

# n8n-webhook dat de beschikbaarheid van bedrijfsuitjes/activiteiten controleert.
BESCHIKBAARHEID_URL = os.environ.get(
    "BESCHIKBAARHEID_URL",
    "https://buswhiskyevents.app.n8n.cloud/webhook/beschikbaarheid-check",
)

mcp = FastMCP("Bus Whisky Canon")

# --------------------------------------------------------------------------- #
# In-geheugen cache van de canon
# --------------------------------------------------------------------------- #

_cache: Dict[str, Any] = {"sections": [], "fetched_at": 0.0, "raw": ""}
_lock = threading.Lock()

# Kleine Nederlandse stopwoordenlijst — houdt de zoekscore relevant.
_STOPWORDS = {
    "de", "het", "een", "en", "of", "maar", "want", "dus", "als", "dan", "die",
    "dat", "deze", "dit", "je", "jij", "u", "we", "wij", "ik", "hij", "zij",
    "is", "zijn", "was", "ben", "bent", "wordt", "worden", "heb", "hebt", "heeft",
    "hebben", "kan", "kun", "kunt", "kunnen", "mag", "moet", "moeten", "wil",
    "willen", "op", "in", "aan", "van", "voor", "met", "naar", "bij", "om",
    "over", "onder", "door", "tot", "uit", "per", "hoe", "wat", "waar", "wie",
    "wanneer", "welke", "welk", "er", "te", "ook", "nog", "wel", "niet", "geen",
    "jullie", "hun", "hen", "mij", "ons", "onze", "jouw", "zo",
}

# Merknaam-woorden komen overal in de canon voor en zeggen dus niets over
# relevantie. We negeren ze bij het bouwen van de zoekopdracht.
_BRAND = {"bus", "whisky", "whiskey", "buswhisky"}

# Veelvoorkomende Nederlandse achtervoegsels — voor lichte stemming
# (betere recall op vervoegingen, bijv. eigenaren <-> eigenaars).
_SUFFIXES = ("ingen", "ing", "en", "er", "es", "e", "s", "t")


def _fetch_canon() -> str:
    """Haal het rauwe canon-bestand op van GitHub."""
    req = Request(CANON_RAW_URL, headers={"User-Agent": "buswhisky-canon-mcp"})
    if GITHUB_TOKEN and "github" in CANON_RAW_URL:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    with urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8")


def _tokenize(text: str) -> List[str]:
    return [
        t for t in re.findall(r"[a-z0-9]+", text.lower())
        if len(t) > 2 and t not in _STOPWORDS
    ]


def _stem(t: str) -> str:
    """Zeer lichte Nederlandse stemmer: strip 'ge'-voorvoegsel en één achtervoegsel."""
    if len(t) > 5 and t.startswith("ge"):
        t = t[2:]
    for suf in _SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[: -len(suf)]
    return t


def _stem_set(text: str) -> set:
    return {_stem(t) for t in _tokenize(text)}


def _split_sections(md: str) -> List[Dict[str, Any]]:
    """Splits het canon-document in secties op basis van de '## '-koppen.

    Per sectie worden meteen stam-sets voorberekend, zodat zoeken supersnel blijft.
    """
    parts = re.split(r"\n(?=##\s)", md)
    sections: List[Dict[str, Any]] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = re.match(r"##\s+(.*)", part)
        if m:
            title = m.group(1).strip()
            body = part[m.end():].strip()
        else:
            title = "Inleiding"
            body = part
        sections.append({
            "titel": title,
            "inhoud": body,
            "_title_l": title.lower(),
            "_body_l": body.lower(),
            "_title_stems": _stem_set(title),
            "_body_stems": _stem_set(body),
        })
    return sections


_refreshing = threading.Event()


def _do_refresh() -> None:
    """Haal de canon op en werk de cache bij. Faalt stil (behoud oude cache)."""
    try:
        raw = _fetch_canon()
        sections = _split_sections(raw)
        with _lock:
            _cache["raw"] = raw
            _cache["sections"] = sections
            _cache["fetched_at"] = time.time()
    except Exception:
        # Bij een ophaal-fout houden we de bestaande cache; nooit een lege server.
        pass
    finally:
        _refreshing.clear()


def _ensure_fresh(force: bool = False) -> None:
    """Zorg dat de canon (vrijwel) vers is.

    - force=True: blokkerend verversen (alleen bij het opstarten).
    - anders: als de cache verlopen is, start een verversing op de ACHTERGROND en
      serveer meteen de bestaande (iets oudere) canon. Zo wacht een telefoongesprek
      nooit op een GitHub-aanroep.
    """
    if force:
        _do_refresh()
        return
    now = time.time()
    if _cache["sections"] and (now - _cache["fetched_at"] < REFRESH_TTL_SECONDS):
        return
    if not _refreshing.is_set():
        _refreshing.set()
        threading.Thread(target=_do_refresh, daemon=True).start()


def _query_stems(vraag: str) -> List[str]:
    """Bouw de betekenisvolle stammen uit de vraag (zonder stopwoorden en merknaam)."""
    out: List[str] = []
    seen = set()
    for t in _tokenize(vraag or ""):
        if t in _BRAND:
            continue
        s = _stem(t)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _prefix_match(qs: str, stems: set) -> bool:
    """Match op gedeelde woordstam: vangt Nederlandse vervoegingen zoals
    'gevestigd' <-> 'vestigingsadres' en 'eigenaren' <-> 'eigenaars'."""
    if len(qs) < 4:
        return False
    for s in stems:
        if len(s) < 4:
            continue
        if s.startswith(qs) or qs.startswith(s):
            return True
        if _common_prefix_len(qs, s) >= 5:
            return True
    return False


def _match_points(qs: str, section: Dict[str, Any]) -> int:
    """Ruwe punten van één zoekterm tegen één sectie (vóór distinctiviteitsweging).

    Naast exacte- en prefix-matching gebruiken we substring-matching, zodat Nederlandse
    samenstellingen ook gevonden worden (bijv. 'adres' in 'vestigingsadres',
    'camper' in 'campergasten').
    """
    pts = 0
    # Titel
    if qs in section["_title_stems"]:
        pts += 3
    elif _prefix_match(qs, section["_title_stems"]):
        pts += 2
    elif len(qs) >= 4 and qs in section["_title_l"]:
        pts += 2
    # Inhoud
    if qs in section["_body_stems"]:
        pts += 2
    elif _prefix_match(qs, section["_body_stems"]):
        pts += 1
    elif len(qs) >= 4 and qs in section["_body_l"]:
        pts += 1
    return pts


def _distinct_weight(hits: int, total_sections: int) -> float:
    """Onderscheidende termen (in weinig secties) wegen zwaarder dan alledaagse."""
    if hits <= 0:
        return 0.0
    if hits <= 2:
        return 2.0
    if hits <= max(3, total_sections // 3):
        return 1.4
    return 1.0


def _rank_sections(query_stems: List[str], sections: List[Dict[str, Any]]) -> List:
    """Twee-pass scoring: bepaal per term hoe zeldzaam de match is en weeg mee."""
    n = len(sections)
    per_term = []  # lijst van (weight, {section_idx: points})
    for qs in query_stems:
        hitmap = {}
        for i, sec in enumerate(sections):
            pts = _match_points(qs, sec)
            if pts > 0:
                hitmap[i] = pts
        if hitmap:
            per_term.append((_distinct_weight(len(hitmap), n), hitmap))

    scores = [0.0] * n
    for weight, hitmap in per_term:
        for i, pts in hitmap.items():
            scores[i] += weight * pts

    ranked = [(scores[i], sections[i]) for i in range(n) if scores[i] > 0]
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return ranked


# --------------------------------------------------------------------------- #
# Filtering en inkorten van zoekresultaten
# --------------------------------------------------------------------------- #

# Secties die ALLEEN over het gedrag van de bots gaan of interne werkdocumentatie
# zijn. Een beller heeft er nooit iets aan, ze zijn samen goed voor meer dan de helft
# van de canon en ze scoren makkelijk hoog omdat ze zo veel woorden bevatten.
INTERNE_SECTIES = (
    "10. taal & toon",
    "10a.",
    "10b.",
    "6o.",
    "website-leads",
    "openstaande punten",
)

# Maximale lengte van een sectie in het antwoord. Een hele sectie kan 30.000 tekens
# zijn; dat is te veel om aan de telefoon doorheen te lezen en het maakt elke
# volgende beurt van het gesprek trager.
MAX_SECTIE_TEKENS = 3500


def _is_intern(titel: str) -> bool:
    t = (titel or "").strip().lower()
    return any(t.startswith(p) for p in INTERNE_SECTIES)


def _knip(inhoud: str, query_stems: List[str], limiet: int = MAX_SECTIE_TEKENS) -> str:
    """Geef het best passende aaneengesloten stuk van een lange sectie terug.

    Scoort per regel hoeveel zoekstammen erin voorkomen en kiest het venster met de
    hoogste score. Zo krijgt de beller het antwoord en niet de hele sectie.
    """
    if len(inhoud) <= limiet:
        return inhoud
    regels = inhoud.splitlines()
    scores = []
    for r in regels:
        rs = _stem_set(r)
        rl = r.lower()
        n = 0
        for qs in query_stems:
            if qs in rs or (len(qs) >= 4 and qs in rl):
                n += 1
        scores.append(n)
    beste_start, beste_score, beste_eind = 0, -1, 0
    for i in range(len(regels)):
        lengte, score, j = 0, 0, i
        while j < len(regels) and lengte + len(regels[j]) + 1 <= limiet:
            lengte += len(regels[j]) + 1
            score += scores[j]
            j += 1
        if score > beste_score:
            beste_start, beste_score, beste_eind = i, score, j
        if j >= len(regels):
            break
    stuk = chr(10).join(regels[beste_start:beste_eind]).strip()
    voor = "(fragment uit deze sectie) " if beste_start > 0 else ""
    return voor + stuk


# --------------------------------------------------------------------------- #
# Auth-helper (optioneel)
# --------------------------------------------------------------------------- #

def _auth_ok() -> bool:
    """True als auth niet vereist is, of als het juiste Bearer-token is meegestuurd.

    We lezen de Authorization-header uit de rauwe request; get_http_headers() filtert
    die standaard weg (include_all=False), waardoor het token anders onzichtbaar is.
    """
    if not BONNIE_AUTH_TOKEN:
        return True
    auth = ""
    try:
        req = get_http_request()
        if req is not None:
            auth = req.headers.get("authorization", "") or ""
    except Exception:
        auth = ""
    if not auth:
        try:
            auth = (get_http_headers(include_all=True) or {}).get("authorization", "") or ""
        except Exception:
            auth = ""
    if isinstance(auth, str) and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip() == BONNIE_AUTH_TOKEN
    return False


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@mcp.tool(
    meta={
        "bonnie_feedback": [
            "Een moment, ik zoek het even voor je op",
            "Ik kijk het meteen even na voor je",
        ],
        "bonnie_states": ["in-progress"],
        "bonnie_channels": ["phone", "whatsapp"],
    }
)
def zoek_in_canon(vraag: str, max_resultaten: int = 3) -> Dict[str, Any]:
    """Zoek het antwoord op een vraag van de beller in de Bus Whisky kennis-canon.

    Gebruik dit voor alle feitelijke vragen over Bus Whisky: openingstijden, adres,
    contact, rondleidingen, producten, arrangementen, camperplaats, busvervoer,
    certificeringen, awards, team en de historie. Geef de exacte vraag van de beller
    mee. De tool geeft de best passende secties uit de canon terug; formuleer daar
    een natuurlijk antwoord van en verzin niets wat hier niet in staat.
    """
    if not _auth_ok():
        return {"fout": "niet_geautoriseerd", "bericht": "Ongeldig of ontbrekend token."}

    _ensure_fresh()
    sections = _cache["sections"]
    if not sections:
        return {"fout": "canon_niet_beschikbaar", "bericht": "De canon kon niet worden geladen."}

    query_stems = _query_stems(vraag)
    zoekbaar = [s for s in sections if not _is_intern(s["titel"])]
    ranked = _rank_sections(query_stems, zoekbaar)

    max_resultaten = max(1, min(int(max_resultaten or 3), 5))
    top = ranked[:max_resultaten]
    # Alleen secties die er echt toe doen: valt een sectie ver onder de beste
    # treffer, dan is het ruis en houden we het antwoord kort.
    if top:
        drempel = top[0][0] * 0.4
        top = [t for t in top if t[0] >= drempel]

    resultaten = [
        {
            "titel": s["titel"],
            "inhoud": _knip(s["inhoud"], query_stems),
            "score": round(score, 1),
        }
        for score, s in top
    ]

    return {
        "vraag": vraag,
        "gevonden": len(resultaten),
        "resultaten": resultaten,
        "bron": "buswhisky-canon (GitHub)",
        "laatst_ververst": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.gmtime(_cache["fetched_at"])
        ),
    }


@mcp.tool(
    meta={
        "bonnie_feedback": ["Een moment, ik zoek het even op"],
        "bonnie_states": ["in-progress"],
        "bonnie_channels": ["phone", "whatsapp"],
    }
)
def haal_hele_canon() -> Dict[str, Any]:
    """Geef de volledige canon terug als lijst van secties.

    Gebruik dit alleen als de vraag breed is of meerdere onderwerpen raakt en
    zoek_in_canon te weinig oplevert. De canon is compact genoeg om in zijn geheel
    te overzien.
    """
    if not _auth_ok():
        return {"fout": "niet_geautoriseerd", "bericht": "Ongeldig of ontbrekend token."}
    _ensure_fresh()
    return {
        "secties": [
            {"titel": s["titel"], "inhoud": s["inhoud"]} for s in _cache["sections"]
        ],
        "bron": "buswhisky-canon (GitHub)",
    }


def _extract(pattern: str) -> Optional[str]:
    """Haal één waarde uit de rauwe canon met een regex (eerste match)."""
    m = re.search(pattern, _cache.get("raw", ""), re.IGNORECASE)
    return m.group(1).strip() if m else None


@mcp.tool(
    meta={
        "bonnie_states": ["setup"],
        "bonnie_channels": ["phone", "whatsapp"],
    }
)
def kernfeiten() -> Dict[str, Any]:
    """Geef vóór het gesprek een paar kernfeiten mee aan Bonnie's promptcontext.

    Wordt automatisch tijdens de setup-fase aangeroepen (zonder argumenten) en levert
    prompt_variables die Bonnie meteen paraat heeft, zonder te hoeven zoeken.
    """
    _ensure_fresh()
    variabelen = {
        "merknaam": "Bus Whisky",
        "adres": _extract(r"vestigingsadres:\s*\*\*(.+?)\*\*") or "Heideweg 1A, 5472 LC Loosbroek",
        "plaats": "Loosbroek",
        "telefoon": _extract(r"Telefoon:\s*(.+)") or "+31 (0)413 418 016",
        "email": _extract(r"E-mail:\s*(\S+)") or "info@buswhisky.com",
    }
    return {"prompt_variables": {k: v for k, v in variabelen.items() if v}}


@mcp.tool(
    meta={
        "bonnie_feedback": [
            "Een moment, ik kijk die datum even voor je na",
            "Ik check die datum meteen even voor je",
        ],
        "bonnie_states": ["in-progress"],
        "bonnie_channels": ["phone", "whatsapp"],
    }
)
def check_beschikbaarheid(activiteit: str, datum: str = "", aantal_personen: str = "") -> Dict[str, Any]:
    """Controleer LIVE of een datum vrij is voor een vergadering, bedrijfsuitje, activiteit of bruiloft.

    ROEP DEZE TOOL ALTIJD AAN zodra de beller een datum noemt, VOORDAT je een offerte, een
    terugbelverzoek of welk vervolg dan ook toezegt. Dat geldt voor elke zakelijke aanvraag: een
    dagvergadering of meeting, een 12-, 24-, 32-, 48- of 56-uurs arrangement, een teamdag of
    heisessie, de 4x4 Ecotrail, kleiduifschieten, een ander groepsuitje of een bruiloft. Zeg nooit
    uit jezelf of een datum kan of niet kan, en beloof nooit een offerte voordat je hebt gecheckt.

    Geef door: de activiteit in de woorden van de beller (bijvoorbeeld "dagvergadering",
    "vergaderlocatie" of "4x4 Ecotrail"), de datum (bij voorkeur dd-mm-jjjj) en het aantal personen.
    Weet de beller de datum nog niet, laat `datum` dan LEEG: de tool geeft dan de eerstvolgende
    vrije datums terug.

    De tool geeft terug: `status` (BESCHIKBAAR / VOL / TE_WEINIG_PLEK / VRIJE_DATUMS /
    GEEN_VRIJE_DATUMS / GEEN_ARRANGEMENT / DATUM_ONDUIDELIJK / ONBEKEND), een kant-en-klaar gesproken
    `antwoord` en zo nodig `alternatieve_datums`. Volg dat antwoord en noem datums letterlijk zoals
    ze er staan; reken zelf geen weekdag of datum uit. Bij BESCHIKBAAR: bevestig stellig en pak door
    naar een offerte op maat (aantal personen, e-mail, evt. bedrijfsnaam en telefoon). Bij VOL of
    TE_WEINIG_PLEK: noem meteen de alternatieve datums en laat de beller nooit met alleen een nee
    achter.
    """
    if not _auth_ok():
        return {"status": "ONBEKEND", "fout": "niet_geautoriseerd", "bericht": "Ongeldig of ontbrekend token."}
    payload = json.dumps({
        "activiteit": (activiteit or "").strip(),
        "datum": (datum or "").strip(),
        "aantal_personen": str(aantal_personen or "").strip(),
    }).encode("utf-8")
    req = Request(
        BESCHIKBAARHEID_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "buswhisky-canon-mcp"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "ONBEKEND",
            "fout": "beschikbaarheid_service_onbereikbaar",
            "bericht": str(exc)[:200],
            "antwoord": "Dat kan ik nu even niet met zekerheid zeggen. Zal ik het laten navragen of alvast een offerte in gang zetten?",
        }
    return data


# --------------------------------------------------------------------------- #
# Discovery- en health-endpoints
# --------------------------------------------------------------------------- #

@mcp.custom_route(path="/.well-known/mcp-server-info", methods=["GET"])
async def mcp_server_info(request: StarletteRequest) -> JSONResponse:  # noqa: ARG001
    return JSONResponse(
        {
            "name": "Bus Whisky Canon",
            "description": (
                "Zoekt in de Bus Whisky kennis-canon (single source of truth op GitHub) "
                "zodat Bonnie feitelijke vragen tijdens telefoon- en WhatsApp-gesprekken "
                "kan beantwoorden."
            ),
            "alias": "canon",
            "author": "Bus Whisky",
            "endpoints": {"mcp": "/mcp"},
            "auth": {"required": bool(BONNIE_AUTH_TOKEN)},
        }
    )


@mcp.custom_route(path="/health", methods=["GET"])
async def health(request: StarletteRequest) -> JSONResponse:  # noqa: ARG001
    _ensure_fresh()
    return JSONResponse(
        {
            "status": "ok",
            "secties_geladen": len(_cache["sections"]),
            "laatst_ververst": _cache["fetched_at"],
        }
    )


if __name__ == "__main__":
    # Laad de canon alvast in bij het opstarten (koude start vermijden).
    _ensure_fresh(force=True)
    # Streamable HTTP transport (één MCP-endpoint op /mcp).
    mcp.run(transport="http", host="0.0.0.0", port=PORT, path="/mcp")
