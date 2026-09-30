from __future__ import annotations


def _obs(**overrides):
    payload = {
        "observer": "zhang",
        "observer_id": "O-001",
        "species": "雨燕",
        "observed_at": "2026-09-20T08:00:00+00:00",
        "location_name": "奥体森林公园南门",
        "latitude": 39.99,
        "longitude": 116.39,
        "photo_caption": "雨燕飞掠湖面",
        "evidence": [{"kind": "photo", "reference": "IMG-0001.jpg", "caption": "雨燕飞掠湖面"}],
        "confidence": 0.95,
        "count_hint": 1,
    }
    payload.update(overrides)
    return payload


def test_submit_and_trace_is_immutable(client):
    created = client.post("/api/birding/observations", json=_obs(request_id="req-1"))
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["replayed"] is False
    trace = client.get(f"/api/birding/observations/{body['observation_id']}/trace").json()
    assert trace["immutable"] is True
    assert trace["observation"]["species"] == "雨燕"
    assert trace["observation"]["evidence"][0]["reference"] == "IMG-0001.jpg"
    assert trace["current_sighting_id"] == body["sighting_id"]
    assert trace["lineage"][0]["reason"] == "create"


def test_retry_same_request_id_returns_same_result(client):
    payload = _obs(request_id="req-dup")
    first = client.post("/api/birding/observations", json=payload)
    second = client.post("/api/birding/observations", json=payload)
    assert first.status_code == second.status_code == 201
    assert second.json()["replayed"] is True
    assert first.json()["observation_id"] == second.json()["observation_id"]
    assert first.json()["sighting_id"] == second.json()["sighting_id"]

    # 同一 request_id 携带不同载荷必须被拒绝。
    conflict = client.post("/api/birding/observations", json=_obs(request_id="req-dup", observer="li"))
    assert conflict.status_code == 409


def test_identical_payload_without_request_id_is_natural_idempotent(client):
    payload = _obs()
    first = client.post("/api/birding/observations", json=payload).json()
    second = client.post("/api/birding/observations", json=payload).json()
    assert second["replayed"] is True
    assert second["replay_reason"] == "identical_payload"
    assert first["observation_id"] == second["observation_id"]


def test_conflicting_records_create_review_candidate(client):
    # 第一位观察者：高可信、明确时间地点。
    first = client.post("/api/birding/observations", json=_obs(request_id="a-1")).json()
    # 第二位观察者从另一个入口提交：时间偏移、地点偏移、照片说明不同、无共同佐证。
    second = client.post("/api/birding/observations", json=_obs(
        request_id="a-2", observer="wang", observer_id="O-002",
        observed_at="2026-09-20T08:40:00+00:00",
        latitude=39.992, longitude=116.395,
        photo_caption="一只深色小鸟停在芦苇上",
        evidence=[{"kind": "photo", "reference": "IMG-7788.jpg"}],
        confidence=0.4,
    )).json()

    # 没有自动合并，是两个实体。
    assert first["sighting_id"] != second["sighting_id"]
    candidates = client.get("/api/birding/candidates").json()["items"]
    assert len(candidates) == 1
    candidate = candidates[0]
    pair = {candidate["sightings"]["a"]["sighting_id"], candidate["sightings"]["b"]["sighting_id"]}
    assert pair == {first["sighting_id"], second["sighting_id"]}
    conflict_fields = {item["field"] for item in candidate["conflicts"]}
    assert "evidence" in conflict_fields
    assert candidate["challenges_adopted"] is True  # 首条高可信记录已被采纳


def test_low_confidence_record_cannot_displace_adopted_winner(client):
    first = client.post("/api/birding/observations", json=_obs(request_id="adopt-1", confidence=0.95)).json()
    second = client.post("/api/birding/observations", json=_obs(
        request_id="adopt-2", observer="wang", observer_id="O-002",
        photo_caption="雨燕在湖面低飞掠过",
        evidence=[{"kind": "photo", "reference": "IMG-9999.jpg"}],
        observed_at="2026-09-20T08:10:00+00:00",
        latitude=39.991, longitude=116.391, confidence=0.2,
    )).json()

    candidate_id = client.get("/api/birding/candidates").json()["items"][0]["id"]
    result = client.post(f"/api/birding/candidates/{candidate_id}/decision",
                         json={"decision": "merge", "reviewer": "reviewer-li"}).json()
    surviving = client.get(f"/api/birding/sightings/{result['resolution']['surviving_sighting_id']}").json()
    # 主证据仍是第一条高可信记录，低可信记录只是支撑证据。
    assert surviving["winner"]["observation_id"] == first["observation_id"]
    member_roles = {m["observation_id"]: m["review_status"] for m in surviving["members"]}
    assert member_roles[first["observation_id"]] == "confirmed"
    assert member_roles[second["observation_id"]] == "pending"

    # 被归档实体可重定向到存活实体。
    archived_id = result["resolution"]["archived_sighting_id"]
    archived = client.get(f"/api/birding/sightings/{archived_id}").json()
    assert archived["status"] == "archived"
    assert archived["merged_into_id"] == surviving["sighting_id"]


