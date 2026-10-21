"""纯语义输入与概率契约测试；已知真值集合不代表模型语义准确率。"""
from __future__ import annotations

import ast
import copy
import json
import math
import random
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from dataset_recommender.agent import semantic_relations as S
from dataset_recommender.secret_patterns import find_secret_patterns, redact


@pytest.fixture
def pair():
    return {"old": {"kind": "proposition", "text": "每个红色方块都是方块。"},
            "new": {"kind": "proposition", "text": "有一个方块。"}}


def test_fixed_names_order_and_immutable_name_maps():
    assert S.SEMANTIC_RELATIONS == (
        "equivalent", "old_entails_new_strict", "new_entails_old_strict",
        "disjoint", "overlap_incomparable",
    )
    assert S.NLI_CLASSES == ("entailment", "neutral", "contradiction")
    assert tuple(S.SEMANTIC_TO_NLI) == tuple(S.SWAPPED_RELATIONS) == S.SEMANTIC_RELATIONS
    assert [S.SEMANTIC_TO_NLI[name] for name in S.SEMANTIC_RELATIONS] == [
        "entailment", "entailment", "neutral", "contradiction", "neutral",
    ]
    for table in (S.SEMANTIC_TO_NLI, S.SWAPPED_RELATIONS):
        with pytest.raises(TypeError):
            table["equivalent"] = "contradiction"


@pytest.mark.parametrize("premise,hypothesis,forward,reverse", [
    ({1, 2}, {1, 2}, "equivalent", "equivalent"),
    ({1}, {1, 2}, "old_entails_new_strict", "new_entails_old_strict"),
    ({1, 2}, {1}, "new_entails_old_strict", "old_entails_new_strict"),
    ({1}, {2}, "disjoint", "disjoint"),
    ({1, 2}, {2, 3}, "overlap_incomparable", "overlap_incomparable"),
])
def test_known_nonempty_truth_sets_both_directions(premise, hypothesis, forward, reverse):
    if premise == hypothesis:
        known = "equivalent"
    elif premise < hypothesis:
        known = "old_entails_new_strict"
    elif hypothesis < premise:
        known = "new_entails_old_strict"
    elif not premise & hypothesis:
        known = "disjoint"
    else:
        known = "overlap_incomparable"
    assert forward == known
    assert S.swap_relation(forward) == reverse
    assert S.swap_relation(reverse) == forward
    for relation in (forward, reverse):
        leaf = tuple(float(name == relation) for name in S.SEMANTIC_RELATIONS)
        parent = S.project_nli_probabilities(leaf)
        assert parent == tuple(float(name == S.SEMANTIC_TO_NLI[relation]) for name in S.NLI_CLASSES)
        assert S.swap_probabilities(S.swap_probabilities(leaf)) == leaf


def test_strict_reverse_is_forward_neutral_not_entailment():
    forward = (0.0, 1.0, 0.0, 0.0, 0.0)
    reverse = S.swap_probabilities(forward)
    assert reverse == (0.0, 0.0, 1.0, 0.0, 0.0)
    assert S.project_nli_probabilities(forward) == (1.0, 0.0, 0.0)
    assert S.project_nli_probabilities(reverse) == (0.0, 1.0, 0.0)
    with pytest.raises(S.SemanticRelationError):
        S.swap_probabilities(S.project_nli_probabilities(forward))


def test_parent_probability_sum_is_not_leaf_argmax_mapping():
    probabilities = (0.30, 0.05, 0.28, 0.10, 0.27)
    leaf_winner = S.SEMANTIC_RELATIONS[max(range(5), key=probabilities.__getitem__)]
    projected = S.project_nli_probabilities(probabilities)
    parent_winner = S.NLI_CLASSES[max(range(3), key=projected.__getitem__)]
    assert projected == pytest.approx((0.35, 0.55, 0.10))
    assert leaf_winner == "equivalent"
    assert S.SEMANTIC_TO_NLI[leaf_winner] == "entailment"
    assert parent_winner == "neutral"
    assert S.project_nli_probabilities([0.2] * 5) == pytest.approx((0.4, 0.4, 0.2))


def test_random_simplex_projection_and_swap_conserve_mass():
    rng = random.Random(19)
    for _ in range(1000):
        raw = [rng.random() for _ in S.SEMANTIC_RELATIONS]
        probabilities = tuple(value / math.fsum(raw) for value in raw)
        swapped = S.swap_probabilities(probabilities)
        assert swapped == (probabilities[0], probabilities[2], probabilities[1],
                           probabilities[3], probabilities[4])
        assert S.swap_probabilities(swapped) == probabilities
        assert math.fsum(S.project_nli_probabilities(probabilities)) == pytest.approx(1.0)
        assert math.fsum(S.project_nli_probabilities(swapped)) == pytest.approx(1.0)


@pytest.mark.parametrize("bad", [
    None, {}, "0.2,0.2,0.2,0.2,0.2", b"12345", bytearray(b"12345"),
    [], [1.0], [0.0] * 5, [1.0] * 5, [0.2] * 3,
    (value for value in [0.2] * 5),
])
@pytest.mark.parametrize("function", [S.validate_probabilities, S.project_nli_probabilities, S.swap_probabilities])
def test_invalid_probability_container_and_simplex(bad, function):
    with pytest.raises(S.SemanticRelationError) as error:
        function(bad)
    assert error.value.code == "semantic_invalid_probabilities"
    assert error.value.hint == str(error.value)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), -1e-12,
                                  1.0 + 1e-12, True, False, "0.2", None, 0.2j])
@pytest.mark.parametrize("index", range(5))
def test_probability_values_are_finite_real_nonboolean_and_bounded(bad, index):
    values = [0.2] * 5
    values[index] = bad
    for function in (S.validate_probabilities, S.project_nli_probabilities, S.swap_probabilities):
        with pytest.raises(S.SemanticRelationError):
            function(values)


def test_probability_tolerance_is_absolute_and_does_not_renormalize():
    values = (0.5000005, 0.0, 0.5, 0.0, 0.0)
    assert S.validate_probabilities(values) == values
    assert S.project_nli_probabilities(values) == (0.5000005, 0.5, 0.0)
    assert S.validate_probabilities((0, 0, 0, 1, 0)) == (0.0, 0.0, 0.0, 1.0, 0.0)
    with pytest.raises(S.SemanticRelationError):
        S.validate_probabilities((0.500002, 0.5, 0.0, 0.0, 0.0))
    with pytest.raises(S.SemanticRelationError):
        S.project_nli_probabilities((0.5000005, 0.5, 0.0, 0.0, 0.0))
    assert S.validate_probabilities([0.2, 0.3, 0.5], size=3) == (0.2, 0.3, 0.5)
    for size in (True, 1, 4, 6, "5", None):
        with pytest.raises(S.SemanticRelationError):
            S.validate_probabilities([0.2] * 5, size=size)


