#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone testovací nástroj pro 15min/hodinovou spotřebu a rozpad VT/NT
(appka Proud, MEPAS/AWS Gateway + HDO signály z portálu ČEZ Distribuce).

DŮLEŽITÉ: tenhle skript NEIMPLEMENTUJE vlastní logiku - importuje přímo
api.py, pnd_processing.py, hdo_split.py a const.py z custom_components/cez_distribuce/,
TEDY STEJNÉ SOUBORY, JAKÉ BĚŽÍ V HA PLUGINU. Cokoliv tady otestuješ, je
doslova stejný kód jako v pluginu - žádná paralelní, časem rozjetá
implementace.

Co skript NEDĚLÁ (na rozdíl od pluginu):
  - nezapisuje nic do HA databáze / dlouhodobých statistik
  - nepočítá běžící součet (ten vyžaduje přístup do HA recorderu,
    mimo HA nedává smysl) - místo toho ukáže hodinové přírůstky celkem,
    VT, NT a náklady

Použití:
  pip install aiohttp beautifulsoup4

  # Základní běh (poslední 2 dny, 15min data + rozpad VT/NT + náklady)
  CEZ_USER='login' CEZ_PASS='heslo' python3 scripts/test_pnd_consumption.py

  # Konkrétní odběrné místo, HDO signál a vlastní ceny VT/NT
  python3 scripts/test_pnd_consumption.py --ean 859182400708532693 --hdo-signal a3b7dp01 --price-vt 5.20 --price-nt 2.80

  # Porovnání 15min (03) vs. hodinového profilu (05) vedle sebe
  python3 scripts/test_pnd_consumption.py --assembly 03,05

  # Víc kódů najednou (issue #30 - hledáme, který sudý kód vrací stav
  # registru dodávky -E u výrobního/mikrozdrojového EAN)
  python3 scripts/test_pnd_consumption.py --ean <vyrobni EAN> --assembly 02,04,06,08,10,12 --no-hdo

  # Ruční zadání období (ISO8601, UTC)
  python3 scripts/test_pnd_consumption.py --from 2026-08-01T00:00:00 --to 2026-08-10T00:00:00

  # Simulace toho, co by plugin natáhl PŘÍŠTĚ, kdyby poslední uložená
  # hodina v HA byla tahle - přesně nahrazuje "co má coordinator.py
  # v get_last_statistics", bez nutnosti mít běžící HA
  python3 scripts/test_pnd_consumption.py --simulate-last-known 2026-08-19T01:00:00

Přihlašovací údaje: --username/--password, nebo proměnné prostředí
CEZ_USER/CEZ_PASS (shodně s dump_readings.py), nebo interaktivní dotaz.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Stejné soubory jako v HA pluginu - viz docstring výše. api.py interně
# importuje const.py relativně (jako HA balíček) a při selhání spadne na
# bare "from const import", proto přidáváme custom_components/cez_distribuce na
# sys.path stejně jako dump_readings.py.
_CEZ_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "cez_distribuce"
if str(_CEZ_DIR) not in sys.path:
    sys.path.insert(0, str(_CEZ_DIR))

from api import CezApiError, CezAuthError, CezDistribuceApiClient  # noqa: E402
from const import (  # noqa: E402
    DEFAULT_PRICE_NT,
    DEFAULT_PRICE_VT,
    OM_TYPES_PRODUCTION,
    PND_ASSEMBLY_15MIN,
    PND_ASSEMBLY_CODE,
    PND_ASSEMBLY_HOURLY,
    PND_INTERVAL_MINUTES,
    PND_INTERVAL_MINUTES_BY_ASSEMBLY,
    PND_TRAILING_SAFETY_DAYS,
    pnd_assembly_code_for,
    pnd_max_interval_days_for,
)
from hdo_split import (  # noqa: E402
    SOURCE_KNOWN,
    SOURCE_NEAREST,
    SOURCE_WEEKDAY,
    ScheduleLookup,
    calculate_hourly_costs,
    extract_signal_schedules,
    split_interval_consumption,
)
from pnd_processing import (  # noqa: E402
    fetch_pnd_chunked,
    infer_interval_minutes,
    process_pnd_points,
)


