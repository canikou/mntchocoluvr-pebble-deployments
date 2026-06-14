from __future__ import annotations

import logging
import re
import asyncio
import contextlib
import secrets
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from math import ceil
from typing import Any

import discord
from discord import app_commands

from famd_bot.app import RuntimeContext
from famd_bot.models import ChangeRequest, Employee, RequestStatus, RequestType, Shift, ShiftStatus
from famd_bot.models import ServiceLog, ServiceLogKind, ServiceLogStatus
from famd_bot.payout_rates import load_payout_rates
from famd_bot.render import (
    BUTTON_STYLE_DANGER,
    BUTTON_STYLE_PRIMARY,
    BUTTON_STYLE_SECONDARY,
    BUTTON_STYLE_SUCCESS,
    DUTY_BOARD_TITLE,
    ReplyPayload,
    ViewPayload,
    admin_manager_payload,
    admin_remove_confirm_payload,
    admin_remove_select_payload,
    break_final_prompt_payload,
    combined_stats_payload,
    cutoff_message,
    doctors_payouts_payload,
    duty_board_payload,
    employee_manager_payload,
    employee_rank_select_payload,
    employee_remove_confirm_payload,
    employee_select_payload,
    logging_board_payload,
    payout_preview_payload,
    request_payload,
    snapshot_revert_confirm_payload,
    snapshot_revert_select_payload,
    service_log_payload,
    service_totals_payload,
    shift_log_payload,
    user_totals_payload,
    weekly_logs_payload,
)
from famd_bot.time_utils import format_display_dt, format_duration, parse_user_datetime, utc_now

LOGGER = logging.getLogger(__name__)
LEGACY_EMPLOYEE_THREAD_PREFIX = "FAMD Logs - "
BOARD_MESSAGE_ID_KEY = "clock_board_message_id"
LOGGING_BOARD_MESSAGE_ID_KEY = "logging_board_message_id"
ADMIN_ID_RE = re.compile(r"<@!?(\d+)>|(\b\d{15,25}\b)")
DISCORD_ID_RE = re.compile(r"\d{15,25}")
TRANSIENT_PANEL_SECONDS = 300


@dataclass(slots=True)
class PendingPayoutSnapshot:
    owner_user_id: int
    snapshot_type: str
    user_ids: list[str] | None
    include_skipped: bool = False
    created_at: datetime = field(default_factory=utc_now)