@pytest.mark.parametrize("relation", ["entailment", "duplicate", "uncertain", "", None, [], 1])
def test_unknown_relations_never_become_a_semantic_leaf(relation):
    with pytest.raises(S.SemanticRelationError) as error:
        S.swap_relation(relation)
    assert error.value.code == "semantic_invalid_relation"


def test_serializer_exact_public_proposition_contract_and_orientation(pair):
    expected = ('{"frame":{"mode":"same_domain","background":""},'
                '"old":{"kind":"proposition","text":"每个红色方块都是方块。"},'
                '"new":{"kind":"proposition","text":"有一个方块。"}}')
    assert S.semantic_pair_text(pair) == expected
    projection = S.validate_semantic_pair(pair)
    assert tuple(projection) == ("frame", "old", "new")
    assert projection["old"] == pair["old"] and projection["new"] == pair["new"]
    swapped = S.swap_pair(pair)
    assert swapped["old"] == pair["new"] and swapped["new"] == pair["old"]
    assert S.swap_pair(swapped) == projection
    assert S.semantic_pair_text(swapped) != expected
    assert "permanence" not in projection["old"]


@pytest.mark.parametrize("kind", ["proposition", "memory", "skill", "example"])
def test_supported_kinds_share_semantic_projection_without_runtime_registry(pair, kind):
    pair["old"]["kind"] = pair["new"]["kind"] = kind
    pair["old"].update(title="规范", trigger="周一且请求完整时", permanence="temporary",
                      payload={"tools": ["step.b", "step.a"], "utterance": "先检查再保存。",
                               "steps": [{"verb": "unregistered.check", "args": "检查输入"},
                                         {"verb": "unregistered.save", "args": "保存结果"}]})
    projection = S.validate_semantic_pair(pair)
    assert projection["old"]["payload"] == pair["old"]["payload"]
    assert projection["old"]["trigger"] == "周一且请求完整时"
    assert projection["old"]["permanence"] == "temporary"


def test_steps_lists_and_conditions_are_preserved_but_mapping_order_is_canonical(pair):
    pair["old"].update(trigger="本周有效，只有请求经过检查后执行。", permanence="temporary",
        payload={"steps": [{"args": {"z": [2, 1], "a": False}, "verb": "check"},
                           {"args": {"output": None}, "verb": "save"}], "tools": ["save", "check"]})
    before = copy.deepcopy(pair)
    text = S.semantic_pair_text(pair)
    reordered = copy.deepcopy(pair)
    reordered["old"]["payload"]["steps"][0]["args"] = {"a": False, "z": [2, 1]}
    assert S.semantic_pair_text(reordered) == text
    reordered["old"]["payload"]["steps"].reverse()
    assert S.semantic_pair_text(reordered) != text
    reordered = copy.deepcopy(pair)
    reordered["old"]["trigger"] = "下周有效，只有请求经过检查后执行。"
    assert S.semantic_pair_text(reordered) != text
    projection = json.loads(text)
    assert [step["verb"] for step in projection["old"]["payload"]["steps"]] == ["check", "save"]
    assert projection["old"]["payload"]["steps"][0]["args"]["z"] == [2, 1]
    assert projection["old"]["payload"]["tools"] == ["save", "check"]
    assert pair == before
    assert S.semantic_pair_text(projection) == text


@pytest.mark.parametrize("field", sorted(S._NONSEMANTIC_FIELDS))
def test_metadata_is_stripped_only_at_declared_structure_levels(pair, field):
    pair.update({field: "pair-private"})
    pair["frame"] = {field: "frame-private"}
    for entry in (pair["old"], pair["new"]):
        entry[field] = "entry-private"
        entry["payload"] = {field: "payload-private", "steps": [
            {"verb": "check", field: "step-private", "args": {field: "args-private", "value": "事实"}},
        ]}
    output = S.semantic_pair_text(pair)
    for omitted in ("pair-private", "frame-private", "entry-private", "payload-private", "step-private"):
        assert omitted not in output
    projection = json.loads(output)
    for side in ("old", "new"):
        assert projection[side]["payload"]["steps"][0]["args"] == {field: "args-private", "value": "事实"}
    assert S.SEMANTIC_RELATIONS == tuple(S.SWAPPED_RELATIONS)


def test_case_variant_metadata_is_omitted_and_identity_differences_do_not_decide(pair):
    original = S.semantic_pair_text(pair)
    pair["old"].update(principal="subject-a", endpoint_fp="endpoint-a", project_id="project-a",
                       permissions=["read"], GOLD="disjoint")
    pair["new"].update(principal="subject-b", endpoint_fp="endpoint-b", project_id="project-b",
                       permissions=["write"], LABEL="equivalent")
    pair["label_origin"] = "public-coarse-gold"
    assert S.semantic_pair_text(pair) == original


@pytest.mark.parametrize("field", ["id", "label", "project_id", "scope", "permission"])
def test_functional_argument_differences_survive_both_directions_and_step_order(pair, field):
    for side, value in (("old", "dataset-a"), ("new", "dataset-b")):
        pair[side].update(kind="example", text="先检查对象，再保存结果。", payload={"steps": [
            {"verb": "check", "args": {field: value, "nested": {"id": value}}},
            {"verb": "save", "args": {"permission": "write", "permissions": ["read", "write"]}},
        ]})
    projection = S.validate_semantic_pair(pair)
    assert projection["old"] != projection["new"]
    assert projection["old"]["payload"]["steps"][0]["args"][field] == "dataset-a"
    assert projection["new"]["payload"]["steps"][0]["args"][field] == "dataset-b"
    assert projection["old"]["payload"]["steps"][0]["args"]["nested"] == {"id": "dataset-a"}
    for side in ("old", "new"):
        steps = projection[side]["payload"]["steps"]
        assert [step["verb"] for step in steps] == ["check", "save"]
        assert steps[1]["args"] == {"permission": "write", "permissions": ["read", "write"]}
    changed = copy.deepcopy(pair)
    changed["new"] = copy.deepcopy(pair["old"])
    assert S.semantic_pair_text(pair) != S.semantic_pair_text(changed)
    swapped = S.swap_pair(pair)
    assert swapped["old"] == projection["new"] and swapped["new"] == projection["old"]
    assert S.semantic_pair_text(pair) != S.semantic_pair_text(swapped)
    swapped["old"]["payload"]["steps"].reverse()
    assert S.semantic_pair_text(swapped) != S.semantic_pair_text(S.swap_pair(pair))


