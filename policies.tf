# AgentCore Policy engine with Dogwood policies.
#
# Dogwood extends Cedar with `temporal { ... }` conditions that look back over
# the agent's earlier tool requests/responses (prerequisites, ordering, rate
# limits, rolling sums). AgentCore Policy accepts Dogwood in the same
# definition.cedar.statement field it uses for plain Cedar, and plain Cedar
# policies keep working unchanged.
#
# The engine is attached to the gateway in tools.tf, so every tool call goes
# through these rules. Default is deny: a tool with no matching permit is blocked,
# and any matching forbid wins over a permit.

resource "aws_bedrockagentcore_policy_engine" "tools" {
  name        = "${var.name}_policy_engine"
  description = "Tool-call governance for ${var.name}"
}

locals {
  policy_vars = {
    gateway_arn                     = aws_bedrockagentcore_gateway.tools.gateway_arn
    knowledge                       = local.knowledge_target
    orders                          = local.orders_target
    approval_window                 = var.approval_window
    max_refund_cents                = var.max_refund_cents
    max_refunds_per_hour            = var.max_refunds_per_hour
    max_refund_total_cents_per_day  = var.max_refund_total_cents_per_day
    max_distinct_customers_per_hour = var.max_distinct_customers_per_hour
    max_kb_results                  = var.max_kb_results
    max_kb_searches_per_hour        = var.max_kb_searches_per_hour
  }

  # policies/NN_snake_name.dogwood -> policy named "<name>_snake_name"
  policy_files = {
    for f in fileset("${path.module}/policies", "*.dogwood") :
    replace(trimsuffix(f, ".dogwood"), "/^[0-9]+_/", "") => f
  }
  policy_statements = {
    for k, f in local.policy_files : k => templatefile("${path.module}/policies/${f}", local.policy_vars)
  }

  # Changing a temporal policy invalidates open policy sessions (the gateway
  # then answers 409). The agent mixes this into its session IDs, so a policy
  # change starts fresh sessions instead of failing.
  policy_set_hash = substr(sha256(join("\n", [for k in sort(keys(local.policy_statements)) : local.policy_statements[k]])), 0, 12)
}

# The aws provider's aws_bedrockagentcore_policy (6.71) can only send
# definition.cedar, and the service rejects `temporal` there. Dogwood policies go
# in definition.policy, which Cloud Control (AWS::BedrockAgentCore::Policy)
# supports. Plain Cedar is valid Dogwood, so every file uses the same path.
#
# Permits and forbids are two resources so forbids are created after the
# permits are ACTIVE. Validation checks each new policy against the engine as it
# stands, and a forbid on an action that has no permit yet is rejected as
# "Overly Restrictive" (it would deny every request).
locals {
  permit_files = { for k, f in local.policy_files : k => f if !startswith(k, "forbid_") }
  forbid_files = { for k, f in local.policy_files : k => f if startswith(k, "forbid_") }

  policy_desired_state = {
    for k, f in local.policy_files : k => jsonencode({
      PolicyEngineId = aws_bedrockagentcore_policy_engine.tools.policy_engine_id
      Name           = "${var.name}_${k}"
      Description    = "Managed by OpenTofu from policies/${f}"
      # Reject policies that reference unknown actions/attributes for this
      # gateway's tool schemas, or that are overly permissive or restrictive.
      ValidationMode = "FAIL_ON_ANY_FINDINGS"
      Definition = {
        Policy = { Statement = local.policy_statements[k] }
      }
    })
  }
}

resource "aws_cloudcontrolapi_resource" "policy" {
  for_each = local.permit_files

  type_name     = "AWS::BedrockAgentCore::Policy"
  desired_state = local.policy_desired_state[each.key]

  # Tool schemas must exist before validation can resolve the actions they reference.
  depends_on = [
    aws_bedrockagentcore_gateway_target.knowledge,
    aws_bedrockagentcore_gateway_target.orders,
  ]
}

resource "aws_cloudcontrolapi_resource" "forbid_policy" {
  for_each = local.forbid_files

  type_name     = "AWS::BedrockAgentCore::Policy"
  desired_state = local.policy_desired_state[each.key]

  depends_on = [aws_cloudcontrolapi_resource.policy]
}

# Forbids created before the split; keeps them instead of recreating.
moved {
  from = aws_cloudcontrolapi_resource.policy["forbid_customer_enumeration"]
  to   = aws_cloudcontrolapi_resource.forbid_policy["forbid_customer_enumeration"]
}
moved {
  from = aws_cloudcontrolapi_resource.policy["forbid_kb_search_rate"]
  to   = aws_cloudcontrolapi_resource.forbid_policy["forbid_kb_search_rate"]
}
moved {
  from = aws_cloudcontrolapi_resource.policy["forbid_refund_rate"]
  to   = aws_cloudcontrolapi_resource.forbid_policy["forbid_refund_rate"]
}
