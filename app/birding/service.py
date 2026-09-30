from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.birding.matching import detect_conflicts, parse_time, score_pair
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS bird_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT UNIQUE,
    observer TEXT NOT NULL,
    observer_id TEXT NOT NULL DEFAULT '',
    species TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    location_name TEXT NOT NULL DEFAULT '',
    latitude REAL,
    longitude REAL,
    photo_caption TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.5,
    count_hint INTEGER NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL UNIQUE,
    sighting_id INTEGER,
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK(review_status IN ('confirmed','pending')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bird_obs_window ON bird_observations(observed_at, species);
CREATE INDEX IF NOT EXISTS idx_bird_obs_sighting ON bird_observations(sighting_id);

CREATE TABLE IF NOT EXISTS bird_sightings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    species TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
    pinned_winner_observation_id INTEGER,
    adopted_at TEXT,
    merged_into_id INTEGER,
    created_at TEXT NOT NULL,
    archived_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bird_sightings_status ON bird_sightings(status);

CREATE TABLE IF NOT EXISTS bird_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sighting_a_id INTEGER NOT NULL REFERENCES bird_sightings(id),
    sighting_b_id INTEGER NOT NULL REFERENCES bird_sightings(id),
    trigger_observation_a_id INTEGER NOT NULL REFERENCES bird_observations(id),
    trigger_observation_b_id INTEGER NOT NULL REFERENCES bird_observations(id),
    pair_key TEXT NOT NULL,
    score REAL NOT NULL,
    signals_json TEXT NOT NULL DEFAULT '[]',
    conflicts_json TEXT NOT NULL DEFAULT '[]',
    rules_json TEXT NOT NULL DEFAULT '{}',
    challenges_adopted INTEGER NOT NULL DEFAULT 0 CHECK(challenges_adopted IN (0,1)),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','merged','kept','rejected','superseded')),
    reviewer TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    resolution_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bird_candidates_status ON bird_candidates(status, created_at);
CREATE INDEX IF NOT EXISTS idx_bird_candidates_pair ON bird_candidates(pair_key, status);

CREATE TABLE IF NOT EXISTS bird_lineage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES bird_observations(id),
    from_sighting_id INTEGER,
    to_sighting_id INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('create','auto_match','review_merge')),
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bird_lineage_obs ON bird_lineage(observation_id, id);

