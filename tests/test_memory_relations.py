"""离线关系合同；替身输出只证明接线与机械核验，不证明语义准确率。"""
from __future__ import annotations

import copy
import hashlib
import json
import socket
import urllib.request
from dataclasses import replace

import pytest

from dataset_recommender.agent import memory_relations as M
from dataset_recommender.agent.semantic_relations import SEMANTIC_RELATIONS, semantic_pair_text
from dataset_recommender.llm.llm_client import LLMConfig, LLMResult


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("unconfigured network in offline test")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(urllib.request, "urlopen", denied)
    monkeypatch.setattr(M, "call_llm", denied)


@pytest.fixture
def inputs():
    cfg = LLMConfig(enable_llm=True, api_key="test-memory-relations-key",
                    base_url="https://model.example/v1", model="decision-model")
    fp = hashlib.sha256(f"{cfg.base_url}|{cfg.model}".encode()).hexdigest()[:12]
    scope = {"principal": "user-a", "endpoint_fp": fp, "project_id": "project-a"}
    quote = "以后汇报先给结论，再给依据。"
    candidate = {**scope, "text": "汇报先给结论，再给依据。", "trigger": "汇报时",
                 "tier": "explicit", "evidence": [quote], "permanence": "persistent"}
    existing = [{**scope, "id": "r1", "text": "汇报先说结论，再解释原因。",
        "trigger": "汇报时", "tier": "explicit", "revision": 7, "evidence": [],
        "provenance": {"type": "knowledge_change", "quotes": ["原来就约定先说结论。"]}}]
    return candidate, existing, [{"id": "e1", "text": quote}], cfg


def decision(request, relation="equivalent", **updates):
    refs = [request["evidence"][0]["id"]]
    result = {"semantic_pairs": [{"old_id": row["id"], "relation": relation,
        "reason": "离线合同响应。", "old_quote": row["text"][:100],
        "new_quote": request["candidate"]["text"][:100], "evidence_refs": refs,
        "error_code": "model_uncertain" if relation is None else None} for row in request["existing"]],
        "edit_checks": [], "evidence_refs": refs, "error_code": None}
    for edit in request["context"]["edits"]:
        after = edit["after"][0]
        result["edit_checks"].append({"entry_id": edit["entry_id"], "supported": True,
            "reason": "离线编辑合同响应。", "before_refs": [{"field": "text", "quote": edit["before"]["text"][:100]}],
            "after_refs": [{"entry_id": after["entry_id"], "field": "text", "quote": after["body"]["text"][:100]}],
            "source_refs": [{"id": refs[0], "quote": request["evidence"][0]["text"][:100]}],
            "preserved": [], "error_code": None})
    result.update(updates)
    return result


def completion(raw, cfg, **updates):
    values = dict(text=raw if type(raw) is str else json.dumps(raw, ensure_ascii=False), attempted=True,
        succeeded=True, response_used=False, provider="openai-compatible", model=cfg.model, finish_reason="stop")
    values.update(updates)
    return LLMResult(**values)


def assess(inputs, *, transform=None, local=False, **kwargs):
    def raw(request):
        result = decision(request)
        return transform(result) if transform else result
    if "llm_call" not in kwargs and "predictor" not in kwargs:
        if local:
            kwargs["predictor"] = raw
        else:
            kwargs["llm_call"] = lambda prompt, cfg: completion(raw(json.loads(prompt.split("事实 JSON：\n", 1)[1])), cfg)
    return M.assess_relations(*inputs, **kwargs)


def failed(out, code):
    assert set(out) == {"version", "semantic_pairs", "edit_checks", "coverage", "evidence_refs", "error_code"}
    assert out["version"] == M.ASSESSMENT_VERSION and out["error_code"] == code
    assert out["coverage"]["complete"] is False
    assert "relation" not in out


