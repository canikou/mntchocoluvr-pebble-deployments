from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from famd_bot.models import (
    AdminUser,
    ChangeRequest,
    CombinedUserStats,
    Employee,
    PayoutPreviewItem,
    PayoutSnapshot,
    PayoutSnapshotItem,
    RequestStatus,
    RequestType,
    ServiceLog,
    ServiceLogKind,
    ServiceLogStatus,
    ServicePeriodTotals,
    Shift,
    ShiftPeriodTotals,
    ShiftStats,
    ShiftStatus,
)
from famd_bot.time_utils import format_display_dt, format_duration, rendered_minutes, utc_now


THEME_SUCCESS = 0x2BA17E
THEME_INFO = 0x4B6BFB
THEME_WARNING = 0xD97706
THEME_ERROR = 0xC1121F

BUTTON_STYLE_PRIMARY = 1
BUTTON_STYLE_SECONDARY = 2
BUTTON_STYLE_SUCCESS = 3
BUTTON_STYLE_DANGER = 4

DUTY_BOARD_TITLE = "FAMD Clock In/Out"
SHIFT_LOG_TITLE = "FAMD Shift Log"
REQUEST_TITLE = "FAMD Shift Change Request"
ADMIN_MANAGER_TITLE = "FAMD Admins"
EMPLOYEE_MANAGER_TITLE = "FAMD Employees"
STATS_TITLE = "FAMD Shift Stats"
USER_TOTALS_TITLE = "FAMD Weekly Summary"
LOGGING_BOARD_TITLE = "FAMD Field Logging"
SERVICE_LOG_TITLE = "FAMD Service Log"
WEEKLY_LOGS_TITLE = "Weekly Doctors Logs"
PAYOUTS_TITLE = "Doctors Payouts"


@dataclass(slots=True)
class EmbedFooter:
    text: str


@dataclass(slots=True)
class EmbedField:
    name: str
    value: str
    inline: bool = False


@dataclass(slots=True)
class EmbedPayload:
    title: str
    description: str
    color: int
    fields: list[EmbedField] = field(default_factory=list)
    footer: EmbedFooter | None = None

    def field(self, name: str, value: str, inline: bool = False) -> EmbedPayload:
        self.fields.append(EmbedField(name=name, value=value, inline=inline))
        return self

    def with_footer(self, text: str) -> EmbedPayload:
        self.footer = EmbedFooter(text=text)
        return self


@dataclass(slots=True)
class ButtonPayload:
    custom_id: str
    label: str
    style: int
    disabled: bool = False


@dataclass(slots=True)
class SelectOptionPayload:
    label: str
    value: str
    description: str | None = None


@dataclass(slots=True)
class SelectPayload:
    custom_id: str
    placeholder: str
    options: list[SelectOptionPayload]


@dataclass(slots=True)
class ViewPayload:
    buttons: list[ButtonPayload] = field(default_factory=list)
    select: SelectPayload | None = None


@dataclass(slots=True)
class ReplyPayload:
    embed: EmbedPayload
    view: ViewPayload | None = None
    silent_mentions: bool = True
    content: str | None = None


def duty_board_payload(
    shifts: list[Shift],
    timezone_name: str,
    *,
    break_cap_minutes: int = 15,
    now: datetime | None = None,
) -> ReplyPayload:
    current = now or utc_now()
    if not shifts:
        description = "No personnel are currently on duty."
    else:
        on_duty: list[str] = []
        on_break: list[str] = []
        for shift in shifts:
            if shift.current_break_start_utc is not None:
                break_elapsed = rendered_minutes(shift.current_break_start_utc, current)
                minutes_left = max(0, break_cap_minutes - break_elapsed)
                on_break.append(f"<@{shift.user_id}> ({minutes_left}m break time left)")
                continue
            minutes = rendered_minutes(shift.clock_in_utc, current)
            on_duty.append(f"<@{shift.user_id}> (On duty for {format_duration(minutes)})")
        sections: list[str] = []
        sections.extend(on_duty)
        if on_break:
            if sections:
                sections.append("")
            sections.append("On break:")
            sections.extend(on_break)
        description = "\n".join(sections) if sections else "No personnel are currently on duty."

    return ReplyPayload(
        embed=EmbedPayload(DUTY_BOARD_TITLE, description, THEME_INFO),
        view=ViewPayload(
            buttons=[
                ButtonPayload("famd:clock_in", "Clock In", BUTTON_STYLE_SUCCESS),
                ButtonPayload("famd:clock_out", "Clock Out", BUTTON_STYLE_DANGER),
                ButtonPayload("famd:break_toggle", "10-7 / 10-8", BUTTON_STYLE_PRIMARY),
            ]
        ),
    )


