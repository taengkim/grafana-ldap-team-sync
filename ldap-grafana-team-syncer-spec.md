# ldap-grafana-team-syncer 요구사항 정의서

LDAP 그룹 멤버십을 Grafana OSS 팀 멤버십에 주기적으로 반영하는 멱등 동기화 잡의
개발 스펙이다. 기존 `grafana-keycloak-group-syncer`(Keycloak → Grafana)와 동일한
운영 모델·안전장치를 따르되, 소스만 LDAP 으로 바꾼 **별도 신규 프로젝트**다.

> **참조 소스**: 기존 저장소의 `sync.py` 중 Grafana 반영부
> (`GrafanaClient`, `sync_team`, `build_user_index`, `member_key`,
> `request_with_retry`, 제거 가드, 종료 코드, logfmt 로깅)는 이 스펙과 동일
> 동작이므로 **그대로 복사해 재사용해도 된다**. `README.md` 의 설정 표·배포
> 절차와 `k8s/cronjob.yaml`, `Dockerfile` 도 같은 형식을 기준으로 한다.
> LDAP 서버 종류(OpenLDAP/AD 등)가 미정이므로, LDAP 관련 속성명·필터는
> **전부 환경변수로 설정 가능**해야 한다.

---

## 1. 배경 / 목표

- Grafana OSS 에는 Enterprise 의 Team Sync 기능이 없다. IdP(LDAP) 그룹과
  Grafana 팀 멤버십이 수동 관리로 어긋나는 문제를 해결한다.
- LDAP 디렉터리의 그룹 멤버십을 **단일 진실 소스(source of truth)** 로 삼아,
  Grafana 팀 생성과 팀 멤버 추가/제거를 자동화한다.
- Kubernetes CronJob 으로 주기 실행(기본 10분)되며, **멱등**해야 한다:
  같은 상태에서 두 번 실행하면 두 번째는 `added=0 removed=0` 이어야 한다.
- org 롤(Viewer/Editor/Admin)은 Grafana 의 인증 연동(OAuth
  `role_attribute_path` 또는 LDAP auth 설정)이 담당한다. 이 도구는
  **팀 멤버십만** 책임진다.

## 2. 범위

**포함**

- LDAP 에서 대상 그룹 검색 및 그룹별 멤버 조회
- 멤버 DN/uid → 매칭 키(email 또는 login) 해석
- Grafana 팀 검색/생성, 팀 멤버 추가/제거 (diff 기반)

**제외 (별도 작업)**

- 폴더 ↔ 팀 권한 부여(Admin/Edit/View) — Terraform 또는 수동
- org 롤 매핑, grafana.ini/LDAP auth 설정 변경
- **팀 삭제**: LDAP 에서 그룹이 사라져도 Grafana 팀은 남는다. 이 잡은 관리
  대상 팀 이름으로만 조회하므로 고아 팀 정리는 수동으로 한다.
- **사용자 생성**: Grafana 사용자를 만들지 않는다. 아직 로그인한 적 없는
  사용자는 pending 으로 집계하고 다음 주기에 재시도한다.
- 중첩 그룹 해석(memberOf 체이닝, AD `LDAP_MATCHING_RULE_IN_CHAIN`) — §4 참고

## 3. 그룹 모델 — 플랫 컨벤션

Keycloak 도구는 그룹 트리(루트→서비스→역할 그룹)를 사용했지만, LDAP 은
디렉터리마다 그룹 구조가 다르고 계층 그룹이 없는 경우가 많다. 따라서 v1 은
**플랫 그룹 + 이름 컨벤션**을 기본 모델로 한다. 결과(역할별 독립 Grafana 팀)는
기존 도구와 동일하다.

### 3.1 대상 그룹 선별

1. `LDAP_GROUP_BASE_DN` 아래를 `LDAP_GROUP_FILTER`(기본
   `(objectClass=groupOfNames)`)로 subtree 검색한다.
2. 검색된 그룹 중 이름 속성(`LDAP_GROUP_NAME_ATTR`, 기본 `cn`)이
   `GROUP_PREFIX` 로 시작하는 그룹만 관리 대상으로 삼는다.
3. **그룹 cn 이 그대로 Grafana 팀 이름**이 된다.

### 3.2 이름 컨벤션 (역할 팀 모델)

