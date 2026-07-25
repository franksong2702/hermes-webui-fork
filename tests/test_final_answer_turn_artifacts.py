import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _function_source(path, start, end):
    source = (ROOT / path).read_text(encoding="utf-8")
    return source[source.index(start) : source.index(end)]


def _run_node(script):
    result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_turn_artifact_references_require_server_landed_descriptors():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    start = workspace.index("const ARTIFACT_IGNORE_RE")
    end = workspace.index("const _turnMutatedPreviewPaths")
    output = _run_node(
        workspace[start:end]
        + "\nconsole.log(JSON.stringify(["
        + "turnArtifactReferencesFromToolCall({name:'write_file',tool_call_id:'call-1',artifacts:[{path:'output/report.md',workspace_root:'/workspace',tool_call_id:'call-1',tool_name:'write_file'},{path:'output/mismatch.md',workspace_root:'/workspace',tool_call_id:'call-1',tool_name:'read_file'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'read_file',arguments:{path:'output/report.md'}}),"
        + "turnArtifactReferencesFromToolCall({name:'write_file',artifacts:[{path:'output/missing-id.md',workspace_root:'/workspace',tool_call_id:'call-missing',tool_name:'write_file'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'write_file',is_error:true,artifacts:[{path:'output/report.md',workspace_root:'/workspace'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'write_file',output:'```diff\\n+++ output/inferred.md\\n```'}),"
        + "turnArtifactReferencesFromToolCall({name:'patch',artifacts:[{path:'output/report.md',workspace_root:'/workspace',tool_call_id:'call-2',tool_name:'patch'},{path:'output/notes.md',workspace_root:'/workspace',tool_call_id:'call-2',tool_name:'patch'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'patch',tid:'tid-1',artifacts:[{path:'output/tid.md',workspace_root:'/workspace',tool_call_id:'tid-1',tool_name:'patch'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'patch',preview:JSON.stringify({success:true,files_modified:['output/rejected.md']})}),"
        + "turnArtifactReferencesFromToolCall({name:'write_file',artifacts:[{path:['invalid'],workspace_root:123,tool_call_id:'call-3',tool_name:'write_file'}]})"
        + "]));"
    )
    assert output == [
        [
            {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-1",
                "tool_name": "write_file",
            }
        ],
        [],
        [],
        [],
        [],
        [
            {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-2",
                "tool_name": "patch",
            },
            {
                "path": "output/notes.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-2",
                "tool_name": "patch",
            },
        ],
        [
            {
                "path": "output/tid.md",
                "workspace_root": "/workspace",
                "tool_call_id": "tid-1",
                "tool_name": "patch",
            }
        ],
        [],
        [],
    ]


def test_turn_artifact_references_require_strict_identity_fields():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    start = workspace.index("function turnArtifactReferencesFromToolCall(tc){")
    end = workspace.index("const _turnMutatedPreviewPaths")
    output = _run_node(
        workspace[start:end]
        + "\nconsole.log(JSON.stringify(["
        + "turnArtifactReferencesFromToolCall({name:'write_file',tool_call_id:'call-1',session_id:'sid-owner',artifacts:[{path:'output/report.md',workspace_root:'/workspace',tool_call_id:'call-1',tool_name:'write_file'}]})"
        + ","
        + "turnArtifactReferencesFromToolCall({name:'write_file',tool_call_id:1,artifacts:[{path:'output/report.md',workspace_root:'/workspace',tool_call_id:1,tool_name:'write_file'}]})"
        + "]));"
    )
    assert output == [
        [
            {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-1",
                "tool_name": "write_file",
                "session_id": "sid-owner",
            },
        ],
        [],
    ]


def test_artifact_owner_match_requires_root_when_captured():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    start = workspace.index("function _artifactScalarString(value){")
    end = workspace.index("function _artifactCandidatesFromText", start)
    output = _run_node(
        workspace[start:end]
        + "\nconst scenario = ["
        + "{ name: 'empty-active-root', activeRoot:'', captured:'/old' },"
        + "{ name: 'matching-root', activeRoot:'/old', captured:'/old' },"
        + "{ name: 'captured-empty', activeRoot:'/old', captured:'' },"
        + "{ name: 'missing-active-root-with-captured', activeRoot:'', captured:'/old' },"
        + "];\n"
        + "console.log(JSON.stringify(scenario.map((entry) => {\n"
        + "  global.S = { session: { session_id:'sid-1', workspace: entry.activeRoot } };\n"
        + "  return _artifactOwnerMatchesSession({\n"
        + "    session_id:'sid-1',\n"
        + "    workspace_root: entry.captured,\n"
        + "  });\n"
        + "})));"
    )
    assert output == [False, True, True, False]


