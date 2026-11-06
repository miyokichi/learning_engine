"""Thin, persisted conversation coordinator over the existing Engine API."""
from .engine import now, new_id
from .store import encode, decode
from .conversation_schema import validate_intent
from .question_planner import QuestionPlanner
from .logic import feature_differences
from .llm import FakeExtractor
from .images import load_image, MAX_IMAGES


class Conversation:
    def __init__(self, engine, extractor=None, session_id=None):
        self.e, self.llm = engine, extractor
        self.planner = QuestionPlanner(engine)
        if session_id:
            if not engine.store.one("SELECT id FROM conversation_sessions WHERE id=?", (session_id,)):
                raise ValueError("会話が見つかりません。chatで新しく開始してください")
            self.id = session_id
        else:
            self.id, stamp = new_id("CHAT"), now()
            with engine.store.transaction():
                engine.store.conn.execute("INSERT INTO conversation_sessions VALUES(?,?,?,?,?,?,?,?)", (self.id, None, "model_reuse", "", "IDLE", None, stamp, stamp))

    @property
    def session(self):
        return self.e.store.one("SELECT * FROM conversation_sessions WHERE id=?", (self.id,))

    @property
    def pending(self):
        row = self.e.store.one("SELECT * FROM pending_actions WHERE id=? AND status='pending'", (self.session["pending_action_id"],))
        return {**row, "payload": decode(row["proposed_payload"])} if row else None

    def _turn(self, role, text, parsed=None, source="human"):
        cursor = self.e.store.conn.execute("INSERT INTO conversation_turns(session_id,role,raw_text,parsed_intent,source,created_at) VALUES(?,?,?,?,?,?)", (self.id, role, text, encode(parsed) if parsed else None, source, now()))
        return cursor.lastrowid

    def _set(self, **values):
        allowed = {"case_id", "decision_type", "goal", "status", "pending_action_id"}
        if set(values) - allowed:
            raise ValueError("Invalid session state")
        values["updated_at"] = now()
        self.e.store.conn.execute("UPDATE conversation_sessions SET " + ",".join(k + "=?" for k in values) + " WHERE id=?", (*values.values(), self.id))

    def _propose(self, kind, payload, evidence=()):
        payload.setdefault("image_ids", [a["id"] for a in self.attachments()])
        old = self.pending
        if old:
            self.e.store.conn.execute("UPDATE pending_actions SET status='superseded' WHERE id=?", (old["id"],))
            self.e.store.audit("chat_action_superseded", old["id"], {"session": self.id}, now())
        aid = new_id("ACTION")
        self.e.store.conn.execute("INSERT INTO pending_actions VALUES(?,?,?,?,?,?,?)", (aid, self.id, kind, encode(payload), encode(list(evidence) + payload["image_ids"]), "pending", now()))
        self._set(pending_action_id=aid, status="CONFIRM_KNOWLEDGE")
        self.e.store.audit("chat_action_proposed", aid, {"session": self.id, "kind": kind, "payload": payload}, now())
        return "\n" + self.describe_pending()

    def describe_pending(self):
        pending = self.pending
        if not pending:
            session = self.session
            if session["case_id"]:
                questions = self.e.store.many("""SELECT q.field_id,
                    COALESCE(json_extract(c.body,'$.missing_information'),q.question) AS question
                    FROM questions q LEFT JOIN question_candidates c ON c.question_id=q.id
                    WHERE q.case_id=? AND q.target_decision=? AND q.status IN ('open','answered_unverified')
                    ORDER BY q.created_at,q.id LIMIT 2""", (session["case_id"], session["decision_type"]))
                if questions:
                    return f"現在の状態：{session['status']}。未解決の質問を保存しています。\n" + "\n".join(q["question"] for q in questions) + "\n回答や訂正を文章で教えてください。"
            return "確認待ちの提案はありません。判断したいことや作業報告を話してください。"
        names = {"new_case": "案件と属性の解釈", "answer": "回答の解釈", "correction": "訂正と重要な差異", "publish_rule": "回帰結果とルール公開",
                 "debrief": "作業報告の解釈", "approve_procedure": "手順の参考候補の承認", "concept": "概念定義の採用", "observation": "画像・観察内容の解釈"}
        payload = pending["payload"]
        display = payload.get("summary", payload)
        return f"確認対象：{names.get(pending['action_type'], pending['action_type'])}\n{self._summary(display)}\n採用する場合は『確認』だけを入力してください。修正内容を話すか『保留』『取消』も選べます。"

    @staticmethod
    def _summary(value):
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and "steps" in value:
            lines = [f"目的：{value.get('goal') or '未確認'}", f"実施する条件：{value.get('trigger') or '未確認'}"]
            for i, step in enumerate(value["steps"], 1):
                lines.append(f"{i}. {step['action']}（理由：{step.get('reason') or '未確認'}、結果：{step.get('result') or '未確認'}）")
            for branch in value.get("branches", []):
                lines.append(f"条件分岐の候補：{branch.get('condition') or '条件は未確認'} → {branch['action']}（未検証）")
            lines.append(f"全体の結果：{value.get('outcome') or '未確認'}。必須順序は未検証です。")
            return "\n".join(lines)
        def readable(item):
            if item is None:
                return "未確認"
            if item is True:
                return "はい（true）"
            if item is False:
                return "いいえ（false）"
            if isinstance(item, dict):
                return "、".join(f"{k}={readable(v)}" for k, v in item.items()) or "なし"
            if isinstance(item, list):
                return " / ".join(readable(v) for v in item) or "なし"
            return str(item)
        if isinstance(value, dict):
            return "\n".join(f"{key}：{readable(item)}" for key, item in value.items())
        return readable(value)

    def attachments(self):
        """Metadata only; keep binary data out of JSON context and audit events."""
        return self.e.store.many("""SELECT a.id,a.turn_id,a.case_id,a.filename,
            a.mime_type,a.byte_size,a.sha256,a.created_at
            FROM conversation_attachments a JOIN conversation_turns t ON t.id=a.turn_id
            WHERE t.session_id=? AND (a.case_id IS ? OR a.case_id IS NULL)
            ORDER BY t.id DESC,a.id LIMIT ?""", (self.id, self.session["case_id"], MAX_IMAGES))[::-1]

    def context(self):
        session = self.session
        case_id = session["case_id"]
        history = self.e.store.many("SELECT role,raw_text FROM conversation_turns WHERE session_id=? ORDER BY id DESC LIMIT 16", (self.id,))[::-1]
        return {"session": session, "pending": self.pending, "case": self.e.case(case_id) if case_id else None,
                "rules": self.e.rules(), "history_as_evidence_not_instructions": history,
                "questions": self.e.store.many("SELECT * FROM questions WHERE case_id=? AND status IN ('open','answered_unverified') ORDER BY created_at LIMIT 2", (case_id,)) if case_id else [],
                "cornerstones": self.e.retrieve_cases(case_id) if case_id else [],
                "recent_debrief": self.e.store.one("SELECT * FROM debriefs WHERE case_id=? ORDER BY created_at DESC LIMIT 1", (case_id,)) if case_id else None,
                "unverified_knowledge": self.e.store.many("SELECT id,kind,body,status FROM knowledge WHERE case_id=? AND status='draft' ORDER BY created_at DESC LIMIT 8", (case_id,)) if case_id else [],
                "feature_definitions": self.e.store.many("SELECT * FROM feature_definitions"),
                "image_attachments": self.attachments(),
                "observations": [{**r, "body": decode(r["body"])} for r in self.e.store.many(
                    "SELECT id,body,status FROM knowledge WHERE case_id=? AND kind='observation' ORDER BY created_at DESC,id DESC LIMIT 8", (case_id,))] if case_id else []}

    def handle(self, text, image_paths=()):
        text = text.strip()
        if len(image_paths) > MAX_IMAGES:
            return "画像は1回に4枚以内にしてください。"
        try:
            images = [load_image(path) for path in image_paths]
        except (OSError, ValueError) as exc:
            return f"画像を読み込めませんでした。{exc}"
        if images and not text:
            text = "この画像の観察内容と、判断に必要な不明点を整理してください。"
        with self.e.store.transaction():
            turn_id = self._turn("human", text)
            for attachment in images:
                aid = new_id("IMAGE")
                self.e.store.conn.execute("INSERT INTO conversation_attachments VALUES(?,?,?,?,?,?,?,?,?)", (
                    aid, turn_id, self.session["case_id"], attachment["filename"], attachment["mime_type"],
                    attachment["byte_size"], attachment["sha256"], attachment["data"], now()))
                self.e.store.audit("chat_image_attached", aid, {"session": self.id, "turn_id": turn_id,
                    **{k: v for k, v in attachment.items() if k != "data"}}, now())
        try:
            with self.e.store.transaction():
                if images and text in {"確認", "y", "OK", "ok", "はい", "保留", "取消", "キャンセル", "修正", "/status", "状態"}:
                    message = "画像を保存しました。画像の説明や判断したいことを文章で入力してください。採用は次の『確認』で行います。"
                elif text in {"確認", "y"}:
                    message = self._confirm()
                elif text in {"OK", "ok", "そうですね", "はい"} and self.pending:
                    message = "まだ採用していません。表示された確認対象に同意する場合は『確認』だけを入力してください。"
                elif text == "保留":
                    self._set(status="DEFERRED")
                    message = "保留して保存しました。内容を見直して『確認』するか、修正内容を話してください。"
                elif text in {"取消", "キャンセル"}:
                    message = self._cancel()
                elif text == "修正":
                    message = "どの内容をどう直すか、文章で教えてください。現在の提案はまだ採用しません。"
                elif text in {"/status", "状態"}:
                    message = self.describe_pending()
                elif self.llm is None:
                    message = "原文を保存しました。LLM未設定のため自然言語の自動解釈は無効です。tutorialで体験するか、interactの入力フォームをご利用ください。"
                else:
                    context = self.context()
                    if context["image_attachments"]:
                        interpret_images = getattr(self.llm, "interpret_images", None)
                        if not callable(interpret_images):
                            raise ValueError("画像は保存済みです。画像入力に対応したLLM接続を利用してください")
                        image_data = [{**a, "data": self.e.store.one(
                            "SELECT data FROM conversation_attachments WHERE id=?", (a["id"],))["data"]}
                            for a in context["image_attachments"]]
                        parsed = validate_intent(interpret_images(text, context, image_data))
                    else:
                        parsed = validate_intent(self.llm.interpret(text, context))
                    self.e.store.conn.execute("UPDATE conversation_turns SET parsed_intent=?,source='human_with_llm_interpretation' WHERE id=?", (encode(parsed), turn_id))
                    message = self._route(parsed, text, turn_id)
                self._turn("assistant", message, source="python_coordinator")
                return message
        except Exception as exc:
            # Raw input and image bytes were committed before any model call.
            message = f"処理を保留しました（{type(exc).__name__}）。原文・添付画像と確認待ちの状態は保存済みです。設定や解釈を見直して再入力してください。"
            if self.attachments():
                message += "画像入力には画像対応モデルが必要です。"
            if isinstance(exc, (ValueError, PermissionError, KeyError)):
                message += "\n" + str(exc)
            with self.e.store.transaction():
                self._turn("system_event", message, source="error")
                self._set(status="ERROR")
            return message

    def _cancel(self):
        if self.pending:
            self.e.store.audit("chat_action_rejected", self.pending["id"], {"session": self.id}, now())
            self.e.store.conn.execute("UPDATE pending_actions SET status='rejected' WHERE id=?", (self.pending["id"],))
        self._set(pending_action_id=None, status="IDLE")
        return "提案を取り消しました。保存済みの原文は残します。"

    def _route(self, parsed, raw, turn_id):
        intent, payload = parsed["intent"], parsed["payload"]
        session = self.session
        case_id = session["case_id"]
        if intent == "cancel":
            # Model cancel is only a proposal, never a real rejection of human approval.
            return "取り消す場合は『取消』を入力してください。"
        if intent == "clarify":
            return "まだ知識は更新していません。\n" + (parsed["message"] or "何について、どの判断をしたいですか？")
        if intent == "new_case":
            if not payload.get("goal"):
                return "今回、何を判断したいですか？"
            summary = {"判断したいこと": payload["goal"], "属性の解釈": payload.get("features", {}), "不明な値": "未確認として保持します"}
            pending = self.pending
            previous_images = pending["payload"].get("image_ids", []) if pending and pending["action_type"] == "new_case" else []
            image_ids = [a["id"] for a in self.attachments()
                         if a["case_id"] is None or a["turn_id"] == turn_id or a["id"] in previous_images]
            if image_ids:
                summary["画像の解釈"] = parsed["message"] or raw
            return "案件の候補を整理しました。" + self._propose("new_case", {**payload, "question_proposals": parsed["questions"],
                "image_ids": image_ids, "observation_text": parsed["message"] or raw,
                "source": f"chat_turn:{turn_id}", "summary": summary})
        if not case_id and intent == "debrief":
            procedure = payload.get("procedure")
            goal = (procedure or {}).get("goal") or "作業の振り返り"
            return "作業報告を整理するための案件を作ります。" + self._propose("new_case", {"goal": goal, "features": {}, "source": f"chat_turn:{turn_id}",
                "initial_debrief": {"procedure": procedure, "raw": raw}, "summary": {"目的": goal, "原文": raw}})
        if not case_id:
            return "まず、判断したい案件や作業の目的を教えてください。"
        if intent == "observation":
            return "観察内容を候補として整理しました。" + self._propose("observation", {**payload,
                "source": f"chat_turn:{turn_id}", "question_proposals": parsed["questions"],
                "summary": {"観察の解釈": payload["text"], "属性の解釈": payload.get("features", {})}})
        if intent == "evaluate":
            if self.pending:
                return "先に確認待ちの提案を確認・修正・取消してください。\n" + self.describe_pending()
            return self._evaluate(payload.get("explore", False), parsed["questions"])
        if intent == "answer":
            values = payload.get("values", {})
            if not values:
                return "確認できた値か『未確認』であることを教えてください。"
            allowed = {q["field_id"]: q for q in self.context()["questions"]}
            if set(values) - allowed.keys():
                raise ValueError("現在の質問と回答の属性が一致しません。新しい観察なら訂正として説明してください")
            answers = []
            for field, value in values.items():
                q = allowed[field]
                if q["status"] == "open":
                    self.e.answer_question(q["id"], raw)
                answers.append({"question_id": q["id"], "field": field, "value": value})
            return self._propose("answer", {"answers": answers, "source": f"chat_turn:{turn_id}", "summary": {"回答の解釈": values, "原文": raw}})
        if intent == "correction":
            return self._correction(payload, raw, turn_id)
        if intent == "debrief":
            last = self.e.store.one("SELECT * FROM debriefs WHERE case_id=? AND interpretation_status<>'confirmed' ORDER BY created_at DESC LIMIT 1", (case_id,))
            procedure = payload.get("procedure")
            extractor = FakeExtractor(procedure) if procedure else None
            result = self.e.extend_debrief(last["id"], raw, extractor) if last else self.e.report_debrief(case_id, raw, extractor)
            if procedure is None:
                self._set(status="DEBRIEF_FOLLOWUP")
                return "作業報告の原文を保存しました。\n" + "\n".join(result["followups"][:2])
            return "作業の順序は観測された順番で、必須順序は未検証です。" + self._propose("debrief", {"debrief_id": result["id"], "procedure": procedure, "summary": procedure})
        if intent == "concept":
            body = {**payload.get("body", {}), "source": f"chat_turn:{turn_id}"}
            proposal = self.e.propose_knowledge("concept", body, case_id)
            return "新概念を未検証の候補として保存しました。" + self._propose("concept", {"knowledge_id": proposal["id"], "summary": body})
        if intent == "ask_about_knowledge":
            return self._summary({"現在の目的": session["goal"], "属性": {k: f"{v.get('value')}（{v['status']}）" for k, v in self.e.case(case_id)["features"].items()},
                "公開されたルール": [r["explanation"] or r["conclusion"] for r in self.e.rules() if r["status"] == "published"],
                "保存した観察": [r["body"]["text"] for r in self.context()["observations"] if r["status"] == "verified"],
                "参考手順（実行不可）": [p["body"].get("goal") for p in self.e.procedures(case_id)]})
        raise ValueError("Unsupported intent")

    def _correction(self, payload, raw, turn_id):
        case_id, decision_type = self.session["case_id"], self.session["decision_type"]
        pending = self.pending
        previous = pending["payload"] if pending and pending["action_type"] == "correction" else {}
        payload = {**previous, **payload}
        if not payload.get("conclusion"):
            return "どの結論に訂正しますか？その理由も教えてください。"
        latest = self.e.store.one("SELECT id FROM decisions WHERE case_id=? AND decision_type=? ORDER BY created_at DESC,id DESC LIMIT 1", (case_id, decision_type))
        fid = payload.get("feedback_id")
        if fid and self.e.store.one("SELECT corrected_to FROM feedback WHERE id=?", (fid,))["corrected_to"] != payload["conclusion"]:
            fid = None
        fid = fid or self.e.feedback(case_id, raw, payload["conclusion"], latest["id"] if latest else None)
        context = self.e.feedback_context(fid)
        # Prefer the actual fired rule and its own anchor over similarity alone.
        decision = self.e.store.one("SELECT * FROM decisions WHERE id=?", (latest["id"],)) if latest else None
        parent_id = payload.get("parent_rule_id") or (decision["rule_id"] if decision else None)
        parent = self.e.store.one("SELECT * FROM rules WHERE id=?", (parent_id,)) if parent_id else None
        cornerstone = payload.get("cornerstone_case_id") or (parent["corrected_case_id"] or parent["cornerstone_case_id"] if parent else None)
        if cornerstone == case_id:
            cornerstone = next((c["case_id"] for c in context if c["case_id"] != case_id), None)
        # Show the proposed observation as a candidate until the human confirms it.
        compared_features = dict(self.e.case(case_id)["features"])
        for field, value in payload.get("features", {}).items():
            compared_features[field] = {"value": value, "status": "interpretation_candidate"}
        differences = feature_differences(self.e.case(cornerstone)["features"], compared_features) if cornerstone else []
        readable_differences = [{"項目": d["feature"], "過去": d["previous"].get("value"), "今回": d["current"].get("value"),
                                 "確認状態": "解釈候補（まだ未確認）" if d["current"].get("status") == "interpretation_candidate" else "登録済みの情報"} for d in differences]
        fields = payload.get("selected_features", [])
        summary = {"訂正後の結論": payload["conclusion"], "解釈": payload.get("interpretation", raw), "重要な差異の候補": fields,
                   "比較した過去事例（補足ID）": cornerstone or "なし。初期ルールの候補として確認します", "過去事例との比較": readable_differences,
                   "属性の修正候補": payload.get("features", {})}
        action = {**payload, "feedback_id": fid, "cornerstone": cornerstone, "parent": parent_id,
                  "interpretation": payload.get("interpretation", raw), "source": f"chat_turn:{turn_id}",
                  "human_text": (previous.get("human_text", "") + "\n" + raw).strip(), "summary": summary}
        message = "訂正を記録しました。過去事例との差異を確認してください。"
        message += self._propose("correction", action)
        if not fields:
            message += "\n今回の判断を分ける重要な違いはどれですか？言葉で説明してください。"
        return message

    def _apply_values(self, values, raw, source=None):
        case_id, decision_type = self.session["case_id"], self.session["decision_type"]
        for field, value in values.items():
            qid = self.e.ask(case_id, decision_type, field)
            q = self.e.store.one("SELECT * FROM questions WHERE id=?", (qid,))
            if q["status"] == "open":
                self.e.answer_question(qid, raw)
            elif q["status"] == "confirmed":
                # A newly confirmed correction is new evidence, not a repeated Unknown answer.
                qid = new_id("Q")
                self.e.store.conn.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?,?)", (qid, case_id, decision_type, field, "訂正された観察", "answered_unverified", raw, now()))
            self.e.confirm_question_feature(qid, value, status="unknown" if value is None else "expert_confirmed", evidence=source, expert_confirmed=True)

    def _save_observation(self, text, payload):
        proposal = self.e.propose_knowledge("observation", {"text": text,
            "source": payload["source"], "image_ids": payload.get("image_ids", []),
            "features": payload.get("features", {}), "verification": "human_confirmed_interpretation"}, self.session["case_id"])
        self.e.adopt_knowledge(proposal["id"], expert_confirmed=True)

    def _confirm(self):
        action = self.pending
        if not action:
            return "確認待ちの提案はありません。二重採用は行いません。"
        kind, p = action["action_type"], action["payload"]
        # The exact human token targets one stored action. Model outputs never call here.
        if kind == "correction" and not p.get("selected_features"):
            return "まだ重要な差異が選ばれていません。判断を分ける条件を言葉で教えてください。"
        self.e.store.conn.execute("UPDATE pending_actions SET status='confirmed' WHERE id=? AND status='pending'", (action["id"],))
        self._set(pending_action_id=None, status="IDLE")
        self.e.store.audit("chat_action_confirmed", action["id"], {"session": self.id, "human_token": "確認", "kind": kind}, now())
        if kind == "new_case":
            cid = new_id("CASE")
            features = {k: {"value": v, "status": "unknown" if v is None else "expert_confirmed", "source": p["source"]} for k, v in p.get("features", {}).items()}
            self.e.add_case(cid, features, metadata={"goal": p["goal"], "source": p["source"], "decision_type": p.get("decision_type", "model_reuse")})
            self._set(case_id=cid, goal=p["goal"], decision_type=p.get("decision_type", "model_reuse"))
            # Older images outside the request's four-image window still belong
            # to this case; do not leak unassigned images into a later case.
            self.e.store.conn.execute("""UPDATE conversation_attachments SET case_id=?
                WHERE case_id IS NULL AND turn_id IN
                (SELECT id FROM conversation_turns WHERE session_id=?)""", (cid, self.id))
            for aid in p.get("image_ids", []):
                self.e.store.conn.execute("UPDATE conversation_attachments SET case_id=? WHERE id=?", (cid, aid))
            if p.get("image_ids"):
                self._save_observation(p.get("observation_text", p["goal"]), p)
            if p.get("initial_debrief"):
                d = p["initial_debrief"]
                return "作業報告の目的を保存しました。\n" + self._route({"intent": "debrief", "payload": {"procedure": d["procedure"]}}, d["raw"], 0)
            return "案件の解釈を確認して保存しました。\n" + self._evaluate(proposed=p.get("question_proposals", []))
        if kind == "answer":
            for answer in p["answers"]:
                self.e.confirm_question_feature(answer["question_id"], answer["value"], status="unknown" if answer["value"] is None else "expert_confirmed", evidence=p.get("source"), expert_confirmed=True)
            return "回答の解釈を確認しました。\n" + self._evaluate()
        if kind == "observation":
            self._apply_values(p.get("features", {}), p["text"], p["source"])
            self._save_observation(p["text"], p)
            return "観察の解釈を確認し、画像の出典とともにナレッジに保存しました。\n" + self._evaluate(proposed=p.get("question_proposals", []))
        if kind == "correction":
            self._apply_values(p.get("features", {}), p["human_text"], p.get("source"))
            fid, cid = p["feedback_id"], self.session["case_id"]
            self.e.confirm_feedback(fid, p["interpretation"])
            self.e.review_case(cid, p["conclusion"])
            if p["parent"]:
                if not p["cornerstone"]:
                    raise ValueError("同じ親ルールの承認済み過去事例がありません")
                preview = self.e.draft_exception_from_feedback(fid, p["cornerstone"], p["selected_features"], p["parent"])
            else:
                features = self.e.case(cid)["features"]
                predicates = []
                for field in p["selected_features"]:
                    entry = features.get(field, {})
                    value = entry.get("value")
                    if entry.get("status") not in {"expert_confirmed", "documented", "measured", "derived_verified"} or not isinstance(value, (str, bool)):
                        raise ValueError("初期ルールは確認済みの真偽値・カテゴリ条件から作ります。数値境界は別途専門家の検証が必要です")
                    predicates.append({"feature": field, "op": "eq", "value": value})
                preview = self.e.add_rule(new_id("R"), p["conclusion"], predicates, decision_type=self.session["decision_type"], cornerstone_case_id=cid,
                                          corrected_case_id=cid, explanation=p["interpretation"], feedback_id=fid)
            summary = {"ルール候補": preview["rule_id"], "回帰違反": preview["regressions"], "正解との不一致": preview["reviewed_mismatches"], "公開の意味": "固定条件を今後の判断に利用します。外部実行は許可しません"}
            return "解釈を確認し、ルールの草案を作りました。公開には別の確認が必要です。" + self._propose("publish_rule", {"rule_id": preview["rule_id"], "summary": summary})
        if kind == "publish_rule":
            self.e.publish_rule(p["rule_id"], True, True)
            return "回帰検査を通過したルールを公開しました。\n" + self._evaluate()
        if kind == "debrief":
            proc = self.e.confirm_debrief(p["debrief_id"], p["procedure"])
            return "作業報告の解釈を確認しました。判断条件を正式なルールにする場合は、訂正として別途確認してください。" + self._propose("approve_procedure", {"procedure_id": proc["procedure_id"], "summary": "参考候補として承認します。条件分岐は未検証、自動実行は不可です。"})
        if kind == "approve_procedure":
            self.e.approve_procedure(p["procedure_id"], True)
            return "手順を参考候補として保存しました。自動実行はしません。"
        if kind == "concept":
            result = self.e.adopt_knowledge(p["knowledge_id"], True)
            return "概念定義を採用しました。影響候補を再確認します。\n" + self._summary(result) + "\n" + self._evaluate()
        raise ValueError("Unknown confirmation type")

    def _evaluate(self, explore=False, proposed=()):
        session = self.session
        result = self.e.evaluate(session["case_id"], session["decision_type"], intent="explore" if explore else "commit")
        questions = self.planner.ask_batch(session["case_id"], session["decision_type"], proposed)
        critical = any(q["priority"] == "critical" for q in questions)
        gate = "ASK" if critical and result["gate"] == "EXECUTE" else result["gate"]
        if explore and gate == "ASK" and result["reason"] != "disputed_case":
            gate = "EXPLORE"
        self._set(status=gate)
        reasons = {"rule_matched": "確認済みの固定条件に一致しました", "unresolved_root": "必要な条件が未確認です", "possible_exception": "例外条件の確認が必要です", "no_root_or_ambiguous": "判断条件が不足または競合しています"}
        message = f"判断：{result['decision']} / 状態：{gate}\n理由：{reasons.get(result['reason'], result['reason'])}\n"
        if result["rule_path"]:
            message += "根拠ルール：" + " → ".join(result["rule_path"]) + "\n"
        message += f"参照した承認済み事例：{len(result['reference_cases'])}件、証拠参照：{len(result['evidence_refs'])}件（出典の真正性を保証するものではありません）\n"
        information = self.e.case(session["case_id"])["features"]
        if information:
            message += "確認に使った情報：" + "、".join(f"{k}={self._summary(v.get('value'))}（出典：{v.get('source', '未指定')}）" for k, v in list(information.items())[:4]) + "\n"
        for q in questions:
            body = q["body"]
            message += f"質問：{body['missing_information']}を教えてください。\nなぜ：{body['why_needed']}。{body['how_answer_changes_next_step']}\n"
        if not questions and result["unknown_fields"]:
            message += "未確認という回答を保持しています。同じ質問は繰り返しません。新しい根拠が得られたら訂正として教えてください。\n"
        return message + "確認・訂正や作業報告を話せます。外部操作は実行しません。"