- `<서비스명>_<suffix>` 또는 `<서비스명>-<suffix>` (대소문자 무관) 이고
  suffix ∈ `ROLE_SUFFIXES`(기본 `adm,admin,editor,viewer,member,mbr`)이면
  **역할 팀**이다. 각 역할 팀은 **독립 Grafana 팀**으로 동기화되며, 폴더 권한
  (Admin/Edit/View)은 이후 팀 단위로 별도 부여한다(Terraform, 범위 밖).
- suffix 가 없는 이름(예: `xyz`)은 서비스 팀으로, 역시 이름 그대로 동기화한다.
- `GROUP_PREFIX` 는 매칭 후 팀 이름에서 **제거하지 않는다** (cn 그대로 팀명).
  prefix 를 팀명에 포함하고 싶지 않은 운영이라면 prefix 를 짧게 잡거나 빈
  값 허용 여부를 구현 시 결정한다 — 단 Keycloak 도구와 동일하게
  `GROUP_PREFIX` 빈 값은 설정 오류(전체 그룹 동기화 방지)로 처리한다.

예: `GROUP_PREFIX=grafana-`, `ROLE_SUFFIXES=adm,editor,viewer` 일 때

```
ou=groups,dc=example,dc=com
├── cn=grafana-abc_adm       → Grafana 팀 "grafana-abc_adm"   (이후 abc 폴더 Admin)
├── cn=grafana-abc_editor    → Grafana 팀 "grafana-abc_editor"(이후 abc 폴더 Edit)
├── cn=grafana-abc_viewer    → Grafana 팀 "grafana-abc_viewer"(이후 abc 폴더 View)
├── cn=grafana-xyz           → Grafana 팀 "grafana-xyz"       (서비스 팀)
├── cn=grafana-legacy!ops    → 컨벤션 미인식: 경고 로그 후 스킵
└── cn=hr-payroll            → prefix 미매칭: 조회/변경 대상 아님
```

### 3.3 미인식 이름 / 대상 0개 처리

- prefix 로 시작하지만 팀 이름으로 쓸 수 없는 그룹(빈 cn, Grafana 팀명 제약
  위반 등)은 `unknown_group_skipped` 경고를 남기고 건너뛴다. suffix 검사는
  로그 참고용(어느 팀이 역할 팀인지 표시)이며, Keycloak 도구와 달리 플랫
  모델에서는 suffix 미인식이 스킵 사유가 아니다 — cn 이 유효하면 동기화한다.
  (스킵 정책을 strict 하게 바꿀 수 있도록 구현 시 상수로 분리해 둘 것.)
- prefix 매칭 그룹이 **0개면 `no_managed_groups` 경고만 남기고 아무것도
  변경하지 않은 채 종료 코드 0** 으로 끝난다. (필터/base DN 오설정으로 전체
  팀이 비워지는 사고 방지)

### 3.4 확장 여지 (v2 후보, 스펙에 명시만)

- 계층 그룹(OU 트리) 지원: `LDAP_GROUP_SCOPE`(subtree/onelevel) 는 v1 부터
  설정으로 열어두고, 트리 → 서비스/역할 매핑은 v2 에서 검토
- 중첩 그룹(그룹의 member 가 그룹인 경우) 해석 — v1 은 사용자 엔트리가
  아닌 member 는 경고 후 무시
- 그룹 cn → 팀명 변환 규칙(prefix 제거, 치환 맵) — v1 은 cn 그대로

## 4. LDAP 조회 요구사항

### 4.1 접속·인증

- **simple bind 전용**: `LDAP_BIND_DN` + `LDAP_BIND_PASSWORD` 로 bind 한다.
  둘 다 필수이며, **익명 bind 는 금지**한다(빈 password → 설정 오류, 종료 코드 2).
- 접속 스킴 3종 지원:
  - `ldap://host:389` (평문 — 내부망 테스트용)
  - `ldaps://host:636` (LDAP over TLS)
  - `ldap://` + `LDAP_STARTTLS=true` (StartTLS 승격)
- TLS 옵션은 기존 도구와 **동일한 변수명**을 사용한다:
  - `SSL_VERIFY=false` → 인증서 검증 생략, 시작 시 `ssl_verification_disabled`
    경고 (Grafana HTTPS 요청에도 동일 적용)
  - `SSL_CA_BUNDLE=/path/ca.pem` → 사설 CA 로 검증 유지 (권장)
