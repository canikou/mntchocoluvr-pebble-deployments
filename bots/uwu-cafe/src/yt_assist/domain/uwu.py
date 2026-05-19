from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

COMBO_ITEM_NAME = "UWU Cafe Combo"
COMBO_UNIT_PRICE = 15_000
COMBO_UNIT_COST = 0
COMBO_COMMISSION_CENTS = 5_000 * 100
BULK_DISCOUNT_MIN_COMBOS = 20
BULK_DISCOUNT_BPS = 1_000
BPS_DENOMINATOR = 10_000

REMIT_RATES: dict[str, int] = {
    "tomato": 80,
    "packed_meat": 200,
    "arabica": 80,
    "wheat": 80,
    "spatula": 2_000,
    "cooking_oil": 200,
    "glass": 80,
}

REMIT_ALIASES: dict[str, str] = {
    "tomato": "tomato",
    "tomatoes": "tomato",
    "packedmeat": "packed_meat",
    "packed_meat": "packed_meat",
    "packed meat": "packed_meat",
    "pmeat": "packed_meat",
    "meat": "packed_meat",
    "arabica": "arabica",
    "coffee": "arabica",
    "wheat": "wheat",
    "spatula": "spatula",
    "cooking oil": "cooking_oil",
    "cookingoil": "cooking_oil",
    "cooking_oil": "cooking_oil",
    "oil": "cooking_oil",
    "glass": "glass",
}


@dataclass(frozen=True, slots=True)
class RemitItem:
    key: str
    display_name: str
    unit_rate: int


@dataclass(frozen=True, slots=True)
class RemitCatalogItem:
    key: str
    display_name: str
    unit_rate: int
    enabled: bool


def combo_commission_cents(quantity: int) -> int:
    return quantity * COMBO_COMMISSION_CENTS


def combo_discount_bps(quantity: int) -> int:
    return BULK_DISCOUNT_BPS if quantity >= BULK_DISCOUNT_MIN_COMBOS else 0


def combo_discount_amount(quantity: int) -> int:
    subtotal = quantity * COMBO_UNIT_PRICE
    return subtotal * combo_discount_bps(quantity) // BPS_DENOMINATOR


def combo_sale_total(quantity: int) -> int:
    subtotal = quantity * COMBO_UNIT_PRICE
    return subtotal - combo_discount_amount(quantity)


def combo_effective_unit_price(quantity: int) -> int:
    if quantity <= 0:
        return COMBO_UNIT_PRICE
    return combo_sale_total(quantity) // quantity


def combo_quantity_from_sale(total_sale: int) -> int:
    if total_sale <= 0:
        return 0
    highest_possible_quantity = total_sale // (COMBO_UNIT_PRICE - (COMBO_UNIT_PRICE * BULK_DISCOUNT_BPS // BPS_DENOMINATOR))
    for quantity in range(highest_possible_quantity + 1, 0, -1):
        if combo_sale_total(quantity) == total_sale:
            return quantity
    return total_sale // COMBO_UNIT_PRICE


def combo_commission_cents_from_sale(total_sale: int) -> int:
    return combo_commission_cents(combo_quantity_from_sale(total_sale))


def parse_combo_quantity(value: str | None) -> int:
    cleaned = _clean_count(value or "")
    if not cleaned:
        raise ValueError("Use `u!log <combo_count>` with a proof image.")
    quantity = _parse_positive_integer(cleaned, "Combo count")
    return quantity


def parse_remit_args(value: str | None) -> tuple[RemitItem, int]:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError("Use `u!remit <item> <amount>` with a proof image.")

    parts = cleaned.split()
    if len(parts) < 2:
        raise ValueError("Use `u!remit <item> <amount>` with a proof image.")

    amount = _parse_positive_integer(_clean_count(parts[-1]), "Remit amount")
    raw_item = " ".join(parts[:-1])
    key = normalize_remit_item_key(raw_item)
    if key is None:
        valid = ", ".join(remit_item(item_key).display_name for item_key in REMIT_RATES)
        raise ValueError(f"Unknown remit item `{raw_item}`. Valid items: {valid}.")
    return remit_item(key), amount


def normalize_remit_item_key(value: str) -> str | None:
    normalized = _normalize_item_token(value)
    return REMIT_ALIASES.get(normalized)


def remit_item(key: str) -> RemitItem:
    return RemitItem(
        key=key,
        display_name=key.replace("_", " ").title(),
        unit_rate=REMIT_RATES[key],
    )


def default_remit_statuses() -> dict[str, bool]:
    return {key: True for key in REMIT_RATES}


def load_remit_statuses(path: Path | str) -> dict[str, bool]:
    path = Path(path)
    statuses = default_remit_statuses()
    if not path.exists():
        save_remit_statuses(path, statuses)
        return statuses

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        save_remit_statuses(path, statuses)
        return statuses

    if isinstance(data, dict):
        raw_items = data.get("items", data)
        if isinstance(raw_items, dict):
            for key in REMIT_RATES:
                value = raw_items.get(key)
                if isinstance(value, bool):
                    statuses[key] = value
                elif isinstance(value, dict) and isinstance(value.get("enabled"), bool):
                    statuses[key] = bool(value["enabled"])
    return statuses


def save_remit_statuses(path: Path | str, statuses: dict[str, bool]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = {key: bool(statuses.get(key, True)) for key in REMIT_RATES}
    path.write_text(
        json.dumps({"items": normalized}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def set_remit_item_enabled(path: Path | str, key: str, enabled: bool) -> dict[str, bool]:
    if key not in REMIT_RATES:
        raise ValueError(f"Unknown remit item `{key}`.")
    statuses = load_remit_statuses(path)
    statuses[key] = enabled
    save_remit_statuses(path, statuses)
    return statuses


def set_all_remit_items_enabled(path: Path | str, enabled: bool) -> dict[str, bool]:
    statuses = {key: enabled for key in REMIT_RATES}
    save_remit_statuses(path, statuses)
    return statuses


def remit_catalog_items(statuses: dict[str, bool]) -> list[RemitCatalogItem]:
    return [
        RemitCatalogItem(
            key=key,
            display_name=remit_item(key).display_name,
            unit_rate=rate,
            enabled=bool(statuses.get(key, True)),
        )
        for key, rate in REMIT_RATES.items()
    ]


def _clean_count(value: str) -> str:
    return value.strip().replace(",", "").replace("_", "")


def _parse_positive_integer(value: str, label: str) -> int:
    if not value or not value.isdigit():
        raise ValueError(f"{label} must be a positive whole number.")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{label} must be greater than 0.")
    return parsed


def _normalize_item_token(value: str) -> str:
    return " ".join(value.strip().lower().replace("-", " ").replace("_", " ").split())
