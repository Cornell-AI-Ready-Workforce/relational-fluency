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
  description = "Text model that reads each turn and writes one stage direction (LiteLLM alias). Recorded per stage_direction event as director_model; it is NOT the deployment's text engine — see text_model."
  type        = string
  default     = "nto.gemini-2.5-flash"
}

# The director and the text engine are two different jobs and there is no reason
# they must be one model. They were briefly collapsed onto director_model to stop
# provenance.text_model reporting a code default, and that fixed the record by
# repointing the engine: CLAUDE_MODEL is read by server/engine.py and
# server/claude_engine.py as DEFAULT_MODEL, and rendered by server/app.py as the
# default in the researcher's pre-start model picker, so a text-mode encounter and
# the picker silently moved to the director's model. Splitting them keeps both
# true at once, because the director's model is recorded on its own: every
# stage_direction event carries director_model from the live Director instance
# (server/realtime_voice_session.py:750, :2498, :2574, :2745), so provenance's
# single text_model field is free to mean what its readers assume it means — the
# text engine that would serve a text-mode encounter in this deployment.
# The default is the value server/engine.py, server/claude_engine.py,
# server/llm.py and .env.example all already use, so a deployment that sets
# neither variable behaves exactly as the code and the local .env do.
variable "text_model" {
  description = "Text model for the text-mode engine, and the default in the researcher's pre-start model picker. Reported as provenance.text_model on every encounter record."
  type        = string
  default     = "nto.gemini-3.1-flash-lite"
}

# UNUSED, and left declared only so a `terraform apply` against an existing
# tfvars file does not fail on an undeclared variable. Nothing in infra/ reads
# it: grep var.scenario_id. It is a leftover from the agents/ prototype server,
# which does take a single SCENARIO_ID; the platform does not, because a
# participant runs four scenarios in one deployment and the run assignment
# chooses them. Setting it changes nothing — do not read it as a way to pin the
# wave to one scenario.
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
