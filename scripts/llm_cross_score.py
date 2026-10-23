# -*- coding: utf-8 -*-
"""Cross-model LLM semantic labelling of LTR query-document pairs.

Stage-3b of the LTR data pipeline. A single LLM pseudo-label is biased by that
model's own judgement style; this script labels every candidate pair with
several heterogeneous models (served by an OpenAI-compatible gateway) and
aggregates a consensus grade. The prompt, the 0-3 anchors and the metadata
truncation are reused verbatim from ``llm_label_pairs`` so the consensus label
stays semantically comparable to the single-model one.

For every candidate the script adds:

- ``xscore``: ``{model_id: 0..3 | None}`` per-judge grades
- ``llm_semantic_single``: the previous single-model grade (preserved)
- ``llm_semantic``: overwritten with the rounded median of the non-null
  cross-model grades (the consensus label consumed by training)
- ``xscore_n`` / ``xscore_spread``: judge count and max-min disagreement

``label_model`` names only the judges that actually returned a score, so a
requested judge that failed (``None`` after bounded retries) is never claimed.

The gateway key is read from the ``OPENCODE_GO_API_KEY`` environment variable
and is never written to disk. ``--endpoint`` switches the call to any
OpenAI-compatible chat/completions deployment instead of the gateway's own
routing; with an explicit endpoint the key may come from the repository LLM
config, so no environment setup is needed.

Resume semantics: a row is published only when every requested judge scored
every candidate of that row. Scores already on disk are reused (only the gaps
are re-called), pairs whose ``xscore`` covers every requested model are
skipped, and a row with any failed judge stays **unpublished** — the next run
retries it, and the per-judge failure counts are printed. Disk scores are
backfilled into the in-memory row *before* the skip decision, so a candidate
already complete on disk never leaves its row looking incomplete after the
other candidates' gaps are filled (that used to keep the row unpublished and
re-bill the same missing judge on every run). ``load_progress`` also merges
complementary partial rows across old lines of the same query; when that
backfill already completes a row (zero calls needed) the row is still
**materialized** through the same publish path (``_row_complete`` →
``_finalise_row`` → append → ``_compact_rows``) — skipped only when the disk
already holds exactly one complete line identical to the canonical row, so a
second run is byte-stable. Nothing treats ``None`` as done, and no bookkeeping
key (``_written``) is written into the rows; rows written by an older run are
compacted away so each ``query_id`` keeps exactly one line, the latest
complete one.

Usage:
  OPENCODE_GO_API_KEY=... python scripts/llm_cross_score.py --limit 2   # smoke
  OPENCODE_GO_API_KEY=... python scripts/llm_cross_score.py             # full
  python scripts/llm_cross_score.py --endpoint <url> --models a,b --in-dir <dir> --splits <name>
"""
from __future__ import annotations

import argparse
import io
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))
sys.path.insert(0, str(AGENT_ROOT / "scripts"))

from llm_label_pairs import PROMPT, load_uid_texts, pair_key  # noqa: E402

API_URL = "https://opencode.ai/zen/go/v1/chat/completions"
MESSAGES_URL = "https://opencode.ai/zen/go/v1/messages"
#: Models served by the gateway's Anthropic-style /messages endpoint.
MESSAGES_MODELS = {"qwen3.8-flash", "minimax-m3", "minimax-m2.7", "minimax-m2.5",
                   "qwen3.8-max", "qwen3.7-max", "qwen3.7-plus", "qwen3.6-plus"}
#: Models served by the gateway's OpenAI Responses endpoint. These reject the
#: temperature parameter (fixed at 1), so scores carry sampling variance.
RESPONSES_MODELS = {"gpt-5.6-luna", "grok-4.6", "muse-spark-1.3-contributor",
                    "muse-spark-1.2-contributor"}
DEFAULT_MODELS = ["deepseek-v4-pro", "mimo-v2.5", "gpt-5.6-luna"]
DEFAULT_IN_DIR = AGENT_ROOT / "research" / "ltr_data"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

_print_lock = threading.Lock()
_session_id = str(uuid.uuid4())


RESPONSES_URL = "https://opencode.ai/zen/go/v1/responses"