@pytest.mark.parametrize("relation", SEMANTIC_RELATIONS)
@pytest.mark.parametrize("local", [False, True])
def test_five_leaves_keep_direction_quotes_origins_and_no_action(inputs, relation, local):
    out = assess(inputs, local=local, transform=lambda raw: dict(raw,
        semantic_pairs=[dict(raw["semantic_pairs"][0], relation=relation)]))
    assert M.RELATIONS == frozenset(SEMANTIC_RELATIONS)
    assert out["error_code"] is None and out["coverage"]["complete"]
    pair = out["semantic_pairs"][0]
    assert pair["relation"] == relation and pair["old"]["revision"] == 7
    assert pair["old"]["provenance"]["quotes"] == ["原来就约定先说结论。"]
    assert pair["sources"] == inputs[2]
    assert "approved" not in out and "replaceable_ids" not in out


def test_pair_contract_is_pure_shared_serializer_and_trusted_context(inputs):
    requests = []
    out = assess(inputs, predictor=lambda req: requests.append(copy.deepcopy(req)) or decision(req))
    assert out["error_code"] is None
    request = requests[0]
    pair = request["existing"][0]["semantic_pair"]
    assert semantic_pair_text(pair) == semantic_pair_text({"old": pair["old"], "new": pair["new"]})
    assert pair["old"]["text"] == inputs[1][0]["text"] and pair["new"]["text"] == inputs[0]["text"]
    serialized = json.dumps(request)
    assert inputs[0]["principal"] not in serialized and inputs[0]["endpoint_fp"] not in serialized
    assert inputs[0]["project_id"] not in serialized


@pytest.mark.parametrize("local", [False, True])
def test_no_old_still_checks_sources_without_fake_leaf_or_permanence_guess(inputs, local):
    inputs[1].clear()
    inputs[0].pop("permanence")
    calls = []
    out = assess(inputs, **({"predictor": lambda req: calls.append(req)} if local else {"llm_call": lambda *args: calls.append(args)}))
    assert out["coverage"] == {"status": "no_existing", "required_ids": [], "checked_ids": [], "complete": True}
    assert out["semantic_pairs"] == [] and out["evidence_refs"] == ["e1"] and calls == []
    inputs[0]["evidence"] = ["不在来源的引文"]
    failed(assess(inputs), "unverified_evidence")


@pytest.mark.parametrize("field,value,code", [
    ("enable_llm", False, "disabled"), ("mock_llm", True, "mock_not_used"),
    ("provider", "mock", "mock_not_used"), ("provider", "trial", "trial_not_used"),
    ("provider", "other", "unsupported_provider"), ("api_key", None, "no_key"),
    ("api_key", "replace_me", "no_key"), ("api_key", 8, "invalid_config"),
    ("enable_llm", "false", "invalid_config"), ("base_url", "", "invalid_config"),
    ("model", "", "invalid_config"), ("provider", None, "invalid_config")])
def test_config_bounds_never_call_or_fallback(inputs, field, value, code):
    setattr(inputs[3], field, value)
    calls = []
    failed(assess(inputs, llm_call=lambda *args: calls.append(args)), code)
    assert not calls


@pytest.mark.parametrize("settings,code", [({"enable_llm": False}, "disabled"),
    ({"mock_llm": True}, "mock_not_used"), ({"provider": "trial"}, "trial_not_used")])
def test_predictor_respects_ai_trial_mock(inputs, settings, code):
    for field, value in settings.items():
        setattr(inputs[3], field, value)
    failed(assess(inputs, local=True), code)


@pytest.mark.parametrize("relation", ["duplicate", "complement", "contextual", "temporary", "supersede", "conflict", "uncertain", [], "activate"])
def test_old_seven_classes_and_actions_are_not_leaves(inputs, relation):
    failed(assess(inputs, transform=lambda raw: dict(raw, semantic_pairs=[dict(raw["semantic_pairs"][0], relation=relation)])), "invalid_relation")


def test_abstention_is_outside_leaves_and_partial_results_remain_visible(inputs):
    inputs[1].append(dict(inputs[1][0], id="r2"))
    def partial(raw):
        raw["semantic_pairs"][1].update(relation=None, error_code="insufficient_evidence")
        return raw
    out = assess(inputs, transform=partial)
    failed(out, "insufficient_evidence")
    assert len(out["semantic_pairs"]) == 2 and out["semantic_pairs"][0]["relation"] == "equivalent"
    assert out["coverage"]["checked_ids"] == ["r1"]
    failed(assess(inputs, transform=lambda raw: dict(raw, semantic_pairs=raw["semantic_pairs"][:1])), "incomplete_coverage")


