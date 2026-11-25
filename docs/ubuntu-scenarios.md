# HTTPS en regeling met co-simulatie

Gebruik deze proeven voor wijzigingen aan HTTPS, Power House, flowbeveiliging
of de stooklijn. De co-simulatiebackend gebruikt esp-emu 1.2.0 en draait ook
op Apple Silicon. Het rapport bevat de firmwarehash, de bron van de modellen,
de gastklok en de controles per fase.

Installeer de [CLI en runtime](handleiding.md#1-installeren). Voer de opdrachten
uit vanuit de emulatorrepo. Zie [de profielkeuze](scenarios.md#firmwareproeven-op-github)
om HTTPS en flowherstel via GitHub te starten.

## Firmware klaarzetten

Gebruik de gewone Q Duo-factory-bin. Power House, flowherstel en stooklijn
gebruiken de OpenTherm-ontvangers van de firmware. [esp-emu 1.2.0](https://github.com/espressif/esp-emulator/releases/tag/v1.2.0)
ondersteunt deze RMT-ontvangst.

```sh
oq_firmware="/pad/naar/firmware.factory.bin"
```

Bouw voor de HTTPS-succesproef een aparte bin met een lokale test-CA.
Volg de [HTTPS-receptuur](scenarios.md#https-volledig-lokaal) om de fixture
te genereren en aan de binhash te binden. Bewaar de privésleutels lokaal en
gebruik die bin uitsluitend in de emulator.

## Power House

```sh
python3 -m scripts.native_spi.run --backend cosim --firmware "$oq_firmware" \
  --scenario power-house --network wifi --memory \
  --power-house-climate mild --seconds 3300 --port-base 23330 --name ubuntu-power-house
```

De normale vraagreeks loopt via Single naar Duo, bouwt weer af, stopt en
start opnieuw. De minimumlooptijd van 300 s en minimum-uit-tijd van 240 s
blijven actief. De virtuele buitenunits volgen hun normale frequentietabel.
De profielen `mild`, `cold` en `warm-water` draaien als afzonderlijke proeven.
Het woningmodel en het leergedrag vragen eigen proeven.

Met de co-simulatiebackend wordt geheugen gemeten. De recorder levert
de vrije interne heap, het minimum sinds boot en het grootste vrije blok
bij opstarten, Single, Duo, stop en herstel. Tijdens de regelproef zijn
een SSE-stream en herhaalde webbundeldownloads actief. Voor ieder meetvenster
vereist het rapport een geslaagde download binnen één SSE-stream. De
[rapportcontrole](#rapport-beoordelen) bepaalt of de belasting aanhoudt.

## Power House met lowflowherstel

```sh
python3 -m scripts.native_spi.run --backend cosim --firmware "$oq_firmware" \
  --scenario lowflow --network wifi --memory \
  --seconds 1100 --port-base 23520 --name ubuntu-lowflow
```

Deze proef kiest Power House expliciet. Tijdens een bevestigde defrost valt
de flow weg. De runner volgt het ontstaan van de fout, de compressorstop,
de vastgehouden fout en de vrijgave na flowherstel. Daarna moet de
compressor weer mogen starten met inachtneming van de minimum-uit-tijd.
De lowflow- en hersteltimers uit het bestaande scenario blijven gelden.

Beoordeel deze timers vanaf de gefilterde flow die de firmware ziet. Het
injectiemoment is niet hetzelfde als het begin van de fout- of hersteltimer.
`fault_elapsed_window_ms` en `recovery_elapsed_window_ms` begrenzen de
meetonnauwkeurigheid. Lees de stop- en herstartopdrachten naast de bevestigde
bedrijfsfeedback; een opdracht alleen bewijst geen fysieke stilstandsmarge.

De geheugenvensters omvatten opstarten, defrost, lowflowherstel en
herstart. Ook hier moeten SSE en geslaagde webdownloads de gemeten
bedrijfsvensters dekken.

## Stooklijn

```sh
python3 -m scripts.native_spi.run --backend cosim --firmware "$oq_firmware" \
  --scenario heating-curve --network wifi --memory \
  --seconds 3000 --port-base 23340 --name ubuntu-stooklijn
```

Het Comfort-profiel begint in Single, houdt de tweede pomp tegen tijdens
defrost, schakelt daarna bij naar Duo en bouwt bij lagere vraag af naar
stabiel Single-bedrijf. De controle gebruikt de volledige
Modbus-schrijfhistorie en gemeten bedrijfsfeedback. Het geheugen wordt
bepaald bij opstarten, defrost, Duo en het stabiele Single-herstel.

## HTTPS onder webbelasting

Gebruik `oq_https_bin` en `oq_tls` uit de [HTTPS-receptuur](scenarios.md#https-volledig-lokaal).

```sh
python3 -m scripts.native_spi.run --backend cosim --firmware "$oq_https_bin" \
  --scenario https --network ethernet --https-fixture "$oq_tls" \
  --seconds 420 --port-base 23500 --name ubuntu-https
```

De Ethernet-fixture handelt DNS en NTP lokaal af en leidt de gekozen
HTTPS-domeinen naar de eigen TLS-server. De firmware moet weergegevens
accepteren en een nieuwe manifestrevisie publiceren. De proef vraagt het
manifest op terwijl de weerverbinding nog open is. De TLS-worker moet de
verbindingen één voor één afhandelen.

Daarna verbreekt de fixture een manifestverbinding. Een expliciete nieuwe
aanvraag moet een volgende publicatierevisie opleveren. Een SSE-stream en
webbundeldownloads blijven actief. Het rapport vereist geslaagde downloads
binnen het TLS-belastingsvenster. De bundel wordt iedere 5 gastseconden
aangevraagd; bij de regelproeven iedere 10 gastseconden. Iedere GET heeft
een eigen deadline van maximaal 4 hostseconden. De recorder meet vóór de belasting,
tijdens de succes-/fout-/herstelreeks en na herstel.

Gebruik voor [Onbekende CA afwijzen](scenarios.md#onbekende-ca-afwijzen) een
gewone Q Duo-bin. Die afzonderlijke proef controleert de TLS-testopstelling.

## Rapport beoordelen

Gebruik exitcode 0 en `status = PASS_COSIM` in de rapportmap die de runner
bij het starten meldt. Lees bij afkeur `failure_reasons` en de controles
onder `qualification`.

- `dynamics` bevat de regelproef, meetpunten, holds en de gecontroleerde
  OpenTherm-antwoorden. `opentherm.json` bewaart de digitale frames, inclusief
  vooruit geplande verzendingen. Alleen frames waarvan het volledige
  tijdvenster op de gastklok verstreken is tellen mee in `dynamics.wire`.
- `https` bevat de TLS-pogingen, manifestrevisies, downloads en herstelchecks.
- `memory.phases` bevat meetdekking en geheugenwaarden per venster.
- Power House en stooklijn gebruiken UART rechtstreeks via het co-simulatieprotocol op de
  Q Duo-pinnen GPIO40 (TX) en GPIO42 (RX). De runner houdt de Modbus-timingcontrole
  van de firmware aan. De overige scenario’s gebruiken de afzonderlijke UART1-TCP-brug.
- Bij vier seconden zonder SSE-event opent de runner opnieuw een stream. Een
  eventgat boven 12 gastseconden geeft ook na herstel afkeur. Aan beide randen
  van elk meetvenster moet binnen 12 gastseconden een event vallen. Een download
  telt alleen als overlappend als één stream hem volledig omsluit.
- `scenario_load` bevat de SSE-/webbelasting van de regelproeven en de
  overlapcontroles per meetvenster.

Op GitHub kies je `https` voor HTTPS of `regeling` voor flowherstel.
`uitgebreid` combineert die proeven met webgeheugen, softwareherstart en beide
netwerkproeven. Power House en stooklijn draaien lokaal via de opdrachten
hierboven of samen met `--profile regeling-kern` in de CLI. Zie de
[profielkeuze](scenarios.md#firmwareproeven-op-github).

Een onvoltooide fase of ontbrekende recorderdekking geeft afkeur. De
10 s-sampling kan korte dips missen. Heapmin is het minimum sinds boot.
Fysieke DRAM-, DMA-, stack- en elektrische marges vragen fysieke HIL.

Kies voor parallelle functionele runs verschillende poortreeksen. Reserveer
base t/m base +3. Meet baseline en kandidaat voor een geheugenvergelijking
achter elkaar op dezelfde host met dezelfde configuratie en belasting.
Noteer de architectuur en hostdeadlines. Verhoog bij een trage VM zo nodig
de hostduur, terwijl de firmwaretimers en scenario-eisen blijven gelden.

## Validatie met esp-emu 1.2.0

### Hertest Power House en stooklijn

Op 23 november 2026 draaide `regeling-kern` nog via GitHub op Ubuntu x86-64 met esp-emu 1.2.0
en OpenQuatt-dev `89e28f5`. Beide proeven deelden dezelfde gewone Q Duo-bin.
[GitHub-run en rapporten](https://github.com/jeroen85/openquatt-emulator/actions/runs/45246577482)

| Proef | Uitslag |
| --- | --- |
| Power House-vraag en Single/Duo | Afgekeurd door één te laat Modbus-antwoord van HP2. Alle regelstappen, inclusief stabiel Single, Duo, stop en herstart, waren geslaagd. |
| Stooklijn, defrostblokkade en Duo-afbouw | Geslaagd. Alle regelfasen en geheugenvensters waren compleet. De firmware gaf geen Modbus-timingwaarschuwingen. |

Power House bleef na de herstart stabiel draaien. Ruim twaalf minuten nadat
de herstartfase was afgerond, gaf de firmware bij één verzoek aan HP2 een
timeout en wees het antwoord af. De ingestelde wachttijd is 100 ms. De
firmware meldde de timeout na circa 113 ms en het late antwoord na circa
116 ms. De virtuele buitenunits stuurden 2.254 antwoorden op 2.254 verzoeken.
De runner keurt de proef af bij zo’n timingwaarschuwing. De metingen laten
open waar de vertraging ontstond tussen aanvraag en ontvangst.

### Volledige run van 20 november

De GitHub-run van 20 november 2026 gebruikte Ubuntu x86-64, esp-emu 1.2.0
en OpenQuatt `f0da4507`. De zeven gewone proeven deelden één factory-bin.
HTTPS gebruikte de testbuild met een lokale CA. De run duurde 2 uur en 32
minuten inclusief builds. [GitHub-run en artefacten](https://github.com/jeroen85/openquatt-emulator/actions/runs/44801606553) ·
[Uitslag en geheugencijfers](https://github.com/jeroen85/openquatt-emulator/pull/13#issuecomment-7048580968)

| Proef | Uitslag |
| --- | --- |
| Webbelasting en geheugen | Geslaagd |
| Instellingen behouden na softwareherstart | Geslaagd |
| W5500-Ethernet en lokale tijdsynchronisatie | Geslaagd |
| Automatische terugval van Ethernet naar Wi-Fi | Geslaagd |
| HTTPS, afgebroken verbinding en herstel | Geslaagd |
| Power House-vraag en Single/Duo | Afgekeurd. De compressor startte bij 2.600 W en stopte toen de comfortstopgrens daalde naar de gesimuleerde kamertemperatuur van 21 °C. Stabiel Single-bedrijf bleef uit. |
| Flowverlies tijdens defrost en herstel | Geslaagd |
| Stooklijn, defrostblokkade en Duo-afbouw | Afgekeurd. Alle regelfasen werden doorlopen. De firmware wees twee Modbus-antwoorden af die na 112–113 ms binnenkwamen. |

In de afzonderlijke OpenTherm-hertest ontving dezelfde ongewijzigde Q Duo-bin
geldige frames van de thermostaat en de ketel op Ubuntu arm64 en macOS
arm64. Die hertest volgde op de RMT-correctie in esp-emu 1.2.0.
[Terugkoppeling bij Espressif](https://github.com/espressif/esp-emulator/issues/66#issuecomment-7072998967)

## Eerdere verificatie met esp-emu 1.1.0

Op 17 november 2026 zijn de proeven met esp-emu 1.1.0 uitgevoerd in lokale
Ubuntu arm64-VM’s op Apple Silicon. De normale firmwaretimers bleven actief.

| Proef | Hostduur | Geslaagde bundeldownloads | Uitslag |
| --- | ---: | ---: | --- |
| Power House, mild, inclusief stop en herstart | 3300 s | 326/326 | Geslaagd |
| Power House, flowverlies tijdens defrost en herstel | 1100 s | 109/109 | Geslaagd |
| Stooklijn, defrostblokkade en Duo-afbouw | 3000 s | 296/296 | Geslaagd |
| HTTPS, serialisatie, verbinding afbreken en herstel | 420 s | 64/64 | Geslaagd |
| Onbekende CA afwijzen, opstellingscontrole | 240 s | 34/34 | Geslaagd |

Alle recorder- en webbelastingsvensters zijn compleet. De drie regelproeven
gebruiken dezelfde GPIO-labbin op lokale dev-basis `acfe7df2`; HTTPS gebruikt
de afzonderlijke test-CA-build. De invoerbins en flashkopieën bleven gelijk.
Daarnaast zijn 176 Python-regressietests en beide C++-testsuites op Ubuntu
geslaagd.

De temperatuurprofielen `cold` en `warm-water` zijn in deze ronde niet opnieuw uitgevoerd.