def call_gateway(prompt: str, model: str, api_key: str, max_tokens: int, timeout: int = 180,
                 endpoint: str | None = None) -> str:
    """Call one model, over either the gateway's per-family routing or a raw endpoint.

    ``endpoint=None`` keeps the original behaviour: the gateway decides which of its
    three shapes (chat completions / Anthropic messages / Responses) a model needs.
    An explicit ``endpoint`` always speaks plain OpenAI-compatible
    ``chat/completions``, so the same caller works against any such deployment
    without a second code path.
    """
    if endpoint is None:
        if model in MESSAGES_MODELS:
            return _call_messages(prompt, model, api_key, max_tokens, timeout)
        if model in RESPONSES_MODELS:
            return _call_responses(prompt, model, api_key, max_tokens, timeout)
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(endpoint or API_URL, data=body, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "x-opencode-session": _session_id,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    content = data["choices"][0]["message"].get("content")
    if not content:
        raise ValueError("empty content (reasoning exhausted budget)")
    return str(content)


def resolve_api_key(env_name: str, endpoint: "str | None") -> "str | None":
    """Key resolution for a labelling run: ``--api-key-env`` first, then, when an
    explicit ``--endpoint`` was given, the repository's own LLM config.

    The fallback exists so an endpoint that is configured through the project's
    ``.env`` (the normal case for non-gateway deployments) needs no key handling
    at the call site. The key is read, used and never printed or written.
    """
    key = os.environ.get(env_name)
    if key:
        return key
    if endpoint:
        from dataset_recommender.llm.llm_client import load_llm_config
        return load_llm_config().api_key
    return None


def _call_responses(prompt: str, model: str, api_key: str, max_tokens: int, timeout: int) -> str:
    body = json.dumps({
        "model": model,
        "input": prompt,
        "max_output_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(RESPONSES_URL, data=body, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "x-opencode-session": _session_id,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    if data.get("status") not in (None, "completed"):
        raise ValueError(f"response status={data.get('status')}")
    content = "".join(
        block.get("text", "") for out in data.get("output", [])
        if isinstance(out, dict) and out.get("type") == "message"
        for block in out.get("content", [])
        if isinstance(block, dict) and block.get("type") == "output_text")
    if not content:
        raise ValueError("empty content (reasoning exhausted budget)")
    return content


def _call_messages(prompt: str, model: str, api_key: str, max_tokens: int, timeout: int) -> str:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(MESSAGES_URL, data=body, headers={
        "Authorization": f"Bearer {api_key}",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "x-opencode-session": _session_id,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    content = "".join(
        block.get("text", "") for block in data.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text")
    if not content:
        raise ValueError("empty content (reasoning exhausted budget)")
    return content


def score_one(query: str, uid: str, texts: dict, model: str,
              api_key: str, max_tokens: int, retries: int = 3,
              endpoint: "str | None" = None):
    meta = texts.get(uid) or {"title": uid, "description": ""}
    prompt = PROMPT.format(query=query, title=meta["title"], description=meta["description"])
    for attempt in range(retries + 1):
        try:
            text = call_gateway(prompt, model, api_key, max_tokens, endpoint=endpoint)
            start = text.find("{")
            payload, _ = json.JSONDecoder().raw_decode(text, start)
            score = int(payload["score"])
            if 0 <= score <= 3:
                return model, score
        except urllib.error.HTTPError as exc:
            wait = 20 * (attempt + 1) if exc.code == 429 else 1.5 * (attempt + 1)
            time.sleep(wait)
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return model, None


def aggregate(xscore: dict):
    vals = [v for v in xscore.values() if v is not None]
    if not vals:
        return None, 0, None
    median = statistics.median(vals)
    return int(round(median)), len(vals), max(vals) - min(vals)


def _is_int_score(value) -> bool:
    """有效评委分（0..3 的整数，与 score_one 的校验同界）；None/缺键/坏类型/越界都不算成功。"""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 3


def _label_model(xscore: dict, models: list) -> str:
    """label_model 只记**实际成功**的评委（顺带钉住 models 顺序，避免按完成顺序抖动）。"""
    ok = [m for m in models if _is_int_score(xscore.get(m))]
    return "xscore:" + ",".join(ok)


def load_progress(dst: Path, models: list, *, collector: "dict | None" = None) -> "tuple[set, dict, set]":
    """读取既有产物做断点续跑：(已完成 pair, 已有分数, 文件里出现过的 query_id 集合)。

    已完成 = 每个请求评委都有整数分。失败（None）不算完成，下次重跑；已有分数用于
    只补缺失评委（**不重打**已成功样本），也是"旧行是否会被新行取代"的判定依据。
    同一 query 多行（旧版分批写下的部分行）按出现顺序合并，后者覆盖前者。

    collector（可选，kw-only）：填入每个 qid 的磁盘物化状态
    ``{qid: {"rows": 行数, "complete": [完整行 dict...]}}``——供零调用路径判断
    「磁盘上是否已有唯一且相同的规范完整行」（有则跳过，保证重跑零写）。"""
    done: set = set()
    prior: "dict[tuple, dict]" = {}
    known_qids: set = set()
    if not dst.exists():
        return done, prior, known_qids
    for line in dst.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        qid = str(row.get("query_id") or "")
        if qid:
            known_qids.add(qid)
            if collector is not None:
                entry = collector.setdefault(qid, {"rows": 0, "complete": []})
                entry["rows"] += 1
                if _row_complete(row, models):
                    entry["complete"].append(row)
        for cand in row.get("candidates") or []:
            uid = cand.get("uid")
            if not uid:
                continue
            key = pair_key(row.get("query_id"), uid)
            xs = cand.get("xscore") or {}
            slot = prior.setdefault(key, {})
            for model in models:
                if _is_int_score(xs.get(model)):
                    slot[model] = xs[model]
            if all(_is_int_score(slot.get(m)) for m in models):
                done.add(key)
    return done, prior, known_qids


def _row_complete(row: dict, models: list) -> bool:
    """整行可发布：每个带 uid 的候选都被全部请求评委打出整数分。"""
    for cand in row.get("candidates") or []:
        if not cand.get("uid"):
            continue
        xs = cand.get("xscore") or {}
        if not all(_is_int_score(xs.get(m)) for m in models):
            return False
    return True


def _compact_rows(dst: Path, query_ids: set) -> None:
    """把同 query 的旧行收敛掉（原子替换）：每个 query 只留最后一行。

    不收敛会让旧的部分行继续遮蔽新写出的完整行——消费者普遍按 query 取首行。
    """
    kept: list[str] = []
    seen: set = set()
    for line in reversed(dst.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            qid = str(json.loads(line).get("query_id") or "")
        except json.JSONDecodeError:
            qid = ""                       # 解析不了的行不属本次收敛范围，原样保留
        if qid and qid in query_ids:
            if qid in seen:
                continue
            seen.add(qid)
        kept.append(line)
    body = "\n".join(reversed(kept))
    tmp = dst.with_name(dst.name + ".compact.tmp")
    tmp.write_text(body + ("\n" if body else ""), encoding="utf-8", newline="\n")
    os.replace(tmp, dst)


def _report_failures(split: str, failures: "Counter", incomplete_rows: int) -> None:
    if not failures and not incomplete_rows:
        return
    detail = ", ".join(f"{model}:{count}" for model, count in sorted(failures.items())) or "-"
    with _print_lock:
        print(f"  [{split}] 评委调用失败 {detail}；不完整行 {incomplete_rows}（不落盘，下次重跑）",
              flush=True)


def _finalise_row(row: dict, models: list) -> None:
    """写出行前的派生字段：共识分/评委数/分歧度/只含成功评委的 label_model。"""
    for cand in row.get("candidates") or []:
        if not cand.get("uid"):
            continue
        if "llm_semantic_single" not in cand:
            cand["llm_semantic_single"] = cand.get("llm_semantic")
        consensus, n_judges, spread = aggregate(cand.get("xscore") or {})
        cand["llm_semantic"] = consensus
        cand["xscore_n"] = n_judges
        cand["xscore_spread"] = spread
        cand["label_model"] = _label_model(cand.get("xscore") or {}, models)


def _pending_for_row(row: dict, done: set, prior: dict, models: list):
    """返回 [(cand, 缺失评委列表)]：**先回填 prior，再判 skip**，沿用已有分数只补缺口。

    回填必须先于 done 判断：一个 pair 在盘上完整（done）时也要把它的分数带进内存行，
    否则 `_row_complete` 看到的该 candidate 仍是源行里的空 xscore——同行另一 candidate
    补齐后整行仍判不完整、永不发布，缺失评委每次重跑都被重打（付费循环）。
    """
    todo = []
    for cand in row.get("candidates") or []:
        if not cand.get("uid"):
            continue
        key = pair_key(row.get("query_id"), cand.get("uid"))
        # 盘上已有分数是新近一次调用的结果：优先于源行里可能残留的旧值。
        merged = {**(cand.get("xscore") or {}), **prior.get(key, {})}
        cand["xscore"] = merged
        if key in done:
            continue
        missing = [m for m in models if not _is_int_score(merged.get(m))]
        if missing:
            todo.append((cand, missing))
    return todo


def _publish_row(row: dict, models: list, out, written_qids: set) -> bool:
    """唯一发布规范化机制（两引擎共用）：不完整不发布；完整行 finalise 后落盘并记账 qid。

    调用路径与零调用物化路径都走这里，保证「None 永不发布」与「落盘行形状唯一」。"""
    if not _row_complete(row, models):
        return False
    _finalise_row(row, models)
    out.write(json.dumps(row, ensure_ascii=False) + "\n")
    out.flush()
    written_qids.add(str(row.get("query_id") or ""))
    return True


def _row_already_materialized(row: dict, entry: "dict | None", models: list) -> bool:
    """磁盘上已有该 qid 的**唯一且与本轮规范行逐字段相同**的完整行 → 跳过（零写幂等）。

    row 必须先经 `_pending_for_row` 回填 prior；比较在 finalise 后的规范形态上做
    （JSON 归一化深拷贝，不改原行）。旧的部分行/陈旧行不算物化——走发布并收敛。"""
    if not entry or entry["rows"] != 1 or len(entry["complete"]) != 1:
        return False
    probe = json.loads(json.dumps(row, ensure_ascii=False))
    _finalise_row(probe, models)
    return entry["complete"][0] == probe


def _query_of_row(row: dict) -> str:
    query = row.get("query_nl") if row.get("rewrite_status") == "ok" else row["query"]
    return query or row["query"]


def run_split(split: str, in_dir: Path, texts: dict, args, api_key: str) -> dict:
    src = in_dir / f"synth_{split}_vec.jsonl"
    dst = in_dir / f"synth_{split}_xscore.jsonl"
    disk_state: dict = {}
    done, prior, known_qids = load_progress(dst, args.models, collector=disk_state)
    rows = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        rows = rows[: args.limit]
    print(f"[{split}] rows={len(rows)} already_scored={len(done)} models={args.models}", flush=True)
    t0 = time.time()
    written_qids: set = set()
    incomplete_rows = 0
    failures: Counter = Counter()
    with dst.open("a", encoding="utf-8", newline="\n") as out:
        for i, row in enumerate(rows, 1):
            todo = _pending_for_row(row, done, prior, args.models)
            if not todo:
                # 零调用也要经同一发布机制物化规范完整行：互补的旧 partial 行（跨旧行
                # 补全 prior 后已无缺口）否则永远拼不出一条完整共识行。
                if not _row_already_materialized(
                        row, disk_state.get(str(row.get("query_id") or "")), args.models):
                    _publish_row(row, args.models, out, written_qids)
                continue
            query = _query_of_row(row)
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                futures = {}
                for cand, missing in todo:
                    for model in missing:
                        fut = pool.submit(score_one, query, cand["uid"], texts, model,
                                          api_key, args.max_tokens, endpoint=args.endpoint)
                        futures[fut] = (cand, model)
                for fut in as_completed(futures):
                    cand, model = futures[fut]
                    _, score = fut.result()
                    cand.setdefault("xscore", {})[model] = score
                    if not _is_int_score(score):
                        failures[model] += 1
            if not _publish_row(row, args.models, out, written_qids):
                # 不完整行不落盘：否则 None 会被下次 resume 当"已完成"永久跳过。
                incomplete_rows += 1
                continue
            if i % 10 == 0 or i == len(rows):
                rate = i / max(1e-9, time.time() - t0)
                with _print_lock:
                    print(f"  [{split}] {i}/{len(rows)} queries "
                          f"({rate:.2f} q/s, elapsed {time.time()-t0:.0f}s)", flush=True)
    _report_failures(split, failures, incomplete_rows)
    if written_qids & known_qids:
        _compact_rows(dst, written_qids)
    return {"written": len(written_qids), "incomplete_rows": incomplete_rows,
            "failures": dict(failures)}


def run_split_pipelined(split: str, in_dir: Path, texts: dict, args, api_key: str) -> dict:
    """Pipelined engine: one global task pool across all rows (no per-row
    barrier), so workers stay idle-free behind a row's slowest judge. A row is
    written as soon as every requested judge scored it; rows with any failed
    judge stay unpublished and are retried by the next run (explicit failure
    state, never a silent ``None``-as-done). Rows already completed by the
    disk backfill (zero calls) are materialized through the same publish path
    before the pool starts. No bookkeeping key is written into the rows: the
    double-write guard is in-memory only."""
    src = in_dir / f"synth_{split}_vec.jsonl"
    dst = in_dir / f"synth_{split}_xscore.jsonl"
    disk_state: dict = {}
    done, prior, known_qids = load_progress(dst, args.models, collector=disk_state)
    rows = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        rows = rows[: args.limit]
    pending = []
    materialize = []
    for row in rows:
        todo = _pending_for_row(row, done, prior, args.models)
        if todo:
            pending.append((row, todo))
        elif not _row_already_materialized(
                row, disk_state.get(str(row.get("query_id") or "")), args.models):
            # 零调用物化：互补旧 partial 行补全后，规范完整行尚未落盘
            materialize.append(row)
    total_calls = sum(len(missing) for _, todo in pending for _, missing in todo)
    print(f"[{split}] pipelined rows={len(pending)} calls={total_calls} "
          f"workers={args.concurrency} models={args.models}", flush=True)
    t0 = time.time()
    write_lock = threading.Lock()
    finished_rows = 0
    written_qids: set = set()
    failures: Counter = Counter()
    with dst.open("a", encoding="utf-8", newline="\n") as out:
        for row in materialize:                     # 线程池启动前的单线程段，无需 write_lock
            _publish_row(row, args.models, out, written_qids)
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {}
            for row, todo in pending:
                query = _query_of_row(row)
                for cand, missing in todo:
                    for model in missing:
                        fut = pool.submit(score_one, query, cand["uid"], texts, model,
                                          api_key, args.max_tokens, endpoint=args.endpoint)
                        futures[fut] = (row, cand, model)
            for fut in as_completed(futures):
                row, cand, model = futures[fut]
                _, score = fut.result()
                cand.setdefault("xscore", {})[model] = score
                if not _is_int_score(score):
                    failures[model] += 1
                if _row_complete(row, args.models):
                    with write_lock:
                        qid = str(row.get("query_id") or "")
                        if qid in written_qids:
                            continue
                        if not _publish_row(row, args.models, out, written_qids):
                            continue
                        finished_rows += 1
                        if finished_rows % 25 == 0:
                            rate = finished_rows / max(1e-9, time.time() - t0)
                            eta = (len(pending) - finished_rows) / max(1e-9, rate) / 3600
                            print(f"  [{split}] {finished_rows}/{len(pending)} rows "
                                  f"({rate:.2f} rows/s, eta {eta:.1f}h)", flush=True)
    incomplete_rows = len(pending) - finished_rows
    _report_failures(split, failures, incomplete_rows)
    if written_qids & known_qids:
        _compact_rows(dst, written_qids)
    print(f"[{split}] pipelined done: {finished_rows} rows in {time.time()-t0:.0f}s", flush=True)
    return {"written": len(written_qids), "incomplete_rows": incomplete_rows,
            "failures": dict(failures)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=lambda s: [m.strip() for m in s.split(",") if m.strip()],
                        default=DEFAULT_MODELS)
    parser.add_argument("--splits", type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                        default=["train", "heldout"])
    parser.add_argument("--in-dir", type=Path, default=DEFAULT_IN_DIR)
    parser.add_argument("--limit", type=int, default=0, help="max queries per split (smoke)")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--engine", choices=("rowwise", "pipelined"), default="pipelined")
    parser.add_argument("--endpoint", default=None,
                        help="OpenAI-compatible chat/completions endpoint "
                             "(default: the gateway, which routes by model family)")
    parser.add_argument("--api-key-env", default="OPENCODE_GO_API_KEY",
                        help="environment variable holding the key")
    args = parser.parse_args()
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    api_key = resolve_api_key(args.api_key_env, args.endpoint)
    if not api_key:
        print(f"{args.api_key_env} is not set; aborting", file=sys.stderr)
        return 2
    texts = load_uid_texts()
    runner = run_split_pipelined if args.engine == "pipelined" else run_split
    for split in args.splits:
        runner(split, args.in_dir, texts, args, api_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
