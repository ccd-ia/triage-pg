# The triage-pg image repository. The image itself is built + pushed OUT OF BAND
# (docs/cloud-runbook.md): the featurizer git+ssh dependency needs `--ssh default` at build
# time, which rules out a naive CI build until the dependency moves to CodeArtifact.

resource "aws_ecr_repository" "triage" {
  name                 = "${var.name_prefix}-pg"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}
