"use strict";

/* ============================================================================
 * knowledge_spec.mjs —— 统一知识窗纯核「真行为」规格（node 跑）
 * ----------------------------------------------------------------------------
 * 由 tests/test_knowledge_frontend_behavior.py 经 `node <this>` 驱动；断言失败 → 非零退出。
 * 表单→operations、撤回/移动、变更可见性（失败/候选/冲突不下沉）、提交前置、
 * 来源选择载荷与真实计数——这些错了，道闸 UI 就是「发了不该发的 / 把草稿当生效」，
 * 静态门测不出来。
 * ========================================================================== */

import * as K from "../../web/static/js/panel/knowledge_core.js";

let failures = 0;
function check(name, cond, detail) {
    if (cond) { console.log(`  ok   ${name}`); }
    else { failures++; console.log(`  FAIL ${name}${detail ? "  —— " + detail : ""}`); }
}

const form = { kind: "memory", title: "Preference", text: "Conclusion first", sourceText: "I request conclusion first", projectId: "", payloadText: "", linksText: "" };
const tools = ["rank", "curate.db_status"];
check("memory default payload is an object", K.knowledgeOperationsFromForm(form).operations[0].after.payload !== null);
check("create needs sources", !K.knowledgeOperationsFromForm({ ...form, sourceText: "" }).ok);
check("invalid kind rejected", !K.knowledgeOperationsFromForm({ ...form, kind: "evil" }).ok);
for (const [key, limit] of [["title",180],["text",2000],["trigger",500]]) {
    check(`${key} limit is visible, not truncated`, !K.knowledgeOperationsFromForm({ ...form, [key]: "x".repeat(limit+1) }).ok);
}
check("temporary and unknown accepted as draft input", ["temporary","unknown"].every((permanence) => K.knowledgeOperationsFromForm({ ...form, permanence }).ok));
check("ephemeral rejected", !K.knowledgeOperationsFromForm({ ...form, permanence: "ephemeral" }).ok);
check("skill tools required and registered", !K.knowledgeOperationsFromForm({ ...form, kind:"skill" }).ok
    && !K.knowledgeOperationsFromForm({ ...form, kind:"skill", payloadText:'{"tools":["invented"]}', registeredTools:tools }).ok
    && K.knowledgeOperationsFromForm({ ...form, kind:"skill", payloadText:'{"tools":["rank"]}', registeredTools:tools }).ok);
const sample = { utterance: "show catalog", steps: [{verb:"curate.db_status",args:""}], verified_success:false };
check("manual example is not verified success", K.knowledgeOperationsFromForm({ ...form, kind:"example", payloadText:JSON.stringify(sample), registeredTools:tools }).ok
    && !K.knowledgeOperationsFromForm({ ...form, kind:"example", payloadText:JSON.stringify({...sample,verified_success:true}), registeredTools:tools }).ok);
check("example args uses actual server string contract", !K.knowledgeOperationsFromForm({ ...form, kind:"example", payloadText:JSON.stringify({...sample,steps:[{verb:"rank",args:{}}]}), registeredTools:tools }).ok);
check("JSON object and knowledge IDs only", !K.knowledgePayloadParse("null").ok && !K.knowledgeLinksParse("https://a.test/x").ok
    && !K.knowledgeLinksParse("x".repeat(129)).ok && K.knowledgeLinksParse("ke_one\nke_two").ok);
const existing = K.knowledgeEntryFromServer({id:"ke_one",revision:5,status:"active",scope_level:"user",project_id:"",kind:"skill",title:"old",text:"body",trigger:"context",tier:"implicit",permanence:"persistent",payload:{tools:["rank"]},links:["ke_two"],provenance:{type:"knowledge_change"}});
const draft = K.knowledgeDraftFromEntry(existing);
const updated = K.knowledgeOperationsFromForm({...draft,title:"new",sourceText:"rename only",projectId:"",registeredTools:tools}, existing);
check("editor seeds every semantic field", updated.ok && updated.operations[0].after.title === "new" && updated.operations[0].after.trigger === existing.trigger
    && updated.operations[0].after.payload.tools[0] === "rank" && updated.operations[0].after.links[0] === "ke_two" && updated.operations[0].after.tier === "implicit");
