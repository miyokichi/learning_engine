"""Example CLI; all confirmations are intentional user actions."""
import argparse
import json
import os
import sqlite3
from pathlib import Path
from .engine import Engine
from .llm import LocalChatExtractor
from .config import read_env


def data_arg(value):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return json.loads(Path(value).read_text(encoding="utf-8-sig"))


def extractor(args):
    env_file = getattr(args, "env_file", None)
    config = {**read_env(env_file or ".env", required=env_file is not None), **os.environ}
    url = args.llm_url or config.get("LE_LLM_URL")
    model = args.model or config.get("LE_LLM_MODEL")
    if bool(url) != bool(model):
        raise ValueError("Set both LE_LLM_URL and LE_LLM_MODEL")
    return LocalChatExtractor(url, model, config.get("LE_LLM_API_KEY") or "local") if url else None


def main(argv=None):
    p = argparse.ArgumentParser(description="Learning Engine - conversational judgment and procedure learning")
    p.add_argument("--db", default="learning.sqlite3")
    p.add_argument("--env-file", help="LLM設定ファイル（省略時は作業ディレクトリの.env）")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("demo", help="架空の回答・承認を使ったオフラインE2Eデモ")
    x = sub.add_parser("tutorial", help="初めての方向けの日本語学習体験（架空教材）"); x.add_argument("--auto", action="store_true", help="架空の回答と承認を自動再生")
    x = sub.add_parser("chat", help="日本語で判断・質問・訂正・作業報告"); x.add_argument("--session"); x.add_argument("--sample", action="store_true", help="固定された架空会話サンプルを使用"); x.add_argument("--llm-url"); x.add_argument("--model")
    x = sub.add_parser("interact"); x.add_argument("--llm-url"); x.add_argument("--model")
    sub.add_parser("pending")
    x = sub.add_parser("doctor", help="DBとLLM設定の診断。既定では接続しません"); x.add_argument("--check-llm", action="store_true", help="明示したLLMに架空の疎通リクエストを送る"); x.add_argument("--llm-url"); x.add_argument("--model")
    sub.add_parser("metrics")
    x = sub.add_parser("replay-experiment", help="隔離DBで架空イベントを逐次再生"); x.add_argument("--scenario", help="時系列イベントのJSONファイル。省略時は架空教材"); x.add_argument("--strategy", choices=["learning_engine", "baseline_llm_plus_cases", "compare"], default="compare")
    x = sub.add_parser("consult"); x.add_argument("--case", required=True); x.add_argument("--text", required=True); x.add_argument("--task", choices=["case", "answer", "correction", "concept"], default="case"); x.add_argument("--llm-url"); x.add_argument("--model")
    x = sub.add_parser("withdraw-rule"); x.add_argument("--rule", required=True); x.add_argument("--expert-confirmed", action="store_true")
    x = sub.add_parser("propose-knowledge"); x.add_argument("--kind", required=True); x.add_argument("--body", required=True); x.add_argument("--case")
    x = sub.add_parser("adopt-knowledge"); x.add_argument("--id", required=True); x.add_argument("--expert-confirmed", action="store_true"); x.add_argument("--reject", action="store_true")
    x = sub.add_parser("concept-impacts"); x.add_argument("--id", required=True)
    x = sub.add_parser("procedures"); x.add_argument("--case", required=True)
    x = sub.add_parser("compare-procedures"); x.add_argument("--id", action="append", required=True); x.add_argument("--semantic", help="手動の意味的比較候補JSON"); x.add_argument("--use-llm", action="store_true"); x.add_argument("--llm-url"); x.add_argument("--model")
    x = sub.add_parser("validate-procedure"); x.add_argument("--id", required=True); x.add_argument("--scope", required=True); x.add_argument("--evidence", required=True); x.add_argument("--expert-confirmed", action="store_true")
    x = sub.add_parser("add-case"); x.add_argument("--id", required=True); x.add_argument("--features", required=True); x.add_argument("--occurred-at")
    x = sub.add_parser("review-case"); x.add_argument("--case", required=True); x.add_argument("--decision", required=True); x.add_argument("--status", default="expert_approved")
    x = sub.add_parser("evaluate"); x.add_argument("--case", required=True); x.add_argument("--decision-type", default="model_reuse"); x.add_argument("--intent", choices=["commit", "explore"], default="commit")
    x = sub.add_parser("feedback"); x.add_argument("--case", required=True); x.add_argument("--text", required=True); x.add_argument("--corrected-to", required=True); x.add_argument("--decision-id")
    x = sub.add_parser("confirm-feedback"); x.add_argument("--id", required=True); x.add_argument("--interpretation", required=True)
    x = sub.add_parser("compare"); x.add_argument("--current", required=True); x.add_argument("--cornerstone", required=True)
    x = sub.add_parser("draft-exception"); x.add_argument("--feedback", required=True); x.add_argument("--cornerstone", required=True); x.add_argument("--feature", action="append", required=True); x.add_argument("--parent", required=True); x.add_argument("--rule-id")
    x = sub.add_parser("add-rule"); x.add_argument("--rule", required=True, help="JSON file with id, conclusion, predicates etc.")
    x = sub.add_parser("preview-rule"); x.add_argument("--rule", required=True)
    x = sub.add_parser("publish-rule"); x.add_argument("--rule", required=True); x.add_argument("--expert-confirmed", action="store_true"); x.add_argument("--impacts-reviewed", action="store_true")
    x = sub.add_parser("questions"); x.add_argument("--case")
    x = sub.add_parser("answer-question"); x.add_argument("--id", required=True); x.add_argument("--text", required=True)
    x = sub.add_parser("confirm-answer"); x.add_argument("--id", required=True); x.add_argument("--value", required=True, help="JSON value e.g. true, 12, or string with quotes"); x.add_argument("--status", default="expert_confirmed"); x.add_argument("--evidence"); x.add_argument("--expert-confirmed", action="store_true")
    x = sub.add_parser("debrief"); x.add_argument("--case", required=True); x.add_argument("--text"); x.add_argument("--llm-url"); x.add_argument("--model")
    x = sub.add_parser("extend-debrief"); x.add_argument("--id", required=True); x.add_argument("--text", required=True); x.add_argument("--llm-url"); x.add_argument("--model")
    x = sub.add_parser("confirm-debrief"); x.add_argument("--id", required=True); x.add_argument("--edited", help="edited JSON interpretation file")
    x = sub.add_parser("approve-procedure"); x.add_argument("--id", required=True); x.add_argument("--expert-approved", action="store_true")
    x = sub.add_parser("replay"); x.add_argument("--mode", choices=["current", "current_knowledge", "point_in_time"], default="current_knowledge")
    args = p.parse_args(argv)
    if args.command == "replay-experiment":
        from .replay_experiment import run_experiment, compare_experiment
        from .demo_experiment import sample_scenario
        try:
            scenario = data_arg(args.scenario) if args.scenario else sample_scenario()
            output = compare_experiment(scenario) if args.strategy == "compare" else run_experiment(scenario, args.strategy)
            print(json.dumps(output, ensure_ascii=False, indent=2))
        except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
            p.error(str(exc))
        return
    try:
        with Engine(args.db) as e:
            dispatch(args, e)
    except (ValueError, KeyError, PermissionError, OSError, sqlite3.Error) as exc:
        p.error(str(exc))


