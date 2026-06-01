"""Regression coverage for compression-exhausted stream finalization."""

from pathlib import Path

from api.streaming import (
    _agent_result_terminal_failure,
    _session_lacks_final_assistant_answer,
)

ROOT = Path(__file__).resolve().parents[1]


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def test_compression_exhausted_result_is_terminal_failure_even_after_streamed_text():
    result = {
        "failed": True,
        "partial": True,
        "compression_exhausted": True,
        "error": "Context length exceeded: 119,194 tokens. Cannot compress further.",
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "I am still working through the files."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "large output"},
        ],
    }

    assert _agent_result_terminal_failure(result) is True
    assert _session_lacks_final_assistant_answer(result["messages"]) is True


def test_terminal_failure_checks_final_answer_even_when_assistant_was_added():
    src = _read("api/streaming.py")
    start = src.find("_terminal_failure = (")
    assert start != -1, "terminal failure assignment not found"
    end = src.find("if _terminal_failure:", start)
    assert end != -1, "terminal failure guard not found"
    block = src[start:end]

    assert "_session_lacks_final_assistant_answer(_all_result_messages)" in block
    assert "not _assistant_added" not in block


def test_completed_tool_tail_without_final_assistant_is_not_successful_done():
    messages = [
        {"role": "user", "content": "Run the tool then answer."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ]

    assert _session_lacks_final_assistant_answer(messages) is True


def test_assistant_content_with_tool_calls_is_not_final_answer():
    messages = [
        {"role": "user", "content": "Search, then answer."},
        {
            "role": "assistant",
            "content": "I found a likely source and will inspect it.",
            "tool_calls": [{"id": "call_1"}],
        },
    ]

    assert _session_lacks_final_assistant_answer(messages) is True


def test_context_compaction_marker_is_not_final_answer():
    messages = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
        },
    ]

    assert _session_lacks_final_assistant_answer(messages) is True


def test_context_compaction_marker_before_final_text_is_successful_answer():
    messages = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
        },
        {"role": "assistant", "content": "Here is the final answer."},
    ]

    assert _session_lacks_final_assistant_answer(messages) is False


def test_context_compaction_marker_before_tool_tail_is_not_final_answer():
    messages = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
        },
        {
            "role": "assistant",
            "content": "I will inspect the result.",
            "tool_calls": [{"id": "call_1"}],
        },
    ]

    assert _session_lacks_final_assistant_answer(messages) is True


def test_final_assistant_text_is_successful_terminal_answer():
    messages = [
        {"role": "user", "content": "Run the tool then answer."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "assistant", "content": "Here is the final answer."},
    ]

    assert _session_lacks_final_assistant_answer(messages) is False


def test_assistant_tool_call_turn_followed_by_final_text_is_successful_answer():
    messages = [
        {"role": "user", "content": "Search, then answer."},
        {
            "role": "assistant",
            "content": "I found a likely source and will inspect it.",
            "tool_calls": [{"id": "call_1"}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "assistant", "content": "Here is the final answer."},
    ]

    assert _session_lacks_final_assistant_answer(messages) is False


def test_compression_exhausted_apperror_clears_reference_ui_and_labels_error():
    src = _read("static/messages.js")
    start = src.find("source.addEventListener('apperror'")
    assert start != -1, "apperror listener not found"
    end = src.find("source.addEventListener('warning'", start)
    assert end != -1, "warning listener after apperror not found"
    block = src[start:end]

    assert "const isCompressionExhausted=d.type==='compression_exhausted';" in block
    assert "isCompressionExhausted?'Context compression exhausted'" in block
    assert "if(typeof clearCompressionUi==='function') clearCompressionUi();" in block
    assert "window._compressionUi=null;" in block