check("update source and revision preserved", updated.source_refs.length === 1 && updated.operations[0].expected_entry_revision === 5);
check("stale editor revision rejected without discarding draft", !K.knowledgeOperationsFromForm({...draft,entryRevision:4,sourceText:"rename"},existing).ok);
check("retract and move CAS", K.knowledgeRetractOperation(existing).expected_entry_revision === 5 && K.knowledgeMoveOperation(existing,{scope_level:"project",project_id:"p2"}).destination.project_id === "p2"
    && K.knowledgeMoveOperation(existing,{scope_level:"system",project_id:""}) === null);
const assessment = {version:"biodata-memory-relations/2",id:"a1",change_revision:2,change_fingerprint:"fp",catalog_generation:3,committable:true,complete:true,reports:[{relation:{semantic_pairs:[{relation:"overlap_incomparable",label:"相交且互不包含"}],coverage:{status:"complete",complete:true,required_ids:["ke_old"],checked_ids:["ke_old"]}}}],edit_checks:[],differences:[{op:"create"}],assessed_count:1,required_count:1};
const pending = K.knowledgeChangeFromServer({id:"c1",revision:2,fingerprint:"fp",status:"pending",assessment_id:"a1",assessment});
check("record fields normalized, not invented", pending.change_revision === 2 && pending.change_fingerprint === "fp");
check("real committable and bound versions required", K.knowledgeCommitReady(pending,{catalog_generation:3,capabilities:{assessment_version:"biodata-memory-relations/2"}}).ok
    && !K.knowledgeCommitReady({...pending,assessment:{...assessment,committable:false}},{catalog_generation:3,capabilities:{assessment_version:"biodata-memory-relations/2"}}).ok
    && !K.knowledgeCommitReady(pending,{catalog_generation:4}).ok);
for(const status of ["stale","committed","rejected","draft"]) check(`${status} cannot commit`, !K.knowledgeCommitReady({...pending,status},{catalog_generation:3,capabilities:{assessment_version:"biodata-memory-relations/2"}}).ok);
check("null/string/bool catalog cannot commit", [null,"3",true].every((catalog_generation)=>!K.knowledgeCommitReady(pending,{catalog_generation}).ok));
check("reports and stale failures visible", K.knowledgeChangesVisibility([pending,{status:"draft",assessment:{error_code:"disabled"}},{status:"stale"}]).conflicts===1
    && K.knowledgeChangesVisibility([{status:"draft",assessment:{error_code:"disabled"}}]).failed===1);
check("trace shows human review and real counts", K.knowledgeTraceSummary(assessment).includes("不是自动批准") && K.knowledgeTraceSummary(assessment).includes("1/1"));
check("missing permanence stays unknown in form and server projections", K.knowledgeOperationsFromForm(form).operations[0].after.permanence === "unknown"
    && K.knowledgeDraftFromEntry(null).permanence === "unknown" && K.knowledgeEntryFromServer({}).permanence === "unknown");
check("old pending contract is never committable", !K.knowledgeCommitReady({...pending,assessment:{...assessment,version:undefined}},
    {catalog_generation:3,capabilities:{assessment_version:"biodata-memory-relations/2"}}).ok);
check("pair coverage and no-old coverage have honest Chinese text", K.knowledgeRelationSummary(assessment.reports[0].relation).includes("1/1 对")
    && K.knowledgeRelationSummary({coverage:{status:"no_existing",complete:true,required_ids:[],checked_ids:[]}}).includes("没有旧项"));
check("full body exposes permanence and payload", K.knowledgeBodyText(existing).includes("长期保留") && K.knowledgeBodyText(existing).includes("rank"));
check("future scores cannot approve", !K.knowledgeCommitReady({...pending,assessment:{...assessment,committable:false,score:1}},
    {catalog_generation:3,capabilities:{assessment_version:"biodata-memory-relations/2"}}).ok);
