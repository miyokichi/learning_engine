"""Work CLI: narrative interpretation proposes operations, human confirms writes."""
import json
from .work_session import WorkSession


def work_cli(engine, extractor=None):
    api = WorkSession(engine,extractor)
    sessions = engine.store.many('SELECT id,goal FROM work_sessions ORDER BY started_at DESC LIMIT 5')
    print('Workモード。外部作業は実行しません。終了 /exit、状態 /status、再開 /resume、振り返り /complete。')
    for i,s in enumerate(sessions,1):
        print(f'{i}. {s["goal"]}')
    choice = input('再開する番号、Enterで新規> ').strip() if sessions else ''
    wid = sessions[int(choice)-1]['id'] if choice else None
    while True:
        try:
            text = input('仕事> ').strip()
            if text=='/exit':
                return
            if not wid:
                if text:
                    wid=api.start_work(text,{})['work_id']
                    print('仕事を開始しました。判断したいことや作業結果を話してください。')
                continue
            if text=='/status':
                print(json.dumps(api.get_resume_context(wid),ensure_ascii=False,indent=2)); continue
            if text=='/resume':
                print(api.resume_work(wid)); continue
            if text=='/complete':
                print(api.complete_work(wid,'人間による完了報告'))
                review_cli(api,wid,extractor); continue
            if not extractor:
                api.record_step(wid,text,'人間の報告',None,None)
                print('報告を保存しました。自然言語の判断解釈にはLLMを設定してください。'); continue
            with engine.store.transaction():
                api._event(wid,'NARRATIVE_RECORDED',{'text':text},source='human')
            system='''Interpret a Japanese work report as JSON {operation,payload}. Allowed operations: decision {target,known_facts}, step {action,reason,result,evidence,state}, answer {question_id,answer,interpretation:{value}}, complete {result}, resume {}. Never invent facts. Scalar facts are assumed. Read questions from work context. An interpretation is a proposal, never approval. No tools or approval operations. Return JSON only.'''
            parsed=extractor._complete(system,json.dumps({'text':text,'work':api.get_resume_context(wid)},ensure_ascii=False))
            op,p=parsed['operation'],parsed['payload']
            if op not in {'decision','step','answer','complete','resume'} or not isinstance(p,dict):
                raise ValueError('許可されていない提案です')
            print('解釈候補:',json.dumps(parsed,ensure_ascii=False))
            if input('正しければ「確認」> ').strip()!='確認':
                print('採用しません。修正内容をもう一度話してください。'); continue
            if op=='decision':
                print(api.request_decision(wid,p['target'],p['known_facts']))
            elif op=='step':
                api.record_step(wid,p['action'],p.get('reason'),p.get('result'),p.get('evidence'),state=p.get('state','executed'))
            elif op=='answer':
                api.record_answer(wid,p['question_id'],text)
                print(api.confirm_answer(wid,p['question_id'],p['interpretation'],expert_confirmed=True))
            elif op=='resume':
                print(api.resume_work(wid))
            else:
                print(api.complete_work(wid,p['result'])); review_cli(api,wid,extractor)
        except (EOFError,KeyboardInterrupt):
            return
        except Exception as exc:
            print('保留:',type(exc).__name__,str(exc))


def review_cli(api,wid,extractor):
    print('学習候補を順番に確認します。承認は正式な知識採用で、作業回答の確認とは別です。')
    candidates = api.list_learning_candidates(wid)
    for i,c in enumerate(candidates,1):
        print(i,c['kind'],json.dumps(c['body'],ensure_ascii=False,indent=2))
    if candidates and input('全候補を承認する場合は「まとめて承認」、個別ならEnter> ').strip()=='まとめて承認':
        for c in candidates:
            if c['status'] not in {'approved','rejected'}:
                print(api.review_learning_candidate(c['id'],'approve',expert_confirmed=True))
        return
    for c in candidates:
        if c['status'] in {'approved','rejected'}:
            continue
        print(c['kind'],json.dumps(c['body'],ensure_ascii=False,indent=2))
        action=input('承認 / 修正 / 保留 / 却下> ').strip()
        if action=='修正' and extractor:
            note=input('修正内容を文章で> ')
            body=extractor._complete('Return ONLY JSON for the corrected candidate body. Preserve case_id and evidence_events. Do not approve or invent evidence.',json.dumps({'candidate':c,'human_correction':note},ensure_ascii=False))
            print(json.dumps(body,ensure_ascii=False,indent=2))
            if input('修正して承認する場合は「確認」> ').strip()=='確認':
                print(api.review_learning_candidate(c['id'],'approve',correction=body,expert_confirmed=True))
            continue
        if action in {'承認','保留','却下'}:
            print(api.review_learning_candidate(c['id'],{'承認':'approve','保留':'defer','却下':'reject'}[action],expert_confirmed=True))
