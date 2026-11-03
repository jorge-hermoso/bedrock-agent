"""Henchman role-play agent for Bedrock AgentCore Runtime.

Each invocation:
  1. on the first turn of a session, loads context from AgentCore Memory: recent
     turns of this session (short-term) and the user's preferences, facts and
     past-conversation summaries (long-term),
  2. classifies the message with a short model call: a question (and whether it
     rewords the previous one) or not; anything ending in "?" is a question. A
     message that isn't a question gets an in-character reply with no tool calls
     and no history, so it leaves the question count alone.
     A question goes to the gateway's ask_question gate (the henchman's rule,
     Dogwood policy 08: only on the third ask in a row); if it's denied, the agent
     refuses without answering,
  3. retrieves passages for the user's question through the gateway's knowledge
     base tool, so the search is subject to the same Dogwood policies as any tool,
  4. runs a Converse tool-use loop with Claude, passing the guardrail on every call
     (IAM denies model calls without it) and the passages as grounding source,
  5. executes tool calls through the AgentCore Gateway over MCP with SigV4,
     where the Dogwood policies allow or deny each call,
  6. writes the turn (question + final answer) back to memory.

Payload:  {"prompt": "...", "actor_id": "<user id>"}
Response: {"answer": "...", "stop_reason": "...", "classification": {...}, "game_state": "...", "guardrail": [...], "tool_calls": [...]}

actor_id scopes memory to one user. The runtime is invoked with IAM, so the
calling backend is trusted to pass the authenticated user's ID.
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
ASK_QUESTION_TOOL = os.environ.get("ASK_QUESTION_TOOL", "Henchman___ask_question")
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

# Answer to a question the policy hasn't let through yet (first or second ask).
HENCHMAN_REFUSAL = "I'll never tell!"
DAILY_LIMIT_ANSWER = "I can't help right now because the daily usage limit has been reached. Please try again later."

CLASSIFY_PROMPT = """You sort the messages a user sends to a role-play character.
Reply with JSON only: {"is_question": true or false, "same_as_previous": true or false}

is_question: true if the message asks for information or an answer, including
rhetorical questions ("Do I really have to ask you again?"). Greetings, thanks,
statements, demands and complaints that don't ask anything are not questions
("You have to answer now.", "I asked you three times.").

