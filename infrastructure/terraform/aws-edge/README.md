# AWS 선택형 진입 어댑터

10/1 허들 3:24:31–3:25:48의 EIP·WireGuard 51820 요청과 Route53/ALB 도면을 코드로 구체화한 **미적용 모듈**이다. 공통 애플리케이션 명세에 AWS 종속 필드를 넣지 않는다.

`Route53 Alias → ALB HTTPS443 → AWS instance private IP:명시한 NodePort`와 `고객 peer → gateway EIP:UDP51820`를 별도로 생성한다. Route53은 DNS 응답이며 HTTP 프록시가 아니다. ALB에 EIP를 붙이지 않는다. 온프레 서비스 공개 경로도 이 ALB에 자동 편입하지 않는다.

운영자가 기존 VPC, **다른 두 AZ**의 public subnet, IGW 경로, 타깃 instance/SG, 인증된 ACM 인증서, DNS zone/domain, 실제 Service NodePort, WireGuard gateway ENI/SG와 peer NAT 출구 CIDR을 제공해야 한다. subnet 수만 입력 검증하며 AZ·라우팅·인증서 소유권은 실제 plan과 운영 검토가 필요하다. gateway ENI는 다른 EIP와 연결되지 않아야 하며, 해당 SG가 실제 ENI에 붙어 있어야 한다. 기존 SG에 inline ingress와 별도 rule을 혼용하면 drift가 날 수 있으므로 이 모듈의 규칙을 관리할 전용 SG를 넘긴다.

이 모듈은 WireGuard 패키지·키·peer·AllowedIPs·forwarding·경로/MTU를 구성하지 않는다. 51820 ingress만으로 터널이 완성되지 않는다. guest의 WireGuard 설정과 고객 client/API 인증은 화균 담당 지원 레이어와 합쳐 인수한다. 서버가 터널의 return route가 아닌 공용 인터넷으로 peer에 선제 패킷을 보낼 경우 별도 outbound UDP 정책도 필요하다.

웹 타깃은 고객 workload NodePort이며 현재 localhost 전용 RAILSHOT UI를 공개하기 위한 인증 대체물이 아니다. TLS는 ALB에서 종료되고 backend HTTP는 VPC 보안 경계 안에 있다. 운영 API는 별도 인증·Host/Origin 계약이 완료되기 전 공개 대상으로 등록하지 않는다.

검증은 `terraform fmt -check`, `terraform init -backend=false`, `terraform validate`까지다. apply·계정 조회·배포는 수행하지 않는다. DNS/TLS·ALB target health·실외 HTTP·WireGuard handshake는 별도 인수 증거로 남긴다. ALB가 두 AZ에 있어도 단일 workload 노드의 HA를 보장하지 않는다.

근거: [Route53 Alias](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/routing-to-elb-load-balancer.html), [ALB 생성](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/create-application-load-balancer.html), [EIP 지정 가능 LB 유형](https://docs.aws.amazon.com/elasticloadbalancing/latest/APIReference/API_SetSubnets.html), [WireGuard](https://www.wireguard.com/quickstart/).
