"""Two-process SDK persistence smoke; synthetic vectors test storage only.

python scripts/smoke_kylin_vector.py write --manifest data/vector-smoke.json
python scripts/smoke_kylin_vector.py verify --manifest data/vector-smoke.json
Only the synthetic IDs in this manifest are removed; no collection drop.
This does not measure semantic recall or certify embedding SDK compliance.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.knowledge.memory_service.kylin_vector_client import KylinVectorClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['write','verify'])
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    if args.phase == 'write':
        if args.manifest.exists():
            raise FileExistsError('Use a new manifest; refusing to overwrite cleanup information')
        run = uuid4().hex
        state = {'collection':'mokoa_sdk_smoke_'+run, 'user':'smoke_'+run,
                 'memory_id':str(uuid4()),'vector_dim':int(os.getenv('MEMORY_VECTOR_DIM','768'))}
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(state), encoding='utf-8')
    else:
        state = json.loads(args.manifest.read_text(encoding='utf-8'))
    vector = [1.0] + [0.0] * (state['vector_dim']-1)
    client = KylinVectorClient(collection=state['collection'], vector_dim=state['vector_dim'],
        endpoint=os.getenv('OS_AGENT_KYLIN_VECTOR_ENDPOINT',''))
    try:
        if args.phase == 'write':
            assert client.insert(state['memory_id'], '系统向量SDK持久化测试', vector,
                                 {'user_id':state['user'],'memory_type':'document_chunk'})
            assert client.update(state['memory_id'], {'content':'更新后持久化测试'})
        else:
            row = client.get_by_id(state['memory_id'], include_embedding=True)
            assert row and row['content']=='更新后持久化测试'
            assert len(row['embedding'])==state['vector_dim']
            hits = client.search(vector, {'user_id':state['user']}, top_k=1)
            assert hits and hits[0]['memory_id']==state['memory_id']
            assert not client.search(vector, {'user_id':state['user']+'_other'}, top_k=1)
            assert client.delete(state['memory_id'], soft=False)
            assert client.get_by_id(state['memory_id']) is None
        print(json.dumps({'passed':True,'phase':args.phase,'backend':client.bridge.info,
                          'note':'synthetic vectors; storage smoke only'},ensure_ascii=False))
    finally:
        client.close()


if __name__ == '__main__':
    main()
