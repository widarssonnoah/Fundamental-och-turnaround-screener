# Fundamental och turnaround screen

Två screeners i en körning, presenterade i en HTML-sida med två flikar:

- **Fundamental**: poängbaserad (0 till 100), billiga bolag som börjat vända. Ingen hård filtrering.
- **Turnaround, tidig fas**: bara tidig fas ur din `turnaround_screener.py`, sorterad på **Score (0 till 100)**. Vändningsstyrka (0 till 10) visas som extra kolumn men styr inte sorteringen.

GitHub kör det automatiskt **10:00 och 16:00 svensk tid på vardagar** och publicerar resultatet på GitHub Pages.

## Sätt upp (en gång)

1. Skapa ett **publikt** repo på GitHub, till exempel `fundamental-turnaround-screen`.
2. Ladda upp alla filer i den här mappen, **inklusive den dolda mappen `.github`**. Enklast är `git`:
   ```
   git init && git add -A && git commit -m "Första version"
   git branch -M main
   git remote add origin https://github.com/DITT-ANVÄNDARNAMN/fundamental-turnaround-screen.git
   git push -u origin main
   ```
3. På GitHub: **Settings, Pages, Build and deployment, Source: GitHub Actions**.
4. Gå till fliken **Actions**, välj *Fundamental och turnaround screen* och klicka **Run workflow**. Första körningen tar ofta 20 till 40 minuter.
5. Sidan finns därefter på `https://DITT-ANVÄNDARNAMN.github.io/fundamental-turnaround-screen/`
   (heter repot `DITT-ANVÄNDARNAMN.github.io` blir adressen bara `https://DITT-ANVÄNDARNAMN.github.io/`).

Direktlänk till turnaround-fliken: lägg till `#turnaround` sist i adressen.

## Tider och sommartid

GitHub räknar i UTC och känner inte till sommartid. Därför ligger fyra schematider i `.github/workflows/screener.yml`, och ett vaktsteg släpper bara igenom körningen när klockan i Stockholm är 10 eller 16. GitHub kan dessutom försena schemalagda körningar med 5 till 30 minuter när det är hög belastning. Vill du ändra tiderna: ändra `cron`-raderna och timmarna i vaktsteget.

Två saker att känna till:
- GitHub stänger av schemalagda körningar i ett repo som varit helt inaktivt i 60 dagar. Gör någon ändring (eller klicka *Enable workflow*) om det händer.
- Kurser vid 10:00 och 16:00 inkluderar den pågående handelsdagen (delvis dagsstapel), så indikatorerna förändras något under dagen.

## Köra lokalt

```
pip install -r requirements.txt
python build.py                   # skriver docs/index.html
python build.py --demo            # påhittad data, utan internet, skriver demo_output/index.html
python build.py --max-tickers 80  # snabbtest
```

## Filer

| Fil | Innehåll |
|---|---|
| `build.py` | Startpunkt. Hämtar kurser (batchar), kör båda screenerna, bygger rapporten |
| `template.html` | Utseendet på rapporten (flikarna) |
| `report.py` | Bygger HTML och CSV |
| `turnaround.py` | Plockar tidig fas ur din turnaround-logik och sorterar på Score |
| `aktiescreener.py` | Fundamental-poängen (kan också köras fristående) |
| `master_screener.py`, `turnaround_screener.py`, `avanzaEuUs.py` | Dina filer, oförändrade |
| `demo.py` | Påhittad data för test |

## Justera

- **Aktielista:** `avanzaEuUs.py`.
- **Vikter och trösklar, fundamental:** `CATEGORIES` och `CRITERIA` i `aktiescreener.py` (vikterna kan också ändras direkt i rapporten).
- **Vad som räknas som turnaround:** `turnaround_screener.py` (konstanterna högst upp och `compute_turnaround`).

## Begränsningar

- Yahoo Finance är en inofficiell källa och blockerar ibland GitHubs servrar. Går det för dåligt (färre än 30 % av tickers får kursdata) avbryts körningen och förra sidan ligger kvar. Saknas bara nyckeltal visas en gul notering i fundamental-fliken.
- Fundamental-fliken utesluter micro caps som standard. Turnaround-fliken använder hela listan med ditt volymkrav.
- Verktyget sorterar data. Det är inte finansiell rådgivning.
