<div align="center">

<img height="84" alt="Logo ČEZ" src="https://github.com/user-attachments/assets/cf5f5141-946a-4303-a515-2a65bd3a5efe" />

# ČEZ Distribuce pro Home Assistant

**HDO, odečty, odstávky a 15minutový rozpad spotřeby na VT/NT s náklady přímo pro Energy dashboard.**

[![HA Marketplace](https://img.shields.io/badge/HA%20Marketplace-Custom-F05A22?style=for-the-badge&labelColor=1F1F1F)](https://www.home-assistant.io/integrations/marketplace/)
[![Marketplace Check](https://img.shields.io/github/actions/workflow/status/FejbyK/ha-cez-distribuce/hacs.yaml?branch=main&style=for-the-badge&label=Marketplace%20Check&labelColor=1F1F1F&color=F05A22)](https://github.com/FejbyK/ha-cez-distribuce/actions/workflows/hacs.yaml)
[![Code Quality](https://img.shields.io/github/actions/workflow/status/FejbyK/ha-cez-distribuce/quality.yaml?branch=main&style=for-the-badge&label=Tests%20%26%20Quality&labelColor=1F1F1F&color=F05A22)](https://github.com/FejbyK/ha-cez-distribuce/actions/workflows/quality.yaml)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/FejbyK/ha-cez-distribuce/hassfest.yaml?branch=main&style=for-the-badge&label=Hassfest&labelColor=1F1F1F&color=F05A22)](https://github.com/FejbyK/ha-cez-distribuce/actions/workflows/hassfest.yaml)
[![Licence GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-F05A22?style=for-the-badge&labelColor=1F1F1F)](LICENSE)

[![Otevřít repozitář v Marketplace](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=FejbyK&repository=ha-cez-distribuce&category=integration)

</div>

---

## Co integrace umí

<table>
  <tr>
    <td width="50%" valign="top">
      <strong>⚡ 15min rozpad VT / NT</strong><br />
      Stahuje čtvrthodinový profil z chytrého elektroměru, dělí ho podle skutečného HDO a zapisuje statistiky do <strong>Energy dashboardu</strong>.
    </td>
    <td width="50%" valign="top">
      <strong>💰 Měsíční a denní náklady</strong><br />
      Přehled spotřeby i ceny za aktuální a minulý měsíc i poslední uzavřený den (celkem, VT, NT).
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>🕒 HDO, tarify a odhady</strong><br />
      Aktuální tarif, časy spínání (i přes půlnoc), aktuální cena za kWh a odhad stavu elektroměru.
    </td>
    <td width="50%" valign="top">
      <strong>☀️ FVE a mikrozdroje</strong><br />
      Samostatná statistika dodávky do sítě bez zbytečných HDO senzorů. U jednotarifu se HDO entity skryjí automaticky.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>🏠 Více EAN i prostředí</strong><br />
      Podpora více odběrných míst i účtů kombinujících <strong>Domácnost</strong> a <strong>Podnikatel</strong>.
    </td>
    <td width="50%" valign="top">
      <strong>🚀 Šetrný provoz</strong><br />
      Samostatná doména <code>cez_distribuce</code>, sdílená spojení, 4h dotazy po doručení včerejška a žádné zbytečné zápisy do Recorderu.
    </td>
  </tr>
</table>

### Jak to vypadá v Home Assistantu


<details>
<img width="1817" height="733" alt="Graf spotřeby v Home Assistantu" src="https://github.com/user-attachments/assets/6277ae12-1542-4322-aa2e-8b8e3f60e37f" />

</details>

---

## Rychlá instalace

1. Klikněte na tlačítko **Otevřít repozitář v Marketplace** nahoře *(nebo v HA: **Nastavení → Marketplace → Přidat z odkazu** → `https://github.com/FejbyK/ha-cez-distribuce`; vlastní repozitář vyžaduje propojený GitHub účet)*.
2. Nainstalujte **ČEZ Distribuce** *(u starších verzí HA restartujte Home Assistant)*.
3. Přidejte integraci v **Nastavení → Zařízení a služby → Přidat integraci → ČEZ Distribuce** a přihlaste se účtem z [dip.cezdistribuce.cz](https://dip.cezdistribuce.cz/).

<details>
<summary>Ruční instalace a tipy k nastavení</summary>

- **Ruční instalace**: zkopírujte složku `custom_components/cez_distribuce` do `config/custom_components/` a restartujte Home Assistant.
- **Přihlášení**: funguje e-mail a heslo portálu [dip.cezdistribuce.cz](https://dip.cezdistribuce.cz/) (přihlášení přes Google/Apple není podporováno).
- **Více odběrných míst**: každé OM (EAN) přidejte jako další položku přes **Přidat položku**.
- **Změna cen VT/NT nebo HDO signálu**: kdykoliv přes **Konfigurovat** u dané položky (integrace se sama znovu načte).
- **Změna hesla**: přes **⋮ → Překonfigurovat** (nebo automaticky při výzvě k re-autorizaci).

</details>

---

## Napojení na Energy dashboard

> [!TIP]
> Přidejte do Energy dashboardu buď dvojici `consumption_vt` + `consumption_nt`, **nebo** jen `consumption` – nikdy obojí najednou. ČEZ data publikuje zpětně kolem 1:00 v noci za předchozí den.

| Statistika | Jednotka | Kam přidat |
| --- | --- | --- |
| `cez_distribuce:<ean>_consumption_vt` | kWh | **Spotřeba ze sítě** – VT *(+ náklady `…_cost_vt`)* |
| `cez_distribuce:<ean>_consumption_nt` | kWh | **Spotřeba ze sítě** – NT *(+ náklady `…_cost_nt`)* |
| `cez_distribuce:<ean>_consumption` | kWh | **Spotřeba ze sítě** – bez dělení tarifů *(+ `…_cost_total`)* |
| `cez_distribuce:<ean>_production` | kWh | **Vrácení do sítě** *(jen u FVE / výroby)* |

*(Přesná ID najdete v atributu `statistiky` entity `sensor.hodinova_data_spotreby_k`. Hodinová/15min data vyžadují chytrý elektroměr s dálkovým odečtem.)*

<details>
<summary>🔄 Zpětný přepočet statistik po změně cen nebo HDO signálu</summary>

V **Nástroje pro vývojáře → Akce** spusťte:

```yaml
action: cez_distribuce.recalculate_statistics
data:
  days: 30
  recalculate_costs_only: true
  # ean: "859182400100000000"  # volitelné; prázdné = všechna spotřební OM
```

- `days` (`1`–`1095`, výchozí `30`) – počet dní zpět.
- `recalculate_costs_only: true` – rychlý přepočet jen nákladů bez volání ČEZ API.
- `recalculate_costs_only: false` – znovu stáhne 15min profil a přepočítá VT/NT i náklady.

</details>

---

## Přehled entit

<details>
<summary>📊 Zobrazit kompletní seznam vytvářených senzorů</summary>

### Spotřeba a náklady (chytrý elektroměr)

| Entita | Jednotka | Popis |
| --- | --- | --- |
| `sensor.celkova_mesicni_spotreba` | kWh | Spotřeba za aktuální měsíc *(v atributech VT/NT, náklady a minulý měsíc)* |
| `sensor.mesicni_vt_spotreba` / `sensor.mesicni_nt_spotreba` | kWh | Měsíční spotřeba VT / NT |
| `sensor.celkove_mesicni_naklady` | měna HA | Náklady za aktuální měsíc (VT + NT) |
| `sensor.mesicni_naklady_vt` / `sensor.mesicni_naklady_nt` | měna HA | Měsíční náklady VT / NT |
| `sensor.posledni_den_spotreba` | kWh | Spotřeba za poslední uzavřený kalendářní den |
| `sensor.posledni_den_vt_spotreba` / `sensor.posledni_den_nt_spotreba` | kWh | Spotřeba VT / NT za poslední uzavřený den |

### Odečty, odhady, HDO a diagnostika

| Entita | Typ / jednotka | Popis |
| --- | --- | --- |
| `sensor.spotreba_vt` / `sensor.spotreba_nt` / `sensor.celkova_spotreba` | kWh | Poslední odečet z portálu |
| `sensor.odhadovany_stav_elektromeru` (`_vt`, `_nt`) | kWh | Odečet + hodinová spotřeba od data odečtu |
| `sensor.aktualni_cena` | Kč/kWh | Aktuální cena podle stavu HDO *(u jednotarifu cena VT)* |
| `sensor.stav_hdo` / `sensor.spinani_hdo_dnes` | Stav / počet | Aktuální tarif (**VT** / **NT**) a dnešní NT bloky v atributech |
| `binary_sensor.nizky_tarif` / `binary_sensor.vysoky_tarif` | Binární | Aktivní nízký tarif (NT) a opačný indikátor vysokého tarifu (VT) |
| `sensor.vysoky_tarif_start` / `_konec`, `sensor.nizky_tarif_start` / `_konec` | Čas | Začátek a konec aktuálního / nejbližšího VT a NT bloku |
| `sensor.odpocet_do_konce_vysokeho_tarifu` / `_nizkeho_tarifu` | min | Odpočet do konce bloku *(ve výchozím stavu skryté)* |
| `binary_sensor.porucha_odstavka` | Binární | Hlášená porucha nebo plánovaná odstávka |
| `sensor.posledni_uspesna_aktualizace` / `sensor.hodinova_data_spotreby_k` | Diagnostika | Čas posledního stažení a konec poslední importované hodiny |

> [!NOTE]
> U potvrzeně **jednotarifních OM** se HDO entity nevytvářejí. U **FVE / výroby** (`typ` `V`, `M`, `B`) se vytváří pouze `sensor.celkova_dodavka_do_site`, `binary_sensor.porucha_odstavka` a diagnostika.

</details>

<details>
<summary>🛠️ Řešení problémů, intervaly a diagnostika</summary>

- **Intervaly**: portál (HDO, odečty, odstávky) každou hodinu; MEPAS po doručení včerejška každé 4 hodiny + denní kontrola děr za 62 dní.
- **Historie HDO**: integrace ukládá denní plány až 400 dní zpět a počítá přesný poměr minut NT/VT v každé čtvrthodině (včetně DST).

| Problém | Řešení |
| --- | --- |
| **Nesprávné přihlášení** | Ověřte e-mail a heslo na [dip.cezdistribuce.cz](https://dip.cezdistribuce.cz/). |
| **Portál vyžaduje akci** | Přihlaste se jednou v prohlížeči a potvrďte podmínky/smlouvu. |
| **Žádná odběrná místa** | Účet Můj ČEZ nestačí – OM musí být v portálu ČEZ Distribuce (ne PRE/EG.D). |
| **Portál nedostupný** | Běžná noční údržba – integrace podrží poslední data a zkusí to znovu za hodinu. |
| **Statistika se neposouvá** | Zkontrolujte `sensor.hodinova_data_spotreby_k` a zapněte debug log níže. |

```yaml
# configuration.yaml – debug logování
logger:
  logs:
    custom_components.cez_distribuce: debug
```

```bash
# Ruční ověření bez HA (pip install aiohttp beautifulsoup4)
CEZ_USER='email' CEZ_PASS='heslo' python3 scripts/test_pnd_consumption.py

# Spuštění unit testů
python3 -m unittest discover -s tests -v
```

</details>

---

## Licence a poděkování

- Neoficiální integrace využívající portál [ČEZ Distribuce](https://dip.cezdistribuce.cz) a API aplikace [Proud](https://www.cezdistribuce.cz/proud).
- Vychází z [MattXcz/CEZ](https://github.com/MattXcz/CEZ), rozpad VT/NT a náklady inspirovány [igracek/HACS_CEZD_PND](https://github.com/igracek/HACS_CEZD_PND), za analýzu aplikace Proud díky Romanu Vohradníkovi.
- Šířeno pod licencí [GNU GPL-3.0](LICENSE) (původní MIT oznámení viz [NOTICE](NOTICE)).
