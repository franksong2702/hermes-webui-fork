# Assistant Turn Anchor — Track B Implementation Plan

## Background

The anchor infrastructure is at `slice5-activity-scene` in upstream. The file
`static/assistant_turn_anchors.js` provides:

- `createAssistantTurnAnchorRegistry(seed)` — mutable registry wrapping an anchor seed
- `applyAssistantTurnAnchorSourceEvent(registry, event, context)` — normalize + route + dedupe
- `projectAssistantTurnAnchorActivityScene(anchor, { mode })` — project all activity events
  into `activity_rows[]` with `display_hints`, `group`, `tool.*`, `thinking.*`
- `projectAssistantTurnAnchorSettledMessageFinalAnswer(message, context)` — extract final
  answer from a settled message (already wired in `renderMessages()` as of slice 4/5)

**Nothing is wired into the live stream path yet.** The only active wiring is
settled final-answer projection in `ui.js`.

The five slices below must be done in order. Each one is a prerequisite for the next.

---

## Slice 6 — Live Anchor Shadow Feed

**Goal:** Wire the registry into `attachLiveStream` without touching any renderer.
The anchor accumulates events in parallel with the existing render path. No
visual change. This is the prerequisite for all subsequent slices.

### Files

Primarily `static/messages.js`. May also touch `static/assistant_turn_anchors.js`
for diagnostic helpers, and `tests/` for unit coverage of event shape and grouping.
Do not write file scope in stone — follow the code.

### What to add

**At `attachLiveStream` open (after `streamId` and `runId` are known):**

```js
// run_id is not available on S directly. Extract from the stream start response,
// or from e.lastEventId parsed in the first event. Pass null until resolved;
// _syncAnchorIdentity() inside applyAssistantTurnAnchorSourceEvent will backfill
// run_id from the first event that carries it.
const _anchorRegistry = (
  typeof HermesAssistantTurnAnchors !== 'undefined' &&
  typeof HermesAssistantTurnAnchors.createAssistantTurnAnchorRegistry === 'function'
) ? HermesAssistantTurnAnchors.createAssistantTurnAnchorRegistry({
  session_id: activeSid,
  stream_id: streamId,
  run_id: null,   // filled in by _syncAnchorIdentity on first event with run_id
}) : null;

// Store registry in a window-level Map so renderMessages() (ui.js) can find it at
// settlement. These files are NOT ES modules — the established pattern is a
// window-attached global (cf. window._carryForwardEphemeralTurnFields,
// window.HermesAssistantTurnAnchors). A bare module-level const in messages.js
// would not be reachable from ui.js.
window._liveAnchorRegistries = window._liveAnchorRegistries || new Map();
if (_anchorRegistry) window._liveAnchorRegistries.set(streamId, _anchorRegistry);

// _applyToAnchor: accepts the SSE event object e to carry e.lastEventId (replay
// cursor) as event_id into the anchor. rawEventData is d = JSON.parse(e.data).
// For done/cancel/error, pass a slimmed payload — never spread the full d when
// d.session is present (d.session.messages is the full transcript; storing it
// in activity_events would inflate the anchor with irrelevant data).
function _applyToAnchor(sourceEventType, rawEventData, sseEvent) {
  if (!_anchorRegistry) return;
  try {
    HermesAssistantTurnAnchors.applyAssistantTurnAnchorSourceEvent(
      _anchorRegistry,
      {
        ...rawEventData,                                // spread first
        source_event_type: sourceEventType,            // override — must come after spread
        // SSE transport cursor (run_id:seq). Fall back to any event_id already on
        // the raw payload — do NOT clobber an existing id with null when
        // lastEventId is absent.
        event_id: (sseEvent && sseEvent.lastEventId)
          || rawEventData.event_id || rawEventData.lastEventId || null,
        activitySegmentSeq: _assistantSegmentSeq,      // inject closure var
        activityBurstId: _currentActivityBurstId,      // inject closure var
      },
      { session_id: activeSid, stream_id: streamId }
    );
  } catch (_) {}
}
```

