# AgentCore Memory.
#
# Short-term: the agent writes one event per conversation turn (user question +
# final answer), keyed by actor (user) and runtime session. A session that
# lands on a fresh runtime instance restores its recent turns from these events.
#
# Long-term: built-in strategies extract records from those events asynchronously.
# The agent retrieves them at the start of each session:
#   /preferences/{actorId}/            how the user likes to be answered
#   /facts/{actorId}/                  durable facts the user told us
#   /summaries/{actorId}/{sessionId}/  one summary per past conversation

resource "aws_bedrockagentcore_memory" "agent" {
  name                  = "${var.name}_memory"
  description           = "Conversation memory for ${var.name}"
  event_expiry_duration = var.memory_event_expiry_days
}

locals {
  memory_strategies = var.enable_long_term_memory ? {
    USER_PREFERENCE = { name = "preferences", namespace = "/preferences/{actorId}/" }
    SEMANTIC        = { name = "facts", namespace = "/facts/{actorId}/" }
    SUMMARIZATION   = { name = "summaries", namespace = "/summaries/{actorId}/{sessionId}/" }
  } : {}
}

resource "aws_bedrockagentcore_memory_strategy" "agent" {
  for_each = local.memory_strategies

  memory_id           = aws_bedrockagentcore_memory.agent.id
  name                = "${var.name}_${each.value.name}"
  type                = each.key
  namespace_templates = [each.value.namespace]
}
