import pytest

from scripts.privacy_ner_eval import decode_bio, positions, score_cases


def test_bio_keeps_source_offsets_and_entity_boundaries():
    text = "陈雨禾住北京"
    offsets = [(0, 0), (0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (0, 0)]
    labels = ["O", "B-PER", "I-PER", "I-PER", "O", "B-LOC", "I-LOC", "O"]
    assert decode_bio(text, offsets, labels) == [(0, 3, "PER"), (4, 6, "LOC")]


def test_partial_address_is_not_counted_as_success():
    cases = [{"id":"address", "text":"我家北京云杉路1号", "secrets":["北京云杉路1号"]}]
    result = score_cases(cases, [positions([(2, 4)])])
    assert result["fully_covered_spans"] == 0


def test_public_entity_counts_as_false_positive():
    result = score_cases([{"id":"public", "text":"北京天气", "secrets":[]}], [positions([(0, 2)])])
    assert result["clean_cases_altered"] == 1
    assert result["extra_masked_chars"] == 2


def test_missing_prediction_does_not_silently_improve_score():
    with pytest.raises(ValueError):
        score_cases([{"id":"missing", "text":"hi", "secrets":[]}], [])
