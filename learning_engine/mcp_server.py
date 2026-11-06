"""Standalone stdio MCP adapter. No LLM or external execution required."""
import base64
import hashlib
import hmac
import json
import logging
from pathlib import Path
import secrets
import sys
import time
from .engine import Engine
from .work_session import WorkSession
from .mcp_protocol import validate

LOG=logging.getLogger(__name__)
STR={'type':'string','minLength':1}
OBJ={'type':'object'}
ANY={}
SCALAR={'type':['string','number','boolean','null']}
FEATURE={'oneOf':[SCALAR,{'type':'object','properties':{'value':SCALAR,'status':{'type':'string','enum':['measured','documented','expert_confirmed','derived_verified','unknown','assumed','conflicting']},'source':STR,'unit':{'type':['string','null']},'definition_version':{'type':'integer'}},'required':['value','status'],'additionalProperties':False}]}
FACTS={'type':'object','additionalProperties':FEATURE}
W={'work_id':STR}
KEY={'idempotency_key':STR}
TOKEN={'approval_token':STR}


def schema(properties,required):
    return {'type':'object','properties':properties,'required':required,'additionalProperties':False}


def definitions():
    entries=[
        ('start_work','Start a work session.',{'goal':STR,'context':OBJ,**KEY},['goal','context']),
        ('get_work_status','Get persisted work status.',W,['work_id']),
        ('get_reusable_knowledge','Related knowledge is not execution permission.',{'context':OBJ},['context']),
        ('request_decision','Evaluate; READY never authorizes external execution.',{**W,'target':STR,'known_facts':FACTS,'decision_type':STR,'checkpoint':ANY,'case_id':STR,'proposed_questions':{'type':'array','items':OBJ,'maxItems':2},'explore':{'type':'boolean'},**KEY},['work_id','target','known_facts']),
        ('record_step','Record executed, planned or hypothetical work.',{**W,'action':STR,'reason':{'type':['string','null']},'result':ANY,'evidence':ANY,'state':{'type':'string','enum':['executed','planned','hypothesis']},'source':STR,'occurred_at':STR,**KEY},['work_id','action','reason','result','evidence']),
        ('record_answer','Store raw reported answer; does not confirm a human approval.',{**W,'question_id':STR,'answer':STR,**KEY},['work_id','question_id','answer']),
        ('confirm_answer','TRUSTED HUMAN UI ONLY. Confirm this exact interpretation.',{**W,'question_id':STR,'interpretation':schema({'value':SCALAR,'rule_candidate':OBJ},['value']),**TOKEN},['work_id','question_id','interpretation','approval_token']),
        ('get_resume_context','Return explicit checkpoint and learning context.',W,['work_id']),
        ('pause_work','Pause learning-side state.',{**W,'checkpoint':ANY},['work_id']),
        ('resume_work','Resume state only, never execute external actions.',W,['work_id']),
        ('complete_work','Complete work and produce unapproved reflection.',{**W,'result':ANY},['work_id','result']),
        ('generate_reflection','Generate draft learning candidates.',W,['work_id']),
        ('list_learning_candidates','List drafts and review status.',W,['work_id']),
        ('review_learning_candidate','TRUSTED HUMAN UI ONLY. Separate learning publication approval.',{'candidate_id':STR,'action':{'type':'string','enum':['approve','reject','defer']},'correction':OBJ,**TOKEN},['candidate_id','action','approval_token']),
        ('record_expert_judgment','TRUSTED HUMAN UI ONLY. Apply a judgment to this work, not a general rule.',{**W,'case_id':STR,'conclusion':STR,'reason':STR,**KEY,**TOKEN},['work_id','case_id','conclusion','reason','approval_token'])]
    return { 'le_'+name: {'name':'le_'+name,'description':desc,'inputSchema':schema(props,required)} for name,desc,props,required in entries }

PRIVILEGED={'le_confirm_answer','le_review_learning_candidate','le_record_expert_judgment'}


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def create_approval_token(key,tool,arguments,*,ttl=120):
    """Call ONLY from trusted UI after showing exact arguments to a human."""
    if tool not in PRIVILEGED or not 0<ttl<=300:
        raise ValueError('Invalid approval scope or TTL')
    payload={'tool':tool,'digest':hashlib.sha256(canonical(arguments)).hexdigest(),'expires':time.time()+ttl,'nonce':secrets.token_hex(24)}
    raw=canonical(payload)
    return base64.urlsafe_b64encode(raw).decode()+'.'+hmac.new(key,raw,hashlib.sha256).hexdigest()


