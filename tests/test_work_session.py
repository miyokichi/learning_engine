import tempfile
from pathlib import Path
import unittest
from learning_engine import Engine, WorkSession


class WorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'work.db'
        self.e=Engine(self.path); self.addCleanup(self.e.close)
        self.w=WorkSession(self.e)

    def work(self):
        return self.w.start_work('架空Via抵抗計算',{})['work_id']

    def learn(self):
        wid=self.work()
        q={'field_id':'sidewall_changed','target_decision':'model_reuse','missing_information':'側壁は異なりますか','why_needed':'モデルの適用を変えます','possible_answers':['はい','いいえ','不明'],'how_answer_changes_next_step':'異なる場合は再検証','source_case_or_rule':[],'priority':'critical'}
        r=self.w.request_decision(wid,'既存抵抗モデルの再利用',{},proposed_questions=[q],checkpoint={'task':'resistance','next':'calculate'})
        self.assertEqual(r['reason'],'no_rules')
        # Answer both the general judgment-basis question and the shape question.
        for question in r['questions']:
            qid=question['question_id']
            self.w.record_answer(wid,qid,'側壁が違うため直接利用できない',idempotency_key=qid)
            self.w.record_answer(wid,qid,'側壁が違うため直接利用できない',idempotency_key=qid)
            field=question['field_id']
            interpretation={'value':True if field=='sidewall_changed' else '形状の違い'}
            if field=='sidewall_changed':
                interpretation['rule_candidate']={'conclusion':'REQUIRE_VALIDATION','predicates':[{'feature':field,'op':'eq','value':True}],'reason':'側壁が変わると適用外'}
            self.w.confirm_answer(wid,qid,interpretation,expert_confirmed=True)
        self.assertEqual(self.w.get_work_status(wid)['status'],'READY_TO_RESUME')
        self.w.resume_work(wid)
        self.w.record_step(wid,'断面比較','形状差の確認','側壁が異なる','架空図')
        self.w.record_step(wid,'再計算','モデルを再検証','計算完了','架空結果')
        reflection=self.w.complete_work(wid,'完了')
        self.assertEqual(reflection,self.w.complete_work(wid,'完了'))
        self.assertFalse(self.e.rules())
        candidates=self.w.list_learning_candidates(wid)
        for c in candidates:
            self.w.review_learning_candidate(c['id'],'approve',expert_confirmed=True)
        return wid

    def test_two_work_learning_and_unknown(self):
        self.learn()
        wid=self.work()
        r=self.w.request_decision(wid,'再利用',{'sidewall_changed':{'value':True,'status':'documented','source':'架空観測'}})
        self.assertEqual(r['status'],'READY')
        self.assertEqual(r['decision']['decision'],'REQUIRE_VALIDATION')
        self.assertFalse(r['external_execution_authorized'])
        wid2=self.work(); r=self.w.request_decision(wid2,'再利用',{})
        self.assertEqual(r['status'],'NEED_INPUT')
        self.assertEqual(r['reason'],'missing_attributes')

    def test_restart_isolation_and_idempotency(self):
        wid=self.work(); other=self.work()
        r=self.w.request_decision(wid,'判断',{},idempotency_key='D')
        self.w.request_decision(wid,'判断',{},idempotency_key='D')
        self.assertEqual(len(self.w.get_work_status(wid)['cases']),1)
        q=r['questions'][0]['question_id']
        with self.assertRaises(ValueError): self.w.record_answer(other,q,'回答')
        self.w.record_answer(wid,q,'不明',idempotency_key='A')
        with self.assertRaises(ValueError): self.w.record_answer(wid,q,'違う',idempotency_key='A')
        self.e.close(); self.e=Engine(self.path); self.addCleanup(self.e.close); self.w=WorkSession(self.e)
        self.w.confirm_answer(wid,q,{'value':None},expert_confirmed=True)
        self.assertEqual(self.w.get_work_status(wid)['status'],'READY_TO_RESUME')
        self.assertEqual(self.w.get_work_status(other)['cases'],[])

    def test_no_implicit_approval_and_partial_review(self):
        wid=self.learn()
        with self.assertRaises(PermissionError): self.w.review_learning_candidate(self.w.list_learning_candidates(wid)[0]['id'],'approve')
        other=self.work(); self.w.request_decision(other,'判断',{})
        self.w.record_step(other,'予定','まだ実施していない',None,None,state='planned')
        self.w.complete_work(other,'中断終了')
        self.assertFalse(self.w.list_learning_candidates(other))

    def test_failure_preserves_answer_and_pause_cannot_skip_question(self):
        class Broken:
            def interpret(self,*a): raise TimeoutError()
        self.w.llm=Broken(); wid=self.work(); r=self.w.request_decision(wid,'判断',{})
        q=r['questions'][0]['question_id']
        self.assertEqual(self.w.record_answer(wid,q,'原文')['error'],'TimeoutError')
        self.w.pause_work(wid,{'next':'調査'})
        with self.assertRaises(ValueError): self.w.resume_work(wid)
        self.assertTrue(self.e.store.one('SELECT * FROM work_answers WHERE question_id=?',(q,)))

    def test_steps_without_decision_create_procedure_and_partial_review(self):
        wid=self.work()
        self.w.record_step(wid,'比較','確認','差あり','資料')
        self.w.complete_work(wid,'完了')
        c=self.w.list_learning_candidates(wid)[0]
        self.w.review_learning_candidate(c['id'],'defer',expert_confirmed=True)
        self.assertFalse(self.e.store.many('SELECT * FROM procedure_candidates'))
        self.w.review_learning_candidate(c['id'],'reject',expert_confirmed=True)
        self.assertFalse(self.e.store.many('SELECT * FROM procedure_candidates'))

    def test_multiple_cases_and_numeric_rule_rejection(self):
        wid=self.work()
        a=self.w.request_decision(wid,'A',{'x':{'value':2,'status':'documented','source':'資料'}})
        b=self.w.request_decision(wid,'B',{})
        self.assertNotEqual(a['case_id'],b['case_id'])
        self.assertEqual(len(self.w.get_work_status(wid)['cases']),2)
        class Reflection:
            def reflect_work(self,ctx):
                return [{'kind':'rule','body':{'case_id':a['case_id'],'evidence_events':[], 'conclusion':'OK','predicates':[{'feature':'x','op':'eq','value':2}]}}]
        self.w.llm=Reflection();self.w.complete_work(wid,'終了')
        c=self.w.list_learning_candidates(wid)[0]
        with self.assertRaises(ValueError):self.w.review_learning_candidate(c['id'],'approve',expert_confirmed=True)
        self.assertFalse(self.e.rules())

    def test_cli_reports_and_reflects_without_case_ids(self):
        from unittest.mock import patch
        from learning_engine.work_coordinator import work_cli
        inputs=iter(['断面比較をしたい','断面を比較した','/complete','','却下','/exit'])
        with patch('builtins.input',side_effect=lambda _:next(inputs)),patch('builtins.print'):
            work_cli(self.e)
        self.assertEqual(self.e.store.one('SELECT status FROM work_sessions')['status'],'COMPLETED')
        self.assertFalse(self.e.store.many('SELECT * FROM procedure_candidates'))

    def test_exception_commit_preserves_cornerstone_and_conflicting_root_rolls_back(self):
        self.e.add_case('OLD',{'shape':{'value':'via','status':'documented'},'coating':{'value':False,'status':'documented'}})
        self.e.review_case('OLD','REUSE')
        self.e.add_rule('ROOT','REUSE',[{'feature':'shape','op':'eq','value':'via'}],cornerstone_case_id='OLD')
        self.e.publish_rule('ROOT',True,True)
        wid=self.work()
        r=self.w.request_decision(wid,'再利用',{'shape':{'value':'via','status':'documented'},'coating':{'value':True,'status':'documented'}})
        body={'case_id':r['case_id'],'evidence_events':[],'conclusion':'VALIDATE','predicates':[{'feature':'coating','op':'eq','value':True}],'parent_rule_id':'ROOT','cornerstone_case_id':'OLD','reason':'膜による例外'}
        class Reflection:
            def reflect_work(self,context):return [{'kind':'rule','body':body}]
        self.w.llm=Reflection();self.w.complete_work(wid,'完了')
        candidate=self.w.list_learning_candidates(wid)[0]
        self.w.review_learning_candidate(candidate['id'],'approve',expert_confirmed=True)
        self.assertEqual(self.e.evaluate('OLD',persist=False)['decision'],'REUSE')
        self.assertEqual(self.e.evaluate(r['case_id'],persist=False)['decision'],'VALIDATE')
        other=self.work();d=self.w.request_decision(other,'再利用',{'shape':{'value':'via','status':'documented'}})
        body={'case_id':d['case_id'],'evidence_events':[],'conclusion':'WRONG','predicates':[{'feature':'shape','op':'eq','value':'via'}]}
        self.w.complete_work(other,'完了'); c=self.w.list_learning_candidates(other)[0]
        count=len(self.e.rules())
        with self.assertRaises(ValueError):self.w.review_learning_candidate(c['id'],'approve',expert_confirmed=True)
        self.assertEqual(len(self.e.rules()),count)
        self.assertEqual(self.e.evaluate('OLD',persist=False)['decision'],'REUSE')
