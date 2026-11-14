# EMF is extracted from the existing awslogs stdout stream. No PutMetricData
# permission is needed. Application dimensions contain no encounter identifiers.
resource "aws_sns_topic" "monitoring" {
  name = "${var.project}-collection-alarms"
}

resource "aws_sns_topic_policy" "monitoring" {
  arn = aws_sns_topic.monitoring.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudwatch.amazonaws.com" }
      Action    = "sns:Publish"
      Resource  = aws_sns_topic.monitoring.arn
      Condition = {
        StringEquals = { "aws:SourceAccount" = split(":", aws_sns_topic.monitoring.arn)[4] }
        ArnLike = {
          "aws:SourceArn" = "arn:${split(":", aws_sns_topic.monitoring.arn)[1]}:cloudwatch:${var.region}:${split(":", aws_sns_topic.monitoring.arn)[4]}:alarm:${var.project}-*"
        }
      }
    }]
  })
}

resource "aws_sns_topic_subscription" "monitoring_email" {
  count     = trimspace(var.monitoring_email) == "" ? 0 : 1
  topic_arn = aws_sns_topic.monitoring.arn
  protocol  = "email"
  endpoint  = trimspace(var.monitoring_email)
}

locals {
  application_alarms = {
    LoopLagMax             = { stat = "Maximum", threshold = var.monitoring_thresholds.loop_lag_ms, period = 60, evaluation = 3, datapoints = 2 }
    LoopLagP99             = { stat = "Maximum", threshold = var.monitoring_thresholds.loop_lag_ms, period = 60, evaluation = 3, datapoints = 2 }
    SpeechEndToFirstPlayed = { stat = "p90", threshold = var.monitoring_thresholds.first_played_ms, period = 300, evaluation = 1, datapoints = 1 }
    TtsQueueWait           = { stat = "p90", threshold = var.monitoring_thresholds.tts_queue_ms, period = 60, evaluation = 1, datapoints = 1 }
    ReplyMissing           = { stat = "Sum", threshold = 0, period = 60, evaluation = 1, datapoints = 1 }
    ArchiveFailed          = { stat = "Sum", threshold = 0, period = 60, evaluation = 1, datapoints = 1 }
  }
  ecs_dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.agent.name
  }
  ecs_utilization = {
    cpu    = { used = "CpuUtilized", reserved = "CpuReserved", threshold = var.monitoring_thresholds.cpu_percent }
    memory = { used = "MemoryUtilized", reserved = "MemoryReserved", threshold = var.monitoring_thresholds.memory_percent }
  }
  alb_alarms = {
    ALB5xx = {
      metric     = "HTTPCode_ELB_5XX_Count", stat = "Sum", threshold = 3, period = 300
      comparison = "GreaterThanOrEqualToThreshold", missing = "notBreaching"
      dimensions = { LoadBalancer = aws_lb.main.arn_suffix }
    }
    UnHealthyHostCount = {
      metric     = "UnHealthyHostCount", stat = "Maximum", threshold = 0, period = 60
      comparison = "GreaterThanThreshold", missing = "notBreaching"
      dimensions = { LoadBalancer = aws_lb.main.arn_suffix, TargetGroup = aws_lb_target_group.agent.arn_suffix }
    }
    HealthyHostCount = {
      metric     = "HealthyHostCount", stat = "Minimum", threshold = 1, period = 60
      comparison = "LessThanThreshold", missing = "breaching"
      dimensions = { LoadBalancer = aws_lb.main.arn_suffix, TargetGroup = aws_lb_target_group.agent.arn_suffix }
    }
  }
}

# Deliberately use global rollups: internal probes must also raise fault alarms.
# Percentile alarms evaluate sparse samples; the dashboard also shows counts.
resource "aws_cloudwatch_metric_alarm" "application" {
  for_each                              = local.application_alarms
  alarm_name                            = "${var.project}-${each.key}"
  alarm_description                     = "${each.key} > ${each.value.threshold}; see Watching a collection session in docs/OPERATIONS.md."
  namespace                             = "RelationalFluency"
  metric_name                           = each.key
  statistic                             = startswith(each.value.stat, "p") ? null : each.value.stat
  extended_statistic                    = startswith(each.value.stat, "p") ? each.value.stat : null
  evaluate_low_sample_count_percentiles = startswith(each.value.stat, "p") ? "evaluate" : null
  period                                = each.value.period
  threshold                             = each.value.threshold
  comparison_operator                   = "GreaterThanThreshold"
  evaluation_periods                    = each.value.evaluation
  datapoints_to_alarm                   = each.value.datapoints
  treat_missing_data                    = "notBreaching"
  alarm_actions                         = [aws_sns_topic.monitoring.arn]
  ok_actions                            = [aws_sns_topic.monitoring.arn]
}

