# AWS 공유 앱 진입점

`Route53 Alias → 공유 ALB HTTPS443 → 고객 노드 사설 IP:NodePort`를 구성한다. AWS와 GCP에 각각 만든 고객 K3s 한 노드를 여러 앱이 재사용하며, 앱마다 namespace/Application과 NodePort를 별도로 할당한다. 이 모듈은 등록된 NodePort·호스트를 연결한다. K3s/Cilium·Argo·앱 manifest·NodePort 할당·WireGuard guest 설정은 설치하지 않는다.

운영 AWS EC2의 기존 primary ENI에 EIP를 연결해 WireGuard gateway로 사용한다. 별도 gateway VM을 만들지 않고 ALB에도 EIP를 붙이지 않는다. GCP 앱 target은 터널로 도달할 수 있는 RFC1918 IPv4만 허용한다. 공개 IP, metadata IP, IPv6 및 RFC6598 범위는 거부한다. GCP target attachment는 VPC 밖 IP에 필요한 `availability_zone = all`을 사용한다.

이 코드는 기존 single-app 미적용 인터페이스를 대체한다. [gitops/edge.py](../../../gitops/edge.py)가 환경 등록에서 할당한 신규 앱 route를 기존 routes에 추가하고 saved plan을 검사·적용하는 caller다. 종전 `target_instance_ids`, `target_security_group_id`, `target_port`, `health_path`, `app_domain` 입력 대신 아래 `routes`를 사용한다. 기존 state에 적용했던 별도 운영 환경이 있다면 자동 이관하지 말고 먼저 state/plan을 검토한다. [실행·인수 계약](../../../gitops/README.md#신규-앱-주소와-공유-edge-연결)을 따르며 적용만으로 공개 HTTP 성공을 주장하지 않는다.

## 입력 예시

아래 IP/ID는 형식 예제다. 실제 노드 descriptor, 서비스 NodePort와 관리자 조회 결과로 바꾼다. `base_domain`은 구매·위임이 끝난 도메인으로 지정하며, 이 모듈이 도메인 등록을 수행하지는 않는다.

```hcl
name       = "railshot-edge"
account_id = "123456789012"
region     = "ap-northeast-2"
vpc_id     = "vpc-0123456789abcdef0"
public_subnet_ids = ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"]
zone_id        = null # 새 public zone 생성; 기존 zone이면 실제 Z... ID
base_domain    = "railshot.io"
certificate_arn = null # 기존 regional ACM ARN도 가능
http_redirect   = true

routes = {
  demo-a1b2c3 = {
    host                     = "demo-a1b2c3.railshot.io"
    provider_kind            = "aws"
    target_private_ip        = "172.31.10.20"
    node_port                = 30080
    health_path              = "/healthz"
    priority                 = 100
    target_security_group_id = "sg-0123456789abcdef1"
  }
  demo-d4e5f6 = {
    host              = "demo-d4e5f6.railshot.io"
    provider_kind     = "gcp"
    target_private_ip = "10.66.0.2"
    node_port         = 30081
    health_path       = "/ready"
    priority          = 200
  }
}
wireguard_network_interface_id = "eni-0123456789abcdef0"
wireguard_security_group_id    = "sg-0123456789abcdef0"
wireguard_peer_cidrs           = ["192.0.2.20/32"] # 실제 GCP 고정 공인 IP
wireguard_route_table_ids      = ["rtb-0123456789abcdef0"]
```

앱 key·host는 서비스명+안정적인 hash 등 호출자가 정한 값을 사용한다. `routes`는 1–50개를 받고 host와 listener priority는 중복될 수 없다. 한 고객 노드에 앱을 더 배포할 때 새 VM 대신 별도 NodePort와 route entry를 추가한다. 같은 IP/포트의 별칭을 등록해도 AWS SG 규칙과 GCP /32 경로를 중복 생성하지 않는다.

ALB는 서로 다른 두 AZ의 지정 VPC subnet을 확인한다. 운영자는 각 subnet의 실제 IGW 경로와 유효 route table을 확인한다. HTTPS 기본 응답은 404이며 등록된 host만 전달한다. `http_redirect`가 true일 때만 80을 열어 HTTPS로 redirect한다. `web_client_cidrs` 기본값은 공개 웹 `0.0.0.0/0`이며 관리 API 공개 여부를 대신 승인하지 않는다.

`zone_id`가 null이거나 생략되면 `base_domain`의 **public Route53 zone**을 만든다. 기존 ID를 넣으면 새 zone을 만들지 않고 같은 이름의 public zone인지 확인해 재사용한다. 신규 생성 전 같은 도메인의 zone이 이미 있는지 관리자가 확인한다. 관리하는 zone에 `prevent_destroy`를 적용했으므로 생성 후에도 입력을 null로 유지한다. 출력 ID를 다시 입력에 넣으면 관리 대상 제거가 되므로 그렇게 전환하지 않는다.

새 도메인은 private backend/입력을 준비한 후 먼저 `terraform plan -target=aws_route53_zone.app -out=<private-zone-plan>`으로 zone만 계획하고, 그 saved plan을 검토·적용한다. 출력 `zone_id`와 `name_servers`를 확인해 registrar에서 정확한 NS로 위임한다. 그 다음 **target 옵션 없이 전체 plan을 새로 만들고** 검토·적용해 ACM/ALB를 구성한다. 부분 apply는 NS 위임을 준비하는 단계이며 edge 전체 적용이나 앱 공개 완료가 아니다.

`certificate_arn`이 null이면 `*.base_domain` ACM 인증서, DNS 검증 record와 검증 대기를 만든다. Route host는 기본적으로 base domain 바로 아래 한 label만 허용한다. `railshot.io` 같은 apex route는 같은 region/account에서 발급·DNS 검증을 마친 `apex_certificate_arn`을 명시해야 한다. 모듈은 기존 wildcard/default 인증서를 교체하지 않고 추가 SNI 인증서만 연결한다. Apex 인증서와 DNS 검증 receipt는 운영자가 보관하고, 기존 검증 CNAME을 재사용할 때 중복 Terraform 소유자를 만들지 않는다. 기존 ARN을 쓰면 같은 region/account와 모든 host coverage를 운영자가 검증한다. 도메인의 실제 NS 위임이 끝나지 않으면 ACM DNS 검증이 완료되지 않는다. DNS 등록과 TLS 설정은 앱 준비 완료 증거가 아니다.

## 보안 그룹과 WireGuard 소유권

- ALB SG와 그 inline ingress/egress는 이 모듈만 소유한다. egress는 각 target /32와 NodePort에 제한한다.
- AWS 고객 노드에는 별도 **규칙 전용 SG**를 붙이고 그 ID를 route에 넣는다. 이 모듈은 ALB SG에서 해당 NodePort로 들어오는 규칙만 추가한다. 해당 SG에 다른 writer의 inline ingress를 섞지 않는다. 고객 호스트 기본 SG와 직접 공개 HTTP를 대체하는 별도 그룹이다.
- 운영 노드에도 별도 규칙 전용 SG를 미리 붙여 `wireguard_security_group_id`로 넘긴다. 지정 ENI에 실제로 붙어 있는지 확인한다. 이 모듈이 추가하는 규칙은 알려진 peer 공인 /32에 대한 UDP51820 양방향, 그리고 ALB subnet에서 GCP NodePort로 들어오는 전달 트래픽이다.
- 기존 운영 instance/ENI는 원래 Terraform 소유자가 관리한다. 그 소유 모듈에서 `source_dest_check = false`로 설정한다. edge는 primary ENI/소유 계정/VPC와 실제 instance의 false 값을 조회·검증하고 중복 소유하거나 shell로 변경하지 않는다.
- `wireguard_route_table_ids`에는 **모든 ALB subnet의 유효 route table**을 넣는다. 이 모듈은 등록된 GCP target별 `/32 → 운영 ENI` 경로만 추가한다. 같은 목적지 route를 다른 Terraform inline route block에서 소유하면 안 된다. ID의 VPC는 검사하지만 subnet별 실제 유효 route table 선택은 운영자가 확인한다.

EIP association은 다른 EIP의 재연결을 허용하지 않는다. 운영 ENI의 기존 EIP 사용 여부를 먼저 확인한다. VPC CIDR, 고객 노드 및 WireGuard overlay가 겹치지 않아야 한다. 기존 route를 이 모듈이 자동으로 인수하지 않는다.

GCP 노드가 아직 없으면 AWS 앱 route만 등록하고 `wireguard_peer_cidrs = []`, `wireguard_route_table_ids = []`로 먼저 DNS/TLS/ALB를 검증할 수 있다. 이때 운영 EIP는 배정하지만 UDP ingress/egress 규칙과 GCP route는 만들지 않는다. 가짜 peer IP로 채우지 않는다. GCP route를 추가할 때에는 실제 peer 공인 /32와 ALB route table을 함께 등록해야 한다.

GCP 방화벽에는 이 모듈의 `wireguard_public_ip/32`만 peer endpoint로 등록한다. 운영 guest의 IP forwarding, WireGuard 공개키·private key 주입, peer `AllowedIPs`, GCP target 사설 IP 경로 및 **GCP에서 ALB subnet으로 돌아오는 경로**, MTU와 host firewall은 별도 구성한다. 패킷을 실제 전달할 수 없는 상태에서 EIP·UDP 규칙·AWS route만 생성해도 터널은 완성되지 않는다. SG나 Terraform state에 WireGuard/SSH/registry private credential을 넣지 않는다.

## Guest WireGuard 설정 파일

`render_wireguard.py`는 root가 guest에 별도로 배치한 **0600 private key 파일**과 공개 peer JSON으로 설정 파일만 만든다. 키를 생성하거나 전송하지 않고, 키값을 stdout·명령 인자에 넣지 않는다. 공개 JSON에는 정확히 `address`, `peer`만 받는다.

```json
{
  "address": "10.200.0.1/30",
  "peer": {
    "public_key": "REPLACE_WITH_PEER_PUBLIC_KEY",
    "endpoint": "REPLACE_WITH_ACTUAL_PEER_PUBLIC_IPV4:51820",
    "allowed_ips": ["10.200.0.2/32", "10.66.0.2/32"]
  }
}
```

운영 노드 예시이며 CIDR은 기존 네트워크와 충돌하지 않도록 검토한다. GCP 쪽은 interface 주소와 상대 public key/endpoint를 반대로 지정하고, AllowedIPs에 운영 interface /32와 실제 ALB subnet의 return route를 넣는다. 공개/기본 경로, 자기 interface 주소를 포함하는 peer route, shell hook 추가는 거부한다. Endpoint는 실제 public IPv4:51820만 받는다. 실 endpoint가 없으면 파일을 가짜 값으로 완성하지 않는다.

```sh
sudo python3 render_wireguard.py \
  --public-config /etc/railshot/wireguard-peer.json \
  --private-key-file /etc/railshot/keys/wireguard.key \
  --output /etc/wireguard/wg-railshot.conf --check
```

검토 후 `--check`를 제거하면 0700 디렉터리에 0600 파일을 원자적으로 작성한다. 개인키와 출력은 checkout 밖의 절대 경로여야 하고 symlink·개인키 덮어쓰기는 거부한다. Receipt의 `installed`는 계속 false다. 명시적으로 검토한 뒤 guest에서 `wg-quick up wg-railshot`을 실행하고 필요한 IP forwarding·제한된 FORWARD 규칙을 따로 적용한다. Renderer는 패키지 설치, 서비스 시작, iptables 변경이나 모든 트래픽을 허용하는 규칙을 만들지 않는다. `wg show wg-railshot latest-handshakes`와 실제 경로 통신으로 검증하며 private key가 포함된 config 전체를 로그에 출력하지 않는다.

## 검증과 출력

```sh
python3 -m unittest discover -s infrastructure/terraform/aws-edge -p 'test_*.py'
terraform -chdir=infrastructure/terraform/aws-edge fmt -check
```

오프라인 테스트는 임시 source-only Terraform console로 변수 제한과 여러 앱의 SG·route 중복 제거를 확인한다. 실제 provider 검증은 별도 임시 복사본에서 locked provider로 `init -backend=false -lockfile=readonly`, `validate`를 사용한다. 이 검사들은 클라우드 plan/apply, DNS 위임, 인증서 발급 또는 앱 실행을 증명하지 않는다.

출력은 `zone_id`, `name_servers`, `app_urls`, `target_group_arns`, `alb_dns_name`, `alb_security_group_id`, `wireguard_endpoint`, `wireguard_public_ip`, `gcp_private_routes`, `certificate_arn`이다. `readiness`는 계속 `configured-references-only; runtime, tunnel and public HTTP unverified`다. 실제 인수는 WG handshake·양방향 route, 각 target health, DNS/TLS/host routing, NodePort health path와 외부 HTTPS를 각각 확인한다. ALB health check는 target IP와 포트를 Host로 사용하므로 지정 경로가 기본 virtual host에서도 응답해야 한다. 공유 ALB의 두 AZ가 각 단일 고객 노드의 HA를 보장하지는 않는다.

근거: [ALB IP target 제약](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-target-groups.html), [Terraform IP target attachment](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lb_target_group_attachment), [Route53 Alias](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/routing-to-elb-load-balancer.html), [WireGuard](https://www.wireguard.com/quickstart/).