def test_functional_permission_values_and_whitespace_are_not_normalized_away(pair):
    pair["old"]["payload"] = {"steps": [{"verb": "check", "args": {
        "permission": "read", "project_id": "project-a", " label ": "  exact  ", "padding": " \t ",
    }}]}
    pair["new"]["payload"] = copy.deepcopy(pair["old"]["payload"])
    pair["new"]["payload"]["steps"][0]["args"]["permission"] = "write"
    projection = S.validate_semantic_pair(pair)
    assert projection["old"]["payload"]["steps"][0]["args"] == pair["old"]["payload"]["steps"][0]["args"]
    assert projection["new"]["payload"]["steps"][0]["args"]["permission"] == "write"
    assert projection["old"]["payload"] != projection["new"]["payload"]


def test_anchored_redaction_is_single_source_for_all_semantic_strings(pair, monkeypatch):
    secret = "sk-" + "A" * 40
    text = "引用 " + secret
    pair["frame"] = {"background": text}
    for entry in (pair["old"], pair["new"]):
        entry.update(text=text, title=text, trigger=text, payload={"tools": [text], "utterance": text,
            "steps": [{"verb": text, "args": {"nested": [text]}}]})
    output = S.semantic_pair_text(pair)
    assert secret not in output
    assert output.count(redact(text)) == 15
    assert not find_secret_patterns(output)
    assert output == S.semantic_pair_text(json.loads(output))
    calls = []
    real_redact = S.redact
    monkeypatch.setattr(S, "redact", lambda value: calls.append(value) or real_redact(value))
    S.semantic_pair_text(pair)
    assert text in calls


def test_errors_never_echo_input_and_argument_keys_use_redaction(pair):
    secret = "ghp_" + "b" * 36
    invalid = copy.deepcopy(pair)
    invalid["old"][secret] = secret
    with pytest.raises(S.SemanticRelationError) as error:
        S.semantic_pair_text(invalid)
    assert secret not in str(error.value) and secret not in error.value.code
    pair["old"]["payload"] = {"steps": [{"verb": "check", "args": {secret: "事实"}}]}
    output = S.semantic_pair_text(pair)
    assert secret not in output
    assert json.loads(output)["old"]["payload"]["steps"][0]["args"] == {redact(secret): "事实"}
    assert S.semantic_pair_text(json.loads(output)) == output


def test_argument_key_redaction_collisions_fail_closed_without_erasing_a_value(pair):
    first, second = "ghp_" + "a" * 36, "ghp_" + "b" * 36
    pair["old"]["payload"] = {"steps": [{"verb": "check", "args": {first: "A", second: "B"}}]}
    with pytest.raises(S.SemanticRelationError) as error:
        S.semantic_pair_text(pair)
    assert error.value.code == "semantic_invalid_payload"
    assert first not in str(error.value) and second not in str(error.value)


@pytest.mark.parametrize("bad", [None, [], "pair", {}, {"new": {"kind": "proposition", "text": "事实"}},
    {"old": None, "new": {"kind": "proposition", "text": "事实"}},
    {"old": {}, "new": {"kind": "proposition", "text": "事实"}},
    {"old": {"kind": "proposition", "text": ""}, "new": {"kind": "proposition", "text": "事实"}}])
def test_no_old_or_empty_inputs_are_not_fake_pairs(bad):
    with pytest.raises(S.SemanticRelationError):
        S.semantic_pair_text(bad)


@pytest.mark.parametrize("frame", [None, [], "same_domain", {"mode": "unknown"}, {"mode": ""},
    {"mode": "different_domain"}, {"mode": False}, {"old_domain": "a", "new_domain": "b"},
    {"mode": "same_domain", "background": []}])
def test_unknown_empty_or_inconsistent_interpretation_frame_errors(pair, frame):
    pair["frame"] = frame
    with pytest.raises(S.SemanticRelationError):
        S.semantic_pair_text(pair)


def test_same_domain_background_is_visible_and_not_inferred_from_identity(pair):
    pair["frame"] = {"mode": "same_domain", "background": "量词仅限于三个对象，陈述均可满足。"}
    assert json.loads(S.semantic_pair_text(pair))["frame"] == pair["frame"]


@pytest.mark.parametrize("field,value", [
    ("kind", "unknown"), ("kind", None), ("text", " \n\t"), ("text", 3), ("text", None),
    ("text", "事实\x00"), ("text", "事实\x7f"), ("text", "\ud800"), ("title", None),
    ("trigger", False), ("permanence", "unknown"), ("permanence", ""), ("permanence", None),
    ("payload", None), ("payload", {"unexpected": "未知"}), ("unsupported", "未知"),
])
def test_entry_shape_unknowns_and_types_fail_closed(pair, field, value):
    pair["old"][field] = value
    with pytest.raises(S.SemanticRelationError):
        S.semantic_pair_text(pair)


@pytest.mark.parametrize("payload", [
    {"tools": "check"}, {"tools": [None]}, {"tools": [""]}, {"utterance": ""},
    {"steps": []}, {"steps": "check"}, {"steps": [None]}, {"steps": [{"args": "参数"}]},
    {"steps": [{"verb": "check", "unknown": "事实"}]},
    {"steps": [{"verb": "check", "args": float("nan")}]},
    {"steps": [{"verb": "check", "args": float("inf")}]},
    {"steps": [{"verb": "check", "args": {1: "事实"}}]},
    {"steps": [{"verb": "check", "args": {"": "事实"}}]},
    {"steps": [{"verb": "check", "args": {" \t ": "事实"}}]},
    {"steps": [{"verb": "check", "args": 2 ** 63}]},
    {"steps": [{"verb": "check", "args": {"set"}}]},
])
def test_payload_and_json_limits_reject_bad_data_without_guessing(pair, payload):
    pair["old"]["payload"] = payload
    with pytest.raises(S.SemanticRelationError):
        S.semantic_pair_text(pair)


@pytest.mark.parametrize("field", ["text", "title", "trigger"])
def test_text_exact_boundary_without_silent_truncation(pair, field):
    pair["old"][field] = "字" * S.MAX_TEXT_CHARS
    assert json.loads(S.semantic_pair_text(pair))["old"][field] == pair["old"][field]
    pair["old"][field] += "字"
    with pytest.raises(S.SemanticRelationError):
        S.semantic_pair_text(pair)


