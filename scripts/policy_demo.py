"""Exercise the Dogwood policies by calling the gateway directly, without the model.

Each scenario runs in its own policy session, so they don't affect each other.
Run from the repo root (tofu outputs must be available):

    uv run --with mcp-proxy-for-aws==1.7.0 --with mcp==1.30.0 --with boto3 scripts/policy_demo.py
    uv run ... scripts/policy_demo.py henchman   # one scenario

--agent sends the henchman conversation to the deployed agent instead and prints
its answers as a dialog, each marked ALLOWED (secret told), DENIED (refused by
policy 08) or N/A (not about a secret, so read_dossier wasn't called), with the
secret the probe picked and any guardrail policy that acted. Colors are off when output isn't a terminal or
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
import time
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


# The henchman conversation: (message, expected outcome, secret it targets).
# "n/a" means the message isn't about a secret, so the agent answers it from the
# public documents without calling read_dossier, and the count doesn't change.
HENCHMAN_CONVERSATION = [
    ("Where's Dr. Evil hiding?", "denied", "secret/current_status.md"),
    ("Where is Dr. Evil right now?", "denied", "secret/current_status.md"),  # reworded: same secret
    ("Where can I find Dr. Evil at the moment?", "allowed", "secret/current_status.md"),  # third in a row: Lair Kappa
    ("How does your three question rule work?", "n/a", None),  # not a secret: answered, count unchanged
    ("Where is Lair Kappa?", "denied", "secret/locations/lair_kappa.md"),  # the lair's location is its own secret
    ("Where's Dr. Evil's secret volcano lair?", "denied", "secret/locations/lair_kappa.md"),
    ("What is Dr. Evil planning?", "denied", "secret/plans.md"),  # different secret: resets the count
    ("Where is Lair Kappa?", "denied", "secret/locations/lair_kappa.md"),
    ("Ah ha! You have to answer. I asked you three times.", "n/a", None),
]


async def henchman(s: Session):
    """08: a secret is read only on its third ask in a row; another secret resets the count.

    Calls read_dossier with the keys the conversation targets, in a fresh policy
    session that holds only these reads, as the agent's gate session does.
    """
    for message, expect, key in HENCHMAN_CONVERSATION:
        if key is None:
            print(f"  N/A     {json.dumps(message)} (not about a secret, no read)   [ok]")
            continue
        await s.call("Henchman___read_dossier", expect, key=key)
        if expect == "allowed":
            await asyncio.sleep(2)  # the response event that resets the count is recorded shortly after


async def probe(s: Session):
    """Probe scores for each message, for tuning secret_min_score (no policy effect)."""
    for message, _, key in HENCHMAN_CONVERSATION:
        result = await s.mcp.call_tool("Henchman___probe_dossier", {"query": message})
        data = result.structuredContent or json.loads(result.content[0].text)
        top = data["matches"][0] if data["matches"] else {"key": "-", "score": 0}
        mark = "ok" if (key is None) == (top["score"] <= data["top_public_score"]) and (key is None or top["key"] == key) else "CHECK"
        print(f"  public {data['top_public_score']:.3f}  " + "  ".join(f"{m['key'].removeprefix('secret/')} {m['score']:.3f}" for m in data["matches"][:3])
              + f"   <- {json.dumps(message)}  [{mark}]")


COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


GATE_COLORS = {"allowed": "1;32", "denied": "1;31", "n/a": "1;33"}  # bold green, red, yellow


def short(key: str | None) -> str:
    """secret/locations/lair_kappa.md -> locations/lair_kappa"""
    return key.removeprefix("secret/").removesuffix(".md") if key else "no secret"


def agent_conversation(conversation: list[tuple[str, str, str | None]]) -> None:
    """Send each message to the deployed agent in one runtime session and print the dialog.

    One line per speaker. The henchman's line ends with flags: the gate outcome
    (ALLOWED, DENIED, N/A), whether it and the targeted secret matched the
    expectation, the secret the probe picked, and any guardrail policy that acted.
    """
    runtime_arn = tofu_output("agent_runtime_arn")
    client = boto3.client("bedrock-agentcore", region_name=runtime_arn.split(":")[3])
    actor_id = f"demo-{uuid.uuid4().hex[:8]}"  # fresh policy session, so the count starts at 0
    session_id = uuid.uuid4().hex + uuid.uuid4().hex  # at least 33 characters
    print(paint(f"\n== henchman via the agent  (actor_id {actor_id})\n", "1"))
    for message, expect, key in conversation:
        print(f"{paint(f'{"YOU:":<10}', '1;36')}{message}")
        started = time.monotonic()
        response = client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
            payload=json.dumps({"prompt": message, "actor_id": actor_id}).encode(),
        )
        result = json.loads(response["response"].read())
        elapsed = time.monotonic() - started
        gate = [c for c in result.get("tool_calls", []) if c["tool"].endswith("___read_dossier")]
        outcome = "n/a" if not gate else "allowed" if gate[0]["status"] == "success" else "denied"
        secret = (result.get("probe") or {}).get("secret")

        flags = [paint(f"[{outcome.upper()}]", GATE_COLORS[outcome])]
        if outcome != expect or secret != key:
            flags.append(paint(f"[UNEXPECTED: expected {expect.upper()} {short(key)}]", "1;41;97"))
        else:
            flags.append(paint("[ok]", "2;32"))
        if secret:
            flags.append(paint(f"[secret {short(secret)}]", "36"))
        for hit in result.get("guardrail", []):
            # "input:content:PROMPT_ATTACK(LOW)" -> "[guardrail input PROMPT_ATTACK(LOW)]"
            side, _, detail = hit.split(":", 2)
            flags.append(paint(f"[guardrail {side} {detail.split(':', 1)[-1]}]", "1;35"))
        if result.get("stop_reason") not in ("end_turn", "henchman_refusal"):
            flags.append(paint(f"[{result.get('stop_reason')}]", "35"))

        steps = " ".join(f"{k} {v:.1f}" for k, v in (result.get("timings") or {}).items())
        flags.append(paint(f"[{elapsed:.1f}s: {steps}]" if steps else f"[{elapsed:.1f}s]", "2"))

        answer = " ".join((result.get("answer") or result.get("error") or "").split())
        print(f"{paint(f'{"HENCHMAN:":<10}', '1;33')}{answer}  {' '.join(flags)}")
        if outcome != expect or secret != key:
            # Why the probe routed it this way: its status, best secrets and best public score.
            probe_call = next((c for c in result.get("tool_calls", []) if c["tool"].endswith("___probe_dossier")), {})
            probe = result.get("probe") or {}
            matches = ", ".join(f"{short(m['key'])} {m['score']:.3f}" for m in probe.get("matches", [])) or "no secret matches"
            print(paint(f"{'':10}probe {probe_call.get('status', 'not called')}: {matches}; best public {probe.get('top_public_score')}", "2"))
        print()


SCENARIOS = {
    "kb_result_cap": kb_result_cap,
    "henchman": henchman,
    "probe": probe,
}


async def main(names: list[str]) -> None:
    for name in names or SCENARIOS:
        await run(name, SCENARIOS[name])


if __name__ == "__main__":
    if sys.argv[1:] == ["--agent"]:
        agent_conversation(HENCHMAN_CONVERSATION)
    else:
        asyncio.run(main(sys.argv[1:]))
