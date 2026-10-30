# Shared token counter for the agent's per-minute and per-day model limits.
#
# Bedrock's own tokens-per-minute quotas are account-wide and can only be raised
# through Service Quotas, not lowered, so they can't serve as a cost cap. The
# agent checks this table before every model call and adds the call's
# usage.totalTokens afterwards. Items expire on their own via TTL. On-demand
# billing means no cost while idle.

resource "aws_dynamodb_table" "model_usage" {
  name         = "${local.dns_name}-model-usage"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }
}
