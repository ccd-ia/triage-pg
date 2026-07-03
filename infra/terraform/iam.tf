# The ADR-0004/0005 no-passed-credentials model: the Batch job's TASK ROLE is the only
# credential surface — it grants rds-db:connect (the IAM-token handshake) + artifact-bucket
# access; nothing is ever written into configs, the registry, or the container environment.

# ------------------------------------------------------------------ task role (the job's identity)

data "aws_iam_policy_document" "task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task" {
  name               = "${var.name_prefix}-batch-task"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

data "aws_iam_policy_document" "task_permissions" {
  # IAM database authentication (ADR-0004). The resource is the DB user name segment:
  # per-project PG users are created by the runbook bootstrap; scoping to triage_* users
  # keeps a compromised job off the master account entirely.
  statement {
    sid     = "RdsIamConnect"
    actions = ["rds-db:connect"]
    resources = [
      "arn:aws:rds-db:${var.region}:${data.aws_caller_identity.current.account_id}:dbuser:${aws_db_instance.triage.resource_id}/triage_*",
    ]
  }

  # Artifact + config-staging access (cloud-profile-spec §3/§4), scoped to the one bucket.
  statement {
    sid       = "ArtifactBucketList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]
  }
  statement {
    sid       = "ArtifactObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${var.name_prefix}-batch-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_permissions.json
}

# ------------------------------------------------------------------ execution role (image pull + logs)

resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-batch-execution"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ------------------------------------------------------------------ Batch service role

data "aws_iam_policy_document" "batch_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["batch.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "batch_service" {
  name               = "${var.name_prefix}-batch-service"
  assume_role_policy = data.aws_iam_policy_document.batch_assume.json
}

resource "aws_iam_role_policy_attachment" "batch_service" {
  role       = aws_iam_role.batch_service.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}
