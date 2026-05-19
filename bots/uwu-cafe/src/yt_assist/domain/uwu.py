from __future__ import annotations

from dataclasses import dataclass

COMBO_ITEM_NAME = "UWU Cafe Combo"
COMBO_UNIT_PRICE = 15_000
COMBO_UNIT_COST = 0
COMBO_COMMISSION_CENTS = 5_000 * 100

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


def combo_commission_cents(quantity: int) -> int:
    return quantity * COMBO_COMMISSION_CENTS


def combo_quantity_from_sale(total_sale: int) -> int:
    if total_sale <= 0:
        return 0
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