def test_research_limits_long_text_and_pair_share_serializer_and_pieces(pair):
    assert dict(S.SEMANTIC_LIMITS_PRODUCT) == {"max_text_chars": 8192, "max_pair_chars": 65536}
    assert dict(S.SEMANTIC_LIMITS_RESEARCH) == {"max_text_chars": 65536, "max_pair_chars": 262144}
    for limits in (S.SEMANTIC_LIMITS_PRODUCT, S.SEMANTIC_LIMITS_RESEARCH):
        with pytest.raises(TypeError):
            limits["max_text_chars"] = 1
    pair["old"].update(text="甲" * 60000, title="题" * 9000,
                       payload={"utterance": "话" * 9000, "steps": [{"verb": "read", "args": {"contents": ["文" * 60000]}}]})
    pair["new"]["text"] = "乙" * 7000
    pair["frame"] = {"background": "域" * 9000}
    with pytest.raises(S.SemanticRelationError, match="长度"):
        S.semantic_pair_text(pair)
    text = S.semantic_pair_text(pair, limits=S.SEMANTIC_LIMITS_RESEARCH)
    assert 65536 < len(text) <= 262144
    assert json.loads(text)["old"]["text"] == pair["old"]["text"]
    assert json.loads(text)["old"]["payload"] == pair["old"]["payload"]
    pieces = S.semantic_pair_pieces(pair, limits=S.SEMANTIC_LIMITS_RESEARCH)
    assert pieces["raw_text"] == text
    swapped = S.swap_pair(pair, limits=S.SEMANTIC_LIMITS_RESEARCH)
    assert swapped["new"]["text"] == pair["old"]["text"]


@pytest.mark.parametrize("field", ["text", "title", "background"])
def test_research_text_title_background_use_same_cap(pair, field):
    destination = pair.setdefault("frame", {}) if field == "background" else pair["old"]
    destination[field] = "字" * 65536
    output = S.validate_semantic_pair(pair, limits=S.SEMANTIC_LIMITS_RESEARCH)
    assert (output["frame"] if field == "background" else output["old"])[field] == destination[field]
    destination[field] += "字"
    with pytest.raises(S.SemanticRelationError, match="长度"):
        S.validate_semantic_pair(pair, limits=S.SEMANTIC_LIMITS_RESEARCH)


def test_product_pair_cap_and_wire_limits_do_not_change(pair):
    for side in ("old", "new"):
        pair[side].update(text="甲" * 8192, title="题" * 8192, trigger="条" * 8192,
                          payload={"utterance": "话" * 8192, "steps": [{"verb": "read", "args": "文" * 8192}]})
    with pytest.raises(S.SemanticRelationError) as error:
        S.semantic_pair_text(pair)
    assert error.value.code == "semantic_input_too_large"
    assert len(S.semantic_pair_text(pair, limits=S.SEMANTIC_LIMITS_RESEARCH)) > 65536
    pair["limits"] = dict(S.SEMANTIC_LIMITS_RESEARCH)
    with pytest.raises(S.SemanticRelationError) as error:
        S.validate_semantic_pair(pair, limits=S.SEMANTIC_LIMITS_RESEARCH)
    assert error.value.code == "semantic_invalid_pair"


def test_sequence_depth_node_and_serialized_length_boundaries(pair):
    pair["old"]["payload"] = {"tools": ["check"] * S.MAX_PAYLOAD_ITEMS}
    assert len(S.validate_semantic_pair(pair)["old"]["payload"]["tools"]) == S.MAX_PAYLOAD_ITEMS
    pair["old"]["payload"]["tools"].append("check")
    with pytest.raises(S.SemanticRelationError):
        S.semantic_pair_text(pair)
    nested = "事实"
    for _ in range(S.MAX_PAYLOAD_DEPTH):
        nested = [nested]
    pair["old"]["payload"] = {"steps": [{"verb": "check", "args": nested}]}
    S.semantic_pair_text(pair)
    pair["old"]["payload"]["steps"][0]["args"] = [nested]
    with pytest.raises(S.SemanticRelationError):
        S.semantic_pair_text(pair)
    pair["old"]["payload"] = {"steps": [{"verb": "check", "args": [None] * 64}] * 9}
    with pytest.raises(S.SemanticRelationError):
        S.semantic_pair_text(pair)
    pair["old"].pop("payload")
    length = len(S.semantic_pair_text(pair))
    limits = dict(S.SEMANTIC_LIMITS_PRODUCT, max_pair_chars=length)
    S.semantic_pair_text(pair, limits=limits)
    limits["max_pair_chars"] = length - 1
    with pytest.raises(S.SemanticRelationError) as error:
        S.semantic_pair_text(pair, limits=limits)
    assert error.value.code == "semantic_input_too_large"


def test_cyclic_payload_and_huge_mapping_are_bounded(pair):
    cycle = []
    cycle.append(cycle)
    pair["old"]["payload"] = {"steps": [{"verb": "check", "args": cycle}]}
    with pytest.raises(S.SemanticRelationError):
        S.semantic_pair_text(pair)
    pair["old"]["payload"] = {"steps": [{"verb": "check", "args": {str(i): i for i in range(65)}}]}
    with pytest.raises(S.SemanticRelationError):
        S.semantic_pair_text(pair)


def test_stdlib_only_import_and_real_calls_without_site_packages():
    module = Path(S.__file__)
    tree = ast.parse(module.read_text(encoding="utf-8"))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imports <= {"__future__", "collections.abc", "types", "secret_patterns"}
    assert {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} <= {"hashlib", "json", "math"}
    code = (
        "import json,sys; sys.path.insert(0,sys.argv[1]); "
        "from dataset_recommender.agent.semantic_relations import semantic_pair_text,project_nli_probabilities; "
        "p={'old':{'kind':'proposition','text':'A'},'new':{'kind':'proposition','text':'B'}}; "
        "assert json.loads(semantic_pair_text(p))['old']['text']=='A'; "
        "assert project_nli_probabilities([0,0,1,0,0])==(0,1,0); "
        "assert not ({'torch','transformers','numpy','dataset_recommender.agent.memory_relations',"
        "'dataset_recommender.agent.knowledge','dataset_recommender.app.knowledge_service'} & set(sys.modules)); "
        "print('STDLIB_SEMANTIC_OK')"
    )
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", code, str(module.parents[2])],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "STDLIB_SEMANTIC_OK"


@pytest.fixture(scope="module")
def actual_semantic_tokenizer(tmp_path_factory):
    from transformers import AutoTokenizer

    archive = Path(__file__).resolve().parent / "fixtures" / "semantic-tokenizer.zip"
    assert archive.is_file(), "bundled tokenizer fixture is required for this CPU milestone"
    directory = tmp_path_factory.mktemp("semantic-tokenizer")
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(directory)
    names = {path.name for path in directory.iterdir()}
    assert names == {"tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
                     "LICENSE.txt", "MODEL_CARD.md"}, "bundled tokenizer fixture is incomplete"
    assert not any(path.suffix in {".bin", ".safetensors", ".pt"} for path in directory.iterdir()), \
        "bundled tokenizer fixture must not contain model weights"
    return AutoTokenizer.from_pretrained(str(directory), local_files_only=True)


