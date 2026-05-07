"""Pydantic request/response models for the staging endpoints (Slice 9.3)."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


_State = Literal["pending", "approved", "rejected", "written", "failed"]
_TargetResourceType = Literal["Observation", "Task", "AllergyIntolerance"]


class PendingExtractionRow(BaseModel):
    """One row of ``copilot_pending_extractions`` rendered for the UI."""

    model_config = ConfigDict(extra="ignore")

    id: int
    document_reference_id: str
    file_batch_id: str
    patient_id: str
    target_resource_type: _TargetResourceType
    target_resource_id: str
    state: _State
    payload: dict[str, Any]
    write_error: Optional[str] = None
    retry_count: int = 0
    staged_at: Optional[str] = None
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None
    written_at: Optional[str] = None


class ListPendingResponse(BaseModel):
    rows: list[PendingExtractionRow]
    patient_id: str


class ApproveResponse(BaseModel):
    pending_id: int
    state: _State
    target_resource_id: str
    write_error: Optional[str] = None


class BatchApproveBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)


class BatchApproveResultItem(BaseModel):
    pending_id: int
    state: _State
    write_error: Optional[str] = None
    error: Optional[str] = None


class BatchApproveResponse(BaseModel):
    results: list[BatchApproveResultItem]
    n_approved: int
    n_failed: int


class RejectBody(BaseModel):
    reason: str = Field(min_length=1, max_length=256)


class RejectResponse(BaseModel):
    pending_id: int
    state: _State


class RetryResponse(BaseModel):
    pending_id: int
    state: _State
    retry_count: int


__all__ = [
    "PendingExtractionRow",
    "ListPendingResponse",
    "ApproveResponse",
    "BatchApproveBody",
    "BatchApproveResultItem",
    "BatchApproveResponse",
    "RejectBody",
    "RejectResponse",
    "RetryResponse",
]
