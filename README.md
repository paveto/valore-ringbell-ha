<img src="custom_components/valore_ringbell/brand/icon.png" width="72" height="72" alt="">

# Valore Ringbell

Integrace zvonku **Valore IP07 / X6** pro Home Assistant. Běží v Home Assistantu
a vytvoří jedno zařízení s kamerou a samostatnou událostí **Zazvonění**.
Obraz se spouští při otevření kamery. Fyzický stisk zvonku lze použít
pro automatizace i pro upozornění v Apple Domácnosti.

## Instalace

[![Otevřít Valore Ringbell v Tržišti](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=paveto&repository=valore-ringbell-ha&category=integration)

1. V **Nastavení → Tržiště → ⋮ → Vlastní repozitáře** přidej
   `https://github.com/paveto/valore-ringbell-ha`.
   Pokud používáš HACS, otevři **HACS → ⋮ → Vlastní repozitáře** a vyber typ **Integrace**.
2. Nainstaluj **Valore Ringbell**. Pokud to Home Assistant vyžádá, restartuj ho.
3. V **Nastavení → Zařízení a služby → Přidat integraci** vyhledej **Valore Ringbell**.
4. Zadej údaje účtu Tuya Smart a předvolbu země (pro český účet `420`).
   Vyber zvonek podle jména a potvrď. Při shodě názvů pomáhá koncovka ID.

Home Assistant ukládá údaje do vlastní konfigurace. Přístup k ní má mít
jen správce. Tuya Smart je potřeba k prvotnímu spárování zařízení a ke správě
účtu. Tato verze byla ověřena na Home Assistantu 2026.10.4 a 2026.11.3 se simulovaným zvonkem.

## Karta na nástěnce

V seznamu entit má kamera stejný název jako zařízení a ikonu videokamery.
Otevřením této entity můžeš ověřit obraz přímo v Home Assistantu.

Přidej kartu **Obrázek entity** a vyber kameru Valore. S tímto nastavením
spustíš živý obraz kliknutím na kartu.

```yaml
type: picture-entity
entity: camera.valore
name: U dveří
show_state: false
camera_view: auto
tap_action:
  action: more-info
```

## Oznámení v Home Assistantu

Vytvoř automatizaci s triggerem **Stav** na entitě `event.…` z Valore a akcí
`notify.mobile_app_…` svého iPhonu. Při volbě triggeru ponech pole **Z** a **Na**
prázdná. V nastavení automatizace lze omezit oznámení například na konkrétní čas.
Integrace kontroluje události Tuya každých 20 sekund při běžném provozu.

## Apple Domácnost

Přidej vlastní instanci [HomeKit Bridge](https://www.home-assistant.io/integrations/homekit/)
pro kameru v režimu `accessory`. Pro propojení zazvonění s kamerou použij
v `configuration.yaml` nastavení `linked_doorbell_sensor`. Názvy entit nahraď
skutečnými z detailu zařízení v Home Assistantu.

```yaml
homekit:
  - name: Valore u dveří
    mode: accessory
    port: 21065
    filter:
      include_entities:
        - camera.valore
    entity_config:
      camera.valore:
        linked_doorbell_sensor: event.valore_zazvoneni
        max_width: 640
        max_height: 480
        max_fps: 15
```

Pokud už v souboru máš položku `homekit:`, přidej příklad jako další položku
pod ni. Po restartu spáruj nové příslušenství pomocí kódu HomeKit Bridge
a vyzkoušej živý náhled i fyzické zazvonění. FFmpeg dostává obraz ze smyčky
`127.0.0.1`, takže stačí síťové připojení potřebné k párování samotného HomeKitu.

Verze 0.1 nabízí živý obraz a událost tlačítka. U spícího bateriového zvonku
může náběh obrazu trvat déle než čeká Apple Domácnost. Při otevření náhledu
integrace požádá o probuzení, pokud zařízení přes Tuya hlásí spánek
prostřednictvím standardního údaje `wireless_awake`. Při potížích porovnej
chování po fyzickém zazvonění a po delší době nečinnosti. Obousměrný zvuk
z původního panelu do této verze ještě přenesený není.