def logging_board_payload() -> ReplyPayload:
    return ReplyPayload(
        embed=EmbedPayload(
            LOGGING_BOARD_TITLE,
            "Select the field activity you want to log. The bot will open a form, then ask for image proof in your log thread.",
            THEME_INFO,
        ),
        view=ViewPayload(
            select=SelectPayload(
                custom_id="famd:logging_select",
                placeholder="Choose a log type",
                options=[
                    SelectOptionPayload("Robbery/Distress Response", "response", "Log a robbery or distress response."),
                    SelectOptionPayload("Vitals", "vital", "Log treatments, revivals, or bodybags."),
                ],
            )
        ),
    )


def shift_log_payload(
    shift: Shift,
    timezone_name: str,
    *,
    controls: bool,
    user_controls: bool = False,
) -> ReplyPayload:
    total = format_duration(shift.rendered_minutes)
    description = "\n".join(
        [
            f"Shift logged for <@{shift.user_id}> ({shift.user_id})",
            f"Time in: {format_display_dt(shift.clock_in_utc, timezone_name)}",
            f"Time out: {format_display_dt(shift.clock_out_utc, timezone_name)}",
            f"Total time rendered: {total}",
        ]
    )
    color = THEME_SUCCESS
    footer_parts: list[str] = []
    if shift.status is ShiftStatus.INVALIDATED:
        color = THEME_ERROR
        footer_parts.append("Invalidated - excluded from salary calculations")
    elif shift.edited_by_user_id is not None:
        color = THEME_WARNING

    embed = EmbedPayload(SHIFT_LOG_TITLE, description, color)
    if shift.edited_by_user_id is not None:
        edit_detail = shift.edit_summary.strip() or "Shift times were adjusted."
        embed.field("Edited by", f"<@{shift.edited_by_user_id}> ({shift.edited_by_user_id})\n{edit_detail}")
    if footer_parts:
        embed.with_footer(" | ".join(footer_parts))

    view: ViewPayload | None = None
    if controls:
        scope = "user" if user_controls else "admin"
        toggle_label = "Validate" if shift.status is ShiftStatus.INVALIDATED else ("Void" if user_controls else "Invalidate")
        toggle_style = BUTTON_STYLE_SUCCESS if shift.status is ShiftStatus.INVALIDATED else BUTTON_STYLE_DANGER
        view = ViewPayload(
            buttons=[
                ButtonPayload(f"famd:shift:adjust:{scope}:{shift.id}", "Adjust Time", BUTTON_STYLE_PRIMARY),
                ButtonPayload(f"famd:shift:toggle:{scope}:{shift.id}", toggle_label, toggle_style),
            ]
        )
    return ReplyPayload(embed=embed, view=view)


def service_credit_line(log: ServiceLog) -> str:
    if log.log_kind is ServiceLogKind.RESPONSE:
        label = "Response" if log.response_count == 1 else "Responses"
        return f"Credited to <@{log.user_id}>: {log.response_count}x {label}"
    parts: list[str] = []
    if log.treatment_count:
        parts.append(f"{log.treatment_count}x Treatment")
    if log.revival_count:
        parts.append(f"{log.revival_count}x Revival")
    if log.bodybag_count:
        parts.append(f"{log.bodybag_count}x Bodybag")
    return f"Credited to <@{log.user_id}>: {', '.join(parts) if parts else '0x Vitals'}"


