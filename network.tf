# No-egress network for the agent runtime (runtime_network_mode = "VPC").
#
# Private subnets with no internet gateway and no NAT: the only routes out are
# VPC endpoints to the AWS services the agent uses. Anything else, including the
# public internet, is unreachable from the runtime.
#
# Idle cost: interface endpoints bill per hour per AZ (about $8/month each in eu-west-1);
# four for the runtime, plus one more if the tools Lambda is in the VPC.
# endpoint_az_count = 1 keeps one ENI per endpoint; the S3 and DynamoDB gateway
# endpoints are free.

locals {
  use_vpc = var.runtime_network_mode == "VPC"

  # Interface endpoints the runtime (and, if enabled, the tools Lambda) needs.
  # Code is fetched from S3 (gateway endpoint), and the knowledge base is reached
  # through the gateway, so neither ECR nor bedrock-agent-runtime is needed here.
  interface_endpoints = local.use_vpc ? setunion(
    [
      "bedrock-runtime",           # Converse (model calls)
      "bedrock-agentcore",         # Memory data plane
      "bedrock-agentcore.gateway", # MCP tool gateway (tools + knowledge base)
      "logs",                      # runtime logs
    ],
    var.tools_lambda_in_vpc ? ["bedrock-agent-runtime"] : [], # Lambda's KB Retrieve
  ) : toset([])

  private_subnet_ids  = [for az in var.vpc_az_ids : aws_subnet.private[az].id if local.use_vpc]
  endpoint_subnet_ids = slice(local.private_subnet_ids, 0, min(var.endpoint_az_count, length(local.private_subnet_ids)))
  workload_sg_ids     = local.use_vpc ? [aws_security_group.workload[0].id] : []
}

resource "aws_vpc" "agent" {
  count = local.use_vpc ? 1 : 0

  cidr_block           = var.vpc_cidr
  enable_dns_support   = true # required for endpoint private DNS
  enable_dns_hostnames = true

  tags = { Name = "${local.dns_name}-agent" }
}

resource "aws_subnet" "private" {
  for_each = local.use_vpc ? { for i, az in var.vpc_az_ids : az => i } : {}

  vpc_id               = aws_vpc.agent[0].id
  availability_zone_id = each.key
  cidr_block           = cidrsubnet(var.vpc_cidr, 8, each.value)

  tags = { Name = "${local.dns_name}-private-${each.key}" }
}

# Only the implicit local route plus gateway-endpoint prefix lists: no 0.0.0.0/0.
resource "aws_route_table" "private" {
  count  = local.use_vpc ? 1 : 0
  vpc_id = aws_vpc.agent[0].id
  tags   = { Name = "${local.dns_name}-private" }
}

resource "aws_route_table_association" "private" {
  for_each       = aws_subnet.private
  subnet_id      = each.value.id
  route_table_id = aws_route_table.private[0].id
}

# ---------------------------------------------------------------------------
# Security groups
# ---------------------------------------------------------------------------

# Runtime (and tools Lambda when in the VPC): HTTPS out to the endpoints only.
resource "aws_security_group" "workload" {
  count       = local.use_vpc ? 1 : 0
  name_prefix = "${local.dns_name}-workload-"
  description = "Agent runtime / tools: egress to VPC endpoints only"
  vpc_id      = aws_vpc.agent[0].id
}

resource "aws_security_group" "endpoints" {
  count       = local.use_vpc ? 1 : 0
  name_prefix = "${local.dns_name}-endpoints-"
  description = "Interface endpoints: HTTPS in from the agent workloads"
  vpc_id      = aws_vpc.agent[0].id
}

resource "aws_vpc_security_group_egress_rule" "workload_to_endpoints" {
  count                        = local.use_vpc ? 1 : 0
  security_group_id            = aws_security_group.workload[0].id
  referenced_security_group_id = aws_security_group.endpoints[0].id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_egress_rule" "workload_to_gateway_endpoints" {
  for_each          = local.use_vpc ? { s3 = aws_vpc_endpoint.s3[0].prefix_list_id, dynamodb = aws_vpc_endpoint.dynamodb[0].prefix_list_id } : {}
  security_group_id = aws_security_group.workload[0].id
  prefix_list_id    = each.value
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_workload" {
  count                        = local.use_vpc ? 1 : 0
  security_group_id            = aws_security_group.endpoints[0].id
  referenced_security_group_id = aws_security_group.workload[0].id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

resource "aws_vpc_endpoint" "interface" {
  for_each = local.interface_endpoints

  vpc_id              = aws_vpc.agent[0].id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = local.endpoint_subnet_ids
  security_group_ids  = [aws_security_group.endpoints[0].id]

  tags = { Name = "${local.dns_name}-${each.key}" }
}

# AgentCore copies the deployment zip into a service-owned code bucket and the
# runtime fetches it from there. The endpoint policy (from the AgentCore VPC docs)
# allows only that bucket and only for the AgentCore service, so the S3 route
# can't be used to move data to arbitrary buckets.
data "aws_iam_policy_document" "s3_endpoint" {
  statement {
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions = ["s3:GetObject"]
    resources = [
      "arn:${local.partition}:s3:::acr-code-*-${var.region}-an",
      "arn:${local.partition}:s3:::acr-code-*-${var.region}-an/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalServiceName"
      values   = ["bedrock-agentcore.amazonaws.com"]
    }
  }
}

resource "aws_vpc_endpoint" "s3" {
  count = local.use_vpc ? 1 : 0

  vpc_id            = aws_vpc.agent[0].id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private[0].id]
  policy            = data.aws_iam_policy_document.s3_endpoint.json

  tags = { Name = "${local.dns_name}-s3" }
}

# Token-limiter table only.
data "aws_iam_policy_document" "dynamodb_endpoint" {
  statement {
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions   = ["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.model_usage.arn]
  }
}

resource "aws_vpc_endpoint" "dynamodb" {
  count = local.use_vpc ? 1 : 0

  vpc_id            = aws_vpc.agent[0].id
  service_name      = "com.amazonaws.${var.region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private[0].id]
  policy            = data.aws_iam_policy_document.dynamodb_endpoint.json

  tags = { Name = "${local.dns_name}-dynamodb" }
}
