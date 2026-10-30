# RAG: S3 source documents -> Bedrock Knowledge Base -> S3 Vectors index.

resource "aws_s3_bucket" "docs" {
  bucket_prefix = "${local.dns_name}-kb-docs-"
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "docs" {
  bucket                  = aws_s3_bucket.docs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "docs" {
  bucket = aws_s3_bucket.docs.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "docs" {
  bucket = aws_s3_bucket.docs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

resource "aws_s3vectors_vector_bucket" "kb" {
  vector_bucket_name = "${local.dns_name}-kb-vectors"
}

resource "aws_s3vectors_index" "kb" {
  index_name         = "${local.dns_name}-kb-index"
  vector_bucket_name = aws_s3vectors_vector_bucket.kb.vector_bucket_name
  data_type          = "float32"
  dimension          = var.embedding_dimensions
  distance_metric    = "cosine"

  # Bedrock stores chunk text and metadata in these keys; marking them non-filterable
  # keeps them out of the 2 KB filterable-metadata limit.
  metadata_configuration {
    non_filterable_metadata_keys = ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
  }
}

data "aws_iam_policy_document" "kb_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:bedrock:${var.region}:${local.account_id}:knowledge-base/*"]
    }
  }
}

resource "aws_iam_role" "kb" {
  name_prefix        = "${local.dns_name}-kb-"
  assume_role_policy = data.aws_iam_policy_document.kb_assume.json
}

data "aws_iam_policy_document" "kb" {
  statement {
    sid       = "Embed"
    actions   = ["bedrock:InvokeModel"]
    resources = [local.embedding_model_arn]
  }

  statement {
    sid       = "ReadSourceDocs"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.docs.arn]
  }

  statement {
    sid       = "ReadSourceObjects"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.docs.arn}/*"]
  }

  statement {
    sid = "VectorStore"
    actions = [
      "s3vectors:GetIndex",
      "s3vectors:PutVectors",
      "s3vectors:GetVectors",
      "s3vectors:DeleteVectors",
      "s3vectors:QueryVectors",
    ]
    resources = [aws_s3vectors_index.kb.index_arn]
  }
}

resource "aws_iam_role_policy" "kb" {
  role   = aws_iam_role.kb.id
  policy = data.aws_iam_policy_document.kb.json
}

resource "aws_bedrockagent_knowledge_base" "this" {
  name        = "${local.dns_name}-kb"
  description = "Support documentation for ${var.name}"
  role_arn    = aws_iam_role.kb.arn

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = local.embedding_model_arn
      embedding_model_configuration {
        bedrock_embedding_model_configuration {
          dimensions          = var.embedding_dimensions
          embedding_data_type = "FLOAT32"
        }
      }
    }
  }

  storage_configuration {
    type = "S3_VECTORS"
    s3_vectors_configuration {
      index_arn = aws_s3vectors_index.kb.index_arn
    }
  }

  depends_on = [aws_iam_role_policy.kb]
}

resource "aws_bedrockagent_data_source" "docs" {
  name              = "${local.dns_name}-docs"
  knowledge_base_id = aws_bedrockagent_knowledge_base.this.id

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn = aws_s3_bucket.docs.arn
    }
  }

  vector_ingestion_configuration {
    chunking_configuration {
      chunking_strategy = "FIXED_SIZE"
      fixed_size_chunking_configuration {
        max_tokens         = 512
        overlap_percentage = 20
      }
    }
  }
}
