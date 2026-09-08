resource "aws_lb" "main" {
  name               = "${var.project}-alb"
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = module.vpc.public_subnets

  idle_timeout = 120 # streaming chat completions can hold connections open
}

resource "aws_lb_target_group" "agent" {
  name        = "${var.project}-agent"
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = module.vpc.vpc_id
  target_type = "ip"

  health_check {
    path                = "/health"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
    matcher             = "200"
  }

  # The app keeps per-instance state (run files on local disk, sqlite index,
  # in-memory sessions). Pin each client to one task so a participant's follow-up
  # requests and the researcher console reach the task that holds their run.
  # NOTE: researcher + participant for one run must land on the same task.
  stickiness {
    type            = "lb_cookie"
    enabled         = true
    cookie_duration = 86400
  }

  # Keep the ALB from force-closing a draining target's connections at the 30s
  # default. NOTE: this does NOT let a multi-minute voice encounter finish — the
  # container itself is still SIGKILLed at Fargate's hard 120s stopTimeout cap
  # (see ecs.tf), so a deploy rolled mid-encounter drops that session. Deploy
  # between collection sessions.
  deregistration_delay = 1800
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.main.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.agent.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}