@pytest.mark.parametrize("shape", ["long_short", "both_long", "long_frame", "program", "unicode"])
def test_balanced_visible_actual384_spans_and_reverse_information(actual_semantic_tokenizer, shape):
    pair = {"old": {"kind": "proposition", "text": "alpha beta " * 650},
            "new": {"kind": "proposition", "text": "UNIQUE HYPOTHESIS CONTENT"}}
    if shape == "both_long":
        pair["new"]["text"] = "gamma delta " * 600
    elif shape == "long_frame":
        pair["frame"] = {"background": "shared background " * 400}
    elif shape == "program":
        for role in ("old", "new"):
            pair[role].update(kind="skill", trigger="当用户确认之后", permanence="temporary",
                              payload={"tools": ["read", "write"], "utterance": "先读后写",
                                       "steps": [{"verb": "read", "args": {"z": [2, 1], "owner": "functional-object"}},
                                                 {"verb": "write", "args": "  original output  "}]})
    elif shape == "unicode":
        pair["old"]["text"] = ('中文🙂 引号" slash\\ 换行\n混合 ' * 150)
        pair["new"]["text"] = '短假设\t"中文🙂"'
    before = copy.deepcopy(pair)
    prepared = S.prepare_semantic_input(pair, actual_semantic_tokenizer, max_length=384, role_independent_frame=True)
    swapped = S.prepare_semantic_input(S.swap_pair(pair), actual_semantic_tokenizer, max_length=384, role_independent_frame=True)
    assert pair == before and prepared["raw_text"] == S.semantic_pair_text(pair)
    assert prepared["visible_information_sha256"] == swapped["visible_information_sha256"]
    assert prepared["directions"]["reverse"]["text"] == swapped["directions"]["forward"]["text"]
    forward = prepared["directions"]["forward"]
    reverse = prepared["directions"]["reverse"]
    assert forward["visible_information_sha256"] == reverse["visible_information_sha256"]
    for view in (forward, reverse):
        actual = actual_semantic_tokenizer(view["text"], padding=False, truncation=False)
        assert view["token_length"] == len(actual["input_ids"]) <= 384
        assert view["encoding"]["input_ids"] == actual["input_ids"]
        assert view["attention_mask_sum"] == view["token_length"]
        assert view["role_body_covered"] == {"old": True, "new": True}
        for field in view["fields"]:
            a, b = field["original_range"]
            assert 0 <= a < b <= len(prepared["raw_text"])
            for start, end in field["visible_source_ranges"]:
                assert a <= start < end <= b
            if field["rendered_range"]:
                start, end = field["rendered_range"]
                source_start = field["visible_source_ranges"][0][0]
                assert view["text"][start:end] == prepared["raw_text"][source_start:source_start + end - start]
        assert any(field["lost"] for field in view["fields"])
    assert prepared["requires_llm_fallback"] and not prepared["complete"]
    if shape == "long_short":
        assert 'UNIQUE HYPOTHESIS CONTENT' in forward["text"]
        assert next(field for field in forward["fields"] if field["path"] == ["new", "text"])["complete"]
    if shape == "program":
        fields = {tuple(field["path"]): field for field in forward["fields"]}
        assert ("old", "trigger") in fields and ("old", "permanence") in fields
        assert ("old", "payload", "steps", 0, "args", "owner") in fields
        assert prepared["raw_text"].index('"read"') < prepared["raw_text"].index('"write"')
    with pytest.raises(S.SemanticRelationError):
        S.validate_semantic_pair(prepared)


def test_visible_budget_frame_precondition_and_raw_limits_fail_closed(actual_semantic_tokenizer, pair):
    with pytest.raises(S.SemanticRelationError, match="正文"):
        S.prepare_semantic_input(pair, actual_semantic_tokenizer, max_length=8)
    pair["frame"] = {"background": "the old statement refers to the new statement"}
    with pytest.raises(S.SemanticRelationError) as error:
        S.prepare_semantic_input(pair, actual_semantic_tokenizer, max_length=384)
    assert error.value.code == "semantic_role_dependent_frame"
    assert pair["frame"]["background"] == "the old statement refers to the new statement"
    pair["old"]["text"] = "x" * 8193
    with pytest.raises(S.SemanticRelationError) as error:
        S.prepare_semantic_input(pair, actual_semantic_tokenizer, max_length=384, role_independent_frame=True)
    assert error.value.code == "semantic_invalid_text"


def test_visible_byte_bpe_boundary_merges_and_json_escapes():
    from tokenizers import Tokenizer, models, pre_tokenizers, processors
    from tokenizers.pre_tokenizers import ByteLevel
    from transformers import PreTrainedTokenizerFast

    vocab = {"[UNK]": 0, "[CLS]": 1, "[SEP]": 2}
    vocab.update({char: index + 3 for index, char in enumerate(sorted(ByteLevel.alphabet()))})
    for merged in ('="', '~"', "bc", "abc", '"!', '"!"'):
        vocab[merged] = len(vocab)
    backend = Tokenizer(models.BPE(vocab=vocab, merges=[("=", '"'), ("~", '"'), ("b", "c"), ("a", "bc"),
                                                      ('"', "!"), ('"!', '"')], unk_token="[UNK]"))
    backend.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    backend.post_processor = processors.TemplateProcessing(single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 1), ("[SEP]", 2)])
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=backend, unk_token="[UNK]", cls_token="[CLS]", sep_token="[SEP]")
    assert len(tokenizer('"abc"STR')["input_ids"]) < len(tokenizer('"ab"STR')["input_ids"])
    pair = {"old": {"kind": "example", "text": ('🙂 中文\\\"\n' * 200),
                    "payload": {"steps": [{"verb": "write", "args": {"path/with~escape": "  中文🙂\\\"\t  "}}]}},
            "new": {"kind": "memory", "text": "HYP 中文🙂"}}
    prepared = S.prepare_semantic_input(pair, tokenizer, max_length=384)
    for view in prepared["directions"].values():
        assert len(tokenizer(view["text"])["input_ids"]) == view["token_length"] <= 384
        assert view["role_body_covered"] == {"old": True, "new": True}
        assert view["special_tokens"] == 2
        _assert_tree_audit(prepared, view, tokenizer)
    assert prepared["directions"]["forward"]["visible_information_sha256"] == prepared["directions"]["reverse"]["visible_information_sha256"]
    assert tokenizer('"!"', add_special_tokens=False)["input_ids"] == [vocab['"!"']]
    punctuation = {"old": {"kind": "memory", "text": "!"}, "new": {"kind": "memory", "text": "ok"}}
    complete = S.prepare_semantic_input(punctuation, tokenizer, max_length=384)
    assert complete["complete"] and max(complete["full_visible_token_lengths"]) <= 384
    for view in complete["directions"].values():
        field = next(field for field in view["fields"] if field["path"] == ["old", "text"])
        indices = [index for index in field["content_token_indices"] if view["encoding"]["input_ids"][index] == vocab['"!"']]
        assert indices and vocab['"!"'] not in tokenizer.all_special_ids and vocab['"!"'] != tokenizer.pad_token_id
        _assert_tree_audit(complete, view, tokenizer)


