# HTTPS en regeling op Ubuntu

De draagbare backend gebruikt esp-emu 1.1.0 en de lokale netwerk- en
buitenunitmodellen. De proeven draaien de Q Duo-factory-bin rechtstreeks.
De rapporten bevatten de firmwarehash, fixturebronnen, virtuele klok,
Modbus-opdrachten en de controles per fase.

Installeer de Ubuntu-afhankelijkheden en runtime volgens de
[handleiding](handleiding.md#linux-en-windows-met-wsl2). Voer de commando’s
hieronder uit vanuit de emulatorcheckout.

## Firmware klaarzetten

Voor Power House, lowflow en stooklijn gebruik je de aparte Q Duo-GPIO-labbin.
De GPIO-laboverlay staat voorlopig in een aparte lokale OpenQuatt-labcheckout,
niet in de reguliere dev. Volg `scripts/hil/tools/README.md` in die checkout.
Bouw voor een andere kandidaat dezelfde overlay op de gevraagde bronrevisie;
hergebruik geen labbin van een andere firmwarewijziging.
De config is `scripts/hil/configs/gpio_opentherm_duo.yaml`. De labfirmware
gebruikt GPIO-interrupts voor OpenTherm-ontvangst, de bestaande decoders en
RMT voor zenden. De gewone firmware gebruikt RMT ook voor ontvangst.
De runner weigert voor deze Ubuntu-regelproeven een gewone factory-bin.

```sh
oq_source=/pad/naar/openquatt-labcheckout
oq_gpio_bin="$oq_source/.esphome/build/heatpump_controller_q_duo_gpio_ot/build/firmware.factory.bin"
```

HTTPS gebruikt de afzonderlijke [test-CA-build](scenarios.md#https-volledig-lokaal).
Die heeft de normale Q Duo-config als basis. De CA, certificaten en
privésleutels blijven lokaal. Het fixturemanifest bindt de HTTPS-bin aan
de CA en bronrevisie. Voor de regelproeven kun je dezelfde GPIO-bin
hergebruiken. Wanneer je ook HTTPS test, is daarvoor een extra build nodig.
Gebruik beide labimages uitsluitend in de emulator.

## Power House

```sh
python3 -m scripts.native_spi.run --backend cosim --firmware "$oq_gpio_bin" \
  --scenario power-house --network wifi --memory \
  --power-house-climate mild --seconds 3300 --port-base 23330 --name ubuntu-power-house
```

De normale vraagreeks loopt via Single naar Duo, bouwt weer af, stopt en
start opnieuw. De minimumlooptijd van 300 s en minimum-uit-tijd van 240 s
blijven actief. De normale ODU-respons en frequentietabel worden gebruikt.
De temperatuurprofielen `mild`, `cold` en `warm-water` zijn afzonderlijke
proeven. Het interne woningmodel en leren vragen eigen proeven.

Met de co-simulatiebackend wordt geheugen gemeten. De recorder levert
de vrije interne heap, het minimum sinds boot en het grootste vrije blok
bij opstarten, Single, Duo, stop en herstel. Tijdens de regelproef zijn
een SSE-stream en herhaalde webbundeldownloads actief. Ieder gemeten
bedrijfsvenster moet een geslaagde download met SSE-overlap bevatten.
De runner bewaart de eventtijden en keurt eventgaten boven 12 s of een
voortijdig gesloten stream af. Aan beide randen van elk bedrijfsvenster
moet binnen 12 s een SSE-event zijn ontvangen.

## Power House met lowflowherstel

```sh
python3 -m scripts.native_spi.run --backend cosim --firmware "$oq_gpio_bin" \
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
python3 -m scripts.native_spi.run --backend cosim --firmware "$oq_gpio_bin" \
  --scenario heating-curve --network wifi --memory \
  --seconds 3000 --port-base 23340 --name ubuntu-stooklijn
```

Het Comfort-profiel begint in Single, houdt de tweede pomp tegen tijdens
defrost, schakelt daarna bij naar Duo en bouwt bij lagere vraag af naar
stabiel Single-bedrijf. De controle gebruikt de volledige
Modbus-schrijfhistorie en gemeten bedrijfsfeedback. Het geheugen wordt
bepaald bij opstarten, defrost, Duo en het stabiele Single-herstel.

## HTTPS onder webbelasting

Bereid eerst de lokale CA en HTTPS-bin voor en bind ze volgens de
[HTTPS-instructies](scenarios.md#https-volledig-lokaal).

```sh
oq_tls="$PWD/.tmp/native-spi/local-https"
oq_https_bin="$oq_source/.esphome/build/virtual_https/build/firmware.factory.bin"
python3 -m scripts.native_spi.run --backend cosim --firmware "$oq_https_bin" \
  --scenario https --network ethernet --https-fixture "$oq_tls" \
  --seconds 420 --port-base 23500 --name ubuntu-https
```

De Ethernet-fixture handelt DNS en NTP lokaal af en leidt de gekozen
HTTPS-domeinen naar de eigen TLS-server. De firmware moet weergegevens
accepteren en een nieuwe manifestrevisie publiceren. Een manifestaanvraag
wordt tijdens de weerverbinding aangevraagd. Alle TLS-pogingen moeten
aantoonbaar na elkaar worden uitgevoerd.

Daarna verbreekt de fixture een manifestverbinding. Een expliciete nieuwe
aanvraag moet een volgende publicatierevisie opleveren. Een SSE-stream en
webbundeldownloads blijven actief. Het rapport vereist geslaagde downloads
binnen het TLS-belastingsvenster. De bundel wordt iedere 5 gastseconden
aangevraagd; bij de regelproeven iedere 10 gastseconden. Iedere GET heeft
een eigen deadline van maximaal 4 hostseconden. De recorder meet vóór de belasting,
tijdens de succes-/fout-/herstelreeks en na herstel.

`https-reject` blijft de afzonderlijke controle **Onbekende CA afwijzen**
voor de testopstelling. Gebruik daarvoor een bin zonder de lokale CA.

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
- `scenario_load` bevat de SSE-/webbelasting van de regelproeven en de
  overlapcontroles per meetvenster.

Deze proeven zijn lokaal via `run.py` beschikbaar. Het profiel `uitgebreid`
en de GitHub-workflow blijven de vier bestaande draagbare proeven gebruiken.

Een onvoltooide fase of ontbrekende recorderdekking geeft afkeur. De
10 s-sampling kan korte dips missen. Heapmin is het minimum sinds boot.
Fysieke DRAM-, DMA-, stack- en elektrische marges vragen fysieke HIL.

Kies voor parallelle functionele runs verschillende poortreeksen. Reserveer
base t/m base +3. Meet baseline en kandidaat voor een geheugenvergelijking
achter elkaar op dezelfde host met dezelfde configuratie en belasting.
Noteer de architectuur en hostdeadlines. Verhoog bij een trage VM zo nodig
de hostduur, terwijl de firmwaretimers en scenario-eisen blijven gelden.

## Lokale verificatie

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

Deze nieuwe scenario’s en hostdeadlines zijn nog niet op native Ubuntu
x86-64 of WSL2 gekwalificeerd. De temperatuurprofielen `cold` en
`warm-water` zijn in deze ronde niet opnieuw uitgevoerd.