def service_log_payload(log: ServiceLog, *, controls: bool = True) -> ReplyPayload:
    lines = [
        f"Logged for <@{log.user_id}> ({log.user_id})",
        f"Postal: {log.postal}",
        f"Responders: {log.responders_text}",
    ]
    if log.log_kind is ServiceLogKind.RESPONSE:
        lines.insert(1, f"Type: {log.response_type or ''}")
    else:
        lines.insert(1, f"Treatment: {log.treatment_count}")
        lines.insert(2, f"Revival: {log.revival_count}")
        lines.insert(3, f"Bodybag: {log.bodybag_count}")

    color = THEME_SUCCESS
    footer_parts: list[str] = []
    if log.status is ServiceLogStatus.PENDING_PROOF:
        color = THEME_WARNING
        footer_parts.append("Waiting for image proof")
    elif log.status is ServiceLogStatus.INVALIDATED:
        color = THEME_ERROR
        footer_parts.append("Invalidated - excluded from payout calculations")
    elif log.edited_by_user_id is not None:
        color = THEME_WARNING

    embed = EmbedPayload(SERVICE_LOG_TITLE, "\n".join(lines), color)
    if log.edited_by_user_id is not None:
        embed.field("Edited by", f"<@{log.edited_by_user_id}> ({log.edited_by_user_id})\n{log.edit_summary or 'Details were adjusted.'}")
    if footer_parts:
        embed.with_footer(" | ".join(footer_parts))

    view = None
    if controls:
        toggle_label = "Validate" if log.status is ServiceLogStatus.INVALIDATED else "Invalidate"
        toggle_style = BUTTON_STYLE_SUCCESS if log.status is ServiceLogStatus.INVALIDATED else BUTTON_STYLE_DANGER
        view = ViewPayload(
            buttons=[
                ButtonPayload(f"famd:servicelog:edit:{log.id}", "Edit Details", BUTTON_STYLE_PRIMARY),
                ButtonPayload(f"famd:servicelog:toggle:{log.id}", toggle_label, toggle_style),
            ]
        )
    return ReplyPayload(embed=embed, view=view, content=service_credit_line(log))


def request_payload(
    request: ChangeRequest,
    shift: Shift,
    timezone_name: str,
) -> ReplyPayload:
    current = "\n".join(
        [
            f"Time in: {format_display_dt(shift.clock_in_utc, timezone_name)}",
            f"Time out: {format_display_dt(shift.clock_out_utc, timezone_name)}",
            f"Total time rendered: {format_duration(shift.rendered_minutes)}",
        ]
    )
    if request.request_type is RequestType.ADJUST:
        requested_minutes = rendered_minutes(request.requested_clock_in_utc, request.requested_clock_out_utc) if (
            request.requested_clock_in_utc and request.requested_clock_out_utc
        ) else 0
        requested = "\n".join(
            [
                f"Time in: {format_display_dt(request.requested_clock_in_utc, timezone_name)}",
                f"Time out: {format_display_dt(request.requested_clock_out_utc, timezone_name)}",
                f"Total time rendered: {format_duration(requested_minutes)}",
            ]
        )
    else:
        requested = "Void this shift and exclude it from salary calculations."

    color = THEME_INFO
    if request.status is RequestStatus.APPROVED:
        color = THEME_SUCCESS
    elif request.status is RequestStatus.DENIED:
        color = THEME_ERROR

    embed = EmbedPayload(
        REQUEST_TITLE,
        f"<@{request.requester_user_id}> requested `{request.request_type.value}` for shift #{shift.id}.",
        color,
    )
    embed.field("Current shift", current)
    embed.field("Requested change", requested)
    if request.status is not RequestStatus.PENDING:
        decision = f"{request.status.value.title()} by <@{request.decided_by_user_id}>"
        embed.with_footer(decision)
        return ReplyPayload(embed=embed)
    return ReplyPayload(
        embed=embed,
        view=ViewPayload(
            buttons=[
                ButtonPayload(f"famd:req:approve:{request.id}", "Approve", BUTTON_STYLE_SUCCESS),
                ButtonPayload(f"famd:req:deny:{request.id}", "Deny", BUTTON_STYLE_DANGER),
            ]
        ),
    )