@pytest.mark.parametrize("partial", [False, True])
def test_visible_key_heads_without_encoded_body_rejected(actual_semantic_tokenizer, pair, partial):
    if partial:
        pair["old"]["text"] = "alpha beta " * 650
    class MissingBodyOffsets:
        def __getattr__(self, name):
            return getattr(actual_semantic_tokenizer, name)
        def __call__(self, text, **kwargs):
            encoded = dict(actual_semantic_tokenizer(text, **kwargs))
            if "offset_mapping" in encoded:
                encoded["offset_mapping"] = [(0, 0)] * len(encoded["input_ids"])
            return encoded
    with pytest.raises(S.SemanticRelationError) as error:
        S.prepare_semantic_input(pair, MissingBodyOffsets(), max_length=384)
    assert error.value.code == "semantic_body_not_visible"


@pytest.mark.parametrize("left,right", [
    ([1, 2], {"0": 1, "1": 2}),
    ([{"x": {}}, {"x": []}], {"0": {"x": {}}, "1": {"x": []}}),
    ([], {}),
])
def test_typed_container_structure_visible_in_model_text_and_tokens(actual_semantic_tokenizer, left, right):
    pair = {"old": {"kind": "skill", "text": "read items", "payload": {"steps": [{"verb": "read", "args": {"items": left}}]}},
            "new": {"kind": "proposition", "text": "items read"}}
    other = copy.deepcopy(pair)
    other["old"]["payload"]["steps"][0]["args"]["items"] = right
    a = S.prepare_semantic_input(pair, actual_semantic_tokenizer, max_length=384)
    b = S.prepare_semantic_input(other, actual_semantic_tokenizer, max_length=384)
    assert json.loads(a["raw_text"])["old"]["payload"]["steps"][0]["args"]["items"] == left
    assert json.loads(b["raw_text"])["old"]["payload"]["steps"][0]["args"]["items"] == right
    for direction in ("forward", "reverse"):
        assert a["directions"][direction]["text"] != b["directions"][direction]["text"]
        assert a["directions"][direction]["encoding"]["input_ids"] != b["directions"][direction]["encoding"]["input_ids"]
        assert a["directions"][direction]["token_length"] <= 384
    assert a["complete"] and b["complete"]
    if left == [1, 2]:
        text = a["directions"]["forward"]["text"]
        assert '"items":[1,2]' in text
        fields = {tuple(field["path"]): field for field in a["directions"]["forward"]["fields"]}
        path = ("old", "payload", "steps", 0, "args", "items")
        assert fields[(*path, 0)]["rendered_range"][0] < fields[(*path, 1)]["rendered_range"][0]


def test_missing_container_type_is_lost_even_with_all_scalar_values(pair):
    pair["old"]["payload"] = {"steps": [{"verb": "read", "args": {"items": [1, 2]}}]}
    source = S.semantic_pair_pieces(pair)
    prefixes = {tuple(field["path"]): field["range"][1] - field["range"][0]
                for field in source["fields"] if field["leaf"]}
    with pytest.raises(S.SemanticRelationError, match="prefix"):
        S.render_semantic_fragments(source, prefixes)
    nodes = {(): "partial", ("old",): "partial"}
    nodes.update({path: "full" for path in prefixes})
    view = S.render_semantic_fragments(source, {"nodes": nodes})
    field = next(item for item in view["fields"] if item["path"] == ["old", "payload", "steps", 0, "args", "items"])
    assert field["lost"] and not field["complete"] and not view["complete"]


@pytest.mark.parametrize("partial", [False, True])
def test_padding_and_fully_masked_body_not_visible(actual_semantic_tokenizer, pair, partial):
    tokenizer = actual_semantic_tokenizer
    pair["old"]["text"] = pair["new"]["text"] = tokenizer.pad_token
    with pytest.raises(S.SemanticRelationError, match="padding/special"):
        S.prepare_semantic_input(pair, tokenizer, max_length=384)
    pair["old"]["text"], pair["new"]["text"] = "real premise", "real hypothesis"
    if partial:
        pair["old"]["text"] = "real premise " * 600
    class Masked:
        def __getattr__(self, name):
            return getattr(tokenizer, name)
        def __call__(self, text, **kwargs):
            output = dict(tokenizer(text, **kwargs))
            output["input_ids"] = [tokenizer.pad_token_id] * len(output["input_ids"])
            output["attention_mask"] = [0] * len(output["input_ids"])
            return output
    with pytest.raises(S.SemanticRelationError, match="attention"):
        S.prepare_semantic_input(pair, Masked(), max_length=384)
    valid = S.prepare_semantic_input(pair, tokenizer, max_length=384)
    assert valid["directions"]["forward"]["role_body_covered"] == {"old": True, "new": True}


def _assert_tree_audit(prepared, view, tokenizer):
    encoded = tokenizer(view["text"], return_offsets_mapping=True, padding=False, truncation=False)
    assert encoded["input_ids"] == view["encoding"]["input_ids"]
    for field in view["fields"]:
        source_ranges = []
        for segment in field["rendered_segments"]:
            a, b = segment["source_range"]
            c, d = segment["rendered_range"]
            assert prepared["raw_text"][a:b] == view["text"][c:d]
            source_ranges.append((a, b))
        if len(field["rendered_segments"]) != 1:
            assert field["rendered_range"] is None
        a, b = field["original_range"]
        visible = {i for left, right in source_ranges for i in range(left, right)}
        omitted = {i for left, right in field["omitted_ranges"] for i in range(left, right)}
        assert not visible & omitted and visible | omitted == set(range(a, b))
        assert field["complete"] == (not omitted)
        content_ranges = []
        inset = int(field["value_type"] == "string")
        for segment in field["rendered_segments"]:
            source_start, source_end = segment["source_range"]
            start, end = max(source_start, a + inset), min(source_end, b - inset)
            if start < end:
                shift = segment["rendered_range"][0] - source_start
                content_ranges.append((start + shift, end + shift))
        for index in field["content_token_indices"]:
            assert encoded["attention_mask"][index] == 1
            assert encoded["input_ids"][index] not in tokenizer.all_special_ids
            assert encoded["input_ids"][index] != tokenizer.pad_token_id
            left, right = encoded["offset_mapping"][index]
            assert left < right
            assert any(max(left, start) < min(right, end) for start, end in content_ranges)


