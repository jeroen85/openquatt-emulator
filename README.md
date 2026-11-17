# OpenQuatt Emulator

Lokale desktopwerkbank en CLI om een samengevoegde Q Duo-factory-bin op een virtuele ESP32-S3 te draaien. De runner test firmwareboot, de echte OpenQuatt web-app, de PT1000 en Modbus-verkeer met beide buitenunits. De emulator gebruikt de gekozen bin als flashimage. Een firmware-ELF of debuggerbrug is overbodig.

## Installatie en gebruik

Op Ubuntu en Apple Silicon draait de CLI via co-simulatie met esp-emu 1.1.0. Het profiel `uitgebreid` test webbelasting en geheugen, softwareherstart, Ethernet met lokale NTP en terugval naar Wi-Fi. Voor Windows is er een WSL2-route. Power House, lowflowherstel en stooklijn zijn via co-simulatie beschikbaar met de aparte OpenTherm-GPIO-labbin. De desktopwerkbank en de overige regelscenario’s gebruiken de lokale macOS-backend. [Installatie en profielkeuze](docs/handleiding.md#linux-en-windows-met-wsl2)

De handleiding behandelt installatie op macOS arm64, het kiezen van een Q Duo-factory-bin en de drie netwerkproeven. De CLI biedt ook een dynamische proef met OTT, OTB, compressor, flow en defrost. Je vindt er uitleg bij het rapport en oplossingen voor veelvoorkomende problemen. [Lees de handleiding](docs/handleiding.md)

Start op macOS na de installatie de werkbank vanuit deze repo.

```sh
python3 -m scripts.native_spi.server
```

Open [de simulatorwerkbank](http://127.0.0.1:19450/) om een factory-bin te kiezen.

Voor de virtuele dagmeting met OpenTherm-ketel en GPIO-flow is er een aparte labbuild met regressietests. [Dagmeting met ketelwarmte](docs/ketelproef.md)

De CLI biedt een Power House-basisproef met normale vraagopbouw en -afbouw, Single/Duo-overgangen en drie temperatuurprofielen. Er zijn aanvullende proeven voor gemeten ODU-respons en tijdelijke ODU-fasen, een aparte stooklijnproef en lokale HTTPS met foutinjectie en herstel. De HTTPS-succesproef gebruikt een afzonderlijke test-CA-build. **Onbekende CA afwijzen** controleert de TLS-testopstelling met de gewone factory-bin. De [scenariohandleiding](docs/scenarios.md) beschrijft testkeuze, opdrachten en dekkingsgrenzen.

Met de aparte [OpenTherm-GPIO-labbuild](docs/handleiding.md#opentherm-uitval-en-herstel-op-ubuntu) kun je op Ubuntu de digitale uitwisseling met thermostaat en ketel testen, inclusief uitval en herstel.

De CLI biedt ook vaste proeven voor defrostketelhulp, OpenTherm-setpointgrenzen, herstart en OTA op een eigen flashkopie, geheugen tijdens een export, bronuitval, Modbus-uitval, dauwpuntbeveiliging, MQTT-herverbinding onder webbelasting en de uur- en daggrens van de energiehistorie. [Kies een proef bij je wijziging](docs/scenarios.md#pr-tests-kiezen)

## Emulatorcode testen

```sh
python3 -m unittest discover -s scripts/native_spi -t . -p 'test_*.py'
mkdir -p .tmp/native-spi
oq_sdk="$(xcrun --sdk macosx --show-sdk-path)"
xcrun clang++ -isysroot "$oq_sdk" -isystem "$oq_sdk/usr/include/c++/v1" -std=c++17 \
  -Wall -Wextra -Werror scripts/native_spi/test_model.cpp -o .tmp/native-spi/test_model
.tmp/native-spi/test_model
xcrun clang++ -isysroot "$oq_sdk" -isystem "$oq_sdk/usr/include/c++/v1" -std=c++17 \
  -Wall -Wextra -Werror scripts/native_spi/test_scenarios.cpp scripts/native_spi/native/odu_bridge.cpp \
  -o .tmp/native-spi/test_scenarios
.tmp/native-spi/test_scenarios
```

## Ubuntu-proeven voor HTTPS en regeling

[HTTPS, Power House, lowflowherstel en stooklijn](docs/ubuntu-scenarios.md)
draaien via de draagbare backend met geheugenmetingen en gelijktijdige
SSE-/webbelasting. De regelproeven gebruiken de OpenTherm-GPIO-labbin.
HTTPS gebruikt een afzonderlijke lokale test-CA-build.

## GitHub Actions

De workflow [Virtual HIL](.github/workflows/virtual-hil.yml) draait standaard één Q Duo-rooktest met geheugenmeting onder webbelasting op Linux x64. Kies bij een handmatige run de OpenQuatt-ref en een Ubuntu-profiel. Met `uitgebreid` draaien de vier proeven achter elkaar op één Ubuntu-runner. De geheugenproef bevat ook de rooktest. De proeven delen één Q Duo-factory-bin. Een baseline-ref leidt alleen tot een extra build als hij naar een andere OpenQuatt-commit verwijst. [Profielen en rapporten](docs/scenarios.md#firmwareproeven-op-github)

## Gevalideerd met een ongewijzigde Q Duo-bin

De twee proeven gebruikten dezelfde factory-bin van OpenQuatt-dev `31282d13`. De SHA-256 van de invoer en de flashkopie bleef gelijk.

| Proef | Duur | Web-app | Modbus |
| --- | --- | --- | --- |
| Ethernet bij koude start | 90 s | DHCP op `10.42.0.15`, 27 geslaagde controles | 80/80 antwoorden |
| Ethernet → Wi-Fi | 180 s | Ethernet-HTTP viel weg. Na 55,5 s herstelde HTTP via Wi-Fi op `192.168.4.2`, gevolgd door 26 geslaagde vervolgcontroles. | 140/140 antwoorden |

Beide buitenunits antwoordden in beide proeven. De firmware gaf geen crashes of Modbus-timingwaarschuwingen. De hersteltijd is gemeten op de host tijdens de virtuele proef.

## Model en bereik

De uitbreiding modelleert SPI3/GDMA voor MAX31865 en SPI2/GDMA voor de W5500. Het W5500-model biedt socket 0 in MACRAW-modus, RX/TX-ringen, PHY-status en GPIO9-interrupts. De ESP-IDF-driver voert zelf DHCP en TCP/IP uit. De firmware gebruikt de normale provisioning- en herstelpaden.

De hostuitbreiding gebruikt versiegebonden MMIO-entrypoints en de S3-interruptmatrix van een lokale, ad hoc ondertekende kopie van esp-emu. Een andere emulatorbuild heeft een eigen compatibiliteitscontrole nodig. Het model gebruikt de virtuele SYSTIMER als ODU-klok en koppelt digitale OpenTherm-peers aan de native RMT-ontvanger. Metingen van elektrische timing en fysieke geheugenmarges horen bij fysieke HIL.

De buitenunitfixture bouwt uit de meegeleverde GPLv3-broncode van OpenQuatt-Simulator. De oorspronkelijke revisie en hashes staan in [de herkomstgegevens](scripts/native_spi/vendor/odu/provenance.json). Firmwarebeelden, emulatorbestanden en lokale meetlogs blijven in de genegeerde runtimeomgeving.