const selection = K.knowledgeSelectionPayload({projectId:"target",conversations:[{project_id:"p1",conversation_id:"v1"},{project_id:"p2",conversation_id:"v2"}],sourceIds:["s1"],manual:true});
check("selection is a list of original project groups", selection.selection.length===2 && selection.selection[0].project_id==="p1" && selection.project_id==="target");
check("empty selection never means select all", K.knowledgeSelectionPayload({})===null);
check("source IDs preserved", selection.source_ids[0]==="s1");
check("job.result counters and partial are honest", K.knowledgeTriggerCounts({result:{selected_count:13,handled_count:12,consumed_count:9,remaining_selected_count:1,partial:true}}).remaining_selected_count===1);
check("missing counters are unknown, not zero", K.knowledgeTriggerCounts({}).handled_count===null);
check("threshold is strict integer 1..20", [0,21,1.5,"3x","",null,true].every((x)=>K.knowledgeThresholdClamp(x)===null) && K.knowledgeThresholdClamp("3")===3);
const request = K.knowledgeDedupRequestId("中文偏好正文");
check("request ID is legal, stable, and uses complete input", /^kw-[a-f0-9]+$/.test(request) && request===K.knowledgeDedupRequestId("中文偏好正文") && request!==K.knowledgeDedupRequestId("中文偏好正文2")
    && K.knowledgeDedupRequestId("same"+"x".repeat(100)+"1")!==K.knowledgeDedupRequestId("same"+"x".repeat(100)+"2"));
if (process.argv[2]) {
    const {readFileSync} = await import("node:fs");
    const fixture = JSON.parse(readFileSync(process.argv[2],"utf8"));
    const originalStatus = fixture.status;
    check("real HTTP pending receipt is committable", K.knowledgeCommitReady(K.knowledgeChangeFromServer(fixture.changes.pending),originalStatus).ok);
    for(const key of ["stale","disabled","committed","rejected"]) check(`real HTTP ${key} cannot commit`, !K.knowledgeCommitReady(K.knowledgeChangeFromServer(fixture.changes[key]),fixture.status_final).ok);
    for(const row of fixture.entries.items) {
        const d=K.knowledgeDraftFromEntry(K.knowledgeEntryFromServer(row));
        const operation=K.knowledgeOperationsFromForm({...d,sourceText:"explicit update",projectId:row.project_id,registeredTools:fixture.status.capabilities.registered_tools},row);
        check(`real HTTP ${row.kind} retained after body`, operation.ok && JSON.stringify(operation.operations[0].after.payload)===JSON.stringify(row.payload));
    }
}

const { readFileSync } = await import("node:fs");
const { runInNewContext } = await import("node:vm");
const { webcrypto } = await import("node:crypto");
function productionFunctions(file, names, context) {
    const src = readFileSync(new URL("../../web/static/js/" + file, import.meta.url), "utf8");
    const code = names.map((name) => {
        const match = new RegExp("^(?:export )?(?:async )?function " + name + "\\(", "m").exec(src);
        if (!match) throw new Error("Missing production function " + name);
        const lineEnd = src.indexOf("\n", match.index);
        const single = src.slice(match.index,lineEnd).trimEnd().endsWith("}");
        const end = single ? lineEnd : src.indexOf("\n}",match.index)+2;
        return src.slice(match.index,end).replace(/^export /,"");
    }).join("\n");
    return runInNewContext(code+"\n({"+names.join(",")+"})",context);
}
function requestContext() {
    const context = { _requestTokens:new WeakMap(), _requestEpoch:0 };
    Object.assign(context,productionFunctions("core/core.js",["beginRequest","invalidateRequest"],context));
    return context;
}
const reads = requestContext();
const delayed=[];
Object.assign(reads,K,{owner:"fixture-user",_view:"review",_kind:"memory",_scopeLevel:"user",_status:null,_statusError:"",_entries:[],_changes:[],_selectedId:"",_readRequests:{status:{},entries:{},changes:{}},
    currentAccountScope:()=>reads.owner,currentProjectScope:()=>"",_configProvider:()=>({use_llm:true}),endpointCfg:()=>({}),
    API:{knowledgeStatus:"status",knowledgeEntries:"entries",knowledgeChanges:"changes"},
    _get:(url)=>new Promise((resolve)=>delayed.push({url,resolve})),kwStatus:()=>{},renderReviewView:()=>{},renderEntriesView:()=>{},renderError:()=>{throw new Error("unexpected read error")}});
