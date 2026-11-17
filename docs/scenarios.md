# Vaste regressiescenario’s

Gebruik deze CLI-proeven voor wijzigingen aan onder meer Power House, stooklijnbedrijf, OpenTherm, herstarts en netwerkverkeer. De firmwareproeven laden de gekozen Q Duo-factory-bin rechtstreeks, behalve de lokale HTTPS-succesproef: die gebruikt een afzonderlijke Q Duo-testbuild met een eigen test-CA. Een ELF of debugger is voor geen van deze runs nodig.

Kies de exacte bronrevisie, bouw `configs/heatpump_controller_q/duo.yaml` en bewaar de bronrevisie, configuratie, ESPHome-/ESP-IDF-versie en SHA-256 van de factory-bin. De imagecontrole herkent de firmwareopbouw; zij bewijst de bronrevisie niet. De [installatiehandleiding](handleiding.md) beschrijft de runtime en poorten.

Voer de opdrachten uit vanuit de emulatorrepo en stel het pad naar de gekozen gewone factory-bin in.

```sh
oq_firmware="/pad/naar/firmware.factory.bin"
```

## PR-tests kiezen

Begin bij een firmware-PR met de rooktest `compatibility` van 90 s. Kies daarna de proeven die passen bij de wijziging. Een wijziging aan alleen documentatie vraagt geen firmwareproef. Deze indeling beschrijft de testkeuze; er wordt hiermee geen automatische GitHub-workflow ingesteld.

Power House is de primaire regelroute van OpenQuatt. Bij wijzigingen aan de warmtepompaansturing staan de Power House-proeven daarom voorop. De stooklijnproef onderzoekt de afzonderlijke modus `Water Temperature Control (heating curve)`.

