"""Regression: settled assistant answers displace stale live-turn DOM.

The live DOM is a parser target, not ownership evidence.  A settled transcript
must therefore win over stale ``#liveAssistantTurn`` content when the stream is
gone, while the #3877 parser-owned node remains attached during a real stream.
"""

import pathlib
import shutil
import subprocess
import textwrap


REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


def _preserve_guard_src():
    src = read("static/ui.js")
    i = src.find("let _preservedLiveTurn=null;")
    assert i >= 0, "_preservedLiveTurn guard not found"
    # Capture only the production preserve guard, before unrelated render work.
    j = src.find("const compressionState", i)
    assert j > i, "guard block end not found"
    return src[i:j]


def _production_helper_src():
    """Return the pure helper source, if present.

    On the pre-fix checkout no helper exists.  The runtime test still executes
    the old guard below, making RED a real stale-content behavior failure rather
    than merely a missing-symbol assertion.
    """
    src = read("static/ui.js")
    marker = "function _shouldPreserveLiveAssistantTurn"
    start = src.find(marker)
    if start < 0:
        return ""
    signature_end = src.find("){", start)
    brace = signature_end + 1 if signature_end >= 0 else -1
    assert brace > start, "shared helper opening brace not found"
    depth = 0
    end = None
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break
    assert end is not None, "shared helper body is not balanced"
    return src[start:end]


class TestBlankLiveTurnPreserveGuard:
    def test_guard_uses_authoritative_liveness_not_dom_content(self):
        guard = _preserve_guard_src()
        # Keep the test-first RED meaningful on the old production guard: the
        # runtime test below must be the failure, not merely this new symbol.
        if "_shouldPreserveLiveAssistantTurn" not in guard:
            assert "_hasRealLiveContent" in guard
            assert ".msg-body" in guard and ".tool-card-row" in guard and ".wl-reason" in guard
            return
        assert "_shouldPreserveLiveAssistantTurn" in guard
        assert "_hasRealLiveContent" not in guard
        assert ".msg-body" not in guard
        assert ".tool-card-row" not in guard
        assert ".wl-reason" not in guard
        assert "S.messages" in guard
        assert "S.activeStreamId" in guard

    def test_runtime_preserve_matrix_uses_production_guard_and_helper(self):
        node = shutil.which("node")
        if not node:
            import pytest

            pytest.skip("node not available")
        guard = _preserve_guard_src()
        helper = _production_helper_src()
        script = textwrap.dedent(
            f"""
            const assert=require('assert');
            {helper}
            function el(classes){{
              const set=new Set(classes||[]);
              return {{dataset:{{sessionId:'sid'}}, querySelector(sel){{
                return sel.split(',').map(s=>s.trim().slice(1))
                  .some(c=>set.has(c)) ? {{}} : null;
              }}}};
            }}
            function productionGuard({{sid, liveTurn, activeStreamId, messages, inflight}}){{
              const S={{activeStreamId, messages}};
              const INFLIGHT=inflight;
              const document={{getElementById(id){{
                return id==='liveAssistantTurn'?liveTurn:null;
              }}}};
              {guard}
              return _preservedLiveTurn===liveTurn;
            }}
            const staleBody=el(['msg-body']);
            const staleTool=el(['tool-card-row']);
            const staleReason=el(['wl-reason']);
            const empty=el([]);
            const inflight={{sid:{{streamId:'stream-1'}}}};
            const settled=[{{role:'assistant',content:'answer'}}];
            const live=[{{role:'assistant',_live:true,content:''}}];
            const opts=(liveTurn, activeStreamId, messages, owner=inflight)=>({{sid:'sid', liveTurn, activeStreamId, messages, inflight:owner}});
            // active stream + empty/content => preserve (#3877)
            assert.strictEqual(productionGuard(opts(empty,'stream-1',settled)), true);
            assert.strictEqual(productionGuard(opts(staleBody,'stream-1',settled)), true);
            // null active stream + explicit _live projection => preserve
            assert.strictEqual(productionGuard(opts(empty,null,live)), true);
            assert.strictEqual(productionGuard(opts(staleBody,null,live)), true);
            // settled answer + stale body/tool/reason => reject (issue #6948)
            for (const node of [staleBody, staleTool, staleReason]) {{
              assert.strictEqual(productionGuard(opts(node,null,settled)), false);
            }}
            // dead shell, wrong session, and missing INFLIGHT => reject
            assert.strictEqual(productionGuard(opts(empty,null,settled)), false);
            const wrong=el([]); wrong.dataset.sessionId='other';
            assert.strictEqual(productionGuard(opts(wrong,'stream-1',settled)), false);
            assert.strictEqual(productionGuard(opts(empty,'stream-1',settled,{{}})), false);
            console.log('OK');
            """
        )
        out = subprocess.run(
            [node, "-e", script], capture_output=True, text=True, timeout=5
        )
        assert out.returncode == 0, f"node harness failed: {out.stderr}\n{out.stdout}"
        assert "OK" in out.stdout
