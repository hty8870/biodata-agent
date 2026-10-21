import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

class MemoryStorage {
    values = new Map();
    get length() { return this.values.size; }
    key(i) { return Array.from(this.values.keys())[i] ?? null; }
    getItem(k) { return this.values.get(k) ?? null; }
    setItem(k, v) { this.values.set(k, v); }
    removeItem(k) { this.values.delete(k); }
}
globalThis.localStorage = new MemoryStorage();
globalThis.testOwner = "alice"; globalThis.testProject = "project_A";
globalThis.contextListeners = new Set();
const core = `export const API={utterance:"/api/utterance"},LS={pendingRuns:"pending"};
export const nsKeyFor=(base,owner)=>base+"::u:"+owner;
export const currentAccountScope=()=>globalThis.testOwner,currentProjectScope=()=>globalThis.testProject;
export const $=()=>null,toast=()=>{},registerContextChanged=(fn)=>globalThis.contextListeners.add(fn);`;
const url = (source) => "data:text/javascript;base64," + Buffer.from(source).toString("base64");
let source = readFileSync(new URL("../../web/static/js/act/conversation_client.js", import.meta.url), "utf8");
source = source.replace('"#core"', JSON.stringify(url(core)))
    .replace('"#storage_queue"', JSON.stringify(new URL("../../web/static/js/core/storage_queue.js", import.meta.url).href));
const client = await import(url(source));
const reply = (body, status=200) => ({ ok: status<400, status, json:async()=>body });

let posts=0, emitted=[];
globalThis.fetch=async(path,options)=>{
    if(path==="/api/utterance"){posts++;return reply({ok:true,run:{id:"r_test"}});}
    if(path.includes("/events"))return reply({ok:true,events:[{seq:1,event:"step",data:{complete:true}}]});
    return reply({ok:true,run:{status:"done",result:{ok:true,value:"result"}}});
};
const result=await client.startConversationRun({req_id:"req1",utterance:"test",conv_id:"c1",api_key:"synthetic-sensitive-value"},(data)=>emitted.push(data),()=>true);
assert.equal(result.result.value,"result");assert.equal(posts,1);assert.deepEqual(emitted,[{complete:true}]);
const pending=client.pendingConversationRuns();assert.equal(pending.length,1);
assert.equal(pending[0].owner,"alice");assert.equal(pending[0].project_id,"project_A");
assert(!JSON.stringify(pending).includes("api_key"));assert(!JSON.stringify(pending).includes("synthetic-sensitive-value"));
globalThis.testOwner="bob";assert.equal(client.pendingConversationRuns().length,0);
globalThis.testOwner="alice";globalThis.testProject="project_B";assert.equal(client.pendingConversationRuns().length,0);
globalThis.testProject="project_A";client.forgetConversationRun(pending[0]);

posts=0;let resolves=0;
globalThis.fetch=async(path,options)=>{
    if(path==="/api/utterance"){posts++;throw new Error("synthetic lost response");}
    if(path==="/api/runs/resolve"){resolves++;return reply({ok:true,run:{id:"r_existing"}});}
    if(path.includes("/events"))return reply({ok:true,events:[]});
    return reply({ok:true,run:{status:"done",result:{ok:true}}});
};
await assert.rejects(client.startConversationRun({req_id:"req2",utterance:"lost",conv_id:"c2"},null,()=>true),error=>error.durableRun&&error.pending);
const unknown=client.pendingConversationRuns()[0];assert.equal(unknown.run_id,"");
const restored=await client.awaitConversationRun(unknown,null,()=>true);
assert.equal(restored.row.run_id,"r_existing");assert.equal(posts,1);assert.equal(resolves,1);
client.forgetConversationRun(unknown);