const loaded=productionFunctions("panel/knowledge.js",["scopeKey","projectId","identity","endpointQuery","configSnapshot","readPages","loadStatus","loadEntries","loadChanges"],reads);
Object.assign(reads,loaded);
const fixtureForReads=process.argv[2]?JSON.parse(readFileSync(process.argv[2],"utf8")):null;
const trio=Promise.all([loaded.loadStatus(true),loaded.loadEntries(),loaded.loadChanges()]);
delayed.find(r=>r.url.startsWith("entries")).resolve(fixtureForReads?.entries || {ok:true,items:[existing],next_offset:null,catalog_generation:3});
delayed.find(r=>r.url.startsWith("changes")).resolve({ok:true,items:fixtureForReads?Object.values(fixtureForReads.changes):[pending],next_offset:null,catalog_generation:3});
delayed.find(r=>r.url.startsWith("status")).resolve(fixtureForReads?.status_final || {ok:true,identity:"fixture-user",catalog_generation:3});
await trio;
check("parallel status/list/review lanes all publish actual replies", reads._status?.identity==="fixture-user" && reads._entries.length>0 && reads._changes.length>0);
const before=reads._status,late=loaded.loadStatus(true);
reads.owner="other"; reads._requestEpoch+=1; reads.owner="fixture-user"; reads._requestEpoch+=1;
delayed.at(-1).resolve({ok:true,identity:"fixture-user",catalog_generation:999}); await late;
check("account round trips invalidate old status",reads._status===before);

const rank=await import("../../web/static/js/panel/memory_rank.js");
let stored=[{id:"legacy",kind:"note",text:"old text",summary:"original",useCount:1}];
let fills=0; const input={value:"",dispatchEvent:()=>fills++,focus:()=>{}};
const local={...rank,LS:{memory:"m"},learningKey:()=>"m",currentProjectScope:()=>"",readJSON:()=>JSON.parse(JSON.stringify(stored)),
    writeJSON:(_key,rows)=>{stored=JSON.parse(JSON.stringify(rows))},crypto:webcrypto,
    $:(id)=>id==="queryInput"?input:{hidden:true},toast:()=>{},Event:class{},_useMemoryArm:{id:"",until:0},renderMemorySuggestions:()=>{}};
const memories=productionFunctions("panel/memory.js",["getUserMemories","saveUserMemories","normalizedMemoryText","upsertMemory","useMemory"],local);
Object.assign(local,memories);
memories.upsertMemory("note","new text","new summary","");
const newDraft=memories.getUserMemories("").find(row=>row.text==="new text"); memories.useMemory(newDraft);
check("new local records remain draft and cannot fill a prompt",newDraft.status==="draft" && fills===0 && input.value==="");
let old=memories.getUserMemories("").find(row=>row.id==="legacy"); memories.useMemory(old);
check("grandfathered unchanged original can fill, metadata stays nonsemantic",fills===1 && stored.find(row=>row.id==="legacy").status==="legacy");
const changesLocal=memories.getUserMemories(""); changesLocal.find(row=>row.id==="legacy").text="modified old text";
memories.saveUserMemories(changesLocal,"");
check("editing preserves the original and isolates a draft version",stored.some(row=>row.id==="legacy"&&row.text==="old text"&&row.status==="legacy")
    && stored.some(row=>row.text==="modified old text"&&row.status==="draft"&&row.original_id==="legacy"));
const changed=memories.getUserMemories("").find(row=>row.text==="modified old text"); memories.useMemory(changed);
check("changed local version is not referenceable",fills===1 && input.value==="old text");
const dreamSource=readFileSync(new URL("../../web/static/js/panel/dream.js",import.meta.url),"utf8");
check("old dream network and direct write functions are gone",!dreamSource.includes("function dreamRunInner") && !dreamSource.includes("function dreamWriteAccepted") && !dreamSource.includes("upsertMemory"));

