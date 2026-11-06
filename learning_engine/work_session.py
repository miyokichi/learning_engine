"""Persistent learning-side work lifecycle. Never executes an external action."""
from .engine import new_id, now, timestamp
from .store import encode, decode
from .question_planner import QuestionPlanner
from .knowledge import validate_feature

SQL = '''
CREATE TABLE IF NOT EXISTS work_sessions(id TEXT PRIMARY KEY,goal TEXT NOT NULL,context TEXT NOT NULL,status TEXT NOT NULL,checkpoint TEXT,result TEXT,started_at TEXT NOT NULL,ended_at TEXT);
CREATE TABLE IF NOT EXISTS work_cases(work_id TEXT NOT NULL REFERENCES work_sessions(id),case_id TEXT NOT NULL REFERENCES cases(id),target TEXT NOT NULL,PRIMARY KEY(work_id,case_id));
CREATE TABLE IF NOT EXISTS work_events(id TEXT PRIMARY KEY,work_id TEXT NOT NULL REFERENCES work_sessions(id),kind TEXT NOT NULL,target_id TEXT,body TEXT NOT NULL,occurred_at TEXT NOT NULL,recorded_at TEXT NOT NULL,source TEXT NOT NULL,idempotency_key TEXT,UNIQUE(work_id,idempotency_key));
CREATE TABLE IF NOT EXISTS work_answers(question_id TEXT PRIMARY KEY REFERENCES questions(id),work_id TEXT NOT NULL REFERENCES work_sessions(id),body TEXT NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learning_candidates(id TEXT PRIMARY KEY,work_id TEXT NOT NULL REFERENCES work_sessions(id),kind TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,object_id TEXT);
CREATE TABLE IF NOT EXISTS work_operation_results(event_id TEXT PRIMARY KEY REFERENCES work_events(id),body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS work_reflections(work_id TEXT PRIMARY KEY REFERENCES work_sessions(id),body TEXT NOT NULL);
'''