@pytest.mark.parametrize("mutate,code", [
    (lambda raw: raw.update(extra=True), "invalid_output_shape"),
    (lambda raw: raw["semantic_pairs"][0].update(old_id="unknown"), "unknown_reference"),
    (lambda raw: raw["semantic_pairs"][0].update(relation=[]), "invalid_relation"),
    (lambda raw: raw["semantic_pairs"][0].update(reason=""), "invalid_reason"),
    (lambda raw: raw["semantic_pairs"][0].update(extra="x"), "invalid_output_shape"),
    (lambda raw: raw["semantic_pairs"][0].update(evidence_refs=[]), "missing_evidence_refs"),
    (lambda raw: raw["semantic_pairs"][0].update(error_code="provider-secret"), "invalid_error_code"),
    (lambda raw: raw.update(evidence_refs=["unknown"]), "unknown_reference"),
    (lambda raw: raw.update(evidence_refs=["e1", "e1"]), "duplicate_reference"),
    (lambda raw: raw.update(edit_checks=[{}]), "incomplete_edit_coverage"),
    (lambda raw: raw["semantic_pairs"].append(raw["semantic_pairs"][0]), "duplicate_reference")])
@pytest.mark.parametrize("local", [False, True])
def test_model_shape_references_and_codes_fail_closed(inputs, mutate, code, local):
    def transform(raw):
        mutate(raw)
        return raw
    failed(assess(inputs, transform=transform, local=local), code)


@pytest.mark.parametrize("legacy", ["drop", "forged"])
@pytest.mark.parametrize("local", [False, True])
def test_pair_quotes_are_optional_and_server_bound(inputs, legacy, local):
    def transform(raw):
        for pair in raw["semantic_pairs"]:
            pair.pop("old_quote", None)
            pair.pop("new_quote", None)
            if legacy == "forged":
                pair.update(old_quote="伪造的旧文翻译", new_quote="缩略后的新文")
        return raw
    out = assess(inputs, transform=transform, local=local)
    assert out["error_code"] is None and out["coverage"]["complete"]
    pair = out["semantic_pairs"][0]
    assert pair["old_quote"] == inputs[1][0]["text"][:1000]
    assert pair["new_quote"] == inputs[0]["text"][:1000]
    assert "伪造" not in json.dumps(out, ensure_ascii=False)
    assert "缩略" not in json.dumps(out, ensure_ascii=False)


@pytest.mark.parametrize("raw,code", [("not JSON", "invalid_json"), ("```json\n{}\n```", "invalid_json"),
    ("[]", "invalid_output_shape"), ("null", "invalid_output_shape"),
    ('{"semantic_pairs":[],"semantic_pairs":[]}', "duplicate_output_field"),
    pytest.param("x" * 64001, "response_too_large", id="oversized")])
def test_invalid_json_and_large_response(inputs, raw, code):
    failed(assess(inputs, llm_call=lambda prompt, cfg: completion(raw, cfg)), code)


@pytest.mark.parametrize("finish", ["length", "content_filter", "tool_calls", "unknown", 3, True])
def test_incomplete_llm_envelope_rejected(inputs, finish):
    failed(assess(inputs, llm_call=lambda p, c: completion({}, c, finish_reason=finish)), "incomplete_response")


@pytest.mark.parametrize("updates,code", [({"succeeded": False}, "llm_failed"), ({"attempted": False}, "llm_failed"),
    ({"attempted": 1}, "invalid_llm_result"), ({"provider": "mock"}, "response_endpoint_mismatch"),
    ({"model": "another"}, "response_endpoint_mismatch"), ({"text": ""}, "empty_response")])
def test_envelope_verification(inputs, updates, code):
    failed(assess(inputs, llm_call=lambda p, c: completion({}, c, **updates)), code)


@pytest.mark.parametrize("local,exception,code", [(False, TimeoutError, "llm_timeout"), (True, TimeoutError, "predictor_timeout"),
    (False, RuntimeError, "llm_failed"), (True, RuntimeError, "predictor_failed")])
