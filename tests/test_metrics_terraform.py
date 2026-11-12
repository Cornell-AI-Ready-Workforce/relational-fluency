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


def block(pattern):
    """Read a balanced HCL block, including nested metric query blocks."""
    match = re.search(pattern + r"\s*\{", MONITORING)
    assert match, pattern
    start, depth = match.end(), 1
    for index in range(start, len(MONITORING)):
        depth += (MONITORING[index] == "{") - (MONITORING[index] == "}")
        if depth == 0:
            return MONITORING[start:index]
    raise AssertionError("unclosed HCL block")


def literal(body, name):
    value = re.search(rf'\b{re.escape(name)}\s*=\s*("[^"\n]*"|[0-9]+)', body)
    assert value, (name, body)
    text = value[1]
    return text[1:-1] if text.startswith('"') else int(text)


def math_value(expression, **values):
    """Evaluate only arithmetic/comparisons from the configured IF expression."""
    import ast
    tree = ast.parse(expression, mode="eval")
    allowed = (ast.Expression, ast.BinOp, ast.Compare, ast.Name, ast.Load,
               ast.Constant, ast.Mult, ast.Div, ast.GtE)
    assert all(isinstance(node, allowed) for node in ast.walk(tree))
    return eval(compile(tree, "<metric-math>", "eval"), {"__builtins__": {}}, values)


def test_upstream_alarm_requires_three_errors_and_two_breaching_minutes():
    alarm = block(r'resource "aws_cloudwatch_metric_alarm" "upstream_error_rate"')
    expression = literal(alarm, "expression")
    condition, when_true, when_false = re.fullmatch(r"IF\(([^,]+),([^,]+),([^,]+)\)", expression).groups()

    def rate(errors, requests):
        branch = when_true if math_value(condition, errors=errors, requests=requests) else when_false
        return math_value(branch.strip(), errors=errors, requests=requests)

    threshold = literal(VARIABLES.split("default = {", 1)[1], "upstream_error_percent")
    periods = literal(alarm, "evaluation_periods")
    required = literal(alarm, "datapoints_to_alarm")
    assert (periods, required) == (3, 2)
    assert rate(0, 0) == rate(1, 1) == rate(2, 2) == 0
    assert rate(3, 100) == 3 and rate(3, 10) == 30
    assert sum(rate(e, r) > threshold for e, r in [(1, 1), (2, 2), (3, 10)]) < required
    assert sum(rate(e, r) > threshold for e, r in [(1, 1), (3, 10), (3, 10)]) >= required
    assert 'dimensions  = { Upstream = each.key }' in alarm
    assert 'stat        = "Sum"' in alarm


def test_both_loop_lag_alarms_require_two_of_three_windows():
    alarm = block(r'resource "aws_cloudwatch_metric_alarm" "application"')
    assert re.search(r"evaluation_periods\s*=\s*each.value.evaluation", alarm)
    assert re.search(r"datapoints_to_alarm\s*=\s*each.value.datapoints", alarm)
    threshold = literal(VARIABLES.split("default = {", 1)[1], "loop_lag_ms")
    assert threshold == 250
    for name in ("LoopLagMax", "LoopLagP99"):
        config = block(rf"\b{name}\s*=")
        assert literal(config, "stat") == "Maximum"
        assert literal(config, "period") == 60
        periods, required = literal(config, "evaluation"), literal(config, "datapoints")
        assert (periods, required) == (3, 2)
        assert sum(value > threshold for value in [500, 100, 100]) < required
        assert sum(value > threshold for value in [500, 100, 500]) >= required


def test_alb_5xx_volume_and_stopped_task_alarm_semantics():
    alarm = block(r'resource "aws_cloudwatch_metric_alarm" "alb"')
    for field in ("period", "comparison_operator", "treat_missing_data"):
        source = {"comparison_operator": "comparison", "treat_missing_data": "missing"}.get(field, field)
        assert re.search(rf"{field}\s*=\s*each.value.{source}\b", alarm)
    assert re.search(r"threshold\s*=\s*each.value.threshold", alarm)
    errors = block(r"\bALB5xx\s*=")
    assert literal(errors, "metric") == "HTTPCode_ELB_5XX_Count"
    assert literal(errors, "stat") == "Sum"
    assert literal(errors, "period") == 300
    assert literal(errors, "threshold") == 3
    assert literal(errors, "comparison") == "GreaterThanOrEqualToThreshold"
    assert [count >= literal(errors, "threshold") for count in (1, 2, 3)] == [False, False, True]
    healthy = block(r"\bHealthyHostCount\s*=")
    assert literal(healthy, "stat") == "Minimum"
    assert literal(healthy, "threshold") == 1
    assert literal(healthy, "comparison") == "LessThanThreshold"
    assert literal(healthy, "missing") == "breaching"
    assert "TargetGroup = aws_lb_target_group.agent.arn_suffix" in healthy
    assert "LoadBalancer = aws_lb.main.arn_suffix" in healthy
    assert 0 < literal(healthy, "threshold") and not 1 < literal(healthy, "threshold")
