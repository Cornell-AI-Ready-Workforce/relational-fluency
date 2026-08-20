# HTTPS for the study hostnames: one DNS-validated ACM cert covering both, and
# Route 53 alias records pointing at the ALB.
#
#   rf.ai-ready-workforce.ai.cornell.edu       participant entrance (app + WSS broker)
#   api.rf.ai-ready-workforce.ai.cornell.edu   backend API
#
# Cornell IT delegated the ai-ready-workforce.ai.cornell.edu zone to Route 53,
# so these records are self-service — no ticket per change.

data "aws_route53_zone" "main" {
  name = var.domain_name
}

locals {
  app_fqdn = "${var.app_subdomain}.${var.domain_name}"
  api_fqdn = "${var.api_subdomain}.${var.domain_name}"
}

resource "aws_acm_certificate" "main" {
  domain_name               = local.app_fqdn
  subject_alternative_names = [local.api_fqdn]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.main.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id         = data.aws_route53_zone.main.zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.record]
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "main" {
  certificate_arn         = aws_acm_certificate.main.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

resource "aws_route53_record" "app" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = local.app_fqdn
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "api" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = local.api_fqdn
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
