"""The Anthropic route: Claude through the Anthropic API, streamed, prompt-
cached, with API-side tool calls run through llm_tools (split out of
llm_client.py)."""

import request_phase
from llm_runs import _api_run, _check_stop
from llm_tools import MAX_TOOL_ROUNDS, _run_tool, tools_for
from llm_usage import log_usage
from providers.base import Provider


def _anthropic_tools(agent_id: str) -> list[dict]:
    return [
        {
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "input_schema": t["function"]["parameters"],
        }
        for t in tools_for(agent_id)
    ]


def _anthropic_content(content):
    """Passes plain-string content through untouched (every history turn).
    The current turn, when it carries attachments, arrives as chat_service's
    neutral block shape ({"type":"image","media_type":...,"data":...}) —
    adapt just that into Anthropic's nested source-object shape."""
    if isinstance(content, str):
        return content
    return [
        {"type": "image", "source": {"type": "base64", "media_type": b["media_type"], "data": b["data"]}}
        if b.get("type") == "image" else b
        for b in content
    ]


def _chat_anthropic(agent_id: str, model_id: str, system_prompt: str, messages: list[dict], *, max_tokens: int | None = None, effort: str | None = None) -> str:
    import anthropic
    client = anthropic.Anthropic()
    convo = [{"role": m["role"], "content": _anthropic_content(m["content"])} for m in messages]
    # Prompt caching: mark the last tool definition and the system block as
    # cache breakpoints. Anthropic's cache prefix order is tools -> system ->
    # messages, and a breakpoint caches everything up to and including it —
    # so this one system-block marker is enough to cover both (tools and
    # system are both static per agent; conversation history isn't marked,
    # since it changes every turn and wouldn't hit anyway). Confirmed live
    # 2026-09-19 against Rocky's real ~30k-char prompt: first call wrote
    # ~16.5k cache tokens, every call after read them instead of paying full
    # input price again. The newer top-level "automatic" cache_control param
    # documented alongside this did NOT produce a cache hit in testing on
    # this SDK version (1.3.0) — use the explicit per-block form only.
    #
    # 1h TTL, not the 5m default: real conversations don't fire fast enough
    # for a 5-minute cache to matter. Confirmed live 2026-09-20 on Rocky's
    # actual usage — messages 14-20 minutes apart both landed as a full
    # cache_creation (paying the write price again) instead of a read,
    # because the default ephemeral cache had already expired between
    # turns. Writing at 1h TTL costs 2x input price instead of 1.25x, but
    # for this usage pattern (sparse messages, not rapid-fire) a hit some
    # time later at 0.1x is worth the pricier write.
    cache_control = {"type": "ephemeral", "ttl": "1h"}
    tools = _anthropic_tools(agent_id)
    if tools:
        tools[-1]["cache_control"] = cache_control
    system = [{"type": "text", "text": system_prompt, "cache_control": cache_control}]
    usage_total = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }

    with _api_run(agent_id) as stop:
        return _anthropic_rounds(agent_id, client, model_id, system, tools, convo, usage_total, max_tokens, effort, stop)


def _anthropic_rounds(agent_id, client, model_id, system, tools, convo, usage_total, max_tokens, effort, stop) -> str:
    for _ in range(MAX_TOOL_ROUNDS):
        _check_stop(stop)
        request_phase.set_phase(agent_id, "thinking")
        request_phase.reset_text(agent_id)
        # Streamed so the pending bubble shows the reply as it's written
        # (request_phase text); the final message is the same as create's.
        with client.messages.stream(
            model=model_id,
            # Not just a generous reply budget: extended-thinking models (Opus
            # 5 confirmed live) spend real output tokens "thinking" before
            # any visible text, counted against this same cap. Reproduced
            # live at 1024: a large system prompt + a couple of tool calls
            # was enough to burn the whole budget on thinking mid-tool-call,
            # hit stop_reason="max_tokens" with zero text blocks, and return
            # a silently empty reply — no error, just nothing, every time
            # that combination came up. The voice path's override (see
            # chat()) is much lower (200) but safe from that failure mode:
            # voice mode's prompt has no tools/thinking to compete with the
            # budget, so the whole cap is available for visible text.
            max_tokens=max_tokens or 8192,
            system=system,
            tools=tools,
            messages=convo,
            **({"output_config": {"effort": effort}} if effort else {}),
        ) as stream:
            for event in stream:
                _check_stop(stop)
                if event.type == "text":
                    request_phase.add_text(agent_id, event.text)
            response = stream.get_final_message()
        u = response.usage
        usage_total["input_tokens"] += u.input_tokens or 0
        usage_total["output_tokens"] += u.output_tokens or 0
        usage_total["cache_creation_input_tokens"] += u.cache_creation_input_tokens or 0
        usage_total["cache_read_input_tokens"] += u.cache_read_input_tokens or 0

        if response.stop_reason != "tool_use":
            log_usage(agent_id, "anthropic", model_id, usage_total, None)
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            if not text:
                # Surfaced as a real error (run_chat_locked's ProviderReplyError
                # check) instead of silently persisting a blank turn — this is
                # exactly the failure mode the max_tokens bump above targets,
                # kept as a safety net in case some other combination still
                # exhausts the budget on thinking/tool-use with no visible reply.
                return f"(error: model stopped ({response.stop_reason}) without producing any reply text)"
            return text

        # The streamed message's text blocks carry a client-side parsed_output
        # field that the API rejects when sent back (400, seen 2026-10-13).
        convo.append({"role": "assistant", "content": [
            b.model_dump(exclude={"parsed_output"}) for b in response.content]})
        request_phase.set_phase(agent_id, "using tools")
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                _check_stop(stop)
                result = _run_tool(agent_id, block.name, block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        convo.append({"role": "user", "content": tool_results})

    log_usage(agent_id, "anthropic", model_id, usage_total, None)
    return "(reached tool-use round limit without a final reply)"


class AnthropicApiProvider(Provider):
    id = "anthropic"
    route = "API"
    streams_text = True
    in_process_tools = True

    def chat(self, agent_id, model, system_prompt, messages, *, max_tokens=None, effort=None):
        return _chat_anthropic(agent_id, model.id, system_prompt, messages, max_tokens=max_tokens, effort=effort)
