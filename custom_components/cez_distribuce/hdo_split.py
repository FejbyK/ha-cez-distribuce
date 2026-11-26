"""Rozdělení hodinové a 15minutové spotřeby na VT/NT podle plánu HDO.

Data z pnd/data tarif nerozlišují. Plán spínání HDO (NT intervaly pro
každý den) ale známe z portálu, takže lze každý 15minutový (případně
hodinový) interval spotřeby rozpočítat podle toho, kolik minut z něj
připadá na NT, a poté agregovat do hodinových statistik VT/NT a nákladů.
Výsledek je ODHAD:

- uvnitř intervalu se předpokládá rovnoměrná spotřeba (u 15min dat jde
  např. interval 22:30-22:45 s NT od 22:35 z 10/15 do NT, zatímco
  22:45-23:00 celý do NT a 22:00-22:30 celý do VT),
- skutečné sepnutí relé se může od plánu o pár minut lišit,
- portál vrací plán jen pro dnešek a několik dní dopředu, pro starší dny
  se použije plán zapamatovaný z dřívějších aktualizací, případně plán
  stejného dne v týdnu (HDO plány bývají týdně periodické).

Modul je záměrně bez závislosti na Home Assistant, aby šel testovat.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

MINUTES_PER_DAY = 24 * 60

# Kolik dní historie plánů HDO držet v úložišti. Odečty bývají měsíční,
# občas i delší (fakturační období) - rok s rezervou stačí.
HDO_HISTORY_MAX_DAYS = 400

SOURCE_KNOWN = "known"
SOURCE_WEEKDAY = "weekday"
SOURCE_NEAREST = "nearest"


def parse_hhmm(value: str) -> int | None:
    """'22:35' -> 1355, '24:00' -> 1440. Nevalidní -> None."""
    parts = str(value).strip().split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        return None
    total = hour * 60 + minute
    return total if total <= MINUTES_PER_DAY else None


def parse_casy_segments(casy: str) -> list[tuple[int, int]]:
    """Řetězec 'casy' z portálu -> NT úseky v minutách dne [start, end).

    Interval přes půlnoc ('22:35-06:35') se rozdělí na dva úseky v rámci
    téhož kalendářního dne (portál posílá plán pro každý den zvlášť,
    takže ranní část 00:00-06:35 je i v plánu následujícího dne). Překryvy
    a návaznosti se sloučí.
    """
    segments: list[tuple[int, int]] = []
    for part in str(casy or "").split(";"):
        part = part.strip()
        if "-" not in part:
            continue
        raw_start, _, raw_end = part.partition("-")
        start = parse_hhmm(raw_start)
        end = parse_hhmm(raw_end)
        if start is None or end is None or start == end:
            continue
        if end > start:
            segments.append((start, end))
        else:
            if start < MINUTES_PER_DAY:
                segments.append((start, MINUTES_PER_DAY))
            if end > 0:
                segments.append((0, end))

    segments.sort()
    merged: list[tuple[int, int]] = []
    for start, end in segments:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def nt_minutes_in_range(segments: list[tuple[int, int]], start: int, end: int) -> int:
    """Počet NT minut v intervalu [start, end) (minuty dne)."""
    total = 0
    for seg_start, seg_end in segments:
        overlap = min(end, seg_end) - max(start, seg_start)
        if overlap > 0:
            total += overlap
    return total


def extract_signal_schedules(signals_data: Any, hdo_signal: str) -> dict[str, str]:
    """Odpověď get_signals -> {'YYYY-MM-DD': casy} pro zvolený signál.

    Bez nastaveného signálu se bere první záznam pro daný den - stejně
    jako to dělají senzory HDO v sensor.py.
    """
    result: dict[str, str] = {}
    if not isinstance(signals_data, dict):
        return result
    for entry in signals_data.get("signals") or []:
        if not isinstance(entry, dict):
            continue
        if hdo_signal and entry.get("signal") != hdo_signal:
            continue
        try:
            day = datetime.strptime(str(entry.get("datum")), "%d.%m.%Y").date()
        except (TypeError, ValueError):
            continue
        key = day.isoformat()
        if key in result:
            continue
        casy = entry.get("casy")
        if isinstance(casy, str):
            result[key] = casy
    return result


def merge_schedule_history(
    history: dict[str, str], fresh: dict[str, str], today: date
) -> bool:
    """Doplní/aktualizuje historii plánů, ořízne staré dny. Vrací True při změně."""
    changed = False
    for key, casy in fresh.items():
        if history.get(key) != casy:
            history[key] = casy
            changed = True
    cutoff = (today - timedelta(days=HDO_HISTORY_MAX_DAYS)).isoformat()
    for key in [k for k in history if k < cutoff]:
        del history[key]
        changed = True
    return changed


@dataclass
class ScheduleLookup:
    """Hledání plánu HDO pro libovolný den s fallbackem."""

    history: dict[str, str]
    _parsed: dict[date, list[tuple[int, int]]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for key, casy in self.history.items():
            try:
                day = date.fromisoformat(key)
            except ValueError:
                continue
            self._parsed[day] = parse_casy_segments(casy)

    def __bool__(self) -> bool:
        return bool(self._parsed)

    def segments_for(self, day: date) -> tuple[list[tuple[int, int]], str] | None:
        """NT úseky pro den + zdroj (known / weekday / nearest)."""
        if not self._parsed:
            return None
        if day in self._parsed:
            return self._parsed[day], SOURCE_KNOWN
        same_weekday = [d for d in self._parsed if d.weekday() == day.weekday()]
        if same_weekday:
            best = min(same_weekday, key=lambda d: abs((d - day).days))
            return self._parsed[best], SOURCE_WEEKDAY
        best = min(self._parsed, key=lambda d: abs((d - day).days))
        return self._parsed[best], SOURCE_NEAREST


@dataclass
class TariffSplit:
    vt_kwh: float = 0.0
    nt_kwh: float = 0.0
    hours: int = 0
    days_by_source: dict[str, set[date]] = field(default_factory=dict)
    # Spotřeba po lokálních dnech: {date: [vt, nt]}
    daily: dict[date, list[float]] = field(default_factory=dict)

    def days_count(self, source: str) -> int:
        return len(self.days_by_source.get(source, ()))


@dataclass
class HourlyTariffSplit:
    """Hodinové rozdělení spotřeby na VT/NT v UTC hodinových oknech.

    Slouží k zápisu externích dlouhodobých statistik `consumption_vt`,
    `consumption_nt` a návazných nákladů. Klíče jsou UTC `datetime`
    zaokrouhlené dolů na celou hodinu, takže při přechodu letního/zimního
    času nevznikají kolize.
    """

    hourly_vt: dict[datetime, float] = field(default_factory=dict)
    hourly_nt: dict[datetime, float] = field(default_factory=dict)
    days_by_source: dict[str, set[date]] = field(default_factory=dict)

    def days_count(self, source: str) -> int:
        return len(self.days_by_source.get(source, ()))


def split_hourly_consumption(
    hours: Iterable[tuple[datetime, float]],
    lookup: ScheduleLookup,
    to_local: Any,
) -> TariffSplit | None:
    """Rozdělí hodinovou spotřebu na VT/NT.

    hours: (začátek hodiny jako tz-aware datetime, kWh za tu hodinu).
    to_local: funkce převádějící tz-aware datetime do lokální zóny HA
    (dt_util.as_local). Vrací None, když není žádný plán HDO.
    """
    if not lookup:
        return None
    split = TariffSplit()
    for start, kwh in hours:
        local = to_local(start)
        found = lookup.segments_for(local.date())
        if found is None:
            return None
        segments, source = found
        minute = local.hour * 60 + local.minute
        end = min(minute + 60, MINUTES_PER_DAY)
        nt_ratio = nt_minutes_in_range(segments, minute, end) / max(end - minute, 1)
        nt = kwh * nt_ratio
        vt = kwh - nt
        split.vt_kwh += vt
        split.nt_kwh += nt
        split.hours += 1
        split.days_by_source.setdefault(source, set()).add(local.date())
        day = split.daily.setdefault(local.date(), [0.0, 0.0])
        day[0] += vt
        day[1] += nt
    return split


def split_interval_consumption(
    points: Iterable[tuple[datetime, float]],
    interval_minutes: int,
    lookup: ScheduleLookup,
    to_local: Any,
    *,
    fallback_all_vt: bool = False,
    precision: int = 4,
) -> HourlyTariffSplit | None:
    """Rozdělí 15min (nebo 60min) intervaly spotřeby na hodinové VT/NT součty.

    points: (začátek intervalu jako tz-aware UTC datetime, kWh za interval).
    interval_minutes: délka jednoho intervalu v minutách (typicky 15 nebo 60).
    fallback_all_vt: pokud True a `lookup` je prázdný (jednotarifní sazba
    bez HDO), přiřadí 100 % spotřeby do VT a 0 % do NT. Jinak při prázdném
    plánu vrací None.

    Pro každou UTC hodinu platí přesně `hourly_vt[h] + hourly_nt[h] ==
    round(total[h], precision)`.
    """
    if not lookup and not fallback_all_vt:
        return None

    step = max(1, int(interval_minutes))
    result = HourlyTariffSplit()
    raw_total: dict[datetime, float] = {}
    raw_nt: dict[datetime, float] = {}

    for start, kwh in points:
        kwh = max(float(kwh), 0.0)
        hour_start = start.replace(minute=0, second=0, microsecond=0)
        raw_total[hour_start] = raw_total.get(hour_start, 0.0) + kwh

        if lookup:
            local = to_local(start)
            found = lookup.segments_for(local.date())
            if found is None:
                return None
            segments, source = found
            minute = local.hour * 60 + local.minute
            end = min(minute + step, MINUTES_PER_DAY)
            nt_ratio = nt_minutes_in_range(segments, minute, end) / max(end - minute, 1)
            raw_nt[hour_start] = raw_nt.get(hour_start, 0.0) + kwh * nt_ratio
            result.days_by_source.setdefault(source, set()).add(local.date())
        else:
            raw_nt.setdefault(hour_start, 0.0)

    for hour_start in sorted(raw_total):
        total = round(raw_total[hour_start], precision)
        nt = min(round(raw_nt.get(hour_start, 0.0), precision), total)
        vt = round(max(total - nt, 0.0), precision)
        result.hourly_vt[hour_start] = vt
        result.hourly_nt[hour_start] = nt

    return result


def calculate_hourly_costs(
    hourly_vt: dict[datetime, float],
    hourly_nt: dict[datetime, float],
    price_vt: float,
    price_nt: float,
    *,
    precision: int = 4,
) -> tuple[dict[datetime, float], dict[datetime, float], dict[datetime, float]]:
    """Spočítá hodinové náklady VT, NT a jejich přesný součet.

    Zaručuje `cost_total[h] == round(cost_vt[h] + cost_nt[h], precision)` bez
    zaokrouhlovacího rozdílu vůči součtu tarifních proudů.
    """
    p_vt = max(float(price_vt), 0.0)
    p_nt = max(float(price_nt), 0.0)
    cost_vt: dict[datetime, float] = {}
    cost_nt: dict[datetime, float] = {}
    cost_total: dict[datetime, float] = {}

    for hour_start in sorted(set(hourly_vt) | set(hourly_nt)):
        vt_val = round(max(hourly_vt.get(hour_start, 0.0), 0.0) * p_vt, precision)
        nt_val = round(max(hourly_nt.get(hour_start, 0.0), 0.0) * p_nt, precision)
        cost_vt[hour_start] = vt_val
        cost_nt[hour_start] = nt_val
        cost_total[hour_start] = round(vt_val + nt_val, precision)

    return cost_vt, cost_nt, cost_total


def count_schedule_sources(
    timestamps: Iterable[datetime],
    lookup: ScheduleLookup,
    to_local: Any,
) -> tuple[int, int, int]:
    """Spočítá počty dní podle zdroje plánu HDO (known, weekday, nearest)."""
    if not lookup:
        return 0, 0, 0
    days_by_source: dict[str, set[date]] = {}
    for ts in timestamps:
        day = to_local(ts).date()
        found = lookup.segments_for(day)
        if found is None:
            continue
        _, source = found
        days_by_source.setdefault(source, set()).add(day)
    return (
        len(days_by_source.get(SOURCE_KNOWN, ())),
        len(days_by_source.get(SOURCE_WEEKDAY, ())),
        len(days_by_source.get(SOURCE_NEAREST, ())),
    )
