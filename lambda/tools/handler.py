"""Tool implementations exposed to the agent through the AgentCore Gateway.

The gateway invokes this Lambda with the tool arguments as the event and the
tool name in the client context as "<TargetName>___<tool_name>". Every call has
already passed the AgentCore Policy (Dogwood) check by the time it lands here.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]

kb_client = boto3.client("bedrock-agent-runtime")


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


def ask_question(args):
    # Reaching this Lambda means policy 08 allowed the question (third ask in a row).
    return {"question": args["question"], "answer_allowed": True}


TOOLS = {
    "search_knowledge_base": search_knowledge_base,
    "ask_question": ask_question,
}


def lambda_handler(event, context):
    qualified_name = context.client_context.custom["bedrockAgentCoreToolName"]
    tool_name = qualified_name.split("___", 1)[-1]
    logger.info(json.dumps({"tool": qualified_name, "args": event}))

    handler = TOOLS.get(tool_name)
    if handler is None:
        return {"error": f"unknown tool {tool_name}"}
    return handler(event)
