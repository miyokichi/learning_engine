"""Evidence-linked draft extraction and explicit learning commits."""
from .engine import new_id
from .store import encode, decode
from .logic import ACCEPTED_STATUSES


def generate_reflection(w, wid):
    with w.e.store.transaction():
        work = w.get_work_status(wid)
        if not work['ended_at']:
            raise ValueError('Complete work before reflection')
        old = w.e.store.one('SELECT body FROM work_reflections WHERE work_id=?', (wid,))
        if old:
            return decode(old['body'])
        events = w.events(wid)
        refs = [e['id'] for e in events]
        unresolved = [q for c in work['cases'] for q in w.e.store.many("SELECT * FROM questions WHERE case_id=? AND status IN ('open','answered_unverified')", (c['case_id'],))]
        if not work['cases'] and any(e['kind']=='STEP_RECORDED' and e['body']['state']=='executed' for e in events):
            cid = new_id('CASE')
            w.e.add_case(cid,{},metadata={'work_id':wid,'target':'procedure_context','decision_type':'procedure_context'})
            w.e.store.conn.execute('INSERT INTO work_cases VALUES(?,?,?)',(wid,cid,'procedure_context'))
            work = w.get_work_status(wid)
        proposals = []
        for c in work['cases']:
            case = w.e.case(c['case_id'])
            # A confirmed answer may contain a human-provided generalization.
            for a in w.e.store.many("SELECT a.body FROM work_answers a JOIN questions q ON q.id=a.question_id WHERE a.work_id=? AND q.case_id=? AND a.status='confirmed'", (wid,c['case_id'])):
                interpretation = decode(a['body'])['interpretation']
                rule = interpretation.get('rule_candidate')
                if rule:
                    proposals.append(('rule', {**rule, 'case_id': c['case_id'], 'evidence_events': refs}))
            steps = [e['body'] for e in events if e['kind'] == 'STEP_RECORDED' and e['body']['state'] == 'executed']
            if steps and not any(k=='procedure' for k,b in proposals):
                procedure = dict(trigger=work['goal'], goal=work['goal'], steps=[dict(action=s['action'],reason=s['reason'],result=s['result']) for s in steps], outcome=str(work['result']), decision_criteria=[], unverified_inferences=[], missing=['validation'], required_order='unknown')
                proposals.append(('procedure', {'case_id':c['case_id'],'procedure':procedure,'evidence_events':refs}))
        if w.llm and hasattr(w.llm, 'reflect_work'):
            try:
                extra = w.llm.reflect_work({'work':work,'events':events})
                if not isinstance(extra,list):
                    raise ValueError('Reflection must be a list')
                validated = []
                for p in extra:
                    if p['kind'] not in {'rule','procedure','concept','observation','causal_hypothesis'} or not set(p['body'].get('evidence_events',[])) <= set(refs):
                        raise ValueError('Invalid reflection evidence')
                    if p['body'].get('case_id') not in [c['case_id'] for c in work['cases']]:
                        raise ValueError('Foreign case')
                    validated.append((p['kind'],p['body']))
                proposals.extend(validated)
            except Exception as exc:
                unresolved.append({'extraction_error':type(exc).__name__})
        for event in events:
            if event['kind']=='EXPERT_JUDGMENT_RECORDED':
                proposals.append(('observation', {'case_id':event['target_id'], 'text':event['body']['conclusion']+': '+event['body']['reason'], 'source':'work_event:'+event['id'], 'evidence_events':[event['id']], 'scope': {'work_id':wid}}))
        ids = []
        for kind, body in proposals:
            cid = new_id('LC')
            duplicate = next((r['id'] for r in w.e.rules() if kind=='rule' and r['status']=='published' and r['conclusion']==body.get('conclusion') and r['predicates']==body.get('predicates')),None)
            body['existing_object_id'] = duplicate
            w.e.store.conn.execute('INSERT INTO learning_candidates VALUES(?,?,?,?,?,?)',(cid,wid,kind,encode(body),'pending',None))
            ids.append(cid)
        summary = {'work_id':wid,'candidate_ids':ids,'unresolved':unresolved,'evidence_events':refs,
                   'sections':{'judgment_criteria':[b for k,b in proposals if k=='rule'], 'procedures':[b for k,b in proposals if k=='procedure'], 'concepts':[b for k,b in proposals if k=='concept'], 'exceptions':[b for k,b in proposals if b.get('parent_rule_id')], 'unresolved':unresolved},
                   'followup_questions':['未解決の判断と根拠を確認してください'] if unresolved else []}
        w.e.store.conn.execute('INSERT INTO work_reflections VALUES(?,?)',(wid,encode(summary)))
        w._event(wid,'REFLECTION_CREATED',summary)
        return summary


