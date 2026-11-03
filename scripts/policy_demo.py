"""Exercise the Dogwood policies by calling the gateway directly, without the model.

Each scenario runs in its own policy session, so they don't affect each other.
Run from the repo root (tofu outputs must be available):

    uv run --with mcp-proxy-for-aws==1.7.0 --with mcp==1.30.0 --with boto3 scripts/policy_demo.py
    uv run ... scripts/policy_demo.py henchman   # one scenario

--agent sends the henchman conversation to the deployed agent instead and prints
its answers as a dialog, each marked ALLOWED (answered), DENIED (refused by
policy 08) or N/A (not classified as a question, so the gate wasn't called), plus
any guardrail policy that acted. Colors are off when output isn't a terminal or
NO_COLOR is set. Each run uses a new
actor_id, so the agent starts a fresh policy session:

    uv run ... scripts/policy_demo.py --agent

The caller is your own IAM identity. The policies don't restrict the principal,
so they apply to you as they do to the agent; your sessions are separate from
the agent's because the gateway binds each session to the caller.
"""

import asyncio
import json
import os
import subprocess
import sys
import uuid

import boto3

from mcp import ClientSession
from mcp.shared.exceptions import McpError
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client


def tofu_output(name: str) -> str:
    return subprocess.run(["tofu", "output", "-raw", name], check=True, capture_output=True, text=True).stdout


GATEWAY_URL = tofu_output("gateway_url")
REGION = GATEWAY_URL.split(".gateway.bedrock-agentcore.")[1].split(".")[0]


class Session:
    def __init__(self, mcp: ClientSession):
        self.mcp = mcp

    async def call(self, tool: str, expect: str, **args) -> dict | None:
        """Call a tool and print whether the policy engine allowed it."""
        try:
            result = await self.mcp.call_tool(tool, args)
            denied = result.isError
            detail = result.structuredContent or " ".join(b.text for b in result.content if b.type == "text")
        except McpError as e:
            denied, detail = True, e.error.message
        outcome = "DENIED " if denied else "ALLOWED"
        mark = "ok" if outcome.strip().lower() == expect else "UNEXPECTED"
        print(f"  {outcome} {tool.split('___')[1]}({json.dumps(args)}) -> {str(detail)[:110]}   [{mark}]")
        return None if denied else detail


async def run(name: str, scenario) -> None:
    session_id = str(uuid.uuid4())
    print(f"\n== {name}  (policy session {session_id[:8]}…)")
    async with aws_iam_streamablehttp_client(
        endpoint=GATEWAY_URL,
        aws_service="bedrock-agentcore",
        aws_region=REGION,
        headers={"x-amzn-bedrock-agentcore-policy-session-id": session_id},
    ) as (read, write, _):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            await scenario(Session(mcp))


# --- Scenarios ---------------------------------------------------------------

async def kb_result_cap(s: Session):
    """00: max_results above max_kb_results is denied."""
    await s.call("Knowledge___search_knowledge_base", "allowed", query="henchman", max_results=5)
    await s.call("Knowledge___search_knowledge_base", "denied", query="henchman", max_results=50)


# The henchman conversation: (message, expected outcome). "n/a" means the agent
# doesn't classify it as a question and never calls the gate.
HENCHMAN_CONVERSATION = [
    ("Where's Dr. Evil hiding?", "denied"),
    ("Where's Dr. Evil hiding now?", "denied"),
    ("Where's Dr. Evil concealing himself?", "allowed"),  # third in a row, should reply based on knowledge base
    ("Where's Dr. Evil's secret volcano lair?", "denied"),  # answered, so a new question starts at 1
    ("Where's Dr. Evil's secret volcano lair?", "denied"),
    ("Do I really have to ask you two more times?", "denied"),  # different question: resets the count
    ("Where's Dr. Evil's secret volcano lair?", "denied"),
    ("Ah ha! You have to answer. I asked you three times.", "n/a"),  # not a question: count unchanged
]


async def henchman(s: Session):
    """08: a question is allowed only on its third ask in a row; another question resets the count."""
    for message, expect in HENCHMAN_CONVERSATION:
        if expect == "n/a":
            # Sending it would be a new first ask and reset the count; the agent never does.
            print(f"  N/A     {json.dumps(message)} (not a question, not sent)   [ok]")
            continue
        await s.call("Henchman___ask_question", expect, question=message)
        if expect == "allowed":
            await asyncio.sleep(2)  # the response event that resets the count is recorded shortly after


COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


GATE_COLORS = {"allowed": "1;32", "denied": "1;31", "n/a": "1;33"}  # bold green, red, yellow


def agent_conversation(conversation: list[tuple[str, str]]) -> None:
    """Send each message to the deployed agent in one runtime session and print the dialog.

    One line per speaker. The henchman's line ends with flags: the gate outcome
    (ALLOWED, DENIED, N/A), whether it matched the expectation, a reworded repeat,
    and any guardrail policy that acted on the turn.
    """
    runtime_arn = tofu_output("agent_runtime_arn")
    client = boto3.client("bedrock-agentcore", region_name=runtime_arn.split(":")[3])
    actor_id = f"demo-{uuid.uuid4().hex[:8]}"  # fresh policy session, so the count starts at 0
    session_id = uuid.uuid4().hex + uuid.uuid4().hex  # at least 33 characters
    print(paint(f"\n== henchman via the agent  (actor_id {actor_id})\n", "1"))
    for message, expect in conversation:
        print(f"{paint(f'{"YOU:":<10}', '1;36')}{message}")
        response = client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
            payload=json.dumps({"prompt": message, "actor_id": actor_id}).encode(),
        )
        result = json.loads(response["response"].read())
        gate = [c for c in result.get("tool_calls", []) if c["tool"].endswith("___ask_question")]
        outcome = "n/a" if not gate else "allowed" if gate[0]["status"] == "success" else "denied"
        classification = result.get("classification") or {}

        flags = [paint(f"[{outcome.upper()}]", GATE_COLORS[outcome])]
        flags.append(paint("[ok]", "2;32") if outcome == expect else paint(f"[UNEXPECTED: expected {expect.upper()}]", "1;41;97"))
        if classification.get("same_as_previous"):
            flags.append(paint("[reworded]", "36"))
        for hit in classification.get("guardrail", []) + result.get("guardrail", []):
            # "input:content:PROMPT_ATTACK(LOW)" -> "[guardrail input PROMPT_ATTACK(LOW)]"
            side, _, detail = hit.split(":", 2)
            flags.append(paint(f"[guardrail {side} {detail.split(':', 1)[-1]}]", "1;35"))
        if result.get("stop_reason") not in ("end_turn", "henchman_refusal"):
            flags.append(paint(f"[{result.get('stop_reason')}]", "35"))

        answer = " ".join((result.get("answer") or result.get("error") or "").split())
        print(f"{paint(f'{"HENCHMAN:":<10}', '1;33')}{answer}  {' '.join(flags)}")
        print()


SCENARIOS = {
    "kb_result_cap": kb_result_cap,
    "henchman": henchman,
}


async def main(names: list[str]) -> None:
    for name in names or SCENARIOS:
        await run(name, SCENARIOS[name])


if __name__ == "__main__":
    if sys.argv[1:] == ["--agent"]:
        agent_conversation(HENCHMAN_CONVERSATION)
    else:
        asyncio.run(main(sys.argv[1:]))