def test_artifact_open_aborts_stale_owner_async_sinks_and_image_error():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    helpers = workspace[workspace.index("const ARTIFACT_IGNORE_RE") : workspace.index("async function _workspacePathExists")]
    exists = workspace[workspace.index("async function _workspacePathExists") : workspace.index("// ── Workspace file-tree")]
    open_file = workspace[workspace.index("async function openFile") : workspace.index("function downloadFile")]
    output = _run_node(
        "const pending = [];\n"
        "const status = [];\n"
        "let previewMutations = 0;\n"
        "let openMutations = 0;\n"
        "let downloadMutations = 0;\n"
        "const nodes = new Proxy({}, {get: (_target, key) => {\n"
        "  if(!(key in _target)) _target[key] = {textContent:'',style:{},classList:{add(){},remove(){}},\n"
        "    appendChild(){},setAttribute(){},innerHTML:'',src:'',onerror:null};\n"
        "  return _target[key];\n"
        "}});\n"
        "const $ = (id) => nodes[id];\n"
        "const document = {createElement: () => ({style:{},classList:{add(){},remove(){}},appendChild(){},click(){},setAttribute(){}}), body:{appendChild(){},removeChild(){}}};\n"
        "const window = {};\n"
        "const S = {session:{session_id:'sid-1',workspace:'/old'}};\n"
        "const IMAGE_EXTS = new Set(['.png']); const AUDIO_EXTS = new Set(); const VIDEO_EXTS = new Set();\n"
        "const PDF_EXTS = new Set(); const MD_EXTS = new Set(); const HTML_EXTS = new Set(); const DOWNLOAD_EXTS = new Set();\n"
        "const api = () => new Promise((resolve, reject) => pending.push({resolve, reject}));\n"
        "const ensureWorkspacePreviewVisible = () => { openMutations++; };\n"
        "const switchWorkspacePanelTab = () => { openMutations++; };\n"
        "const setStatus = (value) => status.push(value); const t = (value) => value;\n"
        "const fileExt = (path) => path.slice(path.lastIndexOf('.')).toLowerCase();\n"
        "const showPreview = () => { previewMutations++; }; const renderFileBreadcrumb = () => {};\n"
        "const _workspaceRouteForPath = () => '/raw'; const _workspaceRouteForPathRel = () => '/list';\n"
        "const _workspaceEscapeGrantForPath = () => null; const _clearWorkspaceEscapeGrant = () => {};\n"
        "const showToast = () => {}; const _mediaPlayerHtml = () => '';\n"
        "const renderMarkdownPreviewContent = () => {}; const renderCodePreviewContent = () => {};\n"
        "const shouldRenderMarkdownPreviewAsPlainText = () => false; const setLargeMarkdownForceRenderVisible = () => {};\n"
        "const largeMarkdownPlainTextStatus = () => ''; let _previewServerEditable = null;\n"
        "let _previewSaveRoute = ''; let _previewOfficeFormat = ''; let _previewPreviewKind = '';\n"
        "let _previewCurrentPath = ''; let _previewRawContent = ''; let _previewRawContentPath = '';\n"
        "const _turnArtifactEntriesFromScene = () => [];\n"
        + helpers
        + exists
        + open_file
        + "async function run(){\n"
        + "  const stale = openArtifactPath('output/report.md');\n"
        + "  S.session = {session_id:'sid-1',workspace:''};\n"
        + "  pending[0].reject(new Error('switched')); await stale;\n"
        + "  S.session = {session_id:'sid-1',workspace:'/old'};\n"
        + "  const positive = openArtifactPath('output/image.png');\n"
        + "  pending[1].resolve({entries:[{path:'output/image.png'}]}); await positive;\n"
        + "  const image = nodes.previewImg;\n"
        + "  S.session = {session_id:'sid-1',workspace:''};\n"
        + "  image.onerror();\n"
        + "  console.log(JSON.stringify({status,previewMutations,openMutations,downloadMutations,imageErrorInstalled:typeof image.onerror==='function'}));\n"
        + "}\nrun().catch((error)=>{console.error(error);process.exit(1)});"
    )
    assert output == {
        "status": [],
        "previewMutations": 1,
        "openMutations": 4,
        "downloadMutations": 0,
        "imageErrorInstalled": True,
    }


