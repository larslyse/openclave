"""Request/response schemas — shared across all agents, no per-agent logic."""
from pydantic import BaseModel, Field
from typing import Literal



class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    attachments: list[str] = []


class ChatResponse(BaseModel):
    conversation_id: str
    reply: str
    elapsed_ms: int | None = None


class ChatRequestCreate(BaseModel):
    request_id: str
    message: str
    conversation_id: str | None = None
    attachments: list[str] = []


class ChatRequestStatus(BaseModel):
    request_id: str
    conversation_id: str
    status: str
    error: str | None = None
    created_at: str | None = None


class ConversationSummary(BaseModel):
    id: str
    title: str
    date: str
    updated_ms: int | None = None
    last_is_reply: bool = False
    from_agent: str | None = None  # another agent's "From <agent>" conversation
    folder: str | None = None  # folders.py


class AttachmentMeta(BaseModel):
    id: str
    filename: str
    media_type: str
    size: int


class UploadAttachmentRequest(BaseModel):
    conversation_id: str | None = None
    filename: str
    media_type: str
    data_base64: str


class AttachmentUploadResponse(BaseModel):
    conversation_id: str
    attachment: AttachmentMeta


class Turn(BaseModel):
    role: str
    content: str
    ts: str
    elapsed_ms: int | None = None
    model_id: str | None = None
    model_name: str | None = None
    from_agent: str | None = None  # a message another agent sent (agent_messages.py)
    from_name: str | None = None
    attachments: list[AttachmentMeta] = []


class ConversationDetail(BaseModel):
    id: str
    title: str
    date: str
    turns: list[Turn]
    summary: str | None = None
    folder: str | None = None  # folders.py
    purged: bool = False  # purge.py: transcript archived, Undo restores it
    # Set when it's an archived chat opened from Recaps (read-only until the
    # next message, which brings it back): its recap card, if written.
    archived: bool = False
    recap: dict | None = None


class RenameConversationRequest(BaseModel):
    title: str | None = None
    # folders.py: a folder to put the chat in, or "" to take it out.
    folder: str | None = None


class FolderRequest(BaseModel):
    name: str


class MoveConversationRequest(BaseModel):
    source_agent: str
    target_agent: str
    conversation_id: str


class HandoverRequest(BaseModel):
    source_agent: str
    target_agent: str
    conversation_id: str
    files: list[str] = []


class PlanPhase(BaseModel):
    name: str
    status: str
    percent: int


class PlanResponse(BaseModel):
    status: str
    updated: str
    target_event: str
    phases: list[PlanPhase]
    notes: str


class BacklogItem(BaseModel):
    text: str
    done: bool


class BacklogResponse(BaseModel):
    updated: str
    items: list[BacklogItem]


class KanbanItem(BaseModel):
    text: str
    stage: str


class KanbanResponse(BaseModel):
    items: list[KanbanItem]


class SetKanbanStageRequest(BaseModel):
    item: str
    stage: str | None = None


class MeasurementEntry(BaseModel):
    date: str
    metric: str
    value: str | None = None
    unit: str | None = None
    note: str | None = None


class TrackingResponse(BaseModel):
    updated: str
    entries: list[MeasurementEntry]


class LogMeasurementRequest(BaseModel):
    metric: str
    value: str | None = None
    unit: str | None = None
    note: str | None = None
    date: str | None = None
    section: str = "Progress"


class LogMeasurementsRequest(BaseModel):
    entries: list[LogMeasurementRequest]
    section: str = "Progress"


class UsageWindow(BaseModel):
    tokens_in: int
    tokens_out: int
    cost_usd: float | None = None
    api_equiv_usd: float | None = None


class UsageResponse(BaseModel):
    windows: dict[str, UsageWindow]


class AgentUsage(BaseModel):
    agent_id: str
    windows: dict[str, UsageWindow]


class AllUsageResponse(BaseModel):
    total: dict[str, UsageWindow]
    by_agent: list[AgentUsage]


class InboxItem(BaseModel):
    id: str
    from_agent: str
    from_name: str
    summary: str
    status: str
    result: str | None = None
    created_at: str
    updated_at: str


class InboxResponse(BaseModel):
    items: list[InboxItem]


class UpdateInboxItemRequest(BaseModel):
    status: str | None = None
    result: str | None = None