def admin_manager_payload(admins: list[AdminUser], owner_user_id: int) -> ReplyPayload:
    if admins:
        description = "\n".join(f"- <@{admin.user_id}> ({admin.user_id})" for admin in admins)
    else:
        description = "No active admins are registered."
    return ReplyPayload(
        embed=EmbedPayload(ADMIN_MANAGER_TITLE, description, THEME_INFO),
        view=ViewPayload(
            buttons=[
                ButtonPayload(f"famd:admin:add:{owner_user_id}", "Add Admin", BUTTON_STYLE_SUCCESS),
                ButtonPayload(f"famd:admin:remove:{owner_user_id}", "Remove Admin", BUTTON_STYLE_DANGER, disabled=not admins),
            ]
        ),
    )


def admin_remove_select_payload(admins: list[AdminUser], owner_user_id: int) -> ReplyPayload:
    options = [
        SelectOptionPayload(
            label=(admin.display_name or admin.user_id)[:100],
            value=admin.user_id,
            description=admin.user_id,
        )
        for admin in admins[:25]
    ]
    return ReplyPayload(
        embed=EmbedPayload(ADMIN_MANAGER_TITLE, "Select the admin to remove.", THEME_INFO),
        view=ViewPayload(
            select=SelectPayload(
                custom_id=f"famd:admin:remove_select:{owner_user_id}",
                placeholder="Choose an admin",
                options=options,
            )
        ),
    )


def admin_remove_confirm_payload(target_user_id: str, owner_user_id: int) -> ReplyPayload:
    return ReplyPayload(
        embed=EmbedPayload(
            ADMIN_MANAGER_TITLE,
            f"Remove <@{target_user_id}> ({target_user_id}) from active admins?",
            THEME_WARNING,
        ),
        view=ViewPayload(
            buttons=[
                ButtonPayload(f"famd:admin:remove_confirm:{owner_user_id}:{target_user_id}", "Confirm", BUTTON_STYLE_DANGER),
                ButtonPayload(f"famd:admin:remove_cancel:{owner_user_id}", "Cancel", BUTTON_STYLE_SECONDARY),
            ]
        ),
    )


def employee_manager_payload(employees: list[Employee], owner_user_id: int) -> ReplyPayload:
    if employees:
        description = "\n".join(f"<@{employee.user_id}> ({employee.user_id}) | {employee.rank}" for employee in employees)
    else:
        description = "No active employees are registered."
    return ReplyPayload(
        embed=EmbedPayload(EMPLOYEE_MANAGER_TITLE, description, THEME_INFO),
        view=ViewPayload(
            buttons=[
                ButtonPayload(f"famd:employee:add:{owner_user_id}", "Add Employee", BUTTON_STYLE_SUCCESS),
                ButtonPayload(f"famd:employee:remove:{owner_user_id}", "Remove Employee", BUTTON_STYLE_DANGER, disabled=not employees),
                ButtonPayload(f"famd:employee:manage:{owner_user_id}", "Manage Employee", BUTTON_STYLE_PRIMARY, disabled=not employees),
            ]
        ),
    )


def employee_select_payload(employees: list[Employee], owner_user_id: int, action: str) -> ReplyPayload:
    options = [
        SelectOptionPayload(
            label=(employee.display_name or employee.user_id)[:100],
            value=employee.user_id,
            description=f"{employee.rank} | {employee.user_id}"[:100],
        )
        for employee in employees[:25]
    ]
    verb = "remove" if action == "remove_select" else "manage"
    return ReplyPayload(
        embed=EmbedPayload(EMPLOYEE_MANAGER_TITLE, f"Select the employee to {verb}.", THEME_INFO),
        view=ViewPayload(
            select=SelectPayload(
                custom_id=f"famd:employee:{action}:{owner_user_id}",
                placeholder="Choose an employee",
                options=options,
            )
        ),
    )