| Wijziging | Aanvullende proef | Wat je ermee onderzoekt |
| --- | --- | --- |
| Normale Power House-aansturing of een brede regelwijziging | Power House-basisproef (`power-house`), met de drie temperatuurprofielen | Opbouw en afbouw van warmtevraag, normale Single/Duo-overgangen, stop en herstart. |
| Power House-dispatch, vermogensinschatting of Single/Duo-keuze | Power House: gemeten ODU-respons (`odu-response`) | Reactie op het verschil tussen gevraagde en gemeten frequentie, Single-keuze en stop/herstart. |
| Power House bij ontbrekende frequentie, opstartverhoging of oil return | Power House: tijdelijke ODU-fasen (`odu-transients`) | Benodigde capaciteit behouden tijdens de afwijking en terugkeren naar normaal bedrijf. |
| Stooklijnregeling | `heating-curve` | RECOVERY/MAINTAIN, defrostblokkade, Duo-afbouw en stabiel Single. |
| OpenTherm, compressor-, flow- of defrostkoppeling | `dynamics` | Digitale OTT/OTB-uitwisseling en de gekoppelde toestanden. |
| Lowflowbeveiliging of vrijgave na flowherstel | `lowflow` | Beveiligingsingreep en herstel met de vastgelegde firmwaretimers. |
| Web, netwerk, buffers, taken of andere geheugenbeïnvloedende code | `pr-memory` met baseline en kandidaat | Vergelijkende heapmeting onder gelijktijdige web/SSE-belasting; volg de [handleiding](handleiding.md#geheugenproef-voor-een-pr). |
| Weer-/manifestverzoeken, TLS-worker of gelijktijdige webactiviteit | `https` | Geslaagde TLS, serialisatie, verbinding afbreken en herstel met de lokale test-CA-build. |
| Ethernetdriver of netwerkterugval | `compatibility` op Ethernet of met `--network failover` | DHCP, bereikbaarheid en herstel via Wi-Fi. |
| Dagmeting met ketelwarmte | [Ketelproef](ketelproef.md) | Doorlopen van dezelfde dagteller in CM3/CM4; aparte labbuild vereist. |
| Uitval en terugkeer van een verwarmingsbron | [`source-loss`](#uitval-van-de-verwarmingsbron) | OpenTherm-frames vallen weg, stop en expliciete bronwissel, daarna terug naar OpenTherm. |
| Eén buitenunit niet bereikbaar | [`odu-link-loss`](#tijdelijke-uitval-van-een-buitenunit) | HP2 valt uit, HP1 blijft antwoorden, watchdog en herstel via Modbus. |
| Koelbeveiliging bij een ongeldig of hoog dauwpunt | [`dew-guard`](#dauwpuntbeveiliging-bij-koelen) | Compressorstop, wachttijd en herstart met veilig dauwpunt. |
| Ketelhulp tijdens defrost | [`defrost-assist`](#ketelhulp-tijdens-defrost) | R1 of OpenTherm, inclusief de betreffende CM3-timers en beveiligingen. |
| OpenTherm-thermostaat en bronselectie | [`ott-setpoint`](#opentherm-setpointgrenzen) | Verse digitale waarden, setpointgrenzen en herstel. |
| MQTT-ingress, reconnect en webbelasting | [`mqtt-reconnect`](#mqtt-herverbinding-onder-webbelasting) | Sessie-uitval, verse vrijgave na herstel, webverkeer en heap in hetzelfde venster. |
| Uur- en daggrens van het energiearchief | [`energy-boundary`](#uur--en-daggrens-van-de-energiehistorie) | Uurtoedeling, dagafsluiting, herstelmarkering en behoud bij herstart. |
| OTA, opslag, herstart of ODU-overname | [`restart`](#herstart-en-ota) | Nieuwe boot, bewaarde instelling en ODU-continuïteit, afhankelijk van de variant. |
| Geheugen bij recorder- of Vooruitblik-export | [`pr-memory --memory-export`](#geheugen-tijdens-een-export) | Echte download tijdens webbelasting en gerichte meetdekking rond de overdracht. |

**Dekking van Power House:** de basisproef gebruikt het normale frequentiebereik en de gewone ODU-modelrespons. De aanvullende routes `odu-response` en `odu-transients` blijven gerichte regressies met geïnjecteerde afwijkingen. De basisproef stuurt externe warmtevraag via de gewone API; zij kwalificeert daarmee nog niet het interne woningmodel, leren/voorspellen of elke combinatie van niveau en temperatuur.

Een gerichte proef kan gedrag vereisen dat nog niet in de gekozen baseline zit. Vergelijk dan baseline en kandidaat en leg de ontbrekende fase vast; zo’n bestaande afkeur is nog geen door de PR veroorzaakte regressie. De proef krijgt daarmee geen PASS.

### Firmwareproeven op GitHub

OpenQuatt gebruikt de gewone CI-checks en gerichte regressietests. Aanvullende HIL-proeven volgen het risico van de wijziging. De [PR-richtlijn](https://github.com/OpenQuatt/OpenQuatt/blob/dev/CONTRIBUTING.md#pull-request-validation) behandelt aansturing en hardware apart van runtimegeheugen.

De GitHub-workflow in deze emulatorrepo draait standaard profiel **basis** op één Ubuntu x64-runner. Die run controleert boot, Wi-Fi, web-app, PT1000 en beide Modbus-buitenunits. Tegelijk meet hij de vrije interne heap, heapmin sinds boot en het grootste vrije blok in de vensters vóór, tijdens en na overlappende SSE en downloads.

Kies bij **Actions → Virtual HIL → Run workflow** de OpenQuatt-commit of PR-ref in `openquatt_ref`. Gebruik `profile` voor één profiel of vul bij `profiles` bijvoorbeeld `herstart,netwerk` in. Een ingevuld `profiles` krijgt voorrang. Kies `uitgebreid` afzonderlijk om alle Ubuntu-proeven op één runner achter elkaar uit te voeren.

| Profiel | Aanvulling op de basisrun |
| --- | --- |
| `basis` | Rooktest en geheugen onder webbelasting |
| `herstart` | Instelling opslaan, softwareherstart en opnieuw uitlezen |
| `netwerk` | W5500-Ethernet, lokale NTP en terugval naar Wi-Fi |
| `uitgebreid` | Bovenstaande proeven achter elkaar, met één basisrun |

GitHub voert elk profiel op Ubuntu uit. Kies eventueel `architecture=arm64`. Andere workflows in de emulatorrepo kunnen dezelfde profielen via `workflow_call` starten. De geselecteerde profielen delen één Q Duo-factory-bin en bewaren de commit en SHA-256 in hun rapport.

Gebruik voor Modbus-uitval onder warmtevraag, energiehistorie en regeling de lokale runner met `--backend native` op Apple Silicon. Daar blijven de CLI-profielen `modbus`, `energie`, `regeling` en `alles` beschikbaar. Die proeven controleren ook OpenTherm-ontvangst.

Vul voor een geheugenvergelijking bij `baseline_ref` bijvoorbeeld de exacte dev-commit in waar de kandidaat op gebaseerd is. Bij een andere commit bouwt GitHub één extra firmwarebin. Bij dezelfde commit meet GitHub de kandidaatbin tweemaal met een verse flashkopie. Bij meerdere profielen draait de vergelijking één keer, op een gekozen Linux-profiel als dat beschikbaar is. De baseline en kandidaat draaien achter elkaar op dezelfde runner. Het rapport toont de verschillen per fase en het bewijs voor overlap. Zolang de virtuele meetgrenzen niet zijn gekalibreerd zie je verschillen in bytes zonder automatisch regressieoordeel. Bij relevante veranderingen in buffers, taken of overlappende netwerkactiviteit blijft de hardwaregeheugenproef van OpenQuatt vereist.

## Gerichte firmwareproeven

| Scenario | Standaardduur op de host | Firmware | Controle |
| --- | ---: | --- | --- |
| Power House-basisproef (`power-house`) | 2700 s per profiel | Gewone Q Duo-bin | 0 → 2600 → 5000 → 10000 → 5000 → 2600 → 0 → 2600 W, normale Single/Duo-overgangen en blijvend bedrijf na herstel. |
| Power House: gemeten ODU-respons (`odu-response`) | 2100 s | Gewone Q Duo-bin | Eerst normale 26 Hz, daarna 2×26 gevraagd en 2×48 gemeten. Bij 5 kW volgt de Single-keuze, daarna stop en herstart. |
| Power House: tijdelijke ODU-fasen (`odu-transients`) | 2100 s | Gewone Q Duo-bin | Vers 0 Hz onder Heating-mode, 210 s opstartverhoging op 45 Hz, normaal herstel, 120 s oil return op 50 Hz en opnieuw stop/herstart. |
| Stooklijn (`heating-curve`) | 2400 s | Gewone Q Duo-bin | Single in RECOVERY, defrostblokkade, nieuwe inschakelwachttijd, Duo in MAINTAIN, afbouw en 60 s stabiel Single. |
| `https` | 420 s | Q Duo met lokale test-CA | Weer en manifest met echte TLS, onderlinge serialisatie, verbinding afbreken, nieuwe manifestcheck en herstel onder web/SSE-belasting. |

Een doorlopen run is pas geslaagd als alle scenario- en algemene controles slagen. Een onbekende entity, ontbrekende meetfase, afgekapt bewijs of mislukte cleanup levert geen PASS op. Een afwijking kan in de firmware, fixture of meetdekking zitten; lees de gefaalde controle voordat je de oorzaak toewijst.

De stooklijn- en ODU-proeven accepteren maximaal 3600 hostseconden. Gebruik `--seconds` voor extra meettijd als de gastklok onder hostbelasting achterloopt. De vereiste holds blijven op de gastklok staan. Een korte run die de eindfase mist, kwalificeert niet.

## Power House-basisproef

De basisproef kiest `Heating Control Mode = Power House`, beide ODU’s als V2 en de normale volledige frequentietabel. Er is geen frequentie-injectie of uitgesloten frequentiegebied. De nominale serieflow is 800 L/h. De kamertemperatuur en het setpoint staan via de gewone API beide op 21 °C; OTT levert de echte digitale heat-enable. Zo blijft de room-feedback nul en kan de proef de opgegeven warmtevraag volgen.

De instellingen zijn Balanced met 8 min stijgtijd en 3 min daaltijd, een opgegeven huisvermogen van 15000 W en een minimumlooptijd van 300 s. De minimum-uit-tijd van 240 s blijft actief. De firmwaretimers worden niet versneld. De runner ververst de buiten- en kamertemperatuur, het setpoint en de externe vraag elke 5 gastseconden, ook na de laatste herstelfase.

| Profiel | Buiten | ODU-retour |
| --- | ---: | ---: |
| `mild` | 8 °C | 30 °C |
| `cold` | −5 °C | 30 °C |
| `warm-water` | 8 °C | 40 °C |

Elke run test één profiel; zonder extra keuze is dat `mild`. Voor dekking van de drie temperatuurcombinaties voer je alle drie de opdrachten uit. Functionele runs mogen naast elkaar op eigen poorten. Bij zware hostbelasting kun je ze achter elkaar draaien of `--seconds` verhogen tot maximaal 3600.

```sh
python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --network wifi --scenario power-house --power-house-climate mild \
  --port-base 19700 --name power-house-mild

python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --network wifi --scenario power-house --power-house-climate cold \
  --port-base 19704 --name power-house-cold

python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --network wifi --scenario power-house --power-house-climate warm-water \
  --port-base 19708 --name power-house-warm-water
```

De vraag loopt achtereenvolgens door 2600, 5000, 10000, 5000 en 2600 W. De runner wacht steeds tot de geselecteerde API-vraag én de effectieve `Power House – P_req` overeenkomen met de stap. Daarna moet geldig bedrijf aanhouden: 90 s op de lage/middenstappen en 300 s op de hoge stap. Bij 2600 W wordt Single verwacht, bij 10000 W Duo. Bij 5000 W zijn beide topologieën toegestaan; de optimizer weegt ook elektrisch verbruik, foutband en hysterese. Er wordt geen specifieke unit, exact niveau of exacte frequentie voorgeschreven.

Bij de stap naar 0 W schakelt de virtuele thermostaat ook heat-enable uit. Dit is dus geen afzonderlijke proef of alleen een API-vraag van 0 W een stop afdwingt. De compressoren moeten aantoonbaar stilstaan. In het koude profiel mag CM98-antivriescirculatie de flow behouden; CM0 vereist nulflow. Na 30 s bevestigde stilstand vraagt OTT weer warmte bij 2600 W. De bestaande herstartbeveiliging moet worden gerespecteerd en Single moet 90 s geldig blijven draaien. De runner blijft dit bedrijf daarna bewaken tot het einde van de ingestelde hostduur; blijvende uitval of een ongeldige eindmeting geeft geen PASS.

Onder `dynamics.phase_results` staan de holds, de gebruikte units en het mediane modelvermogen per fase. Het modelvermogen moet met de vraagstappen stijgen en dalen. Ook moeten frequentiefeedback, firmwaretemperaturen en serieflow bij het model passen. De volledige Modbus-historie controleert de vaste topologie tijdens de lage/hoge holds en keurt verkeerde modi af.

`command_minimum_runtime` en `command_minimum_off` meten intervallen tussen opdrachten; zij bewijzen niet zelfstandig de marges vanaf werkelijk bevestigde start of stilstand. De hoge hold bewijst wel 300 s gemeten, samenhangend Duo-bedrijf. De P_req-sensor publiceert elke 30 s; `demand_slew_plausible` controleert daarom alleen plausibele richting en bovengrenzen met die publicatieonzekerheid. Korte afwijkingen of exacte filtertijdconstanten worden daarmee niet volledig gekwalificeerd.

De ODU-inlaat-/uitlaattemperaturen en hun performance-model variëren met het profiel. De geselecteerde lokale PT1000 blijft voor deze proef een vaste 30 °C. Dit kwalificeert de Power House-regelroute onder deze afgebakende invoer, niet een gekoppeld woningmodel, COP, fysieke capaciteit of de waterlimiter bij een meebewegende systeemaanvoer.

## Power House: ODU-respons en tijdelijke verhogingen

```sh
python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --network wifi --scenario odu-response --name response

python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --network wifi --scenario odu-transients --name transients
```

Beide proeven kiezen expliciet `Heating Control Mode = Power House` en gebruiken V2 en externe warmtevraag via de gewone API. Het beschikbare frequentiebereik is voor deze fixture beperkt tot 20 en 26 Hz. De normale warmtevraag is 9 kW. De minimumlooptijd staat op 300 s; de minimum-uit-tijd van 240 s blijft gelden. De firmwaretimers worden niet versneld.

`odu-response` wijzigt de gemeten respons van 20/26 Hz naar 48/48 Hz. Het gevraagde tabelpunt blijft zichtbaar in R2102; R2103 en het thermische model volgen de gewijzigde respons. Bij 5 kW moet de firmware daarna stabiel voldoende vermogen met één unit kiezen. Deze verwachting hoort bij deze specifieke fixture. Andere warmtevraag of responstabellen kunnen terecht Duo opleveren.

`odu-transients` onderbreekt eerst de frequentietelemetrie gedurende 65 s zonder Heating-mode te wijzigen. Daarna volgen de 45 Hz-opstartfase en oil return met R2119 bit 3. De benodigde tweede unit moet behouden blijven en na de tijdelijke fasen moet normaal bedrijf terugkeren. Vervolgens vraagt de thermostaat een echte stop en opnieuw warmte. De runner controleert de afgesloten aan- en uit-intervallen in de Modbus-opdrachten.

De minimum-uit-tijd geldt per warmtepomp. Een snelle herstart na de gezamenlijke thermostaatstop kan geldig zijn als de gekozen unit daarvoor al langer stilstond. Het rapport vermeldt daarom de unit bij elk interval. Na herstart moeten beide units minstens 60 s aantoonbaar draaien met flow; alleen nieuwe startopdrachten zijn onvoldoende.

Deze intervallen meten de opdrachten. Ze zijn een noodzakelijke controle, maar bewijzen op zichzelf niet de volledige rusttijd vanaf bevestigde stilstand; `timing_coverage` legt die grens vast.

Dit zijn geïnjecteerde tegenproeven. De volledige STM8-firmware draait niet mee; 210 s is geen bewezen opstartduur van een echte ODU. Het geleverde vermogen is modelvermogen. Gebruik de uitkomst voor de reactie van OpenQuatt op deze invoer.

Een firmware zonder de vereiste terugkoppeling kan de responsproef terecht niet halen. Verander de verwachting niet uitsluitend om een groene uitslag te krijgen; leg de waargenomen keuze en het ontbrekende gedrag vast.

## Stooklijn

```sh
python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --network wifi --scenario heating-curve --name curve
```

De fixture gebruikt Comfort, een constante PT1000-aanvoer van 30 °C en aanvoerdoelen van achtereenvolgens 45, 30 en 28,5 °C. `Dual HP Disable Hold` staat expliciet op 1 minuut en `Minimum runtime` op 300 s. De API-aanvoerdoelen en buitentemperatuur worden ververst zodat hun versheid niet tijdens een lange proef verloopt.

Na 360 s bevestigd Single-bedrijf activeert de fixture defrost. De firmware moet defrost en de vierwegklep minstens 195 s blijven bevestigen. De tweede unit mag tijdens deze fase geen startopdracht krijgen. Na vrijgave geldt opnieuw de inschakelwachttijd van 240 s. Daarna moet Duo minstens 120 s in MAINTAIN rond het aanvoerdoel blijven draaien. Een onderbreking of te groot gat tussen waarnemingen begint een nieuwe hold.

Bij het lagere doel moet Duo eerst terugmoduleren. De eindcontrole vereist minstens 60 s lagere opdrachten met beide units actief vóór Single, gevolgd door 60 s stabiel Single. Zowel het frequentiecommando als het moderegister telt mee. De volledige schrijfhistorie mag in deze verwarmingsproeven alleen Standby (0) en Heating (2) bevatten; ook een korte verkeerde modus tussen webmetingen wordt afgekeurd. Een stilstaande compressor met een oud positief frequentiecommando geldt niet als draaiend Single.

De interne afschakeltimer begint bij een vermogensgrens waarbij Single kan overnemen. Beide units hoeven daarom niet de hele minuut op niveau 1 te staan. `minimum_pair_wire_hold` registreert die laatste fase als diagnose, zonder daar een extra minimumtijd aan op te leggen. De proef bewijst het terugmoduleren en de volgorde; de precieze interne vermogensgrens en timer worden hiermee niet afzonderlijk gekwalificeerd.

Deze proef onderzoekt regeltoestanden bij gecontroleerde temperaturen. De constante PT1000 is geen gevalideerd woningmodel en bewijst geen comfort, COP of werkelijke thermische stabiliteit.

## HTTPS volledig lokaal

De productie-bin vertrouwt onze lokale CA niet. Voor geslaagde HTTPS-verzoeken maak je daarom een **afzonderlijke testbuild** van dezelfde Q Duo-bronnen. De configuratie voegt de lokale CA aan de certificaatcontrole toe. Certificaatverificatie blijft aan. Flash deze testbuild nooit op een fysieke controller en publiceer haar niet als normale firmware.

De fixture genereert eigen certificaten die 30 dagen geldig zijn. Privésleutels blijven in de lokale fixturemap. Gebruik een aparte firmwareworktree. De voorbereiding maakt daar `configs/heatpump_controller_q/.virtual-https.yaml`; bestaande bestanden worden niet overschreven. Gebruik één terminal vanuit de emulatorrepo.

```sh
oq_source="/pad/naar/openquatt-testworktree"
oq_tls="$PWD/.tmp/native-spi/local-https"

python3 -m scripts.native_spi.https_fixture \
  --firmware-root "$oq_source" --output "$oq_tls"
```

Bouw eerst de gewone Q Duo-bin en bewaar die. Bouw daarna de HTTPS-configuratie met de ESPHome-versie uit die firmwarecheckout, sequentieel en met de afgesproken compilerlimiet. De HTTPS-build schrijft naar `.esphome/build/virtual_https/build/`.

```sh
cd "$oq_source"
ESPHOME_DEFAULT_COMPILE_PROCESS_LIMIT=2 esphome compile \
  configs/heatpump_controller_q/.virtual-https.yaml
cd -

oq_https_bin="$oq_source/.esphome/build/virtual_https/build/firmware.factory.bin"
python3 -m scripts.native_spi.https_fixture \
  --firmware-root "$oq_source" --output "$oq_tls" --bind-firmware "$oq_https_bin"

python3 -m scripts.native_spi.run --firmware "$oq_https_bin" \
  --network ethernet --scenario https --https-fixture "$oq_tls" --name local-https
```

`--bind-firmware` legt de gekozen binhash naast de bronrevisie, overlay en CA vast. Controleer zelf dat dit het resultaat van de beschreven build is. De succesproef weigert een andere binhash. Genereer bij verlopen certificaten een nieuwe fixture en bouw opnieuw; omzeil de controle niet.

De Ethernet-fixture beantwoordt DNS en NTP lokaal. Verkeer voor de weer- en manifestdomeinen gaat naar de eigen TLS-server op loopback. Andere uitgaande verbindingen worden geblokkeerd. Tijdens de proef worden GitHub en Open-Meteo niet benaderd.

De runner bewaart de weerlocatie met een requesttoken en controleert het bijbehorende opslagbewijs. Vervolgens meet hij 30 s vóór de belasting. Terwijl de weerverbinding actief is, vraagt hij een manifestcheck aan. De controle op serialisatie omvat **alle verbindingen**, ook mislukte handshakes. Een extra parallelle TLS-poging mag niet buiten het oordeel vallen.

Na de eerste geslaagde manifestpublicatie verbreekt de server een volgende manifestverbinding. Een nieuwe check moet daarna een verse publicatierevisie opleveren. De runner legt elke aanvraag vóór de button-POST vast. Alleen een nieuwe manifestpoging binnen 60 gastseconden daarna telt mee; de expliciete succes- en herstelreeks moet vóór 300 s sinds boot zijn afgerond. Zo kan de bekende automatische bootcheck de ontbrekende reactie niet vervangen. Te late uitvoering geeft onvoldoende bewijs, zonder daarmee zelfstandig een firmwarefout aan te tonen. De API biedt geen correlatie-ID; deze controle toont een begrensd tijdverband.

Een oude zichtbare versietekst bewijst geen herstel. Een SSE-client en webverzoeken blijven actief. De slotfase meet minstens 30 s herstel.

## Controles van de testopstelling

Deze controles valideren het harnas en de meetketen. Voer ze uit bij wijzigingen aan de relevante testopstelling; ze horen niet standaard bij iedere OpenQuatt-PR.

### Onbekende CA afwijzen

Deze proef controleert of de gewone productie-bin de lokale test-CA blijft weigeren. Gebruik haar bij wijzigingen aan de TLS-fixture, certificaten of de manier waarop de testbuild haar CA vertrouwt. De technische CLI-id blijft `https-reject`, zodat bestaande scripts en rapporten bruikbaar blijven. De standaardduur is 240 hostseconden.

Bereid de fixture voor zoals bij [HTTPS volledig lokaal](#https-volledig-lokaal). Kies voor deze controle de gewone factory-bin.

```sh
python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --network ethernet --scenario https-reject --https-fixture "$oq_tls" --name unknown-ca
```

Beide lokale certificaten moeten expliciete TLS-afwijzingen geven; geen TLS-handshake of HTTP-request mag slagen. De firmware moet ook een mislukte weeropvraag rapporteren. Web en recorder blijven bereikbaar. Dit controleert de afwijzing van deze onbekende CA; het is geen volledige certificaatbeveiligingsaudit.

### Geheugenproef zonder overlap

Gebruik `pr-memory --without-overlap` bij wijzigingen aan de belastinggenerator of overlapmeting. De clients moeten werken, terwijl de gemeten overlap nul blijft. **Onvergelijkbaar** is dan de verwachte uitkomst. Voor een gewone PR-geheugenvergelijking gebruik je de route mét overlap uit de [handleiding](handleiding.md#geheugenproef-voor-een-pr).

De Python- en native regressietests uit de [README](../README.md#emulatorcode-testen) controleer je bij wijzigingen aan de emulatorcode. Ze valideren het harnas en vervangen de firmwareproeven niet.

## Ketelhulp tijdens defrost

```sh
python3 -m scripts.native_spi.run --firmware "$oq_firmware" --network ethernet \
  --scenario defrost-assist --assist-case r1 --name q-duo-defrost-r1
```

Gebruik een gewone Q Duo-bin met defrostketelhulp. De proef kiest lokaal 34 °C aanvoer, laat die zakken naar 28 °C en gebruikt de productietimers. De CLI stopt na een afgeronde proef plus 15 gastseconden of op de ingestelde hostlimiet. De hoofdcontroles staan onder `dynamics.checks`.

| `--assist-case` | Wat de proef controleert |
| --- | --- |
| `r1` | Opt-in, afwijzing van een korte melding, 10 s bevestiging, 60 s minimumduur, 120 s herinschakelwachttijd, begrenzing van het keteldoel en aanvoersensoruitval. |
| `ot` | Warmstartblokkade bij 42,5 °C, afkoeling naar 30 °C, CH en `TSet` op de bus, geen R1-activiteit en vrijgave naar CM2. |
| `curve` | Stooklijnbedrijf. Flowverlies laat R1 afvallen en herstel start dezelfde defrosthulp niet opnieuw. |
| `expire` | R1 valt na 900 gastseconden af bij een hangende defrostmelding en blijft daarna uit. |
| `handover` | Normale Duo-hulp in CM3 gaat via haar eigen timers over naar defrosthulp. `demand-drop` verlaagt de vraag, `held-load` gebruikt hoge vraag en een verhoogde uitschakeldrempel. Kies met `--handover-variant`. |

Voor R1 en de R1-varianten reageert de virtuele OpenTherm-ketel niet. De runner meet GPIO16 rechtstreeks en gebruikt CM3-overgangen voor de timerintervallen. De gemeten R1-onderbrekingen tijdens `handover` staan apart in het rapport. Reserveer voor de volledige 900 s- en handoverproef ruim voldoende rekentijd. Controleer of de gastklok bij parallelle runs snel genoeg vordert.

## OpenTherm-setpointgrenzen

```sh
python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --scenario ott-setpoint --name q-duo-ott-setpoint
```

De virtuele thermostaat verstuurt TrSet 21, 0, 4,5, 5, 35, 35,5 en opnieuw 21 °C via OpenTherm. De proef vergelijkt de ontvangen en geselecteerde waarden terwijl de link geldig blijft. Bij TrSet 0 biedt zij ook een meetwaarde van 0 °C voor de kamertemperatuur. Die blijft geldig. Voor setpoints is alleen 5–35 °C geldig. Het scenario controleert ook dat er geen startopdracht naar een compressor gaat.

## Herstart en OTA

```sh
python3 -m scripts.native_spi.run --firmware "$oq_firmware" --network ethernet \
  --scenario restart --restart-case hard --name q-duo-hard-reset
```

`hard` gebruikt de resetopdracht van de emulator. `software` gebruikt de knop Restart in de firmware. Beide schrijven een testinstelling, wachten op opslag en lezen de instelling na de volgende boot terug. `running` gebruikt de softwareherstart met beide compressoren actief en volgt de compressorfrequenties van vlak vóór de reset tot minstens 20 gastseconden nadat de web-app terugkomt. De ODU-modelklok loopt tijdens de controllerreset door. De beoordeling gebruikt ook de Modbus-schrijfhistorie. Voor afschakeltimers per unit is afzonderlijk bewijs van het precieze overnamemoment nodig.

Voor OTA geef je een afzonderlijk ESP32-S3-appimage en Python uit een passende ESPHome-omgeving op.

```sh
python3 -m scripts.native_spi.run --firmware "$oq_firmware" --network ethernet \
  --scenario restart --restart-case ota --ota-image "$oq_ota" \
  --ota-python "$oq_esphome_python" --name q-duo-ota
```

De runner reserveert vier lokale poorten vanaf `--port-base`, of vijf met OTA. Alleen de eigen flashkopie is schrijfbaar. Het rapport controleert de originele firmwarehash, beide boots, de nieuwe app-partitie en de exacte geschreven OTA-bytes. Het legt vast welke verzoeken naar `/energy/history` een volledig antwoord kregen terwijl de OTA-client actief was. Die sessie omvat ook de handshake en het wachten op bevestiging. Om samenloop met een specifieke firmwaremutex te bewijzen, voeg je firmwaremarkers voor lockbezit en shutdown toe aan een gerichte testbuild.

## Geheugen tijdens een export

```sh
python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --scenario pr-memory --memory-export recorder --name q-duo-recorder-export
```

De extra client downloadt de systeemrecorder binnen het bestaande belastingsvenster met SSE en GET-verzoeken. `--memory-export forecast` kiest de Vooruitblik-export. Gebruik een build die deze route aanbiedt en richt de proef in met de gewenste hoeveelheid daadwerkelijke of vooraf gevulde meetdata. `--export-min-rows` geeft de minimaal vereiste vulling aan. De meting controleert inhoud en overlap, en bewaart de payloadhash en het aantal rijen.

`export_memory.phases` toont vóór, tijdens en na de overdracht hoeveel meetpunten beschikbaar waren. De gewone recorder kan bij een korte download nul meetpunten tijdens de overdracht opleveren. Gebruik bij een gerichte labbuild `--export-probe` om seriële metingen tijdens de overdracht te vereisen. De proef leest het formaat `hil.exp … mem <gast_ms> <vrije-interne-heap-B> <grootste-interne-blok-B> <heapmin-B> <HTTPD-stackreserve-B> <vrije-PSRAM-B> [grootste-PSRAM-blok-B]`. Bij een oudere probe blijft het grootste PSRAM-blok onbekend. Gebruik dezelfde probe, vulling en belasting in baseline en kandidaat voor vergelijkende resultaten. De runner toont verschillen per exportfase met beide aantallen meetpunten. Een verschil vereist dat beide runs die waarde in de fase hebben gemeten. HA API, MQTT en andere extra clients moeten aantoonbaar actief zijn als die belasting deel uitmaakt van de vraag.

De openstaande Vooruitblik-export kun je lokaal vullen met synthetische meetdata. Kopieer de twee bestanden uit [`fixtures/export-memory`](../fixtures/export-memory/) naar de map naast `configs/heatpump_controller_q/duo.yaml` in een afzonderlijke firmwarecheckout. Compileer daar `configs/heatpump_controller_q/duo_export_probe.yaml` met de ESPHome-versie van die firmware. Gebruik de resulterende testbin in de virtuele HIL. Deze overlay voegt een proefknop en seriële metingen toe.

```sh
python3 -m scripts.native_spi.run --firmware "$oq_probe_bin" \
  --scenario pr-memory --memory-export forecast --seed-forecast-export \
  --export-min-rows 4321 --export-probe --name q-duo-forecast-full
```

De proefknop vult 4.321 synthetische meetmomenten en 49 scenario’s op firmware met Vooruitblik-opname. De runner controleert de vulling voordat hij de webbelasting start. De probe meet rond de export zo vaak als iedere 100 ms. Ontbreken meetpunten tijdens de overdracht, dan kwalificeert de specifieke piekmeting niet.

## Uitval van de verwarmingsbron

```sh
python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --scenario source-loss --name q-duo-source-loss
```

De kamerinputs komen via de API. OpenTherm levert de geselecteerde verwarmingsvrijgave. Zodra een compressor draait, stopt de fixture alle thermostaatframes terwijl de OpenTherm-ketel aangesloten blijft. Een eerder gegeven API-vrijgave blijft beschikbaar als alternatief. De proef eist dat OpenQuatt de geselecteerde bron ongeldig meldt, de compressoren stopt en de stop 25 gastseconden vasthoudt. Pas daarna selecteert de proef expliciet de API, wacht op verwarming en keert terug naar OpenTherm zodra de thermostaat weer verse frames levert. Het rapport koppelt de uitkomsten aan stop- en startopdrachten op Modbus.

Bij bronuitval kan de firmware lopende compressoren tijdens hun resterende minimumlooptijd op lage stand houden. De runner kwalificeert de stop op de gemeten frequentie en beide Modbus-stopopdrachten. Na bekend bronverlies mag alleen de minimumstand van een al draaiende unit worden aangevraagd tot de stop.

Deze proef gebruikt het contract voor een geldige geselecteerde verwarmingsbron uit `tests/hil/scenarios/input-sources.mjs` in OpenQuatt. Uitval van de OpenTherm-verbinding gebruikt de productietimers.

## Tijdelijke uitval van een buitenunit

```sh
python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --scenario odu-link-loss --name q-duo-odu-link-loss
```

De proef start beide buitenunits. Alleen HP2 wordt gedurende minstens 135 gastseconden onbereikbaar gemaakt. HP1 moet blijven antwoorden. Als HP1 blijft draaien, moet de geselecteerde flow minstens 400 L/h zijn. De runner controleert na minstens 135 gastseconden of de communicatie-watchdog van het HP2-model actief is en de compressor stilstaat. OpenQuatt moet HP2 als offline herkennen en tijdens de uitval een enkelregisterprobe op R2099 proberen. Na het verbinden kan een beantwoorde probe of een bevestigd schrijfcommando HP2 weer online brengen. De gewone HP2-telemetriepolls moeten daarna hervatten. Het rapport toont de tijdelijk onbeantwoorde frames, de offline- en onlinemeldingen binnen het injectie- en herstelvenster, de grootste pauze tussen HP1-antwoorden (grens 20 gastseconden) en de hervatte HP2-telemetriepoll op Modbus. Onverwachte time-outs, bijvoorbeeld van HP1 of buiten het injectievenster, geven afkeur.

De virtuele watchdog volgt het gedrag van het ingebouwde ODU-model. Het testcontract in OpenQuatt staat in `openquatt/oq_HP_io.yaml` en `tests/python/test_modbus_reliability_telemetry_contract.py`.

## Dauwpuntbeveiliging bij koelen

```sh
python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --scenario dew-guard --dew-case rise --name q-duo-dew-rise
python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --scenario dew-guard --dew-case loss --name q-duo-dew-loss
```

De fixture houdt de aanvoermeting op 18 °C en vraagt koeling via de API. Met dauwpunt 10 °C moet één compressor aantoonbaar koelen. `rise` geeft 23 °C door en wacht tot de gefilterde veilige aanvoergrens boven de gemeten aanvoer komt. `loss` kiest tijdens bedrijf een Home Assistant-bron zonder geldig dauwpunt. De runner meet de onveilige toestand terwijl de compressor nog draait, controleert de Modbus-stop en bewaakt beide units op startopdrachten tot zowel het dauwpunt als de veilige aanvoergrens herstel bevestigen. De runner controleert beide waarden opnieuw bij de herstart. In beide varianten geldt 240 gastseconden minimum-uit-tijd vanaf de stopmelding. De controle volgt beide units zodat een vroege start via de andere unit onder dezelfde uitgangsvoorwaarden ook afkeurt.

De twee dauwpuntvarianten meten een verschillend firmwarepad. Een bevestigde overschrijding van de dauwpuntgrens moet de compressor vóór afloop van de minimumlooptijd stoppen. Bij een ontbrekend dauwpunt vervalt de koelvrijgave meteen zodra de firmware het verlies bevestigt. De huidige firmware kan een lopende compressor daarna nog op minimumstand houden tot de minimumlooptijd voorbij is. De proef bewaakt welke opdrachten tijdens die resterende minimumlooptijd over Modbus gaan. Het rapport toont de tijd tussen de onveilige meting en de bevestigde stop. Een run zonder meetpunt waarop de unit nog draait bij de onveilige conditie levert onvoldoende bewijs voor deze volgorde. Houd bij `loss` rekening met beide productietimers.

De stopmelding heeft een resolutie van één seconde. Voor de minimum-uit-tijd gebruikt de toets het vroegste mogelijke stoptijdstip. Voor de bovengrens op de looptijd bij hoog dauwpunt gebruikt hij het laatste mogelijke stoptijdstip en de eerste bevestigde Modbus-opdracht voor koelen. De proef gebruikt een vaste aanvoermeting en het contract uit `tests/python/test_cooling_request_dew_stop_runtime.py` in OpenQuatt.

## MQTT-herverbinding onder webbelasting

```sh
python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --network ethernet --scenario mqtt-reconnect --port-base 19600 --name q-duo-mqtt
```

De runner maakt per run een tijdelijke MQTT 3.1.1-broker op host-loopback. De firmware bereikt die via `10.42.0.2:1883`. De broker accepteert de ingebouwde MQTT-ingressclient en levert verwarmingsvrijgave op het topic uit `/mqtt/status`. Je hebt geen externe broker, wachtwoord of aangepaste firmware nodig.

De proef schakelt voor deze ingang de optie voor retained berichten uit. Daardoor hoort een geldige vrijgave bij de actieve sessie. De proef bevestigt eerst zo'n vrijgave. Met een werkende SSE-stream en geslaagde aanvragen van `/0.js` verbreekt de broker de verbinding minstens 26 gastseconden. Tijdens uitval moet de vrijgave ongeldig worden en moet een onafhankelijke kamerinput via de API doorwerken. Na de nieuwe sessie en het nieuwe abonnement blijft de broker minstens zes gastseconden stil. De proef controleert in die periode dat de oude vrijgave ongeldig blijft. Daarna stuurt de broker een vers bericht met de waarde `false`. OpenQuatt moet dit als geldige MQTT-invoer toepassen en de sessie tot aan afsluiting behouden.

Het rapport toont de sessies, beide ontvangen waarden en bewezen overlap van SSE met downloads vóór, tijdens en na de storing. In `memory.phases` staan vrije interne heap, heapmin sinds boot en het grootste vrije blok in de rust-, uitval- en herstelvensters. De recorder meet elke tien gastseconden. De fixture test MQTT-ingress zonder TLS en gebruikt de productietimers.

## Uur- en daggrens van de energiehistorie

```sh
python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --network ethernet --scenario energy-boundary --energy-case hour \
  --port-base 19610 --name q-duo-energy-hour
python3 -m scripts.native_spi.run --firmware "$oq_firmware" \
  --network ethernet --scenario energy-boundary --energy-case day \
  --port-base 19614 --name q-duo-energy-day
```

De lokale SNTP-fixture zet de klok op 15 januari 2026, acht minuten voor de grens in `Europe/Amsterdam`. De virtuele buitenunits leveren verbruik en warmte via de gewone firmwarepaden. De runner leest de werkelijke uur- en dagrecords uit `/energy/history`. Elke variant start op een eigen flashkopie van dezelfde factory-bin.

- `hour` loopt van 11:52 over 12:00. Het afgesloten uur moet behouden blijven. De runner slaat een tussenstand op, laat de teller verder oplopen en voert een harde herstart uit. Energie die nog ontbrak in het uurarchief moet in het herstartuur terechtkomen met een herstelmarkering. De uursom moet na herstel aansluiten op de herstelde dagteller. De herstelde waarde moet tussen het opgeslagen energiecheckpoint en de laatste meting vóór de reset liggen, met een afrondingsmarge op die bovengrens. Een harde reset kan het laatste nog niet opgeslagen deel van de live teller verliezen. Na opslaan controleert de runner de herstelmarkering ook in het flasharchief.
- `day` loopt van 23:52 over middernacht. De vorige dag moet één definitief dagrecord krijgen met de bijbehorende uurrecords. De runner volgt de nieuwe dag minstens 75 gastseconden. Hij controleert of de nieuwe teller vanaf nul oploopt en of de uursom aansluit op de nieuwe dagteller. Zo kan een meegekomen dagtotaal ook opvallen als de uursom vlak na middernacht toevallig nog aansluit. De vorige dag en zijn uurarchief moeten na een harde herstart gelijk blijven.

De proef gebruikt normale tellerupdates en registreert één middernacht buiten de zomertijdwissel. De invoerbin blijft byte-identiek. De harde herstart en archiefschrijfacties gebruiken alleen de private virtuele flashkopie.

## Rapport en grenzen

Open `report.json` in de map die de eigen CLI als `run` meldt. Onder `dynamics` of `https` staan fase, meetpunten en `checks`. De algemene `qualification` bevat ook deze controles. `peer.write_history_complete` laat zien of alle Modbus-opdrachten beschikbaar zijn. Bij truncatie krijgen de nieuwe dynamische scenario’s geen PASS.

`scenario-source/` bewaart de Python-bronnen en de gecontroleerde native buildbronnen bij deze run. Bewaar deze map samen met het oorspronkelijke rapport en de firmwarehash. De restagecontrole toont een interval tussen fixturevrijgave en de eerste HTTP-bevestiging. Als de vereiste wachttijd binnen die onzekerheid valt, kwalificeert het bewijs niet; dat is op zichzelf geen aangetoonde firmwarefout.

Bij HTTPS staan de TLS-pogingen met host- en gasttijden onder `https.tls_events`. De firmware moet de weerdata accepteren en de manifestpublicatie bevestigen. `memory.phases` bevat de recorderwaarden vóór, tijdens en na de activiteit. De 10 s-bemonstering kan korte dips missen; heapmin blijft het minimum sinds boot. Dit zijn diagnostische virtuele waarden, zonder gekalibreerd geheugenbudget of bewijs voor fysieke stack-/DMA-marges.

De heapvensters horen bij de aangevraagde weer- en manifestproef en haar herstel. Ze bewijzen geen dekking van alle latere automatische opstarttaken, zoals de manifestcheck na 300 s stabiel netwerk.

Houd baseline en kandidaat bij geheugenvergelijkingen achter elkaar op dezelfde host, met dezelfde firmwareconfiguratie, fixture en belasting. Vergelijk de gewone productie-bin en de test-CA-build niet alsof alleen hun functionele wijziging verschilt.

De desktopwerkbank blijft voor de bestaande compatibiliteits- en netwerkproeven. Deze uitgebreidere scenario’s zijn via de CLI beschikbaar. Voor elektrische signalen, fysieke power-on en werkelijke geheugenmarges blijft [fysieke HIL](handleiding.md#7-wanneer-fysieke-hil-nodig-blijft) nodig.