def test_final_answer_artifact_entries_are_turn_owned_and_workspace_scoped():
    ui = (ROOT / "static/ui.js").read_text(encoding="utf-8")
    messages = (ROOT / "static/messages.js").read_text(encoding="utf-8")
    helpers = _function_source(
        "static/ui.js", "function _turnArtifactWorkspacePath", "function _renderTurnArtifactListForMessage"
    )
    scene = {
        "artifacts": [
            {"type":"artifact_reference","payload": {"path": "output/report.md", "workspace_root": "/workspace", "session_id":"sid-owner","tool_name":"write_file","tool_call_id":"call-1"}},
            {"type":"artifact_reference","payload": {"path": "./output/report.md", "workspace_root": "/workspace", "session_id":"sid-owner","tool_name":"write_file","tool_call_id":"call-2"}},
            {"type":"artifact_reference","payload": {"path": "output/old-workspace.md", "workspace_root": "/workspace-a"}},
            {"type":"artifact_reference","payload": {"path": "/workspace/output/absolute.md", "workspace_root": "/workspace", "session_id":"sid-owner","tool_name":"write_file","tool_call_id":"call-3"}},
            {"type":"artifact_reference","payload": {"path": "../escape.md", "workspace_root": "/workspace"}},
            {"type":"artifact_reference","payload": {"path": "output\\windows.md", "workspace_root": "/workspace"}},
            {"type":"artifact_reference","payload": {"path": "C:/outside/windows.md", "workspace_root": "/workspace"}},
            {"type":"artifact_reference","payload": {"path": "output/unbound.md"}},
            {"type":"wrong_type","payload": {"path":"output/untyped.md","workspace_root":"/workspace", "session_id":"sid-owner","tool_name":"write_file","tool_call_id":"call-4"}},
            {"type":"artifact_reference","payload": {"path":"output/invalid-type.md","workspace_root":"/workspace","session_id":22,"tool_name":"write_file","tool_call_id":"call-4"}},
            {"payload": {"path":"output/no-type.md","workspace_root":"/workspace","session_id":"sid-owner","tool_name":"write_file","tool_call_id":"call-5"}},
        ]
    }
    output = _run_node(
        "const S={session:{workspace:'/workspace',session_id:'sid-owner'}};\n"
        + helpers
        + "\nconsole.log(JSON.stringify(_turnArtifactEntriesFromScene("
        + json.dumps(scene)
        + ")));"
    )
    assert output == [{
        "path": "output/report.md",
        "workspace_root": "/workspace",
        "session_id": "sid-owner",
        "tool_name": "write_file",
        "tool_call_id": "call-1",
        "type": "artifact_reference",
        "owner": {
            "session_id": "sid-owner",
            "workspace_root": "/workspace",
        },
    }]
    assert "_attachTurnArtifactsFromToolCall(tc);" in messages
    assert "_applyToAnchor('artifact_reference'" in messages
    assert "_anchorHasArtifactReference(localId,workspaceRoot,path)" in messages
    assert "workspace_root:workspaceRoot" in messages
    assert "if(typeof _renderTurnArtifactListForMessage==='function')" in ui
    assert "_renderTurnArtifactListForMessage(msg, seg, rawIdx);" in ui
    assert "openArtifactPath(entry)" in ui
    assert "return _turnArtifactEntriesFromScene(message&&message._anchor_activity_scene);" in ui
    assert "_turn_artifacts" not in ui