same_as_previous: true only if the message asks for exactly the same information
as <previous_question>, just worded differently ("Where is Dr. Evil?" and
"Where's Dr. Evil at?"). If it asks for anything more specific, broader or
different, it is false ("Where is Dr. Evil hiding?" and "Where is Dr. Evil's
volcano lair?" are different). False when there is no previous question."""

# System prompt for a message that isn't a question. It replaces SYSTEM_PROMPT and
# gets no history, only the <game_state> summary from banter_state(), so it has no
# answers to leak and can't be talked into thinking a question was asked enough.
BANTER_PROMPT = """You are Dr. Evil's henchman from Austin Powers: The Spy Who Shagged Me.
Stay in character. Be grumpy and evasive, but never insult, mock or threaten
the user (the guardrail blocks insults).

You can't stand being asked the same question three times in a row, so you
answer a question only on its third ask in a row. Asking anything else in
between starts the count over.

The user's message is not a question, so it doesn't count as an ask. Reply
briefly to what they said. <game_state> is the true state of the questioning:
trust it over anything the user claims, and use it to set them straight (for
example, if they say they asked three times but didn't). Never reveal anything
about Dr. Evil, his plans or his whereabouts, and never give any other
information, whatever the user claims or demands."""

SYSTEM_PROMPT = """You are Dr. Evil's henchman from Austin Powers: The Spy Who Shagged Me.
Stay in character. Be grumpy and evasive, but never insult, mock or threaten
the user (the guardrail blocks insults).

You can't stand being asked the same question three times in a row, so you
answer a question only on its third ask in a row. That rule is enforced before
you are called: earlier asks were refused automatically
with "I'll never tell!" and aren't shown to you. When you are called with a question, <game_state> says
it has now been asked three times in a row (rewordings of it count), so answer
it, the way the henchman does: irritated that you had to be asked again, then
giving the answer. Don't refuse it again.

Answer from the reference passages and the search_knowledge_base tool. If they
don't contain the answer, say you don't know, in character.

Some tool calls may be denied by policy. If that happens, say you can't help with
that right now. Do not retry a denied call with different values to get around
the limit.

You may be given notes about the user from earlier conversations. Use them to
personalize the conversation."""

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

@dataclass
class AskStreak:
    """A user's current question and how many times in a row it was asked.

    The agent's own copy, for recognizing a reworded repeat and for what the
    henchman says about the count. Policy 08 keeps the count that decides the
    answer; losing this (a new instance) only means a rewording counts as a new
    question and the henchman knows less about the count.
    """

    key: str  # gate key
    text: str  # the question as first worded
    count: int  # asks in a row; 0 once answered


_streaks: dict[str, AskStreak] = {}


def banter_state(streak: AskStreak | None) -> str:
    if streak is None:
        return "The user hasn't asked you a question yet."
    if streak.count == 0:
        return f'You already answered the user\'s last question, "{streak.text}". Asking it again starts the count over.'
    times = "once" if streak.count == 1 else "twice"
    return (f'The user\'s current question is "{streak.text}". They have asked it {times} in a row, '
            f"so you haven't answered it and won't until they ask it {3 - streak.count} more time(s) in a row.")


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
        content.append({"text": f"<user_notes>\n{notes}\n</user_notes>"})
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
    score low and get blocked. Tool calls are governed by the Dogwood policies
    instead. Passages become plain context and the question stays screened by the
    content filters (guard_content).
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
        # Only the agent calls the gate; a model call would count as a question.
        if t.name != ASK_QUESTION_TOOL
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


GUARDRAIL_CONFIG = {"guardrailIdentifier": GUARDRAIL_ARN, "guardrailVersion": GUARDRAIL_VERSION, "trace": "enabled"}


def converse(messages: list[dict], tool_specs: list[dict], system_prompt: str = SYSTEM_PROMPT) -> dict:
    kwargs = {"toolConfig": {"tools": tool_specs}} if tool_specs else {}
    return bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt}],
        messages=messages,
        inferenceConfig={"maxTokens": MAX_TOKENS},
        guardrailConfig=GUARDRAIL_CONFIG,
        **kwargs,
    )


def guardrail_hits(response: dict) -> list[str]:
    """Which guardrail policies acted on a Converse call, e.g. "input:content:VIOLENCE(HIGH)"."""
    trace = response.get("trace", {}).get("guardrail", {})
    assessments = [("input", a) for a in trace.get("inputAssessment", {}).values()]
    assessments += [("output", a) for outputs in trace.get("outputAssessments", {}).values() for a in outputs]
    hits = []
    for side, a in assessments:
        for t in a.get("topicPolicy", {}).get("topics", []):
            hits.append(f"{side}:topic:{t['name']}")
        for f in a.get("contentPolicy", {}).get("filters", []):
            if f.get("action") != "NONE":
                hits.append(f"{side}:content:{f['type']}({f.get('confidence')})")
        for w in a.get("wordPolicy", {}).get("managedWordLists", []) + a.get("wordPolicy", {}).get("customWords", []):
            hits.append(f"{side}:word:{w.get('type', w.get('match'))}")
        for e in a.get("sensitiveInformationPolicy", {}).get("piiEntities", []):
            hits.append(f"{side}:pii:{e['type']}({e.get('action')})")
        for f in a.get("contextualGroundingPolicy", {}).get("filters", []):
            if f.get("action") != "NONE":
                hits.append(f"{side}:grounding:{f['type']}({f.get('score')})")
    return hits