def test_keep_decision_preserves_two_birds(client):
    client.post("/api/birding/observations", json=_obs(request_id="k-1")).json()
    client.post("/api/birding/observations", json=_obs(
        request_id="k-2", observer="wang", observer_id="O-002",
        observed_at="2026-09-20T08:45:00+00:00",
        photo_caption="芦苇上的深色小鸟",
        evidence=[{"kind": "photo", "reference": "IMG-K2.jpg"}], confidence=0.5,
    )).json()
    candidate_id = client.get("/api/birding/candidates").json()["items"][0]["id"]
    decision = client.post(f"/api/birding/candidates/{candidate_id}/decision",
                           json={"decision": "keep", "reviewer": "r-1", "note": "确实两只"})
    assert decision.status_code == 200, decision.text
    stats = client.get("/api/birding/stats/window", params={"start": "2026-09-20T00:00:00+00:00",
                                                            "end": "2026-09-21T00:00:00+00:00"}).json()
    assert stats["bird_count"] == 2
    assert stats["adopted_count"] == 2

    # 对同一决定的重试必须幂等。
    replay = client.post(f"/api/birding/candidates/{candidate_id}/decision",
                         json={"decision": "keep", "reviewer": "r-1", "note": "确实两只"})
    assert replay.json()["replayed"] is True


def test_reject_decision_does_not_merge(client):
    client.post("/api/birding/observations", json=_obs(request_id="r-1")).json()
    client.post("/api/birding/observations", json=_obs(
        request_id="r-2", observer="wang", observer_id="O-002",
        observed_at="2026-09-20T08:45:00+00:00",
        photo_caption="疑似燕子", evidence=[{"kind": "photo", "reference": "IMG-R2.jpg"}],
        confidence=0.3,
    )).json()
    candidate_id = client.get("/api/birding/candidates").json()["items"][0]["id"]
    response = client.post(f"/api/birding/candidates/{candidate_id}/decision",
                           json={"decision": "reject", "reviewer": "r-2"})
    assert response.status_code == 200
    queue = client.get("/api/birding/candidates").json()["items"]
    assert queue == []
    rejected = client.get(f"/api/birding/candidates/{candidate_id}").json()
    assert rejected["status"] == "rejected"


def test_out_of_order_submission_still_detects_candidate(client):
    # 先到低可信记录，再到高可信记录。
    late = client.post("/api/birding/observations", json=_obs(
        request_id="oo-1", confidence=0.3, observer="wang", observer_id="O-002",
        observed_at="2026-09-20T08:40:00+00:00",
        photo_caption="深色小鸟停芦苇",
        evidence=[{"kind": "photo", "reference": "IMG-OO1.jpg"}],
    )).json()
    early = client.post("/api/birding/observations", json=_obs(request_id="oo-2")).json()
    candidates = client.get("/api/birding/candidates").json()["items"]
    assert len(candidates) == 1
    pair = {candidates[0]["sightings"]["a"]["sighting_id"], candidates[0]["sightings"]["b"]["sighting_id"]}
    assert pair == {late["sighting_id"], early["sighting_id"]}


