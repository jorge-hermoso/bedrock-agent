# Bedrock Guardrail applied to every model call the agent makes.
# runtime.tf scopes the agent's bedrock:InvokeModel* permission to this guardrail
# version via the bedrock:GuardrailIdentifier condition key, so the agent cannot
# call the model without it.

resource "aws_bedrock_guardrail" "agent" {
  name                      = "${local.dns_name}-guardrail"
  description               = "Input/output safety, PII protection and RAG grounding for ${var.name}"
  blocked_input_messaging   = "Sorry, I can't help with that request."
  blocked_outputs_messaging = "Sorry, I can't share that response."

  content_policy_config {
    dynamic "filters_config" {
      for_each = ["HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT"]
      content {
        type            = filters_config.value
        input_strength  = "HIGH"
        output_strength = "HIGH"
      }
    }

    # Prompt-injection / jailbreak detection only applies to input.
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "HIGH"
      output_strength = "NONE"
    }
  }

  topic_policy_config {
    dynamic "topics_config" {
      for_each = var.guardrail_denied_topics
      content {
        name       = topics_config.value.name
        definition = topics_config.value.definition
        examples   = topics_config.value.examples
        type       = "DENY"
      }
    }
  }

  sensitive_information_policy_config {
    dynamic "pii_entities_config" {
      for_each = ["CREDIT_DEBIT_CARD_NUMBER", "CREDIT_DEBIT_CARD_CVV", "US_SOCIAL_SECURITY_NUMBER", "AWS_ACCESS_KEY", "AWS_SECRET_KEY", "PASSWORD"]
      content {
        type   = pii_entities_config.value
        action = "BLOCK"
      }
    }

    dynamic "pii_entities_config" {
      for_each = ["EMAIL", "PHONE", "ADDRESS"]
      content {
        type   = pii_entities_config.value
        action = "ANONYMIZE"
      }
    }
  }

  word_policy_config {
    managed_word_lists_config {
      type = "PROFANITY"
    }
  }

  # Blocks answers that aren't supported by the knowledge-base passages passed as grounding source.
  dynamic "contextual_grounding_policy_config" {
    for_each = var.enable_contextual_grounding ? [1] : []
    content {
      filters_config {
        type      = "GROUNDING"
        threshold = var.grounding_threshold
      }
      filters_config {
        type      = "RELEVANCE"
        threshold = var.relevance_threshold
      }
    }
  }
}

# Immutable numbered version that the agent (and IAM condition) pins to.
resource "aws_bedrock_guardrail_version" "agent" {
  guardrail_arn = aws_bedrock_guardrail.agent.guardrail_arn
  description   = "Managed by OpenTofu"

  lifecycle {
    replace_triggered_by = [aws_bedrock_guardrail.agent]
  }
}
