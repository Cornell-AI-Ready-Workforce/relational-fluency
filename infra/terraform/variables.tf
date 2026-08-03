variable "project" {
  description = "Name prefix for all resources"
  type        = string
  default     = "relational-fluency"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "domain_name" {
  description = "Root domain with an existing Route 53 hosted zone (e.g. example.org)"
  type        = string
}

variable "agent_subdomain" {
  description = "Subdomain for the agent endpoint (agent.<domain_name>)"
  type        = string
  default     = "agent"
}

variable "container_image" {
  description = "Full ECR image URI with tag; set after first image push. Empty on first apply is fine (service starts once set)."
  type        = string
  default     = ""
}

variable "actor_model" {
  description = "Model for the actor (pin a snapshot and freeze for the study wave)"
  type        = string
  default     = "claude-sonnet-4-5"
}

variable "director_model" {
  description = "Model for the director"
  type        = string
  default     = "claude-haiku-4-5"
}

variable "scenario_id" {
  type    = string
  default = "S2A"
}

variable "desired_count" {
  description = "Number of agent tasks (2 during collection for resilience)"
  type        = number
  default     = 1
}
