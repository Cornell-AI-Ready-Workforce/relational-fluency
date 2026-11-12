"""Guard CloudWatch aggregation semantics without contacting AWS.

Terraform fmt/validate remain separate gates. These contracts prevent valid
HCL from silently changing the meaning of the dashboard or alarms.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
MONITORING = (ROOT / "infra/terraform/monitoring.tf").read_text(encoding="utf-8")
VARIABLES = (ROOT / "infra/terraform/variables.tf").read_text(encoding="utf-8")


def test_loop_windows_use_maximum_and_room_counts_stay_separate():
    lag = re.split(r'title\s*=\s*"Event-loop lag windows', MONITORING, maxsplit=1)[1].split("\n      },", 1)[0]
    assert 'stat = "Maximum"' in lag
    assert '"LoopLagP99"' in lag and '"LoopLagMax"' in lag
    room = re.split(r'title\s*=\s*"Room members dropped', MONITORING, maxsplit=1)[1].split("\n      },", 1)[0]
    assert '"RoomMemberDropped"' in room and '"RoomMemberLost"' in room
    assert "expression" not in room


def test_alarms_use_raw_p90_samples_and_attempt_error_denominators():
    assert 'SpeechEndToFirstPlayed = { stat = "p90"' in MONITORING
    assert 'TtsQueueWait           = { stat = "p90"' in MONITORING
    assert '100 * FILL(errors, 0) / requests' in MONITORING
    assert 'requests = "UpstreamRequests", errors = "UpstreamErrors"' in MONITORING
    assert 'for_each            = toset(["gateway", "elevenlabs"])' in MONITORING
    assert 'evaluate_low_sample_count_percentiles' in MONITORING


def test_container_insights_uses_used_reserved_with_service_dimensions():
    assert 'namespace   = "ECS/ContainerInsights"' in MONITORING
    assert 'used = "CpuUtilized", reserved = "CpuReserved"' in MONITORING
    assert 'used = "MemoryUtilized", reserved = "MemoryReserved"' in MONITORING
    assert 'ClusterName = aws_ecs_cluster.main.name' in MONITORING
    assert 'ServiceName = aws_ecs_service.agent.name' in MONITORING
    assert '100 * used / reserved' in MONITORING


def test_dashboard_cohort_collection_per_task_gauge_and_optional_email():
    assert 'max(ActiveSessions) by bin(1m), @logStream' in MONITORING
    assert '"EncounterStarted", "EncounterCompleted", "EncounterWithdrawn"' in MONITORING
    assert 'period = 3600' in MONITORING
    assert '"EncounterDurationS", "Cohort", "study"' in MONITORING
    assert 'count     = trimspace(var.monitoring_email) == "" ? 0 : 1' in MONITORING
    email = VARIABLES.split('variable "monitoring_email" {', 1)[1].split('variable "monitoring_thresholds"', 1)[0]
    assert 'default     = ""' in email
    assert 'protocol  = "email"' in MONITORING
