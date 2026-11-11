"""Deterministic query, loader and metric checks; no model or dataset download."""
import json
import random
from types import SimpleNamespace

import pytest

from src.core.dialogue_query import task_query


@pytest.mark.parametrize("ending", ["Thank you.", "No, that's all I need.", "Thanks, goodbye!", "谢谢", "好的"])
def test_closing_does_not_replace_task(ending):
    result = task_query(["I need a cheap hotel in Cambridge.", "In the north, for two people.", ending])
    assert "hotel" in result and "two people" in result
    assert ending not in result


def test_no_task_stays_empty_and_constraints_are_preserved():
    assert task_query(["Thank you", "Bye"]) == ""
    assert "Actually Friday" in task_query(["I need a hotel " * 100, "Actually Friday, not Monday."], max_chars=100)
    assert task_query(["Thanks, but I need a taxi too."]) == "Thanks, but I need a taxi too."


@pytest.mark.parametrize("legacy", [False, True])
def test_multiwoz_loader_preserves_only_user_query_evidence(tmp_path, monkeypatch, legacy):
    from benchmarks import run_dataset_eval as module
    path = tmp_path / "MultiWOZ/data/MultiWOZ_2.2/train"
    path.mkdir(parents=True)
    row = {"dialogue_id": "example", "turns": [
        {"speaker": "USER", "utterance": "I need a hotel in the north."},
        {"speaker": "SYSTEM", "utterance": "Secret answer Hotel Alpha."},
        {"speaker": "USER", "utterance": "Thank you."},
    ]}
    if legacy:
        row = {"dialogue": [{"transcript": "I need a hotel in the north.",
                              "system_transcript": "Secret answer Hotel Alpha."},
                             {"transcript": "Thank you."}]}
    (path / "dialogues_001.json").write_text(json.dumps([row]), encoding="utf-8")
    monkeypatch.setattr(module, "GIT", tmp_path)
    items, indices = module.load_multiwoz(1, random.Random(42))
    assert indices == [0]
    content, meta = items[0]
    assert "Hotel Alpha" in content
    assert "hotel in the north" in task_query(meta["user_turns"])
    assert "Alpha" not in task_query(meta["user_turns"])
    assert meta["last_user"] == "Thank you."


def test_retrieval_uses_ids_and_excludes_rejected_and_empty_queries(monkeypatch):
    from scripts import eval_dialog_tool_datasets as module
    monkeypatch.setattr(module, "EmbeddingClient", lambda _: object())
    responses = iter([SimpleNamespace(created=True, memory_id="target"),
                      SimpleNamespace(created=False, memory_id=None),
                      SimpleNamespace(created=True, memory_id="empty-query")])
    monkeypatch.setattr(module, "create_memory", lambda *a, **kw: next(responses))
    calls = []
    def query(request, **kw):
        calls.append(request.query)
        return SimpleNamespace(items=[{"memory_id": "wrong-id", "content": "same prefix"}])
    monkeypatch.setattr(module, "query_memory", query)
    result = module.retrieve("MultiWOZ", [("same prefix", {})] * 3,
                             ["hotel", "taxi", ""], "u", "local")
    assert result["hit_at_5"] == 0
    assert result["n_indexed"] == 2 and result["n_queries"] == 1
    assert result["n_query_skipped"] == 2 and calls == ["hotel"]


def test_empty_corpus_is_not_a_pass(monkeypatch):
    from scripts import eval_dialog_tool_datasets as module
    monkeypatch.setattr(module, "EmbeddingClient", lambda _: object())
    result = module.retrieve("MultiWOZ", [], [], "u", "local")
    assert result["n_queries"] == 0 and not result["latency_passed"]


@pytest.mark.parametrize("turn", [{"from": "human", "value": "Find weather in Cambridge"},
                                 {"role": "user", "content": "Find weather in Cambridge"}])