@pytest.mark.parametrize("reverse", [False, True])
def test_direct_full_renderer_without_reasons_is_unspecified(pair, reverse):
    source = S.semantic_pair_pieces(pair)
    view = S.render_semantic_fragments(source, {"nodes": {(): "full"}}, reverse=reverse)
    assert view["complete"] and all(field["complete"] for field in view["fields"])
    assert view["text"] == S.semantic_pair_text(S.swap_pair(pair) if reverse else pair)
    assert all(field["selection_reason"] == "unspecified" for field in view["fields"])


def test_full_raw_fit_exact_and_all_fields_complete(actual_semantic_tokenizer, pair):
    pair["old"].update(kind="skill", payload={"steps": [{"verb": "read", "args": {"id": 0, "owner": False, "label": None,
                                                                                           "empty": {}, "list": [], "s": "0"}}]})
    prepared = S.prepare_semantic_input(pair, actual_semantic_tokenizer, max_length=384)
    assert prepared["recipe"] == "semantic-json-tree-witness-v3"
    assert prepared["complete"] and not prepared["requires_llm_fallback"]
    assert prepared["directions"]["forward"]["text"] == S.semantic_pair_text(pair)
    assert prepared["directions"]["reverse"]["text"] == S.semantic_pair_text(S.swap_pair(pair))
    assert prepared["raw_pair_sha256"] == prepared["directions"]["forward"]["view_sha256"]
    for view in prepared["directions"].values():
        assert all(field["complete"] and field["structural_complete"] and not field["lost"] for field in view["fields"])
        _assert_tree_audit(prepared, view, actual_semantic_tokenizer)


@pytest.mark.parametrize("target", [383, 384, 385])
def test_actual_full_fit_384_boundary(actual_semantic_tokenizer, target):
    pair = {"old": {"kind": "memory", "text": "alpha"}, "new": {"kind": "memory", "text": "beta"}}
    for count, suffix in ((count, suffix) for count in range(1, 400) for suffix in ("", "!")):
        pair["old"]["text"] = "alpha " * count + suffix
        lengths = [len(actual_semantic_tokenizer(S.semantic_pair_text(value))["input_ids"])
                   for value in (pair, S.swap_pair(pair))]
        if max(lengths) == target:
            break
    else:
        pytest.fail("actual tokenizer fixture did not reach requested boundary")
    result = S.prepare_semantic_input(pair, actual_semantic_tokenizer, max_length=384)
    assert result["full_visible_token_lengths"] == lengths
    assert result["complete"] == (target <= 384)
    if target <= 384:
        assert result["directions"]["forward"]["text"] == S.semantic_pair_text(pair)
    assert max(view["token_length"] for view in result["directions"].values()) <= 384


def test_only_one_full_direction_fits_still_uses_shared_partial(actual_semantic_tokenizer, pair):
    raw = S.semantic_pair_text(pair)
    class Directional:
        def __getattr__(self, name):
            return getattr(actual_semantic_tokenizer, name)
        def __call__(self, text, **kwargs):
            result = dict(actual_semantic_tokenizer(text, **kwargs))
            if text == raw:
                extra = 385 - len(result["input_ids"])
                for key in result:
                    result[key] += ([(0, 0)] if key == "offset_mapping" else [1] if key == "attention_mask" else
                                    [0] if key == "token_type_ids" else [self.sep_token_id]) * extra
            return result
    result = S.prepare_semantic_input(pair, Directional(), max_length=384)
    assert result["full_visible_token_lengths"][0] == 385 and result["full_visible_token_lengths"][1] <= 384
    assert not result["complete"]
    swapped = S.prepare_semantic_input(S.swap_pair(pair), Directional(), max_length=384)
    assert result["directions"]["reverse"]["encoding"] == swapped["directions"]["forward"]["encoding"]


@pytest.mark.parametrize("location", ["head", "middle", "tail", "repeated"])
def test_symmetric_difference_boundary_witnesses(actual_semantic_tokenizer, location):
    common = "alpha beta " * 320
    x, y = "WITNESSAPPLE", "WITNESSORANGE"
    if location == "head":
        left, right = x + common, y + common
    elif location == "tail":
        left, right = common + x, common + y
    else:
        left, right = common + x + common, common + y + common
        if location == "repeated":
            left, right = common + x + common, common + x + y + common
    pair = {"old": {"kind": "memory", "text": left}, "new": {"kind": "memory", "text": right}}
    prepared = S.prepare_semantic_input(pair, actual_semantic_tokenizer, max_length=384)
    swapped = S.prepare_semantic_input(S.swap_pair(pair), actual_semantic_tokenizer, max_length=384)
    view = prepared["directions"]["forward"]
    assert y in view["text"] and x in view["text"]
    assert view["text"] != prepared["directions"]["reverse"]["text"]
    assert prepared["directions"]["reverse"]["encoding"] == swapped["directions"]["forward"]["encoding"]
    assert any(field["selection_reason"] == "witness_string_boundary" for field in view["fields"])
    _assert_tree_audit(prepared, view, actual_semantic_tokenizer)


def test_explicit_tree_holes_multisegments_and_hidden_information_hash(pair):
    pair["old"]["text"] = '头部🙂"\\\n中间hidden尾部'
    pair["old"]["payload"] = {"steps": [{"verb": "read", "args": {"items": [11, 22, 33], "flag": True, "nil": None, "empty": []}}]}
    source = S.semantic_pair_pieces(pair)
    nodes = {(): "partial", ("frame",): "full", ("new",): "full", ("old",): "partial", ("old", "text"): [[1, 4], [10, 12]],
             ("old", "payload"): "partial", ("old", "payload", "steps"): "partial", ("old", "payload", "steps", 0): "partial",
             ("old", "payload", "steps", 0, "args"): "partial", ("old", "payload", "steps", 0, "args", "items"): "partial",
             ("old", "payload", "steps", 0, "args", "items", 1): "full"}
    view = S.render_semantic_fragments(source, {"nodes": nodes})
    assert "[ITEMS1,22,ITEMS1]" in view["text"] and "STR" in view["text"]
    field = next(field for field in view["fields"] if field["path"] == ["old", "text"])
    assert field["rendered_range"] is None and len(field["rendered_segments"]) > 1
    other = copy.deepcopy(pair)
    other["old"]["payload"]["steps"][0]["args"]["flag"] = False
    second = S.render_semantic_fragments(S.semantic_pair_pieces(other), {"nodes": nodes})
    assert source["raw_pair_sha256"] != S.semantic_pair_pieces(other)["raw_pair_sha256"]
    assert view["text"] == second["text"] and view["visible_information_sha256"] == second["visible_information_sha256"]
    bad = copy.deepcopy(nodes)
    bad[("old", "text")] = [[5, 6]]
    with pytest.raises(S.SemanticRelationError, match="Unicode"):
        S.render_semantic_fragments(source, {"nodes": bad})
    for segment in field["rendered_segments"]:
        a, b = segment["source_range"]
        c, d = segment["rendered_range"]
        assert source["raw_text"][a:b] == view["text"][c:d]