if (fixtureForReads?.learning_source_pages) {
    let renderCount=0;
    const sources=requestContext();
    Object.assign(sources,{_sourceRequests:{},_sources:[],_sourcesError:"",_sourceIdentity:"",API:{learningSources:"/api/learning/sources"},
        currentAccountScope:()=>"fixture-user",endpointCfg:()=>({base_url:"https://model.example/v1",model:"fixture-model",provider:"openai-compatible"}),
        URLSearchParams,apiErrorText:()=>"unexpected source error",$:(id)=>id==="kwLearn"?{}:null,renderLearnPanel:()=>renderCount++,
        fetch:async(url)=>({ok:true,json:async()=>fixtureForReads.learning_source_pages[Number(new URL(url,"https://example.invalid").searchParams.get("offset"))]})});
    const api=productionFunctions("panel/dream.js",["endpointIdentity","loadSources"],sources);Object.assign(sources,api);
    await api.loadSources();
    check("real sources fresh/hash/id and original projects retained across pages",sources._sources.length===3
        && sources._sources.every(r=>r.fresh===true&&r.id&&r.source_hash) && new Set(sources._sources.map(r=>r.project_id)).size===3 && renderCount===1);
}
check("continuation uses the same fixed selection, not a new subset job",dreamSource.includes("payload = s.intent.payload;")
    && dreamSource.includes("same ? s.intent.request_id") && !dreamSource.includes("request_id: globalThis.crypto.randomUUID"));

// 变更审查 commit/reject 连击单飞（真实 production 函数提取）：同一条同一动作在途时
// 再点必须拒绝且可见，不得重复 POST；在途按钮禁用、结束后恢复；不同动作互不吞回执。
{
    const reviewCtx=requestContext(),reviewCalls=[],reviewNotices=[];
    const btn=()=>({disabled:false,isConnected:true});
    Object.assign(reviewCtx,K,{_reviewRequests:new Map(),_reviewFeedback:new Map(),_status:{catalog_generation:3,capabilities:{assessment_version:"biodata-memory-relations/2"}},
        currentAccountScope:()=>"fixture-user",identity:()=>"fixture-user|scope",
        API:{knowledgeCommit:"/commit",knowledgeReject:"/reject"},
        kwStatus:(text,error)=>reviewNotices.push({text:String(text||""),error:!!error}),
        loadStatus:async()=>{},loadEntries:async()=>{},loadChanges:async()=>{},
        _post:(url,body)=>new Promise((resolve)=>reviewCalls.push({url,body,resolve}))});
    const review=productionFunctions("panel/knowledge.js",["beginReviewAction","endReviewAction","commitChange","rejectChange"],reviewCtx);
    const b1=btn(),b2=btn();
    const first=review.commitChange(pending,b1);
    await Promise.resolve();
    check("commit button disabled while in flight",b1.disabled===true);
    const dup=review.commitChange(pending,b2);
    await dup;
    check("duplicate confirm click cannot send a second commit",reviewCalls.length===1
        && reviewNotices.some((n)=>n.error&&n.text.includes("还在进行")));
    reviewCalls[0].resolve({ok:true,detail:""}); await first;
    check("first commit completes and re-enables the button",reviewCalls[0].body.change_id==="c1"
        && b1.disabled===false && reviewNotices.some((n)=>!n.error&&n.text.includes("已生效")));
    const again=review.commitChange(pending,b1);
    check("a finished commit slot can be retried deliberately",reviewCalls.length===2);
    reviewCalls[1].resolve({ok:true,detail:""}); await again;
    const r1=review.rejectChange(pending,b2),r2=review.rejectChange(pending,btn());
    await r2;
    check("reject double-click stays single-flight",reviewCalls.length===3 && reviewCalls[2].url==="/reject");
    reviewCalls[2].resolve({ok:true}); await r1;
    check("reject receipt is honest",reviewNotices.some((n)=>!n.error&&n.text.includes("已驳回（没有写进任何地方）")));
}

if (failures) {
    console.error(`KNOWLEDGE_CORE_SPEC_FAILED: ${failures} failures`);
    process.exit(1);
}
console.log("KNOWLEDGE_CORE_SPEC_OK");
