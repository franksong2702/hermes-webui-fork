# Assistant Turn Anchor — Track B Implementation Plan

## Background

The anchor infrastructure is at `slice5-activity-scene` in upstream. The file
`static/assistant_turn_anchors.js` provides:

- `createAssistantTurnAnchorRegistry(seed)` — mutable registry wrapping an anchor seed
- `applyAssistantTurnAnchorSourceEvent(registry, event, context)` — normalize + route + dedupe
- `projectAssistantTurnAnchorActivityScene(anchor, { mode })` — project all activity events
  into `activity_rows[]` with `display_hints`, `group`, `tool.*`, `thinking.*`
- `projectAssistantTurnAnchorSettledMessageFinalAnswer(message, context)` — extract final
  answer from a settled message (already wired in `renderMessages()`)

**Nothing is wired into the live stream path yet.** The only active wiring is
settled final-answer projection in `ui.js:9101`.

The four slices below must be done in order. Each one is a prerequisite for the next.

---

## Slice 6 — Shadow Wiring

**Goal:** Wire the registry into `attachLiveStream` without touching any renderer.
The anchor accumulates events in parallel with the existing render path.
After this slice, `activity_rows` can be compared against the real DOM to validate
correctness before any render changes are made.

### Files

- `static/messages.js` only

### What to add

**At `attachLiveStream` open (after `streamId` is known):**

```js
const _anchorRegistry = (
  typeof HermesAssistantTurnAnchors !== 'undefined' &&
  typeof HermesAssistantTurnAnchors.createAssistantTurnAnchorRegistry === 'function'
) ? HermesAssistantTurnAnchors.createAssistantTurnAnchorRegistry({
  session_id: activeSid,
  stream_id: streamId,
  run_id: S.activeRunId || null,
}) : null;

function _applyToAnchor(rawEventData, sourceEventType) {
  if (!_anchorRegistry) return;
  try {
    HermesAssistantTurnAnchors.applyAssistantTurnAnchorSourceEvent(
      _anchorRegistry,
      { ...rawEventData, source_event_type: sourceEventType,
        activitySegmentSeq: _assistantSegmentSeq,      // inject closure var
        activityBurstId: _currentActivityBurstId },    // inject closure var
      { session_id: activeSid,
        stream_id: streamId,
        run_id: S.activeRunId || null }
    );
  } catch (_) {}
}
```

**In each SSE event handler** (`reasoning`, `token`, `tool`, `tool_complete`,
`interim_assistant`, `done`, `cancel`, `error`, `compressing`, `compressed`,
`approval`, `clarify`, `pending_steer_leftover`, `goal_continue`):

```js
source.addEventListener('reasoning', e => {
  const d = JSON.parse(e.data);
  _applyToAnchor(d, 'reasoning');   // ← add this line
  // ... existing handler unchanged ...
});
```

**In `done` handler, for validation only (remove before shipping render migration):**

```js
if (_anchorRegistry && typeof HermesAssistantTurnAnchors !== 'undefined') {
  const scene = HermesAssistantTurnAnchors.projectAssistantTurnAnchorActivityScene(
    _anchorRegistry.anchor, { mode: 'compact_worklog' }
  );
  console.debug('[anchor] activity_rows:', scene.activity_rows.map(r =>
    `${r.group.group_key} | ${r.kind} | ${r.display_hint} | ${r.tool?.name || r.text?.slice(0,40) || ''}`
  ));
}
```

### Key constraint

`_assistantSegmentSeq` and `_currentActivityBurstId` are closure-local variables
that the backend does NOT send in SSE payloads. They must be injected at the
`_applyToAnchor` call site — this is what makes `activity_rows[].group.group_key`
produce correct `segment:N` / `burst:N` keys instead of the fallback `event:seq`.

Without this injection, all rows collapse into one flat group and multi-cycle
worklog rendering (Scenario D) will be wrong.

### Validation criteria

After wiring, run a multi-cycle session (model calls 2+ tool groups) and check the
console output from the `done` debug log:

- Each worklog cycle should appear under a different `group_key` (`segment:1`,
  `segment:2`, etc.)
- Tool rows should show `kind=tool_started` / `kind=tool_completed`
- Prose segments between cycles should show `kind=process_prose`
- No duplicate rows (dedupe working)
- `_anchorRegistry.stats` should show `skipped_duplicate: 0` on a clean run,
  `> 0` on a reconnect run

