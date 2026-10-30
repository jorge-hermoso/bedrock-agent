"""Exercise the Dogwood policies by calling the gateway directly, without the model.

Each scenario runs in its own policy session, so they don't affect each other.
Run from the repo root (tofu outputs must be available):

    uv run --with mcp-proxy-for-aws==1.7.0 --with mcp==1.30.0 scripts/policy_demo.py
    uv run ... scripts/policy_demo.py enumeration   # one scenario

The caller is your own IAM identity. The policies don't restrict the principal,
so they apply to you as they do to the agent; your sessions are separate from
the agent's because the gateway binds each session to the caller.
"""

import asyncio
import json
import subprocess
import sys
import uuid

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

async def refund_without_approval(s: Session):
    """03: issue_refund needs a prior approved request_refund_approval."""
    await s.call("Orders___lookup_order", "allowed", order_id="A1001", customer_id="C001")
    await s.call("Orders___issue_refund", "denied", order_id="A1001", amount_cents=1000)


async def approval_without_lookup(s: Session):
    """02: an approval can only be requested for an order looked up earlier."""
    await s.call("Orders___request_refund_approval", "denied", order_id="A1002", amount_cents=1000, reason="customer request")


async def happy_path_and_one_time_approval(s: Session):
    """02 + 03: lookup -> approval -> refund works once; the approval is then used up."""
    await s.call("Orders___lookup_order", "allowed", order_id="A1001", customer_id="C001")
    await s.call("Orders___request_refund_approval", "allowed", order_id="A1001", amount_cents=3000, reason="customer request")
    await asyncio.sleep(2)  # response events are recorded shortly after the call returns
    await s.call("Orders___issue_refund", "denied", order_id="A1001", amount_cents=4000)  # not the approved amount
    await s.call("Orders___issue_refund", "allowed", order_id="A1001", amount_cents=3000)
    await asyncio.sleep(2)
    await s.call("Orders___issue_refund", "denied", order_id="A1001", amount_cents=3000)  # approval consumed


async def rejected_approval(s: Session):
    """03: an approval that came back approved=false doesn't unlock the refund.

    The stub auto-approves up to auto_approve_refund_cents (5000) and never more
    than the order total (4599), so 4599 is approved and 4600 is not."""
    await s.call("Orders___lookup_order", "allowed", order_id="A1001", customer_id="C001")
    await s.call("Orders___request_refund_approval", "allowed", order_id="A1001", amount_cents=4600, reason="customer request")
    await asyncio.sleep(2)
    await s.call("Orders___issue_refund", "denied", order_id="A1001", amount_cents=4600)


async def kb_result_cap(s: Session):
    """00: max_results above max_kb_results is denied."""
    await s.call("Knowledge___search_knowledge_base", "allowed", query="refund policy", max_results=5)
    await s.call("Knowledge___search_knowledge_base", "denied", query="refund policy", max_results=50)


async def enumeration(s: Session):
    """06: more than max_distinct_customers_per_hour (20) distinct customers is denied."""
    for i in range(1, 21):
        detail = await s.call("Orders___lookup_order", "allowed", order_id="A1001", customer_id=f"C9{i:02d}")
        if detail is None:
            return
    await s.call("Orders___lookup_order", "denied", order_id="A1001", customer_id="C001")  # 21st distinct
    # The denied lookup still counts as a request event, so the session now has 21
    # distinct customers and stays locked until the oldest ages out of the hour.
    await s.call("Orders___lookup_order", "denied", order_id="A1001", customer_id="C901")


SCENARIOS = {
    "refund_without_approval": refund_without_approval,
    "approval_without_lookup": approval_without_lookup,
    "happy_path": happy_path_and_one_time_approval,
    "rejected_approval": rejected_approval,
    "kb_result_cap": kb_result_cap,
    "enumeration": enumeration,
}


async def main(names: list[str]) -> None:
    for name in names or SCENARIOS:
        await run(name, SCENARIOS[name])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
