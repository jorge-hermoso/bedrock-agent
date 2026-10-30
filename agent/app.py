"""Support agent for Bedrock AgentCore Runtime.

Each invocation:
  1. on the first turn of a session, loads context from AgentCore Memory: recent
     turns of this session (short-term) and the customer's preferences, facts and
     past-conversation summaries (long-term),
  2. retrieves passages for the user's question through the gateway's knowledge
     base tool, so the search is subject to the same Dogwood policies as any tool,
  3. runs a Converse tool-use loop with Claude, passing the guardrail on every call
     (IAM denies model calls without it) and the passages as grounding source,
  4. executes tool calls through the AgentCore Gateway over MCP with SigV4,
     where the Dogwood policies allow or deny each call,
  5. writes the turn (question + final answer) back to memory.

Payload:  {"prompt": "...", "actor_id": "<customer id>"}
Response: {"answer": "...", "stop_reason": "...", "tool_calls": [...]}

actor_id scopes memory to one customer. The runtime is invoked with IAM, so the
calling backend is trusted to pass the authenticated customer's ID.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

import boto3
from bedrock_agentcore.memory import MemoryClient
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext
from mcp import ClientSession
from mcp.shared.exceptions import McpError
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

logger = logging.getLogger("agent")
logging.basicConfig(level=logging.INFO)

REGION = os.environ.get("AWS_REGION") or boto3.session.Session().region_name
MODEL_ID = os.environ["MODEL_ID"]
GUARDRAIL_ARN = os.environ["GUARDRAIL_ARN"]
GUARDRAIL_VERSION = os.environ["GUARDRAIL_VERSION"]
# Whether the guardrail has a contextual grounding policy (enable_contextual_grounding).
GUARDRAIL_GROUNDING = os.environ.get("GUARDRAIL_GROUNDING", "false") == "true"
KB_SEARCH_TOOL = os.environ.get("KB_SEARCH_TOOL", "Knowledge___search_knowledge_base")
GATEWAY_URL = os.environ["GATEWAY_URL"]
MEMORY_ID = os.environ["MEMORY_ID"]
MEMORY_LONG_TERM = os.environ.get("MEMORY_LONG_TERM", "true") == "true"
MEMORY_TURNS = int(os.environ.get("MEMORY_TURNS", "10"))

MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "16000"))
MAX_TURNS = int(os.environ.get("MAX_TURNS", "20"))
RETRIEVE_RESULTS = int(os.environ.get("RETRIEVE_RESULTS", "5"))
MEMORY_RECORDS = int(os.environ.get("MEMORY_RECORDS", "3"))

USAGE_TABLE = os.environ["USAGE_TABLE"]
TOKENS_PER_MINUTE = int(os.environ["TOKENS_PER_MINUTE"])
TOKENS_PER_DAY = int(os.environ["TOKENS_PER_DAY"])
# Hash of the deployed Dogwood policies. A policy change invalidates open policy
# sessions (409), so it is mixed into the session ID to start fresh ones.
POLICY_SET = os.environ.get("POLICY_SET", "")

ACTOR_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,254}$")

SYSTEM_PROMPT = """You are a customer support agent.

Answer product and policy questions from the reference passages and the
search_knowledge_base tool. If they don't contain the answer, say you don't know.

For refunds: look up the order, request approval for the exact amount, and only
issue the refund if approval comes back approved=true. Amounts are integer cents.

Some tool calls may be denied by policy. If that happens, tell the customer the
request can't be completed automatically and that a person will follow up. Do not
retry a denied call with different values to get around the limit.

