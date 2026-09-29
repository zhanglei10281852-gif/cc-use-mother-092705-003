from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.birding.schemas import ObservationCreate, ReviewDecision, RuleConfigUpdate
from app.birding.service import BirdEvidenceService

router = APIRouter(prefix="/api/birding", tags=["飞羽观察证据整理"])


def service() -> BirdEvidenceService:
    return BirdEvidenceService()


@router.post("/observations", status_code=201)
def submit_observation(payload: ObservationCreate):
    try:
        return service().submit_observation(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/observations/{observation_id}")
def get_observation(observation_id: int):
    value = service().get_observation(observation_id)
    if value is None:
        raise HTTPException(status_code=404, detail="观察记录不存在")
    return value


@router.get("/clusters/{cluster_id}")
def get_cluster(cluster_id: int):
    value = service().get_cluster(cluster_id)
    if value is None:
        raise HTTPException(status_code=404, detail="合并体不存在")
    return value


@router.get("/candidates")
def list_candidates(status: str = Query(default="pending", pattern="^(pending|merged|kept_separate|rejected|cancelled|all)$"),
                    limit: int = Query(default=100, ge=1, le=500)):
    query_status = None if status == "all" else status
    return service().list_candidates(status=query_status or "pending", limit=limit)


@router.post("/candidates/{candidate_id}/review")
def review_candidate(candidate_id: int, payload: ReviewDecision):
    try:
        return service().review_candidate(candidate_id, payload.decision, payload.reviewer, payload.note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="候选关系不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/migration-window")
def migration_window(
    start: str = Query(..., description="窗口起点 ISO8601"),
    end: str = Query(..., description="窗口终点 ISO8601"),
    species_code: str | None = None,
    location_name: str | None = None,
):
    try:
        return service().migration_window(start, end, species_code=species_code,
                                          location_name=location_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/rules")
def get_rules():
    return service().current_rules()


@router.put("/rules")
def update_rules(payload: RuleConfigUpdate, actor: str = Query(..., min_length=1, max_length=80)):
    try:
        return service().update_rules(payload.model_dump(), actor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/audit")
def audit_log(observation_id: int | None = None, cluster_id: int | None = None,
              limit: int = Query(default=200, ge=1, le=1000)):
    return service().audit_log(observation_id=observation_id, cluster_id=cluster_id, limit=limit)
