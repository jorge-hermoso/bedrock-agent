# Agent runtime on Bedrock AgentCore, deployed as a zip (direct code deployment).
# agent/package.sh builds agent/dist/agent.zip (arm64 wheels + app.py); OpenTofu
# uploads it to the code bucket and points the runtime at that object version, so
# re-running package.sh + apply rolls out new code. No ECR, so no ECR endpoints.
#
# The agent must:
#   - call the model with the guardrail below (IAM denies model calls without it),
#   - reach tools and the knowledge base only through the gateway (SigV4).

locals {
  agent_zip        = startswith(var.agent_code_zip, "/") ? var.agent_code_zip : "${path.module}/${var.agent_code_zip}"
  agent_zip_exists = fileexists(local.agent_zip)
}

resource "aws_s3_bucket" "agent_code" {
  bucket_prefix = "${local.dns_name}-agent-code-"
}

resource "aws_s3_bucket_public_access_block" "agent_code" {
  bucket                  = aws_s3_bucket.agent_code.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning gives every upload a version_id, which the runtime pins to.
resource "aws_s3_bucket_versioning" "agent_code" {
  bucket = aws_s3_bucket.agent_code.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "agent_code" {
  bucket = aws_s3_bucket.agent_code.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "agent_code" {
  bucket = aws_s3_bucket.agent_code.id
  rule {
    id     = "expire-old-builds"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

resource "aws_s3_object" "agent_code" {
  count = local.agent_zip_exists ? 1 : 0

  bucket      = aws_s3_bucket.agent_code.id
  key         = "agent/agent.zip"
  source      = local.agent_zip
  source_hash = filemd5(local.agent_zip)

  depends_on = [aws_s3_bucket_versioning.agent_code]
}

data "aws_iam_policy_document" "runtime_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "runtime" {
  name_prefix        = "${local.dns_name}-rt-"
  assume_role_policy = data.aws_iam_policy_document.runtime_assume.json
}

data "aws_iam_policy_document" "runtime" {
  statement {
    sid       = "ReadAgentCode"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.agent_code.arn}/agent/*"]
  }

  # ---- Model access ---------------------------------------------------------
  # One route only: the configured inference profile, with this guardrail version.
  # Bedrock authorizes a profile call against the profile and against the model in
  # each Region it may route to; the model grant is only usable through the profile.

  dynamic "statement" {
    for_each = local.model_is_profile ? [1] : []
    content {
      sid       = "InvokeConfiguredProfile"
      actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      resources = [local.model_profile_arn]
      condition {
        test     = "StringEquals"
        variable = "bedrock:GuardrailIdentifier"
        values   = [local.guardrail_condition_value]
      }
    }
  }

  # With a profile, the model itself is reachable only through that profile.
  statement {
    sid       = "InvokeModel"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = local.model_arns
    dynamic "condition" {
      for_each = local.model_is_profile ? [1] : []
      content {
        test     = "StringEquals"
        variable = "bedrock:InferenceProfileArn"
        values   = [local.model_profile_arn]
      }
    }
    condition {
      test     = "StringEquals"
      variable = "bedrock:GuardrailIdentifier"
      values   = [local.guardrail_condition_value]
    }
  }

  # Explicit denies win over any Allow, so these hold even if a broad policy such as
  # AmazonBedrockFullAccess is attached to this role later.
  statement {
    sid           = "DenyOtherModels"
    effect        = "Deny"
    actions       = ["bedrock:InvokeModel*"] # Converse/ConverseStream authorize as InvokeModel*
    not_resources = local.allowed_model_arns
  }

  statement {
    sid       = "DenyWithoutGuardrail"
    effect    = "Deny"
    actions   = ["bedrock:InvokeModel*"]
    resources = ["*"]
    # Also matches when no guardrail is sent: a negated operator is true for an absent key.
    condition {
      test     = "StringNotEquals"
      variable = "bedrock:GuardrailIdentifier"
      values   = [local.guardrail_condition_value]
    }
  }

  statement {
    sid    = "DenyModelSideChannels"
    effect = "Deny"
    actions = [
      "bedrock:CreateModelInvocationJob",         # batch inference
      "bedrock:CreateInferenceProfile",           # new routes to other models
      "bedrock:CreateProvisionedModelThroughput", # committed spend
      "bedrock:InvokeAgent",
      "bedrock:RetrieveAndGenerate", # model call outside the guardrail check
      "bedrock:Retrieve",            # the KB is only reachable through the gateway (Dogwood)
      "aws-marketplace:Subscribe",   # enabling new third-party models
    ]
    resources = ["*"]
  }

  statement {
    sid       = "ApplyGuardrail"
    actions   = ["bedrock:ApplyGuardrail"]
    resources = [aws_bedrock_guardrail.agent.guardrail_arn]
  }

  statement {
    sid = "Memory"
    actions = [
      "bedrock-agentcore:CreateEvent",
      "bedrock-agentcore:ListEvents",
      "bedrock-agentcore:GetEvent",
      "bedrock-agentcore:RetrieveMemoryRecords",
    ]
    resources = [aws_bedrockagentcore_memory.agent.arn]
  }

  statement {
    sid       = "CallTools"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = [aws_bedrockagentcore_gateway.tools.gateway_arn]
  }

  statement {
    sid       = "TokenLimits"
    actions   = ["dynamodb:BatchGetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.model_usage.arn]
  }

  statement {
    sid = "Observability"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "cloudwatch:PutMetricData",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "runtime" {
  role   = aws_iam_role.runtime.id
  policy = data.aws_iam_policy_document.runtime.json
}

resource "aws_bedrockagentcore_agent_runtime" "agent" {
  count = local.agent_zip_exists ? 1 : 0

  agent_runtime_name = "${var.name}_runtime"
  description        = "Bedrock agent with guardrails, RAG and Dogwood-governed tools"
  role_arn           = aws_iam_role.runtime.arn

  agent_runtime_artifact {
    code_configuration {
      runtime     = "PYTHON_3_13"
      entry_point = ["app.py"]
      code {
        s3 {
          bucket     = aws_s3_bucket.agent_code.id
          prefix     = aws_s3_object.agent_code[0].key # the zip's object key, not a folder
          version_id = aws_s3_object.agent_code[0].version_id
        }
      }
    }
  }

  # VPC: private subnets with no internet route (network.tf). PUBLIC: AWS-managed
  # network with internet egress.
  network_configuration {
    network_mode = var.runtime_network_mode

    dynamic "network_mode_config" {
      for_each = local.use_vpc ? [1] : []
      content {
        subnets         = local.private_subnet_ids
        security_groups = local.workload_sg_ids
      }
    }
  }

  lifecycle_configuration {
    idle_runtime_session_timeout = var.runtime_idle_timeout_seconds
    # The service default, set explicitly: left unset, provider 6.71 fails with
    # "inconsistent result after apply" when the service fills it in.
    max_lifetime = 28800
  }

  environment_variables = {
    MODEL_ID = var.model_id
    # The full ARN, so the request matches the IAM bedrock:GuardrailIdentifier condition exactly.
    GUARDRAIL_ARN     = aws_bedrock_guardrail.agent.guardrail_arn
    GUARDRAIL_VERSION = aws_bedrock_guardrail_version.agent.version
    # Marks passages and question for the guardrail's contextual grounding check.
    GUARDRAIL_GROUNDING = tostring(var.enable_contextual_grounding)
    KB_SEARCH_TOOL      = "${local.knowledge_target}___search_knowledge_base"
    PROBE_TOOL          = "${local.henchman_target}___probe_dossier"
    READ_DOSSIER_TOOL   = "${local.henchman_target}___read_dossier"
    SECRET_MIN_SCORE    = tostring(var.secret_min_score)
    GATEWAY_URL         = aws_bedrockagentcore_gateway.tools.gateway_url
    MEMORY_ID           = aws_bedrockagentcore_memory.agent.id
    MEMORY_LONG_TERM    = tostring(var.enable_long_term_memory)
    MEMORY_TURNS        = tostring(var.memory_restore_turns)
    USAGE_TABLE         = aws_dynamodb_table.model_usage.name
    TOKENS_PER_MINUTE   = tostring(var.model_tokens_per_minute)
    TOKENS_PER_DAY      = tostring(var.model_tokens_per_day)
    POLICY_SET          = local.policy_set_hash
  }

  # In VPC mode the code fetch and every API call go through the endpoints.
  depends_on = [
    aws_iam_role_policy.runtime,
    aws_vpc_endpoint.interface,
    aws_vpc_endpoint.s3,
    aws_vpc_endpoint.dynamodb,
    aws_route_table_association.private,
    aws_vpc_security_group_egress_rule.workload_to_endpoints,
    aws_vpc_security_group_egress_rule.workload_to_gateway_endpoints,
    aws_vpc_security_group_ingress_rule.endpoints_from_workload,
  ]
}
