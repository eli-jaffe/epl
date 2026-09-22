"""The Reason -> Plan -> Execute -> Reflect -> Synthesize agent loop.

See docs/agent_ui_architecture_plan.md for the overall design and
.claude/plans/vast-hopping-origami.md for why this shape was chosen:
- Execute uses Claude's native tool-calling (no hand-rolled JSON parsing).
- Reflect's SUCCESS/RETRY/CANNOT_ANSWER decision is a forced tool call
  (submit_reflection below), not regex-parsed free text -- the USMNT
  reference (references/agentic_analyst_review_usmnt.md) calls out silent
  regex-parsing failures as a real bug in that design; this avoids the
  class of bug entirely rather than writing a more careful parser.
- Reason/Plan/Synthesize stay free text: nothing branches on their exact
  wording, they're just context passed to the next phase.

Observability calls (agent.observability) write to Postgres, not DuckDB --
see agent/db/observability_models.py and
.claude/plans/vast-hopping-origami.md for why (keeps this process's DuckDB
connections strictly read-only; agent/observability_sync.py periodically
copies rows into DuckDB for analysis). They're genuinely async network I/O,
so every call is awaited, unlike agent.tools.registry.dispatch()'s tool
calls, which are sync local functions run via asyncio.to_thread instead.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from agent import llm, observability
from agent.glossary import DOMAIN_GLOSSARY
from agent.tools.registry import TOOL_REGISTRY, dispatch, to_claude_tools

MAX_OUTER_RETRIES = 3
MAX_EXECUTE_ROUNDS = 5

# Optional per-phase status callback, threaded through run_agent_query and
# each phase helper -- lets a caller (agent/api/chat.py's SSE endpoint)
# surface what the loop is doing in real time. None (the default) means
# "no one is listening" -- every existing caller (agent/mcp_server.py's
# ask_epl_agent) is unaffected. Called right before the work each phase is
# about to do, not after, so the message announces what's about to happen.
OnStatus = Callable[[str], Awaitable[None]]

STATUS_MESSAGES = {
    "reason": "Thinking about your question...",
    "plan": "Planning how to answer...",
    "reflect": "Double-checking the results...",
    "synthesize": "Writing your answer...",
}

# Per-tool status phrases for the execute-phase streaming status line, keyed
# by name against agent/tools/registry.py's TOOL_REGISTRY -- friendlier than
# the raw tool name for a fantasy-football user watching the status line.
# Deliberately covers only the tools that exist today: _execute's lookup
# below falls back to the old raw-name phrasing for anything not in this
# dict, so a future tool added to TOOL_REGISTRY without a matching entry
# here degrades to something functional (if unpolished) instead of
# emitting None or erroring.
TOOL_STATUS_MESSAGES = {
    "search_players": "Searching for players...",
    "get_player_summary": "Looking up player stats...",
    "get_player_gameweek_history": "Pulling gameweek history...",
    "compare_players": "Comparing players...",
    "get_top_performers": "Finding top performers...",
    "get_differentials": "Finding differential picks...",
    "get_value_analysis": "Analyzing value for money...",
    "get_schema": "Checking the data...",
    "run_sql": "Running a custom data query...",
}


async def _emit(on_status: OnStatus | None, message: str) -> None:
    if on_status is not None:
        await on_status(message)


SUBMIT_REFLECTION_TOOL = {
    "name": "submit_reflection",
    "description": "Report whether the gathered tool results are sufficient to answer the user's question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["SUCCESS", "RETRY", "CANNOT_ANSWER"]},
            "reasoning": {"type": "string", "description": "Why this status, in one or two sentences."},
        },
        "required": ["status", "reasoning"],
    },
}


def _text_from(message) -> str:
    return "".join(block.text for block in message.content if block.type == "text")


def _build_context(query_text: str, failed_calls: list[str]) -> str:
    parts = [f"User question: {query_text}"]
    if failed_calls:
        parts.append(
            "Tool calls already attempted and failed this session (do not repeat "
            "them verbatim): " + "; ".join(failed_calls)
        )
    return "\n\n".join(parts)


async def _reason(
    query_id: uuid.UUID,
    query_text: str,
    step_index: int,
    failed_calls: list[str],
    on_status: OnStatus | None = None,
) -> tuple[str, int]:
    await _emit(on_status, STATUS_MESSAGES["reason"])
    system = (
        f"{DOMAIN_GLOSSARY}\n\nYou are reasoning about a fantasy Premier League "
        "question before planning how to answer it. Restate your understanding "
        "of the question and any assumptions you're making, in 2-4 sentences. "
        "Do not answer the question yet."
    )
    messages = [{"role": "user", "content": _build_context(query_text, failed_calls)}]
    started_at = datetime.now(timezone.utc)
    response = await llm.call_claude(system=system, messages=messages, max_tokens=512)
    text = _text_from(response)
    step_id = await observability.log_step(query_id, "reason", step_index, started_at, summary=text)
    await observability.log_llm_call(step_id, response.model, llm.usage_dict(response))
    return text, step_index + 1


async def _plan(
    query_id: uuid.UUID,
    query_text: str,
    reason_text: str,
    step_index: int,
    on_status: OnStatus | None = None,
) -> tuple[str, int]:
    await _emit(on_status, STATUS_MESSAGES["plan"])
    tool_names = ", ".join(t.name for t in TOOL_REGISTRY)
    system = (
        f"{DOMAIN_GLOSSARY}\n\nGiven the question and your prior reasoning, "
        f"describe which tools you plan to call and in what order, in 2-4 "
        f"sentences. Available tools: {tool_names}. Do not call any tools yet."
    )
    messages = [{"role": "user", "content": f"User question: {query_text}\n\nYour reasoning: {reason_text}"}]
    started_at = datetime.now(timezone.utc)
    response = await llm.call_claude(system=system, messages=messages, max_tokens=512)
    text = _text_from(response)
    step_id = await observability.log_step(query_id, "plan", step_index, started_at, summary=text)
    await observability.log_llm_call(step_id, response.model, llm.usage_dict(response))
    return text, step_index + 1


async def _execute(
    query_id: uuid.UUID,
    query_text: str,
    plan_text: str,
    step_index: int,
    failed_calls: list[str],
    on_status: OnStatus | None = None,
) -> tuple[str, int]:
    system = (
        f"{DOMAIN_GLOSSARY}\n\nCall whatever tools you need to answer the "
        "question, following your plan. You may call multiple tools across "
        "multiple turns if needed. Once you have what you need, or if you've "
        "confirmed the data isn't available, stop calling tools and briefly "
        "summarize what you found."
    )
    messages: list[dict] = [
        {"role": "user", "content": f"User question: {query_text}\n\nYour plan: {plan_text}"}
    ]
    tools = to_claude_tools()
    results_summary: list[str] = []

    for round_num in range(MAX_EXECUTE_ROUNDS):
        round_started_at = datetime.now(timezone.utc)
        response = await llm.call_claude(system=system, messages=messages, tools=tools, max_tokens=1024)
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        step_id = await observability.log_step(
            query_id,
            "execute",
            step_index,
            round_started_at,
            summary=f"round {round_num}: {'requested ' + str(len(tool_uses)) + ' tool call(s)' if tool_uses else 'stopped requesting tools'}",
        )
        await observability.log_llm_call(step_id, response.model, llm.usage_dict(response))
        step_index += 1

        if not tool_uses:
            results_summary.append(_text_from(response))
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_result_blocks = []
        for tu in tool_uses:
            await _emit(on_status, TOOL_STATUS_MESSAGES.get(tu.name, f"Looking up {tu.name}..."))
            tool_started_at = datetime.now(timezone.utc)
            envelope = await dispatch(tu.name, **tu.input)
            call_desc = f"{tu.name}({tu.input})"
            if envelope["success"]:
                results_summary.append(f"{call_desc} -> {envelope['data']}")
            else:
                failed_calls.append(call_desc)
                results_summary.append(f"{call_desc} -> ERROR: {envelope['error']}")

            await observability.log_step(
                query_id,
                "execute",
                step_index,
                tool_started_at,
                tool_name=tu.name,
                tool_args=tu.input,
                # Truncated to bound row size, matching the USMNT reference's
                # tool-output-truncation tip already cited elsewhere in this
                # codebase -- only stored on success, since the failure case
                # is already captured by tool_error.
                # default=str: some tool/run_sql results include datetime
                # values straight from DuckDB, which json.dumps can't
                # serialize by default -- found by actually running a fixture
                # question through ask_epl_agent, 2026-09-20, where it crashed
                # the whole query rather than just this logging call.
                tool_result=json.dumps(envelope["data"], default=str)[:2000] if envelope["success"] else None,
                tool_success=envelope["success"],
                tool_error=envelope["error"],
                summary=call_desc,
            )
            step_index += 1

            tool_result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": str(envelope["data"]) if envelope["success"] else str(envelope["error"]),
                    "is_error": not envelope["success"],
                }
            )
        messages.append({"role": "user", "content": tool_result_blocks})
    else:
        results_summary.append("(reached max tool-call rounds without the model stopping on its own)")

    return "\n".join(results_summary), step_index


async def _reflect(
    query_id: uuid.UUID,
    query_text: str,
    execute_summary: str,
    step_index: int,
    on_status: OnStatus | None = None,
) -> tuple[str, str, int]:
    await _emit(on_status, STATUS_MESSAGES["reflect"])
    system = (
        f"{DOMAIN_GLOSSARY}\n\nDecide whether the gathered tool results are "
        "sufficient to answer the user's question. Call submit_reflection "
        "with your decision."
    )
    messages = [
        {"role": "user", "content": f"User question: {query_text}\n\nTool results gathered:\n{execute_summary}"}
    ]
    # max_tokens=1024, not 256: confirmed empirically (2026-09-20) that 256
    # truncates mid-generation on real-sized tool result summaries -- the
    # model writes "status" before "reasoning" (schema field order), so a
    # truncated response (stop_reason="max_tokens") comes back with "status"
    # present but "reasoning" missing entirely, not just short.
    started_at = datetime.now(timezone.utc)
    response = await llm.call_claude(
        system=system,
        messages=messages,
        tools=[SUBMIT_REFLECTION_TOOL],
        tool_choice={"type": "tool", "name": "submit_reflection"},
        max_tokens=1024,
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    status = tool_use.input.get("status", "CANNOT_ANSWER")
    reasoning = tool_use.input.get("reasoning", "(no reasoning provided -- response was truncated)")
    step_id = await observability.log_step(query_id, "reflect", step_index, started_at, summary=f"{status}: {reasoning}")
    await observability.log_llm_call(step_id, response.model, llm.usage_dict(response))
    return status, reasoning, step_index + 1


async def _synthesize(
    query_id: uuid.UUID,
    query_text: str,
    execute_summary: str,
    status: str,
    step_index: int,
    on_status: OnStatus | None = None,
) -> tuple[str, int]:
    await _emit(on_status, STATUS_MESSAGES["synthesize"])
    # Found empirically (2026-09-20, testing ask_epl_agent): with the
    # reflection status folded into the same user turn as the question, the
    # model sometimes replies to the *status* instead of the question --
    # e.g. a bare "Session ended - reflection completed." with no actual
    # answer, even though good tool results were available. Framing the
    # status as an explicit internal aside (not part of the user's question)
    # and telling the model directly not to comment on process fixes the
    # observed cases; the retry-on-empty below is a separate backstop for
    # the rarer case where the model returns no text block at all.
    system = (
        f"{DOMAIN_GLOSSARY}\n\nWrite the final answer to the user's fantasy "
        "Premier League question below, as if speaking directly to them. "
        "Base it only on the tool results gathered. Do not describe your "
        "internal process, mention 'reflection' or 'tool calls', or remark "
        "that the session/conversation has ended -- just give the "
        "substantive answer. If the results are incomplete or a tool wasn't "
        "available, say what's missing as part of that answer, rather than "
        "guessing or making up numbers."
    )
    messages = [
        {
            "role": "user",
            "content": (
                f"User question: {query_text}\n\n"
                f"Tool results gathered:\n{execute_summary}\n\n"
                "(Internal note, not part of the user's question: an earlier "
                f"reflection step marked these results as {status}. Do not "
                "mention this note or its status in your answer.)"
            ),
        }
    ]
    started_at = datetime.now(timezone.utc)
    response = await llm.call_claude(system=system, messages=messages, max_tokens=1024)
    answer = _text_from(response)
    if not answer.strip():
        response = await llm.call_claude(system=system, messages=messages, max_tokens=1024)
        answer = _text_from(response)
    step_id = await observability.log_step(query_id, "synthesize", step_index, started_at, summary=answer[:200])
    await observability.log_llm_call(step_id, response.model, llm.usage_dict(response))
    if not answer.strip():
        answer = (
            "I gathered relevant data but wasn't able to generate a written "
            "answer from it. Please try rephrasing your question."
        )
    return answer, step_index + 1


async def run_agent_query(
    user_id: uuid.UUID, query_text: str, on_status: OnStatus | None = None
) -> str:
    query_id = await observability.start_query(user_id, query_text)
    step_index = 0
    failed_calls: list[str] = []
    execute_summary = ""
    status = "CANNOT_ANSWER"

    try:
        for _attempt in range(MAX_OUTER_RETRIES):
            reason_text, step_index = await _reason(query_id, query_text, step_index, failed_calls, on_status)
            plan_text, step_index = await _plan(query_id, query_text, reason_text, step_index, on_status)
            execute_summary, step_index = await _execute(
                query_id, query_text, plan_text, step_index, failed_calls, on_status
            )
            status, _reasoning, step_index = await _reflect(query_id, query_text, execute_summary, step_index, on_status)
            if status == "SUCCESS":
                break

        answer, step_index = await _synthesize(query_id, query_text, execute_summary, status, step_index, on_status)
        await observability.finish_query(query_id, "success" if status == "SUCCESS" else "failed", answer)
        return answer
    except Exception:
        await observability.finish_query(query_id, "failed", None)
        raise