**Spread order is critical:** `source_event_type` must come **after** `...rawEventData`
so that the explicit value overwrites any `source_event_type` / `type` / `event`
field that `rawEventData` might carry. In JS, later keys in an object literal
overwrite earlier ones.

**In each SSE event handler**, add `_applyToAnchor` before the existing logic,
passing `e` (the SSE event object) as the third argument so `e.lastEventId` is
captured. Covered events: `reasoning`, `tool`, `tool_complete`,
`interim_assistant`, `cancel`, `error`, `compressing`, `compressed`,
`approval`, `clarify`, `pending_steer_leftover`, `goal_continue`.

```js
source.addEventListener('reasoning', e => {
  const d = JSON.parse(e.data);
  _applyToAnchor('reasoning', d, e);   // ← add; existing handler unchanged below
  // ...
});
```

**Do NOT feed `token` per-event.** The `token` handler fires once per token
(`assistantText += d.text`, verified in `messages.js`). Feeding each token would
create one frozen `activity_event` per token — thousands per long response, each
with a unique `event_id` (unique seq) and therefore a unique dedupe key, so none
are collapsed. This is unbounded growth with no consumer:

- The Slice 7 compact-worklog reconciler filters out `display_hint === 'main_prose'`
  rows (which is what `process_prose` maps to), so token-derived rows are never
  rendered in the worklog.
- The final answer comes from `projectAssistantTurnAnchorSettledMessageFinalAnswer`
  (settled message), not from accumulated token events.

`interim_assistant` (the prose between worklog cycles) IS fed — it fires only at
cycle boundaries, so volume is bounded. If a future slice needs the live prose
stream as anchor activity (e.g. transparent-stream mode), feed **coalesced segment
text on segment boundaries**, never raw per-token events.

**`done` event is special — two distinct concerns, both go INSIDE `_finishDone`.**

`d.session.messages` is the full session transcript. Spreading it would store the
whole transcript inside `anchor.activity_events`. Pass only the terminal fields.

Critically, the terminal feed and the registry stamp must both run **inside the
existing `_finishDone` closure**, not at the top of the `done` handler:

- `_finishDone` may be **deferred** — on the stream-fade path the handler calls
  `_drainStreamFadeBeforeDone(_finishDone)` and returns, so `_finishDone` runs
  asynchronously after the fade. Code at the top of the handler runs before
  `S.messages` is settled.
- `_finishDone` already assigns `S.messages` (via `_carryForwardEphemeralTurnFields`,
  then `_filterRecoveryControlMessages`) and already computes the last assistant
  message into a local `lastAsst` variable. **Reuse that `lastAsst`** — do not do a
  second `find` on `d.session.messages`.

The `done` handler is `source.addEventListener('done', e => {...})`. Capture `e`
so it survives into `_finishDone` (which may run later, on the fade path):

```js
source.addEventListener('done', e => {
  // ...
  const _doneEvent = e;   // closure-captured; valid even if _finishDone is deferred
  const _finishDone = () => {
    // ... existing body, including:
    //   const lastAsst = [...S.messages].reverse().find(m => m.role === 'assistant');

    const d = _doneData;   // already in scope inside _finishDone
    _applyToAnchor('done', {
      status: d.status || 'completed',
      usage:  d.usage  || null,
      created_at: d.created_at || null,
      // do NOT spread d — d.session contains the full messages array
    }, _doneEvent);   // pass the done event so the terminal row gets event_id/run_id/seq

    // Stamp the registry lookup key onto the settled assistant message that
    // renderMessages() will read. lastAsst is already the correct S.messages object
    // reference (carry-forward mutates and returns d.session.messages in place, so
    // S.messages entries share identity with d.session.messages entries).
    if (_anchorRegistry && lastAsst) lastAsst._anchor_stream_id = streamId;
  };
});
```

