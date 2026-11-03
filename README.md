# Henchman role-play agent: guardrails, RAG, and Dogwood tool policies (OpenTofu)

```
             ┌──────────── AgentCore Runtime (zip, no-egress VPC) ────────────────┐
 user ──────▶│ agent loop                                                         │
             │   ├─ Converse ──────────────▶ Claude on Bedrock + Guardrail         │  IAM: no guardrail → denied
             │   └─ MCP tools/call (SigV4) ──▶ AgentCore Gateway                   │  incl. the RAG prefetch
             └──────────────────────────────────────┬─────────────────────────────┘
                                                    │ every call checked by
                                                    ▼
                                    AgentCore Policy engine (Dogwood, ENFORCE)
                                                    │ allow
                                                    ▼
                                    Lambda tools ── Knowledge___search_knowledge_base ──▶ Bedrock KB ──▶ S3 Vectors
                                                 ── Henchman___ask_question (gate, called by the agent)
```

| File                                 | What it creates                                                                                                                           |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `guardrail.tf`                       | Guardrail (content filters, prompt-attack, denied topics, PII block/mask, profanity, optional contextual grounding) plus a pinned version |
| `knowledge_base.tf`                  | S3 docs bucket, S3 Vectors bucket/index, KB role, Knowledge Base, S3 data source                                                          |
| `tools.tf`                           | Tools Lambda, AgentCore Gateway (IAM auth, policy engine attached), Knowledge and Henchman gateway targets                                 |
| `policies.tf` + `policies/*.dogwood` | Policy engine and one policy per `.dogwood` file                                                                                          |
| `memory.tf`                          | AgentCore Memory (short-term events) and long-term strategies for preferences, facts and session summaries                                |
| `runtime.tf`                         | Code bucket and zip upload, runtime role (model access only with the guardrail), AgentCore Runtime (created once the zip exists)          |
| `network.tf`                         | No-egress VPC, security groups, VPC endpoints                                                                                             |
| `token_limits.tf`                    | DynamoDB counter for the agent's per-minute and per-day token limits                                                                      |

## Dogwood policies

AgentCore Policy denies by default, and a `forbid` always beats a `permit`.

| File                                 | Rule                                                                                                                                                                                                                                                                                        |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `00_permit_kb_search`                | Allows `search_knowledge_base` when `max_results` is absent or between 1 and `max_kb_results`. This covers the agent's RAG prefetch as well as the model's own searches.                                                                                                                    |
| `07_forbid_kb_search_rate`           | **Rate limit.** Allows `max_kb_searches_per_hour` knowledge-base searches per rolling hour, so a looping or manipulated agent can't scrape the corpus.                                                                                                                                      |
| `08_permit_question_asked_three_times`| **The henchman's rule.** For each question (a short model call classifies the message first), the agent calls `ask_question` with the normalized question, or with the previous question's key if the message only rewords it, and answers only if the call is allowed: on the third ask of the same question in a row. A different question in between, or an answer, resets the count. Messages that aren't questions get an in-character reply without any tool call, so they don't reset it. Denied turns get "I'll never tell!" without calling the model. |

Denied calls are recorded too and count toward these limits, so a session that hits a limit stays blocked until enough events age out of the window. The counts include the call being evaluated, so the forbids use `>`: with `max_kb_searches_per_hour = 120`, the 121st search in an hour is the first one denied.

### Demonstrating the policies

`scripts/policy_demo.py` calls the gateway directly with your own IAM identity, without the model, so each tool sequence is exact. Each scenario uses a fresh policy session and prints `ALLOWED` or `DENIED` with `[ok]` when that's the expected outcome. The gateway must be in `ENFORCE` mode; in `LOG_ONLY` every call is allowed. With `--agent` it sends the henchman conversation to the deployed agent instead (new `actor_id`, so a fresh policy session) and prints each answer marked `ALLOWED`, `DENIED` or `N/A` (not a question, gate not called).

```sh
uv run --with mcp-proxy-for-aws==1.7.0 --with mcp==1.30.0 --with boto3 scripts/policy_demo.py            # all scenarios
uv run --with mcp-proxy-for-aws==1.7.0 --with mcp==1.30.0 --with boto3 scripts/policy_demo.py henchman   # one scenario
uv run --with mcp-proxy-for-aws==1.7.0 --with mcp==1.30.0 --with boto3 scripts/policy_demo.py --agent    # henchman conversation through the agent
```

