"""Synthetic storage-size latency probe; does not measure retrieval accuracy."""
import json
import math
import statistics
import sys
import time
from pathlib import Path
import httpx

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
with httpx.Client(base_url='http://127.0.0.1:18085', timeout=120, trust_env=False) as c:
    health_response = c.get('/health')
    health_response.raise_for_status()
    health = health_response.json()
    user = 'scale_'+str(time.time_ns())
    rows = []
    count = 0
    for size in (100, 1000):
        start = time.perf_counter()
        while count < size:
            content = f'项目第 {count} 号操作案例：Python安装依赖超时可以改用国内镜像，先检查网络连接，再重试下载。'
            r = c.post('/store-entry', json={'entry':{'user_id':user,'content':content,'memory_type':'document_chunk','source':'manual','confidence':.95}})
            r.raise_for_status()
            if not r.json().get('created'):
                raise RuntimeError(r.text)
            count += 1
        insertion_seconds = time.perf_counter()-start
        latencies = []
        for i in range(20):
            start = time.perf_counter()
            r = c.post('/recall',json={'user_id':user,'query':f'第 {i} 号项目Python下载超时怎么办','memory_type':'knowledge','top_k':5})
            r.raise_for_status()
            latencies.append((time.perf_counter()-start)*1000)
        row = {'documents':size,'requests':20,'incremental_insertion_seconds':insertion_seconds,
            'mean_ms':statistics.mean(latencies),'p95_ms':sorted(latencies)[math.ceil(.95*len(latencies))-1],
            'max_ms':max(latencies),'within_500ms':sum(x<=500 for x in latencies),'wall_ms':latencies}
        rows.append(row)
        (out/'scale.json').write_text(json.dumps({'note':'Synthetic near-duplicate corpus for size/latency only; no retrieval accuracy claim.', 'health':health, 'rows':rows},indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in row.items() if k!='wall_ms'}),flush=True)