class MCPServer:
    def __init__(self,engine,*,human_key=None):
        self.work=WorkSession(engine)
        self.key=human_key
        self.initialized=False
        self.ready=False
        self.tools={k:v for k,v in definitions().items() if (k in PRIVILEGED)==(human_key is not None)}

    def _authorize(self,name,args,token):
        try:
            encoded,signature=token.split('.')
            raw=base64.b64decode(encoded,altchars=b'-_',validate=True)
            if not hmac.compare_digest(signature,hmac.new(self.key,raw,hashlib.sha256).hexdigest()):
                raise ValueError()
            p=json.loads(raw)
            if p['tool']!=name or p['digest']!=hashlib.sha256(canonical(args)).hexdigest() or not time.time()<p['expires']<=time.time()+301:
                raise ValueError()
            self.work.e.store.conn.execute('INSERT INTO mcp_used_approvals VALUES(?,?)',(p['nonce'],p['expires']))
        except Exception:
            raise PermissionError('Invalid, expired, mismatched or replayed human approval') from None

    def call(self,name,args):
        if name not in self.tools:
            raise PermissionError('Tool unavailable on this endpoint')
        validate(args,self.tools[name]['inputSchema'])
        args=dict(args)
        with self.work.e.store.transaction():
            if name in PRIVILEGED:
                token=args.pop('approval_token')
                self._authorize(name,args,token)
                args['expert_confirmed']=True
            if name=='le_record_answer':
                # Source is supplied by an agent, not authenticated human input.
                result=self.work.record_answer(**args, source='agent_reported_human')
            else:
                result=getattr(self.work,name[3:])(**args)
            data=result if isinstance(result,dict) else {'result':result}
            return {'content':[{'type':'text','text':json.dumps(data,ensure_ascii=False,allow_nan=False)}], 'structuredContent':data,'isError':False}

    def dispatch(self,message):
        if not isinstance(message,dict) or message.get('jsonrpc')!='2.0' or not isinstance(message.get('method'),str) or ('id' in message and type(message['id']) not in (str,int)):
            return self.error(None,-32600,'Invalid Request')
        method=message['method'];rid=message.get('id');notification='id' not in message
        if notification:
            if method=='notifications/initialized' and self.initialized:
                self.ready=True
            return None
        params=message.get('params',{})
        if not isinstance(params,dict):return self.error(rid,-32602,'Invalid params')
        try:
            if method=='initialize':
                if self.initialized: return self.error(rid,-32600,'Already initialized')
                if not isinstance(params.get('protocolVersion'),str) or not isinstance(params.get('capabilities'),dict) or not isinstance(params.get('clientInfo'),dict):
                    return self.error(rid,-32602,'Invalid initialization')
                version=params['protocolVersion'] if params['protocolVersion'] in {'2024-11-05','2025-03-26','2025-06-18'} else '2025-06-18'
                self.initialized=True
                result={'protocolVersion':version,'capabilities':{'tools':{'listChanged':False}},'serverInfo':{'name':'learning-engine','version':'0.5.0'},'instructions':'Learning state only. READY never authorizes external tools. Human approvals require a separate trusted UI endpoint.'}
            elif method=='ping':result={}
            elif not self.ready:return self.error(rid,-32002,'Initialize and send notifications/initialized first')
            elif method=='tools/list':result={'tools':list(self.tools.values())}
            elif method=='tools/call':
                if not isinstance(params.get('name'),str) or not isinstance(params.get('arguments',{}),dict):
                    return self.error(rid,-32602,'Invalid tool call')
                try:result=self.call(params['name'],params.get('arguments',{}))
                except (ValueError,KeyError,PermissionError) as exc:
                    result={'content':[{'type':'text','text':str(exc)}],'isError':True}
            else:return self.error(rid,-32601,'Method not found')
            return {'jsonrpc':'2.0','id':rid,'result':result}
        except Exception:
            LOG.exception('MCP operation failed')
            return self.error(rid,-32603,'Internal error; see stderr')

    @staticmethod
    def error(rid,code,message):
        return {'jsonrpc':'2.0','id':rid,'error':{'code':code,'message':message}}


def serve(db,*,human_key_file=None,stdin=None,stdout=None):
    key=Path(human_key_file).read_bytes() if human_key_file else None
    if key is not None and len(key)<32:
        raise ValueError('Human UI key must contain at least 32 random bytes')
    stream=stdin or sys.stdin.buffer;out=stdout or sys.stdout
    logging.basicConfig(stream=sys.stderr,level=logging.WARNING)
    with Engine(str(Path(db).expanduser().resolve())) as engine:
        server=MCPServer(engine,human_key=key)
        while True:
            line=stream.readline(1024*1024+1)
            if not line:break
            if len(line)>1024*1024:
                while line and not line.endswith(b'\n'):line=stream.readline(1024*1024+1)
                response=server.error(None,-32600,'Message exceeds 1 MiB')
            else:
                try:
                    message=json.loads(line.decode('utf-8'),parse_constant=lambda x:(_ for _ in ()).throw(ValueError('Nonfinite JSON')))
                    response=server.dispatch(message)
                except (ValueError,UnicodeError):response=server.error(None,-32700,'Parse error')
            if response is not None:
                out.write(json.dumps(response,ensure_ascii=False,allow_nan=False)+'\n');out.flush()