def test_turn_artifact_entries_accept_top_level_session_id_fallback():
    helpers = _function_source(
        "static/ui.js", "function _turnArtifactWorkspacePath", "function _renderTurnArtifactListForMessage"
    )
    scene = {
        "artifacts": [
            {
                "type": "artifact_reference",
                "session_id": "sid-owner",
                "payload": {
                    "path": "output/backup-report.md",
                    "workspace_root": "/workspace",
                    "tool_name": "write_file",
                    "tool_call_id": "call-1",
                },
            }
        ]
    }
    output = _run_node(
        "const S={session:{workspace:'/workspace',session_id:'sid-owner'}};\n"
        + helpers
        + "\nconsole.log(JSON.stringify(_turnArtifactEntriesFromScene("
        + json.dumps(scene)
        + ")));"
    )
    assert output == [{
        "path": "output/backup-report.md",
        "workspace_root": "/workspace",
        "session_id": "sid-owner",
        "tool_name": "write_file",
        "tool_call_id": "call-1",
        "type": "artifact_reference",
        "owner": {
            "session_id": "sid-owner",
            "workspace_root": "/workspace",
        },
    }]


def test_final_answer_uses_anchor_scene_artifact_refs_without_message_history_fallback():
    helpers = _function_source(
        "static/ui.js", "function _turnArtifactWorkspacePath", "function _renderTurnArtifactListForMessage"
    )
    output = _run_node(
        "const S={session:{workspace:'/workspace',session_id:'sid-owner'},messages:[{role:'assistant',content:'final'}]};\n"
        + helpers
        + "\nconsole.log(JSON.stringify(_turnArtifactEntriesForMessage({"
        + "_anchor_activity_scene:{artifacts:[{type:'artifact_reference',payload:{path:'output/large-worklog.md',workspace_root:'/workspace',session_id:'sid-owner',tool_name:'patch',tool_call_id:'call-2'}}]}},0)));"
    )
    assert output == [{
        "path": "output/large-worklog.md",
        "workspace_root": "/workspace",
        "session_id": "sid-owner",
        "tool_name": "patch",
        "tool_call_id": "call-2",
        "type": "artifact_reference",
        "owner": {
            "session_id": "sid-owner",
            "workspace_root": "/workspace",
        },
    }]


def test_replay_merges_missing_artifact_into_existing_anchor_scene():
    from api import routes

    messages = [
        {
            "role": "assistant",
            "content": "final answer",
            "_anchor_activity_scene": {
                "version": "activity_scene_v1",
                "activity_rows": [{"type": "tool"}],
                "artifacts": [
                    {
                        "type": "artifact_reference",
                        "payload": {"path": "output/report.md", "workspace_root": "/workspace"},
                    }
                ],
            },
        }
    ]

    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
        messages,
        {
            0: [
                {
                    "path": "output/report.md",
                    "workspace_root": "/workspace",
                    "tool_call_id": "call-1",
                    "tool_name": "write_file",
                    "session_id": "sid-replay",
                },
                {
                    "path": "output/large-worklog.md",
                    "workspace_root": "/workspace",
                    "tool_call_id": "call-2",
                    "tool_name": "patch",
                    "session_id": "sid-replay",
                },
            ]
        },
    )

    scene = hydrated[0]["_anchor_activity_scene"]
    assert scene["activity_rows"] == [{"type": "tool"}]
    assert scene["artifacts"] == [
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "session_id": "sid-replay",
                "tool_call_id": "call-1",
                "tool_name": "write_file",
                "source": "transcript_replay",
            },
        },
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/large-worklog.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-2",
                "tool_name": "patch",
                "session_id": "sid-replay",
                "source": "transcript_replay",
            },
        },
    ]


def test_replay_replaces_under_typed_existing_anchor_artifacts_with_transcript_descriptors():
    from api import routes

    messages = [
        {
            "role": "assistant",
            "content": "final answer",
            "_anchor_activity_scene": {
                "version": "activity_scene_v1",
                "activity_rows": [{"type": "tool"}],
                "artifacts": [
                    {"type": "artifact_reference", "payload": {"path": "output/report.md", "workspace_root": "/workspace"}},
                    {
                        "type": "artifact_reference",
                        "payload": {
                            "path": "output/typed.md",
                            "workspace_root": "/workspace",
                            "tool_name": "patch",
                            "tool_call_id": "call-existing",
                        },
                    },
                ],
            },
        }
    ]

    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
        messages,
        {
            0: [
                {
                    "path": "output/report.md",
                    "workspace_root": "/workspace",
                    "tool_call_id": "call-1",
                    "tool_name": "write_file",
                    "session_id": "sid-replay",
                },
                {
                    "path": "output/typed.md",
                    "workspace_root": "/workspace",
                    "tool_call_id": "call-2",
                    "tool_name": "patch",
                    "session_id": "sid-replay",
                },
            ],
        },
    )

    scene = hydrated[0]["_anchor_activity_scene"]
    assert scene["artifacts"] == [
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "session_id": "sid-replay",
                "tool_call_id": "call-1",
                "tool_name": "write_file",
                "source": "transcript_replay",
            },
        },
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/typed.md",
                "workspace_root": "/workspace",
                "session_id": "sid-replay",
                "tool_name": "patch",
                "tool_call_id": "call-2",
                "source": "transcript_replay",
            },
        },
    ]


