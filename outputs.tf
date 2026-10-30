output "guardrail_id" {
  value = aws_bedrock_guardrail.agent.guardrail_id
}

output "guardrail_version" {
  value = aws_bedrock_guardrail_version.agent.version
}

output "knowledge_base_id" {
  value = aws_bedrockagent_knowledge_base.this.id
}

output "data_source_id" {
  value = aws_bedrockagent_data_source.docs.data_source_id
}

output "docs_bucket" {
  description = "Upload source documents here, then start an ingestion job."
  value       = aws_s3_bucket.docs.bucket
}

output "gateway_url" {
  value = aws_bedrockagentcore_gateway.tools.gateway_url
}

output "gateway_arn" {
  value = aws_bedrockagentcore_gateway.tools.gateway_arn
}

output "policy_engine_id" {
  value = aws_bedrockagentcore_policy_engine.tools.policy_engine_id
}

output "policies" {
  value = { for k, p in merge(aws_cloudcontrolapi_resource.policy, aws_cloudcontrolapi_resource.forbid_policy) : k => jsondecode(p.properties).PolicyId }
}

output "memory_id" {
  value = aws_bedrockagentcore_memory.agent.id
}

output "agent_code_bucket" {
  value = aws_s3_bucket.agent_code.bucket
}

output "agent_runtime_arn" {
  value = one(aws_bedrockagentcore_agent_runtime.agent[*].agent_runtime_arn)
}

output "start_ingestion_command" {
  value = "aws bedrock-agent start-ingestion-job --region ${var.region} --knowledge-base-id ${aws_bedrockagent_knowledge_base.this.id} --data-source-id ${aws_bedrockagent_data_source.docs.data_source_id}"
}

output "guardrail_arn" {
  value = aws_bedrock_guardrail.agent.guardrail_arn
}