def employee_rank_select_payload(
    ranks: list[str],
    owner_user_id: int,
    target_user_id: int | str,
    *,
    mode: str,
    display_name: str | None = None,
) -> ReplyPayload:
    options = [SelectOptionPayload(label=rank[:100], value=rank, description=None) for rank in ranks[:25]]
    action = "add" if mode == "add" else "change the rank for"
    name = display_name or str(target_user_id)
    return ReplyPayload(
        embed=EmbedPayload(
            EMPLOYEE_MANAGER_TITLE,
            f"Choose a rank to {action} <@{target_user_id}> ({target_user_id}).\n{name}",
            THEME_INFO,
        ),
        view=ViewPayload(
            select=SelectPayload(
                custom_id=f"famd:employee:rank:{owner_user_id}:{mode}:{target_user_id}",
                placeholder="Choose a rank",
                options=options,
            )
        ),
    )


def employee_remove_confirm_payload(target_user_id: str, owner_user_id: int) -> ReplyPayload:
    return ReplyPayload(
        embed=EmbedPayload(
            EMPLOYEE_MANAGER_TITLE,
            f"Remove <@{target_user_id}> ({target_user_id}) from active employees?",
            THEME_WARNING,
        ),
        view=ViewPayload(
            buttons=[
                ButtonPayload(f"famd:employee:remove_confirm:{owner_user_id}:{target_user_id}", "Confirm", BUTTON_STYLE_DANGER),
                ButtonPayload(f"famd:employee:cancel:{owner_user_id}", "Cancel", BUTTON_STYLE_SECONDARY),
            ]
        ),
    )


def payout_preview_payload(
    items: list[PayoutPreviewItem],
    owner_user_id: int,
    token: str,
    *,
    snapshot_type: str,
    include_skipped: bool,
    unregistered_user_ids: list[str] | None = None,
) -> ReplyPayload:
    unregistered = unregistered_user_ids or []
    if unregistered:
        lines = [
            "The following users have valid unpaid logs but are not registered employees yet:",
            *[f"<@{user_id}> ({user_id})" for user_id in unregistered],
            "",
            "Register them with `!manageemployees` first, then run the snapshot again.",
        ]
    elif items:
        lines = [_weekly_log_line(item) for item in items]
    else:
        lines = ["No active registered employees matched this snapshot."]
    skipped_count = sum(1 for item in items if item.skipped)
    description = "\n".join(
        [
            f"Snapshot type: {snapshot_type.title()}",
            f"Skipped early-paid users: {skipped_count}",
            "",
            *lines,
            "",
            "Confirm to mark these valid logs as accounted.",
        ]
    )
    buttons = [
        ButtonPayload(f"famd:payout:confirm:{owner_user_id}:{token}", "Confirm Snapshot", BUTTON_STYLE_SUCCESS, disabled=not items or bool(unregistered)),
        ButtonPayload(f"famd:payout:cancel:{owner_user_id}:{token}", "Cancel", BUTTON_STYLE_SECONDARY),
    ]
    if snapshot_type == "general" and skipped_count and not unregistered:
        label = "Exclude Early Paid" if include_skipped else "Include Early Paid"
        buttons.insert(1, ButtonPayload(f"famd:payout:toggle_skipped:{owner_user_id}:{token}", label, BUTTON_STYLE_PRIMARY))
    return ReplyPayload(embed=EmbedPayload("FAMD Payout Snapshot Preview", description, THEME_INFO), view=ViewPayload(buttons=buttons))


def weekly_logs_payload(items: list[PayoutSnapshotItem]) -> ReplyPayload:
    lines = [_weekly_log_line(item) for item in items] if items else ["No employees were included."]
    return ReplyPayload(embed=EmbedPayload(WEEKLY_LOGS_TITLE, "\n".join(lines), THEME_INFO))