Do not proceed to Slice 7 until these pass.

---

## Slice 7 — Compact Worklog Render Migration

**Goal:** Replace the imperative `appendThinking` / `appendLiveToolCard` calls with
an anchor-driven reconciler. The worklog DOM is now synced from `activity_rows`
on every significant event.

### Files

- `static/messages.js` — remove imperative worklog calls, add reconciler trigger
- `static/ui.js` — add `_reconcileLiveWorklogFromAnchor(turn, anchorRegistry)`

### What to build

**`_reconcileLiveWorklogFromAnchor(turn, registry)` in `ui.js`:**

```
1. Call projectAssistantTurnAnchorActivityScene(registry.anchor, {mode:'compact_worklog'})
2. Filter rows where display_hint !== 'main_prose'  →  worklog rows
3. Coalesce consecutive reasoning rows  →  one accumulated thinking text per group
4. Group rows by group_key  →  Map<group_key, row[]>
5. For each group_key (in order):
   a. ensureLiveWorklogContainer with activityKey matching group_key
   b. Within the worklog, diff current DOM vs expected rows:
      - thinking card: update text if changed
      - tool_started: appendLiveToolCard if not present; skip if present
      - tool_completed: update existing card to done state
6. Move #liveRunStatus to end
```

**In `messages.js`:** Replace direct calls to `appendThinking` / `appendLiveToolCard`
in the `reasoning`, `tool`, and `tool_complete` SSE handlers with:

```js
if (_anchorRegistry) {
  _applyToAnchor(d, 'tool');
  _reconcileLiveWorklogFromAnchor($('liveAssistantTurn'), _anchorRegistry);
} else {
  // existing imperative path as fallback
  appendLiveToolCard(tc, ...);
}
```

Keep the existing imperative path as a fallback (`_anchorRegistry` is null when
the anchor API is unavailable). Remove the fallback only after Slice 9 is stable.

### Reasoning coalescing rule

Group consecutive `reasoning` activity events by `group_key`. Within each group,
concatenate all `event.payload.text` values in `order_index` order. Pass the
concatenated string to the thinking card. This produces one card per worklog cycle,
not one card per text chunk.

### Elapsed timer

The `tool-card-live-duration` timer currently tracks `Date.now()` at the moment
the `tool` SSE event arrives. With anchor-driven render, set `data-live-started-ms`
on the tool card row using `Date.now()` when `tool_started` is first applied, then
read it in the existing `setInterval` timer. The timer logic in `_toolElapsedTimers`
remains unchanged; only the ID source changes from the 5-fallback chain to
`anchor_event.local_id`.

### UX problems solved by this slice

- Multi-cycle worklog grouping is now declarative (group_key), not inferred from DOM
- Duplicate tool cards on reconnect are blocked at the apply layer (dedupe_key)
- Tool card ID is stable (`local_id`) — elapsed timer always mounts

---

## Slice 8 — Settlement Path Migration

**Goal:** Replace the full `renderMessages()` DOM rebuild on `done` with an
in-place settlement that patches `#liveAssistantTurn` directly.
This eliminates the visible flash/jump when the stream ends.

### Files

- `static/messages.js` — modify `done` handler
- `static/ui.js` — add `_settleLiveAssistantTurnFromAnchor(turn, registry)`

### Current behavior (what we're replacing)

```
done fires
  → _finishDone()
  → renderMessages()         ← tears down all DOM, rebuilds from S.messages
  → #liveAssistantTurn removed, replaced by new static .msg-row.assistant-turn
```

### Target behavior

```
done fires
  → wait for settled message to arrive in S.messages (already happens today)
  → _settleLiveAssistantTurnFromAnchor(turn, registry):
      a. Remove data-live-* attributes, id="liveAssistantTurn"
      b. Remove .live-run-status
      c. Remove data-live-worklog-shell="1" on all worklogs  →  they become static
      d. Remove data-thinking-active, data-live-thinking from thinking cards
      e. Remove data-live-assistant="1", data-live-tid, data-interim from segments
      f. Write final prose into the last .assistant-segment .msg-body
         using anchor.content.final_answer (from projectAssistantTurnAnchorSettledMessageFinalAnswer)
  → call a lightweight renderMessages() pass only to update timestamp/token-count
    in the role header (or skip and update inline)
```

### Key constraint: settled message availability

