from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ShiftStatus(StrEnum):
    ACTIVE = "active"
    VALID = "valid"
    INVALIDATED = "invalidated"


class RequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class RequestType(StrEnum):
    ADJUST = "adjust"
    VOID = "void"


class ServiceLogKind(StrEnum):
    RESPONSE = "response"
    VITAL = "vital"


class ServiceLogStatus(StrEnum):
    PENDING_PROOF = "pending_proof"
    VALID = "valid"
    INVALIDATED = "invalidated"


@dataclass(slots=True)
class Shift:
    id: int
    user_id: str
    username: str
    display_name: str
    guild_id: str | None
    clock_in_utc: datetime
    clock_out_utc: datetime | None
    rendered_minutes: int
    break_minutes: int
    unpaid_break_minutes: int
    status: ShiftStatus
    current_break_id: int | None
    current_break_start_utc: datetime | None
    edited_by_user_id: str | None
    edited_by_display_name: str | None
    edited_at_utc: datetime | None
    edit_summary: str
    created_at_utc: datetime
    updated_at_utc: datetime


@dataclass(slots=True)
class MessageRef:
    id: int
    shift_id: int
    ref_type: str
    guild_id: str | None
    channel_id: str
    message_id: str
    thread_id: str | None


@dataclass(slots=True)
class UserLogThread:
    user_id: str
    guild_id: str | None
    forum_channel_id: str
    thread_id: str
    thread_name: str
    summary_message_id: str | None


@dataclass(slots=True)
class UserForumThread:
    thread_type: str
    user_id: str
    guild_id: str | None
    forum_channel_id: str
    thread_id: str
    thread_name: str
    summary_message_id: str | None


@dataclass(slots=True)
class ServiceLog:
    id: int
    log_kind: ServiceLogKind
    user_id: str
    username: str
    display_name: str
    guild_id: str | None
    postal: str
    response_type: str | None
    responders_text: str
    response_count: int
    treatment_count: int
    revival_count: int
    bodybag_count: int
    status: ServiceLogStatus
    edited_by_user_id: str | None
    edited_by_display_name: str | None
    edited_at_utc: datetime | None
    edit_summary: str
    created_at_utc: datetime
    updated_at_utc: datetime
    proof_received_at_utc: datetime | None
    proof_prompt_channel_id: str | None
    proof_prompt_message_id: str | None


@dataclass(slots=True)
class ServiceLogProof:
    id: int
    service_log_id: int
    local_path: str
    original_filename: str
    content_type: str | None


@dataclass(slots=True)
class ShiftBreak:
    id: int
    shift_id: int
    break_start_utc: datetime
    break_end_utc: datetime | None
    credited_minutes: int
    unpaid_minutes: int
    warning_sent_at_utc: datetime | None
    final_prompt_sent_at_utc: datetime | None
    final_prompt_message_channel_id: str | None
    final_prompt_message_id: str | None
    auto_clocked_out: bool
    created_at_utc: datetime
    updated_at_utc: datetime


@dataclass(slots=True)
class ChangeRequest:
    id: int
    shift_id: int
    requester_user_id: str
    requester_display_name: str
    request_type: RequestType
    requested_clock_in_utc: datetime | None
    requested_clock_out_utc: datetime | None
    status: RequestStatus
    admin_message_channel_id: str | None
    admin_message_id: str | None
    decided_by_user_id: str | None
    decided_by_display_name: str | None
    decided_at_utc: datetime | None
    created_at_utc: datetime
    updated_at_utc: datetime


@dataclass(slots=True)
class ShiftStats:
    user_id: str
    total_minutes: int
    shift_count: int


@dataclass(slots=True)
class ShiftPeriodTotals:
    user_id: str
    weekly_minutes: int
    daily_minutes: int


@dataclass(slots=True)
class ServicePeriodTotals:
    user_id: str
    log_kind: ServiceLogKind
    weekly_response_count: int = 0
    daily_response_count: int = 0
    weekly_distress_count: int = 0
    daily_distress_count: int = 0
    weekly_robbery_count: int = 0
    daily_robbery_count: int = 0
    weekly_treatment_count: int = 0
    weekly_revival_count: int = 0
    weekly_bodybag_count: int = 0
    daily_treatment_count: int = 0
    daily_revival_count: int = 0
    daily_bodybag_count: int = 0


@dataclass(slots=True)
class CombinedUserStats:
    user_id: str
    shift_minutes: int
    shift_count: int
    response_count: int
    treatment_count: int
    revival_count: int
    bodybag_count: int


@dataclass(slots=True)
class AdminUser:
    user_id: str
    display_name: str
    active: bool


@dataclass(slots=True)
class Employee:
    user_id: str
    display_name: str
    rank: str
    active: bool


@dataclass(slots=True)
class PayoutPreviewItem:
    user_id: str
    rank: str
    duty_minutes: int
    response_count: int
    vital_count: int
    payout_total: int
    skipped: bool = False


@dataclass(slots=True)
class PayoutSnapshot:
    id: int
    snapshot_type: str
    status: str
    created_by_user_id: str
    created_by_display_name: str
    created_at_utc: datetime
    cutoff_start_utc: datetime
    cutoff_end_utc: datetime
    include_skipped: bool


@dataclass(slots=True)
class PayoutSnapshotItem:
    id: int
    snapshot_id: int
    user_id: str
    rank: str
    duty_minutes: int
    response_count: int
    vital_count: int
    payout_total: int
    skipped: bool