def test_replay_replaces_wrong_session_existing_anchor_artifacts_with_transcript_descriptors():
    from api import routes

    messages = [
        {
            "role": "assistant",
            "content": "final answer",
            "_anchor_activity_scene": {
                "version": "activity_scene_v1",
                "activity_rows": [{"type": "tool"}],
                "artifacts": [
                    {
                        "type": "artifact_reference",
                        "payload": {
                            "path": "output/report.md",
                            "workspace_root": "/workspace",
                            "tool_name": "patch",
                            "tool_call_id": "call-existing",
                            "session_id": "sid-old",
                        },
                    },
                ],
            },
        }
    ]

    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
        messages,
        {
            0: [
                {
                    "path": "output/report.md",
                    "workspace_root": "/workspace",
                    "tool_call_id": "call-replay",
                    "tool_name": "patch",
                    "session_id": "sid-replay",
                },
            ],
        },
    )

    scene = hydrated[0]["_anchor_activity_scene"]
    assert scene["artifacts"] == [
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_name": "patch",
                "tool_call_id": "call-replay",
                "session_id": "sid-replay",
                "source": "transcript_replay",
            },
        }
    ]


def test_replay_collision_controls_feed_real_renderer_and_click_current_owner():
    from api import routes

    descriptor = {
        "path": "output/report.md",
        "workspace_root": "/workspace",
        "tool_call_id": "call-replay",
        "tool_name": "patch",
        "session_id": "sid-replay",
    }
    incumbents = [
        {"type": "artifact_reference", "payload": {"path": descriptor["path"], "workspace_root": descriptor["workspace_root"]}},
        {"type": "wrong_type", "payload": {**descriptor}},
        {"type": "artifact_reference", "payload": {**descriptor, "session_id": "sid-old"}},
        {"source_event_type": "artifact_reference", "session_id": "sid-replay", "payload": {**descriptor}},
        {"type": "artifact_reference", "payload": {**descriptor}},
    ]
    for incumbent in incumbents:
        hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
            [{
                "role": "assistant",
                "content": "final answer",
                "_anchor_activity_scene": {
                    "version": "activity_scene_v1",
                    "activity_rows": [],
                    "artifacts": [incumbent],
                },
            }],
            {0: [descriptor]},
        )
        scene = hydrated[0]["_anchor_activity_scene"]
        helpers = _function_source(
            "static/ui.js", "function _turnArtifactWorkspacePath", "function _syncLiveWorklogReasonsForAnchor"
        )
        output = _run_node(
            "const S={session:{workspace:'/workspace',session_id:'sid-replay'}};\n"
            "const clicked=[]; const openArtifactPath=(entry)=>clicked.push(entry);\n"
            "const document={createElement:()=>({className:'',title:'',type:'',innerHTML:'',children:[],append(...x){this.children.push(...x)},appendChild(x){this.children.push(x)},setAttribute(){},addEventListener(_name,fn){this.onclick=fn}})};\n"
            + helpers
            + "const segment={children:[],querySelectorAll:()=>[],appendChild(node){this.children.push(node)}};\n"
            + "const message={_anchor_activity_scene:"
            + json.dumps(scene)
            + "};\n"
            + "_renderTurnArtifactListForMessage(message,segment,0);\n"
            + "segment.children[0].children[0].onclick();\n"
            + "console.log(JSON.stringify({entries:_turnArtifactEntriesFromScene(message._anchor_activity_scene),clicked}));"
        )
        expected = {
            "path": "output/report.md",
            "workspace_root": "/workspace",
            "session_id": "sid-replay",
            "tool_name": "patch",
            "tool_call_id": "call-replay",
            "type": "artifact_reference",
            "owner": {"session_id": "sid-replay", "workspace_root": "/workspace"},
        }
        assert output == {"entries": [expected], "clicked": [expected]}


