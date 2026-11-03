"""Try Dogwood policy files against the deployed policy engine without tofu apply.

Each file is rendered with this deployment's values, created under a throwaway
name, polled until it leaves CREATING, reported (status and reasons), and
deleted again, so a failed attempt never leaves a policy that blocks the real one.

    uv run --with boto3 scripts/try_policy.py policies/08_permit_question_asked_three_times.dogwood
    uv run --with boto3 scripts/try_policy.py --ignore-findings policies/experiments/*.dogwood

--ignore-findings skips the overly permissive/restrictive analysis
(IGNORE_ALL_FINDINGS); OpenTofu uses FAIL_ON_ANY_FINDINGS.
Run from the repo root with AWS credentials for the deployment (AWS_PROFILE).
"""

import argparse
import re
import subprocess
import time
import uuid

import boto3


def tofu_output(name: str) -> str:
    return subprocess.run(["tofu", "output", "-raw", name], check=True, capture_output=True, text=True).stdout


def render(path: str, values: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        if m.group(1) not in values:
            raise SystemExit(f"{path}: no value for ${{{m.group(1)}}}")
        return values[m.group(1)]

    return re.sub(r"\$\{(\w+)\}", sub, open(path).read())


def try_policy(client, engine_id: str, path: str, statement: str, validation_mode: str) -> None:
    name = "try_" + uuid.uuid4().hex[:12]
    print(f"\n== {path}  ({validation_mode}, {name})")
    try:
        policy_id = client.create_policy(
            policyEngineId=engine_id,
            name=name,
            validationMode=validation_mode,
            definition={"policy": {"statement": statement}},
        )["policyId"]
    except client.exceptions.ClientError as e:
        print(f"  REJECTED  {e.response['Error']['Message']}")
        return

    deadline = time.time() + 20 * 60
    while True:
        policy = client.get_policy(policyEngineId=engine_id, policyId=policy_id)
        if policy["status"] != "CREATING" or time.time() > deadline:
            break
        time.sleep(10)
    print(f"  {policy['status']}")
    for reason in policy.get("statusReasons", []):
        print(f"    {reason}")
    client.delete_policy(policyEngineId=engine_id, policyId=policy_id)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+")
    parser.add_argument("--ignore-findings", action="store_true")
    args = parser.parse_args()

    gateway_arn = tofu_output("gateway_arn")
    region = gateway_arn.split(":")[3]
    values = {
        "gateway_arn": gateway_arn,
        "knowledge": "Knowledge",
        "henchman": "Henchman",
        "max_kb_results": "10",
        "max_kb_searches_per_hour": "120",
        "question_repeat_window": "24h",
    }
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    engine_id = tofu_output("policy_engine_id")
    mode = "IGNORE_ALL_FINDINGS" if args.ignore_findings else "FAIL_ON_ANY_FINDINGS"
    for path in args.files:
        try_policy(client, engine_id, path, render(path, values), mode)


if __name__ == "__main__":
    main()