Note: `_carryForwardEphemeralTurnFields` only preserves fields listed in
`_EPHEMERAL_TURN_FIELDS` across turns. `_anchor_stream_id` does not need to be in
that list — it is stamped fresh on each settlement and only needs to survive until
the immediately following `renderMessages()` call.

### Key constraints

**segment/burst injection:** `_assistantSegmentSeq` and `_currentActivityBurstId`
are closure-local variables that the backend does NOT send in SSE payloads. They
must be injected at the `_applyToAnchor` call site. Without this,
`activity_rows[].group.group_key` falls back to `event:seq` for every row,
collapsing all worklog cycles into a flat group and breaking multi-cycle scenarios.

**run_id:** There is no stable `S.activeRunId` field in the current codebase.
Do not reference it. The registry is created with `run_id: null`. It gets
backfilled automatically — see the next note: `e.lastEventId` has the form
`run_id:seq`, and the normalizer's `_eventIdRunId()` extracts run_id from it, which
`_syncAnchorIdentity()` then writes onto the anchor identity. So as long as `e` is
passed, run_id resolves itself on the first event.

**`e.lastEventId`:** This field lives on the SSE `MessageEvent` object `e`, not
in the parsed JSON `d`. It is the run-journal cursor already consumed by
`_rememberRunJournalCursor` / `_lastRunJournalSeq`, and it IS populated on live
events (not just on reconnect) — verified: every run-journal event type, including
`token`, registers `_rememberRunJournalCursor`. Its format is `run_id:seq`, so
passing it as `event_id` backfills BOTH run_id and seq into the anchor for free
(`_eventIdRunId` / `_eventIdSeq`). Always pass `e` as the third argument to
`_applyToAnchor`. Without it, the dedupe key degrades to the weakest `local:` form
and Slice 10's replay deduplication becomes unreliable.

### Diagnostic snapshot (for validation, remove before Slice 7 PR)

```js
// Inside _finishDone, after the terminal feed + stamp:
if (_anchorRegistry && window.HermesAssistantTurnAnchors) {
  const scene = window.HermesAssistantTurnAnchors
    .projectAssistantTurnAnchorActivityScene(_anchorRegistry.anchor, { mode: 'compact_worklog' });
  console.debug('[anchor] stats:', _anchorRegistry.stats);
  console.debug('[anchor] activity_rows:', scene.activity_rows.map(r =>
    `${r.group.group_key} | ${r.kind} | ${r.display_hint} | ` +
    `${r.tool?.name || r.text?.slice(0, 40) || ''}`
  ));
}
```

### Validation criteria

Run a multi-cycle session (model calls 2+ separate tool groups) and inspect the
console output:

- Each worklog cycle has a distinct `group_key` (`segment:1`, `segment:2`, etc.)
- Tool events show `kind=tool_started` / `kind=tool_completed`
- `interim_assistant` shows `kind=process_prose`
- No `token`-derived rows (tokens are not fed) — `activity_events` count stays
  proportional to tool calls + reasoning + interim notes, NOT to response length
- `run_id` on the anchor identity is populated (backfilled from `event_id`)
- `_anchorRegistry.stats.skipped_duplicate` is `0` on a clean run, `> 0` on reconnect
- No errors thrown in the `_applyToAnchor` try/catch

Do not start Slice 7 until group_key correctness is confirmed.

---

## Slice 7 — Compact Worklog Scene Reconciler, Dual-Run

**Goal:** Add an anchor-driven scene reconciler that runs **after** the existing
imperative worklog mutations. In this slice, the imperative path (`appendThinking`,
`appendLiveToolCard`) remains the primary renderer. The reconciler detects
discrepancies and corrects them. No imperative calls are removed yet.

### Files

Primarily `static/ui.js` (reconciler function). Minor additions in
`static/messages.js` (trigger calls). Do not remove existing worklog code.