// A view can change while a status/event response is being decoded. Neither
// progress nor a terminal failure from that task may be delivered to the view.
for (const stage of ["status", "events"]) {
    globalThis.testOwner="alice"; globalThis.testProject="project_A";
    const delivered=[];
    globalThis.fetch=async(path)=>{
        if(path==="/api/utterance")return reply({ok:true,run:{id:"r_late"}});
        if(path.includes("/events"))return {ok:true,status:200,json:async()=>{
            if(stage==="events")globalThis.testProject="project_B";
            return {ok:true,events:[{seq:1,event:"step",data:{late:true}}]};
        }};
        return {ok:true,status:200,json:async()=>{
            if(stage==="status")globalThis.testOwner="bob";
            return {ok:true,run:{status:"failed",result:{ok:false,detail:"old task failed"}}};
        }};
    };
    await assert.rejects(client.startConversationRun({req_id:"late_"+stage,utterance:"old",conv_id:"c_old"},
        data=>delivered.push(data),()=>true),error=>error.contextChanged);
    assert.deepEqual(delivered,[]);
    globalThis.testOwner="alice"; globalThis.testProject="project_A";
    const original=client.pendingConversationRuns()[0];
    assert.equal(original.request_id,"late_"+stage);
    client.forgetConversationRun(original);
}
globalThis.testOwner="alice"; globalThis.testProject="project_A";
const sharedPaths=[];
globalThis.fetch=async(path,options)=>{
    sharedPaths.push([path,options?.body && JSON.parse(options.body)]);
    if(path==="/api/utterance")return reply({ok:true,shared:true,async:true,run:{id:"r_shared",conversation_id:"c_shared"}});
    if(path.includes("/events"))return reply({ok:true,events:[]});
    if(path.includes("/cancel"))return reply({ok:true,run:{status:"cancelled"}});
    return reply({ok:true,run:{status:"done",result:{ok:true,shared:true,conv_id:"c_shared",revision:1}}});
};
const sr=await client.startConversationRun({shared:true,req_id:"shared-req",utterance:"test",expected_revision:0,state_patch:{sources:["one"]}},null,()=>true);
assert.equal(sr.row.conversation_id,"c_shared"); assert.equal(sr.row.shared,true);
await client.cancelConversationRun(sr.row);
assert(sharedPaths.some(([p])=>p==="/api/runs/r_shared?shared=true"));
assert(sharedPaths.some(([p])=>p==="/api/runs/r_shared/events?after=0&shared=true"));
assert(sharedPaths.some(([p])=>p==="/api/runs/r_shared/cancel?shared=true"));
const secondTab = await import(url(source + "\n// independent client instance"));
let lockQueue = Promise.resolve(), locksTaken = 0;
const lockNames = [];
const locks = { request: (name, options, callback) => {
    locksTaken++; lockNames.push(name); assert.equal(options.mode,"exclusive");
    const result = lockQueue.then(callback); lockQueue = result.catch(() => {}); return result;
} };
Object.defineProperty(globalThis, "navigator", { configurable: true, value: { locks } });
const claims = await Promise.all([client.claimConversationClientAction(sr.row,0),
    secondTab.claimConversationClientAction({...sr.row,request_id:"another-tab"},0)]);
assert.deepEqual(claims,[true,false]); assert.equal(locksTaken,2,"claims must enter an exclusive cross-tab lock");
assert.equal(lockNames[0],lockNames[1],"independent tabs serialize the same owner action ledger");
Object.defineProperty(globalThis, "navigator", { configurable: true, value: {} });
await assert.rejects(client.claimConversationClientAction(sr.row,1),e=>e.message.includes("未派发"));
Object.defineProperty(globalThis, "navigator", { configurable: true, value: { locks } });
client.forgetConversationRun(sr.row);