The `done` event arrives before `/api/session` returns the settled message. The
current `_finishDone()` path already handles this with a timer/retry. The
settlement patch must also wait for `S.messages` to include the final assistant
message before writing `final_answer` into the DOM.

Use the same retry mechanism already in `_finishDone()`, not a new one.

### Fallback

If the anchor's `content.final_answer` is empty (anchor not wired, or settled
message not yet available after retry limit), fall back to the existing full
`renderMessages()` rebuild. Never silently fail and leave stale live DOM.

### UX problems solved by this slice

- **Live → Final visual continuity**: the response text, worklog cards, and
  thinking card all stay in place; only live indicators are removed
- The scroll position is not reset (current rebuild often scrolls to bottom)
- Token count and elapsed time appear in the right place without DOM teardown

---

## Slice 9 — Replay Deduplication + INFLIGHT Simplification

**Goal:** Wire the anchor into the reconnect/replay path so that replayed events
are deduplicated, and remove INFLIGHT fields that are now redundant.

### Part A: Replay deduplication

**File:** `static/messages.js` — `_replay_run_journal` / `_reattachOrRestoreAfterDeferredStreamError`

When replaying run journal events after reconnect, route each event through
`applyAssistantTurnAnchorSourceEvent` before passing to the renderer. Events
whose `dedupe_key` is already in `_anchorRegistry.event_index` are skipped.

```js
const result = HermesAssistantTurnAnchors.applyAssistantTurnAnchorSourceEvent(
  _anchorRegistry, replayEvent, context
);
if (!result.applied) return; // skip duplicate
// ... existing render call
```

This requires the registry to be created at stream-start (Slice 6) and preserved
across reconnect. On reconnect, do NOT create a new registry — reuse the existing
one so its dedupe_key_set is still populated.

If a fresh page load reconnects to an existing run (no in-memory registry), rebuild
the registry from `INFLIGHT.activityBurstAnchors` + the run journal snapshot before
replaying.

### Part B: INFLIGHT simplification

After Slice 7 and 8 are stable, the following INFLIGHT fields become redundant and
can be removed:

| Field | Replaced by |
|-------|-------------|
| `activityBurstAnchors` | `_anchorRegistry.anchor.activity_events` |
| `currentLiveSegmentSeq` | `activity_rows[].group.activity_segment_seq` |
| `currentActivityBurstId` | `activity_rows[].group.activity_burst_id` |

Keep `lastRunJournalSeq` and `toolCalls` until the replay path is fully
anchor-driven. Remove only after a full regression pass.

Do not remove INFLIGHT fields in the same PR as the deduplication wiring — keep
them separate so each can be reverted independently.

### UX problems solved by this slice

- Reconnect no longer shows duplicate tool cards
- Reconnect no longer loses the thinking card (the DOM is rebuilt from
  `activity_events`, not from a stale INFLIGHT snapshot)
- Architecture debt reduced: INFLIGHT shrinks from ~7 tracked fields to ~2

---

## Dependency Graph

```
Slice 6: Shadow wiring
  │  validate group_key correctness, dedupe stats
  ↓
Slice 7: Compact worklog render migration
  │  validate multi-cycle grouping, elapsed timers, reconnect behavior
  ↓
Slice 8: Settlement path migration
  │  validate live-to-final continuity, scroll position, fallback path
  ↓
Slice 9: Replay deduplication + INFLIGHT simplification
```

Each slice should be its own PR. Slice 6 and 7 can be opened as draft PRs
simultaneously since 7 depends on 6 being stable but not necessarily merged.
Slices 8 and 9 must wait for their predecessor to be merged and regression-tested.

---

## Risk Notes

**Slice 7 (medium risk)**
The reconciler adds a new code path for every SSE event that touches the worklog.
The imperative fallback must remain active until Slice 9 removes it. Test with:
- Single tool call
- 3+ tools in one cycle (Scenario C)
- 3 cycles with interim_assistant (Scenario D)
- Reconnect mid-stream

**Slice 8 (high risk)**
Settlement is the most complex path in the codebase. Edge cases:
- `done` arrives before settled message is in S.messages
- User switches session tab during stream
- Stream errors out instead of completing
- Multi-turn sessions with existing messages above the live turn

The full `renderMessages()` fallback is mandatory. Only remove it after 2+ weeks
of production observation.

**Slice 9 (high risk)**
INFLIGHT removal is irreversible once shipped. Remove fields one at a time, each
behind its own PR, with full reconnect regression testing between each removal.