- bind 실패, TLS 협상 실패는 인증 오류로 종료 코드 2.

### 4.2 그룹 멤버십 2모드

`LDAP_MEMBER_MODE` 로 선택한다.

| 모드 | 대상 스키마 | 멤버 속성 | 값 형태 |
|---|---|---|---|
| `member` (기본) | `groupOfNames`, AD `group` | `LDAP_MEMBER_ATTR` (기본 `member`) | 사용자 **DN** |
| `memberUid` | `posixGroup` | `LDAP_MEMBER_ATTR` (기본값이 `memberUid` 로 바뀜) | **uid 문자열** |

- **member 모드**: 각 DN 에 대해 사용자 엔트리를 조회(base=DN, scope=base)해
  `LDAP_USER_MATCH_ATTR`(기본 `mail`) 값을 매칭 키로 쓴다.
  - 한 실행 내 **사용자 캐시**(DN → 매칭 키)를 유지해, 여러 팀에 속한
    사용자를 중복 조회하지 않는다.
  - 조회 실패(엔트리 없음, referral)·매칭 속성 없음 → 해당 멤버만
    `member_missing_match_key` 경고 후 스킵.
- **memberUid 모드**: uid 문자열로 `LDAP_USER_BASE_DN` 아래에서
  `(&(<LDAP_USER_UID_ATTR>=<uid>)<LDAP_USER_FILTER>)` 검색해 사용자 엔트리를
  찾고, 동일하게 `LDAP_USER_MATCH_ATTR` 를 추출한다.
  - `MATCH_KEY=username` 이고 uid 자체가 Grafana login 과 일치하는 환경이면
    `LDAP_USER_MATCH_ATTR=uid` 로 두어 추가 조회 없이 동작 가능해야 한다
    (매칭 속성 = uid 속성이면 사용자 엔트리 조회 생략 최적화 허용).

### 4.3 비활성 계정 제외

- `LDAP_USER_FILTER` 를 사용자 엔트리 검증에 AND 조건으로 적용한다.
  기본값 `(objectClass=*)` (필터링 없음).
- member 모드에서도 DN 조회 시 이 필터를 적용해, 필터에 걸리지 않는
  사용자는 비활성으로 간주하고 제외한다(→ 팀에서 제거 대상이 됨).
- README 에 문서화할 대표 예시:
  - **AD 비활성 계정 제외**:
    `(&(objectClass=user)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))`
    (bit 2 = ACCOUNTDISABLE, matching rule OID 필터)
  - **OpenLDAP**(ppolicy 등 별도 잠금 속성 사용 시): 운영 스키마에 맞는 필터
    예시를 함께 기재

### 4.4 검색 공통

- 모든 search 는 **RFC 2696 simple paged results, 페이지 크기 1000** 을
  사용한다 (AD 기본 sizelimit 1000 대응). 라이브러리의 paged search
  제너레이터를 써도 되고 쿠키 루프를 직접 돌려도 된다.
- 필요한 속성만 요청한다(그룹: 이름 속성 + 멤버 속성, 사용자: 매칭 속성).
- referral 은 따라가지 않는다(무시). 시간 제한: 연결 5s / 작업 30s 수준의
  타임아웃을 두고, 일시 오류는 재시도(지수 백오프, 최대 3회)한다.
- **중첩 그룹은 v1 범위 외**: member 가 그룹 엔트리인 경우 경고 후 무시하고,
  memberOf 체이닝·AD matching-rule-in-chain 은 사용하지 않는다.

### 4.5 구현 스택

- **Python 3.12 + [ldap3](https://ldap3.readthedocs.io/)** (순수 파이썬 —
  `python-ldap` 과 달리 C 라이브러리(OpenLDAP client libs) 빌드 의존이 없어
  slim 이미지 그대로 사용 가능). Grafana 호출은 기존과 동일하게 `requests`.

## 5. Grafana 반영

**기존 도구의 검증된 사양을 그대로 따른다** (코드 재사용 가능 부분).

