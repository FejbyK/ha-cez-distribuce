<div align="center">

<img height="84" alt="ČEZ Logo" src="https://github.com/user-attachments/assets/cf5f5141-946a-4303-a515-2a65bd3a5efe" />

# ČEZ Distribuce for Home Assistant

**HDO tariff switching, meter readings, outages, and 15-minute high/low tariff (VT/NT) consumption & cost breakdown directly for the Energy dashboard.**

[![HA Marketplace](https://img.shields.io/badge/HA%20Marketplace-Custom-F05A22?style=for-the-badge&labelColor=1F1F1F)](https://www.home-assistant.io/integrations/marketplace/)
[![Marketplace Check](https://img.shields.io/github/actions/workflow/status/FejbyK/ha-cez-distribuce/hacs.yaml?branch=main&style=for-the-badge&label=Marketplace%20Check&labelColor=1F1F1F&color=F05A22)](https://github.com/FejbyK/ha-cez-distribuce/actions/workflows/hacs.yaml)
[![Code Quality](https://img.shields.io/github/actions/workflow/status/FejbyK/ha-cez-distribuce/quality.yaml?branch=main&style=for-the-badge&label=Tests%20%26%20Quality&labelColor=1F1F1F&color=F05A22)](https://github.com/FejbyK/ha-cez-distribuce/actions/workflows/quality.yaml)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/FejbyK/ha-cez-distribuce/hassfest.yaml?branch=main&style=for-the-badge&label=Hassfest&labelColor=1F1F1F&color=F05A22)](https://github.com/FejbyK/ha-cez-distribuce/actions/workflows/hassfest.yaml)
[![License GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-F05A22?style=for-the-badge&labelColor=1F1F1F)](LICENSE)

[Čeština](README.md) | **English**

</div>

---

## Features

<table>
  <tr>
    <td width="50%" valign="top">
      <strong>15-min VT / NT breakdown</strong><br />
      Fetches the 15-minute smart meter profile, splits it according to the actual HDO switching schedule, and imports statistics directly into the <strong>Energy dashboard</strong>.
    </td>
    <td width="50%" valign="top">
      <strong>Monthly and daily costs</strong><br />
      Overview of consumption and costs for the current month, previous month, and last completed day (total, VT, NT).
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>HDO, tariffs, and estimates</strong><br />
      Current tariff, switching times (including overnight intervals), current electricity price per kWh, and live meter register estimate.
    </td>
    <td width="50%" valign="top">
      <strong>PV / solar & micro-generation</strong><br />
      Dedicated grid feed-in statistics without unnecessary HDO sensors. For single-tariff meters, HDO entities are hidden automatically.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>Multiple EANs & environments</strong><br />
      Supports multiple supply points as well as accounts combining <strong>Household</strong> and <strong>Business</strong> portal environments.
    </td>
    <td width="50%" valign="top">
      <strong>Lightweight operation</strong><br />
      Dedicated <code>cez_distribuce</code> domain, shared HTTP sessions, 4-hour polling once yesterday's data arrives, and no unnecessary Recorder writes.
    </td>
  </tr>
</table>

### Preview in Home Assistant

<details>
<img width="1817" height="733" alt="Consumption graph in Home Assistant" src="https://github.com/user-attachments/assets/6277ae12-1542-4322-aa2e-8b8e3f60e37f" />

</details>

---

## Quick Installation

[![Open repository in Marketplace](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=FejbyK&repository=ha-cez-distribuce&category=integration)

1. Click the **Open repository in Marketplace** button above *(or in HA go to **Settings → Marketplace → Add from link** → `https://github.com/FejbyK/ha-cez-distribuce`; custom repositories require a linked GitHub account)*.
2. Install **ČEZ Distribuce** *(restart Home Assistant on older HA versions)*.
3. Add the integration under **Settings → Devices & services → Add integration → ČEZ Distribuce** and sign in with your [dip.cezdistribuce.cz](https://dip.cezdistribuce.cz/) account.

<details>
<summary>Manual installation and setup tips</summary>

- **Manual installation**: copy the `custom_components/cez_distribuce` directory into `config/custom_components/` and restart Home Assistant.
- **Sign-in**: uses your [dip.cezdistribuce.cz](https://dip.cezdistribuce.cz/) email and password (Google/Apple sign-in is not supported).
- **Multiple supply points**: add each supply point (EAN) as a separate entry via **Add entry**.
- **Changing VT/NT prices or HDO signal**: anytime via **Configure** on the entry (the integration reloads automatically).
- **Changing password**: via **⋮ → Reconfigure** (or automatically when prompted to re-authenticate).

</details>

---

## Energy Dashboard Setup

> [!TIP]
> Add either the pair `consumption_vt` + `consumption_nt` **or** just `consumption` to the Energy dashboard — never both at the same time. ČEZ publishes the previous day's data retroactively around 1:00 AM.

| Statistic | Unit | Where to add |
| --- | --- | --- |
| `cez_distribuce:<ean>_consumption_vt` | kWh | **Grid consumption** – VT *(+ cost `…_cost_vt`)* |
| `cez_distribuce:<ean>_consumption_nt` | kWh | **Grid consumption** – NT *(+ cost `…_cost_nt`)* |
| `cez_distribuce:<ean>_consumption` | kWh | **Grid consumption** – combined tariffs *(+ `…_cost_total`)* |
| `cez_distribuce:<ean>_production` | kWh | **Return to grid** *(PV / generation only)* |

*(You can find the exact statistic IDs in the `statistiky` attribute of `sensor.hodinova_data_spotreby_k`. Hourly/15-minute data requires a smart meter with remote readout.)*

<details>
<summary>Recalculating statistics after changing prices or HDO signal</summary>

In **Developer tools → Actions**, run:

```yaml
action: cez_distribuce.recalculate_statistics
data:
  days: 30
  recalculate_costs_only: true
  # ean: "859182400100000000"  # optional; empty = all consumption supply points
```

- `days` (`1`–`1095`, default `30`) – number of days backwards to recalculate.
- `recalculate_costs_only: true` – fast cost-only recalculation without calling the ČEZ API.
- `recalculate_costs_only: false` – re-downloads the 15-minute profile and recalculates both VT/NT split and costs.

</details>

---

## Entities Overview

<details>
<summary>Show full list of created sensors</summary>

### Consumption and costs (smart meter)

| Entity | Unit | Description |
| --- | --- | --- |
| `sensor.celkova_mesicni_spotreba` | kWh | Consumption for the current month *(VT/NT, costs, and previous month in attributes)* |
| `sensor.mesicni_vt_spotreba` / `sensor.mesicni_nt_spotreba` | kWh | Monthly VT / NT consumption |
| `sensor.celkove_mesicni_naklady` | HA currency | Costs for the current month (VT + NT) |
| `sensor.mesicni_naklady_vt` / `sensor.mesicni_naklady_nt` | HA currency | Monthly VT / NT costs |
| `sensor.posledni_den_spotreba` | kWh | Consumption for the last completed calendar day |
| `sensor.posledni_den_vt_spotreba` / `sensor.posledni_den_nt_spotreba` | kWh | VT / NT consumption for the last completed day |

### Meter readings, estimates, HDO, and diagnostics

| Entity | Type / unit | Description |
| --- | --- | --- |
| `sensor.spotreba_vt` / `sensor.spotreba_nt` / `sensor.celkova_spotreba` | kWh | Latest meter reading from the portal |
| `sensor.odhadovany_stav_elektromeru` (`_vt`, `_nt`) | kWh | Last reading + hourly consumption since the reading date |
| `sensor.aktualni_cena` | CZK/kWh | Current price based on HDO state *(VT price for single-tariff meters)* |
| `sensor.stav_hdo` / `sensor.spinani_hdo_dnes` | State / count | Current tariff (**VT** / **NT**) and today's NT blocks in attributes |
| `binary_sensor.nizky_tarif` / `binary_sensor.vysoky_tarif` | Binary | Active low tariff (NT) and inverted high tariff (VT) indicator |
| `sensor.vysoky_tarif_start` / `_konec`, `sensor.nizky_tarif_start` / `_konec` | Time | Start and end of the current / next VT and NT block |
| `sensor.odpocet_do_konce_vysokeho_tarifu` / `_nizkeho_tarifu` | min | Countdown to end of current block *(disabled by default)* |
| `binary_sensor.porucha_odstavka` | Binary | Reported grid fault or planned outage |
| `sensor.posledni_uspesna_aktualizace` / `sensor.hodinova_data_spotreby_k` | Diagnostic | Timestamp of last fetch and end of last imported hour |

> [!NOTE]
> For confirmed **single-tariff supply points**, HDO entities are not created. For **PV / generation** (`typ` `V`, `M`, `B`), only `sensor.celkova_dodavka_do_site`, `binary_sensor.porucha_odstavka`, and diagnostics are created.

</details>

<details>
<summary>Troubleshooting, polling intervals, and diagnostics</summary>

- **Polling intervals**: portal data (HDO, readings, outages) every hour; MEPAS every 4 hours once yesterday's data is available + daily gap check over the last 62 days.
- **HDO history**: stores daily switching schedules up to 400 days back and computes exact NT/VT minute ratios for every 15-minute interval (including DST transitions).

| Issue | Solution |
| --- | --- |
| **Invalid credentials** | Verify your email and password at [dip.cezdistribuce.cz](https://dip.cezdistribuce.cz/). |
| **Portal requires action** | Sign in once in a browser and accept pending terms/contracts. |
| **No supply points found** | A Můj ČEZ account is not enough — the supply point must be in the ČEZ Distribuce portal (not PRE or EG.D). |
| **Portal unavailable** | Typical nightly maintenance — the integration keeps the last known data and retries in an hour. |
| **Statistics stopped advancing** | Check `sensor.hodinova_data_spotreby_k` and enable debug logging below. |

```yaml
# configuration.yaml – debug logging
logger:
  logs:
    custom_components.cez_distribuce: debug
```

```bash
# Manual verification outside HA (pip install aiohttp beautifulsoup4)
CEZ_USER='email' CEZ_PASS='password' python3 scripts/test_pnd_consumption.py

# Run unit tests
python3 -m unittest discover -s tests -v
```

</details>

---

## License and Acknowledgements

- Unofficial integration using the [ČEZ Distribuce](https://dip.cezdistribuce.cz) portal and the [Proud](https://www.cezdistribuce.cz/proud) app API.
- Based on [MattXcz/CEZ](https://github.com/MattXcz/CEZ), with VT/NT breakdown and cost calculations inspired by [igracek/HACS_CEZD_PND](https://github.com/igracek/HACS_CEZD_PND); thanks to Roman Vohradník for analyzing the Proud app.
- Distributed under the [GNU GPL-3.0](LICENSE) license (for the original MIT notice see [NOTICE](NOTICE)).
