# Agent tools: one Lambda behind an AgentCore Gateway (MCP). The gateway is the
# single choke point for function calls, so the policy engine attached to it
# (policies.tf) governs every tool invocation.

data "archive_file" "tools" {
  type        = "zip"
  source_dir  = "${path.module}/lambda/tools"
  output_path = "${path.module}/.build/tools.zip"
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "tools" {
  name_prefix        = "${local.dns_name}-tools-"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "tools_logs" {
  role       = aws_iam_role.tools.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "tools_vpc" {
  count      = var.tools_lambda_in_vpc ? 1 : 0
  role       = aws_iam_role.tools.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "tools" {
  statement {
    sid       = "RetrieveFromKnowledgeBase"
    actions   = ["bedrock:Retrieve"]
    resources = [aws_bedrockagent_knowledge_base.this.arn]
  }
}

resource "aws_iam_role_policy" "tools" {
  role   = aws_iam_role.tools.id
  policy = data.aws_iam_policy_document.tools.json
}

resource "aws_cloudwatch_log_group" "tools" {
  name              = "/aws/lambda/${local.dns_name}-tools"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "tools" {
  function_name    = "${local.dns_name}-tools"
  role             = aws_iam_role.tools.arn
  runtime          = "python3.13"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.tools.output_path
  source_code_hash = data.archive_file.tools.output_base64sha256
  timeout          = 30
  memory_size      = 256

  # Optional: run inside the no-egress VPC (reaches the KB via the bedrock-agent-runtime endpoint).
  dynamic "vpc_config" {
    for_each = var.tools_lambda_in_vpc ? [1] : []
    content {
      subnet_ids         = local.private_subnet_ids
      security_group_ids = local.workload_sg_ids
    }
  }

  environment {
    variables = {
      KNOWLEDGE_BASE_ID = aws_bedrockagent_knowledge_base.this.id
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.tools,
    aws_iam_role_policy_attachment.tools_logs,
    aws_iam_role_policy_attachment.tools_vpc,
  ]

  lifecycle {
    precondition {
      condition     = !var.tools_lambda_in_vpc || local.use_vpc
      error_message = "tools_lambda_in_vpc requires runtime_network_mode = \"VPC\"."
    }
  }
}

# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "gateway_assume" {
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

resource "aws_iam_role" "gateway" {
  name_prefix        = "${local.dns_name}-gw-"
  assume_role_policy = data.aws_iam_policy_document.gateway_assume.json
}

data "aws_iam_policy_document" "gateway" {
  statement {
    sid       = "InvokeTools"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.tools.arn]
  }

  statement {
    sid       = "ReadPolicyEngine"
    actions   = ["bedrock-agentcore:GetPolicyEngine"]
    resources = [aws_bedrockagentcore_policy_engine.tools.policy_engine_arn]
  }

  # Authorization is checked against the gateway ARN, which gets a random suffix
  # at creation (and referencing it here would be a cycle), so match by name.
  # CreateGateway verifies this permission before it returns.
  statement {
    sid = "EvaluatePolicies"
    actions = [
      "bedrock-agentcore:AuthorizeAction",
      "bedrock-agentcore:PartiallyAuthorizeActions",
    ]
    resources = [
      aws_bedrockagentcore_policy_engine.tools.policy_engine_arn,
      "arn:${local.partition}:bedrock-agentcore:${var.region}:${local.account_id}:gateway/${local.dns_name}-gateway-*",
    ]
  }

  # Temporal policies: the gateway mints a workload access token carrying the
  # policy session ID and caller for every IAM-authorized request.
  statement {
    sid     = "PolicySessionWorkloadIdentity"
    actions = ["bedrock-agentcore:GetWorkloadAccessToken"]
    resources = [
      "arn:${local.partition}:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default",
      "arn:${local.partition}:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default/workload-identity/${local.dns_name}-gateway-*",
    ]
  }
}

resource "aws_iam_role_policy" "gateway" {
  role   = aws_iam_role.gateway.id
  policy = data.aws_iam_policy_document.gateway.json
}

# IAM changes take a few seconds to reach AgentCore, and CreateGateway checks the
# role's AuthorizeAction permission immediately. Wait whenever the policy changes.
resource "time_sleep" "gateway_iam" {
  create_duration = "30s"
  triggers = {
    policy = aws_iam_role_policy.gateway.policy
  }
}

resource "aws_bedrockagentcore_gateway" "tools" {
  name        = "${local.dns_name}-gateway"
  description = "MCP tool gateway for ${var.name}; all tool calls are authorized by AgentCore Policy"
  role_arn    = aws_iam_role.gateway.arn

  # Callers (the agent runtime role) sign requests with SigV4.
  authorizer_type = "AWS_IAM"
  protocol_type   = "MCP"

  policy_engine_configuration {
    arn  = aws_bedrockagentcore_policy_engine.tools.policy_engine_arn
    mode = var.policy_enforcement_mode
  }

  depends_on = [time_sleep.gateway_iam]
}

# Target "Knowledge" -> action AgentCore::Action::"Knowledge___search_knowledge_base"
resource "aws_bedrockagentcore_gateway_target" "knowledge" {
  name               = local.knowledge_target
  gateway_identifier = aws_bedrockagentcore_gateway.tools.gateway_id
  description        = "RAG search over the support knowledge base"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.tools.arn

        tool_schema {
          inline_payload {
            name        = "search_knowledge_base"
            description = "Search the support documentation. Use before answering any product or policy question."

            input_schema {
              type = "object"
              property {
                name        = "query"
                type        = "string"
                description = "Natural-language search query"
                required    = true
              }
              property {
                name        = "max_results"
                type        = "integer"
                description = "Number of passages to return (1-10)"
              }
            }
          }
        }
      }
    }
  }
}

# Target "Henchman" -> action AgentCore::Action::"Henchman___ask_question"
# The agent calls it at the start of every turn; Dogwood policy 08 allows it only
# once the same question has been asked three times in a row. The model never sees it.
resource "aws_bedrockagentcore_gateway_target" "henchman" {
  name               = local.henchman_target
  gateway_identifier = aws_bedrockagentcore_gateway.tools.gateway_id
  description        = "The henchman's three question rule"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.tools.arn

        tool_schema {
          inline_payload {
            name        = "ask_question"
            description = "Gate for one user question. Allowed only when the same question has been asked three times in a row."

            input_schema {
              type = "object"
              property {
                name        = "question"
                type        = "string"
                description = "The user's question, normalized (lowercase, punctuation and extra spaces removed)"
                required    = true
              }
            }
          }
        }
      }
    }
  }
}