def dispatch(args, e):
    if args.command == "init":
        output = {"db": args.db, "status": "initialized"}
    elif args.command == "demo":
        from .demo import run_demo
        output = run_demo(e)
    elif args.command == "tutorial":
        from .tutorial import tutorial
        tutorial(e, args.auto)
        return
    elif args.command == "chat":
        from .conversation import chat
        if args.sample:
            from .demo_conversation import sample_model, SAMPLE_TEXTS
            print("架空の固定会話サンプルです。一般的な自然言語解釈は行いません。回答・承認も練習用です。")
            print("入力できる練習文：\n" + "\n".join(SAMPLE_TEXTS))
            model = sample_model()
        else:
            model = extractor(args)
        chat(e, model, args.session)
        return
    elif args.command == "interact":
        from .interactive import interact
        interact(e, extractor(args))
        return
    elif args.command == "pending":
        output = e.pending()
    elif args.command == "doctor":
        model = extractor(args)
        with e.store.transaction():
            e.store.conn.execute("CREATE TEMP TABLE IF NOT EXISTS doctor_write_probe (value INTEGER)")
            e.store.conn.execute("INSERT INTO doctor_write_probe VALUES(1)")
            e.store.conn.execute("DROP TABLE doctor_write_probe")
        output = {"db": args.db, "db_read_write": True, "integrity": e.store.one("PRAGMA quick_check")["quick_check"],
                  "schema_migration": e.store.one("SELECT max(version) version FROM schema_migrations")["version"],
                  "env_file": args.env_file or str(Path.cwd() / ".env"), "llm_configured": model is not None,
                  "llm_connection": "not_requested", "api_key": "not_displayed"}
        if args.check_llm:
            if not model:
                raise ValueError("LLM設定がありません。.envのURLとモデル名を確認してください")
            try:
                model._complete('Return only JSON: {"ok":true}', "接続確認用の架空メッセージです。実データは含みません。")
                output["llm_connection"] = "ok"
            except Exception as exc:
                output["llm_connection"] = "failed:" + type(exc).__name__
    elif args.command == "metrics":
        output = e.metrics()
    elif args.command == "consult":
        output = e.suggest(args.case, args.text, extractor(args), args.task)
    elif args.command == "withdraw-rule":
        e.withdraw_rule(args.rule, args.expert_confirmed); output = {"status": "withdrawn"}
    elif args.command == "propose-knowledge":
        output = e.propose_knowledge(args.kind, data_arg(args.body), args.case)
    elif args.command == "adopt-knowledge":
        output = e.adopt_knowledge(args.id, args.expert_confirmed, args.reject)
    elif args.command == "concept-impacts":
        output = e.concept_impacts(args.id)
    elif args.command == "procedures":
        output = e.procedures(args.case)
    elif args.command == "compare-procedures":
        output = e.compare_procedures(args.id, extractor(args) if args.use_llm else None, data_arg(args.semantic) if args.semantic else None)
    elif args.command == "validate-procedure":
        output = e.validate_procedure_scope(args.id, data_arg(args.scope), args.evidence, args.expert_confirmed)
    elif args.command == "add-case":
        output = e.add_case(args.id, data_arg(args.features), occurred_at=args.occurred_at)
    elif args.command == "review-case":
        e.review_case(args.case, args.decision, args.status); output = {"case": args.case, "status": args.status}
    elif args.command == "evaluate":
        output = e.evaluate(args.case, args.decision_type, intent=args.intent)
    elif args.command == "feedback":
        fid = e.feedback(args.case, args.text, args.corrected_to, args.decision_id)
        output = {"feedback_id": fid, "cornerstone_candidates": e.feedback_context(fid)}
    elif args.command == "confirm-feedback":
        e.confirm_feedback(args.id, args.interpretation); output = {"status": "interpretation_confirmed"}
    elif args.command == "compare":
        output = e.cornerstone_comparison(args.current, args.cornerstone)
    elif args.command == "draft-exception":
        output = e.draft_exception_from_feedback(args.feedback, args.cornerstone, args.feature, args.parent, args.rule_id)
    elif args.command == "add-rule":
        output = e.add_rule(**data_arg(args.rule))
    elif args.command == "preview-rule":
        output = e.rule_preview(args.rule)
    elif args.command == "publish-rule":
        output = e.publish_rule(args.rule, args.expert_confirmed, args.impacts_reviewed)
    elif args.command == "questions":
        sql = "SELECT * FROM questions" + (" WHERE case_id=?" if args.case else "") + " ORDER BY created_at"
        output = e.store.many(sql, (args.case,) if args.case else ())
    elif args.command == "answer-question":
        output = e.answer_question(args.id, args.text)
    elif args.command == "confirm-answer":
        output = e.confirm_question_feature(args.id, json.loads(args.value), args.status, args.evidence, args.expert_confirmed)
    elif args.command == "debrief":
        narrative = args.text or input("作業で何を行い、なぜそれを選び、結果はどうでしたか？\n> ")
        output = e.report_debrief(args.case, narrative, extractor(args))
    elif args.command == "extend-debrief":
        output = e.extend_debrief(args.id, args.text, extractor(args))
    elif args.command == "confirm-debrief":
        output = e.confirm_debrief(args.id, data_arg(args.edited) if args.edited else None)
    elif args.command == "approve-procedure":
        output = e.approve_procedure(args.id, args.expert_approved)
    elif args.command == "replay":
        output = e.replay(args.mode)
    else:
        raise RuntimeError(args.command)
    print(json.dumps(output, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