def test_selector_ignores_outer_metadata_and_synchronizes_equal_entries(actual_semantic_tokenizer):
    entry = {"kind": "memory", "text": "alpha beta " * 650}
    pair = {"old": entry, "new": copy.deepcopy(entry)}
    dirty = dict(pair, id="alternate", label="strict", provenance={"root_id": "different", "split": "anything"})
    before = S.prepare_semantic_input(pair, actual_semantic_tokenizer, max_length=384)
    after = S.prepare_semantic_input(dirty, actual_semantic_tokenizer, max_length=384)
    assert before == after
    assert before["directions"]["forward"]["text"] == before["directions"]["reverse"]["text"]
    assert before["directions"]["forward"]["encoding"] == before["directions"]["reverse"]["encoding"]


@pytest.mark.parametrize("fault", ["negative", "past_end", "shape", "attention"])
def test_malformed_actual_offsets_fail_closed(actual_semantic_tokenizer, pair, fault):
    class Broken:
        def __getattr__(self, name):
            return getattr(actual_semantic_tokenizer, name)
        def __call__(self, text, **kwargs):
            result = dict(actual_semantic_tokenizer(text, **kwargs))
            if "offset_mapping" in result:
                if fault == "attention":
                    result["attention_mask"] = [2] * len(result["input_ids"])
                else:
                    result["offset_mapping"][1] = {"negative": (-1, 1), "past_end": (1, len(text) + 1), "shape": (1,)}[fault]
            return result
    with pytest.raises(S.SemanticRelationError) as error:
        S.prepare_semantic_input(pair, Broken(), max_length=384)
    assert error.value.code == "semantic_invalid_encoding"


@pytest.mark.parametrize("value,marker", [(12345, "INT"), (1.25, "NUM"), (True, "BOOL"), (None, "NULL"), ({}, "OBJ"), ([], "ARR")])
def test_atomic_values_are_full_or_typed_holes_only(pair, value, marker):
    path = ("old", "payload", "steps", 0, "args")
    pair["old"]["payload"] = {"steps": [{"verb": "read", "args": value}]}
    source = S.semantic_pair_pieces(pair)
    nodes = {(): "partial", ("frame",): "full", ("new",): "full"}
    nodes.update({path[:depth]: "partial" for depth in range(1, len(path))})
    nodes[path] = "hole"
    view = S.render_semantic_fragments(source, {"nodes": nodes})
    assert '"args":' + marker in view["text"]
    nodes[path] = "full"
    full = S.render_semantic_fragments(source, {"nodes": nodes})
    assert '"args":' + json.dumps(value, separators=(",", ":")) in full["text"]
    nodes[path] = [[0, 1]]
    with pytest.raises(S.SemanticRelationError, match="非string"):
        S.render_semantic_fragments(source, {"nodes": nodes})


def test_key_membership_witness_and_asymmetric_tree_swap(actual_semantic_tokenizer):
    shared = "alpha beta " * 500
    pair = {role: {"kind": "skill", "text": shared, "payload": {"steps": [{"verb": "read", "args": args}]}}
            for role, args in (("old", {"a": shared, "left": [1, 2], "same": True}),
                               ("new", {"a": shared, "right": [2, 1], "same": None}))}
    result = S.prepare_semantic_input(pair, actual_semantic_tokenizer, max_length=384)
    swapped = S.prepare_semantic_input(S.swap_pair(pair), actual_semantic_tokenizer, max_length=384)
    assert result["directions"]["reverse"]["text"] == swapped["directions"]["forward"]["text"]
    assert result["directions"]["reverse"]["encoding"] == swapped["directions"]["forward"]["encoding"]
    view = result["directions"]["forward"]
    fields = {tuple(field["path"]): field for field in view["fields"]}
    for role, keys in (("old", ("a", "left", "same")), ("new", ("a", "right", "same"))):
        parent = (role, "payload", "steps", 0, "args")
        assert fields[parent]["structural_complete"]
        assert fields[parent]["selection_reason"] == "witness_membership"
        assert all(fields[(*parent, key)]["key_rendered_segments"] for key in keys)
    _assert_tree_audit(result, view, actual_semantic_tokenizer)


def test_larger_candidate_is_considered_after_an_over_budget_candidate(actual_semantic_tokenizer):
    seen = []
    class Nonmonotone:
        def __getattr__(self, name):
            return getattr(actual_semantic_tokenizer, name)
        def __call__(self, text, **kwargs):
            result = dict(actual_semantic_tokenizer(text, **kwargs))
            reject = '"alpha"STR' in text
            seen.append((text, reject))
            if reject:
                extra = max(0, 400 - len(result["input_ids"]))
                for key in result:
                    result[key] += ([(0, 0)] if key == "offset_mapping" else [1] if key == "attention_mask" else
                                    [0] if key == "token_type_ids" else [self.sep_token_id]) * extra
            return result
    pair = {"old": {"kind": "memory", "text": "alpha beta " * 650}, "new": {"kind": "memory", "text": "gamma"}}
    result = S.prepare_semantic_input(pair, Nonmonotone(), max_length=384)
    assert any(reject for _, reject in seen) and any('"alpha beta' in text and not reject for text, reject in seen)
    assert not result["complete"] and all(view["token_length"] <= 384 for view in result["directions"].values())


def test_base_uses_a_real_interior_token_not_special_prefix_or_suffix(actual_semantic_tokenizer):
    text = actual_semantic_tokenizer.pad_token * 500 + "alpha" + actual_semantic_tokenizer.pad_token * 500
    pair = {"old": {"kind": "memory", "text": text}, "new": {"kind": "memory", "text": "beta"}}
    result = S.prepare_semantic_input(pair, actual_semantic_tokenizer, max_length=384)
    field = next(field for field in result["directions"]["forward"]["fields"] if field["path"] == ["old", "text"])
    assert field["content_token_indices"]
    start = result["raw_text"].index("alpha", field["original_range"][0])
    encoded = actual_semantic_tokenizer(result["directions"]["forward"]["text"], return_offsets_mapping=True)
    assert any(start <= segment["source_range"][0] + encoded["offset_mapping"][index][0] - segment["rendered_range"][0]
               < segment["source_range"][0] + encoded["offset_mapping"][index][1] - segment["rendered_range"][0] <= start + len("alpha")
               for index in field["content_token_indices"] for segment in field["rendered_segments"]
               if segment["rendered_range"][0] <= encoded["offset_mapping"][index][0] < encoded["offset_mapping"][index][1] <= segment["rendered_range"][1])
    _assert_tree_audit(result, result["directions"]["forward"], actual_semantic_tokenizer)
