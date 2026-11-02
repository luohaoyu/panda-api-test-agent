#!/usr/bin/env python3
"""Pure routing for the API Test Agent entry points.

This module only classifies an intent. It never reads secrets, touches files, or
sends a request.
"""
from __future__ import annotations

import re
from typing import Any


INTENTS = {
    "requirement_validation",
    "single_api_quality",
    "api_debug",
    "environment_probe",
}

_SAFE_METADATA_KEYS = {
    "requirement_id",
    "interface_id",
    "interface_profile",
    "target",
    "environment_id",
    "scope",
}

_PATTERNS = {
    "environment_probe": (
        r"环境|连通|健康检查|探测|probe|health|connectivity|environment",
    ),
    "api_debug": (
        r"调试|调通|为什么.*(?:4\d\d|5\d\d)|定位|排查|复现|debug|diagnos|troubleshoot|repro",
    ),
    "requirement_validation": (
        r"需求|功能|验收|prd|acceptance|requirement|feature|user story",
    ),
    "single_api_quality": (
        r"单接口|接口质量|接口测试|测试接口|检查接口|api quality|quality test|endpoint test",
    ),
}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _metadata_hint(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    # Only keys are used as routing hints. Values are deliberately ignored.
    return " ".join(str(key) for key in metadata if str(key).lower() in {
        "intent",
        "intent_type",
        "entry",
        "request_type",
        "goal",
    })


def _explicit_intent(metadata: Any) -> str | None:
    if not isinstance(metadata, dict):
        return None
    for key in ("intent", "intent_type", "entry", "request_type"):
        value = metadata.get(key)
        if isinstance(value, str) and value in INTENTS:
            return value
    return None


def route_intent(text: str, request_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Offer routing hints without replacing AI/Skill intent understanding.

    This is deliberately a deterministic fallback. It may provide a candidate
    entry point for an AI/Skill decision, but it must not be treated as the
    semantic interpretation of a natural-language request. The returned
    metadata is an allow-list projection and never contains values from
    secret-like fields. Callers must still create a design and obtain human
    review before any HTTP execution.
    """
    text_value = _text(text)
    explicit = _explicit_intent(request_metadata)
    scores = {intent: 0 for intent in INTENTS}
    if explicit:
        scores[explicit] += 100
    for intent, patterns in _PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text_value, flags=re.IGNORECASE):
                scores[intent] += 1
    best = max(scores, key=scores.get)
    top_score = scores[best]
    tied = [intent for intent, score in scores.items() if score == top_score and score > 0]
    if explicit:
        intent = explicit
        confidence = "high"
    elif not tied:
        intent = None
        confidence = "low"
    elif len(tied) > 1:
        intent = None
        confidence = "ambiguous"
    else:
        intent = best
        confidence = "medium"
    safe_metadata = {
        key: request_metadata[key]
        for key in _SAFE_METADATA_KEYS
        if isinstance(request_metadata, dict) and key in request_metadata
        and isinstance(request_metadata[key], (str, int, float, bool, type(None)))
    }
    ranked = sorted(
        ({"intent_type": name, "score": score} for name, score in scores.items()),
        key=lambda item: (-item["score"], item["intent_type"]),
    )
    matched = [item for item in ranked if item["score"] > 0]
    return {
        "intent": intent,
        "intent_type": intent,
        "candidate_intent": intent,
        # Structured candidates keep an unmatched request actionable for the AI
        # instead of degrading to an empty answer.
        "candidates": matched or ranked,
        "candidate_source": "keyword_match" if matched else "unmatched_default_order",
        "supported": intent in INTENTS,
        "confidence": confidence,
        "source": "metadata" if explicit else "text",
        "decision_owner": "explicit_metadata" if explicit else "ai_skill",
        "fallback_only": not bool(explicit),
        "metadata": safe_metadata,
        "requires_design_review": True,
        "requires_ai_clarification": intent is None,
        "execution_allowed": False,
        "request_sent": False,
        "side_effects": False,
    }
