"""Evaluator correctness with explicit fakes; no claim of real SDK execution."""
import json
import pytest

from scripts.eval_kylin_joint import evaluate, load_dataset, summarize, validate_vectors


@pytest.mark.parametrize('vectors', [[], [[]], [[0.,0.]], [[float('nan'),1.]], [[float('inf'),1.]], [[1.]]])
def test_invalid_vectors_rejected(vectors):
    with pytest.raises(ValueError):
        validate_vectors(vectors, 1, 2)


def test_invalid_labels_rejected(tmp_path):
    dataset = {'documents':[{'id':'a','text':'中文'}],
               'queries':[{'id':'q','text':'查询','relevant_ids':['missing']}]}
    file = tmp_path/'data.json'
    file.write_text(json.dumps(dataset), encoding='utf-8')
    with pytest.raises(ValueError, match='relevant_ids'):
        load_dataset(file)


class Provider:
    using_fallback = False
    def embed(self, texts):
        return [[1.,0.] for _ in texts]


class Client:
    def __init__(self):
        self.rows = {}
    def insert(self, mid, text, vector, metadata):
        self.rows[mid] = {'memory_id':mid,'content':text,'metadata':metadata}
        return True
    def get_by_id(self, mid):
        return self.rows.get(mid)
    def search(self, vector, filters, top_k):
        assert filters == {'user_id':'isolated'}
        return [dict(next(iter(self.rows.values())), score=.9)]


def test_joint_evaluation_measures_fraction_of_all_relevant_docs():
    dataset = {'documents':[{'id':'a','text':'知识一'},{'id':'b','text':'知识二'}],
               'queries':[{'id':'q','text':'两项知识','relevant_ids':['a','b']}]}
    report = {'embedding':{'dimension':2},'memory_ids':{},'insertions':[],
              'queries':[],'user_id':'isolated'}
    evaluate(Provider(), Client(), dataset, report, k=1, repeats=2, save=lambda:None)
    assert report['metrics']['macro_recall_at_k'] == .5  # hit rate would incorrectly be 1
    assert report['metrics']['hit_at_1'] == 1
    assert report['metrics']['unique_queries'] == 1
    assert report['metrics']['measurements'] == 2
    assert report['passed'] is False
    assert all(r['end_to_end_ms'] >= r['embedding_ms'] for r in report['queries'])


def test_latency_records_outliers_and_nearest_rank_p95():
    rows = [{'query_id':str(i),'end_to_end_ms':t,'recall_at_k':1.,'hit_at_1':True}
            for i,t in enumerate([10]*19+[600])]
    result = summarize(rows, 5)
    assert result['p95_ms'] == 10
    assert result['max_ms'] == 600
    assert result['over_500ms'] == 1


def test_default_dataset_valid():
    from scripts.eval_kylin_joint import ROOT
    dataset, digest = load_dataset(ROOT/'benchmarks/datasets/kylin_joint_retrieval.json')
    assert len(dataset['documents']) == 12
    assert len(dataset['queries']) == 15
    assert len(digest) == 64


def test_scale_recall_dataset_is_sparse_and_labeled():
    from scripts.build_kylin_scale_recall_dataset import BEACON_ANCHORS, BEACONS, OUT, RESERVED_PHRASES, build
    from scripts.eval_kylin_joint import ROOT, load_dataset
    rebuilt = build()
    dataset, digest = load_dataset(ROOT/'benchmarks/datasets/kylin_scale_recall_1000.json')
    assert OUT.name == 'kylin_scale_recall_1000.json'
    assert len(digest) == 64
    assert len(dataset['documents']) == 1000
    assert len(dataset['queries']) == 15 + len(BEACONS)
    assert [row['id'] for row in dataset['documents'][:12]] == [
        'pip', 'ssh', 'venv', 'docker', 'git', 'excel', 'word', 'ppt', 'csv', 'minutes', 'weekly', 'bug']
    texts = [row['text'] for row in dataset['documents']]
    assert len(set(texts)) == 1000
    distractors = [row['text'] for row in dataset['documents'] if row['id'].startswith('dist-')]
    assert len(distractors) == 1000 - 12 - len(BEACONS)
    forbidden = RESERVED_PHRASES + BEACON_ANCHORS
    for text in distractors:
        assert not any(phrase in text for phrase in forbidden)
    for query in dataset['queries']:
        assert 1 <= len(query['relevant_ids']) <= 2
    assert dataset['contest_metrics']['recall_at_k'] == 0.85
    assert rebuilt['documents'] == dataset['documents']
    assert rebuilt['queries'] == dataset['queries']
