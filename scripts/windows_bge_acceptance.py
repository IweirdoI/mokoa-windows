"""Windows BGE-M3 acceptance: real HTTP, isolated data, no hash fallback.

Run: python scripts/windows_bge_acceptance.py --out data/evidence/windows_bge_run
The memory backend is deliberately reported as non-persistent. No existing
user data or default configuration files are changed. --serve keeps the same
isolated API available for browser verification after the automated checks.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sqlite3
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18085)
    parser.add_argument('--serve', action='store_true')
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ['OS_AGENT_EMBEDDING_BACKEND'] = 'local'
    os.environ['OS_AGENT_MEMORY_BACKEND'] = 'memory'
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'

    from src.common.config import load_default_config
    from src.embedding.embedding_interface import load_embedding_config, create_embedding_backend
    cfg = load_default_config()
    cfg['storage']['staging_sqlite'] = str(out / 'staging.db')
    cfg['preference']['storage_dir'] = str(out / 'preferences')
    cfg['index_path'] = str(out / 'security_index.json')
    load_embedding_config()['allow_stub_fallback'] = False

    import torch
    import httpx
    import uvicorn
    from src.preference.llm_client import SiliconFlowClient
    report = {'platform': platform.platform(), 'python': sys.version,
              'backend': 'InMemoryMilvusClient (non-persistent)',
              'preference_llm_available': SiliconFlowClient().available,
              'checks': [], 'http': []}

    def save():
        (out / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def check(name, passed, detail=None):
        report['checks'].append({'name': name, 'passed': bool(passed), 'detail': detail})
        print(f"{'PASS' if passed else 'FAIL'} {name}: {detail}", flush=True)
        save()

    t = time.perf_counter()
    provider = create_embedding_backend('local')
    vectors = provider.embed(['安装Python包超时换国内镜像', 'pip下载慢可切换清华源', '今天阳光明媚'])
    report['embedding'] = {'model': 'BAAI/bge-m3', 'device': str(provider._model.device),
        'gpu': torch.cuda.get_device_name() if torch.cuda.is_available() else None,
        'dimension': len(vectors[0]), 'fallback': provider._fallback_mode,
        'cold_seconds': time.perf_counter() - t, 'torch_threads': torch.get_num_threads()}
    check('real_bge_m3', not provider._fallback_mode and len(vectors[0]) == 1024, report['embedding'])
    check('semantic_similarity', sum(a*b for a,b in zip(vectors[0],vectors[1])) > sum(a*b for a,b in zip(vectors[0],vectors[2])))

    from api.server import app, orchestrator
    # Port check prevents accidentally testing another server.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', args.port))
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.1)
    if not server.started:
        raise RuntimeError('Acceptance HTTP server did not start')
    client = httpx.Client(base_url=f'http://127.0.0.1:{args.port}', timeout=120, trust_env=False)

    def call(method, path, data=None):
        t = time.perf_counter()
        r = client.request(method, path, json=data) if data is not None else client.request(method, path)
        elapsed = (time.perf_counter()-t)*1000
        body = r.json() if 'application/json' in r.headers.get('content-type','') else {'text': r.text}
        report['http'].append({'method':method, 'path':path, 'request':data, 'status':r.status_code, 'wall_ms':elapsed, 'response':body})
        save()
        return body, elapsed

    def post(path, data=None):
        return call('POST',path,data)[0]

    def get(path):
        return call('GET',path)[0]

    uid = 'win_acceptance'
    def ingest(source, payload, user=uid, scene=None):
        return post('/ingest', {'user_id':user, 'source':source, 'payload':payload, 'scene_id':scene})

    try:
        check('health', get('/health').get('status') == 'ok')
        payloads = [
            ('tool_result', {'tool':'pip','content':'pip 安装超时后改用清华镜像成功','exit_code':0}),
            ('user_behavior', {'content':'用户经常打开 VSCode','action':'经常打开','target':'VSCode','occurrence_count':5,'confidence':.85}),
            ('manual_config', {'key':'reply_language','value':'中文','confidence':.95}),
            ('conversation', {'content':'我的手机号是 13800138000，请记住'}),
        ]
        ingested = [ingest(s,p) for s,p in payloads]
        check('four_source_ingestion', all(r.get('accepted') for r in ingested))
        pii = ingested[-1]
        check('pii_redaction', '13800138000' not in json.dumps(pii.get('record')) and '<HASH_' in json.dumps(pii.get('record')))
        duplicate = ingest(*payloads[0])
        check('duplicate_rejected', not duplicate.get('accepted'), duplicate.get('reject_code'))
        low = ingest('user_behavior', {'action':'打开','target':'记事本','occurrence_count':1})
        check('low_quality_rejected', not low.get('accepted'), low.get('reject_code'))
        cleaned = ingest('conversation', {'content':'  操作说明：\x00打开设置查看显示分辨率。  '}, user='clean_user')
        cleaned_content = (cleaned.get('record') or {}).get('content', '')
        check('control_character_cleaning', bool(cleaned_content) and '\x00' not in cleaned_content, {'content':cleaned_content,'accepted':cleaned.get('accepted'),'reject_code':cleaned.get('reject_code')})

        for text, scene in [('下载默认放 D 盘','general'),('以后下载改到 E 盘','general'),('办公时默认用 Excel 处理表格','office'),('数据分析时默认用 Python 处理表格','data_analysis')]:
            result = post('/preferences/extract', {'user_id':uid,'text':text,'scene_id':scene})
            check('extract_'+scene+'_'+text, result.get('extracted_count',0)>0, {'mode':result.get('mode'), 'degraded':result.get('degraded_to_rules')})
        history = get(f'/preferences/{uid}/history?preference_key=habit.download_path')
        check('preference_version_chain', history.get('count') == 2, history)
        profile = get(f'/preferences/{uid}/profile?scene_id=general')
        check('preference_latest_value', 'E:/Downloads' in json.dumps(profile) and 'D:/Downloads' not in json.dumps(profile), profile)
        office = get(f'/preferences/{uid}/profile?scene_id=office')
        analysis = get(f'/preferences/{uid}/profile?scene_id=data_analysis')
        check('cross_scene_preferences', 'excel' in json.dumps(office).lower() and 'python' in json.dumps(analysis).lower(), {'office':office,'analysis':analysis})
        recalled = post('/recall', {'user_id':uid,'query':'下载路径','top_k':20})
        check('superseded_excluded', 'D:/Downloads' not in json.dumps(recalled.get('items',[])))
        check('user_isolation', post('/recall',{'user_id':'other_user','query':'下载路径'}).get('count') == 0)

        for item in ingested:
            if item.get('memory_id'):
                post(f"/short-term/{uid}/{item['memory_id']}/touch")
        mid = post(f'/consolidate/{uid}/mid')
        check('short_to_mid', mid.get('promoted_to_mid',0)>0, mid)
        long = post(f'/consolidate/{uid}')
        check('mid_to_long', long.get('promoted_to_long',0)>0, long)
        forget = post('/forget', {'user_id':uid,'instruction':'忘掉下载路径偏好'})
        check('forget_preference_versions', forget.get('count') == 2, forget)
        check('forgotten_history_empty', get(f'/preferences/{uid}/history?preference_key=habit.download_path').get('count') == 0)
        phone = post('/forget', {'user_id':uid,'instruction':'忘记我的手机号'})
        check('forget_phone', phone.get('count',0)>0, phone)
        check('forget_preserves_other_scene_preferences', 'excel' in json.dumps(get(f'/preferences/{uid}/profile?scene_id=office')).lower())
        post('/staging/purge', {})
        with sqlite3.connect(out / 'staging.db') as db:
            serialized = '\n'.join(db.iterdump())
        check('staging_no_plaintext_phone', '13800138000' not in serialized)

        # Frozen small diagnostic corpus: 20 documents, 2 independently worded
        # queries per document. Not an official or held-out benchmark.
        corpus = [
            ('pip 安装超时后改用清华镜像成功', ['Python包下载一直超时怎么办','pip下载慢换什么源']),
            ('SSH 免密登录需要将公钥追加到 authorized_keys 文件', ['远程登录服务器不想每次输入密码','SSH公钥应该放在哪里']),
            ('Python 虚拟环境使用 conda create 创建，conda activate 激活', ['如何新建独立的Python运行环境','conda环境创建后如何启用']),
            ('Docker 容器通过 -p 8080:80 将容器服务映射到宿主机端口', ['如何从电脑访问容器内的网页服务','Docker怎样映射端口']),
            ('Git 工作区未提交改动可用 git stash 暂存，稍后 git stash pop 恢复', ['切换分支前如何暂时收起未提交代码','Git暂存改动之后怎么拿回来']),
            ('Windows 查看磁盘空间可打开资源管理器的此电脑页面', ['电脑硬盘还剩多少容量在哪里看','Windows如何查看磁盘剩余空间']),
            ('Excel 汇总不同部门的销售数据可使用数据透视表', ['如何按部门自动合计销售额','Excel销售报表分组汇总怎么做']),
            ('Word 长文档使用标题样式后可自动生成目录', ['论文目录如何自动更新页码','Word如何生成章节目录']),
            ('PowerPoint 可以使用演讲者视图查看备注而不向观众展示', ['放幻灯片时只让自己看见讲稿','PPT如何使用演讲备注']),
            ('浏览器误关闭标签页后使用 Ctrl Shift T 恢复', ['刚才关掉的网页如何重新打开','恢复浏览器标签页的快捷键是什么']),
            ('Windows 截图可使用 Win Shift S 选择屏幕区域', ['电脑如何截取指定区域图片','Windows局部截屏快捷键']),
            ('打印任务卡住时可先清空打印队列，再重启打印后台服务', ['打印机一直等待文档出不来怎么办','如何处理堵塞的打印队列']),
            ('Windows 网络故障排查先检查网线与路由器，再尝试刷新 DNS 缓存', ['电脑连不上网首先检查哪些设备','网络解析异常如何处理DNS缓存']),
            ('会议纪要模板包含会议主题、参会人、决策事项、责任人和截止日期', ['开会之后的记录应包含哪些栏目','会议记录如何明确待办责任']),
            ('项目周报模板包括本周进展、问题风险、下周计划和所需支持', ['每周工作总结应该写哪些部分','项目进度报告如何组织内容']),
            ('CSV 文件中文乱码时应核对文件编码并尝试使用 UTF-8 导入', ['表格导入后汉字变成乱码怎么办','读取CSV时编码应该怎样检查']),
            ('大文件邮件发送失败时可上传共享盘，并发送带访问权限的链接', ['附件超过邮箱限制怎么交付文件','邮件不能附加大文件有什么替代办法']),
            ('恢复误删文件时先检查回收站，再查看文件历史记录备份', ['不小心删掉文档后怎么找回','已删除文件可以在哪里恢复']),
            ('代码评审流程是创建分支、提交修改、发起合并请求、评审通过后合入主线', ['团队代码修改怎样经过审核再合并','提交开发成果的评审步骤有哪些']),
            ('软件故障工单应记录复现步骤、预期结果、实际结果和环境版本', ['向开发人员报告bug需要提供什么','问题反馈模板包含哪些字段']),
        ]
        retrieval_user = 'win_retrieval'
        for i,(document, _) in enumerate(corpus):
            created = post('/store-entry', {'entry':{'content':document,'memory_type':'document_chunk','source':'manual','user_id':retrieval_user,'confidence':.95,'metadata':{'case_id':i}}})
            check(f'corpus_insert_{i}', created.get('created'))
        rows = []
        for i, (document, queries) in enumerate(corpus):
            for query in queries:
                response, wall = call('POST','/recall',{'user_id':retrieval_user,'query':query,'memory_type':'knowledge','top_k':5})
                contents = [x['content'] for x in response.get('items',[])]
                rows.append({'query':query,'expected':document,'top1':bool(contents and contents[0]==document),'top5':document in contents,'wall_ms':wall,'api_ms':response.get('latency_ms'),'hits':contents})
        latencies = sorted(x['wall_ms'] for x in rows)
        metrics = {'documents':len(corpus),'queries':len(rows),'recall_at_1':sum(x['top1'] for x in rows)/len(rows),'recall_at_5':sum(x['top5'] for x in rows)/len(rows),'wall_mean_ms':statistics.mean(latencies),'wall_p95_ms':latencies[int(.95*len(latencies))-1],'wall_max_ms':max(latencies),'within_500ms':sum(x<=500 for x in latencies),'rows':rows}
        report['retrieval'] = metrics
        check('knowledge_recall_at_5', metrics['recall_at_5']>=.85, {k:v for k,v in metrics.items() if k!='rows'})
        check('warm_http_latency_all_under_500ms', max(latencies)<=500, metrics['wall_max_ms'])
        from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient
        reopened = InMemoryMilvusClient()
        check('long_term_survives_backend_recreation', len(reopened.search(None, {'user_id':retrieval_user}, top_k=100)) == len(corpus), 'Default backend has no durable storage')
        import psutil
        report['resources'] = {'process_rss_mib':psutil.Process().memory_info().rss/1024**2,
            'gpu_allocated_mib':torch.cuda.memory_allocated()/1024**2 if torch.cuda.is_available() else None,
            'gpu_peak_allocated_mib':torch.cuda.max_memory_allocated()/1024**2 if torch.cuda.is_available() else None,
            'staging_db_bytes':(out/'staging.db').stat().st_size}
        report['passed'] = all(x['passed'] for x in report['checks'])
        save()
        print('REPORT '+str(out / 'acceptance.json'),flush=True)
        if args.serve:
            print(f'API remains available at http://127.0.0.1:{args.port}',flush=True)
            while True:
                time.sleep(1)
    finally:
        client.close()
        server.should_exit = True
        thread.join(timeout=10)
    return 0 if report.get('passed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
