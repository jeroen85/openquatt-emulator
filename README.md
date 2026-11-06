# OpenQuatt Emulator

Lokale desktopwerkbank en CLI om een samengevoegde Q Duo-factory-bin op een virtuele ESP32-S3 te draaien. De runner test firmwareboot, de echte OpenQuatt web-app, de PT1000 en Modbus-verkeer met beide buitenunits. De emulator gebruikt de gekozen bin als flashimage. Een firmware-ELF of debuggerbrug is overbodig.

## Installatie en gebruik

De handleiding behandelt installatie op macOS arm64, het kiezen van een Q Duo-factory-bin en de drie netwerkproeven. Je vindt er ook uitleg bij het rapport en oplossingen voor veelvoorkomende problemen. [Lees de handleiding](docs/handleiding.md)

Start na de installatie de werkbank vanuit deze repo.

```sh
python3 -m scripts.native_spi.server
```

Open [de simulatorwerkbank](http://127.0.0.1:19450/) om een factory-bin te kiezen.

## Emulatorcode testen

```sh
python3 -m unittest scripts.native_spi.test_tooling
mkdir -p .tmp/native-spi
oq_sdk="$(xcrun --sdk macosx --show-sdk-path)"
xcrun clang++ -isysroot "$oq_sdk" -isystem "$oq_sdk/usr/include/c++/v1" -std=c++17 \
  -Wall -Wextra -Werror scripts/native_spi/test_model.cpp -o .tmp/native-spi/test_model
.tmp/native-spi/test_model
```

## Gevalideerd met een ongewijzigde Q Duo-bin

De twee proeven gebruikten dezelfde factory-bin van OpenQuatt-dev `31282d13`. De SHA-256 van de invoer en de flashkopie bleef gelijk.

| Proef | Duur | Web-app | Modbus |
| --- | --- | --- | --- |
| Ethernet bij koude start | 90 s | DHCP op `10.42.0.15`, 27 geslaagde controles | 80/80 antwoorden |
| Ethernet → Wi-Fi | 180 s | Ethernet-HTTP viel weg. Na 55,5 s herstelde HTTP via Wi-Fi op `192.168.4.2`, gevolgd door 26 geslaagde vervolgcontroles. | 140/140 antwoorden |

Beide buitenunits antwoordden in beide proeven. De firmware gaf geen crashes of Modbus-timingwaarschuwingen. De hersteltijd is gemeten op de host tijdens de virtuele proef.

## Model en bereik

De uitbreiding modelleert SPI3/GDMA voor MAX31865 en SPI2/GDMA voor de W5500. Het W5500-model biedt socket 0 in MACRAW-modus, RX/TX-ringen, PHY-status en GPIO9-interrupts. De ESP-IDF-driver voert zelf DHCP en TCP/IP uit. De firmware gebruikt de normale provisioning- en herstelpaden.

De hostuitbreiding gebruikt versiegebonden MMIO-entrypoints en de S3-interruptmatrix van een lokale, ad hoc ondertekende kopie van esp-emu. Een andere emulatorbuild heeft een eigen compatibiliteitscontrole nodig. Dit prototype kwalificeert functionele Ethernet- en Wi-Fi-routes. Metingen van elektrische timing en fysieke geheugenmarges horen bij fysieke HIL.

De buitenunitfixture bouwt uit de meegeleverde GPLv3-broncode van OpenQuatt-Simulator. De oorspronkelijke revisie en hashes staan in [de herkomstgegevens](scripts/native_spi/vendor/odu/provenance.json). Firmwarebeelden, emulatorbestanden en lokale meetlogs blijven in de genegeerde runtimeomgeving.
