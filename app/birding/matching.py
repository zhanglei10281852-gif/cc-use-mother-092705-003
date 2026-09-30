from __future__ import annotations

import math
import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

from app.core.clock import from_storage


def parse_time(value: str) -> datetime | None:
    try:
        parsed = from_storage(value) or datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        from datetime import UTC

        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def time_gap_minutes(left: str, right: str) -> float | None:
    a, b = parse_time(left), parse_time(right)
    if a is None or b is None:
        return None
    return abs((a - b).total_seconds()) / 60.0


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


_TOKEN_RE = re.compile(r"[0-9a-z一-鿿]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.casefold()))


def caption_similarity(left: str, right: str) -> float:
    left, right = (left or "").strip(), (right or "").strip()
    if not left and not right:
        return 0.0
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    ratio = SequenceMatcher(None, left, right).ratio()
    overlap = _tokens(left) & _tokens(right)
    union = _tokens(left) | _tokens(right)
    jaccard = len(overlap) / len(union) if union else 0.0
    return max(ratio, jaccard)


def evidence_overlap(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    keys = {(item.get("kind", ""), (item.get("reference") or "").strip().casefold()) for item in left}
    other = {(item.get("kind", ""), (item.get("reference") or "").strip().casefold()) for item in right}
    return bool(keys & other)


def score_pair(left: dict[str, Any], right: dict[str, Any], rules: dict[str, Any]) -> tuple[float, list[str]]:
    """对两条原始提交评分。返回 (0~1 的疑似重复分, 命中的信号说明)。"""
    if rules.get("require_same_species", True) and left["species"].strip().casefold() != right["species"].strip().casefold():
        return 0.0, ["物种判断不同"]

    signals: list[str] = []
    score = 0.0
    gap = time_gap_minutes(left["observed_at"], right["observed_at"])
    window = float(rules["time_window_minutes"])
    if gap is None:
        signals.append("时间无法解析")
    elif gap <= window:
        score += 0.35 if gap <= window / 2 else 0.2
        signals.append(f"时间接近（相差 {gap:.0f} 分钟，窗口 {window:.0f} 分钟）")
    else:
        return 0.0, [f"时间相差 {gap:.0f} 分钟，超出窗口"]

    location_match = False
    if left.get("latitude") is not None and right.get("latitude") is not None:
        distance = haversine_meters(left["latitude"], left["longitude"], right["latitude"], right["longitude"])
        limit = float(rules["distance_meters"])
        if distance <= limit:
            location_match = True
            score += 0.35 if distance <= limit / 2 else 0.2
            signals.append(f"地点接近（相距 {distance:.0f} 米，阈值 {limit:.0f} 米）")
        else:
            signals.append(f"地点相距 {distance:.0f} 米，超出阈值")
    elif left.get("location_name") and right.get("location_name"):
        if left["location_name"].strip().casefold() == right["location_name"].strip().casefold():
            location_match = True
            score += 0.2
            signals.append("地点名称一致（无坐标）")
        else:
            signals.append("地点名称不同且缺少坐标")
    else:
        signals.append("地点信息不足以比对")

    left_evidence = left.get("evidence") or []
    right_evidence = right.get("evidence") or []
    if rules.get("evidence_overlap_matches", True) and evidence_overlap(left_evidence, right_evidence):
        score += 0.4
        signals.append("引用了相同佐证")
        location_match = True

    if rules.get("caption_similarity", True):
        similarity = caption_similarity(left.get("photo_caption", ""), right.get("photo_caption", ""))
        threshold = float(rules.get("caption_similarity_threshold", 0.6))
        if similarity >= threshold:
            score += 0.2
            signals.append(f"照片说明相似（{similarity:.2f}）")

    if left.get("observer_id") and right.get("observer_id") and left["observer_id"] == right["observer_id"]:
        signals.append("同一观察者（不作为重复信号）")

    return min(1.0, round(score, 3)), signals


def detect_conflicts(left: dict[str, Any], right: dict[str, Any], rules: dict[str, Any]) -> list[dict[str, Any]]:
    """列出两条候选记录之间互相矛盾的字段，供复核人判断。"""
    conflicts: list[dict[str, Any]] = []
    gap = time_gap_minutes(left["observed_at"], right["observed_at"])
    if gap is not None and gap > max(1.0, float(rules.get("time_window_minutes", 120)) / 2):
        conflicts.append({"field": "observed_at", "left": left["observed_at"], "right": right["observed_at"], "detail": f"时间相差 {gap:.0f} 分钟"})
    if left.get("latitude") is not None and right.get("latitude") is not None:
        distance = haversine_meters(left["latitude"], left["longitude"], right["latitude"], right["longitude"])
        if distance > 200:
            conflicts.append({"field": "location", "left": f"{left['latitude']},{left['longitude']}", "right": f"{right['latitude']},{right['longitude']}", "detail": f"坐标相距 {distance:.0f} 米"})
    elif left.get("location_name") and right.get("location_name") and left["location_name"].strip().casefold() != right["location_name"].strip().casefold():
        conflicts.append({"field": "location_name", "left": left["location_name"], "right": right["location_name"], "detail": "地点名称不一致"})
    if left.get("photo_caption") and right.get("photo_caption"):
        similarity = caption_similarity(left["photo_caption"], right["photo_caption"])
        if similarity < 0.3:
            conflicts.append({"field": "photo_caption", "left": left["photo_caption"], "right": right["photo_caption"], "detail": f"照片说明相似度仅 {similarity:.2f}"})
    if not evidence_overlap(left.get("evidence") or [], right.get("evidence") or []):
        conflicts.append({"field": "evidence", "left": len(left.get("evidence") or []), "right": len(right.get("evidence") or []), "detail": "无共同佐证引用"})
    return conflicts