def test_window_stats_explain_composition(client):
    first = client.post("/api/birding/observations", json=_obs(request_id="w-1")).json()
    second = client.post("/api/birding/observations", json=_obs(
        request_id="w-2", observer="wang", observer_id="O-002",
        observed_at="2026-09-20T08:30:00+00:00",
        photo_caption="雨燕掠过水面", evidence=[{"kind": "photo", "reference": "IMG-W2.jpg"}],
        confidence=0.5,
    )).json()

    stats = client.get("/api/birding/stats/window", params={
        "start": "2026-09-20T00:00:00+00:00", "end": "2026-09-21T00:00:00",
    }).json()
    assert stats["raw_submission_count"] == 2
    assert stats["bird_count"] == 2  # 未复核：疑似重复仍按两实体计
    assert stats["open_candidate_count"] == 1
    by_sighting = {item["sighting_id"]: item for item in stats["composition"]}
    pending_item = by_sighting[second["sighting_id"]]
    assert pending_item["open_candidate_ids"]
    assert pending_item["evidence"][0]["role"] == "primary"
    # 所有原始提交都在构成明细中可查。
    referenced = {e["observation_id"] for item in stats["composition"] for e in item["evidence"]}
    assert referenced == {first["observation_id"], second["observation_id"]}

    # 复核合并后计数下降到 1，但原始提交仍在构成中。
    candidate_id = pending_item["open_candidate_ids"][0]
    client.post(f"/api/birding/candidates/{candidate_id}/decision",
                json={"decision": "merge", "reviewer": "r-3"})
    stats_after = client.get("/api/birding/stats/window", params={
        "start": "2026-09-20T00:00:00+00:00", "end": "2026-09-21T00:00:00",
    }).json()
    assert stats_after["bird_count"] == 1
    assert stats_after["adopted_count"] == 1
    only = stats_after["composition"][0]
    assert only["member_count"] == 2
    assert {e["observation_id"] for e in only["evidence"]} == {first["observation_id"], second["observation_id"]}
    roles = {e["role"] for e in only["evidence"]}
    assert roles == {"primary", "supporting"}

    # reviewed_only 视图同样一致。
    reviewed = client.get("/api/birding/stats/window", params={
        "start": "2026-09-20T00:00:00+00:00", "end": "2026-09-21T00:00:00", "reviewed_only": True,
    }).json()
    assert reviewed["bird_count"] == 1


def test_shared_evidence_auto_matches(client):
    first = client.post("/api/birding/observations", json=_obs(request_id="s-1")).json()
    second = client.post("/api/birding/observations", json=_obs(
        request_id="s-2", observer="wang", observer_id="O-002",
        # 时间窗口内 + 坐标接近 + 相同佐证引用 => 1.0 分自动归并
        observed_at="2026-09-20T08:20:00+00:00",
        latitude=39.9905, longitude=116.3905,
        evidence=[{"kind": "photo", "reference": "IMG-0001.jpg"}],
        confidence=0.5,
    )).json()
    assert second["matched"] is True
    assert second["sighting_id"] == first["sighting_id"]
    assert client.get("/api/birding/candidates").json()["items"] == []


def test_audit_trail_records_every_decision(client):
    created = client.post("/api/birding/observations", json=_obs(request_id="au-1")).json()
    client.post("/api/birding/observations", json=_obs(
        request_id="au-2", observer="wang", observer_id="O-002",
        observed_at="2026-09-20T08:40:00+00:00",
        photo_caption="水面上的黑鸟", evidence=[{"kind": "photo", "reference": "IMG-AU2.jpg"}],
        confidence=0.4,
    ))
    candidate_id = client.get("/api/birding/candidates").json()["items"][0]["id"]
    client.post(f"/api/birding/candidates/{candidate_id}/decision",
                json={"decision": "merge", "reviewer": "auditor-q"})

    audit = client.get("/api/birding/audit", params={"limit": 50}).json()["items"]
    actions = [item["action"] for item in audit]
    assert "observation.submit" in actions
    assert "candidate.detected" in actions
    assert "candidate.merge" in actions
    assert "sighting.merge" in actions

    trace = client.get(f"/api/birding/observations/{created['observation_id']}/trace").json()
    assert any(item["action"] == "observation.submit" for item in trace["audit"])
    # 原始提交合并后仍可追溯到存活实体。
    sighting = client.get(f"/api/birding/sightings/{trace['current_sighting_id']}").json()
    assert sighting["status"] == "active"
    assert any(m["observation_id"] == created["observation_id"] for m in sighting["members"])


def test_rules_are_configurable(client):
    new_rules = client.put("/api/birding/rules?actor=admin-1", json={
        "time_window_minutes": 30,
        "distance_meters": 300,
        "score_threshold": 0.5,
        "auto_merge_threshold": 1.01,
    })
    assert new_rules.status_code == 200, new_rules.text
    # 相差 40 分钟、超 300 米 => 不再产生候选。
    client.post("/api/birding/observations", json=_obs(request_id="cfg-1"))
    client.post("/api/birding/observations", json=_obs(
        request_id="cfg-2", observer="wang", observer_id="O-002",
        observed_at="2026-09-20T08:40:00+00:00",
        latitude=40.0, longitude=116.41,
        photo_caption="另一只鸟", evidence=[{"kind": "photo", "reference": "IMG-CFG.jpg"}],
        confidence=0.5,
    ))
    assert client.get("/api/birding/candidates").json()["items"] == []
    audit = client.get("/api/birding/audit", params={"resource_type": "rule_config"}).json()["items"]
    assert any(item["action"] == "rules.update" for item in audit)


