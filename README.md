<div align="center">

<img height="84" alt="Logo ČEZ" src="https://github.com/user-attachments/assets/cf5f5141-946a-4303-a515-2a65bd3a5efe" />

# ČEZ Distribuce pro Home Assistant

Integrace pro **Home Assistant** napojená na portál [ČEZ Distribuce](https://dip.cezdistribuce.cz) a API aplikace [Proud](https://www.cezdistribuce.cz/proud).
Přináší sledování **HDO**, **odečtů**, **odstávek** a u chytrých elektroměrů **15minutový rozpad spotřeby na VT/NT**, náklady a dlouhodobé statistiky připravené přímo pro **Energy dashboard**.

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-F05A22?style=for-the-badge&labelColor=1F1F1F&logoColor=F05A22)](https://github.com/hacs/integration)
[![HACS Validation](https://img.shields.io/github/actions/workflow/status/FejbyK/CEZ/hacs.yaml?branch=main&style=for-the-badge&label=HACS%20Check&labelColor=1F1F1F&color=F05A22)](https://github.com/FejbyK/CEZ/actions/workflows/hacs.yaml)
[![Code Quality](https://img.shields.io/github/actions/workflow/status/FejbyK/CEZ/quality.yaml?branch=main&style=for-the-badge&label=Tests%20%26%20Quality&labelColor=1F1F1F&color=F05A22)](https://github.com/FejbyK/CEZ/actions/workflows/quality.yaml)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/FejbyK/CEZ/hassfest.yaml?branch=main&style=for-the-badge&label=Hassfest&labelColor=1F1F1F&color=F05A22)](https://github.com/FejbyK/CEZ/actions/workflows/hassfest.yaml)
[![Licence MIT](https://img.shields.io/badge/License-MIT-F05A22?style=for-the-badge&labelColor=1F1F1F)](LICENSE)

[![Otevřít repozitář v HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?category=Integration&owner=FejbyK&repository=CEZ)

</div>

---

## Hlavní funkce

- **Rozdělená spotřeba VT / NT pro Energy dashboard** – stahuje 15minutový profil (`assemblyCode=03`, záložně hodinový `05`), rozděluje ho podle skutečného plánu HDO a zapisuje statistiky spotřeby i nákladů.
- **Měsíční a denní přehledy** – entity pro aktuální měsíc (spotřeba i náklady celkem / VT / NT, v atributech i minulý měsíc) a za poslední uzavřený kalendářní den.
- **Stav HDO, ceny a odhady** – aktuální tarif, časy spínání (včetně intervalů přes půlnoc), aktuální cena za kWh a odhad stavu elektroměru (celkem, VT, NT).
- **Podpora FVE / mikrozdrojů** – samostatná sada entit a statistika dodávky do sítě (`assemblyCode=06`) bez zbytečných HDO senzorů.
- **Více odběrných míst a prostředí** – podpora více EAN i účtů kombinujících prostředí **Domácnost** a **Podnikatel**.
- **Šetrný provoz a samostatná doména** – doména `cez_distribuce` (lze provozovat i souběžně s jinými ČEZ integracemi), sdílená HTTP spojení, 4h interval dotazů po doručení včerejška a přeskakování neměnných zápisů do Recorderu.

---

## Instalace

### Přes HACS (doporučeno)

1. Klikněte na tlačítko **Otevřít repozitář v HACS** výše (nebo **HACS → ⋮ → Vlastní repozitáře** → `https://github.com/FejbyK/CEZ`, kategorie **Integrace**).
2. Nainstalujte **ČEZ Distribuce** a restartujte Home Assistant.
3. Přidejte integraci v **Nastavení → Zařízení a služby → Přidat integraci → ČEZ Distribuce**.

### Ručně

1. Zkopírujte složku `custom_components/cez_distribuce` do `config/custom_components/`.
2. Restartujte Home Assistant a přidejte integraci **ČEZ Distribuce**.

---

## Nastavení

Integrace používá přihlašovací údaje k portálu [dip.cezdistribuce.cz](https://dip.cezdistribuce.cz/) (přihlášení přes Google/Apple není podporováno).

- **Více odběrných míst**: každé OM (EAN) je samostatná položka integrace – přidejte ji opakovaně přes **Přidat položku**.
- **Domácnost + Podnikatel**: průvodce automaticky projde všechna prostředí portálu a uloží správné prostředí k vybranému EAN.
- **Změna cen VT/NT nebo HDO signálu**: kdykoliv přes **Nastavení → Zařízení a služby → ČEZ Distribuce → Konfigurovat** (po uložení se integrace sama načte znovu).
- **Změna přihlašovacích údajů**: přes **⋮ → Překonfigurovat** (při změně hesla vyzve HA k re-autorizaci automaticky).

---

## Dostupné entity

### Spotřeba a náklady (chytrý elektroměr)

| Entita | Jednotka | Popis |
| --- | --- | --- |
| `sensor.celkova_mesicni_spotreba` | kWh | Spotřeba za aktuální měsíc (v atributech VT/NT, náklady a minulý měsíc) |
| `sensor.mesicni_vt_spotreba` / `sensor.mesicni_nt_spotreba` | kWh | Měsíční spotřeba VT / NT |
| `sensor.celkove_mesicni_naklady` | CZK* | Náklady za aktuální měsíc (součet VT + NT) |
| `sensor.mesicni_naklady_vt` / `sensor.mesicni_naklady_nt` | CZK* | Měsíční náklady VT / NT |
| `sensor.posledni_den_spotreba` | kWh | Spotřeba za poslední uzavřený kalendářní den (typicky včerejšek) |
| `sensor.posledni_den_vt_spotreba` / `sensor.posledni_den_nt_spotreba` | kWh | Spotřeba VT / NT za poslední uzavřený den |

*\* Používá měnu nastavenou v Home Assistantu.*

### Odečty, odhady, HDO a diagnostika

| Entita | Jednotka / typ | Popis |
| --- | --- | --- |
| `sensor.spotreba_vt` / `sensor.spotreba_nt` / `sensor.celkova_spotreba` | kWh | Poslední odečet z portálu (VT, NT, součet) |
| `sensor.odhadovany_stav_elektromeru` (`_vt`, `_nt`) | kWh | Poslední odečet + hodinová spotřeba od data odečtu |
| `sensor.aktualni_cena` | Kč/kWh | Aktuální cena podle stavu HDO (u jednotarifu přímo cena VT) |
| `sensor.stav_hdo` / `sensor.spinani_hdo_dnes` | Stav / počet | Aktuální tarif (**VT** / **NT**) a dnešní NT intervaly v atributech |
| `sensor.vysoky_tarif_start` / `_konec`, `sensor.nizky_tarif_start` / `_konec` | Čas | Začátek a konec aktuálního / nejbližšího VT a NT bloku |
| `sensor.odpocet_do_konce_vysokeho_tarifu` / `_nizkeho_tarifu` | min | Odpočet do konce tarifního bloku (ve výchozím stavu skryté) |
| `binary_sensor.porucha_odstavka` | Binární | Hlášená porucha nebo plánovaná odstávka |
| `sensor.posledni_uspesna_aktualizace` / `sensor.hodinova_data_spotreby_k` | Diagnostika | Čas posledního stažení a konec poslední importované hodiny |

> [!NOTE]
> U potvrzeně **jednotarifních odběrných míst** (portál nevrací žádné HDO signály) se zbytečné HDO entity nevytvářejí. U **výroby / FVE** (`typ` `V`, `M`, `B`) se vytváří pouze `sensor.celkova_dodavka_do_site`, `binary_sensor.porucha_odstavka` a diagnostické senzory.

---

## Energy dashboard a dlouhodobé statistiky

> [!IMPORTANT]
> Hodinová/15min data vyžadují chytrý elektroměr a využívají neoficiální API aplikace **Proud** (MEPAS). U elektroměrů bez dálkového odečtu se tato část tiše přeskočí a ostatní entity fungují beze změny.

Integrace vytváří externí dlouhodobé statistiky (přesné ID najdete v atributu `statistiky` entity `sensor.hodinova_data_spotreby_k`):

| Statistika | Jednotka | Použití v Energy dashboardu |
| --- | --- | --- |
| `cez_distribuce:<ean>_consumption_vt` | kWh | **Spotřeba ze sítě** – vysoký tarif (+ náklady `…_cost_vt`) |
| `cez_distribuce:<ean>_consumption_nt` | kWh | **Spotřeba ze sítě** – nízký tarif (+ náklady `…_cost_nt`) |
| `cez_distribuce:<ean>_consumption` | kWh | **Spotřeba ze sítě** – alternativa bez dělení tarifů (+ `…_cost_total`) |
| `cez_distribuce:<ean>_cost_vt` / `_nt` / `_total` | měna HA | Volitelné statistiky nákladů podle cen z konfigurace |
| `cez_distribuce:<ean>_production` | kWh | **Vrácení do sítě** (jen u výrobních / FVE EAN) |

> [!TIP]
> Do Energy dashboardu přidejte buď dvojici `consumption_vt` + `consumption_nt`, **nebo** pouze `consumption` – nikdy obojí najednou, jinak se spotřeba započítá dvakrát. ČEZ data publikuje zpětně kolem 1:00 v noci za celý předchozí den.

### Přepočet historických statistik

Po změně cen elektřiny nebo HDO signálu můžete zpětně přepočítat statistiky v **Nástroje pro vývojáře → Akce**:

```yaml
action: cez_distribuce.recalculate_statistics
data:
  days: 30
  recalculate_costs_only: true
  # ean: "859182400100000000"  # volitelné; prázdné = všechna spotřební OM
```

- `days` (`1`–`1095`, výchozí `30`) – počet dní zpět.
- `recalculate_costs_only: true` – rychle přepočítá jen náklady z uložených statistik VT/NT bez volání ČEZ API.
- `recalculate_costs_only: false` – znovu stáhne 15min profil z MEPAS a přepočítá VT/NT i náklady.

---

## Ukázky

<details>
<summary>Zobrazit náhledy grafů v Home Assistantu</summary>

### Spotřeba
<img width="1615" height="485" alt="Graf spotřeby v Home Assistantu" src="https://github.com/user-attachments/assets/3642cec5-063c-4aa2-a0e4-c6796b6320fa" />

### Přetoky a výroba (FVE)
<img width="1455" height="914" alt="Graf přetoků a výroby v Home Assistantu" src="https://github.com/user-attachments/assets/8a5e63be-1f50-4ab0-98f9-30f6acd0f104" />

</details>

---

## Aktualizace a řešení problémů

- **Portálová data** (HDO, odečty, odstávky) se obnovují každou hodinu.
- **MEPAS / hodinová data** se po doručení včerejška (obvykle mezi 1:00–2:00) dotazují jen každé 4 hodiny a neměnná data se do Recorderu nezapisují. Jednou denně probíhá kontrola a doplnění případných děr za posledních 62 dní.
- **Přesnost rozpadu VT/NT**: integrace si ukládá denní plány HDO až 400 dní zpět a u 15min profilu počítá přesný poměr minut NT/VT v každé čtvrthodině (včetně přechodů přes půlnoc i změny letního/zimního času).

<details>
<summary>Nejčastější chyby a diagnostika</summary>

| Problém | Řešení |
| --- | --- |
| **Nesprávné přihlašovací údaje** | Ověřte e-mail a heslo na [dip.cezdistribuce.cz](https://dip.cezdistribuce.cz/). |
| **Portál vyžaduje akci** | Přihlaste se jednou v prohlížeči na [dip.cezdistribuce.cz](https://dip.cezdistribuce.cz/) a potvrďte podmínky/smlouvu. |
| **Žádná odběrná místa** | Účet Můj ČEZ nestačí – OM musí být přiřazené v portálu ČEZ Distribuce (ne PRE ani EG.D). |
| **Portál dočasně nedostupný** | Běžné při noční údržbě – integrace podrží poslední data a zkusí to za hodinu znovu. |
| **Statistika se neposouvá** | Zkontrolujte `sensor.hodinova_data_spotreby_k` a zapněte debug log níže. |

**Debug logování** (`configuration.yaml`):

```yaml
logger:
  logs:
    custom_components.cez_distribuce: debug
```

**Ruční ověření bez Home Assistantu** (vyžaduje `pip install aiohttp beautifulsoup4`):

```bash
CEZ_USER='email' CEZ_PASS='heslo' python3 scripts/test_pnd_consumption.py
```

**Spuštění testů**:

```bash
python3 -m unittest discover -s tests -v
```

</details>

---

## Licence a poděkování

- Integrace využívá neoficiální API portálu ČEZ Distribuce a aplikace Proud – struktura odpovědí se může bez upozornění změnit.
- Vychází z projektu [MattXcz/CEZ](https://github.com/MattXcz/CEZ), rozpad spotřeby na VT/NT a nákladové statistiky jsou inspirovány [igracek/HACS_CEZD_PND](https://github.com/igracek/HACS_CEZD_PND), za původní reverzní analýzu aplikace Proud patří poděkování Romanu Vohradníkovi.
- Šířeno pod licencí [MIT](LICENSE).
