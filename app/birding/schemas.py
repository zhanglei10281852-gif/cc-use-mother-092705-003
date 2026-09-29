from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class EvidenceRef(BaseModel):
    """单条佐证引用（照片、录像或其他材料）。"""

    kind: str = Field(default="photo", max_length=20)
    uri: str = Field(..., min_length=1, max_length=500)
    caption: str = Field(default="", max_length=500)
    captured_at: Optional[str] = Field(default=None, max_length=40)


class ObservationCreate(BaseModel):
    observer: str = Field(..., min_length=1, max_length=80)
    observed_at: str = Field(..., min_length=4, max_length=40, description="ISO8601 时间，无时区按 UTC 处理")
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    location_name: str = Field(default="", max_length=200)
    species_code: str = Field(..., min_length=1, max_length=40)
    species_name: str = Field(default="", max_length=120)
    count: int = Field(default=1, ge=1, le=100000)
    confidence: float = Field(default=0.5, ge=0, le=1, description="提交者自报可信程度 0-1")
    behavior: str = Field(default="", max_length=200)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    note: str = Field(default="", max_length=1000)
    idempotency_key: Optional[str] = Field(default=None, max_length=120)


class RuleConfigUpdate(BaseModel):
    time_window_minutes: Optional[int] = Field(default=None, ge=1, le=43200)
    distance_meters: Optional[float] = Field(default=None, gt=0, le=1000000)
    auto_merge_threshold: Optional[float] = Field(default=None, ge=0, le=1)
    review_threshold: Optional[float] = Field(default=None, ge=0, le=1)
    same_species_bonus: Optional[float] = Field(default=None, ge=0, le=1)
    species_conflict_penalty: Optional[float] = Field(default=None, ge=0, le=1)
    evidence_bonus: Optional[float] = Field(default=None, ge=0, le=1)
    observer_repeat_bonus: Optional[float] = Field(default=None, ge=0, le=1)


class ReviewDecision(BaseModel):
    decision: str = Field(..., pattern="^(merge|keep|reject)$")
    reviewer: str = Field(..., min_length=1, max_length=80)
    note: str = Field(default="", max_length=1000)


class MigrationWindowQuery(BaseModel):
    species_code: Optional[str] = None
    location_name: Optional[str] = None


class ReanalysisRequest(BaseModel):
    window_start: Optional[str] = Field(default=None, max_length=40)
    window_end: Optional[str] = Field(default=None, max_length=40)