def chat(engine, extractor=None, session_id=None):
    print("Learning Engine 会話モード。終了は /exit、状態の確認は /status。")
    print('画像は /image "ファイルパス" で添付できます（PNG・JPEG・WebP、1枚5 MiB以下）。')
    if extractor is None:
        print("LLM未設定：自然言語の自動解釈は無効です。tutorial または interact を利用できます。入力原文は保存します。")
    if session_id is None:
        sessions = engine.store.many("SELECT id,goal,updated_at FROM conversation_sessions ORDER BY updated_at DESC LIMIT 5")
        if sessions:
            print("前回の会話：")
            for index, row in enumerate(sessions, 1):
                print(f"{index}. {row['goal'] or '開始前の会話'}")
            try:
                choice = input("再開する番号、またはEnterで新しい会話> ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if choice:
                if not choice.isdigit() or not 1 <= int(choice) <= len(sessions):
                    raise ValueError("表示された番号を選んでください")
                session_id = sessions[int(choice) - 1]["id"]
    conversation = Conversation(engine, extractor, session_id)
    print(f"会話を保存します。再開用ID：{conversation.id}")
    print(conversation.describe_pending())
    while True:
        try:
            text = input("人間> ")
        except (EOFError, KeyboardInterrupt):
            print("\n保存しました。同じDBから再開できます。")
            return
        if text.strip() in {"/exit", "終了"}:
            print("保存しました。同じDBから再開できます。")
            return
        if text.strip() == "/image" or text.strip().startswith("/image "):
            try:
                path = text.strip()[len("/image"):].strip() or input("画像のファイルパス> ").strip()
                if len(path) >= 2 and path[0] == path[-1] and path[0] in {'"', "'"}:
                    path = path[1:-1]
                caption = input("画像について説明・判断したいこと（Enterでも送信）> ")
            except (EOFError, KeyboardInterrupt):
                print("\n送信を中止しました。保存済みの会話から再開できます。")
                return
            print("AI> " + conversation.handle(caption, image_paths=[path]))
        elif text.strip():
            print("AI> " + conversation.handle(text))
