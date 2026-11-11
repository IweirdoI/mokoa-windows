"""Strict Kylin embedding + vector SDK retrieval evaluation (not HTTP latency)."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_dataset(path):
    raw = path.read_bytes()
    data = json.loads(raw)
    docs, queries = data['documents'], data['queries']
    ids = [d['id'] for d in docs]
    qids = [q['id'] for q in queries]
    if not docs or not queries or len(set(ids)) != len(ids) or len(set(qids)) != len(qids):
        raise ValueError('Dataset must be nonempty with unique document/query IDs')
    for row in docs + queries:
        if not isinstance(row['id'], str) or not isinstance(row['text'], str) or not row['text'].strip():
            raise ValueError('Each row requires a string ID and nonempty text')
    for q in queries:
        relevant = q['relevant_ids']
        if not relevant or len(set(relevant)) != len(relevant) or not set(relevant) <= set(ids):
            raise ValueError('Each query requires unique, valid relevant_ids')
    return data, hashlib.sha256(raw).hexdigest()


def validate_vectors(vectors, count, dimension=None):
    if len(vectors) != count:
        raise ValueError('Embedding batch size mismatch')
    for vector in vectors:
        if not vector or (dimension is not None and len(vector) != dimension):
            raise ValueError('Embedding dimension mismatch')
        if not all(math.isfinite(x) for x in vector) or not any(x != 0 for x in vector):
            raise ValueError('Embedding contains nonfinite values or is zero')
    return len(vectors[0])


def summarize(rows, k):
    if not rows:
        raise ValueError('No query measurements')
    times = sorted(r['end_to_end_ms'] for r in rows)
    return {
        'measurements':len(rows), 'unique_queries':len({r['query_id'] for r in rows}),
        'k':k, 'macro_recall_at_k':statistics.mean(r['recall_at_k'] for r in rows),
        'hit_at_1':statistics.mean(r['hit_at_1'] for r in rows),
        'mean_ms':statistics.mean(times), 'p50_ms':statistics.median(times),
        'p95_ms':times[math.ceil(.95*len(times))-1], 'max_ms':max(times),
        'over_500ms':sum(t > 500 for t in times),
    }


def evaluate(provider, client, dataset, report, *, k, repeats, save):
    """Dependencies injectable for tests; CLI always constructs both real SDKs."""
    dimension = report['embedding']['dimension']
    mapping = report['memory_ids']
    for doc in dataset['documents']:
        memory_id = str(uuid4())
        mapping[doc['id']] = memory_id
        save()  # retain cleanup IDs even if insertion outcome becomes uncertain
        start = time.perf_counter()
        vectors = provider.embed([doc['text']])
        validate_vectors(vectors, 1, dimension)
        if provider.using_fallback:
            raise RuntimeError('Embedding fallback detected')
        if not client.insert(memory_id, doc['text'], vectors[0], {
            'user_id':report['user_id'], 'memory_type':'document_chunk',
            'source':'manual', 'confidence':1., 'dataset_id':doc['id'],
        }):
            raise RuntimeError('Insert rejected: '+doc['id'])
        report['insertions'].append({'document_id':doc['id'],'ms':(time.perf_counter()-start)*1000})
    # Prove the labeled corpus was actually stored before calculating recall.
    for doc in dataset['documents']:
        stored = client.get_by_id(mapping[doc['id']])
        if not stored or stored['content'] != doc['text']:
            raise RuntimeError('Stored corpus verification failed: '+doc['id'])
    reverse = {v:key for key,v in mapping.items()}
    for repeat in range(repeats):
        for q in dataset['queries']:
            start = time.perf_counter()
            vectors = provider.embed([q['text']])
            validate_vectors(vectors, 1, dimension)
            if provider.using_fallback:
                raise RuntimeError('Embedding fallback detected')
            encoded = time.perf_counter()
            hits = client.search(vectors[0], {'user_id':report['user_id']}, top_k=k)
            ended = time.perf_counter()
            predicted = [reverse.get(h['memory_id'], 'UNKNOWN:'+h['memory_id']) for h in hits]
            if len(predicted) > k or len(set(predicted)) != len(predicted):
                raise RuntimeError('SDK returned too many or duplicate results')
            if any(h['memory_id'] not in reverse for h in hits):
                raise RuntimeError('Unexpected memory outside the isolated corpus')
            relevant = set(q['relevant_ids'])
            report['queries'].append({
                'query_id':q['id'], 'repeat':repeat, 'text':q['text'],
                'relevant_ids':q['relevant_ids'], 'predicted_ids':predicted,
                'scores':[h.get('score') for h in hits],
                'recall_at_k':len(relevant.intersection(predicted))/len(relevant),
                'hit_at_1':bool(predicted and predicted[0] in relevant),
                'embedding_ms':(encoded-start)*1000, 'search_ms':(ended-encoded)*1000,
                'end_to_end_ms':(ended-start)*1000,
            })
            save()
    report['metrics'] = summarize(report['queries'], k)
    report['metrics']['first_search_end_to_end_ms'] = report['queries'][0]['end_to_end_ms']
    report['metrics']['recall_passed'] = report['metrics']['macro_recall_at_k'] >= .85
    report['metrics']['latency_passed'] = report['metrics']['max_ms'] <= 500
    report['passed'] = report['metrics']['recall_passed'] and report['metrics']['latency_passed']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=ROOT/'benchmarks/datasets/kylin_joint_retrieval.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--top-k', type=int, default=5)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--keep-data', action='store_true', help='Keep only this run\'s records for inspection')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output already exists; use a new path to preserve evidence')
    if not 1 <= args.top_k <= 100 or args.repeats < 1:
        parser.error('Require 1 <= top-k <= 100 and repeats >= 1')
    dataset, digest = load_dataset(args.dataset)
    if args.top_k >= len(dataset['documents']):
        parser.error('top-k must be smaller than corpus size to avoid trivial recall')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    run = uuid4().hex
    report = {'passed':False, 'platform':platform.platform(), 'python':sys.version,
              'dataset':str(args.dataset.resolve()), 'dataset_sha256':digest,
              'dataset_description':dataset.get('description',''),
              'documents':len(dataset['documents']), 'unique_queries':len(dataset['queries']),
              'collection':'mokoa_joint_'+run, 'user_id':'joint_'+run,
              'latency_scope':'query embedding + ctypes/SDK search; not HTTP; model initialized during setup',
              'memory_ids':{}, 'insertions':[], 'queries':[], 'cleanup':{}}
    revision = subprocess.run(['git','rev-parse','HEAD'], cwd=ROOT, capture_output=True, text=True)
    report['commit'] = revision.stdout.strip() if revision.returncode == 0 else None
    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    provider = client = None
    save()
    try:
        from src.embedding.kylin_adapter import KylinEmbeddingAdapter
        from src.knowledge.memory_service.kylin_vector_client import KylinVectorClient
        import os
        provider = KylinEmbeddingAdapter(allow_fallback=False)
        start = time.perf_counter()
        probe = provider.embed(['系统向量联合验收初始化'])
        dimension = validate_vectors(probe, 1)
        report['embedding'] = {'backend':provider.backend_name, 'using_fallback':provider.using_fallback,
                               'dimension':dimension, 'initialization_and_probe_ms':(time.perf_counter()-start)*1000}
        if provider.using_fallback or provider.backend_name != 'kylin-coreai':
            raise RuntimeError('Real Kylin embedding required')
        client = KylinVectorClient(collection=report['collection'], vector_dim=dimension,
                                  endpoint=os.getenv('OS_AGENT_KYLIN_VECTOR_ENDPOINT',''),
                                  timeout_ms=int(os.getenv('OS_AGENT_KYLIN_VECTOR_TIMEOUT_MS','5000')))
        report['vector_sdk'] = client.bridge.info
        if report['vector_sdk'].get('backend') != 'kylin-vector-sdk':
            raise RuntimeError('Real Kylin vector SDK required')
        evaluate(provider, client, dataset, report, k=args.top_k, repeats=args.repeats, save=save)
    except Exception as exc:
        report['passed'] = False
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        if client:
            for memory_id in report['memory_ids'].values():
                if args.keep_data:
                    report['cleanup'][memory_id] = 'retained'
                    continue
                try:
                    client.delete(memory_id, soft=False)
                    if client.get_by_id(memory_id) is not None:
                        raise RuntimeError('Record still present after delete')
                    report['cleanup'][memory_id] = 'absent'
                except Exception as exc:
                    report['cleanup'][memory_id] = str(exc)
                    report['passed'] = False
            try:
                client.close()
            except Exception as exc:
                report['close_error'] = str(exc)
                report['passed'] = False
        if provider:
            try:
                provider.close()
            except Exception as exc:
                report['embedding_close_error'] = str(exc)
                report['passed'] = False
        save()
    print(json.dumps({key:report[key] for key in ('passed','metrics','error') if key in report}, ensure_ascii=False, indent=2))
    print('Report: '+str(args.output.resolve()))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