def doctors_payouts_payload(items: list[PayoutSnapshotItem]) -> ReplyPayload:
    if items:
        lines = [
            f"<@{item.user_id}> ({item.user_id}) {item.rank} | Total Payout: {_format_money(item.payout_total)}"
            if not item.skipped
            else f"<@{item.user_id}> ({item.user_id}) {item.rank} | Skipped"
            for item in items
        ]
    else:
        lines = ["No payouts were generated."]
    return ReplyPayload(embed=EmbedPayload(PAYOUTS_TITLE, "\n".join(lines), THEME_SUCCESS))


def snapshot_revert_select_payload(snapshots: list[PayoutSnapshot], owner_user_id: int, timezone_name: str) -> ReplyPayload:
    options = [
        SelectOptionPayload(
            label=f"#{snapshot.id} {snapshot.snapshot_type.title()}"[:100],
            value=str(snapshot.id),
            description=format_display_dt(snapshot.created_at_utc, timezone_name)[:100],
        )
        for snapshot in snapshots[:25]
    ]
    description = "Select the snapshot to revert." if options else "No active payout snapshots can be reverted."
    return ReplyPayload(
        embed=EmbedPayload("FAMD Snapshot Revert", description, THEME_INFO),
        view=ViewPayload(
            select=SelectPayload(
                custom_id=f"famd:payout:revert_select:{owner_user_id}",
                placeholder="Choose a snapshot",
                options=options,
            )
        )
        if options
        else None,
    )


def snapshot_revert_confirm_payload(snapshot: PayoutSnapshot, owner_user_id: int, timezone_name: str) -> ReplyPayload:
    return ReplyPayload(
        embed=EmbedPayload(
            "FAMD Snapshot Revert",
            f"Revert snapshot #{snapshot.id} ({snapshot.snapshot_type}) from {format_display_dt(snapshot.created_at_utc, timezone_name)}?",
            THEME_WARNING,
        ),
        view=ViewPayload(
            buttons=[
                ButtonPayload(f"famd:payout:revert_confirm:{owner_user_id}:{snapshot.id}", "Confirm Revert", BUTTON_STYLE_DANGER),
                ButtonPayload(f"famd:payout:revert_cancel:{owner_user_id}", "Cancel", BUTTON_STYLE_SECONDARY),
            ]
        ),
    )


def cutoff_message(label: str, total: str) -> str:
    divider = "-=-=-=-=-=-=-=-=-=-=-=- WEEKLY CUTOFF -=-=-=-=-=-=-=-=-=-=-=-=-"
    bottom = "-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-"
    return "\n".join(
        [
            divider,
            f"TOTAL {label} FOR THE WEEK: {total}",
            bottom,
        ]
    )


def break_final_prompt_payload(break_id: int, user_id: str) -> ReplyPayload:
    return ReplyPayload(
        embed=EmbedPayload(
            "FAMD Break Check",
            "Use the buttons below to report your status.",
            THEME_WARNING,
        ),
        view=ViewPayload(
            buttons=[
                ButtonPayload(f"famd:break:back:{break_id}", "10-8", BUTTON_STYLE_SUCCESS),
                ButtonPayload(f"famd:break:out:{break_id}", "Clock Out", BUTTON_STYLE_DANGER),
            ]
        ),
        silent_mentions=False,
    )


def stats_payload(stats: list[ShiftStats]) -> ReplyPayload:
    if stats:
        description = "\n".join(
            f"<@{stat.user_id}> - Total duty hours: {format_duration(stat.total_minutes)} | "
            f"Total shifts: {stat.shift_count}"
            for stat in stats
        )
    else:
        description = "No valid shifts logged yet."
    return ReplyPayload(embed=EmbedPayload(STATS_TITLE, description, THEME_INFO))


def combined_stats_payload(stats: list[CombinedUserStats]) -> ReplyPayload:
    if stats:
        lines = []
        for stat in stats:
            vitals = stat.treatment_count + stat.revival_count + stat.bodybag_count
            lines.append(
                f"<@{stat.user_id}> - Duty: {format_duration(stat.shift_minutes)} ({stat.shift_count} shifts) | "
                f"Responses: {stat.response_count} | Vitals: {vitals} "
                f"(T {stat.treatment_count}, R {stat.revival_count}, B {stat.bodybag_count})"
            )
        description = "\n".join(lines)
    else:
        description = "No valid shift, response, or vital logs yet."
    return ReplyPayload(embed=EmbedPayload("FAMD Staff Stats", description, THEME_INFO))


