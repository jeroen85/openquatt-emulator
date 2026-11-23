# OpenQuatt Emulator

Start een OpenQuatt Q Duo-factory-bin op een virtuele ESP32-S3. Test de OpenQuatt web-app, beide buitenunits, de PT1000 en digitaal OpenTherm. Geef de samengevoegde `*.factory.bin` van een release of eigen build op. De emulator gebruikt per proef een eigen flashkopie. Voor de gewone firmwareproeven volstaat die bin.

## Aan de slag

| Waar | Hoe |
| --- | --- |
| **Ubuntu x64 of arm64, macOS met Apple Silicon** | Gebruik de CLI met `--backend cosim` en esp-emu 1.2.0. [Installatie en eerste rooktest](docs/handleiding.md#1-installeren) |
| **Windows** | Volg de Ubuntu-route in WSL2. Een afzonderlijke WSL2-validatie staat nog open. [WSL2-instructies](docs/handleiding.md#linux-en-windows-met-wsl2) |
| **Desktopwerkbank op Apple Silicon** | Start `python3 -m scripts.native_spi.server` na installatie van de native backend. Open de [werkbank](http://127.0.0.1:19450/) voor Wi-Fi, Ethernet en netwerkterugval. [Werkbank gebruiken](docs/handleiding.md#3-een-proef-draaien-in-de-desktopwerkbank) |

Voor Ethernet en netwerkterugval heb je de Q Duo-factory-bin met W5500-ondersteuning nodig. Zie [De juiste firmware kiezen](docs/handleiding.md#2-de-juiste-firmware-kiezen).

## Wat kun je testen?

Het GitHub-profiel `uitgebreid` draait acht firmwareproeven op Ubuntu. Met de CLI kun je dezelfde profielen draaien.

| Proef | Wat wordt gecontroleerd |
| --- | --- |
| **Webbelasting en geheugen** | Boot, Wi-Fi, web-app, PT1000 en Modbus met beide buitenunits. Meet vrije interne heap, heapmin en het grootste vrije blok tijdens gelijktijdige SSE en downloads. |
| **Softwareherstart** | De firmware start opnieuw en leest de eerder opgeslagen instelling terug. |
| **Ethernet en NTP** | W5500, DHCP, webtoegang en synchronisatie met een lokale klok. |
| **Ethernet → Wi-Fi** | De virtuele kabel valt weg en de web-app wordt via Wi-Fi weer bereikbaar. |
| **HTTPS** | Lokaal weer- en manifestverkeer, een afgebroken TLS-verbinding en herstel onder webbelasting. |
| **Power House** | Oplopende en aflopende warmtevraag, Single/Duo-overgangen, stop en herstart. |
| **Flowverlies tijdens defrost** | Compressorstop, vrijgave na flowherstel en herstart na de minimum-uit-tijd. |
| **Stooklijn** | Defrostblokkade, bijschakelen naar Duo en afbouw naar stabiel Single. |

Lokaal kun je ook de [digitale OpenTherm-uitwisseling met thermostaat en ketel](docs/handleiding.md#compressor-flow-en-defrost-met-opentherm) draaien via co-simulatie. De native macOS-backend biedt aanvullende proeven, onder meer voor ODU-respons, bronuitval, Modbus-uitval, koelen, MQTT, OTA en energiehistorie. De [scenariogids](docs/scenarios.md#pr-tests-kiezen) helpt bij het kiezen van een proef.

## Testuitslagen

Op **23 november 2026** doorliepen Power House en stooklijn alle regelfasen op Ubuntu x86-64 met OpenQuatt `89e28f5` en esp-emu 1.2.0. **Stooklijn slaagde. Power House kreeg afkeur door één te laat Modbus-antwoord.** [Hertest en diagnose](docs/ubuntu-scenarios.md#hertest-power-house-en-stooklijn)

De rooktest met webbelasting en geheugen slaagde diezelfde dag op `main` met dezelfde OpenQuatt-revisie. [Rooktest](https://github.com/jeroen85/openquatt-emulator/actions/runs/45243760553)

Op **20 november 2026** slaagden **zes van de acht** proeven van `uitgebreid` op Ubuntu x86-64 met esp-emu 1.2.0 en OpenQuatt `f0da4507`. Power House bereikte geen stabiele eerste Single-fase. De stooklijn doorliep alle fasen en kreeg afkeur door twee te late Modbus-antwoorden. Zeven proeven deelden de gewone Q Duo-bin. HTTPS gebruikte een aparte testbuild met een lokale CA. [Uitslagen per proef](docs/ubuntu-scenarios.md#validatie-met-esp-emu-120) · [GitHub-run](https://github.com/jeroen85/openquatt-emulator/actions/runs/44801606553)

De [vroege netwerkmetingen](docs/handleiding.md#referentiemeting-ethernet-en-terugval) horen bij de oorspronkelijke Ethernet-validatie op macOS. Gebruik fysieke HIL voor elektrische signalen, een fysieke power-on en de werkelijke geheugen- en timingmarges. [Dekkingsgrenzen](docs/handleiding.md#7-wanneer-fysieke-hil-nodig-blijft)

## GitHub Actions

- **PR in deze emulatorrepo.** Zonder HIL-label draait `basis` op Ubuntu x64. Voeg bijvoorbeeld `hil:https`, `hil:regeling` of `hil:stooklijn` toe voor gerichte proeven. De bot plaatst één uitslag bij de PR, met firmwarehashes en beschikbare geheugencijfers.
- **PR in OpenQuatt.** Open [Actions → Virtual HIL → Run workflow](https://github.com/jeroen85/openquatt-emulator/actions/workflows/virtual-hil.yml) in deze repo en zet `openquatt_ref` op `refs/pull/<nummer>/merge`. Kies een profiel en lees de uitslag in de Actions-run.

De geselecteerde proeven draaien één keer achter elkaar op dezelfde Ubuntu-runner. De gewone Q Duo-bin wordt gedeeld. Voor HTTPS volgt één aparte testbuild. Een geheugenbaseline vraagt alleen een extra build bij een andere OpenQuatt-revisie. Reserveer voor `uitgebreid` circa 2,5 uur inclusief builds. [Profielkeuze, baseline en PR-rapport](docs/scenarios.md#firmwareproeven-op-github)

## Emulatorcode testen

De Python-suite draait op Ubuntu en macOS.

```sh
python3 -m unittest discover -s scripts/native_spi -t . -p 'test_*.py'
```

De C++-tests controleren de modellen en het harnas. Zie de stap **Test the native models** in de [GitHub-workflow](.github/workflows/virtual-hil.yml) voor Ubuntu. Op macOS voer je ze zo uit:

```sh
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

## Modellen en herkomst

De emulator koppelt MAX31865 via SPI3/GDMA en W5500 via SPI2/GDMA. De W5500-fixture modelleert onder meer MACRAW, RX/TX-ringen, PHY-status en GPIO9-interrupts. De firmware handelt DHCP en TCP/IP zelf af. Bij co-simulatie levert esp-emu de virtuele klok en RMT-ontvangst voor OpenTherm. De native macOS-backend gebruikt versiegebonden MMIO-functies en de S3-interruptmatrix.

De buitenunitfixture bouwt uit de meegeleverde GPLv3-broncode van OpenQuatt-Simulator. De revisie en hashes staan in de [herkomstgegevens](scripts/native_spi/vendor/odu/provenance.json). Firmwarebeelden, emulatorbestanden en meetlogs staan lokaal onder `.tmp/native-spi/`.