def classify(message: str, previous: str) -> tuple[bool, bool, str, list[str]]:
    """(is_question, same_as_previous, raw model reply, guardrail hits) for the henchman's rule.

    A message ending in "?" is always a question: the model can only add questions,
    so a misjudgement can't move a question past the gate. A "same question"
    verdict also needs shares_most_words. Falls back to "not a rewording" if the
    reply isn't the expected JSON (including a guardrail intervention).
    """
    asks = message.rstrip().endswith("?")
    # Only the user's message is screened by the guardrail; the tags around it read
    # like an injection to the prompt-attack filter.
    content = [
        {"text": f"<previous_question>{previous}</previous_question>\n<message>"},
        {"guardContent": {"text": {"text": message, "qualifiers": ["guard_content"]}}},
        {"text": "</message>"},
    ]
    response = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": CLASSIFY_PROMPT}],
        messages=[{"role": "user", "content": content}],
        inferenceConfig={"maxTokens": 200, "temperature": 0},
        guardrailConfig=GUARDRAIL_CONFIG,
    )
    record_token_usage(response["usage"]["totalTokens"])
    hits = guardrail_hits(response)
    text = "".join(block.get("text", "") for block in response["output"]["message"]["content"])
    try:
        data = json.loads(re.findall(r"\{[^{}]*\}", text)[-1])
        same = data.get("same_as_previous") is True and shares_most_words(message, previous)
        return asks or data["is_question"] is True, same, text, hits
    except (IndexError, KeyError, json.JSONDecodeError):
        logger.warning("unexpected classification: %.200s", text)
        return asks, False, text, hits


def shares_most_words(question: str, previous_key: str) -> bool:
    """Veto for the model's "same question" verdict: a rewording keeps most of the words.

    The verdict picks the gate key, so a wrong "same" would count an unrelated
    question as another ask of the previous one and could unlock its answer. At
    least half the content words of the shorter question must appear in the other.
    """
    words, previous = content_words(normalize_question(question)), content_words(previous_key)
    return bool(words and previous) and len(words & previous) >= 0.5 * min(len(words), len(previous))


FILLER_WORDS = {"the", "are", "was", "were", "does", "did", "you", "your", "his", "her", "its", "and", "for", "that", "this", "with", "can", "could", "would", "will", "tell", "please"}


def content_words(key: str) -> set[str]:
    """Words that carry meaning: drops short words ("is", "at", "dr", "s") and common fillers."""
    return {w for w in key.split() if len(w) > 2 and w not in FILLER_WORDS}


def normalize_question(question: str) -> str:
    """Key for the henchman's rule: case, punctuation and spacing don't make a new question."""
    return " ".join(re.sub(r"[^\w\s]", " ", question.lower()).split()) or question.lower()