class AgentSummary(BaseModel):
    id: str
    name: str
    color: str
    tagline: str
    roles: list[str] = []
    domains: list[str]
    conversation_count: int
    plan_status: str
    backlog_open: int
    backlog_total: int
    model_id: str
    model_name: str
    model_supports: dict[str, list[str]] = {}  # model_id → connection tags it supports, e.g. ["HA"]
    avatar_v: int = 0  # cache-buster for /api/agents/<id>/avatar?v=; 0 = generated initials
    last_activity_ms: int | None = None
    last_is_reply: bool = False
    last_conversation_id: str | None = None
    clave: str | None = None  # name of the agent's clave (claves on; clones share a name)


class ModelOption(BaseModel):
    id: str
    provider: str
    display_name: str
    tier: str
    supports_vision: bool = False
    api_input_usd: float | None = None
    api_output_usd: float | None = None
    route: str = ""
    speed: str = ""
    group: str = ""
    supports: list[str] = []  # the agent's connections this model supports, e.g. ["HA"] (connections.py)
    cost_tier: str | None = None  # "low" | "mid" | "high" (llm_models.cost_tier); None = price unknown
    cost_label: str = ""  # "$" / "$$" / "$$$"
    cost_color: str = ""
    # The model picker's columns (2026-11-04, static/model_picker.js).
    vendor: str = ""          # Anthropic / OpenAI / xAI
    short_name: str = ""      # at most llm_models.NAME_MAX characters, no vendor word
    released: str | None = None   # "YYYY-MM"
    released_is_added: bool = False  # the month it was added to OpenClave (no provider date)
    old: str | None = None    # why it counts as old (llm_models.old_reason), or None
    latency_ms: int | None = None  # measured average reply time across agents
    latency_samples: int = 0
    ready: bool = True  # its route is set up on this computer (setup_check.py)


class ClaudeLoginCode(BaseModel):
    code: str  # from the Claude sign-in page (claude_auth.submit_code)


class CleanupAction(BaseModel):
    id: str            # suggestion id (cleanup.py)
    title: str | None = None  # rename: the name the owner settled on


class UnarchiveRequest(BaseModel):
    agent_id: str
    conversation_id: str


class PageAddRequest(BaseModel):
    block: int  # index of the form block in the agent's page.json
    text: str = ""


class SetModelRequest(BaseModel):
    model_id: str


class SetAgentOrderRequest(BaseModel):
    order: list[str]


class VoiceTranscribeRequest(BaseModel):
    audio_base64: str
    media_type: str = "audio/webm"
    language: str = Field("en", pattern=r"^[a-z]{2,3}$")  # en or [voice] second_language


class VoiceTranscribeResponse(BaseModel):
    transcript: str


class VoiceReplyRequest(BaseModel):
    conversation_id: str | None = None
    transcript: str
    language: str = Field("en", pattern=r"^[a-z]{2,3}$")


class VoiceTurnResponse(BaseModel):
    model_name: str | None = None
    conversation_id: str
    transcript: str
    reply: str
    audio_base64: str | None = None
    audio_mime: str | None = None
    elapsed_ms: int | None = None


class CreateAgentRequest(BaseModel):
    name: str
    purpose: str
    model_id: str
    clave: str | None = None  # also put it in this clave (claves overview "+ New agent")


class SetAgentToolsRequest(BaseModel):
    tools: list[str]


class AgentColorRequest(BaseModel):
    color: str


class RenameAgentRequest(BaseModel):
    name: str
    tagline: str


class IdentityBodyRequest(BaseModel):
    body: str


class WorkspaceFileCreate(BaseModel):
    path: str


class WorkspaceFileRename(BaseModel):
    path: str
    new_path: str


class MoveWorkspaceFileRequest(BaseModel):
    source_agent: str
    target_agent: str
    path: str


class WorkspaceFileWrite(BaseModel):
    path: str
    content: str
    version: str


class AvatarFetchRequest(BaseModel):
    url: str = Field(max_length=2000)


class RoomPost(BaseModel):
    text: str
    clave: str = "everyone"


class CreateClaveRequest(BaseModel):
    name: str = Field(max_length=40)
    members: list[str] = []


class UpdateClaveRequest(BaseModel):
    name: str | None = Field(default=None, max_length=40)
    members: list[str] | None = None
    lead: str | None = None
    set_lead: bool = False  # lead is only changed when this is true ("" / null = no lead)


class PlaceAgentRequest(BaseModel):
    agent: str
    to: str
    index: int | None = None
    lead: bool = False


class RemoveFromClaveRequest(BaseModel):
    agent: str
    clave: str


class ClavesEnabledRequest(BaseModel):
    enabled: bool


class AgentClaveRequest(BaseModel):
    clave: str | None = None  # None / "" = in no clave


class CloneAgentRequest(BaseModel):
    agent: str
    to: str
