"""Project learning inherits global evidence without leaking sibling projects.

统一知识道闸链口径：approve 只产生草稿提案，测试助手走完整真实链路
（提案 → 离线关系评估接缝 → 真人确认提交）后条目才生效；分区键必须与评估
配置的有效端点指纹一致（端点绑定钉见 tests/test_knowledge_gate.py）。
"""
import pytest

from dataset_recommender.agent import agent_exec, collab_rules
from dataset_recommender.agent.knowledge import knowledge_context
from dataset_recommender.agent.learning_scope import bind_project_scope, current_project_id, retain_per_partition
from dataset_recommender.app.conversation_runs import RunError
from dataset_recommender.app.conversation_store import ConversationStore
from dataset_recommender.app.knowledge_service import KnowledgeService
from dataset_recommender.app.workspace_store import WorkspaceStore
from dataset_recommender.llm.llm_client import LLMConfig

_GATE_CFG = LLMConfig(enable_llm=True, api_key='synthetic-scope-gate-key',
                      base_url='https://scope-gate.example/v1', model='scope-gate')
EP = agent_exec._endpoint_fp_from_config(_GATE_CFG)


def rule(text):
    return {"text": text, "trigger": "导出 引文", "tier": "explicit", "evidence": [text], "permanence": "persistent"}


def _known(request):
    from test_memory_relations import decision
    return decision(request, 'overlap_incomparable')


def _gate(root):
    return KnowledgeService(ConversationStore(root))


def _commit_change(root, receipt, *, principal, endpoint_fp=EP, project_id=''):
    """草稿提案走完真实统一道闸：离线关系评估接缝 + 真人确认提交。
    接受 propose 回执（change_id/change_revision）或 change 行（id/revision）。"""
    change_id = receipt.get('change_id') or receipt['id']
    change_revision = receipt.get('change_revision') or receipt['revision']
    service = _gate(root)
    assessed = service.assess(principal, endpoint_fp, project_id, change_id=change_id,
        expected_change_revision=change_revision, cfg=_GATE_CFG, predictor=_known)
    assert assessed['ok'], assessed
    committed = service.commit(principal, endpoint_fp, project_id, change_id=change_id,
        expected_change_revision=assessed['change_revision'], assessment_id=assessed['assessment_id'],
        expected_catalog_generation=assessed['catalog_generation'], human_confirmed=True)
    assert committed['ok'], committed
    return committed


def _approve(root, items, *, principal, endpoint_fp=EP, project_id=None):
    """旧「approve 即生效」语义的新链等价：提案 + 逐条评估 + 真人提交。"""
    out = collab_rules.approve_collab_rules(root, items, principal=principal,
                                            endpoint_fp=endpoint_fp, project_id=project_id)
    for receipt in out['changes']:
        if receipt.get('ok'):
            _commit_change(root, receipt, principal=principal, endpoint_fp=endpoint_fp,
                           project_id=project_id or '')
    return out


def test_project_rules_inherit_global_and_exclude_other_projects_and_accounts(tmp_path):
    workspace = WorkspaceStore(tmp_path)
    project_a = workspace.create_project('alice', '项目A')['id']
    project_b = workspace.create_project('alice', '项目B')['id']
    bob_a = workspace.create_project('bob', '项目A')['id']
    scopes = [("alice", "", "导出引文时说明全局依据"),
              ("alice", project_a, "导出引文时说明项目A依据"),
              ("alice", project_b, "导出引文时说明项目B依据"),
              ("bob", bob_a, "导出引文时说明其他账户依据")]
    for owner, project_id, text in scopes:
        _approve(tmp_path, [rule(text)], principal=owner, project_id=project_id)
    # 真实消费端 = 统一知识投影（prompt 注入唯一通道）：项目内继承全局、排除兄弟项目。
    prompt = knowledge_context(tmp_path, "导出引文", principal="alice", endpoint_fp=EP,
                               project_id=project_a, phase='decide', available_tools=())
    assert scopes[1][2] in prompt and scopes[0][2] in prompt
    assert scopes[2][2] not in prompt and scopes[3][2] not in prompt
    global_rows = collab_rules.list_collab_rules(tmp_path, principal="alice", endpoint_fp=EP, project_id="")
    assert [r["text"] for r in global_rows] == [scopes[0][2]]
    assert knowledge_context(tmp_path, "导出引文", principal="alice", endpoint_fp=_endpoint_b(),
                             project_id=project_a, phase='decide', available_tools=()) == ""