- 인증: 서비스 계정 토큰 `Authorization: Bearer <GRAFANA_TOKEN>`.
  **org Admin 롤이면 충분**하다 — 서버 Admin(Grafana Admin) 불필요.
- 사용자 조회는 **`GET /api/org/users/search`** (perpage/page 페이지네이션)
  를 사용하고, 404 가 나는 구버전에서는 `GET /api/org/users` 로 fallback 한다.
  **`/api/users/lookup` 사용 금지** — 서버 Admin 권한이 필요해 Grafana
  10+/12 에서 org Admin 서비스 계정에 403 을 반환한다 (운영에서 실제 확인된
  이슈).
- 실행당 org 사용자 목록을 **한 번만** 조회해 매칭 키(소문자 정규화된
  email 또는 login) → userId 인덱스를 만든다 (`build_user_index`).
- 팀별 처리 (`sync_team` 과 동일):
  1. `GET /api/teams/search?name=` 으로 팀 검색 (이름 완전 일치, 대소문자 무관)
  2. 없으면 `POST /api/teams` 로 생성 — 단 **원하는 멤버가 0명이면 빈 팀을
     만들지 않는다** (`empty_team_not_created`)
  3. `GET /api/teams/{id}/members` 현재 멤버와 LDAP 산출 멤버를 diff 해
     `POST/DELETE /api/teams/{id}/members[/{userId}]` 로 추가/제거
- 매칭 키는 `MATCH_KEY`: `email`(기본, Grafana email ↔ LDAP `mail`) 또는
  `username`(Grafana login ↔ uid/sAMAccountName). Grafana 의
  `login_attribute_path`/LDAP auth 매핑과 일치해야 함을 README 에 명시.
- org 목록에 없는 사용자(아직 미로그인) = **pending** 집계
  (`member_pending_first_login`), 실패가 아니며 다음 주기에 자동 재시도.
- 401/403 → 인증 오류(종료 코드 2). 5xx/타임아웃 → 지수 백오프 재시도
  (최대 3회), 이후 해당 팀만 실패 처리(종료 코드 1)하고 다음 팀 계속.
- prefix 밖의 팀은 조회조차 하지 않으므로 절대 변경되지 않는다.

## 6. 안전장치

전부 기존 도구와 동일하다.

- **`DRY_RUN` 기본값 true** — 실제 변경 없이 예상 diff
  (`would_create_team`/`would_add_member`/`would_remove_member`)만 출력.
  배포 매니페스트에서 명시적으로 `false` 를 줘야 실제 반영된다.
- **제거 가드**: 한 팀에서 제거 대상이 현재 멤버 수의
  `MAX_REMOVAL_RATIO`(기본 0.5)를 **초과**하면 그 팀의 제거를 스킵하고
  (추가는 수행) `removal_guard_triggered` 에러 로그 + 종료 코드 1.
  `MATCH_KEY`/`LDAP_USER_MATCH_ATTR` 오설정으로 전원이 제거되는 사고 방지.
- **빈 팀 미생성**: 원하는 멤버 0명 + 기존 팀 없음 → 생성하지 않음.
- **대상 그룹 0개 → 무변경 성공 종료** (§3.3).
- **시크릿 로그 금지**: `LDAP_BIND_PASSWORD`, `GRAFANA_TOKEN` 은 어떤 로그
  레벨에서도 출력하지 않는다 (URL·DN 은 출력 가능).
- 모든 쓰기 작업은 실행 전에 logfmt 이벤트로 남긴다.

### 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 정상 (dry-run 포함, 대상 0개 포함) |
| 1 | 부분 실패 — 제거 가드 발동, 일부 팀 처리 실패 |
| 2 | 설정 오류, LDAP bind 실패, Grafana 401/403 |

### 로그 형식

기존과 동일한 한 줄 logfmt 스타일:

```
time=2026-08-31T09:00:01+0000 level=INFO event=add_member team=grafana-abc_editor target=alice@example.com
time=2026-08-31T09:00:02+0000 level=INFO event=sync_complete teams=5 failed_teams=0 added=1 removed=0 pending_first_login=1 dry_run=False exit_code=0
```

## 7. 설정 (환경변수)

### LDAP (신규)

