# OpenQuatt Emulator

Lokale desktopwerkbank en CLI om een samengevoegde Q Duo-factory-bin op een virtuele ESP32-S3 te draaien. De runner test firmwareboot, de echte OpenQuatt web-app, de PT1000 en Modbus-verkeer met beide buitenunits. De emulator gebruikt de gekozen bin als flashimage. Een firmware-ELF of debuggerbrug is overbodig.

## Starten op macOS arm64

Vereist Python 3.11 of nieuwer, de Xcode Command Line Tools en libslirp 4.9 met pkg-config. De bevestigde libslirp-versie is 4.9.5.

```sh
brew install libslirp pkgconf
mkdir -p .tmp/native-spi/runtime
```

Pak de macOS-arm64-release van [esp-emu 0.51.0](https://github.com/espressif/esp-emulator/releases/tag/v0.51.0) uit. Plaats het oorspronkelijke uitvoerbare bestand onder `.tmp/native-spi/runtime/esp-emu.original`. De vereiste SHA-256 van dit uitgepakte bestand is `162761d52bcee36fe8be5fd039202fed5873ca550a83a12229038311a4bbc18c`.

```sh
python3 -m scripts.native_spi.server
```

Open [de werkbank](http://127.0.0.1:19450/), kies een factory-bin en selecteer de netwerkproef. De werkbank genereert de hostuitbreiding en de buitenunitfixture bij de eerste run. Als 19450 bezet is, kun je de werkbank op een andere poort starten met `--port 19455`.

| Netwerkproef | Gedrag |
| --- | --- |
| Wi-Fi | Provisioning via de firmware en controles op de Wi-Fi-webpoort. |
| Ethernet | Virtuele kabel aangesloten bij boot. DHCP en HTTP lopen via de W5500. |
| Ethernet → Wi-Fi | Wifi voorbereiden, Ethernet activeren, HTTP over Ethernet bevestigen, kabel lostrekken en automatisch Wi-Fi-herstel controleren. |

Bij een schone flash kan de firmware haar installatieportaal op beide interfaces tonen. De runner vult daar de Wi-Fi-gegevens van de virtuele fixture in. De firmware kiest zelf de actieve route.

De Wi-Fi-webpoort is standaard 19460. Ethernet gebruikt 19461 en UART1 gebruikt 19462. De poorten luisteren op loopback. De Ethernetfixture gebruikt libslirp met DHCP op een eigen virtueel subnet, een lokaal HTTP-forward, restricted mode en uitgeschakelde DNS. De Wi-Fi-route gebruikt het user-netwerk van esp-emu in restricted mode.

## Uitvoeren zonder browser

```sh
python3 -m scripts.native_spi.run --firmware /pad/naar/q-duo.factory.bin --network ethernet --seconds 90
python3 -m scripts.native_spi.run --firmware /pad/naar/q-duo.factory.bin --network failover --seconds 180
```

Kies een ander poortbereik met `--port-base 19600`. Elke run bewaart `report.json`, seriële uitvoer en native events onder `.tmp/native-spi/runs/`. Exitcode 0 betekent dat alle controles voor de gekozen proef zijn geslaagd. De failoverproef vereist ook Ethernet-HTTP, transportverlies na de virtuele kabelbreuk en een door de firmware gemelde terugval met herstelde HTTP-toegang via Wi-Fi.

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
