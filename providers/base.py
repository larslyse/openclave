"""What every chat route offers llm_client.chat().

A provider turns one chat call into a reply string. It gets the catalog entry
(llm_models.ModelOption) rather than a bare id, because the routes read
different fields of it (api_model_id, reasoning_effort, speed). Stopping a
running call is shared, not per provider: llm_runs.cancel_running stops an API
stream at its next chunk (llm_runs._api_run) or a CLI process group by its run
marker (llm_runs._mark_run).
"""


class Provider:
    id: str = ""                    # llm_models.ModelOption.provider
    route: str = ""                 # "API" or "CLI", as llm_models.ModelOption.route
    streams_text: bool = False      # reply text shows in request_phase while it is written
    in_process_tools: bool = False  # tools run here via llm_tools._run_tool (and land in tools_run);
                                    # CLI routes reach them through the vault MCP server instead

    def chat(self, agent_id: str, model, system_prompt: str, messages: list[dict], *,
             max_tokens: int | None = None, effort: str | None = None) -> str:
        """messages as llm_client.chat takes them; returns the final reply text.
        max_tokens and effort are only used where llm_client.chat's docstring says."""
        raise NotImplementedError