| 변수 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `LDAP_URL` | Y | | `ldap://host:389` 또는 `ldaps://host:636` |
| `LDAP_STARTTLS` | N | `false` | `true` 면 ldap:// 연결을 StartTLS 로 승격 |
| `LDAP_BIND_DN` | Y | | bind 계정 DN (예: `cn=sync,ou=svc,dc=example,dc=com`) |
| `LDAP_BIND_PASSWORD` | Y | | bind 비밀번호 — K8s Secret 으로 주입, 빈 값 불가 |
| `LDAP_GROUP_BASE_DN` | Y | | 그룹 검색 base DN (예: `ou=groups,dc=example,dc=com`) |
| `LDAP_GROUP_FILTER` | N | `(objectClass=groupOfNames)` | 그룹 검색 필터. AD: `(objectClass=group)`, posix: `(objectClass=posixGroup)` |
| `LDAP_GROUP_SCOPE` | N | `subtree` | `subtree` 또는 `onelevel` |
| `LDAP_GROUP_NAME_ATTR` | N | `cn` | 팀 이름으로 쓸 그룹 속성 |
| `LDAP_MEMBER_MODE` | N | `member` | `member`(DN 목록) 또는 `memberUid`(uid 문자열) |
| `LDAP_MEMBER_ATTR` | N | 모드별 (`member`/`memberUid`) | 그룹의 멤버 속성. AD 도 `member` |
| `LDAP_USER_BASE_DN` | 조건부 | | 사용자 검색 base DN — `memberUid` 모드에서 필수 |
| `LDAP_USER_UID_ATTR` | N | `uid` | `memberUid` 값과 매칭할 사용자 속성. AD: `sAMAccountName` |
| `LDAP_USER_MATCH_ATTR` | N | `mail` | 매칭 키로 추출할 사용자 속성. `MATCH_KEY=username` 이면 `uid`/`sAMAccountName` 등 |
| `LDAP_USER_FILTER` | N | `(objectClass=*)` | 사용자 유효성 AND 필터 — 비활성 계정 제외 (§4.3 AD 예시) |

### 공통 (기존 도구와 동일)

| 변수 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `GRAFANA_URL` | Y | | Grafana base URL |
| `GRAFANA_TOKEN` | Y | | 서비스 계정 토큰 (org Admin) — K8s Secret 으로 주입 |
| `GROUP_PREFIX` | N | `grafana-` | 관리 대상 그룹 cn prefix. 빈 값 불가 |
| `ROLE_SUFFIXES` | N | `adm,admin,editor,viewer,member,mbr` | 역할 팀으로 인식할 suffix (콤마 구분) |
| `MATCH_KEY` | N | `email` | `email` 또는 `username` |
| `DRY_RUN` | N | `true` | 변경 없이 로그만 출력 |
| `MAX_REMOVAL_RATIO` | N | `0.5` | 팀별 제거 가드 임계값 (0~1) |
| `SSL_VERIFY` | N | `true` | `false` 면 LDAP/Grafana TLS 검증 생략 (경고 로그) |
| `SSL_CA_BUNDLE` | N | | 사설 CA 번들(PEM) 경로 — LDAP·Grafana 공통 적용 |
| `LOG_LEVEL` | N | `INFO` | |

설정 검증 규칙 (위반 시 종료 코드 2): 필수 변수 누락, `MATCH_KEY` ∉
{email, username}, `MAX_REMOVAL_RATIO` ∉ [0,1], `GROUP_PREFIX` 빈 값,
`LDAP_MEMBER_MODE` ∉ {member, memberUid}, memberUid 모드에서
`LDAP_USER_BASE_DN` 누락, `SSL_CA_BUNDLE` 파일 없음, boolean 파싱 실패.

## 8. 산출물

