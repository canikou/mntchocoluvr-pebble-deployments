from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TOKEN_PLACEHOLDERS = {"", "PUT_YOUR_BOT_TOKEN_HERE", "YOUR_DISCORD_BOT_TOKEN"}


@dataclass(slots=True)
class DiscordConfig:
    token: str
    clock_in_out_channel_id: int
    logging_main_channel_id: int
    admin_logs_channel_id: int
    admin_commands_channel_id: int
    user_logs_forum_channel_id: int
    response_logs_forum_channel_id: int
    vital_logs_forum_channel_id: int
    prefix: str = ""
    message_content_intent: bool = True
    test_guild_id: int | None = None
    initial_admin_user_ids: list[int] = field(default_factory=list)
    admin_manager_user_ids: list[int] = field(default_factory=list)
    user_log_controls: bool = True
    status_text: str = "handling FAMD shifts"


@dataclass(slots=True)
class StorageConfig:
    database_path: Path
    log_dir: Path = Path("logs")
    proof_dir: Path = Path("data/proofs")


@dataclass(slots=True)
class TimeConfig:
    timezone: str = "Asia/Manila"
    week_start: str = "sunday"
    break_cap_minutes: int = 15
    break_warning_minutes: int = 14
    break_final_prompt_grace_minutes: int = 2
    break_cooldown_minutes: int = 0


@dataclass(slots=True)
class LoggingConfig:
    level: str = "info"


@dataclass(slots=True)
class AppConfig:
    discord: DiscordConfig
    storage: StorageConfig
    time: TimeConfig = field(default_factory=TimeConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @classmethod
    def load_from(
        cls,
        path: Path,
        env: dict[str, str] | None = None,
        *,
        require_token: bool = True,
    ) -> AppConfig:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        unknown_sections = set(raw) - {"discord", "storage", "time", "logging"}
        if unknown_sections:
            raise ValueError(f"Unknown config section(s): {', '.join(sorted(unknown_sections))}")

        discord_raw = dict(raw.get("discord") or {})
        storage_raw = dict(raw.get("storage") or {})
        time_raw = dict(raw.get("time") or {})
        logging_raw = dict(raw.get("logging") or {})

        _reject_unknown(
            "discord",
            discord_raw,
            {
                "token",
                "prefix",
                "message_content_intent",
                "test_guild_id",
                "clock_in_out_channel_id",
                "logging_main_channel_id",
                "admin_logs_channel_id",
                "admin_commands_channel_id",
                "user_logs_forum_channel_id",
                "response_logs_forum_channel_id",
                "vital_logs_forum_channel_id",
                "initial_admin_user_ids",
                "admin_manager_user_ids",
                "user_log_controls",
                "status_text",
            },
        )
        _reject_unknown("storage", storage_raw, {"database_path", "log_dir", "proof_dir"})
        _reject_unknown(
            "time",
            time_raw,
            {
                "timezone",
                "week_start",
                "break_cap_minutes",
                "break_warning_minutes",
                "break_final_prompt_grace_minutes",
                "break_cooldown_minutes",
            },
        )
        _reject_unknown("logging", logging_raw, {"level"})

        discord = DiscordConfig(
            token=str(discord_raw.get("token", "")),
            prefix=str(discord_raw.get("prefix", "")),
            message_content_intent=bool(discord_raw.get("message_content_intent", True)),
            test_guild_id=_optional_int(discord_raw.get("test_guild_id")),
            clock_in_out_channel_id=int(discord_raw["clock_in_out_channel_id"]),
            logging_main_channel_id=int(discord_raw.get("logging_main_channel_id", discord_raw["clock_in_out_channel_id"])),
            admin_logs_channel_id=int(discord_raw["admin_logs_channel_id"]),
            admin_commands_channel_id=int(discord_raw.get("admin_commands_channel_id", discord_raw["admin_logs_channel_id"])),
            user_logs_forum_channel_id=int(discord_raw["user_logs_forum_channel_id"]),
            response_logs_forum_channel_id=int(discord_raw.get("response_logs_forum_channel_id", discord_raw["user_logs_forum_channel_id"])),
            vital_logs_forum_channel_id=int(discord_raw.get("vital_logs_forum_channel_id", discord_raw["user_logs_forum_channel_id"])),
            initial_admin_user_ids=[int(value) for value in discord_raw.get("initial_admin_user_ids", [])],
            admin_manager_user_ids=[int(value) for value in discord_raw.get("admin_manager_user_ids", [])],
            user_log_controls=bool(discord_raw.get("user_log_controls", True)),
            status_text=str(discord_raw.get("status_text", "handling FAMD shifts")),
        )
        storage = StorageConfig(
            database_path=Path(str(storage_raw["database_path"])),
            log_dir=Path(str(storage_raw.get("log_dir", "logs"))),
            proof_dir=Path(str(storage_raw.get("proof_dir", "data/proofs"))),
        )
        config = cls(
            discord=discord,
            storage=storage,
            time=TimeConfig(
                timezone=str(time_raw.get("timezone", "Asia/Manila")),
                week_start=str(time_raw.get("week_start", "sunday")),
                break_cap_minutes=int(time_raw.get("break_cap_minutes", 15)),
                break_warning_minutes=int(time_raw.get("break_warning_minutes", 14)),
                break_final_prompt_grace_minutes=int(time_raw.get("break_final_prompt_grace_minutes", 2)),
                break_cooldown_minutes=int(time_raw.get("break_cooldown_minutes", 0)),
            ),
            logging=LoggingConfig(level=str(logging_raw.get("level", "info"))),
        )
        config.resolve_token(env or os.environ, require_token=require_token)
        return config

    def resolve_token(self, env: dict[str, str], *, require_token: bool = True) -> None:
        env_token = env.get("FAMD_DISCORD_TOKEN", "").strip()
        file_token = self.discord.token.strip()
        resolved = env_token or file_token
        if require_token and resolved in TOKEN_PLACEHOLDERS:
            raise ValueError("discord.token cannot be blank or a placeholder")
        self.discord.token = resolved

    def ensure_directories(self) -> None:
        self.storage.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage.log_dir.mkdir(parents=True, exist_ok=True)
        self.storage.proof_dir.mkdir(parents=True, exist_ok=True)

    def command_trigger(self, command_name: str) -> str:
        prefix = self.discord.prefix.strip()
        if prefix:
            return f"{prefix}!{command_name}"
        return f"!{command_name}"


def _reject_unknown(section: str, values: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"Unknown [{section}] option(s): {', '.join(sorted(unknown))}")


def _optional_int(value: Any) -> int | None:
    if value in {None, "", 0, "0"}:
        return None
    return int(value)


def default_config_path(path: Path | None = None) -> Path:
    if path is not None and path.exists():
        return path
    if path is not None:
        example = path.with_name(path.name + ".example")
        if example.exists():
            return example
    config_path = Path("config") / "app.toml"
    if config_path.exists():
        return config_path
    example_path = Path("config") / "app.toml.example"
    if example_path.exists():
        return example_path
    raise FileNotFoundError("No runtime config file found.")


def load_runtime_config(path: Path | None = None, *, require_token: bool = True) -> AppConfig:
    return AppConfig.load_from(default_config_path(path), require_token=require_token)
