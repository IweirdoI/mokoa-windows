"""Deterministic PersonaChat / BPMN eval helpers; no model or dataset download."""
import random

from src.core.persona_query import last_persona_b_turn, persona_query
from src.ingestion.bpmn_text import bpmn_task_query, extract_bpmn_labels


def test_persona_query_keeps_traits_not_chitchat():
    facts = ["Hey there!", "I like to play basketball.", "I work as a nurse."]
    result = persona_query(facts)
    assert "basketball" in result and "nurse" in result
    assert "Hey there" not in result
    assert persona_query(["Hi", "Hello"]) == ""


def test_last_persona_b_turn_prefers_speaker_b():
    turns = [
        "Persona A: Hey there!",
        "Persona B: I am a nurse.",
        "Persona A: Cool.",
        "Persona B: What do you do?",
    ]
    assert last_persona_b_turn(turns) == "What do you do?"


def test_bpmn_extract_collapses_newlines_and_skips_geometry():
    xml = """<?xml version="1.0"?>
    <definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
      <process id="p1">
        <task id="t1" name="create a&#10;new&#10;bank account"/>
        <startEvent id="s" name="Application received"/>
      </process>
      <BPMNShape id="shape1" name="should-skip-if-present"/>
    </definitions>
    """
    # ElementTree does not decode numeric entities in attributes the same on all
    # parsers; use a real newline in the attribute via a constructed tree.
    xml = """<?xml version="1.0"?>
    <definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
      <process id="p1">
        <task id="t1" name="create a
new
bank account"/>
        <startEvent id="s" name="Application received"/>
      </process>
    </definitions>
    """
    extracted = extract_bpmn_labels(xml)
    assert extracted["parse_ok"]
    assert "create a new bank account" in extracted["tasks"]
    query = bpmn_task_query(extracted)
    assert "create a new bank account" in query
    assert "Application received" in query
    assert extracted["text"].startswith("BPMN process steps:")
    assert "xmlns" not in extracted["text"]


def test_bpmn_parse_failure_stays_empty():
    extracted = extract_bpmn_labels("<not-xml")
    assert extracted["text"] == "" and extracted["parse_ok"] is False
    assert bpmn_task_query(extracted) == ""


def test_personachat_loader_keeps_persona_facts(tmp_path, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from benchmarks import run_dataset_eval as module

    path = tmp_path / "PersonaChat"
    path.mkdir()
    table = pa.table({
        "conv_id": ["c1"],
        "persona_b": [["I like cats.", "I work nights."]],
        "dialogue": [["Persona A: Hi", "Persona B: I like cats a lot.", "Persona A: Bye"]],
    })
    pq.write_table(table, path / "personachat.parquet")
    monkeypatch.setattr(module, "GIT", tmp_path)
    items, indices = module.load_personachat(1, random.Random(42))
    assert indices == [0]
    content, meta = items[0]
    assert "I like cats" in content
    assert meta["persona_facts"][0] == "I like cats."
    assert meta["last_user"] == "I like cats a lot."
    assert persona_query(meta["persona_facts"]).startswith("I like cats")


def test_bpmn_loader_ignores_words_xml(tmp_path, monkeypatch):
    from benchmarks import run_dataset_eval as module

    annotations = tmp_path / "BPMN/data/annotations/ex00"
    words = tmp_path / "BPMN/data/words/ex00"
    annotations.mkdir(parents=True)
    words.mkdir(parents=True)
    (annotations / "ex00_writer0001.bpmn").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
        <!-- pad so the loader keeps files larger than 200 bytes -->
        <definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
          <process id="Process_1">
            <startEvent id="start" name="Application received"/>
            <task id="t1" name="notify customer by email"/>
            <endEvent id="end" name="done"/>
          </process>
        </definitions>
        """,
        encoding="utf-8",
    )
    (words / "ex00_writer0001.xml").write_text(
        """<?xml version="1.0"?><annotation><object><name>letter</name></object></annotation>""",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "GIT", tmp_path)
    items, indices = module.load_bpmn(2, random.Random(42))
    assert indices == [0]
    content, meta = items[0]
    assert "notify customer by email" in content
    assert "letter" not in content
    assert "ex00_writer0001.bpmn" in meta["task_query"]
    assert "ex00" in meta["task_query"]
    assert "notify customer by email" in meta["task_query"]
    assert "<?xml" in meta["raw_prefix"]
    assert content.startswith("BPMN process steps:")
    assert "file=ex00_writer0001.bpmn" in content
    assert "exercise=ex00" in content


def test_persona_ingest_drops_dialogue_and_keeps_facts():
    from src.core.persona_query import persona_ingest_text, is_persona_as_user_behavior

    text = (
        "[PERSONA] I like cats. I work nights. [DIALOGUE] "
        "Persona A: What kind of movies do you like? Persona B: Hey there!"
    )
    ingested = persona_ingest_text(text)
    assert "I like cats" in ingested
    assert "I work nights" in ingested
    assert "DIALOGUE" not in ingested
    assert "movies" not in ingested
    assert is_persona_as_user_behavior("I am a nurse.", action="stated_persona")
    assert not is_persona_as_user_behavior("用户打开了 VSCode", action="open")