You may be given notes about the customer from earlier conversations. Use them
to personalize help, but verify order details with tools rather than trusting
the notes."""

bedrock = boto3.client("bedrock-runtime", region_name=REGION)
memory = MemoryClient(region_name=REGION)
ddb = boto3.client("dynamodb", region_name=REGION)

app = BedrockAgentCoreApp()


@dataclass
class Session:
    messages: list[dict] = field(default_factory=list)
    # Long-term notes, attached to the session's first new user turn only.
    notes: str = ""


# AgentCore Runtime routes a session to the same instance, so the live Converse
# history (including tool-use and reasoning blocks) stays in process. Memory
# restores a text-only version when the session lands on a fresh instance.
_sessions: dict[tuple[str, str], Session] = {}


def restore_turns(actor_id: str, session_id: str) -> list[dict]:
    """Rebuild recent turns of this session from short-term memory events."""
    events = memory.list_events(memory_id=MEMORY_ID, actor_id=actor_id, session_id=session_id, max_results=100)
    events.sort(key=lambda e: e["eventTimestamp"])
    messages = []
    for event in events[-MEMORY_TURNS:]:
        for item in event.get("payload", []):
            turn = item.get("conversational")
            if turn:
                role = "user" if turn["role"] == "USER" else "assistant"
                messages.append({"role": role, "content": [{"text": turn["content"]["text"]}]})
    # Converse requires the history to start with a user turn and alternate roles.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    while messages and messages[-1]["role"] != "assistant":
        messages.pop()
    return messages


def recall_notes(actor_id: str, question: str) -> str:
    """Long-term records relevant to the question, across all past sessions."""
    sections = []
    for title, kwargs in [
        ("Preferences", {"namespace": f"/preferences/{actor_id}/"}),
        ("Facts", {"namespace": f"/facts/{actor_id}/"}),
        ("Earlier conversations", {"namespace_path": f"/summaries/{actor_id}/"}),
    ]:
        records = memory.retrieve_memories(memory_id=MEMORY_ID, query=question, top_k=MEMORY_RECORDS, **kwargs)
        lines = [f"- {r['content']['text']}" for r in records if r.get("content", {}).get("text")]
        if lines:
            sections.append(f"{title}:\n" + "\n".join(lines))
    return "\n\n".join(sections)


def load_session(actor_id: str, session_id: str, question: str) -> Session:
    key = (actor_id, session_id)
    if key in _sessions:
        return _sessions[key]

    session = Session()
    # Memory is best-effort: a failure here degrades to a fresh session, not an error.
    try:
        session.messages = restore_turns(actor_id, session_id)
    except Exception:
        logger.exception("could not restore session turns from memory")
    if MEMORY_LONG_TERM:
        try:
            session.notes = recall_notes(actor_id, question)
        except Exception:
            logger.exception("could not retrieve long-term memory")
    _sessions[key] = session
    return session


def save_turn(actor_id: str, session_id: str, question: str, answer: str) -> None:
    try:
        memory.create_event(
            memory_id=MEMORY_ID,
            actor_id=actor_id,
            session_id=session_id,
            messages=[(question, "USER"), (answer, "ASSISTANT")],
        )
    except Exception:
        logger.exception("could not save turn to memory")


class DailyTokenLimit(Exception):
    pass


def _usage_keys(now: float) -> tuple[str, str]:
    return f"minute#{int(now // 60)}", f"day#{time.strftime('%Y-%m-%d', time.gmtime(now))}"


def wait_for_token_budget() -> None:
    """Block until the shared per-minute budget has room; raise once the daily budget is spent.

    Limits are checked before a call and charged after it, so concurrent calls can
    overshoot by a few calls' worth of tokens. That's fine for a cost guard.
    """
    while True:
        now = time.time()
        minute_key, day_key = _usage_keys(now)
        items = ddb.batch_get_item(
            RequestItems={USAGE_TABLE: {"Keys": [{"pk": {"S": minute_key}}, {"pk": {"S": day_key}}], "ConsistentRead": True}}
        )["Responses"][USAGE_TABLE]
        used = {i["pk"]["S"]: int(i["used"]["N"]) for i in items}
        if used.get(day_key, 0) >= TOKENS_PER_DAY:
            raise DailyTokenLimit
        if used.get(minute_key, 0) < TOKENS_PER_MINUTE:
            return
        wait = 60 - now % 60 + 0.1
        logger.warning("per-minute token limit reached; waiting %.1fs", wait)
        time.sleep(wait)


def record_token_usage(tokens: int) -> None:
    now = time.time()
    for key, ttl in zip(_usage_keys(now), (120, 2 * 86400)):
        ddb.update_item(
            TableName=USAGE_TABLE,
            Key={"pk": {"S": key}},
            UpdateExpression="ADD used :t SET expires_at = if_not_exists(expires_at, :e)",
            ExpressionAttributeValues={":t": {"N": str(tokens)}, ":e": {"N": str(int(now) + ttl)}},
        )


def append_user(history: list[dict], message: dict) -> None:
    # A turn that stopped before the model answered (token limit, max turns, error)
    # leaves a user message last; merge so roles keep alternating as Converse requires.
    if history and history[-1]["role"] == "user":
        history[-1]["content"].extend(message["content"])
    else:
        history.append(message)


async def retrieve_passages(session: ClientSession, question: str) -> tuple[str, str]:
    """Search the knowledge base through the gateway. Returns (passages, status).

    A denied or failed search returns no passages; the model can still answer
    (or call the search tool itself) and the guardrail checks the result.
    """
    status, content = await call_gateway_tool(session, KB_SEARCH_TOOL, {"query": question, "max_results": RETRIEVE_RESULTS})
    if status != "success":
        return "", status
    block = content[0]
    try:
        data = block["json"] if "json" in block else json.loads(block.get("text") or "{}")
    except json.JSONDecodeError:
        logger.warning("unexpected knowledge base result: %.200s", block.get("text"))
        return "", "error"
    return "\n\n".join(r["text"] for r in data.get("results", []) if r.get("text")), status


def user_turn(question: str, passages: str, notes: str = "") -> dict:
    # guardContent qualifiers drive the guardrail's contextual grounding check:
    # the answer is scored against grounding_source for relevance to query.
    # invoke() drops the check (drop_grounding) once the model calls a tool.
    content = []
    if notes:
        content.append({"text": f"<customer_notes>\n{notes}\n</customer_notes>"})
    if passages and GUARDRAIL_GROUNDING:
        content.append({"guardContent": {"text": {"text": passages, "qualifiers": ["grounding_source"]}}})
        content.append({"guardContent": {"text": {"text": question, "qualifiers": ["query"]}}})
    else:
        # Converse rejects a query without a grounding source. With grounding off,
        # or no passages (empty knowledge base, denied or failed search), passages
        # are plain context and the question is still screened by the content filters.
        if passages:
            content.append({"text": f"<knowledge_base>\n{passages}\n</knowledge_base>"})
        content.append({"guardContent": {"text": {"text": question, "qualifiers": ["guard_content"]}}})
    return {"role": "user", "content": content}


def drop_grounding(messages: list[dict]) -> None:
    """Turn off the contextual grounding check for everything already in messages.

    Grounding scores the answer against the knowledge-base passages. That fits
    answers drawn from the documentation, but answers built on tool results
    (order status, refund outcome) score low and get blocked. Tool calls are
    governed by the Dogwood policies instead. Passages become plain context and
    the question stays screened by the content filters (guard_content).
    """
    for message in messages:
        if message["role"] != "user":
            continue
        for i, block in enumerate(message["content"]):
            guarded = block.get("guardContent", {}).get("text")
            if not guarded:
                continue
            if "grounding_source" in guarded.get("qualifiers", []):
                message["content"][i] = {"text": f"<knowledge_base>\n{guarded['text']}\n</knowledge_base>"}
            elif "query" in guarded.get("qualifiers", []):
                guarded["qualifiers"] = ["guard_content"]


async def list_gateway_tools(session: ClientSession) -> list[dict]:
    tools, cursor = [], None
    while True:
        page = await session.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.nextCursor
        if not cursor:
            break
    return [
        # Converse rejects an empty description.
        {"toolSpec": {"name": t.name, "description": t.description or t.name, "inputSchema": {"json": t.inputSchema}}}
        for t in tools
    ]


async def call_gateway_tool(session: ClientSession, name: str, args: dict) -> tuple[str, list[dict]]:
    """Run one tool call. Returns (status, Converse toolResult content)."""
    try:
        result = await session.call_tool(name, args)
    except McpError as e:
        # Policy denials and gateway errors arrive as JSON-RPC errors.
        logger.warning("tool %s failed: %s", name, e.error.message)
        return "error", [{"text": e.error.message}]

    if result.structuredContent is not None:
        content = [{"json": result.structuredContent}]
    else:
        content = [{"text": block.text} for block in result.content if block.type == "text"] or [{"text": ""}]
    return ("error" if result.isError else "success"), content


def converse(messages: list[dict], tool_specs: list[dict], customer_id: str = "") -> dict:
    kwargs = {"toolConfig": {"tools": tool_specs}} if tool_specs else {}
    system = [{"text": SYSTEM_PROMPT}]
    if customer_id:
        # Without this the model invents a customer_id for lookup_order.
        system.append({"text": f"You are talking to the customer with customer_id {customer_id}. "
                               "Use it for every customer_id tool argument and never use a different one."})
    return bedrock.converse(
        modelId=MODEL_ID,
        system=system,
        messages=messages,
        inferenceConfig={"maxTokens": MAX_TOKENS},
        guardrailConfig={
            "guardrailIdentifier": GUARDRAIL_ARN,
            "guardrailVersion": GUARDRAIL_VERSION,
            "trace": "enabled",
        },
        **kwargs,
    )


def policy_session_id(actor_id: str) -> str:
    """Temporal policies (rate limits, refund caps) count events per policy session.

    One session per user, not per conversation, so the hourly and daily limits
    hold across a user's conversations. Sessions expire after 24h idle, which is
    the longest window any policy uses. Allowed: 1-128 of [A-Za-z0-9-].
    """
    return "u-" + hashlib.sha256(f"{POLICY_SET}:{actor_id}".encode()).hexdigest()[:48]


@app.entrypoint
async def invoke(payload: dict, context: RequestContext) -> dict:
    payload = payload or {}
    question = payload.get("prompt", "").strip()
    actor_id = payload.get("actor_id", "")
    if not question:
        return {"error": "payload must include a non-empty 'prompt'"}
    if not ACTOR_ID_RE.match(actor_id):
        return {"error": "payload must include a valid 'actor_id' (letters, digits, '_' or '-')"}
    session_id = context.session_id or "default"

    conv = await asyncio.to_thread(load_session, actor_id, session_id, question)
    history = conv.messages

    tool_calls = []
    async with aws_iam_streamablehttp_client(
        endpoint=GATEWAY_URL,
        aws_service="bedrock-agentcore",
        aws_region=REGION,
        # Required on every request once the policy engine holds a temporal policy.
        headers={"x-amzn-bedrock-agentcore-policy-session-id": policy_session_id(actor_id)},
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            passages, kb_status = await retrieve_passages(session, question)
            tool_calls.append({"tool": KB_SEARCH_TOOL, "input": {"query": question}, "status": kb_status, "prefetch": True})
            # Only the current question is checked for grounding, against its own passages.
            drop_grounding(history)
            append_user(history, user_turn(question, passages, conv.notes))
            conv.notes = ""
            tool_specs = await list_gateway_tools(session)

            for _ in range(MAX_TURNS):
                try:
                    await asyncio.to_thread(wait_for_token_budget)
                except DailyTokenLimit:
                    stop_reason = "daily_token_limit"
                    break
                response = await asyncio.to_thread(converse, history, tool_specs, actor_id)
                await asyncio.to_thread(record_token_usage, response["usage"]["totalTokens"])
                message = response["output"]["message"]
                # Append the assistant turn unchanged (including reasoning blocks).
                history.append(message)
                stop_reason = response["stopReason"]

                if stop_reason != "tool_use":
                    break
                # The answer will rest on tool results, not only on the passages.
                drop_grounding(history)

                results = []
                for block in message["content"]:
                    if "toolUse" not in block:
                        continue
                    use = block["toolUse"]
                    status, content = await call_gateway_tool(session, use["name"], use["input"])
                    tool_calls.append({"tool": use["name"], "input": use["input"], "status": status})
                    results.append({"toolResult": {"toolUseId": use["toolUseId"], "status": status, "content": content}})

                # All results for one assistant turn go back in a single user message.
                append_user(history, {"role": "user", "content": results})
            else:
                stop_reason = "max_turns"

    if stop_reason == "daily_token_limit":
        answer = "I can't help right now because the daily usage limit has been reached. Please try again later."
    else:
        answer = "".join(block.get("text", "") for block in history[-1]["content"]) if history[-1]["role"] == "assistant" else ""
    # Don't feed guardrail-blocked or truncated turns into long-term extraction.
    if answer and stop_reason == "end_turn":
        await asyncio.to_thread(save_turn, actor_id, session_id, question, answer)
    logger.info(json.dumps({"stop_reason": stop_reason, "tool_calls": tool_calls}))
    return {"answer": answer, "stop_reason": stop_reason, "tool_calls": tool_calls}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
