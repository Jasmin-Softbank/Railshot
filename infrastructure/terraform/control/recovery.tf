# Backups survive the API Pod, PVC and control node. Only the host can upload;
# restore uses the operator identity, never application credentials.
resource "aws_s3_bucket" "recovery" {
  bucket        = "railshot-platform-recovery-${var.account_id}"
  force_destroy = false
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_public_access_block" "recovery" {
  bucket                  = aws_s3_bucket.recovery.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "recovery" {
  bucket = aws_s3_bucket.recovery.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_versioning" "recovery" {
  bucket = aws_s3_bucket.recovery.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "recovery" {
  bucket = aws_s3_bucket.recovery.id
  rule {
    id     = "retain-two-weeks"
    status = "Enabled"
    filter { prefix = "platform/" }
    expiration { days = 14 }
    noncurrent_version_expiration { noncurrent_days = 14 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
  depends_on = [aws_s3_bucket_versioning.recovery]
}

resource "aws_iam_role_policy" "recovery_upload" {
  name = "write-platform-recovery-only"
  role = aws_iam_role.control.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Action = ["s3:PutObject", "s3:AbortMultipartUpload"],
    Resource = "${aws_s3_bucket.recovery.arn}/platform/*"
  }] })
}

output "recovery_bucket" { value = aws_s3_bucket.recovery.id }
