"""Minimal stdlib-only MCP client; Learning Engine need not be installed here."""
import json
import subprocess
import sys


class Client:
    def __init__(self, command):
        self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        self.sequence=0
        self.request('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'learning-engine-example','version':'1'}})
        self.notify('notifications/initialized')

    def send(self,value):
        self.process.stdin.write(json.dumps(value,ensure_ascii=False)+'\n');self.process.stdin.flush()

    def notify(self,method):
        self.send({'jsonrpc':'2.0','method':method})

    def request(self,method,params=None):
        self.sequence+=1
        self.send({'jsonrpc':'2.0','id':self.sequence,'method':method,'params':params or {}})
        line=self.process.stdout.readline()
        if not line:raise RuntimeError('MCP server closed connection')
        value=json.loads(line)
        if 'error' in value:raise RuntimeError(value['error'])
        return value['result']

    def call(self,name,**arguments):
        result=self.request('tools/call',{'name':name,'arguments':arguments})
        if result.get('isError'):raise RuntimeError(result['content'][0]['text'])
        return result['structuredContent']

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=10)
        self.process.stdout.close();self.process.stderr.close()


if __name__=='__main__':
    if len(sys.argv)!=3:
        raise SystemExit('Usage: python mcp_client.py /absolute/server/python /absolute/learning.sqlite3')
    client=Client([sys.argv[1],'-m','learning_engine','--db',sys.argv[2],'serve-mcp'])
    try:
        print(client.request('tools/list'))
        work=client.call('le_start_work',goal='架空Via抵抗計算',context={'fictional':True})
        print(client.call('le_request_decision',work_id=work['work_id'],target='既存モデルを使えるか',known_facts={}))
    finally:client.close()
