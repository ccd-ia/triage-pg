# Recurring forward scoring in cloud (ADR-0027): EventBridge Scheduler → Batch submitJob,
# overriding the job command to `triage score <model_id>` (no date argument = "today").
# Empty by default — populating `forward_score_schedules` is a deploy-time decision made
# with the cloud gate open. No daemon anywhere: the scheduler is AWS's, the command is the CLI.

variable "forward_score_schedules" {
  description = "Recurring forward-scoring jobs: name => schedule + the model + its project identity."
  type = map(object({
    schedule_expression = string # e.g. "cron(0 6 1 * ? *)" — the 1st of each month, 06:00 UTC
    model_id            = number
    project_db          = string # TRIAGE_RDS_DB override (the project's database, ADR-0002)
    project_user        = string # TRIAGE_RDS_USER override (the project's IAM PG user, ADR-0004)
  }))
  default = {}
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  count              = length(var.forward_score_schedules) > 0 ? 1 : 0
  name               = "${var.name_prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler_permissions" {
  statement {
    actions   = ["batch:SubmitJob"]
    resources = [aws_batch_job_queue.triage.arn, aws_batch_job_definition.triage.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  count  = length(var.forward_score_schedules) > 0 ? 1 : 0
  name   = "${var.name_prefix}-scheduler"
  role   = aws_iam_role.scheduler[0].id
  policy = data.aws_iam_policy_document.scheduler_permissions.json
}

resource "aws_scheduler_schedule" "forward_score" {
  for_each = var.forward_score_schedules

  name                = "${var.name_prefix}-score-${each.key}"
  schedule_expression = each.value.schedule_expression

  flexible_time_window {
    mode = "OFF"
  }

  # The Batch SubmitJob universal target: same job definition as experiments, with the
  # command overridden to the monitoring entrypoint and the project identity injected.
  target {
    arn      = "arn:aws:scheduler:::aws-sdk:batch:submitJob"
    role_arn = aws_iam_role.scheduler[0].arn
    input = jsonencode({
      JobName       = "${var.name_prefix}-score-${each.key}"
      JobQueue      = aws_batch_job_queue.triage.name
      JobDefinition = aws_batch_job_definition.triage.name
      Parameters = {
        config_uri = "unused" # the score command reads no config; Ref:: must still resolve
        profile    = "cloud"
      }
      ContainerOverrides = {
        Command = ["triage", "score", tostring(each.value.model_id)]
        Environment = [
          { Name = "TRIAGE_RDS_DB", Value = each.value.project_db },
          { Name = "TRIAGE_RDS_USER", Value = each.value.project_user },
        ]
      }
    })
  }
}
