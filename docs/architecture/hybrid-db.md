# 하이브리드 DB 아키텍처

이번 범위는 기존 가상머신의 단일 Patroni 클러스터입니다. 앱·Kubernetes·클라우드 자원 생성은 다른 디렉터리의 향후 구현 대상입니다. CloudNativePG는 같은 PostgreSQL 인스턴스를 관리하지 않습니다.

Ansible 실행 호스트 → SSH → 대상 서버. Patroni → 상호 인증 TLS → etcd. HAProxy → 인증서 검증 → Patroni 상태. 애플리케이션 → HAProxy의 TCP 전달 → PostgreSQL TLS 접속입니다.

세 개 etcd를 온프레미스·클라우드 A·클라우드 B의 독립 장애 영역에 분산하는 예시입니다. 실제로 독립되지 않은 영역에 이름만 다르게 붙여도 내결함성이 생기지 않습니다. 두 거점에 2:1로 배치하면 두 노드가 있는 거점 상실 시 과반수를 잃습니다. 네트워크 지연과 동기 복제로 인한 쓰기 지연을 실측해야 합니다.

기본 중계 서버 하나는 단일 장애점입니다. proxy_nodes를 늘리는 것만으로 단일 접속 주소가 이중화되지는 않습니다. 외부 부하분산 또는 클라이언트 다중 주소 정책을 별도로 마련해야 합니다.

저장소 경계: infrastructure/ansible은 서버 설정, infrastructure/providers 및 terraform은 향후 자원 생성, deployment는 Kubernetes 배포, gitops는 선언형 배포 관리, observability는 관측 정의입니다. 비어 있는 경로는 구조만 보존하며 구현 완료를 의미하지 않습니다.
