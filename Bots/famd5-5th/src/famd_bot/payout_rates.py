from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path


DEFAULT_RANK_RATES: dict[str, int] = {
    "Head Director": 25000,
    "Medical Director": 24000,
    "Assistant Director": 23000,
    "Head of Department": 18000,
    "Attending Physician": 16000,
    "Fellow Resident": 14000,
    "Senior Resident": 10000,
    "Junior Resident": 8000,
    "Paramedic": 6000,
    "Intern": 4000,
}

DEFAULT_RESPONSE_RATE = 3000
DEFAULT_VITAL_RATE = 3000


@dataclass(frozen=True, slots=True)
class PayoutRates:
    rank_rates: dict[str, int]
    response_log_rate: int = DEFAULT_RESPONSE_RATE
    vital_log_rate: int = DEFAULT_VITAL_RATE

    @property
    def ranks(self) -> list[str]:
        defaults = [rank for rank in DEFAULT_RANK_RATES if rank in self.rank_rates]
        extras = sorted(rank for rank in self.rank_rates if rank not in DEFAULT_RANK_RATES)
        return defaults + extras

    def hourly_rate(self, rank: str) -> int:
        return int(self.rank_rates.get(rank, 0))

    def payout_for(self, *, rank: str, duty_minutes: int, response_count: int, vital_count: int) -> int:
        shift_pay = round(max(0, duty_minutes) * self.hourly_rate(rank) / 60)
        log_pay = max(0, response_count) * self.response_log_rate + max(0, vital_count) * self.vital_log_rate
        return int(shift_pay + log_pay)


def load_payout_rates(path: Path = Path("config") / "payoutrates.cfg") -> PayoutRates:
    parser = configparser.ConfigParser()
    parser.optionxform = str
    if path.exists():
        parser.read(path, encoding="utf-8")
    rank_rates = dict(DEFAULT_RANK_RATES)
    if parser.has_section("Hourly Rates"):
        for rank, value in parser.items("Hourly Rates"):
            rank_rates[rank] = int(str(value).replace(",", "").strip())
    response_rate = DEFAULT_RESPONSE_RATE
    vital_rate = DEFAULT_VITAL_RATE
    if parser.has_section("Log Rates"):
        response_rate = int(str(parser.get("Log Rates", "Response", fallback=str(response_rate))).replace(",", "").strip())
        vital_rate = int(str(parser.get("Log Rates", "Vital", fallback=str(vital_rate))).replace(",", "").strip())
    return PayoutRates(rank_rates=rank_rates, response_log_rate=response_rate, vital_log_rate=vital_rate)