def test_different_species_never_matched(client):
    client.post("/api/birding/observations", json=_obs(request_id="sp-1"))
    client.post("/api/birding/observations", json=_obs(
        request_id="sp-2", species="家燕", observer="wang", observer_id="O-002",
        observed_at="2026-09-20T08:05:00+00:00",
        photo_caption="家燕停在电线上", evidence=[{"kind": "photo", "reference": "IMG-SP2.jpg"}],
    ))
    assert client.get("/api/birding/candidates").json()["items"] == []


def test_chain_merge_keeps_all_raw_submissions_traceable(client):
    a = client.post("/api/birding/observations", json=_obs(request_id="c-1"))
    assert a.status_code == 201, a.text
    a = a.json()
    b = client.post("/api/birding/observations", json=_obs(
        request_id="c-2", observer="wang", observer_id="O-002",
        observed_at="2026-09-20T08:25:00+00:00",
        photo_caption="雨燕掠过水面", evidence=[{"kind": "photo", "reference": "IMG-C2.jpg"}],
        confidence=0.5,
    )).json()
    c = client.post("/api/birding/observations", json=_obs(
        request_id="c-3", observer="chen", observer_id="O-003",
        observed_at="2026-09-20T08:50:00+00:00", latitude=39.993, longitude=116.394,
        photo_caption="湖边一只燕子", evidence=[{"kind": "photo", "reference": "IMG-C3.jpg"}],
        confidence=0.45,
    )).json()

    # 第一次合并 A、B。
    queue = client.get("/api/birding/candidates").json()["items"]
    first = next(x for x in queue if {x["sightings"]["a"]["sighting_id"], x["sightings"]["b"]["sighting_id"]} == {a["sighting_id"], b["sighting_id"]})
    merged1 = client.post(f"/api/birding/candidates/{first['id']}/decision",
                          json={"decision": "merge", "reviewer": "rv"}).json()
    survivor_id = merged1["resolution"]["surviving_sighting_id"]

    # 指向被归档组的候选应自动改挂到存活组（或标记 superseded），不会悬空。
    queue = client.get("/api/birding/candidates").json()["items"]
    assert queue, "A/B 与 C 的候选应仍在队列中"
    remaining = queue[0]
    pair = {remaining["sightings"]["a"]["sighting_id"], remaining["sightings"]["b"]["sighting_id"]}
    assert pair == {survivor_id, c["sighting_id"]}
    merged2 = client.post(f"/api/birding/candidates/{remaining['id']}/decision",
                          json={"decision": "merge", "reviewer": "rv"}).json()
    final_id = merged2["resolution"]["surviving_sighting_id"]

    stats = client.get("/api/birding/stats/window", params={
        "start": "2026-09-20T00:00:00+00:00", "end": "2026-09-21T00:00:00",
    }).json()
    assert stats["bird_count"] == 1
    assert stats["composition"][0]["member_count"] == 3
    assert {e["observation_id"] for e in stats["composition"][0]["evidence"]} == {
        a["observation_id"], b["observation_id"], c["observation_id"],
    }

    # 每条原始提交都能追溯到最终实体，且原始行内容未被修改。
    for submitted in (a, b, c):
        trace = client.get(f"/api/birding/observations/{submitted['observation_id']}/trace").json()
        assert trace["current_sighting_id"] == final_id
        assert trace["immutable"] is True
        assert [step["reason"] for step in trace["lineage"]][0] == "create"
        assert trace["observation"]["request_id"]


def test_auto_match_does_not_replace_winner(client):
    first = client.post("/api/birding/observations", json=_obs(request_id="am-1", confidence=0.95)).json()
    second = client.post("/api/birding/observations", json=_obs(
        request_id="am-2", observer="wang", observer_id="O-002",
        observed_at="2026-09-20T08:05:00+00:00",
        latitude=39.9902, longitude=116.3902,
        photo_caption="同一张图",
        evidence=[{"kind": "photo", "reference": "IMG-0001.jpg"}],
        confidence=0.1,
    )).json()
    assert second["matched"] is True
    sighting = client.get(f"/api/birding/sightings/{first['sighting_id']}").json()
    assert sighting["winner"]["observation_id"] == first["observation_id"]

