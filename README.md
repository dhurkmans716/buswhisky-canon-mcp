# Bus Whisky Canon — MCP-server voor Bonnie

Deze server verbindt Bonnie (de AI-telefoon-/WhatsApp-assistent) met de kennis-canon
op GitHub (`dhurkmans716/buswhisky-canon`). Bonnie roept de server aan tijdens
gesprekken; de server zoekt in de canon en geeft het juiste stukje kennis terug.

**GitHub blijft de enige bron van waarheid.** Deze server leest alleen — hij schrijft
nooit iets terug. Kennis toevoegen of wijzigen doe je in de repo (via commits /
pull requests); de server pikt wijzigingen vanzelf op.

---

## Wat de server aanbiedt

Drie tools (zichtbaar in Bonnie via `tools/list`):

| Tool | Wanneer | Wat |
|---|---|---|
| `zoek_in_canon(vraag)` | tijdens het gesprek | Zoekt de best passende secties bij de vraag van de beller en geeft ze als JSON terug. |
| `haal_hele_canon()` | tijdens het gesprek (fallback) | Geeft de volledige canon als lijst van secties, voor brede vragen. |
| `kernfeiten()` | vóór het gesprek (setup) | Levert adres, plaats, telefoon en e-mail als `prompt_variables`, zodat Bonnie die meteen paraat heeft. |

De zoektool speelt via `bonnie_feedback` een kort "one moment"-zinnetje af terwijl hij
zoekt, zodat er bij telefonie geen stilte valt.

---

## Deployen op Railway (aanbevolen)

1. **Zet deze map in een GitHub-repo.** Aanbevolen: een nieuwe repo, bijv.
   `buswhisky-canon-mcp`, zodat code en kennis netjes gescheiden blijven.
   (Kan ook als submap `server/` in de canon-repo; stel dan in Railway de
   *Root Directory* in op `server`.)

2. Ga naar **railway.app** → **New Project** → **Deploy from GitHub repo** en kies de repo.
   Railway detecteert Python, installeert `requirements.txt` en start via de `Procfile`
   (`python app.py`).

3. Zet onder **Variables** (allemaal optioneel, met verstandige defaults):

   | Variabele | Aanbevolen waarde | Uitleg |
   |---|---|---|
   | `BONNIE_AUTH_TOKEN` | een lange, willekeurige string | Beveiligt de server: alleen aanroepen met dit token worden bediend. |
   | `CANON_RAW_URL` | *(leeg laten)* | Default wijst al naar `main/canon.md` van de publieke repo. |
   | `CANON_REFRESH_TTL` | `300` | Elke 5 min checkt de server op de achtergrond op wijzigingen. |
   | `GITHUB_TOKEN` | *(leeg laten)* | Alleen nodig als de repo ooit privé wordt of bij GitHub rate limits. |

4. Onder **Settings → Networking**: **Generate Domain**. Je krijgt een URL zoals
   `https://buswhisky-canon-mcp-production.up.railway.app`. Het MCP-endpoint is die URL + `/mcp`.

5. Test in de browser: `https://<jouw-domein>/health` moet
   `{"status":"ok","secties_geladen":13,...}` teruggeven.

Kosten: Railway **Hobby** is ~**$5/mnd** (inclusief $5 verbruik — ruim voldoende voor dit servertje).

---

## Bonnie koppelen

Registreer de server in de assistentconfig van Bonnie. Gebruik `bonnie-config.json`
als sjabloon en vervang het domein en het token:

```json
{
  "mcp_servers": [
    {
      "url": "https://<jouw-railway-domein>/mcp",
      "alias": "canon",
      "type": "streamable_http",
      "auth_token": "<zelfde als BONNIE_AUTH_TOKEN>",
      "call-states": ["setup", "in-progress"],
      "channels": ["phone", "whatsapp"],
      "timeout-ms": 2500
    }
  ],
  "mcp_global_timeout_ms": 15000
}
```

Beheer je Bonnie niet zelf via een dashboard? Stuur deze `mcp_servers`-vermelding dan
naar Bonnie-support met het verzoek de server te registreren.

---

## Kennis bijhouden (governance)

- Wijzig kennis **alleen in de canon-repo** (`canon.md`). De server volgt vanzelf.
- Werk bij voorkeur via **pull requests** en zet een `CODEOWNERS`-bestand in de
  canon-repo, zodat elke wijziging jouw goedkeuring vereist voordat hij live gaat.
- Wil je wijzigingen binnen seconden live (i.p.v. binnen 5 min)? Voeg dan een
  **GitHub-webhook** toe die de server een refresh-seintje geeft. Dat is een kleine
  uitbreiding die we later kunnen toevoegen.

---

## Lokaal testen (optioneel)

```bash
pip install -r requirements.txt
python app.py
# In een tweede terminal:
npx @modelcontextprotocol/inspector --cli --transport http http://localhost:8080/mcp --method tools/list
```

---

## Bekende grens van deze versie

De zoekfunctie werkt op **trefwoorden** (met Nederlandse stam- en samenstelling-matching).
Dat dekt veruit de meeste bellervragen goed af. Heel vaag geformuleerde of synoniem-rijke
vragen kunnen af en toe de verkeerde sectie bovenaan zetten (bijv. de exacte prijs van
een specifiek product). De volgende stap — **semantisch zoeken** — lost dat op zonder dat
Bonnie of de canon hoeven te veranderen: alleen de zoeklogica in deze server wordt dan vervangen.
