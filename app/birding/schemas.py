from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class EvidenceRef(BaseModel):
    """单条佐证引用（照片、录音、原始上报链接等）。"""

    kind: str = Field(default="photo", max_length=24)
    reference: str = Field(..., min_length=1, max_length=200)
    caption: str = Field(default="", max_length=500)

    @field_validator("kind")
    @classmethod
    def normalize_kind(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("reference")
    @classmethod
    def strip_reference(cls, value: str) -> str:
        return value.strip()


class ObservationSubmit(BaseModel):
    """观察者提交的一条原始鸟况记录。"""

    request_id: str | None = Field(default=None, max_length=120, description="调用方去重键，重试时保持一致")
    observer: str = Field(..., min_length=1, max_length=80)
    observer_id: str | None = Field(default=None, max_length=80)
    species: str = Field(..., min_length=1, max_length=120)
    observed_at: str = Field(..., min_length=4, max_length=40, description="ISO8601 观测时间")
    location_name: str = Field(default="", max_length=120)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    photo_caption: str = Field(default="", max_length=500)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1, description="观察者对物种判断的自述可信度")
    count_hint: int = Field(default=1, ge=1, le=100000)
    notes: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _validate_coordinates(self) -> "ObservationSubmit":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("纬度和经度必须同时提供或同时省略")
        return self

    @field_validator("observer", "species", "location_name")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class RuleConfig(BaseModel):
    """候选重复识别的可配置规则。"""

    time_window_minutes: int = Field(default=120, ge=0, le=43200)
    distance_meters: int = Field(default=1000, ge=0, le=200000)
    require_same_species: bool = True
    evidence_overlap_matches: bool = True
    caption_similarity: bool = True
    caption_similarity_threshold: float = Field(default=0.6, ge=0, le=1)
    score_threshold: float = Field(default=0.5, ge=0, le=1, description="达到该评分才进入复核队列")
    auto_merge_threshold: float = Field(default=0.95, ge=0, le=2, description="达到该分将自动归并；高于入队阈值但低于它的配对进入人工复核")

    @model_validator(mode="after")
    def _validate_thresholds(self) -> "RuleConfig":
        if self.auto_merge_threshold <= self.score_threshold:
            raise ValueError("auto_merge_threshold 必须大于 score_threshold")
        return self


class ReviewDecision(BaseModel):
    decision: str = Field(..., pattern="^(merge|keep|reject)$")
    reviewer: str = Field(..., min_length=1, max_length=80)
    note: str = Field(default="", max_length=500)
    winning_observation_id: int | None = Field(default=None, ge=1, description="合并时保留为主证据的原始提交；缺省按可信度/时间自动选择")


class WindowQuery(BaseModel):
    species: str | None = None
    location_name: str | None = None
    reviewed_only: bool = False
