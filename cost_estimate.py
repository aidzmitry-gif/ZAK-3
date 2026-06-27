"""Предварительная (плановая) себестоимость импорта из Китая — методика «Расчёт Китай.xlsx».

Per-line buildup ДО покупки: цена поставщика + комиссия + страховка + фрахт + пошлина →
себест/шт в рабочей валюте → ×курс×(1+буфер) + утильсбор → **landed cost BYN/шт**. Это
ПРЕДВАРИТЕЛЬНАЯ оценка для согласования цены сделки (план), не фактическая (факт — из 1С /
плана счетов ERP) и считается на приёмке через ``allocate_landed_cost``.

⚠️ Это НЕ дубль ``core.services.landed_cost.allocate_landed_cost``: тот разносит ОБЩИЕ
издержки партии на позиции по приёмке (факт). Здесь — независимый per-line расчёт из котировок
поставщика и ставок ДО покупки (комиссия/страховка/фрахт/пошлина считаются по каждой позиции).

Цена реализации (наценка/НДС/маржа) тут НЕ считается — это полоса «Маржа/ценообразование».
Останавливаемся ровно на ``unit_landed_cost_byn`` — границе, которую закупки отдают продажам.

Деньги — ``Decimal``, итог в BYN. Буфер курса (мин. +10%) закладывается на валютную часть
(приоритет №2 — сохранить прибыль от курсовых колебаний). Ставка пошлины — из ТН ВЭД
(справочник ``ref_tnved``, резолв на дату — Горизонт 2); сейчас приходит на вход (или дефолт).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

Path = Literal["cny", "usd"]
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class CostLine:
    """Позиция для предв. расчёта. ``price`` — цена единицы у поставщика в валюте ``path``
    (CNY для ``cny``-пути, USD для ``usd``-пути). ``weight`` — кг брутто на единицу.
    ``duty_pct`` — ставка пошлины по ТН ВЭД (``None`` → берётся ``default_duty_pct``).
    ``util`` — утильсбор BYN на единицу (техника), 0 для прочего."""

    sku_code: str
    path: Path
    price: Decimal
    qty: Decimal
    weight: Decimal
    duty_pct: Decimal | None = None
    util: Decimal = Decimal("0")


@dataclass(frozen=True)
class CostRates:
    """Курсы и ставки расчёта (в проде — настройки + ``ref_tnved``/НБ РБ). Проценты — в %
    (например 3 = 3%), не доли. ``fx_buffer_pct`` — буфер на курсовые колебания (мин. 10)."""

    usd_byn: Decimal
    cny_rub: Decimal
    rub_byn: Decimal
    usd_rub: Decimal
    commission_pct: Decimal
    insurance_pct: Decimal
    freight_usd_per_kg: Decimal
    default_duty_pct: Decimal
    fx_buffer_pct: Decimal


def _money(x: Decimal) -> Decimal:
    return x.quantize(_CENT, rounding=ROUND_HALF_UP)


def estimate_china_cost(lines: list[CostLine], rates: CostRates) -> dict:
    """Предв. landed cost BYN/шт по каждой позиции (см. формулу модуля).

    Возврат::

        {"lines": [{sku_code, goods_byn, freight_byn, duty_byn, util_byn,
                    unit_landed_cost_byn}], "total_landed_byn": Decimal}

    ``unit_landed_cost_byn`` — то же поле, что отдаёт фасад ``last_landed_cost`` (граница с
    продажами): предварительная себестоимость единицы в BYN. Деньги округляются до копейки.
    """
    hundred = Decimal("100")
    comm_rate = rates.commission_pct / hundred
    ins_rate = rates.insurance_pct / hundred
    # буфер курса — не ниже 10% (приоритет №2: не дать курсу съесть прибыль)
    fx_buf = max(rates.fx_buffer_pct, Decimal("10")) / hundred

    out_lines: list[dict] = []
    for ln in lines:
        is_cny = ln.path == "cny"
        # рабочая валюта: CNY-путь → считаем в RUB; USD-путь → сразу в BYN
        goods_unit_wc = ln.price * (rates.cny_rub if is_cny else rates.usd_byn)
        wc_to_byn = rates.rub_byn if is_cny else Decimal("1")
        fx_freight = rates.usd_rub if is_cny else rates.usd_byn  # фрахт USD/кг → раб. валюта

        goods = goods_unit_wc * ln.qty
        comm = goods * comm_rate
        ins = goods * ins_rate
        freight = ln.weight * ln.qty * rates.freight_usd_per_kg * fx_freight
        customs = goods + freight  # таможенная стоимость
        duty_rate = (ln.duty_pct if ln.duty_pct is not None else rates.default_duty_pct) / hundred
        duty = customs * duty_rate
        total = goods + comm + ins + freight + duty
        landed_unit_wc = total / ln.qty if ln.qty else Decimal("0")
        # валютная часть в BYN + буфер на курсовые колебания, затем утильсбор (уже BYN)
        unit_landed = landed_unit_wc * wc_to_byn * (Decimal("1") + fx_buf) + ln.util

        out_lines.append({
            "sku_code": ln.sku_code,
            "goods_byn": _money(goods * wc_to_byn),
            "freight_byn": _money(freight * wc_to_byn),
            "duty_byn": _money(duty * wc_to_byn),
            "util_byn": _money(ln.util),
            "unit_landed_cost_byn": _money(unit_landed),
        })

    return {
        "lines": out_lines,
        "total_landed_byn": sum(
            (_money(r["unit_landed_cost_byn"] * ln.qty) for r, ln in zip(out_lines, lines)),
            Decimal("0"),
        ),
    }
