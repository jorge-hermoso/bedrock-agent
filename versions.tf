terraform {
  required_version = ">= 1.8.0" # OpenTofu 1.8+

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.71.0" # AgentCore policy engine/policy + S3 Vectors KB storage
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4.0"
    }
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9.0"
    }
  }

  backend "s3" {
    bucket       = var.state_bucket
    key          = "bedrock-agent/terraform.tfstate"
    region       = var.region
    profile      = var.aws_profile
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = merge(var.tags, {
      Project   = var.name
      ManagedBy = "opentofu"
    })
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