def review_candidate(w, cid, action, correction, confirmed):
    if not confirmed:
        raise PermissionError('Explicit human approval is required')
    if action not in {'approve','reject','defer'}:
        raise ValueError('Invalid review action')
    with w.e.store.transaction():
        row = w.e.store.one('SELECT * FROM learning_candidates WHERE id=?',(cid,))
        if not row:
            raise KeyError(cid)
        body = correction if correction is not None else decode(row['body'])
        if row['status'] in {'approved','rejected'}:
            if (row['status']=='approved') != (action=='approve') or body != decode(row['body']):
                raise ValueError('Final review is immutable')
            return row
        cases = [c['case_id'] for c in w.get_work_status(row['work_id'])['cases']]
        if body.get('case_id') not in cases:
            raise ValueError('Candidate must reference this work')
        if not set(body.get('evidence_events',[])) <= {e['id'] for e in w.events(row['work_id'])}:
            raise ValueError('Foreign evidence')
        obj = None
        if action == 'approve':
            kind = row['kind']
            if kind == 'rule':
                predicates = body['predicates']
                case = w.e.case(body['case_id'])
                for p in predicates:
                    entry = case['features'].get(p['feature'],{})
                    if p['op']!='eq' or not isinstance(p['value'],(bool,str)) or entry.get('status') not in ACCEPTED_STATUSES or entry.get('value')!=p['value']:
                        raise ValueError('Rule requires confirmed categorical evidence; numeric boundaries need separate validation')
                duplicate = next((r['id'] for r in w.e.rules() if r['status']=='published' and r['decision_type']==body.get('decision_type','model_reuse') and r['predicates']==predicates and r['conclusion']==body['conclusion']),None)
                if duplicate:
                    obj = duplicate
                else:
                    w.e.review_case(body['case_id'],body['conclusion'])
                    parent = body.get('parent_rule_id')
                    if parent:
                        fid = w.e.feedback(body['case_id'],body.get('reason','Human correction'),body['conclusion'])
                        w.e.confirm_feedback(fid,body.get('reason','Human correction'))
                        preview = w.e.draft_exception_from_feedback(fid,body['cornerstone_case_id'],[p['feature'] for p in predicates],parent)
                    else:
                        preview = w.e.add_rule(new_id('R'),body['conclusion'],predicates,decision_type=body.get('decision_type','model_reuse'),cornerstone_case_id=body['case_id'],explanation=body.get('reason',''))
                    obj = preview['rule_id']
                    w.e.publish_rule(obj,True,True)
                    if parent:
                        w._event(row['work_id'],'DECISION_REVISED',{'rule_id':obj,'parent_rule_id':parent},body['case_id'],source='human')
            elif kind == 'procedure':
                from .llm import FakeExtractor, validate_procedure
                procedure = validate_procedure(body['procedure'])
                old = next((r for r in w.e.store.many("SELECT id,body FROM procedure_candidates WHERE status IN ('approved_candidate','validated_for_scope')") if decode(r['body']).get('goal')==procedure['goal'] and decode(r['body']).get('steps')==procedure['steps']),None)
                if old:
                    obj = old['id']
                else:
                    d = w.e.report_debrief(body['case_id'],encode(body),FakeExtractor(procedure))
                    obj = w.e.confirm_debrief(d['id'],procedure)['procedure_id']
                    w.e.approve_procedure(obj,True)
            else:
                knowledge = {**body,'source':'work_candidate:'+cid}
                existing = next((r for r in w.e.store.many("SELECT * FROM knowledge WHERE status='verified' AND kind=?",(kind,)) if {k:v for k,v in decode(r['body']).items() if k not in {'source','case_id','evidence_events','existing_object_id'}}=={k:v for k,v in knowledge.items() if k not in {'source','case_id','evidence_events','existing_object_id'}}),None)
                if existing:
                    obj = existing['id']
                else:
                    obj = w.e.propose_knowledge(kind,knowledge,body['case_id'])['id']
                    w.e.adopt_knowledge(obj,expert_confirmed=True)
        status = {'approve':'approved','reject':'rejected','defer':'deferred'}[action]
        w.e.store.conn.execute('UPDATE learning_candidates SET body=?,status=?,object_id=? WHERE id=?',(encode(body),status,obj,cid))
        w._event(row['work_id'],'KNOWLEDGE_APPROVED' if action=='approve' else 'CANDIDATE_REVIEWED',{'action':action,'object_id':obj},cid,source='human')
        return {'id':cid,'status':status,'object_id':obj}
