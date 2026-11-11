"""Additional acceptance probes, only against the isolated Windows test API."""
import json
import sys
import time
from pathlib import Path
import httpx

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
client = httpx.Client(base_url='http://127.0.0.1:18085', timeout=120, trust_env=False)
events = []
def call(path, payload):
    r = client.post(path, json=payload)
    body = r.json() if 'application/json' in r.headers.get('content-type','') else r.text
    events.append({'path':path,'request':payload,'status':r.status_code,'response':body})
    return body

foreign = call('/recall', {'user_id':'boundary_other','query':'SSH免密','filters':{'user_id':'win_retrieval'}})
raw = call('/store-entry', {'entry':{'user_id':'boundary_sensitive','content':'测试联系方式手机号 13800138000，供联调验收使用。','memory_type':'document_chunk','source':'manual','confidence':.95}})
raw_recall = call('/recall', {'user_id':'boundary_sensitive','query':'手机号'})
forgot = call('/forget', {'user_id':'boundary_sensitive','instruction':'忘记我的手机号'})
after_forget = call('/recall', {'user_id':'boundary_sensitive','query':'手机号'})
for content in ['项目帮助服务监听端口为 8080。','项目帮助服务监听端口已改为 9090，旧端口不再使用。']:
    call('/store-entry', {'entry':{'user_id':'boundary_conflict','content':content,'memory_type':'project_context','source':'manual','confidence':.95,'metadata':{'record_key':'help_service_port'}}})
conflict = call('/recall', {'user_id':'boundary_conflict','query':'帮助服务端口','memory_type':'knowledge'})
invalid = call('/recall', {'user_id':'boundary_other','query':'a','memory_type':'invalid'})
report = {'checks':{
    'filter_cannot_override_user':foreign.get('count')==0,
    'store_entry_redacts_pii':'13800138000' not in json.dumps(raw_recall),
    'store_entry_phone_forget_clears_recall':after_forget.get('count')==0,
    'knowledge_update_excludes_obsolete_port':not any('8080' in x['content'] for x in conflict.get('items',[])),
    'invalid_memory_type_is_4xx':400<=events[-1]['status']<500,
}, 'events':events}
(out/'boundary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report['checks'],ensure_ascii=False,indent=2))
client.close()
