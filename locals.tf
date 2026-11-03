locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  # Lowercase/hyphenated form for services that need DNS-style names (S3, S3 Vectors, Lambda).
  dns_name = replace(lower(var.name), "_", "-")

  # model_id is either an inference profile ("global.", "eu.", ... prefix) or an
  # in-Region on-demand model such as "qwen.qwen3-next-80b-a3b".
  model_is_profile = can(regex("^(global|us|eu|apac|au|jp|ca|us-gov)\\.", var.model_id))
  # Strip the inference profile prefix to get the underlying foundation model.
  base_model_id     = replace(var.model_id, "/^(global|us|eu|apac|au|jp|ca|us-gov)\\./", "")
  model_profile_arn = local.model_is_profile ? "arn:${local.partition}:bedrock:${var.region}:${local.account_id}:inference-profile/${var.model_id}" : ""
  model_arns = local.model_is_profile ? [
    # Destination-Region copies of the model (geo profiles).
    "arn:${local.partition}:bedrock:*::foundation-model/${local.base_model_id}",
    # Region-agnostic model ARN evaluated for global profiles.
    "arn:${local.partition}:bedrock:::foundation-model/${local.base_model_id}",
  ] : ["arn:${local.partition}:bedrock:${var.region}::foundation-model/${var.model_id}"]
  # Everything the runtime may invoke; DenyOtherModels denies the rest.
  allowed_model_arns        = compact(concat([local.model_profile_arn], local.model_arns))
  guardrail_condition_value = "${aws_bedrock_guardrail.agent.guardrail_arn}:${aws_bedrock_guardrail_version.agent.version}"

  embedding_model_arn = "arn:${local.partition}:bedrock:${var.region}::foundation-model/${var.embedding_model_id}"

  # Gateway target names become the prefix of the Cedar/Dogwood action:
  #   AgentCore::Action::"<TargetName>___<tool_name>"
  knowledge_target = "Knowledge"
  henchman_target  = "Henchman"
}
