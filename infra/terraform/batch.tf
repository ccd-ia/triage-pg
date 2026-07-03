# One AWS Batch job per experiment (ADR-0005): Fargate compute environment + queue + job
# definition. The container entrypoint mirrors BatchExecution.submit_job's parameters
# (profiles/execution.py): `triage run <config_uri> --profile <profile>` — the config is read
# from S3 and the job authenticates via its task role (iam.tf), zero passed secrets.

resource "aws_security_group" "batch_jobs" {
  name        = "${var.name_prefix}-batch-jobs"
  description = "triage-pg experiment containers (egress-only: RDS + S3 + ECR/logs)"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_batch_compute_environment" "triage" {
  name         = "${var.name_prefix}-fargate"
  type         = "MANAGED"
  service_role = aws_iam_role.batch_service.arn

  compute_resources {
    type               = "FARGATE"
    max_vcpus          = var.batch_max_vcpus
    subnets            = var.private_subnet_ids
    security_group_ids = [aws_security_group.batch_jobs.id]
  }

  depends_on = [aws_iam_role_policy_attachment.batch_service]
}

resource "aws_batch_job_queue" "triage" {
  name     = "${var.name_prefix}-queue"
  state    = "ENABLED"
  priority = 1

  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.triage.arn
  }
}

resource "aws_batch_job_definition" "triage" {
  name = "${var.name_prefix}-experiment"
  type = "container"

  platform_capabilities = ["FARGATE"]

  # Ref:: placeholders are filled by submit_job's parameters (profiles/execution.py §4).
  parameters = {
    profile = "cloud"
  }

  container_properties = jsonencode({
    image            = "${aws_ecr_repository.triage.repository_url}:${var.image_tag}"
    command          = ["triage", "run", "Ref::config_uri", "--profile", "Ref::profile"]
    jobRoleArn       = aws_iam_role.task.arn
    executionRoleArn = aws_iam_role.execution.arn
    fargatePlatformConfiguration = {
      platformVersion = "LATEST"
    }
    networkConfiguration = {
      assignPublicIp = "DISABLED"
    }
    resourceRequirements = [
      { type = "VCPU", value = var.job_vcpus },
      { type = "MEMORY", value = var.job_memory_mib },
    ]
    environment = [
      # The §5 conninfo (no password — CloudAuth mints IAM tokens per connection, ADR-0004).
      { name = "AWS_REGION", value = var.region },
      { name = "TRIAGE_RDS_HOST", value = aws_db_instance.triage.address },
      { name = "TRIAGE_RDS_PORT", value = "5432" },
      { name = "TRIAGE_S3_BUCKET", value = aws_s3_bucket.artifacts.bucket },
      # TRIAGE_RDS_DB + TRIAGE_RDS_USER are per-project (ADR-0002/0004) and arrive as
      # submit-time container OVERRIDES, not baked defaults — see the runbook.
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.batch.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "triage"
      }
    }
  })
}

resource "aws_cloudwatch_log_group" "batch" {
  name              = "/aws/batch/${var.name_prefix}"
  retention_in_days = 90
}
