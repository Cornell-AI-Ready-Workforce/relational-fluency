output "app_url" {
  description = "Participant entrance — WEIP handoff, encounters"
  value       = "https://${local.app_fqdn}"
}

output "broker_wss_url" {
  description = "Session broker WebSocket endpoint for participant audio"
  value       = "wss://${local.app_fqdn}/ws/participant/voice"
}

output "api_url" {
  description = "Backend API"
  value       = "https://${local.api_fqdn}"
}

output "ecr_repository" {
  description = "Push the platform image here, pin <uri>:<tag> as container_image in terraform.tfvars by PR, and release it through tools/deploy.sh"
  value       = aws_ecr_repository.platform.repository_url
}

output "ecr_repository_legacy_agent" {
  value = aws_ecr_repository.agent.repository_url
}

output "study_data_bucket" {
  value = aws_s3_bucket.study_data.bucket
}

output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "log_group" {
  value = aws_cloudwatch_log_group.agent.name
}
