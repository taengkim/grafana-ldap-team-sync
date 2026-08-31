#!/usr/bin/env python3
"""Sync LDAP group memberships to Grafana teams.

Flat group + naming convention model: groups under LDAP_GROUP_BASE_DN
matching LDAP_GROUP_FILTER whose name attribute (LDAP_GROUP_NAME_ATTR,
default cn) starts with GROUP_PREFIX are managed; the group name becomes
the Grafana team name as-is. Names shaped "<service>_<suffix>" with a
suffix from ROLE_SUFFIXES (e.g. grafana-abc_adm) are role teams — each
an INDEPENDENT Grafana team; folder permissions (Admin/Edit/View) are
granted to those teams outside this tool (Terraform or manually). Only
team membership is managed here; org roles stay with Grafana's auth
mapping (role_attribute_path / LDAP auth).

Membership modes (LDAP_MEMBER_MODE):
    member    - group lists user DNs (groupOfNames, AD group); each DN is
                read and LDAP_USER_MATCH_ATTR (default mail) becomes the
                match key. Resolved DNs are cached per run.
    memberUid - group lists uid strings (posixGroup); users are searched
                under LDAP_USER_BASE_DN by LDAP_USER_UID_ATTR.

Nested groups are out of scope: a member that is itself a group entry is
skipped with a warning, and memberOf chaining is not used.

Exit codes:
    0 - success
    1 - partial failure (removal guard triggered or some teams failed)
    2 - configuration error or authentication failure
"""
from __future__ import annotations

import logging
import os
import ssl
import sys
import time
from dataclasses import dataclass

import requests
from ldap3 import BASE, LEVEL, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

logger = logging.getLogger("grafana-team-sync")

CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 30
REQUEST_TIMEOUT = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
PAGE_SIZE = 100
LDAP_PAGE_SIZE = 1000

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CONFIG = 2

# RFC 2696 simple paged results control
PAGED_RESULTS_OID = "1.2.840.113556.1.4.319"
RESULT_SUCCESS = 0
RESULT_NO_SUCH_OBJECT = 32

DEFAULT_GROUP_FILTER = "(objectClass=groupOfNames)"
DEFAULT_USER_FILTER = "(objectClass=*)"

# objectClass values that mark a member entry as a group (nested groups
# are not resolved in v1 - such members are skipped with a warning).
GROUP_OBJECT_CLASSES = frozenset({
    "group", "groupofnames", "groupofuniquenames", "groupofurls", "posixgroup",
})

# Role-team name suffixes (e.g. "grafana-abc_adm"). Informational in the
# flat model: an unrecognized suffix is NOT a skip reason, the group is
# synced as a service team. Overridable via ROLE_SUFFIXES.
DEFAULT_ROLE_SUFFIXES = ("adm", "admin", "editor", "viewer", "member", "mbr")


def parse_role_suffixes(raw: str) -> frozenset[str]:
    """Parse ROLE_SUFFIXES: comma-separated suffixes, e.g. "adm,editor,viewer"."""
    suffixes = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not suffixes:
        raise ConfigError("ROLE_SUFFIXES must contain at least one suffix")
    return frozenset(suffixes)


def is_role_name(name: str, role_suffixes: frozenset[str]) -> bool:
    """Whether a team name follows the "<service>_<suffix>" role convention.

    Matches "<service>_<suffix>" or "<service>-<suffix>" with a suffix
    from role_suffixes, case-insensitive (e.g. "grafana-abc_adm").
    """
    lowered = name.strip().lower()
    for separator in ("_", "-"):
        head, sep, tail = lowered.rpartition(separator)
        if sep and head and tail in role_suffixes:
            return True
    return False


RETRYABLE_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)


class ConfigError(Exception):
    """Invalid or missing configuration."""


class AuthError(Exception):
    """Authentication or authorization failure against LDAP or Grafana."""


