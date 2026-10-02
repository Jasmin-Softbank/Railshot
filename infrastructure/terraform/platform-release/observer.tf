# One-time bootstrap owns these additional rules on the existing rules-only SG.
# The node module's inline-owned SG is deliberately not a target of this module.
resource "aws_vpc_security_group_ingress_rule" "release_observer" {
  for_each                     = toset(["31490", "31491"])
  security_group_id            = "sg-0ad2a18168c4eac2b"
  referenced_security_group_id = "sg-01a71e8be9a585b5a"
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.key)
  to_port                      = tonumber(each.key)
  description                  = "Railshot control observer runtime metrics ${each.key}"
}
