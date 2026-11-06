# OpenQuatt Emulator gebruiken

Met de emulator draai je een ongewijzigde Q Duo-factory-bin op een virtuele ESP32-S3. Je kunt zien of de firmware opstart, of de OpenQuatt web-app antwoordt en of de controller communiceert met beide virtuele buitenunits. De PT1000-fixture geeft de firmware een bekende aanvoertemperatuur. Alles draait lokaal op de Mac. Voor de proef volstaat de factory-bin.

## 1. Installeren op een Mac met Apple Silicon

Open Terminal. Controleer of `uname -m` de waarde `arm64` geeft en of je Python 3.11 of hoger gebruikt. Gebruik een native Terminal-sessie als de opdracht `x86_64` geeft op een Apple Silicon-Mac.

```sh
uname -m
python3 --version
xcrun --find clang++
```

Installeer zo nodig de Xcode Command Line Tools met `xcode-select --install`. Wacht op het einde van de installatie en probeer `xcrun --find clang++` opnieuw. Installeer via Homebrew de netwerkbibliotheek libslirp en `pkgconf`. Met `brew install python` kun je zo nodig ook Python installeren. [Apple Command Line Tools](https://developer.apple.com/documentation/xcode/installing-the-command-line-tools/) · [Homebrew](https://brew.sh/) · [libslirp](https://formulae.brew.sh/formula/libslirp)

```sh
brew install libslirp pkgconf
pkg-config --modversion slirp
```

De oorspronkelijke netwerkproeven zijn uitgevoerd met libslirp 4.9.5. Noteer de uitkomst van `pkg-config` als je resultaten tussen machines vergelijkt.

Clone de emulatorrepo met een GitHub-account dat toegang heeft. Als je de broncode al lokaal hebt, open dan de bestaande map. Voer de volgende opdrachten uit vanuit de hoofdmap van die checkout, waar `README.md` en `scripts/native_spi/` staan.

```sh
git clone https://github.com/jeroen85/openquatt-emulator.git
cd openquatt-emulator
```

### Espressif-runtime klaarzetten

Gebruik exact esp-emu **0.51.0 voor macOS arm64**. De hostuitbreiding gebruikt interne adressen en functies van die specifieke build. Het archief en het uitgepakte programma hebben elk hun eigen SHA-256. Onderstaande opdrachten stoppen bij een verkeerde checksum. [Espressif-release 0.51.0](https://github.com/espressif/esp-emulator/releases/tag/v0.51.0)

```sh
(
  set -e
  oq_asset=esp-emu-0.51.0-aarch64-apple-darwin
  oq_download=.tmp/native-spi/download
  mkdir -p "$oq_download" .tmp/native-spi/runtime
  curl --fail --location \
    "https://github.com/espressif/esp-emulator/releases/download/v0.51.0/$oq_asset.tar.gz" \
    --output "$oq_download/$oq_asset.tar.gz"
  printf 'ab02524bd286ce84577639731d0e48d623ce9be65e45d9e1246dd220c1476102  %s\n' \
    "$oq_download/$oq_asset.tar.gz" | shasum -a 256 -c -
  tar -xzf "$oq_download/$oq_asset.tar.gz" -C "$oq_download"
  printf '162761d52bcee36fe8be5fd039202fed5873ca550a83a12229038311a4bbc18c  %s\n' \
    "$oq_download/$oq_asset/esp-emu" | shasum -a 256 -c -
  install -m 755 "$oq_download/$oq_asset/esp-emu" .tmp/native-spi/runtime/esp-emu.original
)
```

Wie het archief via de browser downloadt, kan het lokaal uitpakken en het bestand `esp-emu` onder dezelfde naam `.tmp/native-spi/runtime/esp-emu.original` plaatsen. Controleer met `shasum -a 256 .tmp/native-spi/runtime/esp-emu.original` of de hash overeenkomt met de tweede waarde hierboven.

## 2. De juiste firmware kiezen

Kies het samengevoegde `*.factory.bin`-bestand van een **OpenQuatt Q Duo-build voor ESP32-S3**.

- Voor een reguliere release heet het bestand `openquatt-heatpump-controller-q-duo.firmware.factory.bin`. [OpenQuatt-releases](https://github.com/OpenQuatt/OpenQuatt/releases)
- Voor een eigen build of PR gebruik je het bestand `firmware.factory.bin` dat bij de gekozen revisie van `configs/heatpump_controller_q/duo.yaml` hoort. Als de PR-publicatie alleen OTA-bestanden bevat, heb je een aparte factory-build van die revisie nodig.

De gecombineerde Q Duo-firmware bevat Wi-Fi en Ethernet. Voor Ethernet en de terugvalproef moet de gekozen firmware de W5500 en automatische verbindingskeuze ondersteunen.

Een `*.ota.bin` of losse applicatiebin mist onderdelen die de virtuele controller nodig heeft voor een koude start. De werkbank controleert de bootloader, partitietabel en het applicatiebeeld. Die controle herkent de firmwareopbouw. Controleer in de buildgegevens zelf of je de beoogde Q Duo-variant en bronrevisie hebt gekozen.

De werkbank bewaart onder `.tmp/native-spi/uploads/` een lokale kopie met de SHA-256 als bestandsnaam. Elke nieuwe proef begint met een nieuwe flashkopie van de gekozen bin. De runner controleert de hash van de invoer en de flashkopie na afloop. Voor een herhaalbare vergelijking kun je de bestandsnaam, SHA-256 en bronrevisie samen bewaren.

## 3. Een proef draaien in de desktopwerkbank

Start de werkbank vanuit de hoofdmap en laat het Terminal-venster open.

```sh
python3 -m scripts.native_spi.server
```

1. Open [de simulatorwerkbank](http://127.0.0.1:19450/) in je desktopbrowser.
2. Klik op **Kies een factory-bin** of sleep de bin in het vak. Controleer de bestandsnaam en open **Technische details** als je de SHA-256 wilt controleren.
3. Kies **Aanvoer simuleren**, bijvoorbeeld `30 °C`. Dit is de temperatuur die de virtuele PT1000 aan de firmware aanbiedt. De werkbank accepteert `0–100 °C`.
4. Kies een **Netwerkproef** uit de tabel hieronder. Gebruik voor Wi-Fi en Ethernet **90 seconden** als eerste proef en voor terugval **3 minuten**.
5. Klik op **Laden en testen**. De eerste keer bouwt de runner op de Mac de hostuitbreiding en het buitenunitmodel. Bij een volgende proef hergebruikt hij de gecontroleerde build zolang de modelbroncode, runtime en toolchain overeenkomen.
6. Volg **Controles**. Zodra de web-app via de actieve route antwoordt, kun je tijdens de proef **Open de web-app** gebruiken. Laat de netwerkinstellingen tijdens een automatische proef staan om het resultaat reproduceerbaar te houden.
7. Wacht op **Proef geslaagd** of **Proef niet geslaagd**. **Controles geslaagd · simulator actief** is een tussenstand. Na het einde sluit de firmwarewebpoort en vind je de uitkomst onder **Bekijk het testrapport**.

Met **Stoppen** breek je alleen de actieve proef af. Het resultaat wordt **Proef gestopt**. Met Ctrl+C in het Terminal-venster sluit je ook de werkbank. De rapporten blijven op schijf staan.

| Netwerkproef | Wat de runner doet | Wat je hoort te zien |
| --- | --- | --- |
| **Wi-Fi** | Voorziet de firmware via haar installatieportaal van gegevens voor een virtueel Wi-Fi-netwerk. | De web-app antwoordt via de Wi-Fi-webpoort. De werkbank toont de algemene controles. |
| **Ethernet** | Start met de virtuele kabel aangesloten. De firmware gebruikt haar eigen W5500-driver en haalt een DHCP-adres op. | **Ethernet actief** en een bereikbare web-app via de Ethernet-webpoort. Het gastadres is `10.42.0.15`. |
| **Ethernet → Wi-Fi** | Bereidt Wi-Fi voor, sluit de virtuele kabel aan, vraagt de firmware om **Automatic** en verifieert drie Ethernet-webcontroles. Daarna trekt de runner de virtuele kabel los. | **Kabel los · Wi-Fi herstellen**. Ethernet-HTTP valt weg en daarna verschijnt **Terug op Wi-Fi**. De web-app opent weer via de Wi-Fi-webpoort. |

Bij een schone flash kan het installatieportaal ook over Ethernet verschijnen. De runner voert provisioning automatisch uit. Je hebt daarvoor geen gegevens van je eigen Wi-Fi of een fysieke netwerkkabel nodig. Tijdens de eerder geslaagde terugvalproef duurde het 55,5 seconden van kabelbreuk tot de eerste geslaagde webcontrole via Wi-Fi. De gemeten hersteltijd geldt voor deze virtuele proef op de Mac.

### Poorten en adressen

| Standaard lokaal adres | Doel |
| --- | --- |
| `http://127.0.0.1:19450/` | Bedienpaneel van de werkbank |
| `http://127.0.0.1:19460/` | OpenQuatt web-app via virtuele Wi-Fi |
| `http://127.0.0.1:19461/` | OpenQuatt web-app via virtueel Ethernet |
| `127.0.0.1:19462` | UART1 voor de twee virtuele Modbus-buitenunits |

Gebruik in de browser `127.0.0.1`. Het IP-adres dat de firmware meldt, hoort bij het virtuele netwerk binnen de emulator. De lokale poort geeft de browser toegang tot die gast. Na een wisseling van Ethernet naar Wi-Fi opent **Open de web-app** de andere lokale poort. Een al geopend tabblad blijft op zijn oorspronkelijke adres.

## 4. Dezelfde proeven via de terminal

Vervang het voorbeeldpad door dat van je eigen factory-bin. Voer de opdrachten uit vanuit de hoofdmap van de emulatorrepo. Je kunt ze ook gebruiken vanuit een lokaal testscript zonder browser.

```sh
oq_firmware="/pad/naar/q-duo.factory.bin"

python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --network wifi --seconds 90 --temperature 30 --name q-duo-wifi

python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --network ethernet --seconds 90 --temperature 30 --name q-duo-ethernet

python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --network failover --seconds 180 --temperature 30 --name q-duo-failover
```

- `--seconds` bepaalt de meetduur in seconden op de host. Voor een volledige proef zijn de bovenstaande tijden een bruikbaar begin. De eerste build van de hostuitbreiding gebeurt vóór die meetduur.
- `--temperature` stelt de PT1000-fixture in. Gebruik een punt bij decimalen, bijvoorbeeld `30.5`.
- `--name` is het herkenbare voorvoegsel van de rapportmap. Gebruik letters, cijfers, underscores of koppeltekens en begin met een letter of cijfer.
- `--port-base 19600` geeft Wi-Fi poort `19600`, Ethernet zo nodig `19601` en UART1 `19602`. Kies verschillende, niet-overlappende reeksen als je twee CLI-proeven naast elkaar draait. De werkbank ondersteunt één actieve proef en gebruikt haar eigen vaste testpoorten. Met `python3 -m scripts.native_spi.server --port 19455` wijzig je uitsluitend de poort van het bedienpaneel.

De CLI drukt aan het begin de rapportmap af als `run`. Exitcode `0` betekent dat alle controles van de gekozen proef geslaagd zijn. Elke andere exitcode betekent dat de proef is afgebroken, een controle faalde of de runner niet kon starten. Voor een geautomatiseerde proef geef je de gewenste factory-bin door aan dezelfde CLI op een macOS-arm64-runner. Gebruik de exitcode en bewaar het rapport van die specifieke proef.

### Compressor, flow en defrost met OpenTherm

Start de dynamische proef met dezelfde Q Duo-factory-bin. Kies een stabiele Wi-Fi- of Ethernet-route. De standaardduur voor dit scenario is zeven minuten. Bij de geteste firmware kan de eerste compressorstart maximaal 240 seconden geblokkeerd blijven vanaf bevestigde stilstand. De proef gebruikt de ingebouwde productietimers.

```sh
python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --network wifi --scenario dynamics --name q-duo-dynamics
```

De runner schakelt OpenTherm in via de gewone instellingen van de firmware en kiest flow uit de buitenunits. Een virtuele thermostaat stuurt warmtevraag, kamertemperatuur en setpoint via OTT. Tegelijk beantwoordt een virtuele ketel de OpenTherm-verzoeken op OTB. De peers leveren digitale Manchester-pulsen aan de RMT-ontvanger. De productiedrivers van de gekozen bin verwerken de frames.

De proef begint met stilstand en nulflow. Daarna controleert hij de echte compressoropdracht en de gemeten frequentie, de gedeelde flow van de twee units in serie, de defroststatus en vierwegklep, en een korte flowonderbreking met herstel. De ODU-modelklok volgt de systeemtimer van de virtuele ESP32. De PT1000 behoudt in deze proef de ingestelde temperatuur.

In `report.json` vind je de meetpunten onder `dynamics.checkpoints`, de bevestigde OpenTherm Data-ID's onder `dynamics.wire` en de klokstand onder `guest_uptime_ms` en `odu_uptime_ms`. De eindcontrole vereist recente voortgang van de ODU-klok en recente antwoorden op beide OpenTherm-aansluitingen. `mmio.jsonl` bevat de OpenTherm-frames met richting, RMT-kanaal en virtuele tijd. Een korte flowonderbreking beproeft de feedbackroute. Een aparte, langere proef is nodig om de volledige lowflowbeveiliging en haar hersteltimer te kwalificeren.

## 5. Het resultaat lezen

Open het rapport via de werkbank of zoek `report.json` in de door de CLI gemelde map onder `.tmp/native-spi/runs/`. De status `PASS_NATIVE_EXTENSION` hoort bij een volledig geslaagde proef. Bij `NOT_QUALIFIED` geven `failure_reasons` en `qualification` aan welke controles ontbreken of faalden. Bij `STOPPED` is de proef voortijdig gestopt.

| Controle | Waar je op let in `report.json` |
| --- | --- |
| Firmwareboot | `setup_complete` is `true`. `panics` en `errors` zijn leeg. |
| Web-app | `web_checks` telt geslaagde controles. `web_assets_verified` betreft de webbundels. De laatste geslaagde webcontrole moet binnen tien seconden van het einde liggen. |
| PT1000 | `pt1000_value_c` ligt minder dan `0,15 °C` van `fixture_temperature_c`. Een kleine afwijking door de sensorrepresentatie is normaal. |
| Beide buitenunits | `peer.recent` bevat geslaagde Modbus-registeruitlezingen voor unit `1` en `2`. Controleer ook `modbus_timing_warnings` en de CRC- en fouttellers onder `peer`. |
| Ethernet | `network.ethernet_checks`, `network.ethernet_ip` en `network.wire` tonen geslaagde webcontroles, het gastadres, DHCP-bevestigingen en frameverkeer. |
| Terugval | `network.ethernet_http_lost` bevestigt verlies van HTTP over Ethernet. `wifi_recovery_event_s` is het tijdstip van de herstelmelding van de firmware. `network.wifi_recovery_checks` telt de succesvolle Wi-Fi-controles na het lostrekken van de virtuele kabel. |
| Herhaalbaarheid | `input.sha256` identificeert de firmware. `input_hash_unchanged` en `flash_file_unchanged` bevestigen de ongewijzigde bestanden. `native_build` bevat de toolchain en libslirp-versie. |

Voor de hersteltijd trek je `network.cable_cut_s` af van `network.wifi_restored_s`. Beide tijden lopen vanaf het begin van dezelfde proef. Vergelijk twee firmwarebuilds met dezelfde netwerkproef, temperatuur, duur en emulatorversie. Leg de twee firmware-SHA's en de afzonderlijke rapporten naast elkaar.

`serial.log` bevat de seriële firmwaremeldingen. Gebruik dat bestand voor bootfouten, driverfouten of timeouts rond het gemeten tijdstip. `mmio.jsonl` bevat gebeurtenissen van het native model, waaronder SPI/DMA-fouten en Ethernet-tellers. `live.json` bevat de laatst opgeslagen werkbankstatus. Deel bij een afwijking de relevante passages en de instellingen van de proef. Controleer logs en rapporten eerst op lokale paden en firmwaregegevens.

## 6. Problemen oplossen

| Symptoom | Controle of herstel |
| --- | --- |
| De werkbank laadt niet | Controleer of de Terminal-opdracht draait, of je in de emulatorcheckout staat en of het adres begint met `http://127.0.0.1:`. Als het bedienpaneel op `19450` bezet is, start de werkbank met `python3 -m scripts.native_spi.server --port 19455` en open `http://127.0.0.1:19455/`. |
| De runner meldt een bezette testpoort | Controleer of een andere proef nog draait. Stop die via haar eigen werkbank of geef je CLI-proef een vrij bereik met `--port-base`. Je kunt luisterende processen bekijken met `lsof -nP -iTCP:19460-19462 -sTCP:LISTEN`. |
| `Unsupported emulator binary` of `esp-emu.original` ontbreekt | Controleer het pad en de SHA-256 van het uitgepakte uitvoerbare bestand. Zet de exacte arm64-release terug op het beschreven pad. |
| Compilatie stopt met een SDK- of slirp-fout | Controleer `xcrun --find clang++`, `xcrun --sdk macosx --show-sdk-path` en `pkg-config --modversion slirp` in hetzelfde Terminal-venster. Bij een fout in de buildcache meldt de runner dat expliciet. Als er geen proeven draaien, kun je de getroffen map onder `.tmp/native-spi/runtime/builds/` apart bewaren en de runner opnieuw laten bouwen. |
| De werkbank weigert de bin | Kies de samengevoegde factory-bin voor Q Duo. Download het bestand opnieuw als de checksum of de lengte afwijkt. |
| Boot is groen en de web-app blijft uit | Geef de firmware tijd om haar installatieportaal en netwerkdetectie te doorlopen. Controleer daarna `failure_reasons` en `serial.log`. Kijk bij Ethernet ook of de firmware een Ethernet-adres meldt. |
| De web-app verdwijnt tijdens terugval | Wacht tot de werkbank **Terug op Wi-Fi** meldt en open de web-app via de link in de werkbank. Is er geen herstel bij het einde van de proef, controleer dan de terugvalvelden en de firmwaremeldingen. |
| Eén buitenunit blijft grijs | Kijk naar `qualification.outside_units`, de units in `peer.recent` en de CRC- en timingtellers. Controleer of het gekozen bestand voor Duo gebouwd is. |

## 7. Wanneer fysieke HIL nodig blijft

De compatibiliteitsproeven dekken boot, webtoegang, de gemodelleerde PT1000, Modbus-registerverkeer met twee buitenunits en de gekozen netwerkroute. De dynamische proef voegt digitaal OpenTherm, compressorfeedback, flow en defrost toe. Gebruik fysieke HIL voor elektrische I/O, de werkelijke OpenTherm-signalen, externe flow- en PWM-signalen en de timing en veiligheidsmarges van de fysieke installatie.

De standaardrapporten bevatten geen heapmetingen. Een aparte virtuele geheugenproef kan met vergelijkbare belasting en firmwaretelemetrie verschillen tussen builds zichtbaar maken. Valideer de werkelijke marges voor interne DRAM, het grootste vrije blok, DMA en taakstacks op de fysieke controller onder representatieve belasting.