class ApiError(Exception):
    """Unexpected LDAP or API response."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _fmt(value: object) -> str:
    text = str(value)
    if text == "" or any(c in text for c in (" ", '"', "=")):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def log_event(level: int, event: str, **fields: object) -> None:
    """Emit one structured logfmt-style line: event=... key=value ..."""
    parts = [f"event={_fmt(event)}"]
    parts.extend(f"{key}={_fmt(value)}" for key, value in fields.items())
    logger.log(level, " ".join(parts))


def setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("time=%(asctime)s level=%(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def parse_bool(raw: str, name: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in ("true", "1", "yes", "on"):
        return True
    if normalized in ("false", "0", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def _validate_filter(raw: str, name: str) -> str:
    filt = raw.strip()
    if not (filt.startswith("(") and filt.endswith(")")):
        raise ConfigError(f"{name} must be a parenthesized LDAP filter, got {raw!r}")
    return filt


@dataclass
class Config:
    ldap_url: str
    ldap_bind_dn: str
    ldap_bind_password: str
    ldap_group_base_dn: str
    grafana_url: str
    grafana_token: str
    ldap_starttls: bool = False
    ldap_group_filter: str = DEFAULT_GROUP_FILTER
    ldap_group_scope: str = "subtree"
    ldap_group_name_attr: str = "cn"
    ldap_member_mode: str = "member"
    ldap_member_attr: str = "member"
    ldap_user_base_dn: str = ""
    ldap_user_uid_attr: str = "uid"
    ldap_user_match_attr: str = "mail"
    ldap_user_filter: str = DEFAULT_USER_FILTER
    group_prefix: str = "grafana-"
    match_key: str = "email"
    dry_run: bool = True
    max_removal_ratio: float = 0.5
    log_level: str = "INFO"
    role_suffixes: frozenset = frozenset(DEFAULT_ROLE_SUFFIXES)
    ssl_verify: bool = True
    ssl_ca_bundle: str = ""

    @property
    def effective_ssl_verify(self):
        """Value for requests' verify=: False, a CA bundle path, or True."""
        if not self.ssl_verify:
            return False
        return self.ssl_ca_bundle or True

    REQUIRED = (
        "LDAP_URL",
        "LDAP_BIND_DN",
        "LDAP_BIND_PASSWORD",
        "LDAP_GROUP_BASE_DN",
        "GRAFANA_URL",
        "GRAFANA_TOKEN",
    )

    @classmethod
    def from_env(cls, env: dict) -> "Config":
        # Empty values count as missing: an empty LDAP_BIND_PASSWORD would
        # request an anonymous (unauthenticated) bind, which is forbidden.
        missing = [name for name in cls.REQUIRED if not env.get(name)]
        if missing:
            raise ConfigError("missing required environment variables: " + ", ".join(missing))

        ldap_url = env["LDAP_URL"].strip()
        if not ldap_url.startswith(("ldap://", "ldaps://")):
            raise ConfigError(f"LDAP_URL must start with ldap:// or ldaps://, got {ldap_url!r}")

        ldap_starttls = parse_bool(env.get("LDAP_STARTTLS", "false"), "LDAP_STARTTLS")
        if ldap_starttls and ldap_url.startswith("ldaps://"):
            raise ConfigError("LDAP_STARTTLS requires an ldap:// URL (ldaps:// is already TLS)")

        ldap_group_scope = env.get("LDAP_GROUP_SCOPE", "subtree").strip().lower()
        if ldap_group_scope not in ("subtree", "onelevel"):
            raise ConfigError(f"LDAP_GROUP_SCOPE must be 'subtree' or 'onelevel', got {ldap_group_scope!r}")

        raw_mode = env.get("LDAP_MEMBER_MODE", "member").strip()
        if raw_mode.lower() not in ("member", "memberuid"):
            raise ConfigError(f"LDAP_MEMBER_MODE must be 'member' or 'memberUid', got {raw_mode!r}")
        ldap_member_mode = "member" if raw_mode.lower() == "member" else "memberUid"

        ldap_member_attr = env.get("LDAP_MEMBER_ATTR", "").strip() or ("member" if ldap_member_mode == "member" else "memberUid")

        ldap_user_base_dn = env.get("LDAP_USER_BASE_DN", "").strip()
        if ldap_member_mode == "memberUid" and not ldap_user_base_dn:
            raise ConfigError("LDAP_USER_BASE_DN is required when LDAP_MEMBER_MODE=memberUid")

        match_key = env.get("MATCH_KEY", "email").strip().lower()
        if match_key not in ("email", "username"):
            raise ConfigError(f"MATCH_KEY must be 'email' or 'username', got {match_key!r}")

        group_prefix = env.get("GROUP_PREFIX", "grafana-")
        if not group_prefix:
            raise ConfigError("GROUP_PREFIX must not be empty")

        raw_ratio = env.get("MAX_REMOVAL_RATIO", "0.5")
        try:
            max_removal_ratio = float(raw_ratio)
        except ValueError as exc:
            raise ConfigError(f"MAX_REMOVAL_RATIO must be a number, got {raw_ratio!r}") from exc
        if not 0.0 <= max_removal_ratio <= 1.0:
            raise ConfigError(f"MAX_REMOVAL_RATIO must be between 0 and 1, got {raw_ratio!r}")

        raw_role_suffixes = env.get("ROLE_SUFFIXES", "")
        role_suffixes = parse_role_suffixes(raw_role_suffixes) if raw_role_suffixes.strip() else frozenset(DEFAULT_ROLE_SUFFIXES)

        ssl_verify = parse_bool(env.get("SSL_VERIFY", "true"), "SSL_VERIFY")
        ssl_ca_bundle = env.get("SSL_CA_BUNDLE", "").strip()
        if ssl_ca_bundle and not os.path.isfile(ssl_ca_bundle):
            raise ConfigError(f"SSL_CA_BUNDLE file not found: {ssl_ca_bundle!r}")

        return cls(
            ldap_url=ldap_url,
            ldap_bind_dn=env["LDAP_BIND_DN"].strip(),
            ldap_bind_password=env["LDAP_BIND_PASSWORD"],
            ldap_group_base_dn=env["LDAP_GROUP_BASE_DN"].strip(),
            grafana_url=env["GRAFANA_URL"].rstrip("/"),
            grafana_token=env["GRAFANA_TOKEN"],
            ldap_starttls=ldap_starttls,
            ldap_group_filter=_validate_filter(env.get("LDAP_GROUP_FILTER", DEFAULT_GROUP_FILTER), "LDAP_GROUP_FILTER"),
            ldap_group_scope=ldap_group_scope,
            ldap_group_name_attr=env.get("LDAP_GROUP_NAME_ATTR", "cn").strip() or "cn",
            ldap_member_mode=ldap_member_mode,
            ldap_member_attr=ldap_member_attr,
            ldap_user_base_dn=ldap_user_base_dn,
            ldap_user_uid_attr=env.get("LDAP_USER_UID_ATTR", "uid").strip() or "uid",
            ldap_user_match_attr=env.get("LDAP_USER_MATCH_ATTR", "mail").strip() or "mail",
            ldap_user_filter=_validate_filter(env.get("LDAP_USER_FILTER", DEFAULT_USER_FILTER), "LDAP_USER_FILTER"),
            group_prefix=group_prefix,
            match_key=match_key,
            dry_run=parse_bool(env.get("DRY_RUN", "true"), "DRY_RUN"),
            max_removal_ratio=max_removal_ratio,
            log_level=env.get("LOG_LEVEL", "INFO"),
            role_suffixes=role_suffixes,
            ssl_verify=ssl_verify,
            ssl_ca_bundle=ssl_ca_bundle,
        )


