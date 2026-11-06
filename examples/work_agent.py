"""Fictional mock agent; no external calculation or real expert truth."""
from learning_engine import Engine, WorkSession


def run_demo(path=':memory:'):
    with Engine(path) as engine:
        work = WorkSession(engine)
        wid = work.start_work('架空Via抵抗計算',{'fictional':True})['work_id']
        question = dict(field_id='sidewall_changed',target_decision='model_reuse',missing_information='側壁形状は従来と異なりますか',why_needed='既存モデルの適用を判断するため',possible_answers=['異なる','同等','未確認'],how_answer_changes_next_step='異なるなら再検証します',source_case_or_rule=[],priority='critical')
        decision = work.request_decision(wid,'既存モデルを使えるか',{},proposed_questions=[question],checkpoint={'agent_task':'mock_calculation','next_step':'compare'})
        for q in decision['questions']:
            work.record_answer(wid,q['question_id'],'側壁が異なる場合は直接使えません',idempotency_key=q['question_id'])
            interpretation={'value':True if q['field_id']=='sidewall_changed' else '側壁の同等性'}
            if q['field_id']=='sidewall_changed':
                interpretation['rule_candidate']={'conclusion':'REQUIRE_VALIDATION','predicates':[{'feature':'sidewall_changed','op':'eq','value':True}],'reason':'架空教材で確認した条件'}
            work.confirm_answer(wid,q['question_id'],interpretation,expert_confirmed=True)
        work.resume_work(wid)
        work.record_step(wid,'断面比較','形状差の確認','側壁が異なる','架空の断面')
        work.record_step(wid,'模擬計算','モデルを検証','計算完了','架空の結果')
        work.complete_work(wid,'架空計算完了')
        for c in work.list_learning_candidates(wid):
            work.review_learning_candidate(c['id'],'approve',expert_confirmed=True)
        second=work.start_work('次の架空Via計算',{'fictional':True})['work_id']
        result=work.request_decision(second,'既存モデルの再利用',{'sidewall_changed':{'value':True,'status':'documented','source':'架空教材'}})
        return {'fictional':True,'first_work':wid,'next_work':second,'status':result['status'],'decision':result['decision']['decision']}


if __name__=='__main__':
    print('架空教材です。実際の計算・専門家承認ではありません。')
    print(run_demo())
