# grafana-ldap-team-sync

LDAP 그룹 멤버십을 Grafana OSS 팀 멤버십에 주기적으로 반영하는 멱등 동기화 잡입니다.
Grafana Enterprise 의 Team Sync 를 대체하며, Kubernetes CronJob 으로 10분마다 실행합니다.
[grafana-keycloak-group-syncer](https://github.com/taengkim/grafana-keyclock-group-synker)
와 동일한 운영 모델(안전장치·종료 코드·로그 형식)을 따르고, 소스만 LDAP 입니다.

## 그룹 모델 — 플랫 컨벤션

`LDAP_GROUP_BASE_DN` 아래에서 `LDAP_GROUP_FILTER` 로 그룹을 검색하고,
이름 속성(`LDAP_GROUP_NAME_ATTR`, 기본 `cn`)이 `GROUP_PREFIX` 로 시작하는
그룹만 관리 대상입니다. **그룹 cn 이 그대로 Grafana 팀 이름**이 됩니다.

`<서비스명>_<suffix>`(또는 `-<suffix>`, suffix ∈ `ROLE_SUFFIXES`) 형태의
이름은 **역할 팀**으로, 각각 **독립 Grafana 팀**으로 동기화됩니다.

```
ou=groups,dc=example,dc=com
├── cn=grafana-abc_adm       → Grafana 팀 "grafana-abc_adm"
├── cn=grafana-abc_editor    → Grafana 팀 "grafana-abc_editor"
├── cn=grafana-abc_viewer    → Grafana 팀 "grafana-abc_viewer"
├── cn=grafana-xyz           → Grafana 팀 "grafana-xyz" (서비스 팀)
└── cn=hr-payroll            ← prefix 미매칭: 조회/변경 대상 아님
```

- **Admin/Editor/Viewer 권한 부여는 폴더 권한 단계에서** 이뤄집니다.
  이 도구는 역할별 팀만 만들어 주고, 폴더 ↔ 팀 권한(Admin/Edit/View)은
  별도 작업(Terraform 또는 수동)에서 `grafana-abc_adm`→Admin,
  `grafana-abc_editor`→Edit, `grafana-abc_viewer`→View 로 부여합니다.
- 같은 cn 의 그룹이 여러 개면(subtree 검색) 멤버가 병합되어 한 팀이 됩니다.
- org 롤(Viewer/Editor/Admin)은 기존대로 인증 연동(`role_attribute_path`
  또는 LDAP auth 매핑)이 담당합니다. 이 도구는 팀 멤버십만 책임집니다.
- **중첩 그룹은 해석하지 않습니다**: 그룹의 member 가 다른 그룹이면 경고
  로그(`nested_group_member_skipped`)를 남기고 무시합니다.

## 동작 방식

1. LDAP 에 simple bind(`LDAP_BIND_DN`/`LDAP_BIND_PASSWORD`) 후 그룹을
   검색합니다. 익명 bind 는 지원하지 않습니다. 모든 검색은 RFC 2696
   paged search(1000건)를 사용합니다.
2. 그룹 멤버를 `LDAP_MEMBER_MODE` 에 따라 해석합니다.
   - **`member`** (기본, `groupOfNames`/AD): 멤버 값이 사용자 **DN**.
     각 DN 의 엔트리에서 `LDAP_USER_MATCH_ATTR`(기본 `mail`)를 추출하며,
     한 실행 내에서는 사용자 캐시로 중복 조회를 피합니다.
   - **`memberUid`** (`posixGroup`): 멤버 값이 **uid 문자열**.
     `LDAP_USER_BASE_DN` 아래에서 `LDAP_USER_UID_ATTR`(기본 `uid`)로
     사용자를 찾아 매칭 속성을 추출합니다. `LDAP_USER_MATCH_ATTR` 가 uid
     속성과 같고 `LDAP_USER_FILTER` 가 기본값이면 추가 조회 없이 uid 를
     그대로 사용합니다.
   - `LDAP_USER_FILTER` 에 걸리지 않는 사용자(비활성 계정 등)는 제외됩니다.
3. 추출한 매칭 키를 `MATCH_KEY`(`email` 기본 또는 `username`)로 Grafana
   사용자와 매칭합니다. 비교는 소문자 정규화 후 수행합니다.
4. Grafana 에 팀이 없으면 생성하고, 현재 팀 멤버와 비교해 추가/제거합니다.
   - 사용자 매칭은 org 사용자 목록(`/api/org/users/search`, org Admin 으로
     충분) 기준입니다. 아직 Grafana 에 로그인한 적 없어 목록에 없는
     사용자는 건너뛰고 pending 으로 집계하며, 다음 주기에 자동 재시도됩니다.
   - prefix 밖의 팀은 조회조차 하지 않으므로 절대 변경되지 않습니다.

### 범위 밖 (별도 작업)

- 폴더 ↔ 팀 권한 부여, org 롤 매핑, grafana.ini/LDAP auth 설정 변경
- **팀 삭제**: LDAP 에서 그룹이 사라져도 Grafana 팀은 남습니다. 이 잡은 남은
  팀을 조회하지 않으므로(관리 대상 팀 이름으로만 조회) 고아 팀 정리는 수동으로 합니다.
- **사용자 생성**: Grafana 사용자를 만들지 않습니다.
- 중첩 그룹 해석(memberOf 체이닝, AD matching-rule-in-chain)

## 안전장치

- `DRY_RUN` 기본값 **true** — 실제 변경 없이 예상 diff(`would_create_team`,
  `would_add_member`, `would_remove_member`)만 로그로 출력합니다.
  배포 매니페스트에서 명시적으로 `false` 를 줘야 실제 반영됩니다.
- 한 팀에서 제거 대상이 현재 멤버의 `MAX_REMOVAL_RATIO`(기본 0.5)를 **초과**하면
  해당 팀의 제거를 스킵하고(추가는 수행) 에러 로그를 남기며 종료 코드 1 로 끝납니다.
  `MATCH_KEY`/`LDAP_USER_MATCH_ATTR` 오설정으로 전원이 제거되는 사고를 막기 위한 장치입니다.
- 관리 대상 그룹이 0개면(`no_managed_groups`) 경고만 남기고 아무것도 변경하지
  않습니다. base DN/필터 오설정으로 전체 팀이 비워지는 사고 방지입니다.
- 모든 쓰기 작업은 실행 전에 로그로 남깁니다. bind 비밀번호·토큰은 로그에
  출력하지 않습니다.

## 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 정상 (dry-run, 대상 0개 포함) |
| 1 | 부분 실패 (제거 가드 발동, 일부 팀 처리 실패) |
| 2 | 설정 오류, LDAP bind/TLS 실패, Grafana 401/403 |

## 설정 (환경변수)

### LDAP

| 변수 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `LDAP_URL` | Y | | `ldap://host:389` 또는 `ldaps://host:636` |
| `LDAP_STARTTLS` | N | `false` | `true` 면 ldap:// 연결을 StartTLS 로 승격 (ldaps:// 와 동시 사용 불가) |
| `LDAP_BIND_DN` | Y | | bind 계정 DN. 익명 bind 불가 |
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
| `LDAP_USER_FILTER` | N | `(objectClass=*)` | 사용자 유효성 AND 필터 — 비활성 계정 제외용 (아래 예시) |

**`LDAP_USER_FILTER` 예시 — 비활성 계정 제외**

- Active Directory (ACCOUNTDISABLE 비트 제외):

  ```
  (&(objectClass=user)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))
  ```

- OpenLDAP: 운영 스키마에 맞게 지정합니다. 예를 들어 별도 속성으로 상태를
  관리한다면 `(&(objectClass=inetOrgPerson)(employeeType=active))` 처럼
  씁니다 (ppolicy 의 `pwdAccountLockedTime` 은 operational 속성이라 필터
  가능 여부가 서버 설정에 따라 다릅니다).

필터에 걸리지 않는 멤버는 팀에서 **제거 대상**이 됩니다.

### 공통

| 변수 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `GRAFANA_URL` | Y | | Grafana base URL |
| `GRAFANA_TOKEN` | Y | | 서비스 계정 토큰 (org Admin) — K8s Secret 으로 주입 |
| `GROUP_PREFIX` | N | `grafana-` | 관리 대상 그룹 이름 prefix. 빈 값 불가 |
| `MATCH_KEY` | N | `email` | `email` 또는 `username`. Grafana 로그인 매핑과 일치해야 함 |
| `ROLE_SUFFIXES` | N | `adm,admin,editor,viewer,member,mbr` | 역할 팀으로 인식할 suffix 목록 (콤마 구분) |
| `SSL_VERIFY` | N | `true` | `false` 면 LDAP/Grafana TLS 인증서 검증을 건너뜀 |
| `SSL_CA_BUNDLE` | N | | 사설 CA 번들(PEM) 경로. LDAP·Grafana 에 공통 적용 |
| `DRY_RUN` | N | `true` | 변경 없이 로그만 출력 |
| `MAX_REMOVAL_RATIO` | N | `0.5` | 팀별 제거 가드 임계값 (0~1) |
| `LOG_LEVEL` | N | `INFO` | |

## 사전 준비

### 1. LDAP bind 계정

그룹·사용자 subtree 에 **읽기 권한**만 있는 전용 계정을 만들어 DN 과
비밀번호를 K8s Secret 으로 보관합니다. AD 는 일반 도메인 사용자로도 대개
충분하고, OpenLDAP 은 해당 subtree 에 read ACL 이 있는 계정이면 됩니다.

### 2. Grafana 서비스 계정 발급

1. Grafana → **Administration → Service accounts → Add service account**
   - Role: **Admin** (org admin — 팀 생성/멤버 관리와 org 사용자 목록 조회에 필요)
   - 서버 Admin(Grafana Admin)은 필요 없습니다. 사용자 조회는 org Admin 으로
     접근 가능한 `/api/org/users/search` 를 사용하므로(`/api/users/lookup`
     미사용), 서비스 계정에 서버 Admin 을 줄 수 없는 Grafana 10+/12 에서도
     동작합니다.
2. 생성한 서비스 계정에서 **Add service account token** 으로 토큰을 발급하고
   K8s Secret 으로 보관합니다.
3. `MATCH_KEY`/`LDAP_USER_MATCH_ATTR` 는 Grafana 로그인 계정의
   email/login 이 실제로 어떤 LDAP 속성에서 오는지와 일치해야 합니다.
   login 이 uid/sAMAccountName 이면 `MATCH_KEY=username` +
   `LDAP_USER_MATCH_ATTR=uid`(또는 `sAMAccountName`)를 사용하세요.

### 3. 시크릿 생성

```sh
kubectl create secret generic grafana-team-sync \
  --from-literal=LDAP_BIND_PASSWORD='...' \
  --from-literal=GRAFANA_TOKEN='...'
```

매니페스트에 평문 시크릿을 넣지 마세요. `k8s/secret.example.yaml` 은 키 이름
참고용 예시입니다.

### 사설 인증서 환경 (SSL)

내부망에서 자체 서명/사설 CA 인증서를 쓰는 경우 두 가지 방법이 있습니다.

1. **권장 — 사설 CA 번들 지정**: CA 인증서(PEM)를 ConfigMap 으로 마운트하고
   `SSL_CA_BUNDLE` 로 경로를 지정하면 검증을 유지한 채 동작합니다.
   ldaps/StartTLS 와 Grafana HTTPS 에 공통 적용됩니다.

   ```yaml
   env:
     - name: SSL_CA_BUNDLE
       value: /etc/ssl/private/ca.crt
   volumeMounts:
     - name: private-ca
       mountPath: /etc/ssl/private
       readOnly: true
   volumes:
     - name: private-ca
       configMap:
         name: private-ca
   ```

2. **임시 우회 — 검증 비활성화**: `SSL_VERIFY=false` 로 인증서 검증을
   건너뜁니다. 시작 시 `ssl_verification_disabled` 경고가 로그에 남습니다.
   중간자 공격에 노출되므로 테스트/임시 용도로만 쓰고, 운영에서는 1번을
   사용하세요.

## 사용법

### 로컬 실행

의존성을 설치하고 환경변수를 지정해 바로 실행할 수 있습니다.
`DRY_RUN` 기본값이 `true` 라서 그냥 실행하면 변경 없이 예상 diff 만 출력됩니다.

```sh
pip install -r requirements.txt

export LDAP_URL=ldaps://ldap.example.com:636
export LDAP_BIND_DN='cn=grafana-sync,ou=svc,dc=example,dc=com'
export LDAP_BIND_PASSWORD=...
export LDAP_GROUP_BASE_DN='ou=groups,dc=example,dc=com'
export GRAFANA_URL=https://grafana.example.com
export GRAFANA_TOKEN=...
export GROUP_PREFIX=grafana-
export ROLE_SUFFIXES="adm,editor,viewer"

python sync.py            # dry-run: 로그만 출력
DRY_RUN=false python sync.py   # 실제 반영
echo $?                   # 0 정상 / 1 부분 실패 / 2 설정·인증 오류
```

posixGroup(`memberUid`) 디렉터리는 다음을 추가합니다.

```sh
export LDAP_GROUP_FILTER='(objectClass=posixGroup)'
export LDAP_MEMBER_MODE=memberUid
export LDAP_USER_BASE_DN='ou=people,dc=example,dc=com'
```

사설 인증서 환경이면 `SSL_CA_BUNDLE=/path/ca.crt`(권장) 또는
`SSL_VERIFY=false`(임시)를 추가합니다.

### Docker 실행

```sh
docker build -t grafana-team-sync .

cat > sync.env <<'EOF'
LDAP_URL=ldaps://ldap.example.com:636
LDAP_BIND_DN=cn=grafana-sync,ou=svc,dc=example,dc=com
LDAP_BIND_PASSWORD=...
LDAP_GROUP_BASE_DN=ou=groups,dc=example,dc=com
GRAFANA_URL=https://grafana.example.com
GRAFANA_TOKEN=...
GROUP_PREFIX=grafana-
ROLE_SUFFIXES=adm,editor,viewer
DRY_RUN=true
EOF

docker run --rm --env-file sync.env grafana-team-sync
```

### Kubernetes (CronJob)

시크릿 생성 후 CronJob 을 배포하면 10분마다 자동 실행됩니다.
상세 절차는 아래 [최초 배포 절차](#최초-배포-절차-dry-run-먼저)를 따르세요.

```sh
kubectl create secret generic grafana-team-sync \
  --from-literal=LDAP_BIND_PASSWORD='...' \
  --from-literal=GRAFANA_TOKEN='...'
kubectl apply -f k8s/cronjob.yaml

# 수동으로 1회 실행
kubectl create job --from=cronjob/grafana-team-sync team-sync-manual
kubectl logs -f job/team-sync-manual

# 일시 중지 / 재개
kubectl patch cronjob grafana-team-sync -p '{"spec":{"suspend":true}}'
kubectl patch cronjob grafana-team-sync -p '{"spec":{"suspend":false}}'
```

## 최초 배포 절차 (dry-run 먼저)

1. 이미지를 빌드해 레지스트리에 푸시합니다.

   ```sh
   docker build -t registry.example.com/grafana-team-sync:<tag> .
   docker push registry.example.com/grafana-team-sync:<tag>
   ```

2. `k8s/cronjob.yaml` 의 이미지/URL/DN 값을 채우고 **`DRY_RUN=true`** 로 배포합니다.
3. Job 로그에서 예상 diff 를 검토합니다.

   ```sh
   kubectl create job --from=cronjob/grafana-team-sync team-sync-dryrun
   kubectl logs job/team-sync-dryrun
   ```

   확인할 것:
   - `would_create_team` / `would_add_member` / `would_remove_member` 가 기대와 일치하는가
   - `member_pending_first_login` (아직 미로그인 사용자) 수가 타당한가
   - `member_missing_match_key` / `member_user_not_found` 가 많다면
     `LDAP_USER_MATCH_ATTR` 설정을 의심할 것
   - `removal_guard_triggered` 가 떴다면 `MATCH_KEY` 설정을 의심할 것
4. diff 가 정상이면 `DRY_RUN=false` 로 변경해 재배포합니다.
5. 두 번 연속 실행 후 두 번째 실행에서 `added=0 removed=0` 인지(멱등) 확인합니다.

## 로그 형식

한 줄 단위 logfmt 스타일 구조화 로그입니다.

```
time=2026-08-31T09:00:01+0000 level=INFO event=add_member team=grafana-abc_editor target=alice@example.com
time=2026-08-31T09:00:01+0000 level=INFO event=remove_member team=grafana-abc_viewer target=bob@example.com
time=2026-08-31T09:00:02+0000 level=INFO event=sync_complete teams=6 failed_teams=0 added=1 removed=1 pending_first_login=0 dry_run=False exit_code=0
```

## 개발

```sh
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

테스트는 LDAP 을 ldap3 의 MOCK_SYNC 전략(인메모리 DIT)으로, Grafana API 를
`responses` 로 모킹하며, member/memberUid 두 모드, 사용자 캐시, 사용자 필터,
중첩 그룹 스킵, prefix 선별, 팀 생성·멤버 추가/제거, 빈 팀 미생성, pending
집계, paged search, 제거 가드, dry-run 무변경, 설정 검증, bind 실패 시 시크릿
미출력 등을 커버합니다.
