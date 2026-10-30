"""Tool implementations exposed to the agent through the AgentCore Gateway.

The gateway invokes this Lambda with the tool arguments as the event and the
tool name in the client context as "<TargetName>___<tool_name>". Every call has
already passed the AgentCore Policy (Dogwood) check by the time it lands here.

The order/refund tools are stubs: replace the in-memory data with calls to your
order system and route request_refund_approval to a real approver.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
AUTO_APPROVE_REFUND_CENTS = int(os.environ["AUTO_APPROVE_REFUND_CENTS"])

kb_client = boto3.client("bedrock-agent-runtime")

ORDERS = {
    "A1001": {"customer_id": "C001", "status": "delivered", "total_cents": 4599},
    "A1002": {"customer_id": "C002", "status": "shipped", "total_cents": 12900},
}


def search_knowledge_base(args):
    response = kb_client.retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": args["query"]},
        retrievalConfiguration={
            # Policy 00 already bounds max_results; clamp again in case the policy changes.
            "vectorSearchConfiguration": {"numberOfResults": min(max(int(args.get("max_results", 5)), 1), 10)}
        },
    )
    return {
        "results": [
            {
                "text": r["content"]["text"],
                "score": r.get("score"),
                "source": r.get("location", {}).get("s3Location", {}).get("uri"),
            }
            for r in response["retrievalResults"]
        ]
    }


def lookup_order(args):
    order = ORDERS.get(args["order_id"])
    if order is None or order["customer_id"] != args["customer_id"]:
        return {"found": False}
    return {"found": True, "order_id": args["order_id"], **order}


def request_refund_approval(args):
    order = ORDERS.get(args["order_id"])
    amount = int(args["amount_cents"])
    approved = (
        order is not None
        and amount <= order["total_cents"]
        and amount <= AUTO_APPROVE_REFUND_CENTS
    )
    return {
        "approved": approved,
        "order_id": args["order_id"],
        "amount_cents": amount,
        "reason": "auto-approved" if approved else "needs human review",
    }


def issue_refund(args):
    return {
        "status": "refunded",
        "order_id": args["order_id"],
        "amount_cents": int(args["amount_cents"]),
    }


TOOLS = {
    "search_knowledge_base": search_knowledge_base,
    "lookup_order": lookup_order,
    "request_refund_approval": request_refund_approval,
    "issue_refund": issue_refund,
}


def lambda_handler(event, context):
    qualified_name = context.client_context.custom["bedrockAgentCoreToolName"]
    tool_name = qualified_name.split("___", 1)[-1]
    logger.info(json.dumps({"tool": qualified_name, "args": event}))

    handler = TOOLS.get(tool_name)
    if handler is None:
        return {"error": f"unknown tool {tool_name}"}
    return handler(event)