def user_totals_payload(full_rp_name: str, totals: ShiftPeriodTotals) -> ReplyPayload:
    description = "\n".join(
        [
            full_rp_name,
            "",
            "Weekly totals:",
            "",
            f"Duty hours: {format_duration(totals.weekly_minutes)}",
            "",
            "Daily totals:",
            "",
            f"Duty hours: {format_duration(totals.daily_minutes)}",
        ]
    )
    return ReplyPayload(embed=EmbedPayload(USER_TOTALS_TITLE, description, THEME_INFO))


def service_totals_payload(full_rp_name: str, totals: ServicePeriodTotals) -> ReplyPayload:
    if totals.log_kind is ServiceLogKind.RESPONSE:
        weekly = (
            f"## Total Responses: {totals.weekly_response_count}\n"
            f"> Distress: {totals.weekly_distress_count}\n"
            f"> Robbery: {totals.weekly_robbery_count}"
        )
        daily = (
            f"## Total Responses: {totals.daily_response_count}\n"
            f"> Distress: {totals.daily_distress_count}\n"
            f"> Robbery: {totals.daily_robbery_count}"
        )
        title = "FAMD Response Summary"
    else:
        weekly_total = totals.weekly_treatment_count + totals.weekly_revival_count + totals.weekly_bodybag_count
        daily_total = totals.daily_treatment_count + totals.daily_revival_count + totals.daily_bodybag_count
        weekly = (
            f"## Total Vitals: {weekly_total}\n"
            f"> Treatment: {totals.weekly_treatment_count}\n"
            f"> Revival: {totals.weekly_revival_count}\n"
            f"> Bodybag: {totals.weekly_bodybag_count}"
        )
        daily = (
            f"## Total Vitals: {daily_total}\n"
            f"> Treatment: {totals.daily_treatment_count}\n"
            f"> Revival: {totals.daily_revival_count}\n"
            f"> Bodybag: {totals.daily_bodybag_count}"
        )
        title = "FAMD Vitals Summary"
    description = "\n".join([full_rp_name, "", "# Weekly Totals", weekly, "", "# Daily totals:", daily])
    return ReplyPayload(embed=EmbedPayload(title, description, THEME_INFO))


def payload_to_dict(payload: ReplyPayload) -> dict[str, Any]:
    result: dict[str, Any] = {
        "embed": {
            "title": payload.embed.title,
            "description": payload.embed.description,
            "color": payload.embed.color,
            "fields": [
                {"name": field.name, "value": field.value, "inline": field.inline}
                for field in payload.embed.fields
            ],
            "footer": payload.embed.footer.text if payload.embed.footer else None,
        },
        "silent_mentions": payload.silent_mentions,
        "content": payload.content,
    }
    if payload.view is not None:
        result["view"] = {
            "buttons": [
                {
                    "custom_id": button.custom_id,
                    "label": button.label,
                    "style": button.style,
                    "disabled": button.disabled,
                }
                for button in payload.view.buttons
            ],
            "select": None
            if payload.view.select is None
            else {
                "custom_id": payload.view.select.custom_id,
                "placeholder": payload.view.select.placeholder,
                "options": [
                    {"label": option.label, "value": option.value, "description": option.description}
                    for option in payload.view.select.options
                ],
            },
        }
    return result


def _weekly_log_line(item: PayoutPreviewItem | PayoutSnapshotItem) -> str:
    if item.skipped:
        return f"<@{item.user_id}> ({item.user_id}) Skipped"
    return (
        f"<@{item.user_id}> ({item.user_id}) Total Duty Hours: {format_duration(item.duty_minutes)} | "
        f"Total Robbery/Distress Count: {item.response_count} | Total Vital Logs Count: {item.vital_count}"
    )


def _format_money(value: int) -> str:
    return f"${value:,.0f}"
