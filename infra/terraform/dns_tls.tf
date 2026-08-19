# HTTPS for the agent endpoint: ACM cert (DNS-validated) + Route 53 alias.

data "aws_route53_zone" "main" {
  name = var.domain_name
}

locals {
  agent_fqdn = "${var.agent_subdomain}.${var.domain_name}"
}

resource "aws_acm_certificate" "agent" {
  domain_name       = local.agent_fqdn
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.agent.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id = data.aws_route53_zone.main.zone_id
  name    = each.value.name
  type    = each.value.type
  ttl     = 60
  records = [each.value.record]
}

resource "aws_acm_certificate_validation" "agent" {
  certificate_arn         = aws_acm_certificate.agent.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

resource "aws_route53_record" "agent" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = local.agent_fqdn
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
