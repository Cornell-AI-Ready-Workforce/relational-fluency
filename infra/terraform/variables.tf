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
  description = "Zone delegated to Route 53 by Cornell IT"
  type        = string
  default     = "ai-ready-workforce.ai.cornell.edu"
}

variable "app_subdomain" {
  description = "Participant entrance — the web app and session broker (WSS)"
  type        = string
  default     = "rf"
}

variable "api_subdomain" {
  description = "Backend API"
  type        = string
  default     = "api.rf"
}

variable "container_image" {
  description = "Full ECR image URI with tag; set after first image push. Empty on first apply is fine (service starts once set)."
  type        = string
  default     = ""
}

variable "llm_base_url" {
  description = "Cornell LiteLLM gateway base URL. Serves both the realtime actor and the text director."
  type        = string
  default     = "https://api.ai.it.cornell.edu"
}

variable "actor_model" {
  description = "Speech-to-speech model the participant talks to. Frozen for the study wave — the agent is the measurement instrument."
  type        = string
  default     = "nto.gemini-live-2.5-flash"
}

variable "director_model" {
  description = "Text model that reads each turn and writes one stage direction (LiteLLM alias)"
  type        = string
  default     = "nto.gemini-2.5-flash"
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

variable "survey_return_url" {
  description = "Qualtrics continuation link. Participants are sent here after all four encounters, with run id, completion code, and pid appended."
  type        = string
  default     = ""
}