class WorkSession:
    def __init__(self, engine, extractor=None):
        self.e, self.llm = engine, extractor

    def _work(self, wid, active=False):
        row = self.e.store.one('SELECT * FROM work_sessions WHERE id=?', (wid,))
        if not row:
            raise KeyError(wid)
        if active and row['ended_at']:
            raise ValueError('Work is already completed')
        return row

    def _event(self, wid, kind, body, target=None, source='external_agent', occurred_at=None, key=None):
        stamp = now()
        eid = new_id('WE')
        self.e.store.conn.execute('INSERT INTO work_events VALUES(?,?,?,?,?,?,?,?,?)',
            (eid, wid, kind, target, encode(body), timestamp(occurred_at) if occurred_at else stamp, stamp, source, key))
        self.e.store.audit(kind, eid, {'work_id': wid, 'target_id': target, 'source': source}, stamp)
        return eid

    def _retry(self, wid, key, kind, body):
        if not key:
            return None
        row = self.e.store.one('SELECT * FROM work_events WHERE work_id=? AND idempotency_key=?', (wid, key))
        if row and (row['kind'] != kind or decode(row['body']) != body):
            raise ValueError('Idempotency key reused with different content')
        return row

    def start_work(self, goal, context, *, idempotency_key=None):
        if not isinstance(goal, str) or not goal.strip() or not isinstance(context, dict):
            raise ValueError('Work needs a goal and context object')
        with self.e.store.transaction():
            if idempotency_key:
                old = self.e.store.one("SELECT work_id,body FROM work_events WHERE kind='WORK_STARTED' AND idempotency_key=?", (idempotency_key,))
                if old:
                    if decode(old['body']) != {'goal': goal, 'context': context}:
                        raise ValueError('Idempotency key reused')
                    return self.get_work_status(old['work_id'])
            wid = new_id('WORK')
            self.e.store.conn.execute('INSERT INTO work_sessions VALUES(?,?,?,?,?,?,?,?)', (wid, goal, encode(context), 'ACTIVE', None, None, now(), None))
            self._event(wid, 'WORK_STARTED', {'goal': goal, 'context': context}, key=idempotency_key)
            return self.get_work_status(wid)

    def get_work_status(self, work_id):
        row = self._work(work_id)
        for k in ('context', 'checkpoint', 'result'):
            row[k] = decode(row[k])
        row['work_id'] = row['id']
        row['cases'] = self.e.store.many('SELECT case_id,target FROM work_cases WHERE work_id=?', (work_id,))
        row['external_execution_authorized'] = False
        return row

    def events(self, work_id):
        self._work(work_id)
        return [{**r, 'body': decode(r['body'])} for r in self.e.store.many('SELECT * FROM work_events WHERE work_id=? ORDER BY recorded_at,id', (work_id,))]

    def record_step(self, work_id, action, reason, result, evidence, *, state='executed', occurred_at=None, source='external_agent', idempotency_key=None):
        if state not in {'executed', 'planned', 'hypothesis'} or not isinstance(action, str) or not action.strip():
            raise ValueError('Invalid step')
        body = dict(action=action, reason=reason, result=result, evidence=evidence, state=state)
        with self.e.store.transaction():
            self._work(work_id, True)
            old = self._retry(work_id, idempotency_key, 'STEP_RECORDED', body)
            return old['id'] if old else self._event(work_id, 'STEP_RECORDED', body, source=source, occurred_at=occurred_at, key=idempotency_key)

    def request_decision(self, work_id, target, known_facts, *, decision_type='model_reuse', checkpoint=None, case_id=None, proposed_questions=(), explore=False, idempotency_key=None):
        if not isinstance(target,str) or not target.strip() or not isinstance(known_facts,dict):
            raise ValueError('Decision needs a target and facts object')
        body = dict(target=target, known_facts=known_facts, decision_type=decision_type, checkpoint=checkpoint, case_id=case_id, proposed_questions=list(proposed_questions), explore=explore)
        with self.e.store.transaction():
            self._work(work_id, True)
            old = self._retry(work_id, idempotency_key, 'DECISION_REQUESTED', body)
            if old:
                return decode(self.e.store.one('SELECT body FROM work_operation_results WHERE event_id=?',(old['id'],))['body'])
            if case_id:
                if not self.e.store.one('SELECT 1 FROM work_cases WHERE work_id=? AND case_id=?', (work_id, case_id)):
                    raise ValueError('Case belongs to another work')
                if known_facts:
                    raise ValueError('For existing cases supply facts through confirmed answers')
            else:
                facts = {}
                for field, value in known_facts.items():
                    entry = value if isinstance(value, dict) else {'value': value, 'status': 'assumed', 'source': 'agent_proposal'}
                    validate_feature(entry)
                    facts[field] = entry
                case_id = new_id('CASE')
                self.e.add_case(case_id, facts, metadata={'work_id': work_id, 'target': target, 'decision_type': decision_type})
                self.e.store.conn.execute('INSERT INTO work_cases VALUES(?,?,?)', (work_id, case_id, target))
            event_id = self._event(work_id, 'DECISION_REQUESTED', body, case_id, key=idempotency_key)
            self.e.store.conn.execute('UPDATE work_sessions SET checkpoint=? WHERE id=?', (encode(checkpoint), work_id))
            if self.llm and not proposed_questions:
                try:
                    proposal = self.llm.interpret(target, {'work': self.get_work_status(work_id), 'case': self.e.case(case_id), 'questions': []})
                    from .conversation_schema import validate_intent
                    proposed_questions = validate_intent(proposal)['questions']
                except Exception as exc:
                    self._event(work_id, 'LLM_FAILED', {'error': type(exc).__name__}, case_id)
            result = self._decision_result(work_id, case_id, decision_type, proposed_questions, explore)
            self.e.store.conn.execute('INSERT INTO work_operation_results VALUES(?,?)',(event_id,encode(result)))
            return result

    def _decision_result(self, wid, cid, dtype, proposed=(), explore=False, persist=True):
        result = self.e.evaluate(cid, dtype, intent='explore' if explore else 'commit', persist=persist)
        questions = QuestionPlanner(self.e).ask_batch(cid, dtype, proposed) if persist else QuestionPlanner(self.e).select(cid, dtype)
        roots = [r for r in self.e.rules() if r['status'] == 'published' and r['decision_type'] == dtype]
        reason = 'no_rules' if not roots else ('missing_attributes' if result['unknown_fields'] else result['reason'])
        status = {'EXECUTE': 'READY', 'ASK': 'NEED_INPUT'}.get(result['gate'], result['gate'])
        if any(q['priority']=='critical' for q in questions) and status not in {'BLOCK', 'EXPLORE'}:
            status = 'NEED_INPUT'
        out = dict(work_id=wid, case_id=cid, status=status, reason=reason, decision=result, questions=questions, external_execution_authorized=False)
        if persist:
            self.e.store.conn.execute('UPDATE work_sessions SET status=? WHERE id=?', (status, wid))
            self._event(wid, 'KNOWLEDGE_APPLIED' if status == 'READY' else 'UNKNOWN_DETECTED', out, cid)
            for q in questions:
                self._event(wid, 'QUESTION_ASKED', q, q['question_id'])
        return out

    def _question(self, wid, qid):
        q = self.e.store.one('SELECT q.* FROM questions q JOIN work_cases c ON c.case_id=q.case_id WHERE c.work_id=? AND q.id=?', (wid, qid))
        if not q:
            raise ValueError('Question does not belong to this work')
        return q

    def record_answer(self, work_id, question_id, answer, *, idempotency_key=None):
        body = dict(question_id=question_id, answer=answer)
        with self.e.store.transaction():
            self._work(work_id, True)
            self._question(work_id, question_id)
            old = self._retry(work_id, idempotency_key, 'ANSWER_RECORDED', body)
            if old:
                return self.e.store.one('SELECT * FROM work_answers WHERE question_id=?', (question_id,))
            self.e.answer_question(question_id, answer)
            self.e.store.conn.execute('INSERT INTO work_answers VALUES(?,?,?,?) ON CONFLICT(question_id) DO UPDATE SET body=excluded.body,status=excluded.status', (question_id, work_id, encode({'raw': answer}), 'unconfirmed'))
            self._event(work_id, 'ANSWER_RECORDED', body, question_id, source='human', key=idempotency_key)
        if self.llm:
            try:
                proposal = self.llm.interpret(answer, {'questions': [self._question(work_id, question_id)], 'work': self.get_work_status(work_id)})
                return {'raw': answer, 'interpretation_proposal': proposal, 'confirmed': False}
            except Exception as exc:
                return {'raw': answer, 'error': type(exc).__name__, 'confirmed': False}
        return {'raw': answer, 'confirmed': False}

    def confirm_answer(self, work_id, question_id, interpretation, *, expert_confirmed=False):
        if not expert_confirmed:
            raise PermissionError('Human confirmation required')
        if not isinstance(interpretation, dict) or 'value' not in interpretation:
            raise ValueError('Interpretation needs value; unknown is null')
        with self.e.store.transaction():
            self._work(work_id, True)
            q = self._question(work_id, question_id)
            old = self.e.store.one('SELECT * FROM work_answers WHERE question_id=?', (question_id,))
            if not old:
                raise ValueError('Record raw answer first')
            if old['status'] == 'confirmed':
                if decode(old['body'])['interpretation'] != interpretation:
                    raise ValueError('Confirmed answer is immutable')
                return self.get_resume_context(work_id)
            value = interpretation['value']
            self.e.confirm_question_feature(question_id, value, status='unknown' if value is None else 'expert_confirmed', expert_confirmed=True)
            self.e.store.conn.execute('UPDATE work_answers SET body=?,status=? WHERE question_id=?', (encode({**decode(old['body']), 'interpretation': interpretation}), 'confirmed', question_id))
            self._event(work_id, 'ANSWER_CONFIRMED', interpretation, question_id, source='human')
            self._decision_result(work_id, q['case_id'], q['target_decision'])
            remaining = self.e.store.one("SELECT 1 FROM questions q JOIN work_cases c ON c.case_id=q.case_id WHERE c.work_id=? AND q.status IN ('open','answered_unverified')", (work_id,))
            if not remaining:
                self.e.store.conn.execute("UPDATE work_sessions SET status='READY_TO_RESUME' WHERE id=? AND status<>'BLOCK'", (work_id,))
            return self.get_resume_context(work_id)

    def get_resume_context(self, work_id):
        return {'work': self.get_work_status(work_id), 'events': self.events(work_id), 'answers': [{**r, 'body': decode(r['body'])} for r in self.e.store.many('SELECT * FROM work_answers WHERE work_id=?', (work_id,))], 'external_execution_authorized': False}

    def pause_work(self, work_id, checkpoint=None):
        with self.e.store.transaction():
            self._work(work_id, True)
            self.e.store.conn.execute("UPDATE work_sessions SET status='PAUSED',checkpoint=COALESCE(?,checkpoint) WHERE id=?", (encode(checkpoint) if checkpoint is not None else None, work_id))
            self._event(work_id, 'WORK_PAUSED', {'checkpoint': checkpoint})
        return self.get_work_status(work_id)

    def resume_work(self, work_id):
        with self.e.store.transaction():
            row = self._work(work_id, True)
            if row['status'] == 'ACTIVE':
                return self.get_resume_context(work_id)
            if row['status'] not in {'READY_TO_RESUME', 'PAUSED', 'READY', 'EXPLORE'}:
                raise ValueError('Resolve pending input or review before resuming')
            # Pausing cannot bypass an unresolved question or a BLOCK.
            for c in self.get_work_status(work_id)['cases']:
                case = self.e.case(c['case_id'])
                r = self.e.evaluate(c['case_id'], case['metadata']['decision_type'], persist=False)
                if r['gate'] == 'BLOCK':
                    raise ValueError('Blocked decision')
            if self.e.store.one("SELECT 1 FROM questions q JOIN work_cases c ON c.case_id=q.case_id WHERE c.work_id=? AND q.status IN ('open','answered_unverified')", (work_id,)):
                raise ValueError('Unanswered questions remain')
            if row['status'] != 'ACTIVE':
                self._event(work_id, 'WORK_RESUMED', {})
                self.e.store.conn.execute("UPDATE work_sessions SET status='ACTIVE' WHERE id=?", (work_id,))
        return self.get_resume_context(work_id)

    def complete_work(self, work_id, result):
        with self.e.store.transaction():
            row = self._work(work_id)
            if row['ended_at']:
                if decode(row['result']) != result:
                    raise ValueError('Completion result is immutable')
            else:
                self.e.store.conn.execute("UPDATE work_sessions SET status='COMPLETED',result=?,ended_at=? WHERE id=?", (encode(result), now(), work_id))
                self._event(work_id, 'WORK_COMPLETED', {'result': result})
            return self.generate_reflection(work_id)

    def generate_reflection(self, work_id):
        from .reflection import generate_reflection
        return generate_reflection(self, work_id)

    def list_learning_candidates(self, work_id):
        self._work(work_id)
        return [{**r, 'body': decode(r['body'])} for r in self.e.store.many('SELECT * FROM learning_candidates WHERE work_id=? ORDER BY rowid', (work_id,))]

    def review_learning_candidate(self, candidate_id, action, correction=None, *, expert_confirmed=False):
        from .reflection import review_candidate
        return review_candidate(self, candidate_id, action, correction, expert_confirmed)

    def get_reusable_knowledge(self, context):
        # Scope matching is explicit. Related items are never execution permission.
        from .logic import all_predicates
        facts = context.get('features', {})
        rules = [{**r, 'applicability': all_predicates(facts, r['predicates']).value} for r in self.e.rules() if r['status'] == 'published' and r['decision_type'] == context.get('decision_type', 'model_reuse')]
        concepts = []
        for r in self.e.store.many("SELECT * FROM knowledge WHERE status='verified'"):
            body = decode(r['body'])
            scope = body.get('scope')
            if scope is not None and all(facts.get(k, {}).get('value') == v and facts.get(k, {}).get('status') in {'expert_confirmed','documented','measured','derived_verified'} for k,v in scope.items()):
                concepts.append({**r, 'body': body, 'applicability': 'scope_matched'})
        return {'rules': rules, 'knowledge': concepts, 'approved_cases_related_only': [self.e.case(r['id']) for r in self.e.store.many("SELECT id FROM cases WHERE review_status='expert_approved'") if self.e.case(r['id'])['metadata'].get('decision_type','model_reuse')==context.get('decision_type','model_reuse')], 'procedures_related_only': self.e.store.many("SELECT * FROM procedure_candidates WHERE status IN ('approved_candidate','validated_for_scope')"), 'external_execution_authorized': False}