@pytest.mark.parametrize("encoded", [False, True])
def test_toolbench_schemas_have_real_task_and_tool_names(tmp_path, monkeypatch, turn, encoded):
    from benchmarks import run_dataset_eval as module
    path = tmp_path / "ToolBench/data"
    path.mkdir(parents=True)
    tools = [{"name": "weather_lookup"}]
    row = {"id": "tool-1", "conversations": [turn], "tools": json.dumps(tools) if encoded else tools}
    (path / "toolbench.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    monkeypatch.setattr(module, "GIT", tmp_path)
    items, indices = module.load_toolbench(1, random.Random(42))
    assert indices == [1]
    assert "Find weather in Cambridge" in items[0][0]
    assert "weather_lookup" in items[0][0]


def test_dailydialog_loader_keeps_turns(tmp_path, monkeypatch):
    import zipfile
    from benchmarks import run_dataset_eval as module

    dd = tmp_path / "DailyDialog"
    dd.mkdir()
    inner = "train/dialogues_train.txt"
    with zipfile.ZipFile(dd / "train.zip", "w") as zf:
        zf.writestr(inner, "I need a hotel __eou__ Sure. __eou__ In the north. __eou__ Thank you. __eou__\n")
    monkeypatch.setattr(module, "GIT", tmp_path)
    items, indices = module.load_dailydialog(1, random.Random(42))
    assert indices == [0]
    content, meta = items[0]
    assert "hotel" in content
    assert meta["user_turns"][0] == "I need a hotel"
    assert "hotel" in task_query(meta["user_turns"])
    assert "Thank you" not in task_query(meta["user_turns"])


def test_dialogbench_jsonl_skips_bad_lines_and_gold(tmp_path, monkeypatch):
    from benchmarks import run_dataset_eval as module

    path = tmp_path / "DIALOGBENCH/data/data_en"
    path.mkdir(parents=True)
    good = {
        "id": "Conversational_QA0",
        "task": "Conversational QA",
        "label": "GOLD-LETTER-A",
        "analysis": "secret analysis should not be indexed",
        "prompt_SFT": "SYSTEM PROMPT leak",
        "dialogue": [
            {"person1": "What is the smallest country?", "person2": "Vatican City."},
            {"person1": "Thanks, goodbye!", "person2": "Bye."},
        ],
    }
    (path / "Conversational_QA.json").write_text(
        json.dumps(good) + "\n{not-json\n" + json.dumps({"id": "empty"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "GIT", tmp_path)
    items, indices = module.load_dialogbench(5, random.Random(42))
    assert indices == [0]
    content, meta = items[0]
    assert "smallest country" in content
    assert "GOLD-LETTER-A" not in content
    assert "secret analysis" not in content
    assert "SYSTEM PROMPT" not in content
    assert "GOLD-LETTER-A" not in json.dumps(meta)
    assert "leaky_concat" not in meta
    assert "smallest country" in task_query(meta["user_turns"])
    assert "Thanks, goodbye" not in task_query(meta["user_turns"])


def test_dialogbench_window_and_parse_audit(tmp_path, monkeypatch):
    from benchmarks import run_dataset_eval as module
    path = tmp_path / "DIALOGBENCH/data/data_en"
    path.mkdir(parents=True)
    row = {"query": "Output option A only", "label": "GOLDSECRET", "prompt": "PROMPTSECRET",
           "dialogue": [{"person1": "Find a hotel", "person2": "x" * 1480},
                        {"person1": "OUTSIDE_WINDOW_REQUEST", "person2": "answer"}]}
    (path / "task.json").write_text(json.dumps(row) + '\n{bad\n[]\n' + json.dumps({"query": "A"}) + '\n', encoding="utf-8")
    monkeypatch.setattr(module, "GIT", tmp_path)
    audit = {}
    items, _ = module.load_dialogbench(50, random.Random(42), diagnostics=audit)
    assert len(items) == 1
    content, meta = items[0]
    assert len(content) == 1500
    assert meta["user_turns"] == ["Find a hotel"]
    assert meta["last_user"] == "Find a hotel"
    assert meta["n_user_turns_outside_window"] == 1
    assert "GOLDSECRET" not in json.dumps(items) and "Output option" not in json.dumps(items)
    assert audit["task.json"]["bad_json_lines"] == 1
    assert audit["task.json"]["non_object_lines"] == 1
    assert audit["task.json"]["empty_dialogue"] == 1
    assert meta["source_line"] == 1


def test_daily_window_excludes_partial_user_turn(tmp_path, monkeypatch):
    import zipfile
    from benchmarks import run_dataset_eval as module
    path = tmp_path / "DailyDialog"
    path.mkdir()
    with zipfile.ZipFile(path / "train.zip", "w") as zf:
        zf.writestr("train/dialogues_train.txt", "Hotel please __eou__ " + "x" * 1450 + " __eou__ " + "tail" * 40 + " __eou__\n")
    monkeypatch.setattr(module, "GIT", tmp_path)
    items, _ = module.load_dailydialog(1, random.Random(42))
    assert items[0][1]["user_turns"] == ["Hotel please"]
    assert items[0][1]["content_truncated"]


def test_retrieve_metadata_drops_gold_and_prompt_fields(monkeypatch):
    from scripts import eval_dialog_tool_datasets as module
    monkeypatch.setattr(module, "EmbeddingClient", lambda _: object())
    captured = []
    def create(request, **kw):
        captured.append(request.entry)
        return SimpleNamespace(created=True, memory_id="m1")
    monkeypatch.setattr(module, "create_memory", create)
    monkeypatch.setattr(module, "query_memory", lambda *a, **kw: SimpleNamespace(items=[{"memory_id": "m1"}]))
    result = module.retrieve("DIALOGBENCH", [("USER: hotel", {"label": "SECRET", "leaky_concat": "SECRET",
                            "analysis": "SECRET", "prompt_SFT": "SECRET", "query": "SECRET", "source_line": 2})],
                            ["hotel"], "u", "local")
    assert "SECRET" not in str(captured[0])
    assert captured[0].metadata["source_line"] == 2
    assert result["hit_at_5"] == 1