| 파일 | 요구사항 |
|---|---|
| `sync.py` | 단일 파일 스크립트. Grafana 부는 기존 코드 재사용, LDAP 부는 `LdapClient` 로 신규 작성 |
| `requirements.txt` | `requests`, `ldap3` (버전 고정) |
| `Dockerfile` | `python:3.12-slim`, non-root(uid 10001), `PYTHONUNBUFFERED=1` — 기존과 동일 구조 |
| `k8s/cronjob.yaml` | `schedule: "*/10 * * * *"`, `concurrencyPolicy: Forbid`, `backoffLimit: 2`, `activeDeadlineSeconds: 540`, restartPolicy Never, 보안 컨텍스트(runAsNonRoot, readOnlyRootFilesystem, drop ALL), 리소스 requests/limits, 시크릿은 `secretKeyRef` |
| `k8s/secret.example.yaml` | `LDAP_BIND_PASSWORD`, `GRAFANA_TOKEN` 키 이름 예시 (평문 금지 주석) |
| `README.md` | 기존 README 와 동일 구성: 그룹 모델, 예제(그룹 → 팀 표), 설정 표, LDAP bind 계정 준비, Grafana 서비스 계정 준비, SSL 가이드, dry-run 우선 배포 절차, 로그 형식 |
| `tests/test_sync.py` | 아래 테스트 요구사항 |

## 9. 테스트

- Grafana API 모킹은 기존과 동일하게 `responses` 사용.
- LDAP 모킹은 둘 중 하나 (구현 시 선택, 스펙상 둘 다 허용):
  1. **ldap3 MOCK_SYNC 전략** — 라이브러리 내장 mock server 에 그룹/사용자
     엔트리를 심어 실제 검색 경로까지 검증 (권장)
  2. **소스 추상화 + fake** — `LdapClient` 를 인터페이스(그룹 목록,
     그룹 멤버 키 목록)로 분리하고 테스트에서 fake 구현 주입
- 커버할 시나리오:
  - member 모드: DN → mail 해석, 사용자 캐시(중복 DN 1회 조회), 매칭 속성
    없는 멤버 스킵
  - memberUid 모드: uid → 사용자 검색 → 매칭 키, `LDAP_USER_MATCH_ATTR=uid`
    최적화 경로
  - `LDAP_USER_FILTER` 로 비활성 사용자 제외 (제거 diff 에 반영되는 것 포함)
  - prefix 선별, 미인식/빈 이름 그룹 스킵, 대상 0개 무변경 종료
  - paged search 다중 페이지 병합
  - 팀 생성/추가/제거 diff, 빈 팀 미생성, pending 집계
  - 제거 가드 발동 (ratio 초과 시 제거 스킵 + 종료 코드 1)
  - dry-run: 어떤 쓰기 API 도 호출되지 않음
  - 설정 검증 오류들 → 종료 코드 2
  - Grafana `/api/org/users/search` 404 → `/api/org/users` fallback
  - 5xx 재시도 후 성공 / 팀 단위 부분 실패 시 나머지 팀 계속 진행

## 10. 인수 조건 체크리스트

- [ ] **멱등**: 동일 상태에서 연속 2회 실행 시 2회차가 `added=0 removed=0 exit_code=0`
- [ ] **prefix 밖 불변**: `GROUP_PREFIX` 와 무관한 기존 Grafana 팀·멤버십은 조회조차 되지 않음
- [ ] **dry-run 무변경**: `DRY_RUN=true` 로 실행 시 Grafana 에 어떤 쓰기 요청도 발생하지 않음 (팀 생성 포함)
- [ ] **시크릿 미출력**: 모든 로그 레벨에서 `LDAP_BIND_PASSWORD`·`GRAFANA_TOKEN` 이 로그·에러 메시지에 나타나지 않음
- [ ] 익명 bind 불가: bind 정보 누락/빈 값 → 종료 코드 2
- [ ] `ldaps://` 및 StartTLS 연결에서 `SSL_CA_BUNDLE` 사설 CA 검증 동작, `SSL_VERIFY=false` 시 경고 로그
- [ ] member/memberUid 두 모드 모두에서 그룹 → 팀 멤버십 동기화 성공
- [ ] 미로그인 사용자 pending 집계 후 성공 종료, 최초 로그인 후 다음 주기에 자동 편입
- [ ] 제거 가드: 오설정 시나리오에서 대량 제거가 차단되고 종료 코드 1
- [ ] 대상 그룹 0개 → 경고 + 무변경 + 종료 코드 0
- [ ] Grafana 12 에서 org Admin 서비스 계정 토큰만으로 전체 플로우 동작 (`/api/users/lookup` 미사용)
- [ ] CronJob 매니페스트: Forbid, backoffLimit 2, 시크릿 `secretKeyRef` 주입 확인
- [ ] 1000명 초과 그룹/디렉터리에서 paged search 로 전체 멤버 수집
