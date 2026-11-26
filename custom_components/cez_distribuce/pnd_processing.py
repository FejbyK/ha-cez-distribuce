"""Čisté zpracování dat z pnd/data - BEZ závislosti na Home Assistantu.

Sdíleno mezi coordinator.py (plugin) a scripts/test_pnd_consumption.py
(standalone test) - existuje kvůli tomu, aby se tahle logika nikdy
neimplementovala na dvou místech zvlášť a časem se nerozjela do
nesouhlasu. Cokoliv, co testuješ přes ten skript, je doslova stejný kód,
jaký běží v HA pluginu.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any


def infer_interval_minutes(
    raw_points: list[dict[str, Any]], fallback: int
) -> int:
    """Odvodí skutečný krok dat (v minutách) z časových značek odpovědi.

    Nutné pro assemblyCode kódy, jejichž granularitu neznáme napevno
    (issue #24 - výrobní/dodávkové EANy nabízejí jiné kódy než spotřeba
    a jejich krok se dřív hádal podle PND_INTERVAL_MINUTES, což u
    assemblyCode=02 vyšlo 4x špatně: skutečný krok byl 15 minut, ne 60).
    Bere modus rozestupů mezi po sobě jdoucími "time" značkami - jeden
    chybějící/duplicitní bod modus nerozhodne. Když je k dispozici méně
    než dva body, vrací 'fallback' (nelze nic odvodit).
    """
    timestamps: list[datetime] = []
    for entry in raw_points:
        try:
            timestamps.append(
                datetime.strptime(entry["time"], "%Y-%m-%dT%H:%M:%S.%fZ")
            )
        except (KeyError, ValueError, TypeError):
            continue
    if len(timestamps) < 2:
        return fallback

    timestamps.sort()
    deltas = [
        round((b - a).total_seconds() / 60)
        for a, b in zip(timestamps, timestamps[1:])
    ]
    deltas = [d for d in deltas if d > 0]
    if not deltas:
        return fallback
    return Counter(deltas).most_common(1)[0][0]


def parse_pnd_entries(
    raw_points: list[dict[str, Any]],
    unit: str | None,
    interval_minutes: int,
    fetch_start: datetime,
) -> list[tuple[datetime, float]]:
    """Naparsuje syrové záznamy z pnd/data na (start_intervalu, kWh).

    'time' v odpovědi ČEZ je KONEC intervalu, ne začátek - odečítáme
    interval_minutes, aby "start" odpovídal skutečnému začátku.
    Přepočet kW -> kWh se dělá jen pokud API vrátilo výkon (unit == "kW"),
    ne když už vrátilo přímo energii (unit == "kWh").

    Deduplikuje podle start_time (hraniční intervaly při rozdělení backfillu
    na více oken by se jinak mohly započítat dvakrát) a ořezává záporné
    hodnoty na 0, aby kumulativní statistiky zůstaly monotónní.
    """
    needs_conversion = (unit or "").strip().lower() == "kw"
    interval_hours = interval_minutes / 60

    by_start: dict[datetime, float] = {}
    for entry in raw_points:
        try:
            end_time = datetime.strptime(entry["time"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=timezone.utc
            )
            raw_value = float(entry["value"])
            kwh = raw_value * interval_hours if needs_conversion else raw_value
        except (KeyError, ValueError, TypeError):
            continue
        start_time = end_time - timedelta(minutes=interval_minutes)
        if start_time < fetch_start:
            continue
        by_start[start_time] = max(kwh, 0.0)

    return sorted(by_start.items(), key=lambda p: p[0])


def trim_trailing_zero_run(
    points: list[tuple[datetime, float]]
) -> tuple[list[tuple[datetime, float]], int]:
    """Ořízne souvislý 'ocas' nul od NEJNOVĚJŠÍHO konce seznamu.

    ČEZ vrací pro ještě nezpracované intervaly placeholder s hodnotou
    přesně 0 - a může se to týkat víc než jen posledního bodu (klidně
    několika hodin zpátky). Jakmile při procházení od konce narazíme na
    první nenulovou hodnotu, všechno starší (i případné skutečné nuly,
    např. výpadek proudu) se bere jako hotové.

    NEfiltruje nuly uprostřed/na začátku seznamu - jen souvislý tail.
    Vrací (oříznutý seznam, počet odstraněných záznamů).
    """
    points = list(points)  # nemutovat vstup
    trimmed = 0
    while points and points[-1][1] == 0:
        points.pop()
        trimmed += 1
    return points, trimmed


def filter_complete_points(
    points: list[tuple[datetime, float]],
    interval_minutes: int,
    now: datetime,
) -> tuple[list[tuple[datetime, float]], dict[datetime, float]]:
    """Vybere body patřící jen do úplných, již skončených hodin.

    Vynechává:
    - poslední (ještě probíhající) hodinu (hour_start + 1h > now) - mohla
      dorazit jen částečně,
    - neúplné hodiny u jemnějšího rozlišení - pokud (u 15min režimu) ČEZ
      doplní jen 3 ze 4 čtvrthodin, hodina se bere jako ještě nehotová,
      ne uměle nízká.

    Vrací (seřazené_body_úplných_hodin, {hour_start_utc: součet_kWh}).
    """
    expected_points_per_hour = max(1, 60 // interval_minutes)

    by_hour: dict[datetime, list[tuple[datetime, float]]] = {}
    for start_time, kwh in points:
        hour_start = start_time.replace(minute=0, second=0, microsecond=0)
        by_hour.setdefault(hour_start, []).append((start_time, kwh))

    complete_points: list[tuple[datetime, float]] = []
    hourly_sums: dict[datetime, float] = {}
    for hour_start in sorted(by_hour):
        hour_points = by_hour[hour_start]
        if (
            hour_start + timedelta(hours=1) <= now
            and len(hour_points) >= expected_points_per_hour
        ):
            hour_points.sort(key=lambda item: item[0])
            complete_points.extend(hour_points)
            hourly_sums[hour_start] = sum(kwh for _, kwh in hour_points)

    return complete_points, hourly_sums


def aggregate_to_hourly(
    points: list[tuple[datetime, float]],
    interval_minutes: int,
    now: datetime,
) -> dict[datetime, float]:
    """Agreguje (případně 15min) body na hodinové součty (kWh)."""
    _, hourly_sums = filter_complete_points(points, interval_minutes, now)
    return hourly_sums


def summarize_pnd_points(raw_points: list[dict[str, Any]]) -> dict[str, Any]:
    """Shrnutí syrových bodů z pnd/data pro DEBUG log (bez hodnot spotřeby).

    Ukáže, kam až API data skutečně vrací: poslední časovou značku vůbec,
    poslední NENULOVOU značku (za ní jsou placeholdery, které se ořežou)
    a jaká další pole body nesou (např. příznak předběžných dat).
    Pomáhá rozlišit "API dnešek ještě nemá" od chyby importu (issue #37).
    """
    times: list[str] = []
    nonzero_times: list[str] = []
    extra_fields: Counter[str] = Counter()
    status_values: Counter[str] = Counter()
    for entry in raw_points:
        if not isinstance(entry, dict):
            continue
        t = entry.get("time")
        if isinstance(t, str):
            times.append(t)
            try:
                if float(entry.get("value")) != 0:
                    nonzero_times.append(t)
            except (TypeError, ValueError):
                pass
        for key in entry:
            if key not in ("time", "value"):
                extra_fields[key] += 1
        for key in ("status", "state", "quality", "flag"):
            if key in entry:
                status_values[f"{key}={entry[key]}"] += 1
    return {
        "points": len(raw_points),
        "first_time": min(times) if times else None,
        "last_time": max(times) if times else None,
        "last_nonzero_time": max(nonzero_times) if nonzero_times else None,
        "extra_fields": dict(extra_fields),
        "status_values": dict(status_values.most_common(10)),
    }


def process_pnd_points(
    raw_points: list[dict[str, Any]],
    unit: str | None,
    interval_minutes: int,
    fetch_start: datetime,
    now: datetime,
) -> tuple[list[tuple[datetime, float]], dict[datetime, float], int]:
    """Zpracuje syrovou odpověď pnd/data na body úplných hodin i hodinové součty.

    Vrací (body_úplných_hodin, hodinove_soucty, pocet_orezanych_nul).
    Zachování 15min bodů umožňuje přesnější rozpad na VT/NT na přechodech HDO
    před agregací do hodinových statistik HA.
    """
    points = parse_pnd_entries(raw_points, unit, interval_minutes, fetch_start)
    points, trimmed = trim_trailing_zero_run(points)
    complete_points, hourly_buckets = filter_complete_points(
        points, interval_minutes, now
    )
    return complete_points, hourly_buckets, trimmed


def process_pnd_response(
    raw_points: list[dict[str, Any]],
    unit: str | None,
    interval_minutes: int,
    fetch_start: datetime,
    now: datetime,
) -> tuple[dict[datetime, float], int]:
    """Kompletní zpracování syrové odpovědi pnd/data na hodinové součty,
    připravené k importu do HA statistik (nebo k výpisu v testovacím
    skriptu).

    Vrací (hodinove_soucty, pocet_orezanych_nul)."""
    _, hourly_buckets, trimmed = process_pnd_points(
        raw_points, unit, interval_minutes, fetch_start, now
    )
    return hourly_buckets, trimmed


def build_statistic_rows(
    hourly_deltas: dict[datetime, float],
    existing_in_window: list[tuple[datetime, float | None, float | None]],
    baseline_sum: float,
    subsequent_rows: list[tuple[datetime, float | None, float | None]] | None = None,
    *,
    precision: int = 4,
) -> tuple[list[dict[str, Any]], float]:
    """Sestaví monotónně rostoucí řádky pro `async_add_external_statistics`.

    - Sloučí nové hodinové přírůstky s případnými již existujícími hodinami v
      témže okně (nová data mají přednost; hodiny chybějící v nové dávce se
      neztratí).
    - Naváže kumulativní `sum` na `baseline_sum` těsně před nejstarší hodinou
      dávky.
    - Pokud v databázi existují body *po* konci dávky (např. při zpětné opravě
      starší díry), posune jejich `sum` o výsledný rozdíl `delta_shift` tak,
      aby křivka zůstala bez skoků a monotónně neklesající.

    Vrací (seznam_řádků_pro_HA, výsledná_poslední_suma).
    """
    if not hourly_deltas:
        return [], baseline_sum

    sorted_existing = sorted(existing_in_window, key=lambda item: item[0])
    existing_deltas: dict[datetime, float] = {}
    running_prev = float(baseline_sum)
    last_existing_sum = float(baseline_sum)
    for p_dt, p_state, p_sum in sorted_existing:
        if p_sum is not None:
            p_sum_f = float(p_sum)
            if p_state is not None:
                existing_deltas[p_dt] = max(float(p_state), 0.0)
            else:
                existing_deltas[p_dt] = max(p_sum_f - running_prev, 0.0)
            running_prev = p_sum_f
            last_existing_sum = p_sum_f
        elif p_state is not None:
            p_state_f = max(float(p_state), 0.0)
            existing_deltas[p_dt] = p_state_f
            running_prev += p_state_f
            last_existing_sum = running_prev

    all_timestamps = sorted(set(hourly_deltas) | set(existing_deltas))
    rows: list[dict[str, Any]] = []
    running_sum = round(float(baseline_sum), precision)

    for ts in all_timestamps:
        raw_delta = hourly_deltas[ts] if ts in hourly_deltas else existing_deltas[ts]
        state_val = round(max(float(raw_delta), 0.0), precision)
        running_sum = round(running_sum + state_val, precision)
        rows.append(
            {
                "start": ts,
                "state": state_val,
                "sum": running_sum,
            }
        )

    final_sum = running_sum
    sorted_subsequent = sorted(subsequent_rows or [], key=lambda item: item[0])
    if sorted_subsequent:
        delta_shift = round(running_sum - last_existing_sum, precision)
        if abs(delta_shift) > 10 ** (-precision - 1):
            prev_sum = running_sum
            for p_dt, p_state, p_sum in sorted_subsequent:
                if p_sum is not None:
                    shifted = round(float(p_sum) + delta_shift, precision)
                elif p_state is not None:
                    shifted = round(prev_sum + max(float(p_state), 0.0), precision)
                else:
                    continue
                if shifted < prev_sum:
                    shifted = prev_sum
                row: dict[str, Any] = {"start": p_dt, "sum": shifted}
                if p_state is not None:
                    row["state"] = round(max(float(p_state), 0.0), precision)
                rows.append(row)
                prev_sum = shifted
            final_sum = prev_sum
        else:
            last_sub_sum = sorted_subsequent[-1][2]
            if last_sub_sum is not None:
                final_sum = float(last_sub_sum)

    return rows, final_sum


def rows_differ_from_existing(
    rows: list[dict[str, Any]],
    existing_in_window: list[tuple[datetime, float | None, float | None]],
    *,
    precision: int = 4,
) -> bool:
    """Vrátí True, pokud by zápis `rows` skutečně změnil existující řádky v DB.

    Slouží ke snížení zbytečných zápisů do HA Recorderu při opakovaném
    přeimportovacím okně (`PND_TRAILING_SAFETY_DAYS`), kdy ČEZ za posledních
    několik dní nic nezměnil ani nedoplnil.
    """
    if not rows:
        return False
    if len(rows) != len(existing_in_window):
        return True

    tol = 10 ** (-precision - 1)
    existing_map = {
        ts: (state, total) for ts, state, total in existing_in_window
    }
    if len(existing_map) != len(rows):
        return True

    for row in rows:
        ts = row["start"]
        if ts not in existing_map:
            return True
        ex_state, ex_sum = existing_map[ts]
        new_state = row.get("state")
        new_sum = row.get("sum")
        if new_state is not None:
            if ex_state is None or abs(float(new_state) - float(ex_state)) > tol:
                return True
        if new_sum is not None:
            if ex_sum is None or abs(float(new_sum) - float(ex_sum)) > tol:
                return True
    return False


async def fetch_pnd_chunked(
    client: Any,
    partner: str,
    ean: str,
    start: datetime,
    end: datetime,
    assembly_code: str,
    max_interval_days: int,
    on_chunk_error: Any = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Stáhne /pnd/data v částech <= max_interval_days (tvrdý limit API -
    ověřeno: 366 dní projde, 367 ne).

    'client' je instance CezDistribuceApiClient (z api.py) - libovolná
    s metodou async get_pnd_data(partner, ean, date_from, date_to,
    assembly_code). Části se selháním (typicky moc stará data, ze kdy
    elektroměr ještě neexistoval) se přeskočí, volitelně přes
    on_chunk_error(chunk_start, chunk_end, chyba) zalogují/vypíšou.
    Vrací (hodnoty, jednotka)."""
    results: list[dict[str, Any]] = []
    unit: str | None = None
    chunk_start = start
    max_delta = timedelta(days=max_interval_days)

    while chunk_start < end:
        chunk_end = min(chunk_start + max_delta, end)
        try:
            data = await client.get_pnd_data(
                partner=partner,
                ean=ean,
                date_from=_fmt(chunk_start),
                date_to=_fmt(chunk_end),
                assembly_code=assembly_code,
            )
        except Exception as err:  # noqa: BLE001 - typ chyby je na volajícím
            if on_chunk_error:
                on_chunk_error(chunk_start, chunk_end, err)
            chunk_start = chunk_end
            continue

        if isinstance(data, list) and data:
            if unit is None:
                unit = data[0].get("unit")
            values = data[0].get("values", [])
            if isinstance(values, list):
                results.extend(values)

        chunk_start = chunk_end

    return results, unit


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def reading_baseline(
    reading: dict[str, Any], local_tz: Any
) -> tuple[datetime, float] | None:
    """Okamžik (UTC, zaokrouhleno dolů na celou hodinu) a součet VT+NT
    (kWh) posledního odečtu z historie odečtů portálu (issue #40).

    Slouží jako základ pro odhad aktuálního stavu elektroměru: základ
    z odečtu + hodinová spotřeba z pnd/data od tohoto okamžiku.

    Předpoklad (neověřeno na portálu, jen podle běžné praxe ČEZ): odečet
    "k datu" bez času (nebo s časem 00:00) je stav na KONCI toho dne,
    tj. o lokální půlnoci následujícího dne. Pokud portál pošle i
    konkrétní čas odečtu (casOdectu, např. odečet technikem), použije
    se ten. Vrací None, když odečet nemá datum nebo ani jednu hodnotu.
    """
    vt = _to_float(reading.get("stavVt"))
    nt = _to_float(reading.get("stavNt"))
    if vt is None and nt is None:
        return None

    raw_date = str(reading.get("datumOdectu") or "").split("T")[0].strip()
    try:
        day = datetime.strptime(raw_date, "%Y-%m-%d")
    except ValueError:
        return None

    hour = minute = 0
    raw_time = str(reading.get("casOdectu") or "").strip()
    parts = raw_time.split(":")
    if len(parts) >= 2:
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            hour = minute = 0
        if not (0 <= hour < 24 and 0 <= minute < 60):
            hour = minute = 0

    if hour == 0 and minute == 0:
        local_naive = day + timedelta(days=1)
    else:
        local_naive = day.replace(hour=hour)  # minuty zahodíme - statistiky jsou po hodinách

    if hasattr(local_tz, "localize"):  # pytz
        local = local_tz.localize(local_naive)
    else:
        local = local_naive.replace(tzinfo=local_tz)
    baseline = local.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return baseline, (vt or 0.0) + (nt or 0.0)


def find_statistic_gaps(
    hours: list[tuple[datetime, float | None]],
    zero_run_hours: int | None,
) -> tuple[datetime | None, int, int]:
    """Najde díry v hodinové statistice (issue #40).

    hours: (začátek hodiny, kWh) seřazené podle času. Díra je:
    - chybějící hodina mezi dvěma uloženými řádky,
    - souvislý běh alespoň `zero_run_hours` hodin s přesně 0 kWh
      (None = nuly nekontrolovat, např. u výroby).
    Hodiny před prvním řádkem se neřeší (elektroměr mohl začít měřit
    později). Vrací (začátek nejstarší díry nebo None, počet chybějících
    hodin, počet hodin v podezřelých nulových bězích).
    """
    earliest: datetime | None = None
    missing = 0
    zero_hours = 0
    previous: datetime | None = None
    run_start: datetime | None = None
    run_len = 0

    def _close_run() -> None:
        nonlocal earliest, zero_hours, run_start, run_len
        if zero_run_hours and run_start is not None and run_len >= zero_run_hours:
            zero_hours += run_len
            if earliest is None or run_start < earliest:
                earliest = run_start
        run_start, run_len = None, 0

    for start, kwh in hours:
        if previous is not None:
            gap = int((start - previous).total_seconds() // 3600) - 1
            if gap > 0:
                missing += gap
                gap_start = previous + timedelta(hours=1)
                if earliest is None or gap_start < earliest:
                    earliest = gap_start
                _close_run()
        if kwh is not None and kwh == 0:
            if run_start is None:
                run_start = start
            run_len += 1
        else:
            _close_run()
        previous = start
    _close_run()
    return earliest, missing, zero_hours


def reading_registers(reading: dict[str, Any]) -> tuple[float | None, float | None]:
    """Stav registrů VT a NT (kWh) z odečtu - pro odhad po tarifech (issue #49)."""
    return _to_float(reading.get("stavVt")), _to_float(reading.get("stavNt"))


def _to_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).strip().replace(",", "."))
    except (ValueError, TypeError):
        return None