def request_with_retry(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """HTTP request with timeout and exponential-backoff retries.

    Retries connection errors, timeouts, and 5xx responses up to MAX_RETRIES
    times. 4xx responses are returned to the caller without retrying.
    """
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    last_exc: Exception | None = None
    last_resp: requests.Response | None = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log_event(logging.WARNING, "http_retry", method=method, url=url, attempt=attempt, delay_seconds=delay)
            time.sleep(delay)
        try:
            resp = session.request(method, url, **kwargs)
        except RETRYABLE_EXCEPTIONS as exc:
            last_exc = exc
            last_resp = None
            continue
        if resp.status_code >= 500:
            last_exc = None
            last_resp = resp
            continue
        return resp
    if last_resp is not None:
        return last_resp
    assert last_exc is not None
    raise last_exc


def _as_list(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _first(value: object):
    values = _as_list(value)
    return values[0] if values else None


class LdapClient:
    """Reads managed groups and resolves their members to match keys.

    A Connection can be injected for tests (e.g. an ldap3 MOCK_SYNC
    connection); otherwise one is built from the config on connect().
    """

    def __init__(self, cfg: Config, connection: Connection | None = None):
        self.cfg = cfg
        self.conn = connection
        # DN or uid (lowercased) -> match key, or None for skipped members;
        # avoids re-reading users that belong to several teams.
        self._member_cache: dict[str, str | None] = {}

    def _build_connection(self) -> Connection:
        cfg = self.cfg
        verify = cfg.effective_ssl_verify
        if verify is False:
            tls = Tls(validate=ssl.CERT_NONE)
        elif isinstance(verify, str):
            tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=verify)
        else:
            tls = Tls(validate=ssl.CERT_REQUIRED)
        server = Server(cfg.ldap_url, tls=tls, connect_timeout=CONNECT_TIMEOUT_SECONDS)
        return Connection(
            server,
            user=cfg.ldap_bind_dn,
            password=cfg.ldap_bind_password,
            auto_bind=False,
            raise_exceptions=False,
            receive_timeout=READ_TIMEOUT_SECONDS,
        )

    def connect(self) -> None:
        """Connect and simple-bind; anonymous bind is never attempted."""
        if self.conn is None:
            self.conn = self._build_connection()
        try:
            if self.cfg.ldap_starttls and not self.conn.start_tls():
                raise AuthError("ldap StartTLS negotiation failed")
            if not self.conn.bind():
                result = self.conn.result or {}
                raise AuthError(
                    "ldap bind failed for "
                    f"{self.cfg.ldap_bind_dn!r}: {result.get('description', 'unknown')}"
                )
        except LDAPException as exc:
            raise AuthError(f"ldap connection to {self.cfg.ldap_url} failed: {exc}") from exc

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.unbind()
            except LDAPException:
                pass

    def _paged_search(self, base: str, search_filter: str, scope: str, attributes: list[str]) -> list[dict]:
        """RFC 2696 paged search; merges all pages into one entry list."""
        entries: list[dict] = []
        cookie = None
        while True:
            try:
                self.conn.search(
                    base, search_filter, search_scope=scope, attributes=attributes,
                    paged_size=LDAP_PAGE_SIZE, paged_cookie=cookie,
                )
            except LDAPException as exc:
                raise ApiError(f"ldap search under {base!r} failed: {exc}") from exc
            result = self.conn.result or {}
            if result.get("result", -1) != RESULT_SUCCESS:
                raise ApiError(
                    f"ldap search under {base!r} failed: "
                    f"{result.get('description', 'unknown')} {result.get('message', '')}".strip()
                )
            entries.extend(e for e in (self.conn.response or []) if e.get("type") == "searchResEntry")
            controls = result.get("controls") or {}
            cookie = ((controls.get(PAGED_RESULTS_OID) or {}).get("value") or {}).get("cookie")
            if not cookie:
                return entries

    def get_groups(self) -> list[tuple[str, list[str], str]]:
        """All groups matching LDAP_GROUP_FILTER: (name, raw members, dn)."""
        cfg = self.cfg
        scope = SUBTREE if cfg.ldap_group_scope == "subtree" else LEVEL
        entries = self._paged_search(
            cfg.ldap_group_base_dn, cfg.ldap_group_filter, scope,
            [cfg.ldap_group_name_attr, cfg.ldap_member_attr],
        )
        groups: list[tuple[str, list[str], str]] = []
        for entry in entries:
            attrs = entry.get("attributes") or {}
            name = _first(attrs.get(cfg.ldap_group_name_attr))
            members = [str(m) for m in _as_list(attrs.get(cfg.ldap_member_attr)) if str(m).strip()]
            groups.append((str(name).strip() if name is not None else "", members, str(entry.get("dn", ""))))
        return groups

    def resolve_member(self, raw: str, team_name: str) -> str | None:
        """Raw member value (DN or uid) -> lowercased match key, or None to skip."""
        cache_key = raw.strip().lower()
        if cache_key in self._member_cache:
            return self._member_cache[cache_key]
        if self.cfg.ldap_member_mode == "member":
            key = self._lookup_by_dn(raw, team_name)
        else:
            key = self._lookup_by_uid(raw, team_name)
        self._member_cache[cache_key] = key
        return key

    def _lookup_by_dn(self, dn: str, team_name: str) -> str | None:
        cfg = self.cfg
        try:
            self.conn.search(
                dn, cfg.ldap_user_filter, search_scope=BASE,
                attributes=[cfg.ldap_user_match_attr, "objectClass"],
            )
        except LDAPException as exc:
            raise ApiError(f"ldap read of member {dn!r} failed: {exc}") from exc
        result = self.conn.result or {}
        code = result.get("result", -1)
        if code == RESULT_NO_SUCH_OBJECT:
            log_event(logging.WARNING, "member_entry_not_found", team=team_name, dn=dn)
            return None
        if code != RESULT_SUCCESS:
            raise ApiError(f"ldap read of member {dn!r} failed: {result.get('description', 'unknown')}")
        entries = [e for e in (self.conn.response or []) if e.get("type") == "searchResEntry"]
        if not entries:
            # Entry exists but does not match LDAP_USER_FILTER: treated as
            # inactive/ineligible, so it also gets removed from teams.
            log_event(logging.DEBUG, "member_filtered_out", team=team_name, dn=dn)
            return None
        attrs = entries[0].get("attributes") or {}
        object_classes = {str(oc).lower() for oc in _as_list(attrs.get("objectClass"))}
        if object_classes & GROUP_OBJECT_CLASSES:
            log_event(
                logging.WARNING, "nested_group_member_skipped",
                team=team_name, dn=dn, detail="nested groups are not resolved",
            )
            return None
        return self._extract_match_key(attrs, team_name, dn)

    def _lookup_by_uid(self, uid: str, team_name: str) -> str | None:
        cfg = self.cfg
        uid = uid.strip()
        # When the match attribute IS the uid attribute and no user filter is
        # configured, the uid itself is the match key - no lookup needed.
        if cfg.ldap_user_match_attr.lower() == cfg.ldap_user_uid_attr.lower() and cfg.ldap_user_filter == DEFAULT_USER_FILTER:
            return uid.lower()
        search_filter = f"(&({cfg.ldap_user_uid_attr}={escape_filter_chars(uid)}){cfg.ldap_user_filter})"
        entries = self._paged_search(cfg.ldap_user_base_dn, search_filter, SUBTREE, [cfg.ldap_user_match_attr])
        if not entries:
            log_event(
                logging.WARNING, "member_user_not_found",
                team=team_name, uid=uid,
                detail="no entry matches LDAP_USER_FILTER under LDAP_USER_BASE_DN",
            )
            return None
        if len(entries) > 1:
            log_event(logging.WARNING, "member_ambiguous", team=team_name, uid=uid, matches=len(entries))
            return None
        return self._extract_match_key(entries[0].get("attributes") or {}, team_name, uid)

    def _extract_match_key(self, attrs: dict, team_name: str, target: str) -> str | None:
        raw = _first(attrs.get(self.cfg.ldap_user_match_attr))
        if raw is None or not str(raw).strip():
            log_event(
                logging.WARNING, "member_missing_match_key",
                team=team_name, target=target, match_attr=self.cfg.ldap_user_match_attr,
            )
            return None
        return str(raw).strip().lower()


class GrafanaClient:
    def __init__(self, url: str, token: str, session: requests.Session | None = None, verify=True):
        self.base = url.rstrip("/")
        self.token = token
        self.verify = verify
        self.session = session or requests.Session()

    def _request(self, method: str, path: str, params: dict | None = None, json: dict | None = None) -> requests.Response:
        resp = request_with_retry(
            self.session, method, f"{self.base}{path}", params=params, json=json,
            headers={"Authorization": f"Bearer {self.token}"},
            verify=self.verify,
        )
        if resp.status_code in (401, 403):
            raise AuthError(
                f"grafana returned {resp.status_code} for {path}; "
                "check the service account token and its org role"
            )
        return resp

    def find_team(self, name: str) -> dict | None:
        resp = self._request("GET", "/api/teams/search", params={"name": name})
        if resp.status_code != 200:
            raise ApiError(f"grafana team search failed with status {resp.status_code}", status=resp.status_code)
        for team in resp.json().get("teams", []):
            if team.get("name", "").lower() == name.lower():
                return team
        return None

    def create_team(self, name: str) -> int:
        resp = self._request("POST", "/api/teams", json={"name": name})
        if resp.status_code != 200:
            raise ApiError(f"grafana team creation for {name!r} failed with status {resp.status_code}", status=resp.status_code)
        return resp.json()["teamId"]

    def get_team_members(self, team_id: int) -> list[dict]:
        resp = self._request("GET", f"/api/teams/{team_id}/members")
        if resp.status_code != 200:
            raise ApiError(f"grafana team members fetch failed with status {resp.status_code}", status=resp.status_code)
        return resp.json()

    def get_org_users(self) -> list[dict]:
        """All users of the current org.

        Uses the paginated /api/org/users/search endpoint (org Admin is
        sufficient — unlike /api/users/lookup, which needs server admin
        and 403s for org-Admin service accounts on Grafana 10+). Falls
        back to the plain /api/org/users listing when search is absent.
        """
        users: list[dict] = []
        page = 1
        while True:
            resp = self._request("GET", "/api/org/users/search", params={"perpage": PAGE_SIZE, "page": page})
            if resp.status_code == 404:
                resp = self._request("GET", "/api/org/users")
                if resp.status_code != 200:
                    raise ApiError(f"grafana org users fetch failed with status {resp.status_code}", status=resp.status_code)
                return resp.json()
            if resp.status_code != 200:
                raise ApiError(f"grafana org users search failed with status {resp.status_code}", status=resp.status_code)
            payload = resp.json()
            batch = payload.get("orgUsers", [])
            users.extend(batch)
            total = payload.get("totalCount", len(users))
            if not batch or len(users) >= total:
                return users
            page += 1

    def add_team_member(self, team_id: int, user_id: int) -> None:
        resp = self._request("POST", f"/api/teams/{team_id}/members", json={"userId": user_id})
        if resp.status_code != 200:
            raise ApiError(f"grafana add member failed with status {resp.status_code}", status=resp.status_code)

    def remove_team_member(self, team_id: int, user_id: int) -> None:
        resp = self._request("DELETE", f"/api/teams/{team_id}/members/{user_id}")
        if resp.status_code != 200:
            raise ApiError(f"grafana remove member failed with status {resp.status_code}", status=resp.status_code)


@dataclass
class TeamResult:
    added: int = 0
    removed: int = 0
    pending: int = 0
    guard_triggered: bool = False


def desired_members(ldap: LdapClient, team_name: str, member_lists: list[list[str]]) -> set[str]:
    """Desired team membership: resolved match keys of the groups' members."""
    desired: set[str] = set()
    for members in member_lists:
        for raw in members:
            key = ldap.resolve_member(raw, team_name)
            if key:
                desired.add(key)
    return desired


def member_key(cfg: Config, member: dict) -> str:
    raw = member.get("email") if cfg.match_key == "email" else member.get("login")
    return (raw or "").strip().lower()


def build_user_index(cfg: Config, org_users: list[dict]) -> dict[str, int]:
    """Map match key (lowercased email/login) -> Grafana user id."""
    index: dict[str, int] = {}
    for user in org_users:
        key = member_key(cfg, user)
        if key:
            index[key] = user["userId"]
    return index


def sync_team(cfg: Config, ldap: LdapClient, gf: GrafanaClient, team_name: str,
              member_lists: list[list[str]], user_index: dict[str, int]) -> TeamResult:
    result = TeamResult()
    desired = desired_members(ldap, team_name, member_lists)

    team = gf.find_team(team_name)
    team_id: int | None = None
    current_members: list[dict] = []
    if team is None:
        if not desired:
            # Avoid littering empty teams, e.g. a group whose members were
            # all filtered out or that has no members yet.
            log_event(logging.INFO, "empty_team_not_created", team=team_name)
            return result
        if cfg.dry_run:
            log_event(logging.INFO, "would_create_team", team=team_name)
        else:
            log_event(logging.INFO, "create_team", team=team_name)
            team_id = gf.create_team(team_name)
    else:
        team_id = team["id"]
        current_members = gf.get_team_members(team_id)

    current_by_key = {}
    for member in current_members:
        key = member_key(cfg, member)
        if key:
            current_by_key[key] = member

    to_add = sorted(desired - set(current_by_key))
    to_remove = sorted(set(current_by_key) - desired)

    if to_remove and current_members and (len(to_remove) / len(current_members)) > cfg.max_removal_ratio:
        log_event(
            logging.ERROR, "removal_guard_triggered",
            team=team_name, removals=len(to_remove), current_members=len(current_members),
            ratio=round(len(to_remove) / len(current_members), 3), limit=cfg.max_removal_ratio,
        )
        result.guard_triggered = True
        to_remove = []

    for key in to_add:
        user_id = user_index.get(key)
        if user_id is None:
            # Not in the org yet: the user has never logged into Grafana.
            result.pending += 1
            log_event(logging.INFO, "member_pending_first_login", team=team_name, target=key)
            continue
        if cfg.dry_run:
            log_event(logging.INFO, "would_add_member", team=team_name, target=key)
        else:
            log_event(logging.INFO, "add_member", team=team_name, target=key)
            gf.add_team_member(team_id, user_id)
        result.added += 1

    for key in to_remove:
        member = current_by_key[key]
        if cfg.dry_run:
            log_event(logging.INFO, "would_remove_member", team=team_name, target=key)
        else:
            log_event(logging.INFO, "remove_member", team=team_name, target=key)
            gf.remove_team_member(team_id, member["userId"])
        result.removed += 1

    log_event(
        logging.INFO, "team_synced",
        team=team_name, added=result.added, removed=result.removed,
        pending=result.pending, dry_run=cfg.dry_run,
    )
    return result


def collect_teams(cfg: Config, ldap: LdapClient) -> dict[str, list[list[str]]]:
    """Managed teams: team name -> raw member lists (same-named groups merge)."""
    teams: dict[str, list[list[str]]] = {}
    for name, members, dn in ldap.get_groups():
        if not name.startswith(cfg.group_prefix):
            continue
        kind = "role" if is_role_name(name, cfg.role_suffixes) else "service"
        log_event(logging.DEBUG, "managed_group", team=name, dn=dn, kind=kind, members=len(members))
        teams.setdefault(name, []).append(members)
    return teams


def run_sync(cfg: Config, ldap: LdapClient | None = None, gf: GrafanaClient | None = None) -> int:
    verify = cfg.effective_ssl_verify
    if verify is False:
        log_event(
            logging.WARNING, "ssl_verification_disabled",
            detail="TLS certificates are NOT verified; prefer SSL_CA_BUNDLE with a private CA",
        )
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    ldap = ldap or LdapClient(cfg)
    gf = gf or GrafanaClient(cfg.grafana_url, cfg.grafana_token, verify=verify)

    if cfg.dry_run:
        log_event(logging.INFO, "dry_run_enabled")

    try:
        ldap.connect()
        teams = collect_teams(cfg, ldap)

        if not teams:
            log_event(
                logging.WARNING, "no_managed_groups",
                prefix=cfg.group_prefix, base_dn=cfg.ldap_group_base_dn,
                detail="no groups matching the filter and prefix; nothing changed",
            )
            return EXIT_OK

        # One org-wide user listing instead of per-user lookups: org Admin
        # is sufficient, and membership diffs resolve against this index.
        user_index = build_user_index(cfg, gf.get_org_users())

        exit_code = EXIT_OK
        total = TeamResult()
        failed_teams = 0
        for team_name in sorted(teams):
            try:
                result = sync_team(cfg, ldap, gf, team_name, teams[team_name], user_index)
            except (ApiError, LDAPException, requests.RequestException) as exc:
                failed_teams += 1
                exit_code = EXIT_PARTIAL
                log_event(logging.ERROR, "team_sync_failed", team=team_name, error=str(exc))
                continue
            total.added += result.added
            total.removed += result.removed
            total.pending += result.pending
            if result.guard_triggered:
                exit_code = EXIT_PARTIAL

        log_event(
            logging.INFO, "sync_complete",
            teams=len(teams), failed_teams=failed_teams,
            added=total.added, removed=total.removed,
            pending_first_login=total.pending, dry_run=cfg.dry_run, exit_code=exit_code,
        )
        return exit_code
    finally:
        ldap.close()


def main(env: dict | None = None) -> int:
    env = os.environ if env is None else env
    setup_logging(env.get("LOG_LEVEL", "INFO"))
    try:
        cfg = Config.from_env(env)
    except ConfigError as exc:
        log_event(logging.ERROR, "config_error", error=str(exc))
        return EXIT_CONFIG
    try:
        return run_sync(cfg)
    except AuthError as exc:
        log_event(logging.ERROR, "auth_error", error=str(exc))
        return EXIT_CONFIG
    except (ApiError, LDAPException, requests.RequestException) as exc:
        log_event(logging.ERROR, "sync_failed", error=str(exc))
        return EXIT_PARTIAL


if __name__ == "__main__":
    sys.exit(main())
