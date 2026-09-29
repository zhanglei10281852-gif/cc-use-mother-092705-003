from __future__ import annotations


def make_obs(**overrides):
    base = {
        "observer": "zhangsan",
        "observed_at": "2026-09-20T08:00:00+08:00",
        "latitude": 40.0,
        "longitude": 116.4,
        "location_name": "野鸭湖",
        "species_code": "ANAS_PLAT",
        "species_name": "绿头鸭",
        "confidence": 0.9,
        "evidence": [{"kind": "photo", "uri": "s3://feiyu/1.jpg", "caption": "雄鸟繁殖羽"}],
    }
    base.update(overrides)
    return base


def submit(client, **overrides):
    return client.post("/api/birding/observations", json=make_obs(**overrides))


def test_submit_creates_cluster_and_is_traceable(client):
    response = submit(client)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["id"] > 0
    assert data["cluster_id"] > 0
    assert data["status"] == "active"
    assert data["dedup"]["action"] == "new_cluster"

    detail = client.get(f"/api/birding/observations/{data['id']}").json()
    assert detail["evidence"][0]["uri"] == "s3://feiyu/1.jpg"
    assert len(detail["audit_trail"]) >= 1
    assert detail["audit_trail"][0]["action"] == "observation.submit"


def test_idempotent_retry_returns_same_result(client):
    payload = make_obs(idempotency_key="trip-001")
    first = client.post("/api/birding/observations", json=payload)
    second = client.post("/api/birding/observations", json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["replayed"] is True

    # 同键不同载荷必须报冲突
    conflict = client.post("/api/birding/observations",
                           json=make_obs(idempotency_key="trip-001", latitude=41.0))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"

    # 无幂等键、同观察者完全相同载荷也按重试处理
    same_payload = make_obs()  # 无幂等键、同观察者同载荷
    a = client.post("/api/birding/observations", json=same_payload)
    b = client.post("/api/birding/observations", json=same_payload)
    assert a.json()["id"] == b.json()["id"]
    assert b.json()["replayed"] is True


def test_near_identical_records_auto_merge_and_count_once(client):
    first = submit(client, observer="zhangsan", confidence=0.95).json()
    second = submit(client, observer="lisi", observed_at="2026-09-20T08:40:00+08:00",
                    latitude=40.0005, longitude=116.4005, confidence=0.9,
                    evidence=[{"kind": "photo", "uri": "s3://feiyu/2.jpg"}]).json()
    # 40 分钟、约 70 米、同物种、双证据 -> 自动合并
    assert second["dedup"]["action"] == "auto_merged"
    assert second["cluster_id"] == first["cluster_id"]
    assert second["status"] == "merged"

    cluster = client.get(f"/api/birding/clusters/{first['cluster_id']}").json()
    assert cluster["member_count"] == 2
    assert {item["uri"] for item in cluster["evidence"]} == {"s3://feiyu/1.jpg", "s3://feiyu/2.jpg"}

    window = client.get("/api/birding/migration-window",
                        params={"start": "2026-09-20T00:00:00+08:00",
                                "end": "2026-09-20T23:59:59+08:00"}).json()
    assert window["summary"]["bird_count"] == 1
    assert window["summary"]["raw_submission_count"] == 2
    comp = window["count_composition"]
    assert comp["identity_check"]["balanced"] is True
    assert comp["clusters"][0]["submission_count"] == 2
    assert comp["clusters"][0]["evidence_count"] == 2


def _seed_conflict_candidate(client):
    submit(client, confidence=0.95)
    conflict = submit(client, species_code="ANAS_ZONO", species_name="螺纹鸭",
                      observed_at="2026-09-20T08:30:00+08:00",
                      latitude=40.0002, longitude=116.4002, confidence=0.6)
    data = conflict.json()
    assert data["dedup"]["action"] == "review_pending"

    pending = client.get("/api/birding/candidates", params={"status": "pending"}).json()
    assert pending["count"] >= 1
    candidate = pending["items"][0]
    assert candidate["score_breakdown"]["species"] == "conflict"
    assert candidate["observation"]["species_code"] == "ANAS_ZONO"
    return candidate["id"]


def test_conflicting_species_enters_review_queue(client):
    assert _seed_conflict_candidate(client) > 0


def test_review_keep_separate_counts_two_birds(client):
    cid = _seed_conflict_candidate(client)
    result = client.post(f"/api/birding/candidates/{cid}/review",
                         json={"decision": "keep", "reviewer": "wangwu", "note": "羽色不同"})
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "kept_separate"

    # 重复复核是幂等的，不产生第二次副作用
    again = client.post(f"/api/birding/candidates/{cid}/review",
                        json={"decision": "keep", "reviewer": "wangwu"})
    assert again.json()["status"] == "kept_separate"
    assert again.json()["replayed"] is True

    window = client.get("/api/birding/migration-window",
                        params={"start": "2026-09-20T00:00:00+08:00",
                                "end": "2026-09-20T23:59:59+08:00"}).json()
    assert window["summary"]["bird_count"] == 2
    by_species = {item["species_code"]: item["bird_count"]
                  for item in window["count_composition"]["by_species"]}
    assert by_species == {"ANAS_PLAT": 1, "ANAS_ZONO": 1}


def test_review_merge_collapses_clients_and_preserves_audit(client):
    cid = _seed_conflict_candidate(client)
    result = client.post(f"/api/birding/candidates/{cid}/review",
                         json={"decision": "merge", "reviewer": "wangwu", "note": "确认为同一只"})
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "merged"

    window = client.get("/api/birding/migration-window",
                        params={"start": "2026-09-20T00:00:00+08:00",
                                "end": "2026-09-20T23:59:59+08:00"}).json()
    assert window["summary"]["bird_count"] == 1

    # 原始提交仍可追溯
    obs_id = body["observation"]["id"]
    detail = client.get(f"/api/birding/observations/{obs_id}").json()
    actions = [event["action"] for event in detail["audit_trail"]]
    assert "observation.submit" in actions
    assert any(action.startswith("cluster.merge") for action in actions)


def test_review_reject_excludes_low_credibility_but_keeps_raw(client):
    cid = _seed_conflict_candidate(client)
    result = client.post(f"/api/birding/candidates/{cid}/review",
                         json={"decision": "reject", "reviewer": "wangwu",
                               "note": "照片无法辨识，疑误报"})
    assert result.status_code == 200
    obs_id = result.json()["observation"]["id"]

    window = client.get("/api/birding/migration-window",
                        params={"start": "2026-09-20T00:00:00+08:00",
                                "end": "2026-09-20T23:59:59+08:00"}).json()
    assert window["summary"]["bird_count"] == 1
    assert window["summary"]["rejected_submission_count"] == 1
    assert obs_id in window["count_composition"]["rejected_observation_ids"]

    raw = client.get(f"/api/birding/observations/{obs_id}").json()
    assert raw["status"] == "rejected"
    assert raw["reject_reason"] == "照片无法辨识，疑误报"
    assert raw["evidence"][0]["uri"]  # 原始载荷完整保留

    audit = client.get("/api/birding/audit", params={"observation_id": obs_id}).json()
    assert any(item["action"] == "observation.reject" for item in audit["items"])


def test_reject_primary_promotes_remaining_member(client):
    # A 建立合并体；B 物种冲突进入复核队列并自成第二个合并体；
    # C 与 B 高度雷同自动并入 B 的合并体。随后复核人否决 B（候选关系），
    # B 的合并体应由 C 继承而非整体消失：A、C 各计一只鸟。
    submit(client, observed_at="2026-09-20T08:00:00+08:00", confidence=0.95)
    b = submit(client, species_code="ANAS_ZONO", species_name="螺纹鸭",
               observed_at="2026-09-20T08:30:00+08:00",
               latitude=40.001, longitude=116.401, confidence=0.6).json()
    assert b["dedup"]["action"] == "review_pending"
    b_cluster = b["cluster_id"]

    c = submit(client, observer="zhaoliu", species_code="ANAS_ZONO", species_name="螺纹鸭",
               observed_at="2026-09-20T08:35:00+08:00",
               latitude=40.0011, longitude=116.4011, confidence=0.9,
               evidence=[{"kind": "photo", "uri": "s3://feiyu/c.jpg"}]).json()
    assert c["dedup"]["action"] == "auto_merged"
    assert c["cluster_id"] == b_cluster

    pending = client.get("/api/birding/candidates", params={"status": "pending"}).json()
    candidate_id = next(item["id"] for item in pending["items"]
                        if item["observation_id"] == b["id"])
    result = client.post(f"/api/birding/candidates/{candidate_id}/review",
                         json={"decision": "reject", "reviewer": "wangwu",
                               "note": "B 为误报，C 独立成立"}).json()
    assert result["status"] == "rejected"

    cluster = client.get(f"/api/birding/clusters/{b_cluster}").json()
    assert cluster["adopted"] is True
    assert cluster["primary_observation_id"] == c["id"]

    window = client.get("/api/birding/migration-window",
                        params={"start": "2026-09-20T00:00:00+08:00",
                                "end": "2026-09-20T23:59:59+08:00"}).json()
    assert window["summary"]["bird_count"] == 2
    assert window["summary"]["rejected_submission_count"] == 1
    # 被否决的 B 原始提交仍可追溯
    raw_b = client.get(f"/api/birding/observations/{b['id']}").json()
    assert raw_b["status"] == "rejected"
    assert raw_b["evidence"]  # 原始载荷完整


def test_adopted_observation_survives_later_low_confidence_record(client):
    first = submit(client, confidence=0.95, evidence=[
        {"kind": "photo", "uri": "s3://feiyu/good.jpg"}]).json()
    # 后到一条低可信但极度雷同的记录，自动并入但不顶替
    later = submit(client, observer="newbie", confidence=0.1,
                   observed_at="2026-09-20T08:10:00+08:00",
                   latitude=40.0001, longitude=116.4001,
                   evidence=[]).json()
    assert later["dedup"]["action"] == "auto_merged"
    cluster = client.get(f"/api/birding/clusters/{first['cluster_id']}").json()
    assert cluster["primary_observation_id"] == first["id"]
    assert cluster["confidence"] == 0.95

    window = client.get("/api/birding/migration-window",
                        params={"start": "2026-09-20T00:00:00+08:00",
                                "end": "2026-09-20T23:59:59+08:00"}).json()
    assert window["summary"]["bird_count"] == 1
    primary = window["count_composition"]["clusters"][0]
    assert primary["primary_observation_id"] == first["id"]


def test_out_of_order_submissions_still_match(client):
    # 先提交"晚发生"的，再提交"早发生"的
    later = submit(client, observed_at="2026-09-20T09:30:00+08:00", confidence=0.95).json()
    earlier = submit(client, observer="lisi",
                     observed_at="2026-09-20T08:30:00+08:00",
                     latitude=40.0001, longitude=116.4001,
                     confidence=0.9).json()
    assert earlier["dedup"]["action"] in {"auto_merged", "review_pending"}
    # 同物种、60 分钟、近距离、有证据 -> 自动合并
    assert earlier["cluster_id"] == later["cluster_id"]


def test_records_outside_window_do_not_merge(client):
    submit(client, confidence=0.95)
    far = submit(client, observed_at="2026-09-21T08:00:00+08:00")
    assert far.json()["dedup"]["action"] == "new_cluster"


def test_configurable_rules_change_matching(client):
    # 收紧时间窗到 10 分钟：默认会进复核的记录现在直接独立
    updated = client.put("/api/birding/rules?actor=coordinator",
                         json={"time_window_minutes": 10}).json()
    assert updated["version"] == 2
    assert updated["config"]["time_window_minutes"] == 10

    submit(client, observed_at="2026-09-20T08:00:00+08:00", confidence=0.95)
    late = submit(client, observed_at="2026-09-20T08:40:00+08:00",
                  latitude=40.0001, longitude=116.4001, confidence=0.9)
    assert late.json()["dedup"]["action"] == "new_cluster"

    history = client.get("/api/birding/audit").json()
    assert any(item["action"] == "rules.update" for item in history["items"])


def test_invalid_rule_combination_rejected(client):
    response = client.put("/api/birding/rules?actor=coordinator",
                          json={"review_threshold": 0.9, "auto_merge_threshold": 0.5})
    assert response.status_code == 422


def test_window_filters_and_evidence_breakdown(client):
    submit(client, species_code="ANAS_PLAT", location_name="野鸭湖",
           observed_at="2026-09-20T08:00:00+08:00", confidence=0.95)
    submit(client, species_code="ANAS_PLAT", location_name="野鸭湖",
           observed_at="2026-09-20T08:20:00+08:00", confidence=0.9,
           latitude=40.0002, longitude=116.4002,
           evidence=[{"kind": "photo", "uri": "s3://feiyu/x.jpg"}])
    submit(client, species_name="苍鹭", species_code="ARDEA_CIN",
            location_name="密云水库", observed_at="2026-09-22T08:00:00+08:00",
            latitude=40.5, longitude=116.8)

    full = client.get("/api/birding/migration-window",
                      params={"start": "2026-09-01T00:00:00+08:00",
                              "end": "2026-10-01T00:00:00+08:00"}).json()
    assert full["summary"]["bird_count"] == 2

    only = client.get("/api/birding/migration-window",
                      params={"start": "2026-09-01T00:00:00+08:00",
                              "end": "2026-10-01T00:00:00+08:00",
                              "location_name": "野鸭湖"}).json()
    assert only["summary"]["bird_count"] == 1
    cluster = only["count_composition"]["clusters"][0]
    assert cluster["evidence_count"] == 2
    assert cluster["observer_count"] == 1  # 同一观察者两次提交
