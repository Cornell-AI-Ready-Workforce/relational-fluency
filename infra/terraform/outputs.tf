output "agent_url" {
  description = "Paste into ElevenLabs custom-LLM settings (server URL)"
  value       = "https://${local.agent_fqdn}/v1"
}

output "ecr_repository" {
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
