# Virtuele dagmeting met ketelwarmte

Deze proef gebruikt een Q Duo-labbuild met extra meetpunten. De runner controleert of dezelfde dagmeting blijft lopen in CM3, met warmtepomp en ketel, en na de overgang naar CM4, met alleen de ketel. Het scenario draait lokaal op een Mac met Apple Silicon. Flash deze labbuild nooit op een fysieke controller.

De labrunner accepteert Power House-niveaus 0–20 voor de gevraagde en toegepaste waarden van beide buitenunits. Het `ot-boiler`-profiel accepteert ook `OQ_LAB_FLOW` naast OpenTherm. Flowtelemetrie is optioneel voor het ketelprofiel. Na de eerste flowmarker bewaakt de runner dat de metingen blijven binnenkomen.

## Testkopie klaarzetten

Voer de volgende blokken in dezelfde terminal uit. Begin in de hoofdmap van de emulatorrepo. Je hebt Git, Python 3.12 tot en met 3.14 en de Xcode Command Line Tools nodig. Voor de labbuild heb je ook Node.js 24 nodig.

```sh
oq_emulator="$PWD"
oq_lab=$(mktemp -d "${TMPDIR:-/tmp}/oq-boiler-day.XXXXXX")

git -C "$oq_lab" init --quiet
git -C "$oq_lab" fetch --depth=1 https://github.com/jeroen85/OpenQuatt-1.git \
  b4a986654902806eee914f28e1c8749d6ef62c54
git -C "$oq_lab" switch --detach FETCH_HEAD

cp -R "$oq_emulator/fixtures/boiler-day/overlay/." "$oq_lab/"
cd "$oq_lab"
mkdir -p scripts/simulator/vendor/odu
cp -R "$oq_emulator/scripts/native_spi/vendor/odu/." scripts/simulator/vendor/odu/
python3 -B -m unittest -v scripts.tests.test_simulator_boiler_flow_regression
```

De vastgezette OpenQuatt-revisie bevat de dagmeting met ketelwarmte. De overlay voegt het harnas en de virtuele I/O voor deze ketelproef toe. Gebruik in deze testkopie de onderstaande bouwopdracht en de aparte dagmetingrunner. De regressietests controleren het volledige niveaubereik, de combinatie van OpenTherm en GPIO-flow en de bewaking op verouderde flowtelemetrie. [Firmwarebron](https://github.com/jeroen85/OpenQuatt-1/tree/b4a986654902806eee914f28e1c8749d6ef62c54)

## Labbuild maken

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r .github/requirements-esphome.txt
npm ci
python3 -B scripts/simulator/lab.py build --lab-profile ot-boiler --install
```

De build maakt een factory-bin en een ELF-bestand. Bij het starten verifieert de runner de bestanden en de bijbehorende bronnen, waaronder de GPIO-flowfixture en de klokheader, met het bouwmanifest. `--install` haalt zo nodig esp-emu 0.51.0 op en controleert de checksum van het archief. De installatiestappen hebben internettoegang nodig. Tijdens de proef communiceren emulator en runner via lokale poorten.

De labfirmware start met een vaste datum en levert GPIO-pulsen voor 800 L/h. De virtuele ketel meldt 32 °C ketelwater en 28 °C retour. Een gesimuleerde HA-bron levert de aanvoertemperatuur. Zodra keteltransport actief is, telt deze bron 6 °C bij de HP2-uitlaat op. De fixture verkort enkele warmtepomptimers. De warmstartbeveiliging en de minimale ketel-uit-tijd van 120 seconden blijven actief.

## Proef draaien en resultaat lezen

Kies twee vrije lokale poorten en een uitvoermap die nog niet bestaat. De proef duurt enkele minuten.

```sh
PYTHONPATH="$PWD" python3 -B -u "$oq_emulator/fixtures/boiler-day/run-dagmeting.py" \
  --output "$oq_lab/dagmeting" --http-port 19650 --uart-port 19652
```

In CM3 controleert de runner of een compressor draait en de ketel via OpenTherm bijspringt. Daarna simuleert hij een storing op beide buitenunits. In CM4 moeten beide compressoren stilstaan en moet de ketel verwarmen. In beide fasen controleert de runner of GPIO-pulsen blijven binnenkomen en of dezelfde dagteller minstens 20 seconden doorloopt over drie meetmomenten. De proef meet de overgang en het doorlopen van de teller. De volledige dagmeting heeft een doelduur van 24 uur.

Lees het oordeel in `dagmeting/report.json`. Bij `PASS` sluit de runner af met exitcode 0. Bij een afwijking geven `baseline.jsonl`, `cm3.jsonl` en `cm4.jsonl` de meetpunten. De runner stopt de emulator na afloop. Het algemene labrapport onder `dagmeting/emulator/` kan daardoor `CANCELLED` tonen.

Bij de referentieproef draaide HP1 in CM3 op 55 Hz. In CM4 stonden beide compressoren stil. In beide fasen leverde de ketel circa 5,6 kW en bleef de gemeten flow circa 800 L/h. De dagteller ging per fase 20 seconden vooruit zonder wisseling van meetcontext. Dit waren virtuele metingen met ESPHome 2026.10.0, ESP-IDF 5.5.6 en esp-emu 0.51.0.