def _endpoint_b():
    return agent_exec._endpoint_fp_from_config(
        LLMConfig(enable_llm=True, api_key=None, base_url='https://scope-gate.example/v1', model='other-model'))


def test_project_cannot_revoke_global_rule(tmp_path):
    _approve(tmp_path, [rule("导出引文先说明依据")], principal="alice", project_id="")
    row = collab_rules.list_collab_rules(tmp_path, principal="alice", endpoint_fp=EP, project_id="")[0]
    with pytest.raises(RunError) as exc:
        collab_rules.revoke_collab_rules(tmp_path, [row["id"]], principal="alice",
                                         endpoint_fp=EP, project_id="project_A")
    assert exc.value.code == 'knowledge_scope_conflict', "项目层不能撤回用户层知识"
    still = collab_rules.list_collab_rules(tmp_path, principal="alice", endpoint_fp=EP, project_id="")
    assert [r["id"] for r in still] == [row["id"]] and still[0]["status"] == "active"


def test_scoped_success_candidates_and_injection(tmp_path):
    workspace = WorkspaceStore(tmp_path)
    project_a = workspace.create_project('alice', '项目A')['id']
    project_b = workspace.create_project('alice', '项目B')['id']
    steps = [{"verb": "curate.db_status", "ok": True, "slots": {}}]
    for project_id in ("", project_a, project_b):
        with bind_project_scope(project_id):
            agent_exec._maybe_record_success(tmp_path, "导入公开数据集", {}, steps,
                                             principal="alice", endpoint_fp=EP, project_id=project_id)
    candidates = agent_exec.list_example_candidates(tmp_path, principal="alice", endpoint_fp=EP,
                                                    project_id=project_a)
    assert len(candidates) == 1 and candidates[0]["project_id"] == project_a
    for project_id in ("", project_a, project_b):
        rows = agent_exec.list_example_candidates(tmp_path, principal="alice", endpoint_fp=EP,
                                                  project_id=project_id)
        assert len(rows) == 1
        out = agent_exec.approve_example_candidates(tmp_path, [r["id"] for r in rows],
                                                    principal="alice", endpoint_fp=EP,
                                                    project_id=project_id)
        assert out["approved"] == 0 and out["stage"] == "draft", "真人提交前示例不得生效"
        _commit_change(tmp_path, out["changes"][0], principal="alice", project_id=project_id)
    injected = agent_exec._load_success_examples(tmp_path, "导入公开数据集", principal="alice",
                                                 endpoint_fp=EP, project_id=project_a)
    assert {r.get("project_id", "") for r in injected} == {project_a, ""}
    # 跨账户隔离的新口径：拿别人项目编号读是 fail-closed 的作用域错误，不是「空表」。
    with pytest.raises(RunError) as exc:
        agent_exec._load_success_examples(tmp_path, "导入公开数据集", principal="bob",
                                          endpoint_fp=EP, project_id=project_a)
    assert exc.value.code == 'knowledge_project_not_found'
    bob_own = workspace.create_project('bob', 'bob的项目')['id']
    assert agent_exec._load_success_examples(tmp_path, "导入公开数据集", principal="bob",
                                             endpoint_fp=EP, project_id=bob_own) == []


def test_scope_restores_and_retention_is_per_partition():
    assert current_project_id() == ""
    with bind_project_scope("project_A"):
        assert current_project_id() == "project_A"
        with bind_project_scope("project_B"):
            assert current_project_id() == "project_B"
        assert current_project_id() == "project_A"
    assert current_project_id() == ""
    rows = [{"principal": "alice", "project_id": "A", "id": "A1"},
            {"principal": "alice", "project_id": "B", "id": "B1"},
            {"principal": "alice", "project_id": "B", "id": "B2"}]
    assert [r["id"] for r in retain_per_partition(rows, 1)] == ["A1", "B2"]