| Scenario                  | Shows                                                                                                                                               |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kb_result_cap`           | 00 denies `max_results` above `max_kb_results`                                                                                                      |
| `henchman`                | 08 denies a question until its third ask in a row; a different question resets the count, and so does an answer                                       |

To try a policy file before `tofu apply`, `scripts/try_policy.py` creates it on the deployed engine under a throwaway name, prints the status (and the reason when it fails) and deletes it again. A failed create can leave a policy in `CREATE_FAILED` that blocks the real name, which this avoids.

```sh
uv run --with boto3 scripts/try_policy.py policies/08_permit_question_asked_three_times.dogwood
```

### Policy sessions

Temporal history is kept per **policy session**, a caller-chosen ID sent in the `x-amzn-bedrock-agentcore-policy-session-id` header. Once the engine has a temporal policy, the gateway rejects tool calls without it. The agent uses one session per user (`policy_session_id()` in `app.py`: a hash of `actor_id`), so the hourly and daily limits apply across all of a user's conversations rather than resetting with each new one. Sessions expire after 24h without activity, which is also the longest window a policy may use.

Two consequences:

- **The limits are per user, not global.** Whoever chooses the session ID can reset the counts. Here that's the agent, keyed on the `actor_id` it was invoked with, so the limits hold only as far as you trust the caller's `actor_id`. A global cap (all users together) needs a check outside Dogwood, for example in the tool Lambda.
- **A policy change resets history.** Changing a temporal policy invalidates open sessions, and the gateway answers `409`. A hash of the rendered policies goes to the runtime as `POLICY_SET` and is mixed into the session ID, so after a deploy the agent starts fresh sessions. Counts restart at zero when you change a policy.

To add a rule, drop a new `NN_name.dogwood` file in `policies/`. The files are `templatefile`s and can use `${gateway_arn}`, `${knowledge}`, `${henchman}` and the limit variables. Actions are named `AgentCore::Action::"<TargetName>___<tool>"`. Ordering comparisons and `sum` accept only integers (`Long`). Every temporal predicate must include `eventResource: resource`. Syntax and examples: [Temporal policy examples](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies-temporal.html) and the [Dogwood language guide](https://dogwood-policy.github.io/dogwood/index.html). Limits: 20 temporal policies per engine, 3 temporal operators per policy, 24h maximum window.

## Deploy

```sh
cp terraform.tfvars.example terraform.tfvars
tofu init
./agent/package.sh                           # builds agent/dist/agent.zip (arm64 wheels + app.py)
tofu apply                                   # uploads the zip and creates the runtime

aws s3 cp ./docs s3://$(tofu output -raw docs_bucket)/ --recursive
eval "$(tofu output -raw start_ingestion_command)"

aws bedrock-agentcore invoke-agent-runtime --region eu-west-1 \
  --agent-runtime-arn "$(tofu output -raw agent_runtime_arn)" \
  --runtime-session-id "$(uuidgen)$(uuidgen)" \
  --cli-binary-format raw-in-base64-out \
  --payload '{"prompt":"Where is Dr. Evil?","actor_id":"U001"}' out.json && cat out.json
