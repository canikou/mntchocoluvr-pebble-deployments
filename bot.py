from __future__ import annotations

import configparser
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "bot-manager.cfg"
LOCAL_CONFIG_PATH = ROOT / "bot-manager.local.cfg"
SECTION_PREFIX = "bot:"
GRACEFUL_STOP_SECONDS = 30
FORCED_STOP_SECONDS = 10


@dataclass(frozen=True)
class BotSpec:
    key: str
    name: str
    root: Path
    stop_file: Path
    module: str
    enabled: bool

    @property
    def src_dir(self) -> Path:
        return self.root / "src"


shutdown_signal: str | None = None
active_specs: tuple[BotSpec, ...] = ()


def resolve_from_root(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def resolve_from_bot_root(value: str, bot_root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else bot_root / path


def select_config_path() -> Path:
    return LOCAL_CONFIG_PATH if LOCAL_CONFIG_PATH.exists() else DEFAULT_CONFIG_PATH


def load_bot_specs(config_path: Path | None = None) -> tuple[BotSpec, ...]:
    config_path = config_path or select_config_path()
    if not config_path.exists():
        raise FileNotFoundError(f"Bot manager config is missing: {config_path}")

    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")

    specs: list[BotSpec] = []
    for section in parser.sections():
        if not section.startswith(SECTION_PREFIX):
            continue

        key = section.removeprefix(SECTION_PREFIX).strip()
        if not key:
            raise ValueError(f"Invalid empty bot section in {config_path}")

        path_value = parser.get(section, "path", fallback="").strip()
        if not path_value:
            raise ValueError(f"{section} must define path")

        root = resolve_from_root(path_value)
        specs.append(
            BotSpec(
                key=key,
                name=parser.get(section, "name", fallback=key).strip() or key,
                root=root,
                stop_file=resolve_from_bot_root(
                    parser.get(section, "stop_file", fallback="data/bot.stop").strip(),
                    root,
                ),
                module=parser.get(section, "module", fallback="yt_assist").strip() or "yt_assist",
                enabled=parser.getboolean(section, "enabled", fallback=True),
            )
        )

    if not specs:
        raise ValueError(f"No bot sections were found in {config_path}")
    return tuple(specs)


def build_child_env(spec: BotSpec) -> dict[str, str]:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(spec.src_dir)
        if not existing_pythonpath
        else str(spec.src_dir) + os.pathsep + existing_pythonpath
    )
    env["PYTHONUNBUFFERED"] = "1"
    return env


def ensure_layout(spec: BotSpec) -> None:
    missing: list[Path] = []
    for path in (spec.root, spec.src_dir, spec.root / "config"):
        if not path.exists():
            missing.append(path)
    if missing:
        joined = ", ".join(str(path) for path in missing)
        raise FileNotFoundError(f"{spec.name} deployment is incomplete: {joined}")


def start_bot(spec: BotSpec) -> subprocess.Popen[bytes]:
    ensure_layout(spec)
    for directory in ("data", "logs", "exports", "import"):
        (spec.root / directory).mkdir(parents=True, exist_ok=True)
    spec.stop_file.unlink(missing_ok=True)

    print(f"Starting {spec.name} from {spec.root}", flush=True)
    return subprocess.Popen(
        [sys.executable, "-u", "-m", spec.module],
        cwd=spec.root,
        env=build_child_env(spec),
    )


def write_stop_file(spec: BotSpec, reason: str) -> None:
    try:
        spec.stop_file.parent.mkdir(parents=True, exist_ok=True)
        spec.stop_file.write_text(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {reason}\n",
            encoding="utf-8",
        )
    except OSError as error:
        print(f"Failed to write stop signal for {spec.name}: {error}", flush=True)


def request_shutdown(reason: str) -> None:
    global shutdown_signal
    if shutdown_signal is None:
        shutdown_signal = reason
        print(f"PebbleHost requested shutdown via {reason}. Stopping all bots...", flush=True)
    for spec in active_specs:
        write_stop_file(spec, reason)


def handle_signal(signum: int, _frame: object) -> None:
    request_shutdown(signal.Signals(signum).name)


def wait_for_exit(
    processes: dict[BotSpec, subprocess.Popen[bytes]],
    timeout_seconds: int,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if all(process.poll() is not None for process in processes.values()):
            return True
        time.sleep(0.5)
    return all(process.poll() is not None for process in processes.values())


def terminate_remaining(processes: dict[BotSpec, subprocess.Popen[bytes]], reason: str) -> None:
    request_shutdown(reason)
    if wait_for_exit(processes, GRACEFUL_STOP_SECONDS):
        return

    for spec, process in processes.items():
        if process.poll() is None:
            print(f"{spec.name} did not stop cleanly; sending terminate.", flush=True)
            process.terminate()

    if wait_for_exit(processes, FORCED_STOP_SECONDS):
        return

    for spec, process in processes.items():
        if process.poll() is None:
            print(f"{spec.name} did not terminate; killing process.", flush=True)
            process.kill()


def check_layout() -> int:
    config_path = select_config_path()
    print(f"Bot manager config: {config_path}", flush=True)
    for spec in load_bot_specs(config_path):
        ensure_layout(spec)
        runtime_config_path = spec.root / "config" / "app.toml"
        config_status = "present" if runtime_config_path.exists() else "missing runtime config"
        enabled_status = "enabled" if spec.enabled else "disabled"
        print(f"{spec.name}: {spec.root} ({enabled_status}, {config_status})", flush=True)
    return 0


def run() -> int:
    global active_specs

    config_path = select_config_path()
    specs = load_bot_specs(config_path)
    enabled_specs = tuple(spec for spec in specs if spec.enabled)
    if not enabled_specs:
        print(f"No bots are enabled in {config_path}.", flush=True)
        return 1
    active_specs = enabled_specs

    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        signal.signal(handled_signal, handle_signal)

    processes: dict[BotSpec, subprocess.Popen[bytes]] = {}
    try:
        for spec in enabled_specs:
            processes[spec] = start_bot(spec)

        while True:
            if shutdown_signal is not None:
                terminate_remaining(processes, shutdown_signal)
                return max(
                    process.returncode if process.returncode is not None else 1
                    for process in processes.values()
                )

            for spec, process in processes.items():
                exit_code = process.poll()
                if exit_code is not None:
                    print(f"{spec.name} exited with status {exit_code}; stopping remaining bots.", flush=True)
                    terminate_remaining(processes, f"{spec.name} exited")
                    return exit_code

            time.sleep(1)
    finally:
        if processes:
            terminate_remaining(processes, "launcher exit")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in {"--check", "check"}:
        raise SystemExit(check_layout())
    raise SystemExit(run())