def test_failures_are_bounded_no_retry_no_raw_provider_detail(inputs, local, exception, code):
    calls = []
    def fail(*args):
        calls.append(args)
        raise exception("sensitive provider details")
    out = assess(inputs, **({"predictor": fail} if local else {"llm_call": fail}))
    failed(out, code)
    assert len(calls) == 1 and "sensitive" not in json.dumps(out)


@pytest.mark.parametrize("target,field,value,code", [
    (0, "text", None, "invalid_input"), (0, "text", "x" * 2001, "input_too_large"),
    (0, "trigger", {}, "invalid_input"), (0, "trigger", "x" * 501, "input_too_large"),
    (0, "tier", [], "invalid_tier"), (0, "permanence", True, "invalid_permanence"),
    (0, "evidence", "quote", "invalid_input"), (0, "evidence", [], "insufficient_evidence"),
    (0, "principal", None, "invalid_scope"), (0, "endpoint_fp", "", "invalid_scope"),
    (0, "project_id", "project-a ", "invalid_scope"), (1, "id", "bad/id", "invalid_id"),
    (2, "id", False, "invalid_id"), (2, "text", "x" * 4001, "input_too_large")])
def test_input_bounds(inputs, target, field, value, code):
    (inputs[target] if target == 0 else inputs[target][0])[field] = value
    failed(assess(inputs), code)


@pytest.mark.parametrize("field", ["principal", "endpoint_fp", "project_id"])
def test_scope_required_and_foreign_rejected_before_call(inputs, field):
    del inputs[0][field]
    failed(assess(inputs), "invalid_scope")


@pytest.mark.parametrize("field,value", [("principal", "other"), ("endpoint_fp", "other"), ("project_id", "other")])
def test_foreign_rules_rejected(inputs, field, value):
    inputs[1][0][field] = value
    failed(assess(inputs), "scope_mismatch")


def test_endpoint_and_evidence_partition_guards(inputs):
    original = inputs[3].model
    inputs[3].model = "other"
    failed(assess(inputs), "endpoint_mismatch")
    inputs[3].model = original
    inputs[2][0].update(principal="user-a")
    failed(assess(inputs), "invalid_scope")
    inputs[2][0].update(endpoint_fp=inputs[0]["endpoint_fp"], project_id="other")
    failed(assess(inputs), "evidence_scope_mismatch")


def test_inherited_scope_can_be_compared_but_never_grants_replacement(inputs):
    inputs[1][0]["project_id"] = ""
    request = []
    out = assess(inputs, predictor=lambda req: request.append(req) or decision(req, "disjoint"))
    assert out["semantic_pairs"][0]["relation"] == "disjoint"
    assert request[0]["existing"][0]["scope"] == "inherited_global"
    assert "replaceable_ids" not in request[0]["context"]


def test_batches_preserve_different_pairs_instead_of_severity_merge(inputs):
    old = inputs[1][0]
    inputs[1][:] = [dict(old, id=f"r{i}") for i in range(65)]
    calls = []
    def predict(req):
        calls.append(req)
        raw = decision(req, "overlap_incomparable")
        for pair in raw["semantic_pairs"]:
            if pair["old_id"] == "r64":
                pair["relation"] = "disjoint"
        return raw
    out = assess(inputs, predictor=predict)
    assert out["coverage"]["complete"] and len(out["semantic_pairs"]) == 65 and len(calls) > 1
    assert {p["old_id"] for p in out["semantic_pairs"]} == {f"r{i}" for i in range(65)}
    assert {p["relation"] for p in out["semantic_pairs"]} == {"overlap_incomparable", "disjoint"}
    inputs[1][:] = [dict(old, id=f"r{i}", text="句" * 1900) for i in range(20)]
    calls.clear()
    out = assess(inputs, predictor=predict)
    assert out["coverage"]["complete"] and len(calls) > 1 and len(out["semantic_pairs"]) == 20
    inputs[1].append(dict(inputs[1][0]))
    failed(assess(inputs), "duplicate_id")


