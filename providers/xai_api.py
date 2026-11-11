"""The xAI route: Grok through xAI's OpenAI-compatible API, streamed, with
API-side tool calls run through llm_tools (split out of llm_client.py)."""
import json
import os
import threading

import request_phase
from llm_runs import _api_run, _check_stop
from llm_tools import MAX_TOOL_ROUNDS, _run_tool, tools_for
from llm_usage import log_usage


XAI_BASE_URL = "https://api.x.ai/v1"


def _chat_xai(
    agent_id: str, model_id: str, system_prompt: str, messages: list[dict],
    api_model_id: str | None = None, reasoning_effort: str | None = None,
) -> str:
    # model_id is the catalog/selection id (used for usage logging, so
    # "Grok 4.6" vs "Grok 4.6 Fast" stay distinguishable there) — api_model_id
    # is the literal string xAI expects, which can differ (both grok-4.6
    # variants share one real model, distinguished only by reasoning_effort).
    import openai
    client = openai.OpenAI(api_key=os.environ["XAI_API_KEY"], base_url=XAI_BASE_URL)
    def openai_content(content):
        if not isinstance(content, list):
            return content
        return [{"type": "text", "text": b["text"]} if b["type"] == "text" else
                {"type": "image_url", "image_url": {"url": f"data:{b['media_type']};base64,{b['data']}"}}
                for b in content]
    convo = [{"role": "system", "content": system_prompt}] + [{**m, "content": openai_content(m["content"])} for m in messages]
    usage_total = {"input_tokens": 0, "output_tokens": 0}
    with _api_run(agent_id) as stop:
        for _ in range(MAX_TOOL_ROUNDS):
            _check_stop(stop)
            request_phase.set_phase(agent_id, "thinking")
            request_phase.reset_text(agent_id)
            stream = client.chat.completions.create(
                model=api_model_id or model_id,
                max_tokens=1024,
                tools=tools_for(agent_id),
                messages=convo,
                stream=True,
                stream_options={"include_usage": True},
                **({"reasoning_effort": reasoning_effort} if reasoning_effort else {}),
            )
            text, calls, finish_reason = _read_openai_stream(agent_id, stream, usage_total, stop)
            if finish_reason != "tool_calls" or not calls:
                log_usage(agent_id, "xai", model_id, usage_total, None)
                return text.strip()

            convo.append({"role": "assistant", "content": text or None, "tool_calls": calls})
            request_phase.set_phase(agent_id, "using tools")
            for call in calls:
                _check_stop(stop)
                args = json.loads(call["function"]["arguments"] or "{}")
                result = _run_tool(agent_id, call["function"]["name"], args)
                convo.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                })

    log_usage(agent_id, "xai", model_id, usage_total, None)
    return "(reached tool-use round limit without a final reply)"


def _read_openai_stream(agent_id: str, stream, usage_total: dict, stop: threading.Event) -> tuple[str, list[dict], str | None]:
    """Collects one streamed chat completion: the text (shown live through
    request_phase), the tool calls rebuilt from their pieces, and why it
    finished. Tool-call pieces arrive keyed by index: the first carries the
    id and name, later ones more of the arguments string."""
    text, calls, finish_reason = "", {}, None
    try:
        for chunk in stream:
            _check_stop(stop)
            if chunk.usage:
                usage_total["input_tokens"] += chunk.usage.prompt_tokens or 0
                usage_total["output_tokens"] += chunk.usage.completion_tokens or 0
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta and delta.content:
                text += delta.content
                request_phase.add_text(agent_id, delta.content)
            for piece in (delta.tool_calls or []) if delta else []:
                call = calls.setdefault(piece.index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if piece.id:
                    call["id"] = piece.id
                if piece.function and piece.function.name:
                    call["function"]["name"] += piece.function.name
                if piece.function and piece.function.arguments:
                    call["function"]["arguments"] += piece.function.arguments
            if choice.finish_reason:
                finish_reason = choice.finish_reason
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()
    return text, [calls[i] for i in sorted(calls)], finish_reason
