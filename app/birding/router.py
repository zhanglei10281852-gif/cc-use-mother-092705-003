from __future__ import annotations

from fastapi import APIRouter, Query

from app.birding.schemas import ObservationSubmit, ReviewDecision, RuleConfig, WindowQuery
from app.birding.service import BirdEvidenceService

router = APIRouter(prefix="/api/birding", tags=["飞羽观测证据整理"])


def service() -> BirdEvidenceService:
    return BirdEvidenceService()


@router.post("/observations", status_code=201)
def submit_observation(payload: ObservationSubmit):
    return service().submit_observation(payload.model_dump())


@router.get("/observations/{observation_id}/trace")
def trace_observation(observation_id: int):
    return service().trace_observation(observation_id)


@router.get("/sightings/{sighting_id}")
def get_sighting(sighting_id: int):
    return service().get_sighting(sighting_id)


@router.get("/candidates")
def list_candidates(status: str = Query(default="pending", pattern="^(pending|merged|kept|rejected|superseded|all)$")):
    return {"items": service().list_candidates(None if status == "all" else status)}


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: int):
    return service().get_candidate(candidate_id)


@router.post("/candidates/{candidate_id}/decision")
def decide_candidate(candidate_id: int, payload: ReviewDecision):
    return service().decide_candidate(candidate_id, payload.model_dump())


@router.get("/rules")
def get_rules():
    return service().get_rules()


@router.put("/rules")
def update_rules(payload: RuleConfig, actor: str = Query(..., min_length=1, max_length=80)):
    return service().update_rules(payload.model_dump(), actor)


@router.get("/stats/window")
def window_stats(
    start: str | None = None,
    end: str | None = None,
    species: str | None = None,
    location_name: str | None = None,
    reviewed_only: bool = False,
):
    return service().window_stats(start=start, end=end, species=species,
                                  location_name=location_name, reviewed_only=reviewed_only)


@router.post("/stats/window", status_code=200)
def window_stats_post(payload: WindowQuery,
                      start: str | None = None,
                      end: str | None = None):
    data = payload.model_dump(exclude_none=True)
    return service().window_stats(start=start, end=end,
                                  species=data.get("species"),
                                  location_name=data.get("location_name"),
                                  reviewed_only=data.get("reviewed_only", False))


@router.get("/audit")
def list_audit(resource_type: str | None = None, resource_id: str | None = None,
               limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().list_audit(resource_type=resource_type, resource_id=resource_id, limit=limit)}