CREATE TABLE IF NOT EXISTS bird_rule_configs (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    rules_json TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bird_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bird_audit_resource ON bird_audit(resource_type, resource_id, id);
CREATE INDEX IF NOT EXISTS idx_bird_audit_created ON bird_audit(created_at, id);
"""

DEFAULT_RULES: dict[str, Any] = {
    "time_window_minutes": 120,
    "distance_meters": 1000,
    "require_same_species": True,
    "evidence_overlap_matches": True,
    "caption_similarity": True,
    "caption_similarity_threshold": 0.6,
    "score_threshold": 0.5,
    "auto_merge_threshold": 0.95,
    "adopt_confidence_threshold": 0.8,
}


def _digest(payload: dict[str, Any]) -> str:
    canonical = {key: payload.get(key) for key in sorted(payload) if key != "request_id"}
    text = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _pair_key(a: int, b: int) -> str:
    lo, hi = sorted((a, b))
    return f"{lo}:{hi}"


class BirdEvidenceService:
    """接收原始鸟况提交、识别疑似重复、支持人工复核并给出可追溯计数。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.ensure_schema()

    # ------------------------------------------------------------------ schema

    def ensure_schema(self) -> None:
        self.connection.executescript(SCHEMA)
        self.connection.execute(
            "INSERT OR IGNORE INTO bird_rule_configs(id,rules_json,updated_by,updated_at) VALUES(1,?,?,?)",
            (json.dumps(DEFAULT_RULES, ensure_ascii=False), "system", to_storage(self.clock.now())),
        )

    # ------------------------------------------------------------- helpers

    def _now(self) -> str:
        return to_storage(self.clock.now())

    @staticmethod
    def _normalize_observed_at(value: str) -> str:
        parsed = parse_time(value)
        if parsed is None:
            raise ValidationError("observed_at 必须是可解析的 ISO8601 时间")
        return to_storage(parsed)

    def _rules(self) -> dict[str, Any]:
        row = self.connection.execute("SELECT rules_json FROM bird_rule_configs WHERE id=1").fetchone()
        if row is None:
            return dict(DEFAULT_RULES)
        rules = json.loads(row["rules_json"])
        merged = dict(DEFAULT_RULES)
        merged.update(rules)
        return merged

    def _audit(self, connection: sqlite3.Connection, *, action: str, resource_type: str, resource_id: str | int = "",
               actor: str = "system", before: dict | None = None, after: dict | None = None, request_id: str = "") -> None:
        connection.execute(
            "INSERT INTO bird_audit(request_id,actor,action,resource_type,resource_id,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (request_id, actor, action, resource_type, str(resource_id),
             json.dumps(before or {}, ensure_ascii=False), json.dumps(after or {}, ensure_ascii=False), self._now()),
        )

    @staticmethod
    def _observation_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
        return item

    def _get_observation(self, connection: sqlite3.Connection, observation_id: int) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM bird_observations WHERE id=?", (observation_id,)).fetchone()
        if row is None:
            raise NotFoundError("原始提交不存在")
        return self._observation_dict(row)

    def _sighting_members(self, connection: sqlite3.Connection, sighting_id: int) -> list[dict[str, Any]]:
        rows = connection.execute("SELECT * FROM bird_observations WHERE sighting_id=? ORDER BY id", (sighting_id,)).fetchall()
        return [self._observation_dict(row) for row in rows]

    def _choose_winner(self, members: list[dict[str, Any]], pinned_id: int | None = None) -> dict[str, Any]:
        if pinned_id is not None:
            for member in members:
                if member["id"] == pinned_id:
                    return member
        confirmed = [m for m in members if m["review_status"] == "confirmed"] or members
        return sorted(confirmed, key=lambda m: (-m["confidence"], m["observed_at"], m["id"]))[0]

    def _is_adopted(self, connection: sqlite3.Connection, sighting: sqlite3.Row, members: list[dict[str, Any]]) -> bool:
        if sighting["adopted_at"]:
            return True
        threshold = float(self._rules().get("adopt_confidence_threshold", 0.8))
        confirmed = [m for m in members if m["review_status"] == "confirmed"]
        if not confirmed:
            return False
        if len(confirmed) == 1:
            return confirmed[0]["confidence"] >= threshold
        distinct_observers = {m["observer_id"] or m["observer"] for m in confirmed}
        return len(distinct_observers) >= 2

    def _create_sighting(self, connection: sqlite3.Connection, species: str, now: str) -> int:
        cursor = connection.execute("INSERT INTO bird_sightings(species,created_at) VALUES(?,?)", (species, now))
        return int(cursor.lastrowid)

    def _record_lineage(self, connection: sqlite3.Connection, *, observation_id: int, from_id: int | None,
                        to_id: int, reason: str, actor: str, detail: dict | None = None) -> None:
        connection.execute(
            "INSERT INTO bird_lineage(observation_id,from_sighting_id,to_sighting_id,reason,actor,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (observation_id, from_id, to_id, reason, actor, json.dumps(detail or {}, ensure_ascii=False), self._now()),
        )

    # ------------------------------------------------------------- configuration

    def get_rules(self) -> dict[str, Any]:
        return self._rules()

    def update_rules(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        before = self._rules()
        merged = dict(before)
        merged.update({key: value for key, value in payload.items() if value is not None})
        now = self._now()
        with transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE bird_rule_configs SET rules_json=?,updated_by=?,updated_at=? WHERE id=1",
                (json.dumps(merged, ensure_ascii=False), actor, now),
            )
            self._audit(connection, action="rules.update", resource_type="rule_config", resource_id=1,
                        actor=actor, before=before, after=merged)
        return merged

    # ------------------------------------------------------------- submission

    def submit_observation(self, payload: dict[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        evidence = payload.get("evidence") or []
        normalized = dict(payload)
        normalized["evidence"] = evidence
        normalized["observed_at"] = self._normalize_observed_at(normalized["observed_at"])
        payload_hash = _digest(normalized)
        request_id = (normalized.get("request_id") or "").strip() or None
        actor = actor or normalized.get("observer") or "observer"
        now = self._now()

        with transaction(immediate=True) as connection:
            # 1) 幂等：同一 request_id 或完全相同的载荷重试，返回首次处理结果。
            if request_id:
                existing = connection.execute("SELECT * FROM bird_observations WHERE request_id=?", (request_id,)).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != payload_hash:
                        raise ConflictError("同一 request_id 已用于不同的观测提交")
                    return self._submit_replay(connection, existing, reason="request_id")
            duplicate = connection.execute("SELECT * FROM bird_observations WHERE payload_hash=?", (payload_hash,)).fetchone()
            if duplicate is not None:
                return self._submit_replay(connection, duplicate, reason="identical_payload")

            rules = self._rules()
            review_status = "confirmed" if float(normalized.get("confidence", 0.5)) >= float(rules.get("adopt_confidence_threshold", 0.8)) else "pending"

            cursor = connection.execute(
                """INSERT INTO bird_observations(request_id,observer,observer_id,species,observed_at,location_name,
                   latitude,longitude,photo_caption,evidence_json,confidence,count_hint,notes,payload_hash,
                   review_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (request_id, normalized["observer"], normalized.get("observer_id") or "", normalized["species"],
                 normalized["observed_at"], normalized.get("location_name") or "", normalized.get("latitude"),
                 normalized.get("longitude"), normalized.get("photo_caption") or "",
                 json.dumps(evidence, ensure_ascii=False), float(normalized.get("confidence", 0.5)),
                 int(normalized.get("count_hint", 1)), normalized.get("notes") or "", payload_hash,
                 review_status, now),
            )
            observation_id = int(cursor.lastrowid)
            new_observation = self._get_observation(connection, observation_id)

            # 2) 与现存目击实体比对：取各 active 组的代表证据评分（同物种窗口内做粗筛）。
            best: tuple[float, list[str], int, dict[str, Any]] | None = None
            candidates: list[tuple[int, dict[str, Any], float, list[str]]] = []
            sighting_rows = connection.execute(
                "SELECT * FROM bird_sightings WHERE status='active' AND species=? ORDER BY id",
                (normalized["species"],),
            ).fetchall()
            for sighting in sighting_rows:
                members = self._sighting_members(connection, sighting["id"])
                if any(m["id"] == observation_id for m in members):
                    continue
                local_best_score, local_best_member, local_best_signals = 0.0, None, []
                for member in members:
                    score, signals = score_pair(new_observation, member, rules)
                    if score > local_best_score:
                        local_best_score, local_best_member, local_best_signals = score, member, signals
                if local_best_member is None or local_best_score < float(rules["score_threshold"]):
                    continue
                candidates.append((sighting["id"], local_best_member, local_best_score, local_best_signals))
                if best is None or local_best_score > best[0]:
                    best = (local_best_score, local_best_signals, sighting["id"], local_best_member)

            # 3) 近乎确定（共享佐证等高置信信号）才自动归并；否则另立实体并生成复核候选。
            auto_threshold = float(rules["auto_merge_threshold"])
            auto_target = best[2] if best is not None and best[0] >= auto_threshold else None

            if auto_target is not None:
                sighting_id = auto_target
                connection.execute("UPDATE bird_observations SET sighting_id=? WHERE id=?", (sighting_id, observation_id))
                self._record_lineage(connection, observation_id=observation_id, from_id=None, to_id=sighting_id,
                                     reason="auto_match", actor=actor, detail={"score": best[0], "signals": best[1]})
                self._audit(connection, action="observation.auto_match", resource_type="observation", resource_id=observation_id,
                            actor=actor, after={"sighting_id": sighting_id, "score": best[0], "signals": best[1]},
                            request_id=request_id or "")
            else:
                sighting_id = self._create_sighting(connection, normalized["species"], now)
                connection.execute("UPDATE bird_observations SET sighting_id=? WHERE id=?", (sighting_id, observation_id))
                self._record_lineage(connection, observation_id=observation_id, from_id=None, to_id=sighting_id,
                                     reason="create", actor=actor)
                self._audit(connection, action="observation.submit", resource_type="observation", resource_id=observation_id,
                            actor=actor, after={"sighting_id": sighting_id, "review_status": review_status},
                            request_id=request_id or "")

            # 4) 对阈值以上的所有配对生成复核候选；已终审或已有未决候选的同一对不重复入队。
            created_candidates: list[dict[str, Any]] = []
            for other_sighting_id, other_member, score, signals in candidates:
                if other_sighting_id == sighting_id:
                    continue
                pair = _pair_key(sighting_id, other_sighting_id)
                prior = connection.execute(
                    "SELECT status FROM bird_candidates WHERE pair_key=? AND status IN ('pending','kept','rejected') ORDER BY id DESC LIMIT 1",
                    (pair,),
                ).fetchone()
                if prior is not None:
                    continue
                conflicts = detect_conflicts(new_observation, other_member, rules)
                other_sighting = connection.execute("SELECT * FROM bird_sightings WHERE id=?", (other_sighting_id,)).fetchone()
                other_members = self._sighting_members(connection, other_sighting_id)
                new_sighting_row = connection.execute("SELECT * FROM bird_sightings WHERE id=?", (sighting_id,)).fetchone()
                new_members = self._sighting_members(connection, sighting_id)
                challenges_adopted = 1 if (
                    self._is_adopted(connection, other_sighting, other_members)
                    or self._is_adopted(connection, new_sighting_row, new_members)
                ) else 0
                a, b = sorted((sighting_id, other_sighting_id))
                cursor = connection.execute(
                    """INSERT INTO bird_candidates(sighting_a_id,sighting_b_id,trigger_observation_a_id,
                       trigger_observation_b_id,pair_key,score,signals_json,conflicts_json,rules_json,
                       challenges_adopted,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (a, b,
                     new_observation["id"] if sighting_id == a else other_member["id"],
                     new_observation["id"] if sighting_id == b else other_member["id"],
                     pair, score, json.dumps(signals, ensure_ascii=False),
                     json.dumps(conflicts, ensure_ascii=False), json.dumps(rules, ensure_ascii=False),
                     challenges_adopted, now),
                )
                created_candidates.append({
                    "candidate_id": int(cursor.lastrowid), "sighting_id": other_sighting_id,
                    "score": score, "signals": signals, "conflicts": conflicts,
                    "challenges_adopted": bool(challenges_adopted),
                })
                self._audit(connection, action="candidate.detected", resource_type="candidate",
                            resource_id=cursor.lastrowid, actor="rule-engine",
                            after={"pair": pair, "score": score, "conflicts": conflicts,
                                   "challenges_adopted": bool(challenges_adopted)},
                            request_id=request_id or "")

            return {
                "observation_id": observation_id,
                "sighting_id": sighting_id,
                "review_status": review_status,
                "matched": auto_target is not None,
                "replayed": False,
                "candidates": created_candidates,
            }

    def _submit_replay(self, connection: sqlite3.Connection, row: sqlite3.Row, *, reason: str) -> dict[str, Any]:
        observation = self._observation_dict(row)
        open_candidates = connection.execute(
            "SELECT id FROM bird_candidates WHERE status='pending' AND (sighting_a_id=? OR sighting_b_id=?)",
            (observation["sighting_id"], observation["sighting_id"]),
        ).fetchall()
        return {
            "observation_id": observation["id"],
            "sighting_id": observation["sighting_id"],
            "review_status": observation["review_status"],
            "matched": bool(connection.execute(
                "SELECT 1 FROM bird_lineage WHERE observation_id=? AND reason='auto_match'", (observation["id"],)
            ).fetchone()),
            "replayed": True,
            "replay_reason": reason,
            "candidates": [{"candidate_id": item["id"]} for item in open_candidates],
        }

    # ------------------------------------------------------------- review queue

    def list_candidates(self, status: str | None = "pending") -> list[dict[str, Any]]:
        if status is None:
            rows = self.connection.execute(
                "SELECT * FROM bird_candidates ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, challenges_adopted DESC, score DESC, id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM bird_candidates WHERE status=? ORDER BY challenges_adopted DESC, score DESC, id",
                (status,),
            ).fetchall()
        return [self._candidate_detail(row) for row in rows]

    def get_candidate(self, candidate_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM bird_candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise NotFoundError("候选关系不存在")
        return self._candidate_detail(row)

    def _candidate_detail(self, row: sqlite3.Row) -> dict[str, Any]:
        connection = self.connection
        result = dict(row)
        for key in ("signals_json", "conflicts_json", "rules_json", "resolution_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key) or "{}")
        result["challenges_adopted"] = bool(result["challenges_adopted"])
        sightings = {}
        for label, sighting_id in (("a", row["sighting_a_id"]), ("b", row["sighting_b_id"])):
            sighting = connection.execute("SELECT * FROM bird_sightings WHERE id=?", (sighting_id,)).fetchone()
            members = self._sighting_members(connection, sighting_id)
            winner = self._choose_winner(members, sighting["pinned_winner_observation_id"]) if members else None
            sightings[label] = {
                "sighting_id": sighting_id,
                "status": sighting["status"],
                "adopted": bool(sighting["adopted_at"]),
                "winner_observation_id": winner["id"] if winner else None,
                "members": [
                    {
                        "observation_id": m["id"], "observer": m["observer"], "observer_id": m["observer_id"],
                        "species": m["species"], "observed_at": m["observed_at"],
                        "location_name": m["location_name"], "latitude": m["latitude"], "longitude": m["longitude"],
                        "photo_caption": m["photo_caption"], "confidence": m["confidence"],
                        "review_status": m["review_status"], "evidence": m["evidence"],
                    }
                    for m in members
                ],
            }
        result["sightings"] = sightings
        return result

    def decide_candidate(self, candidate_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        decision = payload["decision"]
        decided_status = {"merge": "merged", "keep": "kept", "reject": "rejected"}[decision]
        reviewer = payload["reviewer"]
        note = payload.get("note") or ""
        now = self._now()
        with transaction(immediate=True) as connection:
            candidate = connection.execute("SELECT * FROM bird_candidates WHERE id=?", (candidate_id,)).fetchone()
            if candidate is None:
                raise NotFoundError("候选关系不存在")
            if candidate["status"] != "pending":
                # 同一决定重试：幂等返回首次结果；改变已生效决定则冲突。
                resolution = json.loads(candidate["resolution_json"] or "{}")
                if candidate["status"] == decided_status:
                    return {"candidate_id": candidate_id, "status": candidate["status"], "replayed": True,
                            "reviewer": candidate["reviewer"], "resolution": resolution}
                raise ConflictError(f"候选关系已经{self._status_label(candidate['status'])}，不能重复决定")

            sighting_a = connection.execute("SELECT * FROM bird_sightings WHERE id=?", (candidate["sighting_a_id"],)).fetchone()
            sighting_b = connection.execute("SELECT * FROM bird_sightings WHERE id=?", (candidate["sighting_b_id"],)).fetchone()
            if sighting_a["status"] != "active" or sighting_b["status"] != "active":
                raise ConflictError("候选涉及的目击实体已不是活动状态，请刷新队列")

            resolution: dict[str, Any]
            if decision == "merge":
                resolution = self._apply_merge(connection, candidate, sighting_a, sighting_b, payload, reviewer, now)
            elif decision == "keep":
                resolution = {"kept_separate": [sighting_a["id"], sighting_b["id"]]}
                connection.execute("UPDATE bird_sightings SET adopted_at=COALESCE(adopted_at,?) WHERE id IN (?,?)",
                                   (now, sighting_a["id"], sighting_b["id"]))
                self._pin_if_needed(connection, sighting_a["id"], now)
                self._pin_if_needed(connection, sighting_b["id"], now)
            else:  # reject：候选关系本身是误报，不改变实体归属与采纳状态
                resolution = {"rejected_pair": [sighting_a["id"], sighting_b["id"]]}

            connection.execute(
                "UPDATE bird_candidates SET status=?,reviewer=?,note=?,resolution_json=?,decided_at=? WHERE id=?",
                (decided_status, reviewer, note, json.dumps(resolution, ensure_ascii=False), now, candidate_id),
            )
            self._audit(connection, action=f"candidate.{decision}", resource_type="candidate", resource_id=candidate_id,
                        actor=reviewer, before=dict(candidate),
                        after={"decision": decision, "note": note, "resolution": resolution})
            return {"candidate_id": candidate_id, "status": decided_status, "replayed": False,
                    "reviewer": reviewer, "resolution": resolution}

    def _pin_if_needed(self, connection: sqlite3.Connection, sighting_id: int, now: str) -> None:
        sighting = connection.execute("SELECT * FROM bird_sightings WHERE id=?", (sighting_id,)).fetchone()
        members = self._sighting_members(connection, sighting_id)
        if not members:
            return
        winner = self._choose_winner(members, sighting["pinned_winner_observation_id"])
        connection.execute(
            "UPDATE bird_sightings SET adopted_at=COALESCE(adopted_at,?),pinned_winner_observation_id=COALESCE(pinned_winner_observation_id,?) WHERE id=?",
            (now, winner["id"], sighting_id),
        )
        if winner["review_status"] != "confirmed":
            connection.execute("UPDATE bird_observations SET review_status='confirmed' WHERE id=?", (winner["id"],))

    def _apply_merge(self, connection: sqlite3.Connection, candidate: sqlite3.Row,
                     sighting_a: sqlite3.Row, sighting_b: sqlite3.Row, payload: dict[str, Any],
                     reviewer: str, now: str) -> dict[str, Any]:
        members_a = self._sighting_members(connection, sighting_a["id"])
        members_b = self._sighting_members(connection, sighting_b["id"])
        all_members = members_a + members_b
        winner_id = payload.get("winning_observation_id")
        if winner_id is not None and not any(m["id"] == winner_id for m in all_members):
            raise ValidationError("winning_observation_id 必须是候选双方的原始提交之一")

        # 已采用实体有钉住的主证据；除非复核人显式指定，否则主证据不会被新来的低可信记录顶替。
        pinned = sighting_a["pinned_winner_observation_id"] or sighting_b["pinned_winner_observation_id"]
        winner_id = winner_id or pinned
        winner = self._choose_winner(all_members, winner_id)

        if sighting_a["pinned_winner_observation_id"]:
            surviving, archived = sighting_a, sighting_b
        elif sighting_b["pinned_winner_observation_id"]:
            surviving, archived = sighting_b, sighting_a
        else:
            surviving, archived = (sighting_a, sighting_b) if winner["sighting_id"] == sighting_a["id"] else (sighting_b, sighting_a)

        moved = [m["id"] for m in members_b] if archived["id"] == sighting_b["id"] else [m["id"] for m in members_a]
        connection.execute("UPDATE bird_observations SET sighting_id=? WHERE sighting_id=?", (surviving["id"], archived["id"]))
        for observation_id in moved:
            self._record_lineage(connection, observation_id=observation_id, from_id=archived["id"],
                                 to_id=surviving["id"], reason="review_merge", actor=reviewer,
                                 detail={"candidate_id": candidate["id"], "winner_observation_id": winner["id"]})
            # 被合并方的原始提交降为支撑证据，但永远保留、可追溯。
            connection.execute("UPDATE bird_observations SET review_status='pending' WHERE id=? AND id<>?",
                               (observation_id, winner["id"]))
        if winner["review_status"] != "confirmed":
            connection.execute("UPDATE bird_observations SET review_status='confirmed' WHERE id=?", (winner["id"],))

        connection.execute(
            "UPDATE bird_sightings SET status='archived',archived_at=?,merged_into_id=? WHERE id=?",
            (now, surviving["id"], archived["id"]),
        )
        connection.execute(
            "UPDATE bird_sightings SET adopted_at=COALESCE(adopted_at,?),pinned_winner_observation_id=? WHERE id=?",
            (now, winner["id"], surviving["id"]),
        )

        # 把指向被归档实体的其它未决候选改挂到存活实体；重复对标记为 superseded。
        repointed: list[int] = []
        others = connection.execute(
            "SELECT * FROM bird_candidates WHERE status='pending' AND id<>? AND (sighting_a_id=? OR sighting_b_id=?)",
            (candidate["id"], archived["id"], archived["id"]),
        ).fetchall()
        for other in others:
            new_a, new_b = (surviving["id"], other["sighting_b_id"]) if other["sighting_a_id"] == archived["id"] else (other["sighting_a_id"], surviving["id"])
            if new_a == new_b:
                connection.execute("UPDATE bird_candidates SET status='superseded',decided_at=? WHERE id=?", (now, other["id"]))
                continue
            a, b = sorted((new_a, new_b))
            duplicate = connection.execute(
                "SELECT id FROM bird_candidates WHERE id<>? AND pair_key=? AND status='pending' ORDER BY id LIMIT 1",
                (other["id"], _pair_key(a, b)),
            ).fetchone()
            if duplicate is not None:
                connection.execute("UPDATE bird_candidates SET status='superseded',decided_at=? WHERE id=?", (now, other["id"]))
                continue
            connection.execute(
                "UPDATE bird_candidates SET sighting_a_id=?,sighting_b_id=?,pair_key=? WHERE id=?",
                (a, b, _pair_key(a, b), other["id"]),
            )
            repointed.append(other["id"])

        self._audit(connection, action="sighting.merge", resource_type="sighting", resource_id=surviving["id"],
                    actor=reviewer, before={"archived_sighting_id": archived["id"]},
                    after={"surviving_sighting_id": surviving["id"], "winner_observation_id": winner["id"],
                           "moved_observations": moved, "repointed_candidates": repointed})
        return {
            "surviving_sighting_id": surviving["id"],
            "archived_sighting_id": archived["id"],
            "winner_observation_id": winner["id"],
            "moved_observations": moved,
            "repointed_candidates": repointed,
        }

    @staticmethod
    def _status_label(status: str) -> str:
        return {"merged": "合并", "kept": "保留", "rejected": "否决", "superseded": "替代"}.get(status, status)

    # ------------------------------------------------------------- queries

    def get_sighting(self, sighting_id: int) -> dict[str, Any]:
        sighting = self.connection.execute("SELECT * FROM bird_sightings WHERE id=?", (sighting_id,)).fetchone()
        if sighting is None:
            raise NotFoundError("目击实体不存在")
        members = self._sighting_members(self.connection, sighting_id)
        winner = self._choose_winner(members, sighting["pinned_winner_observation_id"]) if members else None
        lineage = self.connection.execute(
            "SELECT * FROM bird_lineage WHERE to_sighting_id=? OR observation_id IN (SELECT id FROM bird_observations WHERE sighting_id=?) ORDER BY id",
            (sighting_id, sighting_id),
        ).fetchall()
        return {
            "sighting_id": sighting_id,
            "species": sighting["species"],
            "status": sighting["status"],
            "adopted": bool(sighting["adopted_at"]),
            "adopted_at": sighting["adopted_at"],
            "pinned_winner_observation_id": sighting["pinned_winner_observation_id"],
            "merged_into_id": sighting["merged_into_id"],
            "winner": {
                "observation_id": winner["id"], "observer": winner["observer"], "observed_at": winner["observed_at"],
                "location_name": winner["location_name"], "confidence": winner["confidence"],
                "photo_caption": winner["photo_caption"],
            } if winner else None,
            "members": [
                {
                    "observation_id": m["id"], "observer": m["observer"], "observer_id": m["observer_id"],
                    "observed_at": m["observed_at"], "location_name": m["location_name"],
                    "latitude": m["latitude"], "longitude": m["longitude"], "species": m["species"],
                    "photo_caption": m["photo_caption"], "confidence": m["confidence"],
                    "review_status": m["review_status"], "evidence": m["evidence"], "notes": m["notes"],
                    "request_id": m["request_id"], "created_at": m["created_at"],
                }
                for m in members
            ],
            "lineage": [dict(row) for row in lineage],
        }

    def window_stats(self, *, start: str | None = None, end: str | None = None,
                     species: str | None = None, location_name: str | None = None,
                     reviewed_only: bool = False) -> dict[str, Any]:
        rules = self._rules()
        start_parsed = parse_time(start) if start else None
        end_parsed = parse_time(end) if end else None
        if (start and start_parsed is None) or (end and end_parsed is None):
            raise ValidationError("start/end 必须是可解析的 ISO8601 时间")
        start_norm = to_storage(start_parsed) if start_parsed else None
        end_norm = to_storage(end_parsed) if end_parsed else None
        sighting_rows = self.connection.execute("SELECT * FROM bird_sightings WHERE status='active' ORDER BY id").fetchall()
        composition: list[dict[str, Any]] = []
        for sighting in sighting_rows:
            members = self._sighting_members(self.connection, sighting["id"])
            if not members:
                continue
            winner = self._choose_winner(members, sighting["pinned_winner_observation_id"])
            if species and winner["species"].strip().casefold() != species.strip().casefold():
                continue
            if location_name and location_name.strip().casefold() not in (winner["location_name"] or "").casefold():
                continue
            if start_norm and winner["observed_at"] < start_norm:
                continue
            if end_norm and winner["observed_at"] >= end_norm:
                continue
            adopted = self._is_adopted(self.connection, sighting, members)
            if reviewed_only and not adopted:
                continue
            open_candidates = self.connection.execute(
                "SELECT id,score,challenges_adopted FROM bird_candidates WHERE status='pending' AND (sighting_a_id=? OR sighting_b_id=?) ORDER BY id",
                (sighting["id"], sighting["id"]),
            ).fetchall()
            composition.append({
                "sighting_id": sighting["id"],
                "species": winner["species"],
                "observed_at": winner["observed_at"],
                "location_name": winner["location_name"],
                "latitude": winner["latitude"],
                "longitude": winner["longitude"],
                "adopted": adopted,
                "pinned_winner_observation_id": sighting["pinned_winner_observation_id"],
                "winner_observation_id": winner["id"],
                "winner_observer": winner["observer"],
                "member_count": len(members),
                "distinct_observers": len({m["observer_id"] or m["observer"] for m in members}),
                "evidence": [
                    {
                        "observation_id": m["id"],
                        "observer": m["observer"],
                        "observed_at": m["observed_at"],
                        "location_name": m["location_name"],
                        "confidence": m["confidence"],
                        "review_status": m["review_status"],
                        "role": "primary" if m["id"] == winner["id"] else "supporting",
                        "evidence_refs": m["evidence"],
                        "photo_caption": m["photo_caption"],
                    }
                    for m in sorted(members, key=lambda m: (m["id"] != winner["id"], m["id"]))
                ],
                "open_candidate_ids": [row["id"] for row in open_candidates],
                "challenged_by_adopted_candidate": any(row["challenges_adopted"] for row in open_candidates),
            })

        raw_rows = self.connection.execute("SELECT id FROM bird_observations").fetchall()
        open_pending = self.connection.execute("SELECT COUNT(*) AS c FROM bird_candidates WHERE status='pending'").fetchone()["c"]
        adopted_count = sum(1 for item in composition if item["adopted"])
        return {
            "window": {"start": start_norm, "end": end_norm, "species": species, "location_name": location_name,
                       "reviewed_only": reviewed_only},
            "rules_snapshot": {"time_window_minutes": rules["time_window_minutes"],
                               "distance_meters": rules["distance_meters"],
                               "score_threshold": rules["score_threshold"]},
            "raw_submission_count": len(raw_rows),
            "bird_count": len(composition),
            "adopted_count": adopted_count,
            "pending_count": len(composition) - adopted_count,
            "open_candidate_count": open_pending,
            "counting_method": (
                "每个 active 目击实体（sighting）计为一只鸟；原始提交只作为证据不计入只数。"
                "adopted 表示已通过复核或多观察者/高可信自动采纳；pending 为尚未消解的实体，"
                "其与其它实体存在未决候选时可能在复核后合并下降。composition 逐条列出主证据（primary）"
                "与支撑证据（supporting），以及未决候选编号。"
            ),
            "composition": composition,
        }

    def trace_observation(self, observation_id: int) -> dict[str, Any]:
        """原始提交全链路追溯：当前归属、归并历史、相关候选、审计。"""
        row = self.connection.execute("SELECT * FROM bird_observations WHERE id=?", (observation_id,)).fetchone()
        if row is None:
            raise NotFoundError("原始提交不存在")
        observation = self._observation_dict(row)
        lineage = self.connection.execute("SELECT * FROM bird_lineage WHERE observation_id=? ORDER BY id", (observation_id,)).fetchall()
        candidates = self.connection.execute(
            "SELECT * FROM bird_candidates WHERE trigger_observation_a_id=? OR trigger_observation_b_id=? ORDER BY id",
            (observation_id, observation_id),
        ).fetchall()
        audits = self.connection.execute(
            "SELECT * FROM bird_audit WHERE (resource_type='observation' AND resource_id=?) OR request_id=? ORDER BY id",
            (str(observation_id), observation["request_id"] or ""),
        ).fetchall()
        return {
            "observation": observation,
            "current_sighting_id": observation["sighting_id"],
            "immutable": True,
            "lineage": [dict(item) for item in lineage],
            "candidates": [
                {"candidate_id": item["id"], "pair": item["pair_key"], "status": item["status"],
                 "score": item["score"], "reviewer": item["reviewer"], "decided_at": item["decided_at"]}
                for item in candidates
            ],
            "audit": [
                {"id": item["id"], "actor": item["actor"], "action": item["action"],
                 "resource_type": item["resource_type"], "resource_id": item["resource_id"],
                 "before": json.loads(item["before_json"] or "{}"),
                 "after": json.loads(item["after_json"] or "{}"),
                 "request_id": item["request_id"], "created_at": item["created_at"]}
                for item in audits
            ],
        }

    def list_audit(self, *, resource_type: str | None = None, resource_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM bird_audit"
        clauses: list[str] = []
        params: list[Any] = []
        if resource_type:
            clauses.append("resource_type=?")
            params.append(resource_type)
        if resource_id:
            clauses.append("resource_id=?")
            params.append(str(resource_id))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        rows = self.connection.execute(sql, params).fetchall()
        return [
            {"id": row["id"], "request_id": row["request_id"], "actor": row["actor"], "action": row["action"],
             "resource_type": row["resource_type"], "resource_id": row["resource_id"],
             "before": json.loads(row["before_json"] or "{}"),
             "after": json.loads(row["after_json"] or "{}"), "created_at": row["created_at"]}
            for row in rows
        ]
