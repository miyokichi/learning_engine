import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
from learning_engine import Engine
from learning_engine.mcp_server import MCPServer, PRIVILEGED, create_approval_token

spec=importlib.util.spec_from_file_location('example_client',Path(__file__).resolve().parents[1]/'examples/mcp_client.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
Client=module.Client


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/'work.db';self.key=secrets.token_bytes(32)
        self.keyfile=Path(self.tmp.name)/'ui.key';self.keyfile.write_bytes(self.key)
        self.clients=[];self.addCleanup(self.close)

    def close(self):
        for c in self.clients:
            if c.process.poll() is None:c.close()

    def client(self,human=False):
        command=[sys.executable,'-m','learning_engine','--db',str(self.db),'serve-mcp']
        if human:command+=['--human-ui-key-file',str(self.keyfile)]
        c=Client(command);self.clients.append(c);return c

    def approve(self,ui,name,**args):
        token=create_approval_token(self.key,name,args)
        return ui.call(name,**args,approval_token=token)

    def test_process_lifecycle_learning_and_reuse(self):
        agent=self.client();ui=self.client(True)
        self.assertTrue(agent.request('ping')=={})
        names={t['name'] for t in agent.request('tools/list')['tools']}
        self.assertFalse(names & PRIVILEGED)
        self.assertEqual({t['name'] for t in ui.request('tools/list')['tools']},PRIVILEGED)
        w=agent.call('le_start_work',goal='Via',context={},idempotency_key='start')['work_id']
        self.assertEqual(agent.call('le_start_work',goal='Via',context={},idempotency_key='start')['work_id'],w)
        d=agent.call('le_request_decision',work_id=w,target='reuse',known_facts={},checkpoint={'next':'calculate'},idempotency_key='decision')
        self.assertEqual(d['reason'],'no_rules');self.assertFalse(d['external_execution_authorized'])
        q=d['questions'][0]['question_id']
        agent.call('le_record_answer',work_id=w,question_id=q,answer='側壁が異なる',idempotency_key='answer')
        for _ in range(2):agent.call('le_record_answer',work_id=w,question_id=q,answer='側壁が異なる',idempotency_key='answer')
        with self.assertRaises(RuntimeError):agent.call('le_confirm_answer',work_id=w,question_id=q,interpretation={'value':'different'},expert_confirmed=True)
        # The confirmed categorical judgment basis may be generalized only later.
        self.approve(ui,'le_confirm_answer',work_id=w,question_id=q,interpretation={'value':'different','rule_candidate':{'conclusion':'VALIDATE','predicates':[{'feature':'judgment_basis','op':'eq','value':'different'}],'reason':'側壁差'}})
        agent.call('le_resume_work',work_id=w)
        agent.call('le_record_step',work_id=w,action='計算',reason='再検証',result='完了',evidence='架空資料')
        agent.call('le_complete_work',work_id=w,result='完了')
        candidates=agent.call('le_list_learning_candidates',work_id=w)['result']
        for c in candidates:self.approve(ui,'le_review_learning_candidate',candidate_id=c['id'],action='approve')
        agent.close();ui.close()
        agent=self.client()
        self.assertEqual(agent.call('le_get_resume_context',work_id=w)['work']['checkpoint'],{'next':'calculate'})
        other=agent.call('le_start_work',goal='next',context={})['work_id']
        decision=agent.call('le_request_decision',work_id=other,target='reuse',known_facts={'judgment_basis':{'value':'different','status':'documented','source':'fixture'}})
        self.assertEqual(decision['status'],'READY')
        with self.assertRaises(RuntimeError):agent.call('le_record_step',work_id=w,action='late',reason=None,result=None,evidence=None)

    def test_token_binding_replay_and_cross_work(self):
        a=self.client();ui=self.client(True)
        w=a.call('le_start_work',goal='x',context={})['work_id'];other=a.call('le_start_work',goal='y',context={})['work_id']
        d=a.call('le_request_decision',work_id=w,target='x',known_facts={});q=d['questions'][0]['question_id']
        a.call('le_record_answer',work_id=w,question_id=q,answer='不明')
        args=dict(work_id=w,question_id=q,interpretation={'value':None})
        token=create_approval_token(self.key,'le_confirm_answer',args)
        with self.assertRaises(RuntimeError):ui.call('le_confirm_answer',**{**args,'work_id':other},approval_token=token)
        ui.call('le_confirm_answer',**args,approval_token=token)
        with self.assertRaises(RuntimeError):ui.call('le_confirm_answer',**args,approval_token=token)
        with self.assertRaises(RuntimeError):a.call('le_record_answer',work_id=other,question_id=q,answer='wrong')
        # Database retained Unknown, not false.
        with Engine(self.db) as e:self.assertIsNone(e.case(d['case_id'])['features']['judgment_basis']['value'])

    def test_expert_review_no_rule_without_publication(self):
        a=self.client();ui=self.client(True)
        wid=a.call('le_start_work',goal='review',context={})['work_id']
        d=a.call('le_request_decision',work_id=wid,target='new domain',known_facts={})
        r=self.approve(ui,'le_record_expert_judgment',work_id=wid,case_id=d['case_id'],conclusion='INVESTIGATE',reason='適用条件が不足')
        self.assertEqual(r['status'],'READY_TO_RESUME')
        a.call('le_resume_work',work_id=wid)
        with Engine(self.db) as e:self.assertFalse(e.rules())
        a.call('le_complete_work',work_id=wid,result='done')
        self.assertTrue(a.call('le_list_learning_candidates',work_id=wid)['result'])

    def test_malformed_stdio_and_eof(self):
        messages=['bad json', '[]', json.dumps({'jsonrpc':'2.0','id':1,'method':'ping'}),json.dumps({'jsonrpc':'2.0','id':2,'method':'tools/list'})]
        p=subprocess.run([sys.executable,'-m','learning_engine','--db',str(self.db),'serve-mcp'],input='\n'.join(messages)+'\n',capture_output=True,text=True,timeout=10)
        self.assertEqual(p.returncode,0,p.stderr)
        out=[json.loads(line) for line in p.stdout.splitlines()]
        self.assertEqual(out[0]['error']['code'],-32700)
        self.assertEqual(out[1]['error']['code'],-32600)
        self.assertEqual(out[2]['result'],{})
        self.assertEqual(out[3]['error']['code'],-32002)

    def test_schema_rejects_wrong_types_and_approval_spoof(self):
        with Engine(self.db) as e:
            server=MCPServer(e)
            for args in ({'goal':'x','context':[]},{'goal':'x','context':{},'expert_confirmed':True}):
                with self.assertRaises(ValueError):server.call('le_start_work',args)
            with self.assertRaises(PermissionError):server.call('le_review_learning_candidate',{'candidate_id':'x','action':'approve','approval_token':'x'})

    def test_client_runs_in_clean_python_environment(self):
        import ast
        import os
        import venv
        root=Path(self.tmp.name)/'client-env'
        venv.EnvBuilder(with_pip=False).create(root)
        python=root/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        probe=subprocess.run([str(python),'-I','-c',"import importlib.util; assert importlib.util.find_spec('learning_engine') is None"],capture_output=True,text=True,timeout=10)
        self.assertEqual(probe.returncode,0,probe.stderr)
        script=Path(__file__).resolve().parents[1]/'examples/mcp_client.py'
        result=subprocess.run([str(python),'-I',str(script),sys.executable,str(self.db)],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        decision=ast.literal_eval(result.stdout.splitlines()[-1])
        self.assertEqual(decision['status'],'NEED_INPUT')
        self.assertFalse(decision['external_execution_authorized'])

    def test_expired_approval_and_failed_operation_do_not_consume_valid_token(self):
        from unittest.mock import patch
        agent=self.client();ui=self.client(True)
        w=agent.call('le_start_work',goal='x',context={})['work_id']
        d=agent.call('le_request_decision',work_id=w,target='x',known_facts={})
        q=d['questions'][0]['question_id']
        args=dict(work_id=w,question_id=q,interpretation={'value':None})
        with patch('learning_engine.mcp_server.time.time',return_value=0):
            expired=create_approval_token(self.key,'le_confirm_answer',args)
        with self.assertRaises(RuntimeError):ui.call('le_confirm_answer',**args,approval_token=expired)
        token=create_approval_token(self.key,'le_confirm_answer',args)
        with self.assertRaises(RuntimeError):ui.call('le_confirm_answer',**args,approval_token=token)
        agent.call('le_record_answer',work_id=w,question_id=q,answer='未確認',idempotency_key='raw')
        with self.assertRaises(RuntimeError):agent.call('le_record_answer',work_id=w,question_id=q,answer='別の回答',idempotency_key='raw')
        ui.call('le_confirm_answer',**args,approval_token=token)
        with self.assertRaises(RuntimeError):ui.call('le_confirm_answer',**args,approval_token=token)