const calls=[];
globalThis.fetch=async(path,options)=>{
    calls.push(path);
    if(path.startsWith("/api/conversations?")) {
        const offset=Number(new URL(path,"http://example.test").searchParams.get("offset"));
        return reply({ok:true,items:[{id:"c"+offset,revision:offset,title:"server",current_state:{},updated_at:"2026-01-01"}],next_offset:offset===0?200:null});
    }
    const q=new URL(path,"http://example.test").searchParams;
    const m=Number(q.get("after_message")),f=Number(q.get("after_frame"));
    return reply({ok:true,conversation:{id:"paged",revision:3,current_state:{query:"from MCP"},
        messages:m===0?[{seq:1,text:"web"}]:[{seq:2,text:"MCP"}],frames:f===0?[{seq:1,id:"f_1"}]:[],
        next_message:m===0?1:null,next_frame:null}});
};
assert.equal((await client.listSharedConversations()).length,2);
assert.equal(client.sharedHistoryRows().length,2);
const paged=await client.getSharedConversation("paged");
assert.deepEqual(paged.messages.map((v)=>v.text),["web","MCP"]);
assert.deepEqual(paged.frames.map((v)=>v.id),["f_1"]);
assert.equal(paged.current_state.query,"from MCP");
assert(calls.some((p)=>p.includes("after_message=1&after_frame=1")));
globalThis.testOwner="bob"; assert.deepEqual(client.sharedHistoryRows(),[]);
globalThis.testOwner="alice";
globalThis.fetch=async()=>({ok:true,status:200,json:async()=>{globalThis.testOwner="bob";return {ok:true,items:[],next_offset:null};}});
await assert.rejects(client.listSharedConversations(),e=>e.contextChanged);
globalThis.testOwner="alice";
globalThis.fetch=async()=>reply({ok:false,detail:"revision moved"},409);
await assert.rejects(client.sharedConversationAction("paged","rename",3,{title:"new"}),e=>e.conflict&&e.status===409);
let pages=0;
globalThis.fetch=async()=>{
    pages++;
    const seq=pages===1?1:pages===2?2:pages===3?1:2;
    return reply({ok:true,conversation:{id:"moving",revision:pages===1?3:4,
        messages:[{seq,text:pages<=2?"obsolete":"latest"+seq}],frames:[],
        next_message:pages===1||pages===3?1:null,next_frame:null,current_state:{}}});
};
const moving=await client.getSharedConversation("moving");
assert.deepEqual(moving.messages.map((m)=>m.text),["latest1","latest2"]);
assert.equal(moving.revision,4); assert.equal(pages,4);
globalThis.fetch=async()=>reply({ok:true,items:[],next_offset:0});
await assert.rejects(client.listSharedConversations(),e=>e.message.includes("分页没有前进"));
const linked={owner:"alice",project_id:"project_A",shared:true,run_id:"mcp-active",request_id:"mcp-active",conversation_id:"mcp-conversation"};
globalThis.fetch=async()=>{throw new Error("lost status response");};
await assert.rejects(client.awaitConversationRun(linked,null,()=>true),e=>e.pending);
assert(client.pendingConversationRuns().some((r)=>r.run_id==="mcp-active"&&r.shared),"A server-owned active task is remembered before polling so refresh can reconnect");
client.forgetConversationRun(linked);
const summary=(id,revision,frame_count=1)=>({id,revision,frame_count,message_count:4,title:"zero search",current_state:{has_results:false,result_total:0},updated_at:"2026-01-01"});
let listPages=0;
globalThis.fetch=async()=>{listPages++;return reply({ok:true,items:listPages===1?[summary("reordered",2),summary("chat",1,0)]:[summary("reordered",3),summary("reordered",2),summary("next",1)],next_offset:listPages===1?200:null});};
const unique=await client.listSharedConversations();
assert.deepEqual(unique.map((c)=>[c.id,c.revision]),[["reordered",3],["chat",1],["next",1]]);
const summaries=client.sharedHistoryRows();
assert.equal(summaries.find((c)=>c.convId==="reordered").chatOnly,false,"A saved zero-hit frame is a search, not a chat-only conversation");
assert.equal(summaries.find((c)=>c.convId==="reordered").count,0);
assert.equal(summaries.find((c)=>c.convId==="chat").chatOnly,true);
for(const invalid of [NaN,"NaN",-1,1.5]) {
    let calls=0;
    globalThis.fetch=async()=>{calls++;return reply({ok:true,items:[],next_offset:invalid});};
    await assert.rejects(client.listSharedConversations(),e=>e.protocol);
    assert.equal(calls,1,"An invalid list cursor must stop before another page");
    globalThis.fetch=async()=>{calls++;return reply({ok:true,conversation:{id:"bad",revision:1,messages:[{seq:invalid,text:"bad"}],frames:[],next_message:null,next_frame:null}});};
    await assert.rejects(client.getSharedConversation("bad"),e=>e.protocol);
}
globalThis.fetch=async()=>reply({ok:true,conversation:{id:"bad",revision:1,messages:[{seq:1}],frames:[],next_message:"NaN",next_frame:null}});
await assert.rejects(client.getSharedConversation("bad"),e=>e.protocol);
const eventRow={owner:"alice",project_id:"project_A",shared:true,run_id:"bad-events",request_id:"bad-events",conversation_id:"bad"};
let invalidEventsDelivered=0;
globalThis.fetch=async(path)=>path.includes("/events")?reply({ok:true,events:[{seq:"NaN",event:"step",data:{bad:true}}]}):reply({ok:true,run:{status:"done",result:{ok:true}}});
await assert.rejects(client.awaitConversationRun(eventRow,()=>invalidEventsDelivered++,()=>true),e=>e.protocol);
assert.equal(invalidEventsDelivered,0);client.forgetConversationRun(eventRow);
for(const id of ["gone","retained"]) {
    const row={owner:"alice",project_id:"project_A",shared:true,run_id:id,request_id:id,conversation_id:id};
    globalThis.fetch=async()=>{throw new Error("pause polling");};
    await assert.rejects(client.awaitConversationRun(row,null,()=>true),e=>e.pending);
}
client.forgetSharedConversationRuns("gone");
assert.deepEqual(client.pendingConversationRuns().map((r)=>r.conversation_id),["retained"]);
client.forgetConversationRun(client.pendingConversationRuns()[0]);
globalThis.fetch=async()=>({ok:false,status:404,headers:{get:()=>"conversation_not_found"},json:async()=>({ok:false,detail:"gone"})});
await assert.rejects(client.getSharedConversation("gone"),e=>e.status===404&&e.code==="conversation_not_found");
client.initConversationClient();
let enterLock;
Object.defineProperty(globalThis,"navigator",{configurable:true,value:{locks:{request:(_name,_options,callback)=>new Promise((resolve,reject)=>{
    enterLock=()=>{try{resolve(callback());}catch(error){reject(error);}};
})}}});
const guardedRow={owner:"alice",run_id:"wait-for-lock",request_id:"wait-for-lock"};
const lockedClaim=client.claimConversationClientAction(guardedRow,0);
globalThis.contextListeners.forEach((fn)=>fn("before-account"));
globalThis.testOwner="bob";globalThis.contextListeners.forEach((fn)=>fn("account"));
globalThis.testOwner="alice";globalThis.contextListeners.forEach((fn)=>fn("account"));
enterLock(); await assert.rejects(lockedClaim,e=>e.contextChanged);
assert(!Array.from(localStorage.values.values()).some((value)=>value.includes("wait-for-lock")),"an expired account claim cannot persist a receipt while waiting for Web Locks");
for (const transition of ["conversation", "project", "request"]) {
    globalThis.testOwner = "alice"; globalThis.testProject = "project_A";
    let conversation = "A", request = "request_A", dispatches = 0;
    const row = { owner: "alice", project_id: "project_A", conversation_id: "A",
        run_id: "queued-" + transition, request_id: "request_A" };
    const current = () => conversation === "A" && request === "request_A";
    const queued = client.claimConversationClientAction(row, 0, current);
    if (transition === "conversation") conversation = "B";
    if (transition === "project") globalThis.testProject = "project_B";
    if (transition === "request") request = "request_B";
    enterLock(); await assert.rejects(queued, error => error.contextChanged);
    assert(!Array.from(localStorage.values.values()).some(value => value.includes(row.run_id)), "an undispatched action must not consume its receipt");
    conversation = "A"; request = "request_A"; globalThis.testProject = "project_A";
    const resumed = client.claimConversationClientAction(row, 0, current);
    enterLock(); if (await resumed) dispatches++;
    const repeated = client.claimConversationClientAction(row, 0, current);
    enterLock(); if (await repeated) dispatches++;
    assert.equal(dispatches, 1, "returning to A must execute the action once, not reject it forever");
}
console.log("CONVERSATION_CLIENT_SPEC_OK");