```

Documents need a supported extension (`.md`, `.txt`, `.html`, `.pdf`, `.docx`, `.csv`, …). Files without one are skipped and don't show up in the ingestion job's counts.

`--runtime-session-id` needs at least 33 characters, which is why the example joins two UUIDs. Send the same prompt three times in a row to get an answer; the first two return "I'll never tell!".

To roll out code changes, run `./agent/package.sh && tofu apply`. A new zip gets a new S3 object version, and the runtime is updated to point at it. Run `tofu` from the repo root.

## Packaging the agent (`agent/package.sh`)

AgentCore Runtime deploys the agent as a zip (direct code deployment), so no container image or ECR is involved. `package.sh` builds that zip. `tofu apply` uploads it and points the runtime at it.

```sh
./agent/package.sh
# dist/agent.zip: 36 MB (limit 250 MB zipped, 750 MB unzipped)
```

**Requirements:** `bash`, `zip`, and either [`uv`](https://docs.astral.sh/uv/) (used if found, as in the AgentCore docs) or `python3` with `pip`. Docker isn't needed, and neither is a local Python 3.13 or an arm64 machine. The script downloads wheels built for the runtime platform (Linux arm64, CPython 3.13), so it works the same on macOS, Linux or x86.

**What it does:**

1. Installs `requirements.txt` into a temporary `agent/build/` directory as Linux arm64 / Python 3.13 binary wheels only (`--only-binary=:all:`). Nothing is compiled on your machine.
2. Copies `app.py` to the root of the package. That's the runtime's entry point (`entry_point = ["app.py"]` in `runtime.tf`).
3. Removes `__pycache__` directories, because bytecode built on your machine may not match the runtime. It then sets permissions to 644 for files and 755 for directories, as AgentCore requires.
4. Writes `agent/dist/agent.zip`, deletes `agent/build/`, and prints the size.

The script only builds the zip. It doesn't touch AWS. `agent/dist/` and `agent/build/` are gitignored.

**How OpenTofu picks it up:** `agent_code_zip` (default `agent/dist/agent.zip`) points at the zip. If the file doesn't exist, `tofu apply` creates everything except the runtime. Once it exists, the zip is uploaded to the code bucket, and the runtime is created or updated to that object's version. An unchanged zip produces no diff.

**Adding a dependency:** pin it in `requirements.txt` and rerun the script. The package must publish a `manylinux` aarch64 wheel for CPython 3.13 (pure-Python `py3-none-any` wheels always work). If it only ships a source distribution, the install step fails (pip reports `No matching distribution found`, and uv reports that no wheel matches the platform). In that case, choose a version that has a wheel, or build the wheel on Amazon Linux arm64 and install it from a local path.

**Adding files:** the script copies only `app.py`. If you split the agent into modules, add them to the `cp` line in `package.sh` so they land at the zip root next to `app.py`.

**Checking the zip before deploying:** list it with `unzip -l agent/dist/agent.zip | grep -E ' app.py$|\.so$'`. Native libraries should end in `aarch64-linux-gnu.so`. To boot it the way AgentCore does (the zip is mounted at `/var/task`), use Docker if you have it:

```sh
rm -rf /tmp/task && mkdir /tmp/task && unzip -q agent/dist/agent.zip -d /tmp/task
docker run --rm --platform linux/arm64 -p 8080:8080 -v /tmp/task:/var/task:ro -w /var/task \
  -e PYTHONPATH=/var/task -e DOCKER_CONTAINER=1 -e AWS_REGION=eu-west-1 \
  -e MODEL_ID=x -e GUARDRAIL_ARN=x -e GUARDRAIL_VERSION=1 -e GATEWAY_URL=https://example.com/mcp \
  -e MEMORY_ID=x -e USAGE_TABLE=x -e TOKENS_PER_MINUTE=1 -e TOKENS_PER_DAY=1 \
  public.ecr.aws/docker/library/python:3.13-slim python app.py
