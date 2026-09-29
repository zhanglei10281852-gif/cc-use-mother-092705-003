from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.database import get_connection, transaction
from app.core.errors import ConflictError

SCHEMA = """
CREATE TABLE IF NOT EXISTS bird_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_ref TEXT NOT NULL DEFAULT '',
    observer TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    location_name TEXT NOT NULL DEFAULT '',
    species_code TEXT NOT NULL,
    species_name TEXT NOT NULL DEFAULT '',
    individual_count INTEGER NOT NULL DEFAULT 1,
    confidence REAL NOT NULL DEFAULT 0.5,
    behavior TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','merged','rejected')),
    cluster_id INTEGER,
    reject_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bird_obs_client_ref
    ON bird_observations(client_ref) WHERE client_ref <> '';
CREATE INDEX IF NOT EXISTS idx_bird_obs_time ON bird_observations(observed_at);
CREATE INDEX IF NOT EXISTS idx_bird_obs_cluster ON bird_observations(cluster_id);

CREATE TABLE IF NOT EXISTS bird_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    primary_observation_id INTEGER NOT NULL REFERENCES bird_observations(id),
    species_code TEXT NOT NULL,
    species_name TEXT NOT NULL DEFAULT '',
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    observed_at TEXT NOT NULL,
    observer TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    individual_count INTEGER NOT NULL DEFAULT 1,
    member_count INTEGER NOT NULL DEFAULT 1,
    adopted INTEGER NOT NULL DEFAULT 1 CHECK(adopted IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bird_clusters_time ON bird_clusters(observed_at);
CREATE INDEX IF NOT EXISTS idx_bird_clusters_species ON bird_clusters(species_code, observed_at);

CREATE TABLE IF NOT EXISTS bird_duplicate_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id INTEGER NOT NULL REFERENCES bird_clusters(id),
    observation_id INTEGER NOT NULL REFERENCES bird_observations(id),
    score REAL NOT NULL,
    score_breakdown_json TEXT NOT NULL DEFAULT '{}',
    rule_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','merged','kept_separate','rejected','cancelled')),
    reviewer TEXT NOT NULL DEFAULT '',
    review_note TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(cluster_id, observation_id)
);
CREATE INDEX IF NOT EXISTS idx_bird_cand_status
    ON bird_duplicate_candidates(status, created_at);
CREATE INDEX IF NOT EXISTS idx_bird_cand_obs ON bird_duplicate_candidates(observation_id);

CREATE TABLE IF NOT EXISTS bird_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    observation_ids TEXT NOT NULL DEFAULT '[]',
    cluster_id INTEGER,
    candidate_id INTEGER,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bird_audit_cluster ON bird_audit_log(cluster_id);
CREATE INDEX IF NOT EXISTS idx_bird_audit_obs ON bird_audit_log(observation_ids);

CREATE TABLE IF NOT EXISTS bird_rule_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL UNIQUE,
    config_json TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

DEFAULT_RULES: dict[str, float] = {
    "time_window_minutes": 120.0,
    "distance_meters": 500.0,
    "auto_merge_threshold": 0.85,
    "review_threshold": 0.45,
    "same_species_bonus": 0.35,
    "species_conflict_penalty": 0.4,
    "evidence_bonus": 0.2,
    "observer_repeat_bonus": 0.1,
}

RULE_VERSION_PREFIX = "rules-v"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def parse_observed_at(value: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def payload_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class BirdEvidenceService:
    """鸟类观察证据整理：去重识别、复核队列与证据化计数。"""

    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_schema()
        self._bootstrap_rules()

    # ------------------------------------------------------------------ rules

    def _bootstrap_rules(self) -> None:
        row = self.connection.execute("SELECT MAX(version) AS v FROM bird_rule_configs").fetchone()
        if row["v"] is None:
            with transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO bird_rule_configs(version,config_json,changed_by,created_at) VALUES(?,?,?,?)",
                    (1, json.dumps(DEFAULT_RULES, ensure_ascii=False), "system", _now()),
                )

    def current_rules(self) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM bird_rule_configs ORDER BY version DESC LIMIT 1"
        ).fetchone()
        config = json.loads(row["config_json"])
        return {"version": row["version"], "rule_version": f"{RULE_VERSION_PREFIX}{row['version']}",
                "changed_by": row["changed_by"], "created_at": row["created_at"], "config": config}

    def update_rules(self, changes: dict[str, Any], actor: str) -> dict[str, Any]:
        changes = {key: value for key, value in changes.items() if value is not None}
        if not changes:
            return self.current_rules()
        with transaction(immediate=True) as connection:
            latest = connection.execute(
                "SELECT * FROM bird_rule_configs ORDER BY version DESC LIMIT 1"
            ).fetchone()
            config = json.loads(latest["config_json"])
            old_config = dict(config)
            config.update(changes)
            if config["review_threshold"] > config["auto_merge_threshold"]:
                raise ValueError("review_threshold 不能高于 auto_merge_threshold")
            next_version = int(latest["version"]) + 1
            connection.execute(
                "INSERT INTO bird_rule_configs(version,config_json,changed_by,created_at) VALUES(?,?,?,?)",
                (next_version, json.dumps(config, ensure_ascii=False), actor, _now()),
            )
            self._audit(connection, "rules.update", actor, before=old_config,
                        after={"version": next_version, "config": config})
        return self.current_rules()

    # ------------------------------------------------------------ submission

    def submit_observation(self, payload: dict[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        """提交一条观察；乱序、重复安全，重试返回同一处理结果。"""
        actor = actor or payload.get("observer") or "observer"
        evidence = payload.get("evidence") or []
        observed_dt = parse_observed_at(payload["observed_at"])
        observed_storage = observed_dt.isoformat(timespec="seconds")
        canonical = {
            "observer": payload["observer"],
            "observed_at": payload["observed_at"],
            "latitude": payload["latitude"],
            "longitude": payload["longitude"],
            "location_name": payload.get("location_name", ""),
            "species_code": payload["species_code"],
            "species_name": payload.get("species_name", ""),
            "count": payload.get("count", 1),
            "confidence": payload.get("confidence", 0.5),
            "behavior": payload.get("behavior", ""),
            "evidence": [dict(item) for item in evidence],
            "note": payload.get("note", ""),
        }
        digest = payload_fingerprint(canonical)
        client_ref = (payload.get("idempotency_key") or "").strip()

        with transaction(immediate=True) as connection:
            if client_ref:
                existing = connection.execute(
                    "SELECT * FROM bird_observations WHERE client_ref=?", (client_ref,)
                ).fetchone()
                if existing:
                    if existing["payload_hash"] != digest:
                        raise ConflictError(
                            "同一幂等键不能用于不同的观察提交",
                            context={"idempotency_key": client_ref,
                                     "existing_observation_id": existing["id"]},
                        )
                    return self._observation_result(connection, existing, replayed=True)
            # 无幂等键时，同观察者同载荷指纹也视为同一提交的重试
            duplicate = connection.execute(
                "SELECT * FROM bird_observations WHERE payload_hash=? AND observer=? "
                "ORDER BY id LIMIT 1",
                (digest, canonical["observer"]),
            ).fetchone()
            if duplicate:
                return self._observation_result(connection, duplicate, replayed=True)

            now = _now()
            cursor = connection.execute(
                "INSERT INTO bird_observations(client_ref,observer,observed_at,latitude,longitude,"
                "location_name,species_code,species_name,individual_count,confidence,behavior,"
                "evidence_json,note,payload_hash,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active', ?, ?)",
                (client_ref, canonical["observer"], observed_storage, canonical["latitude"],
                 canonical["longitude"], canonical["location_name"], canonical["species_code"],
                 canonical["species_name"], canonical["count"], canonical["confidence"],
                 canonical["behavior"], json.dumps(canonical["evidence"], ensure_ascii=False),
                 canonical["note"], digest, now, now),
            )
            observation = connection.execute(
                "SELECT * FROM bird_observations WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            self._audit(connection, "observation.submit", actor,
                        observation_ids=[observation["id"]],
                        after={"id": observation["id"], "payload": canonical})
            outcome = self._integrate(connection, observation)
            observation = connection.execute(
                "SELECT * FROM bird_observations WHERE id=?", (observation["id"],)
            ).fetchone()
            result = self._observation_result(connection, observation)
            result["dedup"] = outcome
            return result

    def _observation_result(self, connection: sqlite3.Connection, row: sqlite3.Row,
                            *, replayed: bool = False) -> dict[str, Any]:
        result = self._serialize_observation(row)
        result["replayed"] = replayed
        result["cluster_id"] = row["cluster_id"]
        result["status"] = row["status"]
        return result

    # ------------------------------------------------------------- matching

    def _score(self, obs: sqlite3.Row, cluster: dict[str, Any], rules: dict[str, float],
               members: list[sqlite3.Row] | None = None) -> tuple[float, dict[str, Any]]:
        observed_dt = parse_observed_at(obs["observed_at"])
        cluster_dt = parse_observed_at(cluster["observed_at"])
        # 乱序提交时合并体锚点可能离新记录很远：取与全体成员的最小时间差、距离
        member_times = [cluster_dt]
        member_points = [(float(cluster["latitude"]), float(cluster["longitude"]))]
        for member in members or []:
            if member["status"] == "rejected":
                continue
            member_times.append(parse_observed_at(member["observed_at"]))
            member_points.append((float(member["latitude"]), float(member["longitude"])))
        minutes = min(abs((observed_dt - dt).total_seconds()) / 60.0 for dt in member_times)
        meters = min(haversine_meters(obs["latitude"], obs["longitude"], lat, lon)
                     for lat, lon in member_points)
        time_limit = float(rules["time_window_minutes"])
        distance_limit = float(rules["distance_meters"])

        breakdown: dict[str, Any] = {
            "time_gap_minutes": round(minutes, 2),
            "distance_meters": round(meters, 2),
            "time_limit_minutes": time_limit,
            "distance_limit_meters": distance_limit,
        }
        if minutes > time_limit or meters > distance_limit:
            breakdown["eligible"] = False
            breakdown["reasons"] = self._gate_reasons(minutes, meters, time_limit, distance_limit)
            return 0.0, breakdown
        breakdown["eligible"] = True

        time_proximity = max(0.0, 1.0 - minutes / time_limit)
        space_proximity = max(0.0, 1.0 - meters / distance_limit)
        score = 0.45 * time_proximity + 0.45 * space_proximity

        same_species = obs["species_code"].strip().lower() == str(cluster["species_code"]).strip().lower()
        if not same_species:
            same_species = any(
                obs["species_code"].strip().lower() == m["species_code"].strip().lower()
                for m in (members or []) if m["status"] != "rejected"
            )
        if same_species:
            score += float(rules["same_species_bonus"])
            breakdown["species"] = "same"
        else:
            score -= float(rules["species_conflict_penalty"])
            breakdown["species"] = "conflict"

        evidence = json.loads(obs["evidence_json"] or "[]")
        cluster_evidence = self._evidence_list(cluster.get("evidence_json", "[]"))
        if evidence:
            score += float(rules["evidence_bonus"])
            breakdown["evidence"] = f"{len(evidence)} refs"
        same_observer = obs["observer"] == cluster.get("observer") or any(
            obs["observer"] == m["observer"] for m in (members or []) if m["status"] != "rejected"
        )
        if same_observer:
            score += float(rules["observer_repeat_bonus"])
            breakdown["same_observer"] = True
        breakdown["time_proximity"] = round(time_proximity, 4)
        breakdown["space_proximity"] = round(space_proximity, 4)
        breakdown["cluster_evidence_count"] = len(cluster_evidence)
        breakdown["score_before_clip"] = round(score, 4)
        return round(max(0.0, min(1.0, score)), 4), breakdown

    @staticmethod
    def _gate_reasons(minutes: float, meters: float, time_limit: float, distance_limit: float) -> list[str]:
        reasons: list[str] = []
        if minutes > time_limit:
            reasons.append("超出时间窗")
        if meters > distance_limit:
            reasons.append("超出距离半径")
        return reasons

    def _candidate_clusters(self, connection: sqlite3.Connection, obs: sqlite3.Row,
                            rules: dict[str, float]) -> list[tuple[sqlite3.Row, list[sqlite3.Row]]]:
        observed_dt = parse_observed_at(obs["observed_at"])
        start = (observed_dt - timedelta(minutes=float(rules["time_window_minutes"]))).isoformat(timespec="seconds")
        end = (observed_dt + timedelta(minutes=float(rules["time_window_minutes"]))).isoformat(timespec="seconds")
        rows = connection.execute(
            "SELECT DISTINCT c.* FROM bird_clusters c WHERE c.adopted=1 AND ("
            "c.observed_at BETWEEN ? AND ? OR EXISTS ("
            "SELECT 1 FROM bird_observations o WHERE o.cluster_id=c.id "
            "AND o.status<>'rejected' AND o.observed_at BETWEEN ? AND ?))",
            (start, end, start, end),
        ).fetchall()
        result = []
        for cluster in rows:
            members = connection.execute(
                "SELECT * FROM bird_observations WHERE cluster_id=? AND status<>'rejected' ORDER BY id",
                (cluster["id"],),
            ).fetchall()
            result.append((cluster, members))
        return result

    def _integrate(self, connection: sqlite3.Connection, obs: sqlite3.Row) -> dict[str, Any]:
        rules_info = self._current_rules_row(connection)
        rules = json.loads(rules_info["config_json"])
        rule_version = f"{RULE_VERSION_PREFIX}{rules_info['version']}"

        matches: list[tuple[float, dict[str, Any], sqlite3.Row]] = []
        for cluster, members in self._candidate_clusters(connection, obs, rules):
            score, breakdown = self._score(obs, dict(cluster), rules, members)
            if breakdown.get("eligible"):
                matches.append((score, breakdown, cluster))
        matches.sort(key=lambda item: item[0], reverse=True)

        if not matches:
            cluster_id = self._create_cluster(connection, obs)
            self._audit(connection, "cluster.create", obs["observer"],
                        observation_ids=[obs["id"]], cluster_id=cluster_id,
                        after={"cluster_id": cluster_id, "reason": "no_candidate"})
            return {"action": "new_cluster", "cluster_id": cluster_id, "candidates": []}

        best_score, best_breakdown, best_cluster = matches[0]
        candidate_rows: list[dict[str, Any]] = []
        for score, breakdown, cluster in matches[:5]:
            if score < float(rules["review_threshold"]):
                continue
            cursor = connection.execute(
                "INSERT INTO bird_duplicate_candidates"
                "(cluster_id,observation_id,score,score_breakdown_json,rule_version,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(cluster_id,observation_id) DO UPDATE SET "
                "score=excluded.score, score_breakdown_json=excluded.score_breakdown_json, "
                "rule_version=excluded.rule_version, updated_at=excluded.updated_at "
                "WHERE bird_duplicate_candidates.status='pending'",
                (cluster["id"], obs["id"], score, json.dumps(breakdown, ensure_ascii=False),
                 rule_version, _now(), _now()),
            )
            candidate_rows.append({
                "cluster_id": cluster["id"], "score": score, "breakdown": breakdown,
            })

        if best_score >= float(rules["auto_merge_threshold"]):
            self._merge_into_cluster(connection, obs, best_cluster, mode="auto",
                                     score=best_score, rule_version=rule_version)
            connection.execute(
                "UPDATE bird_duplicate_candidates SET status='merged', reviewer='auto', "
                "review_note='达到自动合并阈值', reviewed_at=?, updated_at=? "
                "WHERE observation_id=? AND cluster_id=?",
                (_now(), _now(), obs["id"], best_cluster["id"]),
            )
            connection.execute(
                "UPDATE bird_duplicate_candidates SET status='cancelled', updated_at=? "
                "WHERE observation_id=? AND status='pending'",
                (_now(), obs["id"]),
            )
            return {"action": "auto_merged", "cluster_id": best_cluster["id"],
                    "score": best_score, "candidates": candidate_rows}

        # 中等相似度：进入复核队列；保持独立计数（暂不并入），等待人工决定
        if candidate_rows:
            self._audit(connection, "candidate.flag", obs["observer"],
                        observation_ids=[obs["id"]], cluster_id=best_cluster["id"],
                        after={"candidates": candidate_rows})
            cluster_id = self._create_cluster(connection, obs)
            return {"action": "review_pending", "cluster_id": cluster_id,
                    "candidate_cluster_id": best_cluster["id"],
                    "score": best_score, "candidates": candidate_rows}

        cluster_id = self._create_cluster(connection, obs)
        return {"action": "new_cluster", "cluster_id": cluster_id,
                "best_score": best_score, "candidates": []}

    # ------------------------------------------------------------ clusters

    @staticmethod
    def _evidence_list(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, str):
            return json.loads(value or "[]")
        return list(value or [])

    def _create_cluster(self, connection: sqlite3.Connection, obs: sqlite3.Row) -> int:
        now = _now()
        cursor = connection.execute(
            "INSERT INTO bird_clusters(primary_observation_id,species_code,species_name,latitude,"
            "longitude,observed_at,observer,confidence,evidence_json,individual_count,member_count,"
            "adopted,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (obs["id"], obs["species_code"], obs["species_name"], obs["latitude"], obs["longitude"],
             obs["observed_at"], obs["observer"], obs["confidence"], obs["evidence_json"],
             obs["individual_count"], 1, now, now),
        )
        cluster_id = cursor.lastrowid
        connection.execute(
            "UPDATE bird_observations SET cluster_id=?, updated_at=? WHERE id=?",
            (cluster_id, now, obs["id"]),
        )
        return cluster_id

    def _merge_into_cluster(self, connection: sqlite3.Connection, obs: sqlite3.Row,
                            cluster: sqlite3.Row, *, mode: str, score: float = 0.0,
                            rule_version: str = "") -> None:
        now = _now()
        before = dict(cluster)
        evidence = self._evidence_list(cluster["evidence_json"])
        incoming = self._evidence_list(obs["evidence_json"])
        known = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in evidence}
        for item in incoming:
            key = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if key not in known:
                evidence.append(item)
                known.add(key)

        primary = connection.execute(
            "SELECT * FROM bird_observations WHERE id=?", (cluster["primary_observation_id"],)
        ).fetchone()
        confidence_upgraded = False
        species_code, species_name = cluster["species_code"], cluster["species_name"]
        confidence = float(cluster["confidence"])
        primary_id = cluster["primary_observation_id"]
        # 关键保护：自动合并的后到记录不能顶替已采用的主观察；
        # 只有人工合并且新证据明显更可信时才更新代表字段（主观察本身仍完整保留、可追溯）。
        if mode == "manual" and float(obs["confidence"]) > float(primary["confidence"]):
            confidence_upgraded = True
            confidence = float(obs["confidence"])
            primary_id = obs["id"]
            if obs["species_code"].strip().lower() != cluster["species_code"].strip().lower():
                species_code, species_name = obs["species_code"], obs["species_name"]

        connection.execute(
            "UPDATE bird_clusters SET evidence_json=?, member_count=member_count+1, "
            "species_code=?, species_name=?, confidence=?, primary_observation_id=?, updated_at=? "
            "WHERE id=?",
            (json.dumps(evidence, ensure_ascii=False), species_code, species_name, confidence,
             primary_id, now, cluster["id"]),
        )
        if confidence_upgraded:
            # 新记录被提升为代表观察：旧主观察降为成员，但原始行仍完整保留、可追溯
            connection.execute(
                "UPDATE bird_observations SET status='merged', updated_at=? WHERE id=? AND status='active'",
                (now, primary["id"]),
            )
            connection.execute(
                "UPDATE bird_observations SET status='active', cluster_id=?, updated_at=? WHERE id=?",
                (cluster["id"], now, obs["id"]),
            )
        else:
            connection.execute(
                "UPDATE bird_observations SET status='merged', cluster_id=?, updated_at=? WHERE id=?",
                (cluster["id"], now, obs["id"]),
            )
        connection.execute(
            "UPDATE bird_duplicate_candidates SET status='cancelled', updated_at=? "
            "WHERE observation_id=? AND status='pending' AND cluster_id<>?",
            (now, obs["id"], cluster["id"]),
        )
        self._audit(connection, f"cluster.merge:{mode}", obs["observer"],
                    observation_ids=[obs["id"], primary_id], cluster_id=cluster["id"],
                    before=before,
                    after={"score": score, "rule_version": rule_version,
                           "confidence_upgraded": confidence_upgraded,
                           "evidence_count": len(evidence)})

    # ------------------------------------------------------------- review

    def list_candidates(self, status: str | None = "pending", limit: int = 100) -> dict[str, Any]:
        items = []
        if status is None:
            query, params = (
                "SELECT c.*, cl.species_code AS cluster_species_code, "
                "cl.species_name AS cluster_species_name, cl.observed_at AS cluster_observed_at, "
                "cl.observer AS cluster_observer, cl.latitude AS cluster_latitude, "
                "cl.longitude AS cluster_longitude, cl.member_count AS cluster_member_count "
                "FROM bird_duplicate_candidates c "
                "JOIN bird_clusters cl ON cl.id=c.cluster_id "
                "ORDER BY c.score DESC, c.id LIMIT ?",
                (limit,),
            )
        else:
            query, params = (
                "SELECT c.*, cl.species_code AS cluster_species_code, "
                "cl.species_name AS cluster_species_name, cl.observed_at AS cluster_observed_at, "
                "cl.observer AS cluster_observer, cl.latitude AS cluster_latitude, "
                "cl.longitude AS cluster_longitude, cl.member_count AS cluster_member_count "
                "FROM bird_duplicate_candidates c "
                "JOIN bird_clusters cl ON cl.id=c.cluster_id "
                "WHERE c.status=? ORDER BY c.score DESC, c.id LIMIT ?",
                (status, limit),
            )
        rows = self.connection.execute(query, params).fetchall()
        for row in rows:
            obs = self.connection.execute(
                "SELECT * FROM bird_observations WHERE id=?", (row["observation_id"],)
            ).fetchone()
            items.append({
                "id": row["id"],
                "cluster_id": row["cluster_id"],
                "observation_id": row["observation_id"],
                "score": row["score"],
                "rule_version": row["rule_version"],
                "status": row["status"],
                "reviewer": row["reviewer"],
                "review_note": row["review_note"],
                "reviewed_at": row["reviewed_at"],
                "created_at": row["created_at"],
                "score_breakdown": json.loads(row["score_breakdown_json"] or "{}"),
                "cluster": {
                    "id": row["cluster_id"],
                    "species_code": row["cluster_species_code"],
                    "species_name": row["cluster_species_name"],
                    "observed_at": row["cluster_observed_at"],
                    "observer": row["cluster_observer"],
                    "latitude": row["cluster_latitude"],
                    "longitude": row["cluster_longitude"],
                    "member_count": row["cluster_member_count"],
                },
                "observation": self._serialize_observation(obs) if obs else None,
            })
        return {"items": items, "count": len(items)}

    def review_candidate(self, candidate_id: int, decision: str, reviewer: str, note: str = "") -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            candidate = connection.execute(
                "SELECT * FROM bird_duplicate_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            if candidate is None:
                raise KeyError("candidate_not_found")
            if candidate["status"] != "pending":
                # 幂等：相同决定重复提交返回当前状态，不产生第二次副作用
                return self._candidate_detail(connection, candidate,
                                              replayed=True, message="候选已复核")
            obs = connection.execute(
                "SELECT * FROM bird_observations WHERE id=?", (candidate["observation_id"],)
            ).fetchone()
            if obs is None:
                raise KeyError("observation_not_found")
            target_cluster = connection.execute(
                "SELECT * FROM bird_clusters WHERE id=?", (candidate["cluster_id"],)
            ).fetchone()
            now = _now()

            if decision == "merge":
                if obs["cluster_id"] and obs["cluster_id"] != target_cluster["id"]:
                    self._collapse_cluster(connection, obs["cluster_id"], target_cluster,
                                           reviewer=reviewer, note=note, candidate=candidate)
                else:
                    self._merge_into_cluster(connection, obs, target_cluster, mode="manual",
                                             score=float(candidate["score"]),
                                             rule_version=candidate["rule_version"])
                new_status = "merged"
            elif decision == "keep":
                new_status = "kept_separate"
                self._audit(connection, "candidate.keep_separate", reviewer,
                            observation_ids=[obs["id"]], cluster_id=target_cluster["id"],
                            candidate_id=candidate_id, after={"note": note})
            elif decision == "reject":
                new_status = "rejected"
                self._reject_observation(connection, obs, reviewer=reviewer, note=note,
                                         candidate=candidate, target_cluster=target_cluster)
            else:
                raise ValueError("decision 必须是 merge、keep 或 reject")

            connection.execute(
                "UPDATE bird_duplicate_candidates SET status=?, reviewer=?, review_note=?, "
                "reviewed_at=?, updated_at=? WHERE id=?",
                (new_status, reviewer, note, now, now, candidate_id),
            )
            connection.execute(
                "UPDATE bird_duplicate_candidates SET status='cancelled', updated_at=? "
                "WHERE observation_id=? AND status='pending' AND id<>?",
                (now, obs["id"], candidate_id),
            )
            candidate = connection.execute(
                "SELECT * FROM bird_duplicate_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            return self._candidate_detail(connection, candidate)

    def _collapse_cluster(self, connection: sqlite3.Connection, source_cluster_id: int,
                          target_cluster: sqlite3.Row, *, reviewer: str, note: str,
                          candidate: sqlite3.Row) -> None:
        """人工把两个已存在的合并体判为同一只鸟：保留目标合并体，归并成员与证据。"""
        source = connection.execute(
            "SELECT * FROM bird_clusters WHERE id=?", (source_cluster_id,)
        ).fetchone()
        if source is None:
            return
        members = connection.execute(
            "SELECT * FROM bird_observations WHERE cluster_id=? AND id<>? ORDER BY id",
            (source_cluster_id, target_cluster["primary_observation_id"]),
        ).fetchall()
        # 先把来源主观察并入，再并入其余成员
        source_primary = connection.execute(
            "SELECT * FROM bird_observations WHERE id=?", (source["primary_observation_id"],)
        ).fetchone()
        if source_primary and source_primary["id"] != target_cluster["primary_observation_id"]:
            self._merge_into_cluster(connection, source_primary, target_cluster, mode="manual",
                                     score=float(candidate["score"]),
                                     rule_version=candidate["rule_version"])
        for member in members:
            if member["id"] == source["primary_observation_id"]:
                continue
            if member["status"] == "rejected":
                continue
            latest_target = connection.execute(
                "SELECT * FROM bird_clusters WHERE id=?", (target_cluster["id"],)
            ).fetchone()
            self._merge_into_cluster(connection, member, latest_target, mode="manual",
                                     score=float(candidate["score"]),
                                     rule_version=candidate["rule_version"])
        # 来源合并体失效：其全部成员已归并到目标合并体
        connection.execute(
            "UPDATE bird_clusters SET adopted=0, updated_at=? WHERE id=?",
            (_now(), source_cluster_id),
        )
        connection.execute(
            "UPDATE bird_duplicate_candidates SET status='cancelled', updated_at=? "
            "WHERE cluster_id=? AND status='pending'",
            (_now(), source_cluster_id),
        )
        self._audit(connection, "cluster.collapse", reviewer,
                    cluster_id=target_cluster["id"],
                    before={"source_cluster_id": source_cluster_id},
                    after={"target_cluster_id": target_cluster["id"], "note": note})

    def _reject_observation(self, connection: sqlite3.Connection, obs: sqlite3.Row, *,
                            reviewer: str, note: str, candidate: sqlite3.Row,
                            target_cluster: sqlite3.Row) -> None:
        """否决候选关系：该记录判为无效，不计入统计，但原始提交完整保留。"""
        now = _now()
        before = {"status": obs["status"], "cluster_id": obs["cluster_id"]}
        own_cluster_id = obs["cluster_id"]
        connection.execute(
            "UPDATE bird_observations SET status='rejected', reject_reason=?, updated_at=? WHERE id=?",
            (note or f"复核否决：与合并体 {target_cluster['id']} 的重复关系不成立且记录不可信", now, obs["id"]),
        )
        # 若被否决记录自己构成一个合并体：
        # - 体内还有其他有效成员 -> 把代表权转给可信度最高的成员，合并体继续有效
        # - 体内没有其他成员 -> 合并体失效（adopted=0），不参与计数
        if own_cluster_id:
            own_cluster = connection.execute(
                "SELECT * FROM bird_clusters WHERE id=?", (own_cluster_id,)
            ).fetchone()
            if own_cluster and own_cluster["primary_observation_id"] == obs["id"]:
                remaining = connection.execute(
                    "SELECT * FROM bird_observations WHERE cluster_id=? AND id<>? "
                    "AND status<>'rejected' ORDER BY confidence DESC, id LIMIT 1",
                    (own_cluster_id, obs["id"]),
                ).fetchone()
                if remaining is None:
                    connection.execute(
                        "UPDATE bird_clusters SET adopted=0, updated_at=? WHERE id=?",
                        (now, own_cluster_id),
                    )
                else:
                    connection.execute(
                        "UPDATE bird_clusters SET primary_observation_id=?, observer=?, "
                        "species_code=?, species_name=?, latitude=?, longitude=?, observed_at=?, "
                        "confidence=?, member_count=member_count-1, updated_at=? WHERE id=?",
                        (remaining["id"], remaining["observer"], remaining["species_code"],
                         remaining["species_name"], remaining["latitude"], remaining["longitude"],
                         remaining["observed_at"], remaining["confidence"], now, own_cluster_id),
                    )
                    connection.execute(
                        "UPDATE bird_observations SET status='active', updated_at=? WHERE id=?",
                        (now, remaining["id"]),
                    )
        self._audit(connection, "observation.reject", reviewer,
                    observation_ids=[obs["id"]], cluster_id=target_cluster["id"],
                    candidate_id=candidate["id"], before=before,
                    after={"reject_reason": note, "target_cluster_id": target_cluster["id"]})

    def _candidate_detail(self, connection: sqlite3.Connection, candidate: sqlite3.Row,
                          *, replayed: bool = False, message: str = "") -> dict[str, Any]:
        obs = connection.execute(
            "SELECT * FROM bird_observations WHERE id=?", (candidate["observation_id"],)
        ).fetchone()
        cluster = connection.execute(
            "SELECT * FROM bird_clusters WHERE id=?", (candidate["cluster_id"],)
        ).fetchone()
        return {
            "id": candidate["id"],
            "status": candidate["status"],
            "decision_recorded": candidate["status"] in {"merged", "kept_separate", "rejected"},
            "replayed": replayed,
            "message": message,
            "reviewer": candidate["reviewer"],
            "review_note": candidate["review_note"],
            "reviewed_at": candidate["reviewed_at"],
            "observation": self._serialize_observation(obs) if obs else None,
            "cluster": self._serialize_cluster(cluster) if cluster else None,
        }

    # ------------------------------------------------------------- queries

    def get_observation(self, observation_id: int) -> dict[str, Any] | None:
        obs = self.connection.execute(
            "SELECT * FROM bird_observations WHERE id=?", (observation_id,)
        ).fetchone()
        if obs is None:
            return None
        result = self._serialize_observation(obs)
        result["cluster"] = None
        if obs["cluster_id"]:
            cluster = self.connection.execute(
                "SELECT * FROM bird_clusters WHERE id=?", (obs["cluster_id"],)
            ).fetchone()
            result["cluster"] = self._serialize_cluster(cluster) if cluster else None
        result["audit_trail"] = self._observation_trail(observation_id)
        return result

    def get_cluster(self, cluster_id: int) -> dict[str, Any] | None:
        cluster = self.connection.execute(
            "SELECT * FROM bird_clusters WHERE id=?", (cluster_id,)
        ).fetchone()
        if cluster is None:
            return None
        result = self._serialize_cluster(cluster)
        members = self.connection.execute(
            "SELECT * FROM bird_observations WHERE cluster_id=? ORDER BY id", (cluster_id,)
        ).fetchall()
        result["members"] = [self._serialize_observation(row) for row in members]
        result["candidates"] = self.connection.execute(
            "SELECT id,observation_id,status,score,reviewer,reviewed_at FROM bird_duplicate_candidates "
            "WHERE cluster_id=? ORDER BY id", (cluster_id,)
        ).fetchall()
        result["candidates"] = [dict(row) for row in result["candidates"]]
        result["audit_trail"] = self.connection.execute(
            "SELECT * FROM bird_audit_log WHERE cluster_id=? ORDER BY id", (cluster_id,)
        ).fetchall()
        result["audit_trail"] = [self._serialize_audit(row) for row in result["audit_trail"]]
        return result

    def migration_window(self, start: str, end: str, *, species_code: str | None = None,
                         location_name: str | None = None) -> dict[str, Any]:
        start_dt = parse_observed_at(start)
        end_dt = parse_observed_at(end)
        if end_dt < start_dt:
            raise ValueError("window_end 早于 window_start")

        cluster_where = [
            "(c.observed_at BETWEEN ? AND ? OR EXISTS ("
            "SELECT 1 FROM bird_observations mo WHERE mo.cluster_id=c.id "
            "AND mo.status<>'rejected' AND mo.observed_at BETWEEN ? AND ?))"
        ]
        params: list[Any] = [start_dt.isoformat(timespec="seconds"), end_dt.isoformat(timespec="seconds"),
                             start_dt.isoformat(timespec="seconds"), end_dt.isoformat(timespec="seconds")]
        if species_code:
            cluster_where.append("LOWER(c.species_code)=LOWER(?)")
            params.append(species_code)
        if location_name:
            cluster_where.append(
                "EXISTS (SELECT 1 FROM bird_observations o WHERE o.cluster_id=c.id "
                "AND o.location_name=?)"
            )
            params.append(location_name)
        where_sql = " AND ".join(cluster_where)

        clusters = self.connection.execute(
            f"SELECT * FROM bird_clusters c WHERE {where_sql} ORDER BY c.observed_at, c.id",
            params,
        ).fetchall()

        adopted = [c for c in clusters if c["adopted"] == 1]
        rejected_cluster_ids = [c["id"] for c in clusters if c["adopted"] == 0]

        raw_params: list[Any] = [start_dt.isoformat(timespec="seconds"),
                                 end_dt.isoformat(timespec="seconds")]
        raw_where = ["o.observed_at BETWEEN ? AND ?"]
        if species_code:
            raw_where.append("LOWER(o.species_code)=LOWER(?)")
            raw_params.append(species_code)
        if location_name:
            raw_where.append("o.location_name=?")
            raw_params.append(location_name)
        raw_all = self.connection.execute(
            f"SELECT * FROM bird_observations o WHERE {' AND '.join(raw_where)} ORDER BY o.id",
            raw_params,
        ).fetchall()

        by_species: dict[str, dict[str, Any]] = {}
        cluster_items = []
        for cluster in adopted:
            members = self.connection.execute(
                "SELECT * FROM bird_observations WHERE cluster_id=? ORDER BY id", (cluster["id"],)
            ).fetchall()
            active_members = [m for m in members if m["status"] != "rejected"]
            entry = {
                "cluster_id": cluster["id"],
                "species_code": cluster["species_code"],
                "species_name": cluster["species_name"],
                "observed_at": cluster["observed_at"],
                "observer_count": len({m["observer"] for m in active_members}),
                "observers": sorted({m["observer"] for m in active_members}),
                "submission_count": len(active_members),
                "individual_count": int(cluster["individual_count"]),
                "evidence_count": len(self._evidence_list(cluster["evidence_json"])),
                "evidence": self._evidence_list(cluster["evidence_json"]),
                "primary_observation_id": cluster["primary_observation_id"],
                "member_observation_ids": [m["id"] for m in active_members],
            }
            cluster_items.append(entry)
            bucket = by_species.setdefault(cluster["species_code"], {
                "species_code": cluster["species_code"],
                "species_name": cluster["species_name"],
                "bird_count": 0, "submission_count": 0, "cluster_ids": [],
            })
            bucket["bird_count"] += 1
            bucket["submission_count"] += len(active_members)
            bucket["cluster_ids"].append(cluster["id"])

        rejected_obs = [o for o in raw_all if o["status"] == "rejected"]
        merged_obs = [o for o in raw_all if o["status"] == "merged"]
        active_obs = [o for o in raw_all if o["status"] == "active"]
        adopted_ids = {c["id"] for c in adopted}
        active_in_excluded = [o for o in active_obs if o["cluster_id"] not in adopted_ids]

        return {
            "window_start": start_dt.isoformat(timespec="seconds"),
            "window_end": end_dt.isoformat(timespec="seconds"),
            "filters": {"species_code": species_code, "location_name": location_name},
            "summary": {
                "bird_count": len(adopted),
                "adopted_cluster_count": len(adopted),
                "raw_submission_count": len(raw_all),
                "active_submission_count": len(active_obs),
                "merged_submission_count": len(merged_obs),
                "rejected_submission_count": len(rejected_obs),
                "excluded_cluster_count": len(clusters) - len(adopted),
                "active_in_excluded_clusters": len(active_in_excluded),
                "pending_review_count": self.connection.execute(
                    "SELECT COUNT(*) AS n FROM bird_duplicate_candidates WHERE status='pending'"
                ).fetchone()["n"],
            },
            "count_composition": {
                "method": "每个有效合并体代表同一只鸟的一次在窗出现；"
                          "合并状态的原始提交不重复计数，被否决的提交与失效合并体不计入",
                "bird_count_formula": "bird_count = adopted_cluster_count "
                                      "= active_submission_count - active_in_excluded_clusters；"
                                      "恒等校验：raw_submission_count = active + merged + rejected",
                "identity_check": {
                    "raw": len(raw_all),
                    "active": len(active_obs),
                    "merged": len(merged_obs),
                    "rejected": len(rejected_obs),
                    "balanced": len(raw_all) == len(active_obs) + len(merged_obs) + len(rejected_obs),
                },
                "by_species": sorted(by_species.values(), key=lambda item: item["species_code"]),
                "clusters": cluster_items,
                "excluded_cluster_ids": rejected_cluster_ids,
                "rejected_observation_ids": [o["id"] for o in rejected_obs],
            },
            "rule_version": self.current_rules()["rule_version"],
        }

    def audit_log(self, *, observation_id: int | None = None, cluster_id: int | None = None,
                  limit: int = 200) -> dict[str, Any]:
        sql = "SELECT * FROM bird_audit_log"
        conditions = []
        params: list[Any] = []
        if observation_id is not None:
            conditions.append("EXISTS(SELECT 1 FROM json_each(observation_ids) WHERE value=?)")
            params.append(observation_id)
        if cluster_id is not None:
            conditions.append("cluster_id=?")
            params.append(cluster_id)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(sql, params).fetchall()
        return {"items": [self._serialize_audit(row) for row in rows], "count": len(rows)}

    # ------------------------------------------------------------- helpers

    def _current_rules_row(self, connection: sqlite3.Connection) -> sqlite3.Row:
        return connection.execute(
            "SELECT * FROM bird_rule_configs ORDER BY version DESC LIMIT 1"
        ).fetchone()

    def _observation_trail(self, observation_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM bird_audit_log WHERE EXISTS "
            "(SELECT 1 FROM json_each(observation_ids) WHERE value=?) ORDER BY id",
            (observation_id,),
        ).fetchall()
        return [self._serialize_audit(row) for row in rows]

    @staticmethod
    def _serialize_observation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "observer": row["observer"],
            "observed_at": row["observed_at"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "location_name": row["location_name"],
            "species_code": row["species_code"],
            "species_name": row["species_name"],
            "count": row["individual_count"],
            "confidence": row["confidence"],
            "behavior": row["behavior"],
            "evidence": json.loads(row["evidence_json"] or "[]"),
            "note": row["note"],
            "status": row["status"],
            "reject_reason": row["reject_reason"],
            "cluster_id": row["cluster_id"],
            "client_ref": row["client_ref"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _serialize_cluster(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "primary_observation_id": row["primary_observation_id"],
            "species_code": row["species_code"],
            "species_name": row["species_name"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "observed_at": row["observed_at"],
            "observer": row["observer"],
            "confidence": row["confidence"],
            "individual_count": row["individual_count"],
            "member_count": row["member_count"],
            "adopted": bool(row["adopted"]),
            "evidence": json.loads(row["evidence_json"] or "[]"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _serialize_audit(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "action": row["action"],
            "actor": row["actor"],
            "observation_ids": json.loads(row["observation_ids"] or "[]"),
            "cluster_id": row["cluster_id"],
            "candidate_id": row["candidate_id"],
            "before": json.loads(row["before_json"] or "{}"),
            "after": json.loads(row["after_json"] or "{}"),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _audit(connection: sqlite3.Connection, action: str, actor: str, *,
               observation_ids: list[int] | None = None, cluster_id: int | None = None,
               candidate_id: int | None = None, before: Any | None = None,
               after: Any | None = None) -> None:
        connection.execute(
            "INSERT INTO bird_audit_log(action,actor,observation_ids,cluster_id,candidate_id,"
            "before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (action, actor or "system", json.dumps(observation_ids or []), cluster_id, candidate_id,
             json.dumps(before if before is not None else {}, ensure_ascii=False, default=str),
             json.dumps(after if after is not None else {}, ensure_ascii=False, default=str), _now()),
        )