@pytest.mark.parametrize("quote,text", [("先给结论 再给依据", "先给结论\n  再给依据"), ("prefer fastq files", "Prefer FASTQ Files")])
def test_evidence_reuses_dream_normalized_matching(inputs, quote, text):
    inputs[0]["evidence"] = [quote]
    inputs[2][0]["text"] = text
    assert assess(inputs)["error_code"] is None


def test_evidence_cannot_join_across_sources(inputs):
    inputs[0]["evidence"] = ["先给结论 再给依据"]
    inputs[2][:] = [{"id": "e1", "text": "先给结论"}, {"id": "e2", "text": "再给依据"}]
    failed(assess(inputs), "unverified_evidence")


def test_unknown_fields_are_not_serialized_and_predictor_cannot_mutate_truth(inputs):
    class Opaque:
        def __str__(self):
            raise AssertionError("unknown field inspected")
    inputs[0].update(unknown=Opaque(), context={"principal": "other"})
    before = copy.deepcopy(inputs[1])
    def predict(req):
        out = decision(req)
        req["existing"].clear()
        req["candidate"]["text"] = "mutated"
        return out
    assert assess(inputs, predictor=predict)["coverage"]["complete"]
    assert inputs[1] == before


@pytest.mark.parametrize("secret", ["Bearer synthetic-secret", "password=synthetic-password", "sk-synthetic-token-value", "ghp_" + "A" * 36])
def test_credentials_are_redacted_on_all_surfaces(inputs, secret):
    inputs[0]["text"] += " " + secret
    inputs[1][0]["provenance"]["quotes"].append(secret)
    prompts = []
    def fake(prompt, cfg):
        prompts.append(prompt)
        req = json.loads(prompt.split("事实 JSON：\n", 1)[1])
        raw = decision(req)
        raw["semantic_pairs"][0]["reason"] = secret + " 理由" * 500
        return completion(raw, cfg)
    out = assess(inputs, llm_call=fake)
    assert out["error_code"] is None and secret not in prompts[0] + json.dumps(out)
    assert len(out["semantic_pairs"][0]["reason"]) <= 480


def test_edit_references_and_revisions_are_independent_of_five_leaves(inputs):
    old = dict(inputs[1][0], text="汇报先给结论；引用必须附来源。")
    after = dict(inputs[0], id="r1", text="汇报先给依据；引用必须附来源。", revision=8)
    edits = [{"before": old, "after": [after]}]
    out = assess(inputs, edits=edits)
    check = out["edit_checks"][0]
    assert check["supported"] and check["before_revision"] == 7 and check["before_digest"]
    assert check["after_digests"]["r1"] and check["references_verified"]
    assert "仅核实引用" in check["verification_note"]
    def forged(raw):
        raw["edit_checks"][0]["after_refs"][0]["quote"] = "伪造引用"
        return raw
    failed(assess(inputs, edits=edits, transform=forged), "unverified_reference")
    failed(assess(inputs, edits=edits, transform=lambda raw: dict(raw, edit_checks=[])), "incomplete_edit_coverage")


@pytest.mark.parametrize("field", ["before_refs", "after_refs", "source_refs"])
def test_edit_quote_forgery_stays_rejected(inputs, field):
    old = dict(inputs[1][0], text="汇报先给结论；引用必须附来源。")
    after = dict(inputs[0], id="r1", text="汇报先给依据；引用必须附来源。", revision=8)
    edits = [{"before": old, "after": [after]}]
    def forged(raw):
        raw["edit_checks"][0][field][0]["quote"] = "伪造编辑引用"
        return raw
    failed(assess(inputs, edits=edits, transform=forged), "unverified_reference")


def test_default_transport_never_loads_environment(inputs, monkeypatch):
    calls = []
    def fake(prompt, cfg):
        calls.append(prompt)
        return completion(decision(json.loads(prompt.split("事实 JSON：\n", 1)[1])), cfg)
    monkeypatch.setattr(M, "call_llm", fake)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-environment-key")
    assert M.assess_relations(*inputs)["error_code"] is None
    assert len(calls) == 1 and "unrelated-environment-key" not in calls[0]
