# Conversation projects

A conversation project groups related conversations and defines a learning scope.
It is separate from a dataset research tracker, which stores research goals and
candidate decisions.

Create a project from the navigation panel. History is grouped by project. A
conversation's context menu moves it to a project or removes its membership.
Projects have editable names and colors. The compact navigation uses large color
dots; unclassified conversations use a fixed gray dot. Hovering, focusing or
clicking a dot opens a project card with its conversations and a button to move
the current conversation. Conversation entries reuse the history replay view.
The current conversation's project has a black ring; the project whose card is
open has a gray ring. When both states coincide, the black ring takes precedence.
Click the project name to switch projects, or use the edit button to change its
name and color. Preset colors, a color picker and hexadecimal colors are supported.
Conversation previews share the history row component, including opening in a
new tab, rerunning a search and two-step deletion. Drag project entries to reorder
them, or use Alt+Up/Down while focusing an entry. Order is account-owned and survives
reloads. Unclassified conversations remain below named projects; the create action
is last. Help stays with the primary navigation entries.
Renaming preserves project identity. Archiving retains conversations, membership
and learning evidence; restoring the project restores its group.

The memory panel selects either global learning or a project. Global memories and
approved examples remain available in projects; project-specific evidence is used
only within that project. Project evidence takes precedence among relevant
matches. Current user instructions and deterministic search constraints remain
authoritative. Memory suggestions still require a click and never submit a search.

Project metadata and conversation membership are account-owned server records.
Local memories and existing history snapshots remain in the browser. Explicit
memory extraction sends only the selected project's conversations, or all the
account's conversations for global extraction, to the configured model. Candidates
are previewed and approved through the existing learning flow.

## Compatibility and recovery

Existing memories and evidence without a project identifier remain global.
Project keys extend the existing account namespace without rewriting global data.
Moving a conversation changes its grouping and the scope of future turns; already
approved evidence retains its original scope.

The project database uses additive SQLite tables in the runtime data directory.
Older project records receive the default teal color through a serialized additive
column upgrade. Existing names, project identifiers and membership are preserved;
older clients can continue creating and renaming projects after a code rollback.
Code rollback leaves project data intact. Installed-instance migration includes
the workspace database.

## HTTP interface

- `GET /api/conversation-projects`: owned projects, including archived projects,
  and conversation assignments.
- `POST /api/conversation-projects`: create a project with `name` and optional
  `color` (six-digit `#RRGGBB`, default `#0d9488`).
- `PATCH /api/conversation-projects/{id}`: update the name, color, or both for an
  owned project. At least one value is required. Colors are normalized to lowercase.
- `POST /api/conversation-projects/{id}/archive`: archive or restore it.
- `POST /api/conversation-projects/reorder`: submit the exact active owned
  `project_ids` in display order. Archived slots are retained for restoration;
  duplicate, foreign or stale lists are rejected.
- `PUT /api/conversations/{id}/project`: set membership; an empty `project_id`
  removes membership.

Learning endpoints accept an optional `project_id`. Empty means global.
Conversation routing accepts a project identifier and validates both ownership
and conversation membership before capturing the learning scope.

## Recoverable conversation execution

The browser submits conversations as bounded background tasks. An account-owned
request identifier provides durable replay; a repeated request returns the same
receipt rather than rerunning completed tools. Browser recovery records contain
the task identifier, project and message, never API credentials.

Refreshing reconnects to the original task and replays its progress. Completed
searches can restore their result view. Recovered action receipts are shown for
inspection; browser-side actions are not automatically repeated. If submission
lost its response, recovery first looks up the original request and does not
blindly resend it.

The runtime defaults to two execution workers, sixteen total in-flight tasks and
one task per account, leaving an execution slot available to another account. `BIODATA_RUN_WORKERS`, `BIODATA_RUN_MAX_INFLIGHT`,
`BIODATA_RUN_MAX_PER_USER` and `BIODATA_RUN_TIMEOUT_SECONDS` configure these limits.
Timeout and cancellation are cooperative: an in-progress external call keeps its
own transport timeout, and subsequent work stops at the next safe boundary.
Executed side effects remain executed. Pending receipts after a process restart
are marked interrupted and require inspection instead of automatic replay.

- `GET /api/runs/{id}` returns owned state and the saved result.
- `GET /api/runs/{id}/events?after={seq}` returns progress after the cursor.
- `POST /api/runs/resolve` looks up an owned original request identifier.
- `POST /api/runs/{id}/cancel` requests a stop at a safe boundary.

`background: true` on `/api/utterance` selects the durable transport. Legacy JSON
and SSE transports remain compatible and use the same bounded executor and durable
receipt. Synchronous waiting connections are capped separately so they cannot
occupy the entire HTTP worker pool.

The deployed application retains one HTTP worker. The durable runtime coordinates all JSON, SSE and background dialogue transports. The former in-memory helper API is retained only for embedded compatibility; production dialogue replay is owned by the durable receipt.

## Shared conversations and replay (web and MCP)

The browser and the high-level MCP agent tools read and write **one persisted
scene**: the same conversation store, the same projects, the same replay
annotations. Two front ends see the same records when they run against the same
`data_root` under the same identity; an ordinary MCP client gets the data and the
server-side scene only, never the web graph UI.

Identity is injected by the transport, never accepted as a parameter. Local stdio
without `BIODATA_MCP_ACCESS_TOKEN` runs as the machine-local identity, which is the
same scope as the web edition's local mode. Setting that variable requires a token
minted by this same instance and not yet revoked; anything else fails closed rather
than silently falling back to the local identity — and a token minted by an online
server must not be pointed at a different local library. Online (Bearer) high-level
tools use the server configuration and count against the same account quota.

Conversations are listed and read through paged cursors and mutated with an
expected revision, so an older client cannot overwrite a newer turn. Branching
requires a real frame identifier. Importing a legacy browser history entry is
explicit and idempotent: the caller previews one entry, submits it with a stable
import identifier, and the original is never modified or deleted. Import is never
automatic, and saving a conversation is not training consent.

On a large model call, chats and digests use the configured model and the same
account quota as the web edition. Without a configured model the tools degrade
honestly instead of reporting success.

- `GET /api/conversations`: paged conversation list (`offset`, `limit`,
  `include_archived`), each item carrying its project assignment.
- `GET /api/conversations/{id}`: one conversation, with `after_message` and
  `after_frame` cursors.
- `POST /api/conversations/actions`: create a conversation or import one
  previewed legacy entry.
- `POST /api/conversations/{id}/actions`: rename, archive, restore, delete, select
  a frame, or branch, each with an expected revision.
- `GET /api/conversations/{id}/replay`: read-only replay payload
  (`status`, `points`, `snapshots`).
- `POST /api/conversations/{id}/replay/{action}`: annotate, snapshot,
  annotate a snapshot, mark seen, chat or digest; the body is the strict
  `{data, provider, use_llm, …}` envelope, and chat/digest pass the same quota gate.

The matching MCP tools are `agent_chat`, `agent_conversations`, `agent_projects`,
`agent_task` and `agent_replay`. A chat returns either the complete result or a
pending run identifier to continue with `agent_task`; retrying with the same
request identifier is idempotent and never re-executes writing tools.
