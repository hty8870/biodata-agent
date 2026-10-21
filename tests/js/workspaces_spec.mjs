import assert from "node:assert/strict";
import { workspaceHistoryGroups, workspaceConversationGroups, workspaceProjectColor, workspaceColorValue,
    WORKSPACE_UNCLASSIFIED_COLOR, workspaceReorderIds, learningStorageKey, learningHistory } from "../../web/static/js/core/workspaces_core.js";

const projects = [{ id: "A", name: "Project A" }, { id: "B", name: "Project B" }];
const history = [{ convId: "c1", query: "one" }, { convId: "c2", query: "two" }, { convId: "c3", query: "three" }];
const assignments = { c1: "A", c2: "B", c3: "" };
const groups = workspaceHistoryGroups(history.map((h) => [h]), projects, assignments);
assert.deepEqual(groups.map((g) => [g.id, g.conversations.length]), [["A", 1], ["B", 1], ["", 1]]);
assert.deepEqual(learningHistory(history, "A", assignments).map((h) => h.convId), ["c1"]);
assert.equal(learningHistory(history, "", assignments).length, 3);
assert.equal(learningStorageKey("mem::u:alice", "A"), "mem::u:alice::p:A");
assert.equal(learningStorageKey("mem::u:alice", ""), "mem::u:alice");
assert.notEqual(learningStorageKey("mem::u:alice", "A"), learningStorageKey("mem::u:bob", "A"));
assert.deepEqual(workspaceHistoryGroups(history.map((h) => [h]), projects, { ...assignments, c1: "B" }).map((g) => [g.id, g.conversations.length]), [["B", 2], ["", 1]]);
assert.deepEqual(workspaceConversationGroups([{convId: "c1", query: "latest"}, {query: "legacy"}, {convId: "c1", query: "oldest"}])
    .map((rows) => rows.map((row) => row.query)), [["latest", "oldest"], ["legacy"]]);
assert.equal(workspaceProjectColor(null), WORKSPACE_UNCLASSIFIED_COLOR);
assert.equal(workspaceProjectColor({color: "#8B5CF6"}), "#8b5cf6");
assert.equal(workspaceProjectColor({color: "#fff;display:none"}), "#0d9488");
assert.equal(workspaceColorValue("url(example)"), "#0d9488");
const order=["A","B","C"];
assert.deepEqual(workspaceReorderIds(order,"C","A"),["C","A","B"]);
assert.deepEqual(workspaceReorderIds(order,"A","B",true),["B","A","C"]);
assert.deepEqual(workspaceReorderIds(order,"missing","A"),order);
assert.deepEqual(order,["A","B","C"]);
console.log("WORKSPACES_SPEC_OK");