### What to build

**`_reconcileLiveWorklogFromAnchor(turn, registry)` in `ui.js`:**

```
1. projectAssistantTurnAnchorActivityScene(registry.anchor, {mode:'compact_worklog'})
2. Filter rows: display_hint !== 'main_prose'   →  worklog rows only
3. Coalesce consecutive reasoning rows by group_key  →  one accumulated text per group
4. Group remaining rows by group_key  →  Map<group_key, row[]>
5. For each group in order:
   a. ensureLiveWorklogContainer with activityKey matching group_key
   b. Diff current worklog DOM vs expected rows:
      - thinking card: if text changed, update .thinking-card-body pre
      - tool_started: if card absent, appendLiveToolCard; if present, skip
      - tool_completed: if card present but still shows running, update to done state
   c. Remove any DOM rows not present in the scene (stale entries from earlier render)
6. _moveLiveRunStatusToTurnEnd()
```

**In `messages.js`**, after each existing worklog mutation, add a reconciler call:

```js
// After the existing appendLiveToolCard call in the 'tool' handler:
if (_anchorRegistry) _reconcileLiveWorklogFromAnchor($('liveAssistantTurn'), _anchorRegistry);

// After the existing thinking card update in the 'reasoning' handler:
if (_anchorRegistry) _reconcileLiveWorklogFromAnchor($('liveAssistantTurn'), _anchorRegistry);
```

The reconciler acts as a correction layer. On a clean run it should be a no-op
(imperative render already did the right thing). On a reconnect or ordering
anomaly, it fixes the DOM to match the anchor's view.

### Reasoning coalescing rule

For each `group_key`, collect all `activity_events` with `kind='reasoning'` in
`order_index` order. Concatenate their `event.payload.text` values. Pass the
result to the thinking card as the accumulated text. This produces one thinking
card per worklog cycle, not one card per reasoning chunk.

### Elapsed timer

The `tool-card-live-duration` timer reads `data-live-started-ms` from the card
row. Set this attribute when the reconciler first creates a `tool_started` card,
using `Date.now()`. On subsequent reconciler calls, skip if attribute already
present (preserves the original start time). The existing `_toolElapsedTimers`
interval logic is unchanged; only the card-creation source changes.

### UX problems corrected by this slice

- Multi-cycle worklog grouping discrepancies (reconciler enforces group_key boundaries)
- Duplicate tool cards on reconnect (apply-layer dedupe blocked the event; reconciler
  sees no duplicate row in the scene and removes any stale DOM card)
- Stale running cards that were never completed (reconciler forces done state)

### When to remove the imperative path

Not in this slice. The imperative path is removed in Slice 9 after dual-run
stability is confirmed over several weeks.

---

## Slice 8 — Settlement Continuity From Scene

**Goal:** After `renderMessages()` runs at settlement, the Worklog content must
survive intact — rebuilt from anchor `activity_rows` rather than being re-derived
from `S.messages` tool_use blocks. The full `renderMessages()` call is kept.
No DOM teardown reduction yet.

### Context: how `done` actually works

The `done` SSE event carries the updated session state directly in its payload
(`d.session.messages`). Inside `_finishDone`, messages.js assigns
`S.messages = _carryForwardEphemeralTurnFields(S.messages || [], d.session.messages || [])`
then `S.messages = _filterRecoveryControlMessages(S.messages || [])`, then later
calls `renderMessages()`. This is not an async GET — the settled messages are in
the payload. `_carryForwardEphemeralTurnFields` mutates and returns the next array
in place, so `S.messages` entries share object identity with `d.session.messages`
entries (this is why stamping `lastAsst._anchor_stream_id` in Slice 6 works).

The current problem: `renderMessages()` rebuilds the settled Worklog from the
`tool_use` / `tool_result` content blocks in `S.messages`, which loses the
live-stream grouping (which cycle each tool belonged to) and any UI state
(expanded/collapsed, elapsed durations).