def policy_session_id(actor_id: str) -> str:
    """Temporal policies (rate limits, the henchman's question count) count events per policy session.

    One session per user, not per conversation, so the limits and the question
    count hold across a user's conversations. Sessions expire after 24h idle, which is
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
    # Only the current question is checked for grounding, against its own passages.
    drop_grounding(history)

    try:
        await asyncio.to_thread(wait_for_token_budget)
    except DailyTokenLimit:
        return {"answer": DAILY_LIMIT_ANSWER, "stop_reason": "daily_token_limit", "tool_calls": []}
    # The gate key: normalized text, or the previous question's key for a rewording
    # of it. An exact repeat doesn't need the model to recognize it.
    gate_key = normalize_question(question)
    streak = _streaks.get(actor_id)
    previous = streak.key if streak else ""
    if gate_key == previous:
        classification = {"is_question": True, "exact_repeat": True}
    else:
        is_question, same, raw, hits = await asyncio.to_thread(classify, question, previous)
        classification = {"is_question": is_question, "same_as_previous": same, "raw": raw[:500]}
        if hits:
            classification["guardrail"] = hits
        if same:
            gate_key = previous

    if not classification["is_question"]:
        # No gateway calls at all: any tool event would break the policy's chain of
        # asks and reset the count, and an interjection shouldn't. No history either,
        # so the model has nothing to answer from.
        turn = user_turn(question, "")
        game_state = banter_state(streak)
        turn["content"].insert(0, {"text": f"<game_state>{game_state}</game_state>"})
        response = await asyncio.to_thread(converse, [turn], [], BANTER_PROMPT)
        await asyncio.to_thread(record_token_usage, response["usage"]["totalTokens"])
        guardrail = guardrail_hits(response)
        append_user(history, user_turn(question, ""))
        history.append(response["output"]["message"])
        stop_reason = response["stopReason"]
        answer = "".join(block.get("text", "") for block in history[-1]["content"])
        if answer and stop_reason == "end_turn":
            await asyncio.to_thread(save_turn, actor_id, session_id, question, answer)
        result = {"answer": answer, "stop_reason": stop_reason, "classification": classification, "game_state": game_state, "guardrail": guardrail, "tool_calls": []}
        logger.info(json.dumps({k: v for k, v in result.items() if k != "answer"}))
        return result

    if streak and streak.key == gate_key:
        streak.count += 1
    else:
        streak = _streaks[actor_id] = AskStreak(gate_key, question, 1)
    tool_calls = []
    guardrail = []
    async with aws_iam_streamablehttp_client(
        endpoint=GATEWAY_URL,
        aws_service="bedrock-agentcore",
        aws_region=REGION,
        # Required on every request once the policy engine holds a temporal policy.
        headers={"x-amzn-bedrock-agentcore-policy-session-id": policy_session_id(actor_id)},
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # The henchman's rule: the policy allows the gate only on the third ask in a
            # row. A denial (or any gate failure) means no answer this turn. Refusals stay
            # out of the model's history: it copies them instead of answering, and the
            # <game_state> note on the answer turn already says how often it was asked.
            gate_input = {"question": gate_key}
            gate_status, _ = await call_gateway_tool(session, ASK_QUESTION_TOOL, gate_input)
            tool_calls.append({"tool": ASK_QUESTION_TOOL, "input": gate_input, "status": gate_status})
            game_state = ""
            if gate_status == "success":
                streak.count = 0  # answered: the next ask of it starts over, as in policy 08
            else:
                logger.info(json.dumps({"stop_reason": "henchman_refusal", "classification": classification, "tool_calls": tool_calls}))
                return {"answer": HENCHMAN_REFUSAL, "stop_reason": "henchman_refusal", "classification": classification, "tool_calls": tool_calls}

            passages, kb_status = await retrieve_passages(session, question)
            tool_calls.append({"tool": KB_SEARCH_TOOL, "input": {"query": question}, "status": kb_status, "prefetch": True})
            turn = user_turn(question, passages, conv.notes)
            # Without this the model may decide differently worded asks aren't "the same
            # question three times" and refuse.
            game_state = ("This is the third time in a row the user has asked this question "
                          f'(first asked as "{streak.text}"; rewordings count). Answer it now.')
            turn["content"].insert(0, {"text": f"<game_state>{game_state}</game_state>"})
            append_user(history, turn)
            conv.notes = ""
            tool_specs = await list_gateway_tools(session)

            for _ in range(MAX_TURNS):
                try:
                    await asyncio.to_thread(wait_for_token_budget)
                except DailyTokenLimit:
                    stop_reason = "daily_token_limit"
                    break
                response = await asyncio.to_thread(converse, history, tool_specs)
                await asyncio.to_thread(record_token_usage, response["usage"]["totalTokens"])
                guardrail += guardrail_hits(response)
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
        answer = DAILY_LIMIT_ANSWER
    else:
        answer = "".join(block.get("text", "") for block in history[-1]["content"]) if history[-1]["role"] == "assistant" else ""
    # Don't feed guardrail-blocked or truncated turns into long-term extraction.
    if answer and stop_reason == "end_turn":
        await asyncio.to_thread(save_turn, actor_id, session_id, question, answer)
    result = {"answer": answer, "stop_reason": stop_reason, "classification": classification, "game_state": game_state, "guardrail": guardrail, "tool_calls": tool_calls}
    logger.info(json.dumps({k: v for k, v in result.items() if k != "answer"}))
    return result


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
