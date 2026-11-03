"""Tool implementations exposed to the agent through the AgentCore Gateway.

The gateway invokes this Lambda with the tool arguments as the event and the
tool name in the client context as "<TargetName>___<tool_name>". Every call has
already passed the AgentCore Policy (Dogwood) check by the time it lands here.

Documents under secret/ in the docs bucket are Dr. Evil's secrets. Their text
only leaves through read_dossier, which policy 08 allows on the third ask in a
row; search_knowledge_base never returns them and probe_dossier returns only
which secrets match, not what they say.
"""

import json
import logging
import os
import re

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
DOCS_BUCKET = os.environ["DOCS_BUCKET"]
SECRET_PREFIX = "secret/"
# One secret per file (the data source doesn't chunk), so a key is a file path.
SECRET_KEY_RE = re.compile(r"^secret/[a-z0-9_]+(/[a-z0-9_]+)*\.md$")
# The corpus is small; retrieving this many covers every document.
RETRIEVE_ALL = 25

kb_client = boto3.client("bedrock-agent-runtime")
s3 = boto3.client("s3")


def _retrieve(query: str) -> list[dict]:
    response = kb_client.retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": RETRIEVE_ALL}},
    )
    results = []
    for r in response["retrievalResults"]:
        uri = r.get("location", {}).get("s3Location", {}).get("uri", "")
        key = uri.removeprefix(f"s3://{DOCS_BUCKET}/")
        results.append({"text": r["content"]["text"], "score": r.get("score"), "key": key})
    return results


def search_knowledge_base(args):
    # Policy 00 already bounds max_results; clamp again in case the policy changes.
    limit = min(max(int(args.get("max_results", 5)), 1), 10)
    public = [r for r in _retrieve(args["query"]) if not r["key"].startswith(SECRET_PREFIX)]
    return {"results": [{"text": r["text"], "score": r["score"], "source": r["key"]} for r in public[:limit]]}


def probe_dossier(args):
    """Which secrets match the query, best first, and the best public score. No text."""
    results = _retrieve(args["query"])
    public = [r["score"] for r in results if not r["key"].startswith(SECRET_PREFIX)]
    return {
        "matches": [{"key": r["key"], "score": r["score"]} for r in results if r["key"].startswith(SECRET_PREFIX)][:5],
        "top_public_score": max(public, default=0.0),
    }


def read_dossier(args):
    # Reaching this Lambda means policy 08 allowed it (third ask in a row for this key).
    key = args["key"]
    if not SECRET_KEY_RE.match(key):
        return {"error": f"not a secret: {key}"}
    body = s3.get_object(Bucket=DOCS_BUCKET, Key=key)["Body"].read().decode()
    return {"key": key, "text": body}


TOOLS = {
    "search_knowledge_base": search_knowledge_base,
    "probe_dossier": probe_dossier,
    "read_dossier": read_dossier,
}


def lambda_handler(event, context):
    qualified_name = context.client_context.custom["bedrockAgentCoreToolName"]
    tool_name = qualified_name.split("___", 1)[-1]
    logger.info(json.dumps({"tool": qualified_name, "args": event}))

    handler = TOOLS.get(tool_name)
    if handler is None:
        return {"error": f"unknown tool {tool_name}"}
    return handler(event)
