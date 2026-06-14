from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
import signal
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from famd_bot.config import AppConfig, default_config_path, load_runtime_config
from famd_bot.storage.database import Database

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RuntimeContext:
    config: AppConfig
    database: Database


def init_logging(config: AppConfig) -> None:
    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.storage.log_dir / "famd-bot.log", encoding="utf-8"),
        ],
    )


async def build_runtime_context(config_path: Path | None = None, *, require_token: bool = True) -> RuntimeContext:
    resolved = default_config_path(config_path)
    config = load_runtime_config(resolved, require_token=require_token)
    config.ensure_directories()
    init_logging(config)
    database = await Database.connect(config.storage.database_path)
    await database.seed_admins(config.discord.initial_admin_user_ids)
    return RuntimeContext(config=config, database=database)


async def run(config_path: Path | None = None) -> int:
    from famd_bot.runtime import FAMDDiscordClient

    runtime = await build_runtime_context(config_path, require_token=True)
    client = FAMDDiscordClient(runtime)
    loop = asyncio.get_running_loop()
    shutdown_complete = threading.Event()
    cleanup_callbacks = _install_shutdown_handlers(loop, client, shutdown_complete)
    try:
        LOGGER.info("starting FAMD Discord bot")
        await client.start(runtime.config.discord.token)
        return 0
    except asyncio.CancelledError:
        LOGGER.info("FAMD bot shutdown requested")
        raise
    finally:
        for cleanup in reversed(cleanup_callbacks):
            with contextlib.suppress(Exception):
                cleanup()
        try:
            if not client.is_closed():
                await client.close()
        finally:
            await runtime.database.close()
            shutdown_complete.set()


def _install_shutdown_handlers(
    loop: asyncio.AbstractEventLoop,
    client: object,
    shutdown_complete: threading.Event,
) -> list[Callable[[], None]]:
    callbacks: list[Callable[[], None]] = []

    def request_shutdown(reason: str) -> None:
        LOGGER.info("shutdown requested by %s", reason)
        loop.call_soon_threadsafe(lambda: asyncio.create_task(client.close()))  # type: ignore[attr-defined]

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        previous = signal.getsignal(sig)

        def handler(_signum: int, _frame: object, *, name: str = sig_name) -> None:
            request_shutdown(name)

        with contextlib.suppress(ValueError):
            signal.signal(sig, handler)

            def restore(sig: signal.Signals = sig, previous_handler: object = previous) -> None:
                signal.signal(sig, previous_handler)

            callbacks.append(restore)

    if sys.platform == "win32":
        callbacks.extend(_install_windows_console_close_handler(request_shutdown, shutdown_complete))
    return callbacks


def _install_windows_console_close_handler(
    request_shutdown: Callable[[str], None],
    shutdown_complete: threading.Event,
) -> list[Callable[[], None]]:
    kernel32 = ctypes.windll.kernel32
    handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
    ctrl_close_event = 2
    ctrl_logoff_event = 5
    ctrl_shutdown_event = 6

    @handler_type
    def console_handler(ctrl_type: int) -> bool:
        if ctrl_type in {ctrl_close_event, ctrl_logoff_event, ctrl_shutdown_event}:
            request_shutdown(f"Windows console event {ctrl_type}")
            shutdown_complete.wait(timeout=4)
            return True
        return False

    if not kernel32.SetConsoleCtrlHandler(console_handler, True):
        LOGGER.warning("could not install Windows console close handler")
        return []

    def cleanup() -> None:
        kernel32.SetConsoleCtrlHandler(console_handler, False)

    return [cleanup]