def _parse_iso(value: str) -> datetime:
    """Přijímá ISO8601 s nebo bez 'Z'/offsetu, vrací tz-aware UTC datetime."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def dump_raw(label: str, data) -> None:
    print(f"\n===== RAW: {label} =====")
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def show_supply_points(data) -> list[dict]:
    points = []
    if isinstance(data, dict):
        blocks = data.get("vstelleBlocks", {}).get("blocks", [])
        for block in blocks:
            points.extend(block.get("vstelles", []))

    print(f"\nOdběrná místa nalezena: {len(points)}")
    for i, p in enumerate(points, 1):
        print(f"   [{i}] EAN: {p.get('ean', '?')}  UID: {p.get('uid', '?')}  Partner: {p.get('partner', '?')}  Typ: {p.get('typ', '?')}")
    return points


async def resolve_supply_point(client: CezDistribuceApiClient, ean_filter: str | None) -> dict:
    supply_data = await client.get_supply_points()
    points = show_supply_points(supply_data)
    if not points:
        raise SystemExit("ČEZ nevrátil žádné odběrné místo.")

    if ean_filter:
        selected = next((p for p in points if p.get("ean") == ean_filter), None)
        if not selected:
            raise SystemExit(f"EAN {ean_filter} nebyl v seznamu nalezen.")
    else:
        selected = points[0]

    return selected


async def resolve_anlage(client: CezDistribuceApiClient, uid: str) -> str:
    detail = await client.get_supply_point_detail(uid)
    anlage = ""
    if isinstance(detail, dict):
        anlage = (detail.get("anlage_Dist") or {}).get("cislo") or detail.get("anlage", "")
    if not anlage:
        raise SystemExit("Nepodařilo se zjistit 'anlage' z detailu odběrného místa.")
    print(f"Anlage: {anlage}")
    return anlage


def write_csv(
    path: Path,
    hourly_buckets: dict[datetime, float],
    local_tz: ZoneInfo,
    *,
    hourly_vt: dict[datetime, float] | None = None,
    hourly_nt: dict[datetime, float] | None = None,
    cost_vt: dict[datetime, float] | None = None,
    cost_nt: dict[datetime, float] | None = None,
    cost_total: dict[datetime, float] | None = None,
) -> None:
    has_tariffs = hourly_vt is not None and hourly_nt is not None
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if has_tariffs:
            writer.writerow(
                [
                    "start_utc",
                    "start_local",
                    "kwh",
                    "kwh_vt",
                    "kwh_nt",
                    "cost_vt",
                    "cost_nt",
                    "cost_total",
                ]
            )
            for hour_start in sorted(hourly_buckets):
                writer.writerow(
                    [
                        hour_start.isoformat(),
                        hour_start.astimezone(local_tz).isoformat(),
                        f"{hourly_buckets[hour_start]:.4f}",
                        f"{hourly_vt.get(hour_start, 0.0):.4f}",
                        f"{hourly_nt.get(hour_start, 0.0):.4f}",
                        f"{(cost_vt or {}).get(hour_start, 0.0):.4f}",
                        f"{(cost_nt or {}).get(hour_start, 0.0):.4f}",
                        f"{(cost_total or {}).get(hour_start, 0.0):.4f}",
                    ]
                )
        else:
            writer.writerow(["start_utc", "kwh"])
            for hour_start in sorted(hourly_buckets):
                writer.writerow([hour_start.isoformat(), f"{hourly_buckets[hour_start]:.4f}"])


def print_hourly_preview(
    hourly_buckets: dict[datetime, float],
    hourly_vt: dict[datetime, float],
    hourly_nt: dict[datetime, float],
    cost_vt: dict[datetime, float],
    cost_nt: dict[datetime, float],
    cost_total: dict[datetime, float],
    local_tz: ZoneInfo,
    max_rows: int = 24,
) -> None:
    """Vypíše přehled posledních hodin s rozpadem VT/NT a nákladů."""
    if max_rows <= 0:
        return
    hours = sorted(hourly_buckets)[-max_rows:]
    if not hours:
        return
    print(f"\n   Posledních {len(hours)} hodin (lokální čas {local_tz.key}):")
    header = (
        f"   {'hodina (lokální)':<22} | {'celkem':>8} | {'VT kWh':>8} | "
        f"{'NT kWh':>8} | {'nákl. VT':>9} | {'nákl. NT':>9} | {'celk. Kč':>9}"
    )
    print(header)
    print("   " + "-" * (len(header) - 3))
    for h in hours:
        local_str = h.astimezone(local_tz).strftime("%Y-%m-%d %H:%M %Z")
        print(
            f"   {local_str:<22} | {hourly_buckets[h]:8.4f} | "
            f"{hourly_vt.get(h, 0.0):8.4f} | {hourly_nt.get(h, 0.0):8.4f} | "
            f"{cost_vt.get(h, 0.0):9.2f} | {cost_nt.get(h, 0.0):9.2f} | "
            f"{cost_total.get(h, 0.0):9.2f}"
        )


def print_monthly_summary(
    hourly_buckets: dict[datetime, float],
    hourly_vt: dict[datetime, float],
    hourly_nt: dict[datetime, float],
    cost_vt: dict[datetime, float],
    cost_nt: dict[datetime, float],
    local_tz: ZoneInfo,
) -> None:
    """Vypíše souhrn spotřeby a nákladů podle kalendářních měsíců (lokální čas)."""
    by_month: dict[str, list[float]] = {}
    hours_by_month: dict[str, int] = {}
    for h, val in hourly_buckets.items():
        month_key = h.astimezone(local_tz).strftime("%Y-%m")
        bucket = by_month.setdefault(month_key, [0.0, 0.0, 0.0, 0.0])
        bucket[0] += round(val, 4)
        bucket[1] += hourly_nt.get(h, 0.0)
        bucket[2] += cost_vt.get(h, 0.0)
        bucket[3] += cost_nt.get(h, 0.0)
        hours_by_month[month_key] = hours_by_month.get(month_key, 0) + 1

    print(f"\n   Měsíční souhrn (lokální čas {local_tz.key}):")
    header = (
        f"   {'měsíc':<7} | {'hodin':>5} | {'celkem kWh':>10} | "
        f"{'VT kWh':>10} | {'NT kWh':>10} | {'náklady Kč':>10}"
    )
    print(header)
    print("   " + "-" * (len(header) - 3))
    for month_key in sorted(by_month):
        raw_tot, raw_nt, raw_c_vt, raw_c_nt = by_month[month_key]
        tot = round(raw_tot, 3)
        nt = min(round(raw_nt, 3), tot)
        vt = round(max(tot - nt, 0.0), 3)
        cost_tot = round(round(raw_c_vt, 2) + round(raw_c_nt, 2), 2)
        print(
            f"   {month_key:<7} | {hours_by_month[month_key]:5d} | "
            f"{tot:10.3f} | {vt:10.3f} | {nt:10.3f} | {cost_tot:10.2f}"
        )


def _assumed_interval_minutes(assembly_code: str) -> int:
    """Předpokládaný krok dat pro daný assemblyCode - jen odhad, skutečný
    krok se vždy odvozuje z časových značek odpovědi (issue #24)."""
    return PND_INTERVAL_MINUTES_BY_ASSEMBLY.get(assembly_code, PND_INTERVAL_MINUTES)


async def run_assembly_code(
    client: CezDistribuceApiClient,
    partner: str,
    ean: str,
    assembly_code: str,
    fetch_start: datetime,
    fetch_end: datetime,
    now: datetime,
    outdir: Path,
    timestamp_tag: str,
    *,
    show_raw: bool,
    lookup: ScheduleLookup | None,
    fallback_all_vt: bool,
    local_tz: ZoneInfo,
    price_vt: float,
    price_nt: float,
    preview_hours: int,
) -> dict:
    """Stáhne a vyhodnotí jeden assemblyCode (kroky 3 a 4), uloží JSON+CSV
    a vrátí souhrn pro závěrečnou tabulku (issue #30 - porovnání víc kódů)."""
    interval_minutes = _assumed_interval_minutes(assembly_code)
    summary: dict = {
        "assembly": assembly_code,
        "records": 0,
        "unit": None,
        "interval": None,
        "first": None,
        "last": None,
        "hourly_points": 0,
        "total_kwh": None,
        "vt_kwh": None,
        "nt_kwh": None,
        "cost_total": None,
        "error": None,
    }

    print(
        f"\n3. Stahuji pnd/data (assemblyCode={assembly_code}, "
        f"předpokládaný interval={interval_minutes}min): {fetch_start.isoformat()} .. {fetch_end.isoformat()}"
    )

    errors: list[str] = []

    def _on_chunk_error(chunk_start, chunk_end, err):
        errors.append(str(err))
        print(f"   [chyba] okno {chunk_start} .. {chunk_end} selhalo: {err}", file=sys.stderr)

    raw_points, unit = await fetch_pnd_chunked(
        client, partner, ean, fetch_start, fetch_end, assembly_code,
        pnd_max_interval_days_for(assembly_code), on_chunk_error=_on_chunk_error,
    )
    print(f"   Staženo syrových záznamů: {len(raw_points)}, jednotka: {unit}")
    summary["records"] = len(raw_points)
    summary["unit"] = unit
    if errors:
        summary["error"] = errors[-1]

    json_path = outdir / f"pnd_raw_{assembly_code}_{timestamp_tag}.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(raw_points, f, ensure_ascii=False, indent=2)
    print(f"   Syrová data uložena do: {json_path}")

    if show_raw:
        dump_raw(f"pnd_data (syrová, assemblyCode={assembly_code})", raw_points)

    if not raw_points:
        return summary

    detected_interval = infer_interval_minutes(raw_points, fallback=interval_minutes)
    if detected_interval != interval_minutes:
        print(
            f"   ⚠️  Krok dat podle časových značek je {detected_interval}min, "
            f"ne předpokládaných {interval_minutes}min - používám odvozenou "
            "hodnotu (issue #24)."
        )
        interval_minutes = detected_interval
    summary["interval"] = interval_minutes

    ordered = sorted(
        (e for e in raw_points if "time" in e and "value" in e),
        key=lambda e: e["time"],
    )
    if ordered:
        summary["first"] = (ordered[0]["time"], ordered[0]["value"])
        summary["last"] = (ordered[-1]["time"], ordered[-1]["value"])
        print(f"   První záznam: {ordered[0]['time']} -> {ordered[0]['value']} {unit or ''}")
        print(f"   Poslední záznam: {ordered[-1]['time']} -> {ordered[-1]['value']} {unit or ''}")

    complete_points, hourly_buckets, trimmed = process_pnd_points(
        raw_points, unit, interval_minutes, fetch_start, now
    )
    print("\n4. Zpracováno (stejná logika jako coordinator.py):")
    print(f"   Ořezáno nulových (zatím neúplných) záznamů z konce: {trimmed}")
    print(f"   Hotových hodinových bodů: {len(hourly_buckets)} (z {len(complete_points)} intervalů po {interval_minutes} min)")
    summary["hourly_points"] = len(hourly_buckets)

    hourly_vt = hourly_nt = cost_vt = cost_nt = cost_total = None
    if hourly_buckets:
        oldest = min(hourly_buckets)
        newest = max(hourly_buckets)
        print(f"   Rozsah: {oldest.isoformat()} .. {newest.isoformat()}")
        print(f"   'last_pnd_timestamp' by byl: {(newest + timedelta(hours=1)).isoformat()}")
        total_kwh = sum(round(v, 4) for v in hourly_buckets.values())
        summary["total_kwh"] = total_kwh
        print(f"   Součet energie v okně: {total_kwh:.4f} kWh")

        if (lookup is not None and lookup) or fallback_all_vt:
            split = split_interval_consumption(
                complete_points,
                interval_minutes,
                lookup if lookup is not None else ScheduleLookup({}),
                lambda dt: dt.astimezone(local_tz),
                fallback_all_vt=fallback_all_vt,
            )
            if split is not None:
                hourly_vt = split.hourly_vt
                hourly_nt = split.hourly_nt
                cost_vt, cost_nt, cost_total = calculate_hourly_costs(
                    hourly_vt, hourly_nt, price_vt, price_nt
                )
                sum_vt = sum(hourly_vt.values())
                sum_nt = sum(hourly_nt.values())
                sum_cost_vt = sum(cost_vt.values())
                sum_cost_nt = sum(cost_nt.values())
                sum_cost_total = sum(cost_total.values())
                summary["vt_kwh"] = sum_vt
                summary["nt_kwh"] = sum_nt
                summary["cost_total"] = sum_cost_total
                vt_pct = (sum_vt / total_kwh * 100.0) if total_kwh > 0 else 0.0
                nt_pct = (sum_nt / total_kwh * 100.0) if total_kwh > 0 else 0.0
                max_hour_diff = max(
                    abs((hourly_vt.get(h, 0.0) + hourly_nt.get(h, 0.0)) - round(v, 4))
                    for h, v in hourly_buckets.items()
                )
                print(
                    f"   Rozpad VT/NT (krok {interval_minutes} min): "
                    f"VT = {sum_vt:.4f} kWh ({vt_pct:.1f} %), "
                    f"NT = {sum_nt:.4f} kWh ({nt_pct:.1f} %), "
                    f"max. odchylka/hod = {max_hour_diff:.6f} kWh"
                )
                if lookup:
                    print(
                        f"   Zdroj plánu HDO (dny): z portálu={split.days_count(SOURCE_KNOWN)}, "
                        f"podle dne v týdnu={split.days_count(SOURCE_WEEKDAY)}, "
                        f"nejbližší={split.days_count(SOURCE_NEAREST)}"
                    )
                else:
                    print("   Zdroj plánu HDO: jednotarifní OM (100 % VT, 0 % NT)")
                print(
                    f"   Náklady (VT={price_vt:.2f} Kč/kWh, NT={price_nt:.2f} Kč/kWh): "
                    f"VT = {sum_cost_vt:.2f} Kč, NT = {sum_cost_nt:.2f} Kč, "
                    f"celkem = {sum_cost_total:.2f} Kč"
                )
                print_monthly_summary(
                    hourly_buckets,
                    hourly_vt,
                    hourly_nt,
                    cost_vt,
                    cost_nt,
                    local_tz,
                )
                if preview_hours > 0:
                    print_hourly_preview(
                        hourly_buckets,
                        hourly_vt,
                        hourly_nt,
                        cost_vt,
                        cost_nt,
                        cost_total,
                        local_tz,
                        max_rows=preview_hours,
                    )

    csv_path = outdir / f"pnd_hourly_{assembly_code}_{timestamp_tag}.csv"
    write_csv(
        csv_path,
        hourly_buckets,
        local_tz,
        hourly_vt=hourly_vt,
        hourly_nt=hourly_nt,
        cost_vt=cost_vt,
        cost_nt=cost_nt,
        cost_total=cost_total,
    )
    print(f"   Hodinová data (jak by šla do HA statistik) uložena do: {csv_path}")
    return summary


def print_summary_table(summaries: list[dict]) -> None:
    """Přehled všech testovaných assemblyCode vedle sebe."""
    print("\n===== SOUHRN PODLE assemblyCode =====")
    has_tariffs = any(s["vt_kwh"] is not None for s in summaries)
    if has_tariffs:
        header = (
            f"{'kód':>4} | {'záznamů':>7} | {'jedn.':>5} | {'krok':>5} | "
            f"{'celkem kWh':>10} | {'VT kWh':>10} | {'NT kWh':>10} | {'náklady Kč':>10}"
        )
        print(header)
        print("-" * len(header))
        for s in summaries:
            interval = f"{s['interval']}min" if s["interval"] else "-"
            total = f"{s['total_kwh']:.3f}" if s["total_kwh"] is not None else "-"
            vt = f"{s['vt_kwh']:.3f}" if s["vt_kwh"] is not None else "-"
            nt = f"{s['nt_kwh']:.3f}" if s["nt_kwh"] is not None else "-"
            cost = f"{s['cost_total']:.2f}" if s["cost_total"] is not None else "-"
            print(
                f"{s['assembly']:>4} | {s['records']:>7} | {str(s['unit'] or '-'):>5} | "
                f"{interval:>5} | {total:>10} | {vt:>10} | {nt:>10} | {cost:>10}"
            )
            if s["error"]:
                print(f"       chyba: {s['error']}")
    else:
        header = (
            f"{'kód':>4} | {'záznamů':>7} | {'jedn.':>5} | {'krok':>5} | "
            f"{'součet kWh':>10} | {'první záznam':<28} | {'poslední záznam'}"
        )
        print(header)
        print("-" * len(header))
        for s in summaries:
            interval = f"{s['interval']}min" if s["interval"] else "-"
            total = f"{s['total_kwh']:.3f}" if s["total_kwh"] is not None else "-"
            first = f"{s['first'][0]} = {s['first'][1]}" if s["first"] else "-"
            last = f"{s['last'][0]} = {s['last'][1]}" if s["last"] else "-"
            print(
                f"{s['assembly']:>4} | {s['records']:>7} | {str(s['unit'] or '-'):>5} | "
                f"{interval:>5} | {total:>10} | {first:<28} | {last}"
            )
            if s["error"]:
                print(f"       chyba: {s['error']}")


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test 15min/hodinové spotřeby ČEZ + rozpadu VT/NT a nákladů - stejný kód jako HA plugin",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--username", default=os.getenv("CEZ_USER"))
    parser.add_argument(
        "--password",
        default=None,
        help="Heslo přímo na příkazové řádce (jinak CEZ_PASS env, nebo dotaz)",
    )
    parser.add_argument("--ean", help="EAN odběrného místa; jinak se vybere první")
    parser.add_argument(
        "--assembly",
        default=None,
        help=f"assemblyCode pro pnd/data (výchozí podle typu OM: {PND_ASSEMBLY_CODE} pro spotřebu, "
        f"{PND_ASSEMBLY_HOURLY}=hodinová kWh, {PND_ASSEMBLY_15MIN}=15min kW). "
        "Lze zadat i víc kódů oddělených čárkou (např. 03,05).",
    )
    parser.add_argument(
        "--hdo-signal",
        default="",
        help="Kód HDO signálu (např. a3b7dp01). Bez zadání se použije první nalezený signál.",
    )
    parser.add_argument(
        "--no-hdo",
        action="store_true",
        help="Přeskočit načtení HDO a rozpad VT/NT (užitečné např. pro výrobní EAN).",
    )
    parser.add_argument(
        "--price-vt",
        type=float,
        default=DEFAULT_PRICE_VT,
        help=f"Cena VT v Kč/kWh pro výpočet nákladů (výchozí: {DEFAULT_PRICE_VT}).",
    )
    parser.add_argument(
        "--price-nt",
        type=float,
        default=DEFAULT_PRICE_NT,
        help=f"Cena NT v Kč/kWh pro výpočet nákladů (výchozí: {DEFAULT_PRICE_NT}).",
    )
    parser.add_argument(
        "--tz",
        default="Europe/Prague",
        help="Časová zóna pro lokální rozpad HDO (výchozí: Europe/Prague).",
    )
    parser.add_argument(
        "--preview-hours",
        type=int,
        default=24,
        help="Kolik posledních hodin vypsat v tabulce VT/NT na obrazovku (0 = nevypisovat, výchozí: 24).",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Před stahováním vypsat pnd/status/{partner} - seznam measurement/assembly kódů.",
    )

    time_group = parser.add_mutually_exclusive_group()
    time_group.add_argument(
        "--from",
        dest="date_from",
        help="Ruční počátek období (ISO8601, UTC), např. 2026-08-01T00:00:00. "
        "Nelze kombinovat s --simulate-last-known.",
    )
    time_group.add_argument(
        "--simulate-last-known",
        dest="simulate_last_known",
        help="ISO8601 čas poslední 'uložené' hodiny - simuluje přesně to, "
        "co by coordinator.py zjistil z get_last_statistics().",
    )

    parser.add_argument(
        "--to",
        dest="date_to",
        help="Ruční konec období (ISO8601, UTC); výchozí = teď. "
        "Ignorováno při použití --simulate-last-known.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Vypsat kompletní JSON odpovědi na obrazovku (jinak jen do souboru)",
    )
    parser.add_argument(
        "--outdir",
        default=".",
        help="Kam uložit výstupní soubory (JSON, CSV). Výchozí: aktuální složka.",
    )
    args = parser.parse_args()

    try:
        local_tz = ZoneInfo(args.tz)
    except ZoneInfoNotFoundError as err:
        raise SystemExit(f"Neznámá časová zóna --tz '{args.tz}': {err}") from err

    username = args.username or input("Uživatelské jméno ČEZ: ").strip()
    password = args.password or os.getenv("CEZ_PASS") or getpass.getpass("Heslo ČEZ: ")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    async with CezDistribuceApiClient(username=username, password=password) as client:
        try:
            print("1. Přihlašuji se do ČEZ Distribuce (portál)...")
            await client.login()
            print("   OK")

            selected = await resolve_supply_point(client, args.ean)
            ean = selected.get("ean", "")
            uid = selected.get("uid", "")
            partner = selected.get("partner", "")
            om_type = str(selected.get("typ") or "")
            is_production = om_type in OM_TYPES_PRODUCTION
            print(f"\nVybráno: EAN={ean}  UID={uid}  Partner={partner}  Typ={om_type or '?'}")

            default_assembly = pnd_assembly_code_for(om_type)
            assembly_codes = [
                code.strip()
                for code in (args.assembly or default_assembly).split(",")
                if code.strip()
            ]
            if not assembly_codes:
                raise SystemExit("--assembly neobsahuje žádný kód.")

            await resolve_anlage(client, uid)

            lookup: ScheduleLookup | None = None
            fallback_all_vt = False
            if args.no_hdo or is_production:
                if is_production and not args.no_hdo:
                    print(
                        f"\n1b. Výrobní/mikrozdrojové OM (typ={om_type}) - "
                        "rozpad VT/NT se přeskočí (stejně jako v coordinator.py)."
                    )
            else:
                print("\n1b. Načítám HDO spínací signály z portálu...")
                try:
                    signals_data = await client.get_signals(ean)
                    if args.raw:
                        dump_raw("signals", signals_data)
                    raw_signals = (
                        signals_data.get("signals") or []
                        if isinstance(signals_data, dict)
                        else []
                    )
                    available = sorted(
                        {
                            str(item.get("signal"))
                            for item in raw_signals
                            if isinstance(item, dict) and item.get("signal")
                        }
                    )
                    if available:
                        if args.hdo_signal:
                            if args.hdo_signal in available:
                                chosen_signal = args.hdo_signal
                            else:
                                ci_match = next(
                                    (s for s in available if s.lower() == args.hdo_signal.lower()),
                                    None,
                                )
                                if ci_match:
                                    print(
                                        f"   [pozn.] --hdo-signal '{args.hdo_signal}' upraven na '{ci_match}'."
                                    )
                                    chosen_signal = ci_match
                                else:
                                    raise SystemExit(
                                        f"HDO signál '{args.hdo_signal}' nebyl pro EAN {ean} nalezen. "
                                        f"Dostupné signály: {', '.join(available)}"
                                    )
                        else:
                            chosen_signal = available[0]
                        print(f"   Dostupné HDO signály: {', '.join(available)} (použitý: {chosen_signal})")
                        schedules = extract_signal_schedules(signals_data, chosen_signal)
                        if schedules:
                            lookup = ScheduleLookup(schedules)
                            for day_key in sorted(schedules)[:4]:
                                print(f"   Plán {day_key}: {schedules[day_key]}")
                        else:
                            print(
                                f"   [varování] Pro signál '{chosen_signal}' nebyly nalezeny žádné platné časy spínání."
                            )
                    else:
                        if args.hdo_signal:
                            raise SystemExit(
                                f"HDO signál '{args.hdo_signal}' nelze použít - portál nevrátil žádné HDO signály."
                            )
                        if isinstance(signals_data, dict):
                            fallback_all_vt = True
                            print(
                                "   Portál nevrátil žádné HDO signály -> jednotarifní OM "
                                "(100 % VT, 0 % NT, stejně jako v coordinator.py)."
                            )
                        else:
                            print("   [pozn.] Neočekávaný formát odpovědi get_signals, pokračuji bez rozpadu VT/NT.")
                except SystemExit:
                    raise
                except Exception as err:  # noqa: BLE001
                    print(f"   [pozn.] Nepodařilo se načíst HDO signály ({err}), pokračuji bez rozpadu VT/NT.")

            print("\n2. Přihlašuji se do MEPAS (AWS Gateway)...")
            await client.login_mepas()
            print("   OK")

            if args.status:
                print(f"\n2b. pnd/status/{partner} (dostupné measurement/assembly kódy):")
                try:
                    status = await client.get_pnd_status(partner)
                    print(json.dumps(status, ensure_ascii=False, indent=2))
                except CezApiError as err:
                    print(f"   [chyba] pnd/status selhalo: {err}", file=sys.stderr)

            now = datetime.now(timezone.utc)

            if args.simulate_last_known:
                last_known = _parse_iso(args.simulate_last_known)
                fetch_start = min(
                    last_known + timedelta(hours=1),
                    now - timedelta(days=PND_TRAILING_SAFETY_DAYS),
                ).replace(minute=0, second=0, microsecond=0)
                fetch_end = now
                if args.date_to:
                    print(
                        "   [pozn.] --to se v režimu --simulate-last-known ignoruje "
                        "(reálný plugin se vždy dotazuje až do 'teď')."
                    )
                print(
                    f"\n[simulace] last_known={last_known.isoformat()} -> "
                    f"fetch_start={fetch_start.isoformat()} "
                    f"(stejný výpočet jako coordinator.py, PND_TRAILING_SAFETY_DAYS={PND_TRAILING_SAFETY_DAYS})"
                )
            elif args.date_from:
                fetch_start = _parse_iso(args.date_from).replace(minute=0, second=0, microsecond=0)
                fetch_end = _parse_iso(args.date_to) if args.date_to else now
            else:
                fetch_start = (now - timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
                fetch_end = _parse_iso(args.date_to) if args.date_to else now

            if fetch_start >= fetch_end:
                raise SystemExit(
                    f"Neplatné období: začátek ({fetch_start.isoformat()}) "
                    f"není před koncem ({fetch_end.isoformat()})."
                )

            timestamp_tag = now.strftime("%Y%m%dT%H%M%S")
            summaries: list[dict] = []
            for assembly_code in assembly_codes:
                summary = await run_assembly_code(
                    client,
                    partner,
                    ean,
                    assembly_code,
                    fetch_start,
                    fetch_end,
                    now,
                    outdir,
                    timestamp_tag,
                    show_raw=args.raw,
                    lookup=lookup,
                    fallback_all_vt=fallback_all_vt,
                    local_tz=local_tz,
                    price_vt=args.price_vt,
                    price_nt=args.price_nt,
                    preview_hours=max(0, args.preview_hours),
                )
                summaries.append(summary)

            if len(summaries) > 1:
                print_summary_table(summaries)

        except (CezAuthError, CezApiError) as err:
            print(f"\nCHYBA: {type(err).__name__}: {err}", file=sys.stderr)
            return 1

    print("\n===== HOTOVO =====")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
