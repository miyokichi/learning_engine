"""Fictional recorded dialogue, only used by chat --sample and offline tests."""
from .llm import FakeChatExtractor

SAMPLE_TEXTS = [
    "モデルの再利用を判断したい。材料はAで形状は同じです。",
    "この材料Aなら再利用できる。材料を条件として覚えて。",
    "別の案件です。材料はA、形状の同等性は未確認です。",
    "断面を見たら同じでした。",
    "見直すと形状が違いました。再利用を保留して再検証してください。",
    "次の案件も材料はAで形状が異なります。",
    "断面を確認してから形状計算をしました。形状差が理由で、判断は保留しました。",
]


def sample_model():
    from .demo import PROCEDURE
    q = {"field_id": "geometry_equivalent", "target_decision": "model_reuse", "missing_information": "今回の形状が過去と同等か、その確認根拠",
         "why_needed": "過去の形状と異なる可能性があります。判断への重要性は未検証の仮説です", "possible_answers": ["同等", "異なる", "未確認"],
         "how_answer_changes_next_step": "回答を確認した後に適用条件を再評価します", "source_case_or_rule": [], "priority": "critical"}
    entries = [
        {"intent": "new_case", "payload": {"goal": "モデルの再利用", "decision_type": "model_reuse", "features": {"material": "A", "geometry_equivalent": True}}},
        {"intent": "correction", "payload": {"conclusion": "REUSE_MODEL", "interpretation": "材料Aのとき再利用するという初期条件", "selected_features": ["material"]}},
        {"intent": "new_case", "payload": {"goal": "別のモデル適用案件", "decision_type": "model_reuse", "features": {"material": "A", "geometry_equivalent": None}}, "questions": [q]},
        {"intent": "answer", "payload": {"values": {"geometry_equivalent": True}}},
        {"intent": "correction", "payload": {"conclusion": "REQUIRE_VALIDATION", "interpretation": "形状同等性が否定されたら再検証する", "selected_features": ["geometry_equivalent"], "features": {"geometry_equivalent": False}}},
        {"intent": "new_case", "payload": {"goal": "次の未経験案件", "decision_type": "model_reuse", "features": {"material": "A", "geometry_equivalent": False}}},
        {"intent": "debrief", "payload": {"procedure": {**PROCEDURE, "goal": "モデル適用条件の確認", "branches": [{"condition": "形状が異なる場合", "action": "形状計算を検討", "status": "unverified"}], "stop_conditions": ["検証根拠が不足したら判断を保留"], "unknowns": ["計算を省略できる条件"]}}},
    ]
    return FakeChatExtractor(dict(zip(SAMPLE_TEXTS, entries)))