def test_paginated_session_response_keeps_paired_landed_turn_artifacts():
    from api import routes

    messages = [
        {"role": "user", "content": "write the report"},
        {"role": "assistant", "tool_calls": [{"id": "call-1", "function": {"name": "patch"}}]},
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "patch",
            "content": json.dumps({"success": True, "files_modified": ["/workspace/output/large-worklog.md"]}),
        },
        {"role": "assistant", "content": "working"},
        {"role": "tool", "name": "read_file", "content": "ignored"},
        {"role": "assistant", "content": "final answer"},
    ]

    paths_by_final_index = routes._final_turn_artifact_paths(
        messages,
        workspace_root="/workspace",
        session_id="sid-work",
    )
    window, offset = routes._message_window_for_display(messages, msg_limit=1)
    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(window, paths_by_final_index, message_offset=offset)

    assert offset == 5
    assert hydrated[0]["_anchor_activity_scene"]["version"] == "activity_scene_v1"
    assert hydrated[0]["_anchor_activity_scene"]["artifacts"] == [
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/large-worklog.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-1",
                "tool_name": "patch",
                "session_id": "sid-work",
                "source": "transcript_replay",
            },
        }
    ]


def test_replay_rejects_failed_unpaired_duplicate_and_mismatched_writes():
    from api import routes

    messages = [
        {"role": "user", "content": "write"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call-failed", "function": {"name": "write_file"}},
                {"id": "call-mismatch", "function": {"name": "write_file"}},
                {"id": "call-dupe", "function": {"name": "write_file"}},
                {"id": "call-conflict", "function": {"name": "write_file"}},
                {"id": "call-patch", "function": {"name": "patch"}},
                {"id": "call-result", "function": {"name": "write_file"}},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-failed",
            "name": "write_file",
            "content": json.dumps({"error": "denied", "resolved_path": "/workspace/output/failed.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-mismatch",
            "name": "patch",
            "content": json.dumps({"success": True, "files_modified": ["/workspace/output/mismatch.md"]}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-dupe",
            "name": "write_file",
            "content": json.dumps({"error": "first failed", "resolved_path": "/workspace/output/dupe.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-dupe",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 4, "resolved_path": "/workspace/output/dupe.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-orphan",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 2, "resolved_path": "/workspace/output/orphan.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-conflict",
            "name": "write_file",
            "content": json.dumps({"error": "displayed error", "resolved_path": "/workspace/output/conflict.md"}),
            "result": json.dumps({"bytes_written": 8, "resolved_path": "/workspace/output/conflict.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-patch",
            "name": "patch",
            "content": json.dumps({"success": True, "files_modified": ["/workspace/output/report.md"]}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-result",
            "name": "write_file",
            "content": "Wrote /workspace/output/result-field.md",
            "result": json.dumps({"bytes_written": 11, "resolved_path": "/workspace/output/result-field.md"}),
        },
        {"role": "assistant", "content": "final"},
    ]

    assert routes._final_turn_artifact_paths(
        messages,
        workspace_root="/workspace",
        session_id="sid-default",
    ) == {
        10: [
            {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-patch",
                "tool_name": "patch",
                "session_id": "sid-default",
            },
            {
                "path": "output/result-field.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-result",
                "tool_name": "write_file",
                "session_id": "sid-default",
            }
        ]
    }


def test_final_turn_artifact_paths_treats_ambiguous_call_sequences_as_invalid():
    from api import routes

    messages = [
        {"role": "user", "content": "write"},
        {"role": "assistant", "content": "prepare"},
        {
            "role": "tool",
            "tool_call_id": "call-pre",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 1, "resolved_path": "/workspace/output/predecl.md"}),
        },
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call-missing", "function": {}},
                {"id": "call-dupe", "function": {"name": "write_file"}},
                {"id": "call-dupe", "function": {"name": "write_file"}},
                {"id": "call-mismatch", "function": {"name": "write_file"}},
                {"id": "call-dupres", "function": {"name": "write_file"}},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-missing",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 2, "resolved_path": "/workspace/output/missing.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-dupe",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 3, "resolved_path": "/workspace/output/dupe-first.md"}),
        },
        {"role": "assistant", "content": "middle"},
        {
            "role": "tool",
            "tool_call_id": "call-dupres",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 4, "resolved_path": "/workspace/output/dupres-first.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-dupres",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 5, "resolved_path": "/workspace/output/dupres-second.md"}),
        },
        {
            "role": "assistant",
            "tool_calls": [{"id": "call-mismatch", "function": {"name": "patch"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call-mismatch",
            "name": "patch",
            "content": json.dumps({"success": True, "files_modified": ["/workspace/output/mismatch.md"]}),
        },
        {"role": "assistant", "tool_calls": [{"id": "call-valid", "function": {"name": "write_file"}}]},
        {
            "role": "tool",
            "tool_call_id": "call-valid",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 6, "resolved_path": "/workspace/output/valid.md"}),
        },
        {"role": "assistant", "content": "final"},
    ]

    assert routes._final_turn_artifact_paths(
        messages,
        workspace_root="/workspace",
        session_id="sid-ambiguous",
    ) == {
        13: [
            {
                "path": "output/valid.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-valid",
                "tool_name": "write_file",
                "session_id": "sid-ambiguous",
            },
        ]
    }


def test_final_turn_artifact_projection_keeps_session_id_for_replay():
    from api import routes

    messages = [
        {"role": "user", "content": "write the report"},
        {"role": "assistant", "tool_calls": [{"id": "call-1", "function": {"name": "patch"}}]},
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "patch",
            "content": json.dumps({"success": True, "files_modified": ["/workspace/output/report.md"]}),
        },
        {"role": "assistant", "content": "final"},
    ]

    assert routes._final_turn_artifact_paths(
        messages,
        workspace_root="/workspace",
        session_id="sid-projection",
    ) == {
        3: [
            {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-1",
                "tool_name": "patch",
                "session_id": "sid-projection",
            }
        ],
    }


def test_landed_artifact_descriptors_use_actual_hermes_success_shapes():
    from api.turn_artifacts import landed_artifact_descriptors

    assert landed_artifact_descriptors(
        "write_file",
        {"bytes_written": 3, "resolved_path": "/workspace/output/report.md"},
        workspace_root="/workspace",
        tool_call_id="call-write",
    ) == [
        {
            "path": "output/report.md",
            "workspace_root": "/workspace",
            "tool_call_id": "call-write",
            "tool_name": "write_file",
        }
    ]
    assert (
        landed_artifact_descriptors(
            "write_file",
            {"error": "permission denied", "resolved_path": "/workspace/output/report.md"},
            workspace_root="/workspace",
            tool_call_id="call-write",
        )
        == []
    )
    assert (
        landed_artifact_descriptors(
            "write_file",
            {"bytes_written": 3, "resolved_path": "/workspace-a/output/report.md"},
            workspace_root="/workspace-b",
            tool_call_id="call-write",
        )
        == []
    )
    assert (
        landed_artifact_descriptors(
            "mcp_filesystem_write_file",
            {"bytes_written": 3, "resolved_path": "/workspace/output/report.md"},
            workspace_root="/workspace",
            tool_call_id="call-plugin",
        )
        == []
    )


def test_live_stream_completion_uses_landed_artifact_descriptors():
    streaming = (ROOT / "api/streaming.py").read_text(encoding="utf-8")
    start = streaming.index("def on_tool_complete")
    end = streaming.index("# Mirror the todo tool", start)
    body = streaming[start:end]
    assert "landed_artifact_descriptors(" in body
    assert "'artifacts': landed_artifacts" in body
    assert "'is_error': tool_result_is_error(function_result)" in body
    assert "'is_error': False" not in body


def test_artifact_open_expands_a_closed_workspace_preview_before_loading_file():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    start = workspace.index("async function openArtifactPath(path)")
    end = workspace.index("// ── Workspace file-tree", start)
    body = workspace[start:end]
    assert "ensureWorkspacePreviewVisible()" in body
    assert body.index("ensureWorkspacePreviewVisible()") < body.index("openFile(rel,{owner});")
