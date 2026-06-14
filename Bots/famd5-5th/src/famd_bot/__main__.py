from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from famd_bot.app import run


def main() -> int:
    parser = argparse.ArgumentParser(description="FAMD Discord bot")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()
    return asyncio.run(run(args.config))