resource "aws_cloudwatch_metric_alarm" "upstream_error_rate" {
  for_each            = toset(["gateway", "elevenlabs"])
  alarm_name          = "${var.project}-UpstreamErrorRate-${each.key}"
  alarm_description   = "Per-attempt error percent, including recovered retries; cancellations are separate; inspect upstream_attempt events by route and purpose."
  threshold           = var.monitoring_thresholds.upstream_error_percent
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.monitoring.arn]
  ok_actions          = [aws_sns_topic.monitoring.arn]

  metric_query {
    id          = "rate"
    expression  = "IF(errors >= 3, 100 * errors / requests, 0)"
    label       = "${each.key} attempt error %"
    return_data = true
  }
  dynamic "metric_query" {
    for_each = { requests = "UpstreamRequests", errors = "UpstreamErrors" }
    content {
      id          = metric_query.key
      return_data = false
      metric {
        namespace   = "RelationalFluency"
        metric_name = metric_query.value
        dimensions  = { Upstream = each.key }
        period      = 60
        stat        = "Sum"
      }
    }
  }
}

# Container Insights supplies units/MiB, not utilization percentages.
resource "aws_cloudwatch_metric_alarm" "ecs_utilization" {
  for_each            = local.ecs_utilization
  alarm_name          = "${var.project}-ECS-${each.key}"
  alarm_description   = "Container Insights service used/reserved percentage; check task resources and loop lag."
  threshold           = each.value.threshold
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  treat_missing_data  = "missing"
  alarm_actions       = [aws_sns_topic.monitoring.arn]
  ok_actions          = [aws_sns_topic.monitoring.arn]
  metric_query {
    id          = "percent"
    expression  = "IF(reserved > 0, 100 * used / reserved, 0)"
    label       = "${each.key} %"
    return_data = true
  }
  dynamic "metric_query" {
    for_each = { used = each.value.used, reserved = each.value.reserved }
    content {
      id          = metric_query.key
      return_data = false
      metric {
        namespace   = "ECS/ContainerInsights"
        metric_name = metric_query.value
        dimensions  = local.ecs_dimensions
        period      = 60
        stat        = "Average"
      }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "alb" {
  for_each            = local.alb_alarms
  alarm_name          = "${var.project}-${each.key}"
  alarm_description   = each.key == "HealthyHostCount" ? "No healthy target, including a stopped task or missing health data; inspect ECS service events." : "${each.value.metric} ${each.value.comparison} ${each.value.threshold}; inspect ALB target health and ECS service events."
  namespace           = "AWS/ApplicationELB"
  metric_name         = each.value.metric
  dimensions          = each.value.dimensions
  statistic           = each.value.stat
  period              = each.value.period
  threshold           = each.value.threshold
  comparison_operator = each.value.comparison
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = each.value.missing
  alarm_actions       = [aws_sns_topic.monitoring.arn]
  ok_actions          = [aws_sns_topic.monitoring.arn]
}

resource "aws_cloudwatch_dashboard" "collection" {
  dashboard_name = "${var.project}-collection"
  dashboard_body = jsonencode({
    widgets = concat([
      {
        type = "alarm", x = 0, y = 0, width = 24, height = 3
        properties = {
          title = "Collection alarms"
          alarms = concat(
            [for alarm in aws_cloudwatch_metric_alarm.application : alarm.arn],
            [for alarm in aws_cloudwatch_metric_alarm.upstream_error_rate : alarm.arn],
            [for alarm in aws_cloudwatch_metric_alarm.ecs_utilization : alarm.arn],
            [for alarm in aws_cloudwatch_metric_alarm.alb : alarm.arn]
          )
        }
      },
      {
        type = "log", x = 0, y = 3, width = 12, height = 6
        properties = {
          title  = "ActiveSessions per task (registry sessions; each log stream is one task)"
          region = var.region, view = "timeSeries"
          query  = "SOURCE '${aws_cloudwatch_log_group.agent.name}' | filter ispresent(ActiveSessions) | stats max(ActiveSessions) by bin(1m), @logStream"
        }
      },
      {
        type = "metric", x = 12, y = 3, width = 12, height = 6
        properties = {
          title   = "Event-loop lag windows (Maximum; not a percentile of p99s)"
          region  = var.region, view = "timeSeries", period = 60, stat = "Maximum"
          metrics = [["RelationalFluency", "LoopLagP99"], ["RelationalFluency", "LoopLagMax"]]
        }
      },
      {
        type = "metric", x = 0, y = 9, width = 12, height = 6
        properties = {
          title  = "Speech end to earliest played reply (ms; next speech end or 30 s hold)"
          region = var.region, view = "timeSeries", period = 300, stat = "p90"
          metrics = [["RelationalFluency", "SpeechEndToFirstPlayed"],
          ["RelationalFluency", "SpeechEndToFirstPlayed", { stat = "SampleCount", label = "Played speech ends", yAxis = "right" }]]
        }
      },
      {
        type = "metric", x = 12, y = 9, width = 12, height = 6
        properties = {
          title   = "Missing replies, missing playback ACKs, voice errors, archive failures"
          region  = var.region, view = "timeSeries", period = 60, stat = "Sum"
          metrics = [for name in ["ReplyMissing", "FirstPlayedMissing", "VoiceError", "ArchiveFailed"] : ["RelationalFluency", name]]
        }
      },
      {
        type = "metric", x = 0, y = 15, width = 12, height = 6
        properties = {
          title   = "Gateway latency, TTS stream lifetime and slot wait (ms)"
          region  = var.region, view = "timeSeries", period = 60, stat = "p90"
          metrics = [["RelationalFluency", "TtsQueueWait"], ["RelationalFluency", "UpstreamLatency", "Upstream", "gateway", { label = "Gateway request latency (ms)" }], ["RelationalFluency", "UpstreamLatency", "Upstream", "elevenlabs", { label = "TTS stream lifetime (ms)" }]]
        }
      },
      {
        type = "metric", x = 12, y = 15, width = 12, height = 6
        properties = {
          title   = "Room members dropped / lost (separate counts; never sum)"
          region  = var.region, view = "timeSeries", period = 60, stat = "Sum"
          metrics = [["RelationalFluency", "RoomMemberDropped"], ["RelationalFluency", "RoomMemberLost"]]
        }
      },
      {
        type = "metric", x = 0, y = 21, width = 12, height = 6
        properties = {
          title   = "Study encounters per hour"
          region  = var.region, view = "timeSeries", period = 3600, stat = "Sum"
          metrics = [for name in ["EncounterStarted", "EncounterCompleted", "EncounterWithdrawn"] : ["RelationalFluency", name, "Cohort", "study"]]
        }
      },
      {
        type = "metric", x = 12, y = 21, width = 12, height = 6
        properties = {
          title   = "Study session lifetime including setup (seconds)"
          region  = var.region, view = "timeSeries", period = 3600, stat = "p90"
          metrics = [["RelationalFluency", "EncounterDurationS", "Cohort", "study", { stat = "p50" }], ["RelationalFluency", "EncounterDurationS", "Cohort", "study"]]
        }
      }
      ], [for index, upstream in ["gateway", "elevenlabs"] : {
        type = "metric", x = index * 12, y = 27, width = 12, height = 6
        properties = {
          title  = "${upstream} attempts, cancellations and error percent"
          region = var.region, view = "timeSeries", period = 60, stat = "Sum"
          metrics = [
            ["RelationalFluency", "UpstreamRequests", "Upstream", upstream, { id = "requests" }],
            ["RelationalFluency", "UpstreamErrors", "Upstream", upstream, { id = "errors" }],
            ["RelationalFluency", "UpstreamCancelled", "Upstream", upstream, { id = "cancelled" }],
            [{ expression = "IF(requests > 0, 100 * FILL(errors, 0) / requests, 0)", label = "Attempt error %", yAxis = "right" }]
          ]
        }
        }], [for index, kind in ["cpu", "memory"] : {
        type = "metric", x = index * 12, y = 33, width = 12, height = 6
        properties = {
          title  = "ECS service ${kind} utilization (Container Insights)"
          region = var.region, view = "timeSeries", period = 60, stat = "Average"
          metrics = [
            ["ECS/ContainerInsights", local.ecs_utilization[kind].used, "ClusterName", aws_ecs_cluster.main.name, "ServiceName", aws_ecs_service.agent.name, { id = "used", visible = false }],
            ["ECS/ContainerInsights", local.ecs_utilization[kind].reserved, "ClusterName", aws_ecs_cluster.main.name, "ServiceName", aws_ecs_service.agent.name, { id = "reserved", visible = false }],
            [{ expression = "IF(reserved > 0, 100 * used / reserved, 0)", label = "${kind} %" }]
          ]
        }
      }], [
      {
        type = "metric", x = 0, y = 39, width = 12, height = 6
        properties = {
          title   = "ALB and target 5xx responses"
          region  = var.region, view = "timeSeries", period = 60, stat = "Sum"
          metrics = [["AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", "LoadBalancer", aws_lb.main.arn_suffix], ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", aws_lb.main.arn_suffix]]
        }
      },
      {
        type = "metric", x = 12, y = 39, width = 12, height = 6
        properties = {
          title   = "Target health (healthy Minimum, unhealthy Maximum)"
          region  = var.region, view = "timeSeries", period = 60, stat = "Maximum"
          metrics = [["AWS/ApplicationELB", "UnHealthyHostCount", "LoadBalancer", aws_lb.main.arn_suffix, "TargetGroup", aws_lb_target_group.agent.arn_suffix], ["AWS/ApplicationELB", "HealthyHostCount", "LoadBalancer", aws_lb.main.arn_suffix, "TargetGroup", aws_lb_target_group.agent.arn_suffix, { stat = "Minimum" }]]
        }
      }
    ])
  })
}