**There is more than one settlement path.** Besides `done` → `_finishDone`, the
`stream_end` event settles via `_restoreSettledSession()` (network re-fetch), and
the #3018 paths also reassign `S.messages`. This slice scopes the anchor-driven
worklog rebuild to turns that have a registry stamped via the `done` path. Turns
settled through `_restoreSettledSession` (e.g. a tab that was backgrounded during
the stream) will not have the stamp and fall back to the S.messages-derived
worklog — acceptable, no regression. If those paths later need anchor continuity,
stamp them the same way, reusing whatever last-assistant variable they compute.

### What to change

**In `renderMessages()` / worklog build path in `ui.js`:**

When rendering a settled (non-live) assistant turn that has a matching anchor
registry available for the same `run_id` or `stream_id`:

1. Retrieve the registry from a module-level Map keyed by `run_id || stream_id`
2. Call `projectAssistantTurnAnchorActivityScene(registry.anchor, {mode:'compact_worklog'})`
3. Use `activity_rows` to build the settled Worklog instead of re-deriving from
   `S.messages` tool blocks:
   - Worklog cycles are the distinct `group_key` values
   - Tool cards use `tool.*` fields (name, args, result, duration, done, is_error)
   - Thinking cards use `thinking.text`
4. If no matching registry is found (e.g. page reload), fall back to the existing
   `S.messages`-derived Worklog build — no regression

**Registry retention and lookup across settlement:**

The `_anchorRegistry` is stored in a window-level Map (done in Slice 6):
`window._liveAnchorRegistries.set(streamId, _anchorRegistry)`.

The problem is that settled assistant messages in `S.messages` do not carry
`_stream_id` natively. The lookup key must be stamped onto the settled message
at settlement time. This is done in the `done` handler in Slice 6:

```js
if (lastAssistant) lastAssistant._anchor_stream_id = streamId;
```

In `renderMessages()` / worklog build path, the lookup becomes:

```js
const reg = window._liveAnchorRegistries &&
  window._liveAnchorRegistries.get(message._anchor_stream_id);
```

Without this stamp, `renderMessages()` cannot find the registry and will always
fall back to the S.messages-derived Worklog build. The stamp is ephemeral
(in-memory only, lost on page reload) — that is intentional. On a fresh page
load there is no live registry; the S.messages fallback is the correct behavior.

Entries in `window._liveAnchorRegistries` are removed when the session is unloaded
or after a retention window (e.g. 10 minutes after settlement).

### What this does NOT do

- Does not bypass `renderMessages()` — full rebuild still happens
- Does not patch the live DOM before settlement — wait for renderMessages
- Does not remove `renderMessages()` fallback for missing registry

### UX problems solved by this slice

- Worklog grouping (which tools belonged to which cycle) survives settlement
- Elapsed durations and tool result state are carried from anchor into settled cards
- No more "Worklog flattens to a single group" after done

---

## Slice 9 — In-place Settlement Optimization

**Goal:** Reduce the DOM teardown on settlement so the live turn transitions
in-place rather than being replaced. This requires Slice 8 to be stable.

This is the more aggressive settlement work that was originally planned for Slice 8
but correctly moved later because it requires `renderMessages()` to be trustworthy
as a fallback before we start bypassing it.

### What to change

**In the `done` handler**, after `S.messages` is updated from the payload:

1. Check if `#liveAssistantTurn` can be settled in-place:
   - Anchor scene matches settled message final_answer
   - Worklog rows in scene match settled S.messages tool blocks
2. If yes: apply in-place patch:
   - Remove `id="liveAssistantTurn"`, `data-live-*` attributes
   - Remove `.live-run-status`
   - Remove `data-live-worklog-shell`, `data-thinking-active`, `data-live-tid`
   - Write `content.final_answer` into the last `.assistant-segment .msg-body`
   - Run a lightweight `renderMessages({skipWorklog: true})` to update metadata only