class FAMDDiscordClient(discord.Client):
    def __init__(self, runtime: RuntimeContext) -> None:
        intents = discord.Intents.default()
        intents.message_content = runtime.config.discord.message_content_intent
        super().__init__(intents=intents)
        self.runtime = runtime
        self.tree = app_commands.CommandTree(self)
        self._board_ready = False
        self._break_monitor_started = False
        self._break_monitor_task: asyncio.Task[None] | None = None
        self._duty_board_refresh_started = False
        self._duty_board_refresh_task: asyncio.Task[None] | None = None
        self._transient_cleanup_started = False
        self._transient_cleanup_task: asyncio.Task[None] | None = None
        self._pending_admin_add: dict[int, datetime] = {}
        self._pending_employee_add: dict[int, datetime] = {}
        self._pending_payout_snapshots: dict[str, PendingPayoutSnapshot] = {}

    async def setup_hook(self) -> None:
        self._register_slash_commands()
        test_guild_id = self.runtime.config.discord.test_guild_id
        if test_guild_id is not None:
            guild = discord.Object(id=test_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        if self.user is None:
            return
        await self.change_presence(activity=discord.Game(name=self.runtime.config.discord.status_text))
        LOGGER.info("connected as %s (%s)", self.user.name, self.user.id)
        if not self._board_ready:
            self._board_ready = True
            await self.ensure_duty_board()
            await self.ensure_logging_board()
        if not self._break_monitor_started:
            self._break_monitor_started = True
            self._break_monitor_task = asyncio.create_task(self._monitor_breaks())
        if not self._duty_board_refresh_started:
            self._duty_board_refresh_started = True
            self._duty_board_refresh_task = asyncio.create_task(self._refresh_duty_board_periodically())
        if not self._transient_cleanup_started:
            self._transient_cleanup_started = True
            self._transient_cleanup_task = asyncio.create_task(self._monitor_transient_state())

    async def close(self) -> None:
        for task in (self._break_monitor_task, self._duty_board_refresh_task, self._transient_cleanup_task):
            if task is not None:
                task.cancel()
        await super().close()
        if self._break_monitor_task is not None:
            try:
                await self._break_monitor_task
            except asyncio.CancelledError:
                pass
            self._break_monitor_task = None
        if self._duty_board_refresh_task is not None:
            try:
                await self._duty_board_refresh_task
            except asyncio.CancelledError:
                pass
            self._duty_board_refresh_task = None
        if self._transient_cleanup_task is not None:
            try:
                await self._transient_cleanup_task
            except asyncio.CancelledError:
                pass
            self._transient_cleanup_task = None

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if await self._handle_pending_service_log_proof(message):
            return
        if await self._handle_pending_admin_add(message):
            return
        if await self._handle_pending_employee_add(message):
            return
        parsed = self._parse_prefix_command(message.content)
        if parsed is None:
            return
        command_name, _remainder = parsed
        if command_name == "manageadmins":
            await self._send_manageadmins_from_message(message)
        if command_name == "manageemployees":
            await self._send_manageemployees_from_message(message)
        if command_name == "stats":
            await self._send_stats_from_message(message)
        if command_name == "weeklysnapshot":
            await self._weeklysnapshot_from_message(message, _remainder)
        if command_name == "revertsnapshot":
            await self._revertsnapshot_from_message(message)
        if command_name == "updatethreadname":
            await self._update_thread_name_from_message(message, _remainder)
        if command_name == "attachthread":
            await self._attach_thread_from_message(message, _remainder)

    async def on_interaction(self, interaction: discord.Interaction[Any]) -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = _interaction_custom_id(interaction)
        if not custom_id.startswith("famd:"):
            return
        try:
            await self._dispatch_component(interaction, custom_id)
        except Exception as error:
            LOGGER.exception("custom_id=%s component failed", custom_id)
            await _safe_interaction_message(interaction, f"That action failed: {error}", ephemeral=True)

    def _register_slash_commands(self) -> None:
        @self.tree.command(name="manageadmins", description="Show the FAMD admin management panel.")
        async def manageadmins(interaction: discord.Interaction[Any]) -> None:
            await self._send_manageadmins_from_interaction(interaction)

        @self.tree.command(name="stats", description="Show current FAMD shift totals.")
        async def stats(interaction: discord.Interaction[Any]) -> None:
            await self._send_stats_from_interaction(interaction)

        @self.tree.command(name="manageemployees", description="Show the FAMD employee management panel.")
        async def manageemployees(interaction: discord.Interaction[Any]) -> None:
            await self._send_manageemployees_from_interaction(interaction)

        @self.tree.command(name="weeklysnapshot", description="Preview and confirm a payout snapshot.")
        @app_commands.describe(users="Optional mentions or raw Discord IDs for an early payout snapshot.")
        async def weeklysnapshot(interaction: discord.Interaction[Any], users: str = "") -> None:
            await self._weeklysnapshot_from_interaction(interaction, users)

        @self.tree.command(name="revertsnapshot", description="Choose an active payout snapshot to revert.")
        async def revertsnapshot(interaction: discord.Interaction[Any]) -> None:
            await self._revertsnapshot_from_interaction(interaction)

        @self.tree.command(name="updatethreadname", description="Sync a user's FAMD log thread name to their server nickname.")
        @app_commands.describe(member="The member whose log thread should be renamed. Defaults to you.")
        async def updatethreadname(
            interaction: discord.Interaction[Any],
            member: discord.Member = None,
        ) -> None:
            await self._update_thread_name_from_interaction(interaction, member)

        @self.tree.command(name="attachthread", description="Attach a FAMD log thread to a member.")
        @app_commands.describe(kind="shift, response, or vital.", member="The member to attach the thread to.", thread="Thread ID or Discord thread link.")
        async def attachthread(
            interaction: discord.Interaction[Any],
            kind: str,
            member: discord.Member,
            thread: str,
        ) -> None:
            await self._attach_thread_from_interaction(interaction, kind, member, thread)

    def _parse_prefix_command(self, content: str) -> tuple[str, str] | None:
        stripped = content.strip()
        configured = self.runtime.config.discord.prefix.strip()
        if configured:
            marker = f"{configured}!"
            if not stripped.lower().startswith(marker.lower()):
                return None
            body = stripped[len(marker) :].strip()
        else:
            if not stripped.startswith("!"):
                return None
            body = stripped[1:].strip()
        if not body:
            return ("help", "")
        parts = body.split(maxsplit=1)
        return parts[0].lower(), parts[1] if len(parts) > 1 else ""

    async def ensure_duty_board(self) -> None:
        channel = await self._fetch_text_channel(self.runtime.config.discord.clock_in_out_channel_id)
        if channel is None:
            LOGGER.warning("clock_in_out_channel_id is not a text channel")
            return
        payload = await self._duty_board_payload()
        existing = await self._find_duty_board_message(channel)
        if existing is None:
            sent = await channel.send(**self._send_kwargs(payload))
            await self.runtime.database.set_setting(BOARD_MESSAGE_ID_KEY, str(sent.id))
            return
        await existing.edit(**self._edit_kwargs(payload))
        await self.runtime.database.set_setting(BOARD_MESSAGE_ID_KEY, str(existing.id))

    async def ensure_logging_board(self) -> None:
        channel = await self._fetch_text_channel(self.runtime.config.discord.logging_main_channel_id)
        if channel is None:
            LOGGER.warning("logging_main_channel_id is not a text channel")
            return
        payload = logging_board_payload()
        existing = await self._find_embed_message(channel, LOGGING_BOARD_MESSAGE_ID_KEY, "FAMD Field Logging")
        if existing is None:
            sent = await channel.send(**self._send_kwargs(payload))
            await self.runtime.database.set_setting(LOGGING_BOARD_MESSAGE_ID_KEY, str(sent.id))
            return
        await existing.edit(**self._edit_kwargs(payload))
        await self.runtime.database.set_setting(LOGGING_BOARD_MESSAGE_ID_KEY, str(existing.id))

    async def refresh_duty_board(self) -> None:
        await self.ensure_duty_board()

    async def _find_duty_board_message(self, channel: discord.TextChannel) -> discord.Message | None:
        return await self._find_embed_message(channel, BOARD_MESSAGE_ID_KEY, DUTY_BOARD_TITLE)

    async def _find_embed_message(self, channel: discord.TextChannel, setting_key: str, title: str) -> discord.Message | None:
        stored = await self.runtime.database.get_setting(setting_key)
        if stored:
            try:
                return await channel.fetch_message(int(stored))
            except (discord.HTTPException, ValueError):
                pass
        bot_user_id = self.user.id if self.user is not None else None
        matches: list[discord.Message] = []
        async for message in channel.history(limit=100):
            if bot_user_id is not None and message.author.id != bot_user_id:
                continue
            if any(embed.title == title for embed in message.embeds):
                matches.append(message)
        for duplicate in matches[1:]:
            try:
                await duplicate.delete()
            except discord.HTTPException:
                LOGGER.debug("message_id=%s duplicate board delete skipped", duplicate.id)
        return matches[0] if matches else None

    async def _duty_board_payload(self) -> ReplyPayload:
        shifts = await self.runtime.database.list_active_shifts()
        return duty_board_payload(
            shifts,
            self.runtime.config.time.timezone,
            break_cap_minutes=self.runtime.config.time.break_cap_minutes,
        )

    async def _dispatch_component(self, interaction: discord.Interaction[Any], custom_id: str) -> None:
        parts = custom_id.split(":")
        if custom_id == "famd:clock_in":
            await self._handle_clock_in(interaction)
            return
        if custom_id == "famd:clock_out":
            await self._handle_clock_out(interaction)
            return
        if custom_id == "famd:break_toggle":
            await self._handle_break_toggle(interaction)
            return
        if custom_id == "famd:logging_select":
            await self._handle_logging_select(interaction)
            return
        if len(parts) >= 4 and parts[1] == "break":
            action, break_id = parts[2], int(parts[3])
            await self._handle_break_prompt_response(interaction, break_id, back_on_duty=(action == "back"))
            return
        if len(parts) >= 5 and parts[1] == "shift":
            action, scope, shift_id = parts[2], parts[3], int(parts[4])
            if action == "adjust":
                await self._handle_shift_adjust_click(interaction, scope, shift_id)
                return
            if action == "toggle":
                await self._handle_shift_toggle_click(interaction, scope, shift_id)
                return
        if len(parts) >= 4 and parts[1] == "servicelog":
            action, service_log_id = parts[2], int(parts[3])
            if action == "edit":
                await self._handle_service_log_edit_click(interaction, service_log_id)
                return
            if action == "toggle":
                await self._handle_service_log_toggle_click(interaction, service_log_id)
                return
        if len(parts) >= 4 and parts[1] == "req":
            action, request_id = parts[2], int(parts[3])
            await self._handle_request_decision(interaction, request_id, approve=(action == "approve"))
            return
        if len(parts) >= 4 and parts[1] == "admin":
            await self._handle_admin_component(interaction, parts)
            return
        if len(parts) >= 4 and parts[1] == "employee":
            await self._handle_employee_component(interaction, parts)
            return
        if len(parts) >= 4 and parts[1] == "payout":
            await self._handle_payout_component(interaction, parts)
            return
        await interaction.response.send_message("Unknown FAMD action.", ephemeral=True)

    async def _handle_clock_in(self, interaction: discord.Interaction[Any]) -> None:
        if interaction.channel_id != self.runtime.config.discord.clock_in_out_channel_id:
            await interaction.response.send_message("Clock actions only work on the configured ClockInOut card.", ephemeral=True)
            return
        existing = await self.runtime.database.active_shift_for_user(interaction.user.id)
        if existing is not None:
            await interaction.response.send_message("You are already clocked in.", ephemeral=True)
            return
        shift = await self.runtime.database.start_shift(
            user_id=interaction.user.id,
            username=interaction.user.name,
            display_name=_display_name(interaction.user),
            guild_id=interaction.guild_id,
            actor_display_name=_display_name(interaction.user),
        )
        await interaction.response.send_message(
            f"Clocked in at {format_display_dt(shift.clock_in_utc, self.runtime.config.time.timezone)}.",
            ephemeral=True,
        )
        await self.refresh_duty_board()

    async def _handle_clock_out(self, interaction: discord.Interaction[Any]) -> None:
        if interaction.channel_id != self.runtime.config.discord.clock_in_out_channel_id:
            await interaction.response.send_message("Clock actions only work on the configured ClockInOut card.", ephemeral=True)
            return
        active = await self.runtime.database.active_shift_for_user(interaction.user.id)
        if active is None:
            await interaction.response.send_message("You are not clocked in.", ephemeral=True)
            return
        shift = await self.runtime.database.close_shift(
            shift_id=active.id,
            actor_user_id=interaction.user.id,
            actor_display_name=_display_name(interaction.user),
            break_cap_minutes=self.runtime.config.time.break_cap_minutes,
        )
        await interaction.response.send_message("Clocked out. Your shift was logged.", ephemeral=True)
        await self.refresh_duty_board()
        await self._post_shift_cards(shift)

    async def _handle_break_toggle(self, interaction: discord.Interaction[Any]) -> None:
        if interaction.channel_id != self.runtime.config.discord.clock_in_out_channel_id:
            await interaction.response.send_message("Break actions only work on the configured ClockInOut card.", ephemeral=True)
            return
        active = await self.runtime.database.active_shift_for_user(interaction.user.id)
        if active is None:
            await interaction.response.send_message("You must be clocked in to use 10-7 / 10-8.", ephemeral=True)
            return
        if active.current_break_id is None:
            cooldown_minutes = self.runtime.config.time.break_cooldown_minutes
            if cooldown_minutes > 0:
                last_break_end = await self.runtime.database.latest_break_end_for_shift(active.id)
                if last_break_end is not None:
                    ready_at = last_break_end + timedelta(minutes=cooldown_minutes)
                    if utc_now() < ready_at:
                        remaining = max(1, ceil((ready_at - utc_now()).total_seconds() / 60))
                        await interaction.response.send_message(
                            f"You can take another break in {remaining} minute{'s' if remaining != 1 else ''}.",
                            ephemeral=True,
                        )
                        return
            shift_break = await self.runtime.database.start_break(
                shift_id=active.id,
                actor_user_id=interaction.user.id,
                actor_display_name=_display_name(interaction.user),
            )
            await interaction.response.send_message(
                f"Marked 10-7 at {format_display_dt(shift_break.break_start_utc, self.runtime.config.time.timezone)}.",
                ephemeral=True,
            )
        else:
            await self.runtime.database.end_break(
                shift_id=active.id,
                actor_user_id=interaction.user.id,
                actor_display_name=_display_name(interaction.user),
                break_cap_minutes=self.runtime.config.time.break_cap_minutes,
            )
            await interaction.response.send_message("Marked 10-8. You are back on duty.", ephemeral=True)
        await self.refresh_duty_board()

    async def _handle_logging_select(self, interaction: discord.Interaction[Any]) -> None:
        if interaction.channel_id != self.runtime.config.discord.logging_main_channel_id:
            await interaction.response.send_message("Logging actions only work on the configured logging card.", ephemeral=True)
            return
        values = _interaction_values(interaction)
        if not values:
            await interaction.response.send_message("Choose a log type first.", ephemeral=True)
            return
        if values[0] == ServiceLogKind.RESPONSE.value:
            await interaction.response.send_modal(ResponseLogModal(self, responder_default=f"<@{interaction.user.id}>"))
            await self._reset_logging_select_message(interaction)
            return
        if values[0] == ServiceLogKind.VITAL.value:
            await interaction.response.send_modal(VitalLogModal(self, responder_default=f"<@{interaction.user.id}>"))
            await self._reset_logging_select_message(interaction)
            return
        await interaction.response.send_message("Unknown log type.", ephemeral=True)

    async def _reset_logging_select_message(self, interaction: discord.Interaction[Any]) -> None:
        message = interaction.message
        if message is None:
            return
        with contextlib.suppress(discord.HTTPException, AttributeError):
            await message.edit(**self._edit_kwargs(logging_board_payload()))

    async def _after_service_log_submitted(self, interaction: discord.Interaction[Any], log: ServiceLog) -> None:
        thread = await self._get_or_create_service_thread(log)
        if thread is None:
            await interaction.response.send_message("I could not find or create your log thread.", ephemeral=True)
            return
        await self.refresh_service_summary(log.log_kind, log.user_id, log.display_name, thread=thread)
        prompt = await thread.send(
            f"<@{log.user_id}>, send the image proof for log #{log.id} in this thread.",
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        await self.runtime.database.set_service_log_proof_prompt(
            service_log_id=log.id,
            channel_id=prompt.channel.id,
            message_id=prompt.id,
        )
        await interaction.response.send_message(
            f"Log #{log.id} saved. Please send the image proof in {thread.mention}.",
            ephemeral=True,
        )

    async def _handle_pending_service_log_proof(self, message: discord.Message) -> bool:
        if not isinstance(message.channel, discord.Thread):
            return False
        if not message.attachments:
            return False
        log = await self.runtime.database.pending_service_log_for_user_thread(message.author.id, message.channel.id)
        if log is None:
            return False
        image_attachments = [attachment for attachment in message.attachments if _is_image_attachment(attachment)]
        if not image_attachments:
            await message.channel.send("Please send image proof for the pending log.", allowed_mentions=discord.AllowedMentions.none())
            return True
        proof_paths = await self._save_service_log_proofs(log, image_attachments)
        with contextlib.suppress(discord.HTTPException):
            await message.delete()
        await self._delete_service_log_proof_prompt(log)
        log = await self.runtime.database.mark_service_log_proof_received(log.id)
        await self._post_service_log_card(log, message.channel, proof_paths)
        await self.refresh_service_summary(log.log_kind, log.user_id, log.display_name, thread=message.channel)
        return True

    async def _delete_service_log_proof_prompt(self, log: ServiceLog) -> None:
        if not log.proof_prompt_channel_id or not log.proof_prompt_message_id:
            return
        channel = await self._fetch_messageable_channel(int(log.proof_prompt_channel_id))
        if channel is None:
            return
        try:
            prompt = await channel.fetch_message(int(log.proof_prompt_message_id))
            await prompt.delete()
        except (discord.HTTPException, AttributeError, ValueError):
            pass

    async def _save_service_log_proofs(self, log: ServiceLog, attachments: list[discord.Attachment]) -> list[Path]:
        folder = self.runtime.config.storage.proof_dir / log.log_kind.value / str(log.user_id) / str(log.id)
        folder.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        for index, attachment in enumerate(attachments, start=1):
            suffix = Path(attachment.filename).suffix or ".png"
            target = folder / f"proof-{index}{suffix}"
            await attachment.save(target)
            await self.runtime.database.add_service_log_proof(
                service_log_id=log.id,
                local_path=str(target),
                original_filename=attachment.filename,
                content_type=attachment.content_type,
            )
            saved.append(target)
        return saved

    async def _post_service_log_card(self, log: ServiceLog, thread: discord.Thread, proof_paths: list[Path] | None = None) -> None:
        payload = service_log_payload(log, controls=True)
        files = [discord.File(path) for path in proof_paths or [] if path.exists()]
        message = await thread.send(**self._send_kwargs(payload), files=files)
        await self.runtime.database.add_service_log_message_ref(
            service_log_id=log.id,
            ref_type=log.log_kind.value,
            guild_id=message.guild.id if message.guild else log.guild_id,
            channel_id=message.channel.id,
            message_id=message.id,
            thread_id=thread.id,
        )

    async def _handle_service_log_edit_click(self, interaction: discord.Interaction[Any], service_log_id: int) -> None:
        log = await self.runtime.database.get_service_log(service_log_id)
        if log is None:
            await interaction.response.send_message("That log no longer exists.", ephemeral=True)
            return
        is_admin = await self.runtime.database.is_admin(interaction.user.id)
        if not is_admin and str(interaction.user.id) != log.user_id:
            await interaction.response.send_message("Only the original logger or an admin can edit this log.", ephemeral=True)
            return
        if log.log_kind is ServiceLogKind.RESPONSE:
            await interaction.response.send_modal(ResponseLogModal(self, log))
            return
        await interaction.response.send_modal(VitalLogModal(self, log))

    async def _handle_service_log_toggle_click(self, interaction: discord.Interaction[Any], service_log_id: int) -> None:
        log = await self.runtime.database.get_service_log(service_log_id)
        if log is None:
            await interaction.response.send_message("That log no longer exists.", ephemeral=True)
            return
        is_admin = await self.runtime.database.is_admin(interaction.user.id)
        if not is_admin and str(interaction.user.id) != log.user_id:
            await interaction.response.send_message("Only the original logger or an admin can invalidate this log.", ephemeral=True)
            return
        target = ServiceLogStatus.VALID if log.status is ServiceLogStatus.INVALIDATED else ServiceLogStatus.INVALIDATED
        updated = await self.runtime.database.set_service_log_status(
            service_log_id=log.id,
            status=target,
            actor_user_id=interaction.user.id,
            actor_display_name=_display_name(interaction.user),
        )
        await interaction.response.send_message(f"Log #{log.id} marked {target.value}.", ephemeral=True)
        await self.refresh_service_log_messages(updated)

    async def _handle_break_prompt_response(
        self,
        interaction: discord.Interaction[Any],
        break_id: int,
        *,
        back_on_duty: bool,
    ) -> None:
        shift_break = await self.runtime.database.get_break(break_id)
        if shift_break is None or shift_break.break_end_utc is not None:
            await interaction.response.send_message("That break is already closed.", ephemeral=True)
            return
        shift = await self.runtime.database.get_shift(shift_break.shift_id)
        if shift is None or shift.status is not ShiftStatus.ACTIVE:
            await interaction.response.send_message("That shift is no longer active.", ephemeral=True)
            return
        if str(interaction.user.id) != shift.user_id and not await self.runtime.database.is_admin(interaction.user.id):
            await interaction.response.send_message("Only the credited employee or an admin can answer this break prompt.", ephemeral=True)
            return
        if back_on_duty:
            await self.runtime.database.end_break(
                shift_id=shift.id,
                actor_user_id=interaction.user.id,
                actor_display_name=_display_name(interaction.user),
                break_cap_minutes=self.runtime.config.time.break_cap_minutes,
            )
            await interaction.response.send_message("Marked 10-8. You are back on duty.", ephemeral=True)
            await _safe_delete_interaction_message(interaction)
            await self.refresh_duty_board()
            return
        closed = await self.runtime.database.close_shift(
            shift_id=shift.id,
            actor_user_id=interaction.user.id,
            actor_display_name=_display_name(interaction.user),
            break_cap_minutes=self.runtime.config.time.break_cap_minutes,
            auto_clocked_out=True,
        )
        await interaction.response.send_message("Clocked out from break prompt.", ephemeral=True)
        await _safe_delete_interaction_message(interaction)
        await self.refresh_duty_board()
        await self._post_shift_cards(closed)

    async def _monitor_breaks(self) -> None:
        while not self.is_closed():
            try:
                await self._check_open_breaks()
            except Exception:
                LOGGER.exception("break monitor failed")
            await asyncio.sleep(20)

    async def _refresh_duty_board_periodically(self) -> None:
        while not self.is_closed():
            await asyncio.sleep(60)
            try:
                if await self.runtime.database.list_active_shifts():
                    await self.refresh_duty_board()
            except Exception:
                LOGGER.exception("duty board refresh failed")

    async def _monitor_transient_state(self) -> None:
        while not self.is_closed():
            await asyncio.sleep(60)
            self._expire_transient_state()
            try:
                await self._expire_pending_service_logs()
            except Exception:
                LOGGER.exception("pending service log cleanup failed")

    def _expire_transient_state(self) -> None:
        cutoff = utc_now() - timedelta(seconds=TRANSIENT_PANEL_SECONDS)
        self._pending_admin_add = {
            user_id: created_at for user_id, created_at in self._pending_admin_add.items() if created_at > cutoff
        }
        self._pending_employee_add = {
            user_id: created_at for user_id, created_at in self._pending_employee_add.items() if created_at > cutoff
        }
        self._pending_payout_snapshots = {
            token: pending for token, pending in self._pending_payout_snapshots.items() if pending.created_at > cutoff
        }

    async def _expire_pending_service_logs(self) -> None:
        cutoff = utc_now() - timedelta(seconds=TRANSIENT_PANEL_SECONDS)
        stale_logs = await self.runtime.database.pending_service_logs_before(cutoff)
        for log in stale_logs:
            await self._delete_service_log_proof_prompt(log)
            await self.runtime.database.set_service_log_status(
                service_log_id=log.id,
                status=ServiceLogStatus.INVALIDATED,
                actor_user_id="system",
                actor_display_name="System",
            )

    async def _check_open_breaks(self) -> None:
        now = utc_now()
        open_breaks = await self.runtime.database.list_open_breaks()
        for shift_break in open_breaks:
            shift = await self.runtime.database.get_shift(shift_break.shift_id)
            if shift is None or shift.status is not ShiftStatus.ACTIVE:
                continue
            elapsed = now - shift_break.break_start_utc
            warning_at = timedelta(minutes=self.runtime.config.time.break_warning_minutes)
            final_at = timedelta(minutes=self.runtime.config.time.break_cap_minutes)
            grace = timedelta(minutes=self.runtime.config.time.break_final_prompt_grace_minutes)
            if shift_break.warning_sent_at_utc is None and elapsed >= warning_at:
                await self._send_break_warning(shift)
                await self.runtime.database.mark_break_warning_sent(shift_break.id)
            if shift_break.final_prompt_sent_at_utc is None and elapsed >= final_at:
                message = await self._send_break_final_prompt(shift, shift_break.id)
                await self.runtime.database.mark_break_final_prompt_sent(
                    shift_break.id,
                    channel_id=message.channel.id if message else None,
                    message_id=message.id if message else None,
                )
                continue
            if shift_break.final_prompt_sent_at_utc is not None:
                deadline = shift_break.final_prompt_sent_at_utc + grace
                if now >= deadline:
                    await self._auto_clock_out_for_break_timeout(shift, shift_break)

    async def _send_break_warning(self, shift: Shift) -> None:
        channel = await self._fetch_text_channel(self.runtime.config.discord.clock_in_out_channel_id)
        if channel is None:
            return
        await channel.send(
            f"Hey <@{shift.user_id}>, you only have 1 minute left for your break.",
            allowed_mentions=discord.AllowedMentions(users=True),
            delete_after=90,
        )

    async def _send_break_final_prompt(self, shift: Shift, break_id: int) -> discord.Message | None:
        channel = await self._fetch_text_channel(self.runtime.config.discord.clock_in_out_channel_id)
        if channel is None:
            return None
        payload = break_final_prompt_payload(break_id, shift.user_id)
        kwargs = self._send_kwargs(payload)
        kwargs["content"] = f"Hey <@{shift.user_id}>, your 15-minute break is over. Report to duty or clock out."
        kwargs["allowed_mentions"] = discord.AllowedMentions(users=True)
        return await channel.send(**kwargs)

    async def _auto_clock_out_for_break_timeout(self, shift: Shift, shift_break) -> None:
        current = await self.runtime.database.get_shift(shift.id)
        if current is None or current.status is not ShiftStatus.ACTIVE or current.current_break_id != shift_break.id:
            return
        closed = await self.runtime.database.close_shift(
            shift_id=current.id,
            actor_user_id="system",
            actor_display_name="System",
            break_cap_minutes=self.runtime.config.time.break_cap_minutes,
            auto_clocked_out=True,
        )
        if shift_break.final_prompt_message_channel_id and shift_break.final_prompt_message_id:
            channel = await self._fetch_messageable_channel(int(shift_break.final_prompt_message_channel_id))
            if channel is not None:
                try:
                    message = await channel.fetch_message(int(shift_break.final_prompt_message_id))
                    await message.delete()
                except (discord.HTTPException, AttributeError, ValueError):
                    pass
        await self.refresh_duty_board()
        await self._post_shift_cards(closed)

    async def _post_shift_cards(self, shift: Shift) -> None:
        admin_channel = await self._fetch_text_channel(self.runtime.config.discord.admin_logs_channel_id)
        if admin_channel is not None:
            admin_payload = shift_log_payload(shift, self.runtime.config.time.timezone, controls=True)
            message = await admin_channel.send(**self._send_kwargs(admin_payload))
            await self.runtime.database.add_message_ref(
                shift_id=shift.id,
                ref_type="admin",
                guild_id=message.guild.id if message.guild else shift.guild_id,
                channel_id=message.channel.id,
                message_id=message.id,
            )
        thread = await self._get_or_create_user_log_thread(shift)
        if thread is not None:
            await self.refresh_user_log_summary(shift.user_id, shift.display_name, thread=thread)
            user_payload = shift_log_payload(
                shift,
                self.runtime.config.time.timezone,
                controls=self.runtime.config.discord.user_log_controls,
                user_controls=True,
            )
            message = await thread.send(**self._send_kwargs(user_payload))
            await self.runtime.database.add_message_ref(
                shift_id=shift.id,
                ref_type="user",
                guild_id=message.guild.id if message.guild else shift.guild_id,
                channel_id=message.channel.id,
                message_id=message.id,
                thread_id=thread.id,
            )

    async def _get_or_create_user_log_thread(
        self,
        shift: Shift,
    ) -> discord.Thread | None:
        forum = await self._fetch_forum_channel(self.runtime.config.discord.user_logs_forum_channel_id)
        if forum is None:
            LOGGER.warning("user_logs_forum_channel_id is not a forum channel")
            return None
        thread = await self._find_employee_thread(forum, shift.user_id)
        if thread is not None:
            await _ensure_thread_open(thread)
            await self.runtime.database.upsert_user_log_thread(
                user_id=shift.user_id,
                guild_id=thread.guild.id if thread.guild else shift.guild_id,
                forum_channel_id=forum.id,
                thread_id=thread.id,
                thread_name=thread.name,
            )
            return thread

        payload = await self._user_totals_payload(shift.user_id, shift.display_name)
        created = await forum.create_thread(
            name=_employee_thread_name(shift.display_name or shift.username or shift.user_id, shift.user_id),
            **self._send_kwargs(payload),
        )
        await self.runtime.database.upsert_user_log_thread(
            user_id=shift.user_id,
            guild_id=created.thread.guild.id if created.thread.guild else shift.guild_id,
            forum_channel_id=forum.id,
            thread_id=created.thread.id,
            thread_name=created.thread.name,
            summary_message_id=created.message.id,
        )
        return created.thread

    async def _find_employee_thread(self, forum: discord.ForumChannel, user_id: str) -> discord.Thread | None:
        saved = await self.runtime.database.get_user_log_thread(user_id)
        if saved is not None and saved.forum_channel_id == str(forum.id):
            thread = await self._fetch_thread(int(saved.thread_id))
            if thread is not None:
                return thread
        for thread in forum.threads:
            if _thread_matches_user(thread, user_id):
                await self.runtime.database.upsert_user_log_thread(
                    user_id=user_id,
                    guild_id=thread.guild.id if thread.guild else None,
                    forum_channel_id=forum.id,
                    thread_id=thread.id,
                    thread_name=thread.name,
                )
                return thread
        try:
            async for thread in forum.archived_threads(limit=None):
                if _thread_matches_user(thread, user_id):
                    await self.runtime.database.upsert_user_log_thread(
                        user_id=user_id,
                        guild_id=thread.guild.id if thread.guild else None,
                        forum_channel_id=forum.id,
                        thread_id=thread.id,
                        thread_name=thread.name,
                    )
                    return thread
        except discord.HTTPException:
            LOGGER.debug("forum_id=%s archived thread scan skipped", forum.id)
        return None

    async def _get_or_create_service_thread(self, log: ServiceLog) -> discord.Thread | None:
        forum = await self._forum_for_service_kind(log.log_kind)
        if forum is None:
            return None
        thread = await self._find_service_thread(forum, log.log_kind.value, log.user_id)
        if thread is not None:
            await _ensure_thread_open(thread)
            await self.runtime.database.upsert_user_forum_thread(
                thread_type=log.log_kind.value,
                user_id=log.user_id,
                guild_id=thread.guild.id if thread.guild else log.guild_id,
                forum_channel_id=forum.id,
                thread_id=thread.id,
                thread_name=thread.name,
            )
            return thread
        payload = await self._service_totals_payload(log.log_kind, log.user_id, log.display_name)
        created = await forum.create_thread(
            name=_employee_thread_name(log.display_name or log.username or log.user_id, log.user_id),
            **self._send_kwargs(payload),
        )
        await self.runtime.database.upsert_user_forum_thread(
            thread_type=log.log_kind.value,
            user_id=log.user_id,
            guild_id=created.thread.guild.id if created.thread.guild else log.guild_id,
            forum_channel_id=forum.id,
            thread_id=created.thread.id,
            thread_name=created.thread.name,
            summary_message_id=created.message.id,
        )
        return created.thread

    async def _forum_for_service_kind(self, log_kind: ServiceLogKind) -> discord.ForumChannel | None:
        channel_id = (
            self.runtime.config.discord.response_logs_forum_channel_id
            if log_kind is ServiceLogKind.RESPONSE
            else self.runtime.config.discord.vital_logs_forum_channel_id
        )
        return await self._fetch_forum_channel(channel_id)

    async def _find_service_thread(self, forum: discord.ForumChannel, thread_type: str, user_id: str) -> discord.Thread | None:
        saved = await self.runtime.database.get_user_forum_thread(thread_type, user_id)
        if saved is not None and saved.forum_channel_id == str(forum.id):
            thread = await self._fetch_thread(int(saved.thread_id))
            if thread is not None:
                return thread
        for thread in forum.threads:
            if _thread_matches_user(thread, user_id):
                await self.runtime.database.upsert_user_forum_thread(
                    thread_type=thread_type,
                    user_id=user_id,
                    guild_id=thread.guild.id if thread.guild else None,
                    forum_channel_id=forum.id,
                    thread_id=thread.id,
                    thread_name=thread.name,
                )
                return thread
        try:
            async for thread in forum.archived_threads(limit=None):
                if _thread_matches_user(thread, user_id):
                    await self.runtime.database.upsert_user_forum_thread(
                        thread_type=thread_type,
                        user_id=user_id,
                        guild_id=thread.guild.id if thread.guild else None,
                        forum_channel_id=forum.id,
                        thread_id=thread.id,
                        thread_name=thread.name,
                    )
                    return thread
        except discord.HTTPException:
            LOGGER.debug("forum_id=%s archived service thread scan skipped", forum.id)
        return None

    async def _handle_shift_adjust_click(
        self,
        interaction: discord.Interaction[Any],
        scope: str,
        shift_id: int,
    ) -> None:
        shift = await self.runtime.database.get_shift(shift_id)
        if shift is None or shift.clock_out_utc is None:
            await interaction.response.send_message("That shift no longer exists or is not closed.", ephemeral=True)
            return
        is_admin = await self.runtime.database.is_admin(interaction.user.id)
        if scope == "admin" and not is_admin:
            await interaction.response.send_message("Only admins can adjust this card.", ephemeral=True)
            return
        if scope == "user" and not is_admin and str(interaction.user.id) != shift.user_id:
            await interaction.response.send_message("Only the credited employee or an admin can use this.", ephemeral=True)
            return
        await interaction.response.send_modal(ShiftAdjustModal(self, shift, scope, is_admin))

    async def _handle_shift_toggle_click(
        self,
        interaction: discord.Interaction[Any],
        scope: str,
        shift_id: int,
    ) -> None:
        shift = await self.runtime.database.get_shift(shift_id)
        if shift is None:
            await interaction.response.send_message("That shift no longer exists.", ephemeral=True)
            return
        is_admin = await self.runtime.database.is_admin(interaction.user.id)
        if is_admin:
            target = ShiftStatus.VALID if shift.status is ShiftStatus.INVALIDATED else ShiftStatus.INVALIDATED
            updated = await self.runtime.database.set_shift_status(
                shift_id=shift.id,
                status=target,
                actor_user_id=interaction.user.id,
                actor_display_name=_display_name(interaction.user),
            )
            await interaction.response.send_message(f"Shift #{shift.id} marked {target.value}.", ephemeral=True)
            await self.refresh_shift_messages(updated)
            return
        if scope != "user" or str(interaction.user.id) != shift.user_id:
            await interaction.response.send_message("Only admins or the credited employee can use this.", ephemeral=True)
            return
        if shift.status is ShiftStatus.INVALIDATED:
            await interaction.response.send_message("Only admins can validate an invalidated shift.", ephemeral=True)
            return
        await self._create_void_request(interaction, shift)

    async def _create_void_request(self, interaction: discord.Interaction[Any], shift: Shift) -> None:
        existing = await self.runtime.database.pending_request_for_shift(shift.id)
        if existing is not None:
            await interaction.response.send_message("This shift already has a pending request.", ephemeral=True)
            return
        request = await self.runtime.database.create_change_request(
            shift_id=shift.id,
            requester_user_id=interaction.user.id,
            requester_display_name=_display_name(interaction.user),
            request_type=RequestType.VOID,
            requested_clock_in_utc=None,
            requested_clock_out_utc=None,
        )
        await self._post_request_card(request, shift)
        await interaction.response.send_message("Void request sent to admins.", ephemeral=True)

    async def _post_request_card(self, request: ChangeRequest, shift: Shift) -> None:
        admin_channel = await self._fetch_text_channel(self.runtime.config.discord.admin_logs_channel_id)
        if admin_channel is None:
            return
        payload = request_payload(request, shift, self.runtime.config.time.timezone)
        message = await admin_channel.send(**self._send_kwargs(payload))
        await self.runtime.database.set_request_admin_message(request.id, message.channel.id, message.id)

    async def _handle_request_decision(
        self,
        interaction: discord.Interaction[Any],
        request_id: int,
        *,
        approve: bool,
    ) -> None:
        if not await self.runtime.database.is_admin(interaction.user.id):
            await interaction.response.send_message("Only admins can decide shift requests.", ephemeral=True)
            return
        request = await self.runtime.database.get_change_request(request_id)
        if request is None:
            await interaction.response.send_message("Request not found.", ephemeral=True)
            return
        if request.status is not RequestStatus.PENDING:
            await interaction.response.send_message("This request was already decided.", ephemeral=True)
            return
        shift = await self.runtime.database.get_shift(request.shift_id)
        if shift is None:
            await interaction.response.send_message("Shift not found.", ephemeral=True)
            return
        if approve:
            if request.request_type is RequestType.ADJUST:
                if request.requested_clock_in_utc is None or request.requested_clock_out_utc is None:
                    await interaction.response.send_message("Request is missing adjusted times.", ephemeral=True)
                    return
                shift = await self.runtime.database.adjust_shift(
                    shift_id=shift.id,
                    clock_in_utc=request.requested_clock_in_utc,
                    clock_out_utc=request.requested_clock_out_utc,
                    actor_user_id=interaction.user.id,
                    actor_display_name=_display_name(interaction.user),
                    edit_summary=_shift_edit_summary(
                        shift,
                        request.requested_clock_in_utc,
                        request.requested_clock_out_utc,
                        self.runtime.config.time.timezone,
                    ),
                )
            else:
                shift = await self.runtime.database.set_shift_status(
                    shift_id=shift.id,
                    status=ShiftStatus.INVALIDATED,
                    actor_user_id=interaction.user.id,
                    actor_display_name=_display_name(interaction.user),
                )
        decided = await self.runtime.database.decide_request(
            request_id=request.id,
            status=RequestStatus.APPROVED if approve else RequestStatus.DENIED,
            actor_user_id=interaction.user.id,
            actor_display_name=_display_name(interaction.user),
        )
        payload = request_payload(decided, shift, self.runtime.config.time.timezone)
        await interaction.response.edit_message(**self._edit_kwargs(payload))
        await self.refresh_shift_messages(shift)

    async def refresh_shift_messages(self, shift: Shift) -> None:
        refs = await self.runtime.database.message_refs_for_shift(shift.id)
        for ref in refs:
            channel = await self._fetch_messageable_channel(int(ref.channel_id))
            if channel is None:
                continue
            try:
                message = await channel.fetch_message(int(ref.message_id))
            except (discord.HTTPException, AttributeError, ValueError):
                continue
            controls = True
            user_controls = ref.ref_type == "user"
            if user_controls:
                controls = self.runtime.config.discord.user_log_controls
            payload = shift_log_payload(
                shift,
                self.runtime.config.time.timezone,
                controls=controls,
                user_controls=user_controls,
            )
            try:
                await message.edit(**self._edit_kwargs(payload))
            except discord.HTTPException:
                LOGGER.debug("shift_id=%s message_id=%s refresh skipped", shift.id, ref.message_id)
        pending = await self.runtime.database.pending_request_for_shift(shift.id)
        if pending is not None and pending.admin_message_channel_id and pending.admin_message_id:
            channel = await self._fetch_messageable_channel(int(pending.admin_message_channel_id))
            if channel is not None:
                try:
                    message = await channel.fetch_message(int(pending.admin_message_id))
                    await message.edit(**self._edit_kwargs(request_payload(pending, shift, self.runtime.config.time.timezone)))
                except (discord.HTTPException, AttributeError, ValueError):
                    pass
        await self.refresh_user_log_summary(shift.user_id, shift.display_name)

    async def refresh_user_log_summary(
        self,
        user_id: int | str,
        display_name: str | None = None,
        *,
        thread: discord.Thread | None = None,
    ) -> None:
        forum = await self._fetch_forum_channel(self.runtime.config.discord.user_logs_forum_channel_id)
        if forum is None:
            return
        saved = await self.runtime.database.get_user_log_thread(user_id)
        if thread is None:
            thread = await self._find_employee_thread(forum, str(user_id))
        if thread is None:
            return
        name = _summary_display_name(display_name or thread.name, str(user_id))
        payload = await self._user_totals_payload(str(user_id), name)
        message = await self._fetch_user_summary_message(thread, saved)
        if message is None:
            message = await thread.send(**self._send_kwargs(payload))
        else:
            await self.runtime.database.remove_message_ref_by_location(message.channel.id, message.id)
            await message.edit(**self._edit_kwargs(payload))
        await self.runtime.database.upsert_user_log_thread(
            user_id=user_id,
            guild_id=thread.guild.id if thread.guild else None,
            forum_channel_id=forum.id,
            thread_id=thread.id,
            thread_name=thread.name,
            summary_message_id=message.id,
        )

    async def _fetch_user_summary_message(
        self,
        thread: discord.Thread,
        saved: Any,
    ) -> discord.Message | None:
        candidates: list[int] = []
        if saved is not None and saved.summary_message_id is not None:
            candidates.append(int(saved.summary_message_id))
        candidates.append(thread.id)
        for message_id in dict.fromkeys(candidates):
            try:
                return await thread.fetch_message(message_id)
            except (discord.HTTPException, ValueError):
                continue
        return None

    async def _user_totals_payload(self, user_id: int | str, display_name: str) -> ReplyPayload:
        totals = await self.runtime.database.shift_period_totals(
            user_id=user_id,
            timezone_name=self.runtime.config.time.timezone,
            week_start=self.runtime.config.time.week_start,
        )
        return user_totals_payload(_summary_display_name(display_name, str(user_id)), totals)

    async def refresh_service_log_messages(self, log: ServiceLog) -> None:
        refs = await self.runtime.database.service_log_message_refs(log.id)
        for ref in refs:
            channel = await self._fetch_messageable_channel(int(ref.channel_id))
            if channel is None:
                continue
            try:
                message = await channel.fetch_message(int(ref.message_id))
                await message.edit(**self._edit_kwargs(service_log_payload(log, controls=True)))
            except (discord.HTTPException, AttributeError, ValueError):
                continue
        await self.refresh_service_summary(log.log_kind, log.user_id, log.display_name)

    async def refresh_service_summary(
        self,
        log_kind: ServiceLogKind,
        user_id: int | str,
        display_name: str | None = None,
        *,
        thread: discord.Thread | None = None,
    ) -> None:
        forum = await self._forum_for_service_kind(log_kind)
        if forum is None:
            return
        saved = await self.runtime.database.get_user_forum_thread(log_kind.value, user_id)
        if thread is None:
            thread = await self._find_service_thread(forum, log_kind.value, str(user_id))
        if thread is None:
            return
        name = _summary_display_name(display_name or thread.name, str(user_id))
        payload = await self._service_totals_payload(log_kind, str(user_id), name)
        message = await self._fetch_forum_summary_message(thread, saved)
        if message is None:
            message = await thread.send(**self._send_kwargs(payload))
        else:
            await message.edit(**self._edit_kwargs(payload))
        await self.runtime.database.upsert_user_forum_thread(
            thread_type=log_kind.value,
            user_id=user_id,
            guild_id=thread.guild.id if thread.guild else None,
            forum_channel_id=forum.id,
            thread_id=thread.id,
            thread_name=thread.name,
            summary_message_id=message.id,
        )

    async def _service_totals_payload(self, log_kind: ServiceLogKind, user_id: int | str, display_name: str) -> ReplyPayload:
        totals = await self.runtime.database.service_period_totals(
            user_id=user_id,
            log_kind=log_kind,
            timezone_name=self.runtime.config.time.timezone,
            week_start=self.runtime.config.time.week_start,
        )
        return service_totals_payload(_summary_display_name(display_name, str(user_id)), totals)

    async def _fetch_forum_summary_message(self, thread: discord.Thread, saved: Any) -> discord.Message | None:
        candidates: list[int] = []
        if saved is not None and saved.summary_message_id is not None:
            candidates.append(int(saved.summary_message_id))
        candidates.append(thread.id)
        for message_id in dict.fromkeys(candidates):
            try:
                return await thread.fetch_message(message_id)
            except (discord.HTTPException, ValueError):
                continue
        return None

    async def _send_manageadmins_from_message(self, message: discord.Message) -> None:
        if not await self._can_manage_admins(message.author.id, message.channel.id):
            await message.channel.send("You cannot use the admin manager here.", allowed_mentions=discord.AllowedMentions.none())
            return
        payload = admin_manager_payload(await self.runtime.database.list_admins(), message.author.id)
        sent = await message.channel.send(**self._send_kwargs(payload))
        self._delete_later(sent)

    async def _send_manageadmins_from_interaction(self, interaction: discord.Interaction[Any]) -> None:
        if interaction.channel_id is None or not await self._can_manage_admins(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("You cannot use the admin manager here.", ephemeral=True)
            return
        payload = admin_manager_payload(await self.runtime.database.list_admins(), interaction.user.id)
        await interaction.response.send_message(**self._send_kwargs(payload))
        self._delete_later(await interaction.original_response())

    async def _send_stats_from_message(self, message: discord.Message) -> None:
        if not await self._can_use_admin_commands(message.author.id, message.channel.id):
            await message.channel.send("You cannot use admin commands here.", allowed_mentions=discord.AllowedMentions.none())
            return
        payload = combined_stats_payload(await self.runtime.database.list_combined_user_stats())
        await message.channel.send(**self._send_kwargs(payload))

    async def _send_stats_from_interaction(self, interaction: discord.Interaction[Any]) -> None:
        if interaction.channel_id is None or not await self._can_use_admin_commands(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("You cannot use admin commands here.", ephemeral=True)
            return
        payload = combined_stats_payload(await self.runtime.database.list_combined_user_stats())
        await interaction.response.send_message(**self._send_kwargs(payload))

    async def _send_manageemployees_from_message(self, message: discord.Message) -> None:
        if not await self._can_use_admin_commands(message.author.id, message.channel.id):
            await message.channel.send("You cannot use employee management here.", allowed_mentions=discord.AllowedMentions.none())
            return
        payload = employee_manager_payload(await self._employees_for_guild(message.guild), message.author.id)
        sent = await message.channel.send(**self._send_kwargs(payload))
        self._delete_later(sent)

    async def _send_manageemployees_from_interaction(self, interaction: discord.Interaction[Any]) -> None:
        if interaction.channel_id is None or not await self._can_use_admin_commands(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("You cannot use employee management here.", ephemeral=True)
            return
        payload = employee_manager_payload(await self._employees_for_guild(interaction.guild), interaction.user.id)
        await interaction.response.send_message(**self._send_kwargs(payload))
        self._delete_later(await interaction.original_response())

    async def _weeklysnapshot_from_message(self, message: discord.Message, remainder: str) -> None:
        if not await self._can_use_admin_commands(message.author.id, message.channel.id):
            await message.channel.send("You cannot use payout snapshots here.", allowed_mentions=discord.AllowedMentions.none())
            return
        await self._send_payout_preview(message.channel, message.author.id, remainder)

    async def _weeklysnapshot_from_interaction(self, interaction: discord.Interaction[Any], users: str) -> None:
        if interaction.channel_id is None or not await self._can_use_admin_commands(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("You cannot use payout snapshots here.", ephemeral=True)
            return
        payload = await self._payout_preview_for(interaction.user.id, users)
        await interaction.response.send_message(**self._send_kwargs(payload))
        self._delete_later(await interaction.original_response())

    async def _revertsnapshot_from_message(self, message: discord.Message) -> None:
        if not await self._can_use_admin_commands(message.author.id, message.channel.id):
            await message.channel.send("You cannot revert payout snapshots here.", allowed_mentions=discord.AllowedMentions.none())
            return
        snapshots = await self.runtime.database.list_active_payout_snapshots()
        payload = snapshot_revert_select_payload(snapshots, message.author.id, self.runtime.config.time.timezone)
        sent = await message.channel.send(**self._send_kwargs(payload))
        self._delete_later(sent)

    async def _revertsnapshot_from_interaction(self, interaction: discord.Interaction[Any]) -> None:
        if interaction.channel_id is None or not await self._can_use_admin_commands(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("You cannot revert payout snapshots here.", ephemeral=True)
            return
        snapshots = await self.runtime.database.list_active_payout_snapshots()
        payload = snapshot_revert_select_payload(snapshots, interaction.user.id, self.runtime.config.time.timezone)
        await interaction.response.send_message(**self._send_kwargs(payload))
        self._delete_later(await interaction.original_response())

    async def _send_payout_preview(self, channel: discord.abc.Messageable, owner_user_id: int, targets: str) -> None:
        payload = await self._payout_preview_for(owner_user_id, targets)
        sent = await channel.send(**self._send_kwargs(payload))
        self._delete_later(sent)

    async def _payout_preview_for(self, owner_user_id: int, targets: str) -> ReplyPayload:
        user_ids = _extract_ordered_ids(targets)
        snapshot_type = "early" if user_ids else "general"
        token = secrets.token_urlsafe(6)
        pending = PendingPayoutSnapshot(
            owner_user_id=owner_user_id,
            snapshot_type=snapshot_type,
            user_ids=user_ids or None,
            include_skipped=False,
        )
        self._pending_payout_snapshots[token] = pending
        rates = load_payout_rates()
        items = await self.runtime.database.build_payout_preview(
            rates=rates,
            user_ids=pending.user_ids,
            include_skipped=pending.include_skipped,
        )
        unregistered = await self.runtime.database.unregistered_payout_user_ids(pending.user_ids)
        return payout_preview_payload(
            items,
            owner_user_id,
            token,
            snapshot_type=snapshot_type,
            include_skipped=pending.include_skipped,
            unregistered_user_ids=unregistered,
        )

    async def _update_thread_name_from_message(self, message: discord.Message, remainder: str) -> None:
        if not await self._can_use_admin_commands(message.author.id, message.channel.id):
            await message.channel.send("You cannot use admin commands here.", allowed_mentions=discord.AllowedMentions.none())
            return
        member = await _member_from_command_target(message, remainder)
        if member is None:
            await message.channel.send("I could not find that server member.", allowed_mentions=discord.AllowedMentions.none())
            return
        result = await self._sync_user_log_thread_name(member)
        await message.channel.send(result, allowed_mentions=discord.AllowedMentions.none())

    async def _update_thread_name_from_interaction(
        self,
        interaction: discord.Interaction[Any],
        member: discord.Member | None,
    ) -> None:
        if interaction.channel_id is None or not await self._can_use_admin_commands(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("You cannot use admin commands here.", ephemeral=True)
            return
        target = member if member is not None else interaction.user
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("I could not find that server member.", ephemeral=True)
            return
        result = await self._sync_user_log_thread_name(target)
        await interaction.response.send_message(result, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _attach_thread_from_message(self, message: discord.Message, remainder: str) -> None:
        if not await self._can_use_admin_commands(message.author.id, message.channel.id):
            await message.channel.send("You cannot use admin commands here.", allowed_mentions=discord.AllowedMentions.none())
            return
        thread_type, target_text = _thread_type_from_attach_text(remainder)
        member, thread_id = await _attach_command_target(message, target_text)
        if member is None:
            await message.channel.send("I could not find that server member.", allowed_mentions=discord.AllowedMentions.none())
            return
        if thread_id is None:
            await message.channel.send("Provide a thread ID or Discord thread link.", allowed_mentions=discord.AllowedMentions.none())
            return
        result = await self._attach_thread_to_member(thread_type, member, thread_id)
        await message.channel.send(result, allowed_mentions=discord.AllowedMentions.none())

    async def _attach_thread_from_interaction(
        self,
        interaction: discord.Interaction[Any],
        kind: str,
        member: discord.Member,
        thread_reference: str,
    ) -> None:
        if interaction.channel_id is None or not await self._can_use_admin_commands(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("You cannot use admin commands here.", ephemeral=True)
            return
        thread_type = _normalize_thread_type(kind)
        if thread_type is None:
            await interaction.response.send_message("Use `shift`, `response`, or `vital` for the thread kind.", ephemeral=True)
            return
        thread_id = _thread_id_from_reference(thread_reference, member_id=member.id, guild_id=interaction.guild_id)
        if thread_id is None:
            await interaction.response.send_message("Provide a thread ID or Discord thread link.", ephemeral=True)
            return
        result = await self._attach_thread_to_member(thread_type, member, thread_id)
        await interaction.response.send_message(result, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _sync_user_log_thread_name(self, member: discord.Member) -> str:
        forum = await self._fetch_forum_channel(self.runtime.config.discord.user_logs_forum_channel_id)
        if forum is None:
            return "The configured user logs forum channel is unavailable."
        thread = await self._find_employee_thread(forum, str(member.id))
        if thread is None:
            return f"No FAMD log thread exists yet for {member.display_name} ({member.id})."
        await _ensure_thread_open(thread)
        new_name = _employee_thread_name(member.display_name, str(member.id))
        if thread.name != new_name:
            await thread.edit(name=new_name)
        await self.runtime.database.upsert_user_log_thread(
            user_id=member.id,
            guild_id=member.guild.id,
            forum_channel_id=forum.id,
            thread_id=thread.id,
            thread_name=new_name,
        )
        await self.refresh_user_log_summary(member.id, member.display_name, thread=thread)
        return f"Synced log thread name to `{new_name}`."

    async def _attach_thread_to_member(self, thread_type: str, member: discord.Member, thread_id: int) -> str:
        forum = await self._forum_for_thread_type(thread_type)
        if forum is None:
            return "The configured forum channel is unavailable."
        thread = await self._fetch_thread(thread_id)
        if thread is None:
            return "I could not find that thread."
        if thread.parent_id != forum.id:
            return "That thread does not belong to the configured forum."
        await _ensure_thread_open(thread)
        new_name = _employee_thread_name(member.display_name, str(member.id))
        if thread.name != new_name:
            await thread.edit(name=new_name)
        if thread_type == "shift":
            await self.runtime.database.upsert_user_log_thread(
                user_id=member.id,
                guild_id=member.guild.id,
                forum_channel_id=forum.id,
                thread_id=thread.id,
                thread_name=new_name,
            )
            await self.refresh_user_log_summary(member.id, member.display_name, thread=thread)
        else:
            await self.runtime.database.upsert_user_forum_thread(
                thread_type=thread_type,
                user_id=member.id,
                guild_id=member.guild.id,
                forum_channel_id=forum.id,
                thread_id=thread.id,
                thread_name=new_name,
            )
            await self.refresh_service_summary(ServiceLogKind(thread_type), member.id, member.display_name, thread=thread)
        return f"Attached <@{member.id}>'s {thread_type} thread to `{new_name}`."

    async def _forum_for_thread_type(self, thread_type: str) -> discord.ForumChannel | None:
        if thread_type == "shift":
            return await self._fetch_forum_channel(self.runtime.config.discord.user_logs_forum_channel_id)
        return await self._forum_for_service_kind(ServiceLogKind(thread_type))

    async def _employees_for_guild(self, guild: discord.Guild | None) -> list[Employee]:
        employees = await self.runtime.database.list_employees()
        hydrated: list[Employee] = []
        for employee in employees:
            display_name = await self._employee_display_name(employee.user_id, guild, employee.display_name)
            hydrated.append(Employee(employee.user_id, display_name, employee.rank, employee.active))
        return hydrated

    async def _employee_display_name(
        self,
        user_id: int | str,
        guild: discord.Guild | None,
        fallback: str | None = None,
    ) -> str:
        user_key = str(user_id)
        if guild is not None:
            member = guild.get_member(int(user_key))
            if member is None:
                try:
                    member = await guild.fetch_member(int(user_key))
                except discord.HTTPException:
                    member = None
            if member is not None:
                return _display_name(member)
        stored = await self.runtime.database.best_display_name_for_user(user_key)
        if stored:
            return _summary_display_name(stored, user_key)
        if fallback and fallback != user_key:
            return _summary_display_name(fallback, user_key)
        return "Unknown user"

    async def _can_use_admin_commands(self, user_id: int, channel_id: int) -> bool:
        return (
            channel_id == self.runtime.config.discord.admin_commands_channel_id
            and await self.runtime.database.is_admin(user_id)
        )

    async def _can_manage_admins(self, user_id: int, channel_id: int) -> bool:
        return (
            channel_id == self.runtime.config.discord.admin_commands_channel_id
            and user_id in self.runtime.config.discord.admin_manager_user_ids
        )

    async def _handle_admin_component(self, interaction: discord.Interaction[Any], parts: list[str]) -> None:
        action = parts[2]
        owner_id = int(parts[3])
        if interaction.user.id != owner_id:
            await interaction.response.send_message("This admin panel belongs to another manager.", ephemeral=True)
            return
        if interaction.channel_id is None or not await self._can_manage_admins(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("You cannot manage admins here.", ephemeral=True)
            return
        if action == "add":
            self._pending_admin_add[interaction.user.id] = utc_now()
            await interaction.response.send_message("Mention users or paste Discord IDs to add as admins.", ephemeral=True)
            return
        if action == "remove":
            admins = await self.runtime.database.list_admins()
            await interaction.response.edit_message(**self._edit_kwargs(admin_remove_select_payload(admins, owner_id)))
            return
        if action == "remove_select":
            values = _interaction_values(interaction)
            if not values:
                await interaction.response.send_message("No admin selected.", ephemeral=True)
                return
            await interaction.response.edit_message(**self._edit_kwargs(admin_remove_confirm_payload(values[0], owner_id)))
            return
        if action == "remove_confirm" and len(parts) >= 5:
            target_user_id = parts[4]
            await self.runtime.database.remove_admin(
                target_user_id,
                actor_user_id=interaction.user.id,
                actor_display_name=_display_name(interaction.user),
            )
            payload = admin_manager_payload(await self.runtime.database.list_admins(), owner_id)
            await interaction.response.edit_message(**self._edit_kwargs(payload))
            return
        if action == "remove_cancel":
            payload = admin_manager_payload(await self.runtime.database.list_admins(), owner_id)
            await interaction.response.edit_message(**self._edit_kwargs(payload))

    async def _handle_employee_component(self, interaction: discord.Interaction[Any], parts: list[str]) -> None:
        action = parts[2]
        owner_id = int(parts[3])
        if interaction.user.id != owner_id:
            await interaction.response.send_message("This employee panel belongs to another admin.", ephemeral=True)
            return
        if interaction.channel_id is None or not await self._can_use_admin_commands(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("You cannot manage employees here.", ephemeral=True)
            return
        rates = load_payout_rates()
        if action == "add":
            self._pending_employee_add[interaction.user.id] = utc_now()
            await interaction.response.send_message("Mention one employee or paste their Discord ID to add them.", ephemeral=True)
            return
        if action == "remove":
            employees = await self._employees_for_guild(interaction.guild)
            await interaction.response.edit_message(**self._edit_kwargs(employee_select_payload(employees, owner_id, "remove_select")))
            return
        if action == "manage":
            employees = await self._employees_for_guild(interaction.guild)
            await interaction.response.edit_message(**self._edit_kwargs(employee_select_payload(employees, owner_id, "manage_select")))
            return
        if action == "remove_select":
            values = _interaction_values(interaction)
            if not values:
                await interaction.response.send_message("No employee selected.", ephemeral=True)
                return
            await interaction.response.edit_message(**self._edit_kwargs(employee_remove_confirm_payload(values[0], owner_id)))
            return
        if action == "manage_select":
            values = _interaction_values(interaction)
            if not values:
                await interaction.response.send_message("No employee selected.", ephemeral=True)
                return
            employee = await self.runtime.database.get_employee(values[0])
            display = await self._employee_display_name(
                values[0],
                interaction.guild,
                employee.display_name if employee is not None else values[0],
            )
            await interaction.response.edit_message(
                **self._edit_kwargs(
                    employee_rank_select_payload(rates.ranks, owner_id, values[0], mode="manage", display_name=display)
                )
            )
            return
        if action == "rank" and len(parts) >= 6:
            mode = parts[4]
            target_user_id = parts[5]
            values = _interaction_values(interaction)
            if not values:
                await interaction.response.send_message("No rank selected.", ephemeral=True)
                return
            rank = values[0]
            display = await self._employee_display_name(target_user_id, interaction.guild, target_user_id)
            if mode == "add":
                await self.runtime.database.upsert_employee(
                    user_id=target_user_id,
                    display_name=display,
                    rank=rank,
                    actor_user_id=interaction.user.id,
                    actor_display_name=_display_name(interaction.user),
                )
            else:
                await self.runtime.database.update_employee_rank(
                    target_user_id,
                    rank,
                    actor_user_id=interaction.user.id,
                    actor_display_name=_display_name(interaction.user),
                )
            payload = employee_manager_payload(await self._employees_for_guild(interaction.guild), owner_id)
            await interaction.response.edit_message(**self._edit_kwargs(payload))
            return
        if action == "remove_confirm" and len(parts) >= 5:
            target_user_id = parts[4]
            await self.runtime.database.remove_employee(
                target_user_id,
                actor_user_id=interaction.user.id,
                actor_display_name=_display_name(interaction.user),
            )
            payload = employee_manager_payload(await self._employees_for_guild(interaction.guild), owner_id)
            await interaction.response.edit_message(**self._edit_kwargs(payload))
            return
        if action == "cancel":
            payload = employee_manager_payload(await self._employees_for_guild(interaction.guild), owner_id)
            await interaction.response.edit_message(**self._edit_kwargs(payload))

    async def _handle_payout_component(self, interaction: discord.Interaction[Any], parts: list[str]) -> None:
        action = parts[2]
        owner_id = int(parts[3])
        if interaction.user.id != owner_id:
            await interaction.response.send_message("This payout panel belongs to another admin.", ephemeral=True)
            return
        if interaction.channel_id is None or not await self._can_use_admin_commands(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("You cannot manage payouts here.", ephemeral=True)
            return
        if action in {"confirm", "cancel", "toggle_skipped"} and len(parts) >= 5:
            token = parts[4]
            pending = self._pending_payout_snapshots.get(token)
            if pending is None:
                await interaction.response.send_message("That payout preview expired.", ephemeral=True)
                return
            if action == "cancel":
                self._pending_payout_snapshots.pop(token, None)
                await interaction.response.edit_message(content=None, embed=discord.Embed(title="FAMD Payout Snapshot", description="Cancelled.", color=0x4B6BFB), view=None)
                return
            if action == "toggle_skipped":
                pending.include_skipped = not pending.include_skipped
                rates = load_payout_rates()
                items = await self.runtime.database.build_payout_preview(
                    rates=rates,
                    user_ids=pending.user_ids,
                    include_skipped=pending.include_skipped,
                )
                unregistered = await self.runtime.database.unregistered_payout_user_ids(pending.user_ids)
                payload = payout_preview_payload(
                    items,
                    owner_id,
                    token,
                    snapshot_type=pending.snapshot_type,
                    include_skipped=pending.include_skipped,
                    unregistered_user_ids=unregistered,
                )
                await interaction.response.edit_message(**self._edit_kwargs(payload))
                return
            rates = load_payout_rates()
            unregistered = await self.runtime.database.unregistered_payout_user_ids(pending.user_ids)
            if unregistered:
                items = await self.runtime.database.build_payout_preview(
                    rates=rates,
                    user_ids=pending.user_ids,
                    include_skipped=pending.include_skipped,
                )
                payload = payout_preview_payload(
                    items,
                    owner_id,
                    token,
                    snapshot_type=pending.snapshot_type,
                    include_skipped=pending.include_skipped,
                    unregistered_user_ids=unregistered,
                )
                await interaction.response.edit_message(**self._edit_kwargs(payload))
                return
            snapshot, items = await self.runtime.database.create_payout_snapshot(
                snapshot_type=pending.snapshot_type,
                rates=rates,
                user_ids=pending.user_ids,
                include_skipped=pending.include_skipped,
                actor_user_id=interaction.user.id,
                actor_display_name=_display_name(interaction.user),
            )
            self._pending_payout_snapshots.pop(token, None)
            await interaction.response.edit_message(
                content=None,
                embed=discord.Embed(
                    title="FAMD Payout Snapshot",
                    description=f"Snapshot #{snapshot.id} committed.",
                    color=0x2BA17E,
                ),
                view=None,
            )
            if interaction.channel is not None:
                await interaction.channel.send(**self._send_kwargs(weekly_logs_payload(items)))
                await interaction.channel.send(**self._send_kwargs(doctors_payouts_payload(items)))
            await self._send_snapshot_cutoff_messages(items)
            return
        if action == "revert_select":
            values = _interaction_values(interaction)
            if not values:
                await interaction.response.send_message("No snapshot selected.", ephemeral=True)
                return
            snapshot = await self.runtime.database.get_payout_snapshot(int(values[0]))
            if snapshot is None:
                await interaction.response.send_message("Snapshot not found.", ephemeral=True)
                return
            await interaction.response.edit_message(
                **self._edit_kwargs(snapshot_revert_confirm_payload(snapshot, owner_id, self.runtime.config.time.timezone))
            )
            return
        if action == "revert_confirm" and len(parts) >= 5:
            snapshot_id = int(parts[4])
            await self.runtime.database.revert_payout_snapshot(
                snapshot_id,
                actor_user_id=interaction.user.id,
                actor_display_name=_display_name(interaction.user),
            )
            await interaction.response.edit_message(
                content=None,
                embed=discord.Embed(
                    title="FAMD Snapshot Revert",
                    description=f"Snapshot #{snapshot_id} reverted. Its logs are eligible for payout again.",
                    color=0xD97706,
                ),
                view=None,
            )
            return
        if action == "revert_cancel":
            await interaction.response.edit_message(content=None, embed=discord.Embed(title="FAMD Snapshot Revert", description="Cancelled.", color=0x4B6BFB), view=None)

    async def _handle_pending_admin_add(self, message: discord.Message) -> bool:
        if message.author.id not in self._pending_admin_add:
            return False
        if not await self._can_manage_admins(message.author.id, message.channel.id):
            return False
        self._pending_admin_add.pop(message.author.id, None)
        user_ids = _extract_user_ids(message.content)
        user_ids.update(str(user.id) for user in message.mentions)
        if not user_ids:
            await message.channel.send("No Discord IDs found.", allowed_mentions=discord.AllowedMentions.none())
            return True
        for user_id in sorted(user_ids):
            member = message.guild.get_member(int(user_id)) if message.guild else None
            display = _display_name(member) if member is not None else user_id
            await self.runtime.database.add_admin(
                user_id,
                display,
                actor_user_id=message.author.id,
                actor_display_name=_display_name(message.author),
            )
        payload = admin_manager_payload(await self.runtime.database.list_admins(), message.author.id)
        sent = await message.channel.send(**self._send_kwargs(payload))
        self._delete_later(sent)
        return True

    async def _handle_pending_employee_add(self, message: discord.Message) -> bool:
        if message.author.id not in self._pending_employee_add:
            return False
        if not await self._can_use_admin_commands(message.author.id, message.channel.id):
            return False
        self._pending_employee_add.pop(message.author.id, None)
        user_ids = _extract_ordered_ids(message.content)
        if message.mentions:
            mentioned = [str(user.id) for user in message.mentions]
            user_ids = list(dict.fromkeys(mentioned + user_ids))
        if not user_ids:
            await message.channel.send("No Discord ID found.", allowed_mentions=discord.AllowedMentions.none())
            return True
        target_user_id = user_ids[0]
        display = await self._employee_display_name(target_user_id, message.guild, target_user_id)
        payload = employee_rank_select_payload(
            load_payout_rates().ranks,
            message.author.id,
            target_user_id,
            mode="add",
            display_name=display,
        )
        sent = await message.channel.send(**self._send_kwargs(payload))
        self._delete_later(sent)
        return True

    async def _send_snapshot_cutoff_messages(self, items: list[Any]) -> None:
        for item in items:
            if item.skipped:
                continue
            if item.duty_minutes:
                saved = await self.runtime.database.get_user_log_thread(item.user_id)
                await self._send_cutoff_to_thread(saved, cutoff_message("DUTY HOURS", format_duration(item.duty_minutes)))
            if item.response_count:
                saved = await self.runtime.database.get_user_forum_thread(ServiceLogKind.RESPONSE.value, item.user_id)
                await self._send_cutoff_to_thread(saved, cutoff_message("RESPONSE LOGS", str(item.response_count)))
            if item.vital_count:
                saved = await self.runtime.database.get_user_forum_thread(ServiceLogKind.VITAL.value, item.user_id)
                await self._send_cutoff_to_thread(saved, cutoff_message("VITAL LOGS", str(item.vital_count)))

    async def _send_cutoff_to_thread(self, saved: Any, content: str) -> None:
        if saved is None:
            return
        thread = await self._fetch_thread(int(saved.thread_id))
        if thread is None:
            return
        await _ensure_thread_open(thread)
        await thread.send(content, allowed_mentions=discord.AllowedMentions.none())

    def _delete_later(self, message: discord.Message | None, delay: int = TRANSIENT_PANEL_SECONDS) -> None:
        if message is None:
            return
        asyncio.create_task(_delete_message_after(message, delay))

    async def _fetch_text_channel(self, channel_id: int) -> discord.TextChannel | None:
        channel = self.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        try:
            fetched = await self.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
        return fetched if isinstance(fetched, discord.TextChannel) else None

    async def _fetch_forum_channel(self, channel_id: int) -> discord.ForumChannel | None:
        channel = self.get_channel(channel_id)
        if isinstance(channel, discord.ForumChannel):
            return channel
        try:
            fetched = await self.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
        return fetched if isinstance(fetched, discord.ForumChannel) else None

    async def _fetch_messageable_channel(self, channel_id: int) -> discord.TextChannel | discord.Thread | None:
        channel = self.get_channel(channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        try:
            fetched = await self.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
        return fetched if isinstance(fetched, (discord.TextChannel, discord.Thread)) else None

    async def _fetch_thread(self, thread_id: int) -> discord.Thread | None:
        channel = self.get_channel(thread_id)
        if isinstance(channel, discord.Thread):
            return channel
        try:
            fetched = await self.fetch_channel(thread_id)
        except discord.HTTPException:
            return None
        return fetched if isinstance(fetched, discord.Thread) else None

    def _send_kwargs(self, payload: ReplyPayload) -> dict[str, Any]:
        return {
            "content": payload.content,
            "embed": _discord_embed(payload),
            "view": _discord_view(payload.view),
            "allowed_mentions": discord.AllowedMentions.none() if payload.silent_mentions else None,
        }

    def _edit_kwargs(self, payload: ReplyPayload) -> dict[str, Any]:
        return {
            "content": payload.content,
            "embed": _discord_embed(payload),
            "view": _discord_view(payload.view),
            "allowed_mentions": discord.AllowedMentions.none() if payload.silent_mentions else None,
        }


class ShiftAdjustModal(discord.ui.Modal, title="Adjust shift time"):
    time_in = discord.ui.TextInput(label="Time in", custom_id="time_in", required=True)
    time_out = discord.ui.TextInput(label="Time out", custom_id="time_out", required=True)

    def __init__(self, client: FAMDDiscordClient, shift: Shift, scope: str, actor_is_admin: bool) -> None:
        super().__init__(timeout=300)
        self._client = client
        self._shift_id = shift.id
        self._scope = scope
        self._actor_is_admin = actor_is_admin
        timezone_name = client.runtime.config.time.timezone
        self.time_in.default = format_display_dt(shift.clock_in_utc, timezone_name)
        self.time_out.default = format_display_dt(shift.clock_out_utc, timezone_name)

    async def on_submit(self, interaction: discord.Interaction[Any]) -> None:
        timezone_name = self._client.runtime.config.time.timezone
        try:
            clock_in = parse_user_datetime(str(self.time_in), timezone_name)
            clock_out = parse_user_datetime(str(self.time_out), timezone_name)
            if clock_out < clock_in:
                raise ValueError("Clock-out cannot be earlier than time-in.")
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        shift = await self._client.runtime.database.get_shift(self._shift_id)
        if shift is None:
            await interaction.response.send_message("Shift not found.", ephemeral=True)
            return
        if self._actor_is_admin:
            updated = await self._client.runtime.database.adjust_shift(
                shift_id=shift.id,
                clock_in_utc=clock_in,
                clock_out_utc=clock_out,
                actor_user_id=interaction.user.id,
                actor_display_name=_display_name(interaction.user),
                edit_summary=_shift_edit_summary(shift, clock_in, clock_out, timezone_name),
            )
            await interaction.response.send_message("Shift time adjusted.", ephemeral=True)
            await self._client.refresh_shift_messages(updated)
            return
        if self._scope != "user" or str(interaction.user.id) != shift.user_id:
            await interaction.response.send_message("Only admins or the credited employee can adjust this.", ephemeral=True)
            return
        existing = await self._client.runtime.database.pending_request_for_shift(shift.id)
        if existing is not None:
            await interaction.response.send_message("This shift already has a pending request.", ephemeral=True)
            return
        request = await self._client.runtime.database.create_change_request(
            shift_id=shift.id,
            requester_user_id=interaction.user.id,
            requester_display_name=_display_name(interaction.user),
            request_type=RequestType.ADJUST,
            requested_clock_in_utc=clock_in,
            requested_clock_out_utc=clock_out,
        )
        await self._client._post_request_card(request, shift)
        await interaction.response.send_message("Adjustment request sent to admins.", ephemeral=True)


class ResponseLogModal(discord.ui.Modal, title="Response Log"):
    response_type = discord.ui.TextInput(label="Type", custom_id="type", required=True, placeholder="DISTRESS or ROBBERY")
    postal = discord.ui.TextInput(label="Postal", custom_id="postal", required=True)
    responders = discord.ui.TextInput(label="Responder/s", custom_id="responders", required=True)

    def __init__(self, client: FAMDDiscordClient, log: ServiceLog | None = None, responder_default: str = "") -> None:
        super().__init__(timeout=300)
        self._client = client
        self._log_id = log.id if log is not None else None
        if log is not None:
            self.response_type.default = log.response_type or ""
            self.postal.default = log.postal
            self.responders.default = log.responders_text
        elif responder_default:
            self.responders.default = responder_default

    async def on_submit(self, interaction: discord.Interaction[Any]) -> None:
        try:
            response_type = _parse_response_type(str(self.response_type))
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        postal = str(self.postal).strip()
        responders = str(self.responders).strip()
        if self._log_id is None:
            log = await self._client.runtime.database.create_service_log(
                log_kind=ServiceLogKind.RESPONSE,
                user_id=interaction.user.id,
                username=interaction.user.name,
                display_name=_display_name(interaction.user),
                guild_id=interaction.guild_id,
                postal=postal,
                response_type=response_type,
                responders_text=responders,
                response_count=1,
                treatment_count=0,
                revival_count=0,
                bodybag_count=0,
            )
            await self._client._after_service_log_submitted(interaction, log)
            return
        existing = await self._client.runtime.database.get_service_log(self._log_id)
        if existing is None:
            await interaction.response.send_message("Log not found.", ephemeral=True)
            return
        updated = await self._client.runtime.database.update_service_log_details(
            service_log_id=existing.id,
            postal=postal,
            response_type=response_type,
            responders_text=responders,
            response_count=1,
            treatment_count=0,
            revival_count=0,
            bodybag_count=0,
            actor_user_id=interaction.user.id,
            actor_display_name=_display_name(interaction.user),
            edit_summary=_service_edit_summary(existing, postal, response_type, responders, 1, 0, 0, 0),
        )
        await interaction.response.send_message("Response log updated.", ephemeral=True)
        await self._client.refresh_service_log_messages(updated)


class VitalLogModal(discord.ui.Modal, title="Vital Log"):
    postal = discord.ui.TextInput(label="Postal", custom_id="postal", required=True)
    specify = discord.ui.TextInput(
        label="Specify",
        custom_id="specify",
        required=True,
        placeholder="1x treatment, 2x revival, 2x bodybag",
    )
    responders = discord.ui.TextInput(label="Responder/s", custom_id="responders", required=True)

    def __init__(self, client: FAMDDiscordClient, log: ServiceLog | None = None, responder_default: str = "") -> None:
        super().__init__(timeout=300)
        self._client = client
        self._log_id = log.id if log is not None else None
        if log is not None:
            self.postal.default = log.postal
            self.specify.default = _vital_counts_to_text(log.treatment_count, log.revival_count, log.bodybag_count)
            self.responders.default = log.responders_text
        elif responder_default:
            self.responders.default = responder_default

    async def on_submit(self, interaction: discord.Interaction[Any]) -> None:
        try:
            treatment, revival, bodybag = _parse_vital_counts(str(self.specify))
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        postal = str(self.postal).strip()
        responders = str(self.responders).strip()
        if self._log_id is None:
            log = await self._client.runtime.database.create_service_log(
                log_kind=ServiceLogKind.VITAL,
                user_id=interaction.user.id,
                username=interaction.user.name,
                display_name=_display_name(interaction.user),
                guild_id=interaction.guild_id,
                postal=postal,
                response_type=None,
                responders_text=responders,
                response_count=0,
                treatment_count=treatment,
                revival_count=revival,
                bodybag_count=bodybag,
            )
            await self._client._after_service_log_submitted(interaction, log)
            return
        existing = await self._client.runtime.database.get_service_log(self._log_id)
        if existing is None:
            await interaction.response.send_message("Log not found.", ephemeral=True)
            return
        updated = await self._client.runtime.database.update_service_log_details(
            service_log_id=existing.id,
            postal=postal,
            response_type=None,
            responders_text=responders,
            response_count=0,
            treatment_count=treatment,
            revival_count=revival,
            bodybag_count=bodybag,
            actor_user_id=interaction.user.id,
            actor_display_name=_display_name(interaction.user),
            edit_summary=_service_edit_summary(existing, postal, None, responders, 0, treatment, revival, bodybag),
        )
        await interaction.response.send_message("Vital log updated.", ephemeral=True)
        await self._client.refresh_service_log_messages(updated)


class PassiveButton(discord.ui.Button[Any]):
    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        return None


class PassiveSelect(discord.ui.Select[Any]):
    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        return None


def _discord_view(payload: ViewPayload | None) -> discord.ui.View | None:
    if payload is None:
        return None
    view = discord.ui.View(timeout=None)
    for button in payload.buttons:
        view.add_item(
            PassiveButton(
                label=button.label,
                custom_id=button.custom_id,
                style=_button_style(button.style),
                disabled=button.disabled,
            )
        )
    if payload.select is not None:
        view.add_item(
            PassiveSelect(
                custom_id=payload.select.custom_id,
                placeholder=payload.select.placeholder,
                options=[
                    discord.SelectOption(label=option.label, value=option.value, description=option.description)
                    for option in payload.select.options
                ],
                min_values=1,
                max_values=1,
            )
        )
    return view


def _discord_embed(payload: ReplyPayload) -> discord.Embed:
    embed = discord.Embed(title=payload.embed.title, description=payload.embed.description, color=payload.embed.color)
    for field in payload.embed.fields:
        embed.add_field(name=field.name, value=field.value, inline=field.inline)
    if payload.embed.footer is not None:
        embed.set_footer(text=payload.embed.footer.text)
    return embed


def _button_style(style: int) -> discord.ButtonStyle:
    if style == BUTTON_STYLE_PRIMARY:
        return discord.ButtonStyle.primary
    if style == BUTTON_STYLE_SUCCESS:
        return discord.ButtonStyle.success
    if style == BUTTON_STYLE_DANGER:
        return discord.ButtonStyle.danger
    if style == BUTTON_STYLE_SECONDARY:
        return discord.ButtonStyle.secondary
    return discord.ButtonStyle.secondary


def _display_name(user: Any) -> str:
    return str(getattr(user, "display_name", None) or getattr(user, "global_name", None) or getattr(user, "name", user))


def _interaction_custom_id(interaction: discord.Interaction[Any]) -> str:
    data = interaction.data if isinstance(interaction.data, dict) else {}
    return str(data.get("custom_id") or "")


def _interaction_values(interaction: discord.Interaction[Any]) -> list[str]:
    data = interaction.data if isinstance(interaction.data, dict) else {}
    values = data.get("values")
    return [str(value) for value in values] if isinstance(values, list) else []


async def _safe_interaction_message(interaction: discord.Interaction[Any], content: str, *, ephemeral: bool) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content, ephemeral=ephemeral)


async def _safe_delete_interaction_message(interaction: discord.Interaction[Any]) -> None:
    message = interaction.message
    if message is None:
        return
    try:
        await message.delete()
    except (discord.HTTPException, discord.NotFound, AttributeError):
        LOGGER.debug("message_id=%s break prompt delete skipped", getattr(message, "id", None))


async def _delete_message_after(message: discord.Message, delay: int) -> None:
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.HTTPException, discord.NotFound, AttributeError):
        LOGGER.debug("message_id=%s transient delete skipped", getattr(message, "id", None))


async def _ensure_thread_open(thread: discord.Thread) -> None:
    if not thread.archived and not thread.locked:
        return
    kwargs: dict[str, Any] = {}
    if thread.archived:
        kwargs["archived"] = False
    if thread.locked:
        kwargs["locked"] = False
    if kwargs:
        await thread.edit(**kwargs)


async def _member_from_command_target(message: discord.Message, remainder: str) -> discord.Member | None:
    if message.guild is None:
        return message.author if isinstance(message.author, discord.Member) else None
    if message.mentions:
        return message.mentions[0]
    user_ids = _extract_ordered_ids(remainder)
    if not user_ids:
        return message.author if isinstance(message.author, discord.Member) else None
    user_id = int(user_ids[0])
    member = message.guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await message.guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


async def _attach_command_target(message: discord.Message, remainder: str) -> tuple[discord.Member | None, int | None]:
    member = await _member_from_command_target(message, remainder)
    if member is None:
        return None, None
    thread_id = _thread_id_from_reference(remainder, member_id=member.id, guild_id=message.guild.id if message.guild else None)
    if thread_id is None and isinstance(message.channel, discord.Thread):
        thread_id = message.channel.id
    return member, thread_id


def _thread_type_from_attach_text(remainder: str) -> tuple[str, str]:
    stripped = remainder.strip()
    if not stripped:
        return "shift", stripped
    first, _, rest = stripped.partition(" ")
    thread_type = _normalize_thread_type(first)
    if thread_type is None:
        return "shift", stripped
    return thread_type, rest


def _normalize_thread_type(value: str) -> str | None:
    normalized = value.strip().lower()
    aliases = {
        "shift": "shift",
        "shifts": "shift",
        "response": "response",
        "responses": "response",
        "robbery": "response",
        "distress": "response",
        "vital": "vital",
        "vitals": "vital",
    }
    return aliases.get(normalized)


def _thread_id_from_reference(value: str, *, member_id: int | str | None, guild_id: int | str | None) -> int | None:
    skipped = {str(candidate) for candidate in (member_id, guild_id) if candidate is not None}
    for candidate in _extract_ordered_ids(value):
        if candidate not in skipped:
            return int(candidate)
    return None


def _employee_thread_name(display_name: str, user_id: str) -> str:
    suffix = f"[{user_id}]"
    cleaned = " ".join(display_name.replace("[", "(").replace("]", ")").split()) or "Employee"
    base = f"{cleaned} "
    max_base = max(0, 100 - len(suffix) - 1)
    return f"{base[:max_base].rstrip()} {suffix}".strip()


def _summary_display_name(display_name: str, user_id: str) -> str:
    cleaned = " ".join(display_name.replace("[", "(").replace("]", ")").split()) or "Employee"
    if cleaned.startswith(LEGACY_EMPLOYEE_THREAD_PREFIX):
        cleaned = cleaned[len(LEGACY_EMPLOYEE_THREAD_PREFIX) :].strip()
    suffix = f"({user_id})"
    bracket_suffix = f"[{user_id}]"
    if cleaned.endswith(bracket_suffix):
        cleaned = cleaned[: -len(bracket_suffix)].strip()
    if cleaned.endswith(suffix):
        cleaned = cleaned[: -len(suffix)].strip()
    return cleaned or "Employee"


def _thread_matches_user(thread: discord.Thread, user_id: str) -> bool:
    return thread.name.endswith(f"[{user_id}]") or (
        thread.name.startswith(LEGACY_EMPLOYEE_THREAD_PREFIX) and thread.name.endswith(f"[{user_id}]")
    )


def _shift_edit_summary(shift: Shift, clock_in_utc: datetime, clock_out_utc: datetime, timezone_name: str) -> str:
    changes: list[str] = []
    if shift.clock_in_utc != clock_in_utc:
        changes.append(
            "Changed time in from "
            f"{format_display_dt(shift.clock_in_utc, timezone_name)} to {format_display_dt(clock_in_utc, timezone_name)}."
        )
    if shift.clock_out_utc != clock_out_utc:
        changes.append(
            "Changed time out from "
            f"{format_display_dt(shift.clock_out_utc, timezone_name)} to {format_display_dt(clock_out_utc, timezone_name)}."
        )
    return "\n".join(changes) or "Reviewed shift times; no visible time fields changed."


def _parse_response_type(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in {"DISTRESS", "ROBBERY"}:
        raise ValueError("Type must be `DISTRESS` or `ROBBERY`.")
    return normalized


def _parse_vital_counts(value: str) -> tuple[int, int, int]:
    counts = {"treatment": 0, "revival": 0, "bodybag": 0}
    aliases = {
        "treatment": "treatment",
        "treatments": "treatment",
        "treat": "treatment",
        "revival": "revival",
        "revivals": "revival",
        "revive": "revival",
        "bodybag": "bodybag",
        "bodybags": "bodybag",
        "bag": "bodybag",
    }
    pattern = re.compile(
        r"(?:(?P<count_first>\d+)\s*x?\s*(?P<label_first>[a-zA-Z]+)|(?P<label_second>[a-zA-Z]+)\s*(?P<count_second>\d+))"
    )
    for match in pattern.finditer(value.lower()):
        raw_label = match.group("label_first") or match.group("label_second") or ""
        label = aliases.get(raw_label)
        if label is None:
            continue
        raw_count = match.group("count_first") or match.group("count_second") or "0"
        counts[label] += int(raw_count)
    if not any(counts.values()):
        raise ValueError(
            "Could not read the vitals. Use a format like `1x Treatment, 2x Revival, 2x Bodybag`."
        )
    return counts["treatment"], counts["revival"], counts["bodybag"]


def _vital_counts_to_text(treatment: int, revival: int, bodybag: int) -> str:
    parts = []
    if treatment:
        parts.append(f"{treatment}x Treatment")
    if revival:
        parts.append(f"{revival}x Revival")
    if bodybag:
        parts.append(f"{bodybag}x Bodybag")
    return ", ".join(parts) or "0x Treatment"


def _service_edit_summary(
    log: ServiceLog,
    postal: str,
    response_type: str | None,
    responders: str,
    response_count: int,
    treatment_count: int,
    revival_count: int,
    bodybag_count: int,
) -> str:
    changes: list[str] = []
    if log.postal != postal:
        changes.append(f"Changed postal from {log.postal} to {postal}.")
    if log.response_type != response_type:
        changes.append(f"Changed type from {log.response_type or 'none'} to {response_type or 'none'}.")
    if log.responders_text != responders:
        changes.append("Changed responder list.")
    old_counts = (log.response_count, log.treatment_count, log.revival_count, log.bodybag_count)
    new_counts = (response_count, treatment_count, revival_count, bodybag_count)
    if old_counts != new_counts:
        changes.append("Changed credited counts.")
    return "\n".join(changes) or "Reviewed details; no visible fields changed."


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    suffix = Path(attachment.filename).suffix.lower()
    return content_type.startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _extract_user_ids(content: str) -> set[str]:
    ids: set[str] = set()
    for match in ADMIN_ID_RE.finditer(content):
        user_id = match.group(1) or match.group(2)
        if user_id:
            ids.add(user_id)
    return ids


def _extract_ordered_ids(content: str) -> list[str]:
    return [match.group(0) for match in DISCORD_ID_RE.finditer(content)]
