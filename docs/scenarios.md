# Vaste regressiescenario’s

Gebruik deze CLI-proeven voor wijzigingen aan Power House, stooklijnbedrijf en HTTPS. De firmwareproeven laden de gekozen Q Duo-factory-bin rechtstreeks, behalve de lokale HTTPS-succesproef: die gebruikt een afzonderlijke Q Duo-testbuild met een eigen test-CA. Een ELF of debugger is voor geen van deze runs nodig.

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

**Dekking van Power House:** de basisproef gebruikt het normale frequentiebereik en de gewone ODU-modelrespons. De aanvullende routes `odu-response` en `odu-transients` blijven gerichte regressies met geïnjecteerde afwijkingen. De basisproef stuurt externe warmtevraag via de gewone API; zij kwalificeert daarmee nog niet het interne woningmodel, leren/voorspellen of elke combinatie van niveau en temperatuur.

Een gerichte proef kan gedrag vereisen dat nog niet in de gekozen baseline zit. Vergelijk dan baseline en kandidaat en leg de ontbrekende fase vast; zo’n bestaande afkeur is nog geen door de PR veroorzaakte regressie. De proef krijgt daarmee geen PASS.

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

## Rapport en grenzen

Open `report.json` in de map die de eigen CLI als `run` meldt. Onder `dynamics` of `https` staan fase, meetpunten en `checks`. De algemene `qualification` bevat ook deze controles. `peer.write_history_complete` laat zien of alle Modbus-opdrachten beschikbaar zijn. Bij truncatie krijgen de nieuwe dynamische scenario’s geen PASS.

`scenario-source/` bewaart de Python-bronnen en de gecontroleerde native buildbronnen bij deze run. Bewaar deze map samen met het oorspronkelijke rapport en de firmwarehash. De restagecontrole toont een interval tussen fixturevrijgave en de eerste HTTP-bevestiging. Als de vereiste wachttijd binnen die onzekerheid valt, kwalificeert het bewijs niet; dat is op zichzelf geen aangetoonde firmwarefout.

Bij HTTPS staan de TLS-pogingen met host- en gasttijden onder `https.tls_events`. De firmware moet de weerdata accepteren en de manifestpublicatie bevestigen. `memory.phases` bevat de recorderwaarden vóór, tijdens en na de activiteit. De 10 s-bemonstering kan korte dips missen; heapmin blijft het minimum sinds boot. Dit zijn diagnostische virtuele waarden, zonder gekalibreerd geheugenbudget of bewijs voor fysieke stack-/DMA-marges.

De heapvensters horen bij de aangevraagde weer- en manifestproef en haar herstel. Ze bewijzen geen dekking van alle latere automatische opstarttaken, zoals de manifestcheck na 300 s stabiel netwerk.

Houd baseline en kandidaat bij geheugenvergelijkingen achter elkaar op dezelfde host, met dezelfde firmwareconfiguratie, fixture en belasting. Vergelijk de gewone productie-bin en de test-CA-build niet alsof alleen hun functionele wijziging verschilt.

De desktopwerkbank blijft voor de bestaande compatibiliteits- en netwerkproeven. Deze uitgebreidere scenario’s zijn via de CLI beschikbaar. Voor elektrische signalen, fysieke power-on en werkelijke geheugenmarges blijft [fysieke HIL](handleiding.md#7-wanneer-fysieke-hil-nodig-blijft) nodig.