3. If no (mismatch or anchor missing): fall back to full `renderMessages()`

The "check if in-place is safe" comparison must be conservative. Any doubt → full
rebuild. The optimization is only applied when the anchor scene and settled data
are in full agreement.

### Fallback guarantee

The full `renderMessages()` fallback must remain active indefinitely. In-place
settlement is an optimization, not a replacement. Never remove the fallback.

### UX problem solved

- Eliminates the visual flash when stream ends (text, Worklog cards, and thinking
  card stay in place; only live indicators are removed)
- Scroll position is preserved for non-pinned users

---

## Slice 10 — Replay Deduplication + INFLIGHT Field Retirement

**Goal:** Wire the anchor into the reconnect/replay path for event deduplication,
then retire INFLIGHT fields made redundant by the anchor. These are two separate
PRs within the slice.

### Part A: Replay deduplication (PR 1)

When replaying run journal events after reconnect, route each event through
`applyAssistantTurnAnchorSourceEvent` before passing to the renderer. Events
whose `dedupe_key` is already in the registry are skipped (not rendered).

The registry must survive the reconnect — **do not create a new registry on
reconnect**. Reuse the existing one so its `event_index.dedupe_key_set` remains
populated. If no in-memory registry exists (fresh page load hitting an in-progress
run), rebuild the registry from `INFLIGHT.activityBurstAnchors` + the run journal
snapshot, then replay.

Prove that rebuild-from-INFLIGHT produces correct `group_key` values before
proceeding to Part B.

### Part B: INFLIGHT field retirement (PR 2+, one field at a time)

After Part A is stable, retire redundant INFLIGHT fields one per PR:

| Field | Replaced by | Remove when |
|-------|-------------|-------------|
| `activityBurstAnchors` | `registry.anchor.activity_events` | After Part A is in production 2+ weeks |
| `currentLiveSegmentSeq` | `activity_rows[].group.activity_segment_seq` | After worklog render uses scene |
| `currentActivityBurstId` | `activity_rows[].group.activity_burst_id` | Same |

Keep `lastRunJournalSeq` and `toolCalls` until the replay path is fully
anchor-driven. Each field removal is its own PR with its own regression pass.

### UX problems solved

- Reconnect no longer shows duplicate tool cards
- Reconnect no longer loses thinking card content
- INFLIGHT shrinks from ~7 tracked fields to ~2

---

## Dependency Graph

```
Slice 6: Live Anchor Shadow Feed
  │  validate: group_key correct, dedupe stats, no errors
  ↓
Slice 7: Compact Worklog Scene Reconciler, Dual-Run
  │  validate: multi-cycle grouping, reconnect, elapsed timers
  ↓
Slice 8: Settlement Continuity From Scene
  │  validate: worklog survives settlement, fallback path works
  ↓
Slice 9: In-place Settlement Optimization
  │  validate: no visual flash, scroll preserved, fallback still works
  ↓
Slice 10: Replay Dedup + INFLIGHT Retirement (two PRs)
```

Each slice is its own PR. Slices 6 and 7 can be drafted simultaneously.
Slices 8–10 must wait for the predecessor to be merged and observed in production.

---

## Risk Summary

| Slice | Risk | Key guard |
|-------|------|-----------|
| 6 | Low — inert shadow run | `_anchorRegistry` null-guarded everywhere |
| 7 | Medium — new code path in hot render loop | Imperative path kept; reconciler is correction layer only |
| 8 | Medium — `renderMessages()` must trust anchor for Worklog | Fallback to S.messages if no registry found |
| 9 | High — bypasses full DOM rebuild | In-place only when anchor/settled data are in full agreement; full rebuild always available |
| 10A | Medium — reconnect deduplication | Registry reuse across reconnect; rebuild-from-INFLIGHT proven first |
| 10B | High — removes INFLIGHT fields | One field per PR; each independently revertable |