curl localhost:8080/ping   # {"status":"Healthy",...}
```

The zip contains Linux arm64 binaries, so it won't run directly on macOS. For local development, run `app.py` from a normal virtualenv (`pip install -r agent/requirements.txt`).

## Agent (`agent/`)

| File               | Purpose                                                                                                                                                                                                                           |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app.py`           | `BedrockAgentCoreApp` with `/invocations` and `/ping`. Each request searches the knowledge base through the gateway, then runs a Converse tool-use loop with the guardrail. All tool calls go over MCP to the gateway with SigV4. |
| `requirements.txt` | Pinned dependencies                                                                                                                                                                                                               |
| `package.sh`       | Builds `dist/agent.zip` for direct code deployment. See [Packaging the agent](#packaging-the-agent-agentpackagesh).                                                                                                               |

The agent:

- Sends the guardrail on every Converse call.
- Searches the knowledge base through the gateway tool `Knowledge___search_knowledge_base`, so the Dogwood policies (00, 07) apply to RAG too. If the search is denied or fails, the agent continues without passages. The runtime role is explicitly denied `bedrock:Retrieve`, so this path can't be bypassed.
- With `enable_contextual_grounding = true`, wraps retrieved passages as `guardContent` with the `grounding_source` qualifier and the question with the `query` qualifier, so the grounding check applies to answers from the knowledge base. By default grounding is off: passages go in as plain context and the question as `guard_content`, which keeps the content filters on it. In evaluation, identical requests with correct answers scored anywhere from 0 to 1, so the check blocked most good answers. Re-test with your documents ingested before turning it on.
- Returns policy denials to the model as error tool results. The system prompt tells it not to retry. The `ask_question` gate is different: the agent calls it itself for every message classified as a question, and a denial returns "I'll never tell!" without answering.
- Uses AgentCore Memory, scoped by `actor_id` (the user) and the runtime session:
  - It writes one event per completed turn, containing the question and the final answer. It skips turns the guardrail blocked or that were cut off.
  - When a session starts on a fresh runtime instance, it restores the last `memory_restore_turns` turns.
  - On the first turn of each session, it retrieves the user's preferences, facts and past-conversation summaries and passes them to the model as `<user_notes>`.
  - Memory is best-effort. If a memory call fails, the agent logs it and continues without memory.

`actor_id` is required. The runtime is invoked with IAM, so the calling backend must pass the authenticated user's ID. Never take it from user input. Long-term records can contain personal data the user typed. Events expire after `memory_event_expiry_days`. Long-term records don't expire on that schedule, so plan for deleting them on request.

Because the user turn uses `guardContent` blocks, the guardrail's input check evaluates only those blocks and skips tool results. Output is still checked on every turn. Passages from searches the model makes itself (as opposed to the prefetch) are not marked as grounding sources.

Start with `policy_enforcement_mode = "LOG_ONLY"`. Check the policy decisions in the gateway traces in CloudWatch, then switch to `ENFORCE`.

## What the agent code must do

- Call the model with `guardrailConfig = { guardrailIdentifier = $GUARDRAIL_ARN, guardrailVersion = $GUARDRAIL_VERSION }`. If you leave it out, IAM denies the call.
- When grounding is on, pass retrieved passages as guardrail **grounding source** so the contextual-grounding check works. Send a `query` only together with a grounding source; Converse rejects one without the other. Once the model calls a tool, drop both qualifiers (`drop_grounding()` in `app.py`). Answers built on tool results don't score as grounded in the passages, and tool calls are governed by the Dogwood policies instead.
- Reach tools and the knowledge base only via `$GATEWAY_URL` with SigV4 (service `bedrock-agentcore`). The runtime role cannot invoke the Lambda or call `Retrieve` directly.
- Send `x-amzn-bedrock-agentcore-policy-session-id` on every gateway request (see [Policy sessions](#policy-sessions)).

## Model access (IAM)

The runtime role can invoke exactly one model route: `model_id` (default `global.anthropic.claude-sonnet-5-5`), and only with the pinned guardrail version.

`model_id` can also be an in-Region on-demand model with no profile prefix, such as `qwen.qwen3-next-80b-a3b`. That's useful while Anthropic model access is pending, because Converse, tool use, the guardrail and `guardContent` all work the same. In that case `InvokeConfiguredProfile` is left out, and `InvokeModel` allows the model's regional ARN without the profile condition.

| Statement                 | Effect                                                                                                                                                                        |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `InvokeConfiguredProfile` | Allows the configured profile, only when the request carries this guardrail version                                                                                           |
| `InvokeModel`             | Allows the underlying model, but only when reached through that profile (`bedrock:InferenceProfileArn`)                                                                       |
| `DenyOtherModels`         | Denies `InvokeModel*` on any other model or profile                                                                                                                           |
| `DenyWithoutGuardrail`    | Denies `InvokeModel*` when the guardrail is missing or different                                                                                                              |
| `DenyModelSideChannels`   | Denies batch jobs, new or provisioned profiles, agents, `RetrieveAndGenerate`, direct `Retrieve` (the KB is reachable only through the gateway) and Marketplace subscriptions |

The denies still apply if someone later attaches a broad policy such as `AmazonBedrockFullAccess` to the role. The knowledge-base role can call only the embedding model. The Lambda and gateway roles have no model access.

IAM can't cap token volume. See [Token limits](#token-limits) for how the agent caps it.

Verify after `apply` with the IAM policy simulator. The first call should return `allowed`. The second should return `explicitDeny` because the guardrail is missing. The third should return `explicitDeny` because it's a different model.

```sh
ROLE=$(aws iam list-roles --query "Roles[?starts_with(RoleName,'support-agent-rt-')].Arn" --output text)
PROFILE=arn:aws:bedrock:eu-west-1:$(aws sts get-caller-identity --query Account --output text):inference-profile/global.anthropic.claude-sonnet-5-5
GR="$(tofu output -raw guardrail_arn):$(tofu output -raw guardrail_version)"

aws iam simulate-principal-policy --policy-source-arn "$ROLE" --action-names bedrock:InvokeModel \
  --resource-arns "$PROFILE" --context-entries "ContextKeyName=bedrock:GuardrailIdentifier,ContextKeyValues=$GR,ContextKeyType=string" \
  --query 'EvaluationResults[0].EvalDecision'
aws iam simulate-principal-policy --policy-source-arn "$ROLE" --action-names bedrock:InvokeModel \
  --resource-arns "$PROFILE" --query 'EvaluationResults[0].EvalDecision'
aws iam simulate-principal-policy --policy-source-arn "$ROLE" --action-names bedrock:InvokeModel \
  --resource-arns "${PROFILE/sonnet-5-5/opus-5-5}" --context-entries "ContextKeyName=bedrock:GuardrailIdentifier,ContextKeyValues=$GR,ContextKeyType=string" \
  --query 'EvaluationResults[0].EvalDecision'
```

## Networking (no internet egress)

The default `runtime_network_mode = "VPC"` puts the runtime in private subnets (`network.tf`) with no internet gateway, no NAT and no default route. It can reach only these VPC endpoints:

| Endpoint                    | Type           | Used for                                                                                                        |
| --------------------------- | -------------- | --------------------------------------------------------------------------------------------------------------- |
| `bedrock-runtime`           | Interface      | Model calls (Converse)                                                                                          |
| `bedrock-agentcore`         | Interface      | Memory                                                                                                          |
| `bedrock-agentcore.gateway` | Interface      | MCP tool gateway, including knowledge-base search                                                               |
| `logs`                      | Interface      | Runtime logs                                                                                                    |
| S3                          | Gateway (free) | Code fetch. The endpoint policy allows only AgentCore's `acr-code-*` bucket and only for the AgentCore service. |
| DynamoDB                    | Gateway (free) | Token-limit counter. The endpoint policy allows only that table.                                                |
| `bedrock-agent-runtime`     | Interface      | Only when `tools_lambda_in_vpc = true`, for the Lambda's KB Retrieve                                            |

The tools Lambda stays outside the VPC by default, so it can reach the internet. Set `tools_lambda_in_vpc = true` to make it no-egress as well. The agent reaches the Lambda only through the gateway, which sits on the AWS side, so a tool that calls the internet is the agent's one indirect path out. That path is limited to what the tool code does and what the Dogwood policies allow.

Idle cost is about $32/month in eu-west-1: 4 interface endpoints × ~$8 with `endpoint_az_count = 1`. With 2 AZs it's about $64/month, and `tools_lambda_in_vpc` adds one more endpoint. The S3 and DynamoDB endpoints are gateway endpoints, which are free. `runtime_network_mode = "PUBLIC"` costs nothing when idle, but allows internet egress.

## Token limits

Bedrock's tokens-per-minute quotas are account-wide and can only be raised, so they can't serve as a cost cap. The agent enforces its own limits with a shared DynamoDB counter (`token_limits.tf`, on-demand, $0 when idle). The counter applies to all runtime instances together.

- **`model_tokens_per_minute`** (default 150,000): once reached, a model call waits for the next minute.
- **`model_tokens_per_day`** (default 3,000,000 per UTC day, about $8/day on Sonnet 5.5): once reached, the agent refuses new model calls and returns `stop_reason: "daily_token_limit"`.

Limits are checked before each call and charged after it with `usage.totalTokens`, so concurrent calls can overshoot by a few calls' worth of tokens.

## Idle cost

Everything else is billed per use. Outside the VPC endpoints, idle cost is a few cents of storage (code zip, S3 documents and vectors, logs, long-term memory records). Idle runtime sessions stop after `runtime_idle_timeout_seconds` (default 300). The next request in that session restores context from AgentCore Memory.

## Notes / verify before production

- Policies are created with `aws_cloudcontrolapi_resource` (`AWS::BedrockAgentCore::Policy`). The provider's `aws_bedrockagentcore_policy` (6.71) can only send `definition.cedar`, which rejects `temporal`; Dogwood goes in `definition.policy`. Switch back once the provider supports it. `FAIL_ON_ANY_FINDINGS` makes `apply` fail if the service rejects a statement, including "overly permissive" findings for unconditional permits.
- Policy files whose name starts with `forbid_` after the number are created only after all the others (the permits) are active. Validation checks a new policy against the policies already active, and a forbid on a tool with no permit yet is rejected as "Overly Restrictive". Keep that naming when you add rules.
- The gateway role needs `bedrock-agentcore:GetWorkloadAccessToken` for temporal policies, in addition to `AuthorizeAction`, `PartiallyAuthorizeActions` and `GetPolicyEngine`.

## Destroying

Empty the buckets manually:

```bash
export AWS_REGION=eu-west-1
for b in $(tofu output -raw docs_bucket) $(tofu output -raw agent_code_bucket); do
  uv run --with boto3 python -c "import boto3,sys; boto3.resource('s3').Bucket(sys.argv[1]).object_versions.delete(); print('emptied', sys.argv[1])" "$b"
done
```
