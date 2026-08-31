"""Unit tests for sync.py.

LDAP is mocked with ldap3's MOCK_SYNC strategy (an in-memory DIT served
through the real client code paths); Grafana HTTP calls are mocked with
`responses`.

Group model under test: flat groups + naming convention. Groups under
LDAP_GROUP_BASE_DN whose cn starts with GROUP_PREFIX are managed; the cn
becomes the Grafana team name as-is. "<svc>_<suffix>" names are role
teams, each an independent Grafana team.
"""
import json
import logging
import sys
from pathlib import Path

import pytest
import responses
from ldap3 import MOCK_SYNC, Connection, Server
from responses import matchers

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync  # noqa: E402

GF = "https://grafana.example.com"
BIND_DN = "cn=grafana-sync,ou=svc,dc=example,dc=com"
BIND_PW = "s3cr3t-bind-pw"
GROUPS_DN = "ou=groups,dc=example,dc=com"
USERS_DN = "ou=people,dc=example,dc=com"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(sync.time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def capture_info_logs(caplog):
    caplog.set_level(logging.INFO)


def make_config(**overrides):
    defaults = dict(
        ldap_url="ldap://ldap.example.com",
        ldap_bind_dn=BIND_DN,
        ldap_bind_password=BIND_PW,
        ldap_group_base_dn=GROUPS_DN,
        grafana_url=GF,
        grafana_token="gf-token",
        group_prefix="grafana-",
        match_key="email",
        dry_run=False,
        max_removal_ratio=0.5,
    )
    defaults.update(overrides)
    return sync.Config(**defaults)


def user_dn(uid):
    return f"uid={uid},{USERS_DN}"


def make_ldap(entries, bind_password=BIND_PW):
    """MOCK_SYNC connection with the bind account and the given DIT."""
    conn = Connection(
        Server("ldap://ldap.example.com"), user=BIND_DN, password=bind_password,
        client_strategy=MOCK_SYNC, raise_exceptions=False,
    )
    conn.strategy.add_entry(BIND_DN, {"objectClass": ["person"], "cn": "grafana-sync", "userPassword": BIND_PW})
    for dn, attrs in entries.items():
        conn.strategy.add_entry(dn, attrs)
    return conn


def make_client(cfg, entries):
    return sync.LdapClient(cfg, connection=make_ldap(entries))


def group_entry(cn, members, member_attr="member", object_class="groupOfNames"):
    return f"cn={cn},{GROUPS_DN}", {"objectClass": [object_class], "cn": cn, member_attr: members}


def user_entry(uid, mail=None, **extra):
    attrs = {"objectClass": ["inetOrgPerson"], "uid": uid, "cn": uid}
    if mail:
        attrs["mail"] = mail
    attrs.update(extra)
    return user_dn(uid), attrs


def gf_member(user_id, email, login=None):
    return {"userId": user_id, "email": email, "login": login or email.split("@")[0]}


def org_user(user_id, email, login=None):
    return {"orgId": 1, "userId": user_id, "email": email, "login": login or email.split("@")[0], "role": "Viewer"}


def add_org_users(users):
    """Register the org user listing (/api/org/users/search, single page)."""
    responses.add(
        responses.GET, f"{GF}/api/org/users/search",
        json={"totalCount": len(users), "page": 1, "perPage": 100, "orgUsers": users},
    )


def add_team_search(name, team=None):
    teams = [team] if team else []
    responses.add(
        responses.GET, f"{GF}/api/teams/search",
        match=[matchers.query_param_matcher({"name": name})],
        json={"totalCount": len(teams), "teams": teams},
    )


def grafana_write_calls():
    return [
        c for c in responses.calls
        if c.request.method in ("POST", "DELETE", "PUT", "PATCH") and c.request.url.startswith(GF)
    ]


def created_team_names():
    return [
        json.loads(c.request.body)["name"]
        for c in responses.calls
        if c.request.method == "POST" and c.request.url == f"{GF}/api/teams"
    ]


@responses.activate
def test_member_mode_creates_team_and_adds_members():
    ldap = make_client(make_config(), dict([
        group_entry("grafana-devs", [user_dn("alice"), user_dn("bob")]),
        user_entry("alice", "alice@example.com"),
        user_entry("bob", "bob@example.com"),
    ]))
    add_team_search("grafana-devs")  # team does not exist yet
    responses.add(responses.POST, f"{GF}/api/teams", json={"teamId": 7, "message": "Team created"})
    add_org_users([org_user(101, "alice@example.com"), org_user(102, "bob@example.com")])
    added = responses.add(responses.POST, f"{GF}/api/teams/7/members", json={"message": "Member added"})

    assert sync.run_sync(make_config(), ldap=ldap) == 0
    assert added.call_count == 2


@responses.activate
def test_role_groups_become_independent_teams():
    """grafana-abc_{adm,editor,viewer} -> three independent role teams."""
    ldap = make_client(make_config(), dict([
        group_entry("grafana-abc_adm", [user_dn("alice")]),
        group_entry("grafana-abc_editor", [user_dn("bob")]),
        group_entry("grafana-abc_viewer", [user_dn("carol")]),
        user_entry("alice", "alice@example.com"),
        user_entry("bob", "bob@example.com"),
        user_entry("carol", "carol@example.com"),
    ]))
    add_team_search("grafana-abc_adm")
    add_team_search("grafana-abc_editor")
    add_team_search("grafana-abc_viewer")
    responses.add(responses.POST, f"{GF}/api/teams", json={"teamId": 7, "message": "Team created"})
    add_org_users([
        org_user(101, "alice@example.com"),
        org_user(102, "bob@example.com"),
        org_user(103, "carol@example.com"),
    ])
    responses.add(responses.POST, f"{GF}/api/teams/7/members", json={"message": "Member added"})

    assert sync.run_sync(make_config(), ldap=ldap) == 0
    assert sorted(created_team_names()) == [
        "grafana-abc_adm", "grafana-abc_editor", "grafana-abc_viewer",
    ]


@responses.activate
def test_groups_outside_prefix_are_untouched():
    ldap = make_client(make_config(), dict([
        group_entry("grafana-devs", [user_dn("alice")]),
        group_entry("hr-payroll", [user_dn("bob")]),
        user_entry("alice", "alice@example.com"),
        user_entry("bob", "bob@example.com"),
    ]))
    add_org_users([org_user(101, "alice@example.com"), org_user(102, "bob@example.com")])
    add_team_search("grafana-devs", {"id": 7, "name": "grafana-devs"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[gf_member(101, "alice@example.com")])

    assert sync.run_sync(make_config(), ldap=ldap) == 0
    assert not grafana_write_calls()
    # The out-of-prefix team is never even searched for
    assert not [c for c in responses.calls if "name=hr-payroll" in c.request.url]


@responses.activate
def test_no_managed_groups_changes_nothing(caplog):
    ldap = make_client(make_config(), dict([
        group_entry("hr-payroll", [user_dn("bob")]),
        user_entry("bob", "bob@example.com"),
    ]))

    assert sync.run_sync(make_config(), ldap=ldap) == 0
    assert all(not c.request.url.startswith(GF) for c in responses.calls)
    assert "no_managed_groups" in caplog.text


@responses.activate
def test_empty_team_is_not_created(caplog):
    ldap = make_client(make_config(), dict([
        group_entry("grafana-empty", []),
    ]))
    add_org_users([])
    add_team_search("grafana-empty")  # does not exist and has no desired members

    assert sync.run_sync(make_config(), ldap=ldap) == 0
    assert not grafana_write_calls()
    assert "empty_team_not_created" in caplog.text


@responses.activate
def test_member_dn_cache_reads_each_user_once():
    """alice is in two groups but her entry is read from LDAP only once."""
    cfg = make_config()
    conn = make_ldap(dict([
        group_entry("grafana-abc_adm", [user_dn("alice")]),
        group_entry("grafana-xyz_adm", [user_dn("alice")]),
        user_entry("alice", "alice@example.com"),
    ]))
    reads = []
    original_search = conn.search

    def counting_search(base, *args, **kwargs):
        reads.append(base)
        return original_search(base, *args, **kwargs)

    conn.search = counting_search
    ldap = sync.LdapClient(cfg, connection=conn)
    add_org_users([org_user(101, "alice@example.com")])
    add_team_search("grafana-abc_adm", {"id": 7, "name": "grafana-abc_adm"})
    add_team_search("grafana-xyz_adm", {"id": 8, "name": "grafana-xyz_adm"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[gf_member(101, "alice@example.com")])
    responses.add(responses.GET, f"{GF}/api/teams/8/members", json=[gf_member(101, "alice@example.com")])

    assert sync.run_sync(cfg, ldap=ldap) == 0
    assert reads.count(user_dn("alice")) == 1


@responses.activate
def test_user_filter_excludes_ineligible_members():
    """A member not matching LDAP_USER_FILTER is dropped and removed."""
    cfg = make_config(ldap_user_filter="(employeeType=active)")
    ldap = make_client(cfg, dict([
        group_entry("grafana-devs", [user_dn("alice"), user_dn("gone")]),
        user_entry("alice", "alice@example.com", employeeType="active"),
        user_entry("gone", "gone@example.com", employeeType="disabled"),
    ]))
    add_org_users([org_user(101, "alice@example.com"), org_user(102, "gone@example.com")])
    add_team_search("grafana-devs", {"id": 7, "name": "grafana-devs"})
    responses.add(
        responses.GET, f"{GF}/api/teams/7/members",
        json=[gf_member(101, "alice@example.com"), gf_member(102, "gone@example.com")],
    )
    removed = responses.add(responses.DELETE, f"{GF}/api/teams/7/members/102", json={"message": "Member removed"})

    assert sync.run_sync(cfg, ldap=ldap) == 0
    assert removed.call_count == 1
    assert len(grafana_write_calls()) == 1


@responses.activate
def test_member_missing_match_attr_is_skipped(caplog):
    ldap = make_client(make_config(), dict([
        group_entry("grafana-devs", [user_dn("nomail")]),
        user_entry("nomail"),  # no mail attribute
    ]))
    add_org_users([])
    add_team_search("grafana-devs", {"id": 7, "name": "grafana-devs"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[])

    assert sync.run_sync(make_config(), ldap=ldap) == 0
    assert "member_missing_match_key" in caplog.text
    assert not grafana_write_calls()


@responses.activate
def test_nested_group_member_is_skipped(caplog):
    """A member DN that is itself a group is ignored (no chasing in v1)."""
    inner_dn, inner_attrs = group_entry("grafana-inner", [user_dn("bob")])
    ldap = make_client(make_config(), dict([
        group_entry("grafana-devs", [user_dn("alice"), inner_dn]),
        (inner_dn, inner_attrs),
        user_entry("alice", "alice@example.com"),
        user_entry("bob", "bob@example.com"),
    ]))
    add_org_users([org_user(101, "alice@example.com"), org_user(102, "bob@example.com")])
    # grafana-inner is ALSO a managed group itself (matches prefix+filter)
    add_team_search("grafana-inner", {"id": 8, "name": "grafana-inner"})
    responses.add(responses.GET, f"{GF}/api/teams/8/members", json=[gf_member(102, "bob@example.com")])
    add_team_search("grafana-devs", {"id": 7, "name": "grafana-devs"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[gf_member(101, "alice@example.com")])

    assert sync.run_sync(make_config(), ldap=ldap) == 0
    assert "nested_group_member_skipped" in caplog.text
    # bob was not flattened into grafana-devs
    assert not grafana_write_calls()


@responses.activate
def test_missing_member_entry_is_skipped():
    ldap = make_client(make_config(), dict([
        group_entry("grafana-devs", [user_dn("alice"), user_dn("ghost")]),
        user_entry("alice", "alice@example.com"),
        # no entry for ghost
    ]))
    add_org_users([org_user(101, "alice@example.com")])
    add_team_search("grafana-devs", {"id": 7, "name": "grafana-devs"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[gf_member(101, "alice@example.com")])

    assert sync.run_sync(make_config(), ldap=ldap) == 0
    assert not grafana_write_calls()


@responses.activate
def test_same_named_groups_merge_members():
    """Two groups with the same cn (different OUs) sync as one team."""
    other_dn = f"cn=grafana-devs,ou=extra,{GROUPS_DN}"
    ldap = make_client(make_config(), dict([
        group_entry("grafana-devs", [user_dn("alice")]),
        (other_dn, {"objectClass": ["groupOfNames"], "cn": "grafana-devs", "member": [user_dn("bob")]}),
        user_entry("alice", "alice@example.com"),
        user_entry("bob", "bob@example.com"),
    ]))
    add_org_users([org_user(101, "alice@example.com"), org_user(102, "bob@example.com")])
    add_team_search("grafana-devs", {"id": 7, "name": "grafana-devs"})
    responses.add(
        responses.GET, f"{GF}/api/teams/7/members",
        json=[gf_member(101, "alice@example.com"), gf_member(102, "bob@example.com")],
    )

    assert sync.run_sync(make_config(), ldap=ldap) == 0
    assert not grafana_write_calls()


POSIX_OVERRIDES = dict(
    ldap_member_mode="memberUid",
    ldap_member_attr="memberUid",
    ldap_group_filter="(objectClass=posixGroup)",
    ldap_user_base_dn=USERS_DN,
)


@responses.activate
def test_memberuid_mode_resolves_users_by_uid():
    cfg = make_config(**POSIX_OVERRIDES)
    ldap = make_client(cfg, dict([
        group_entry("grafana-devs", ["alice", "bob"], member_attr="memberUid", object_class="posixGroup"),
        user_entry("alice", "alice@example.com"),
        user_entry("bob", "bob@example.com"),
    ]))
    add_team_search("grafana-devs")
    responses.add(responses.POST, f"{GF}/api/teams", json={"teamId": 7, "message": "Team created"})
    add_org_users([org_user(101, "alice@example.com"), org_user(102, "bob@example.com")])
    added = responses.add(responses.POST, f"{GF}/api/teams/7/members", json={"message": "Member added"})

    assert sync.run_sync(cfg, ldap=ldap) == 0
    assert added.call_count == 2


@responses.activate
def test_memberuid_match_attr_uid_skips_user_lookup():
    """match attr == uid attr + default filter: no user entries needed."""
    cfg = make_config(match_key="username", ldap_user_match_attr="uid", **POSIX_OVERRIDES)
    conn = make_ldap(dict([
        group_entry("grafana-devs", ["alice"], member_attr="memberUid", object_class="posixGroup"),
        # deliberately NO user entry for alice
    ]))
    reads = []
    original_search = conn.search

    def counting_search(base, *args, **kwargs):
        reads.append(base)
        return original_search(base, *args, **kwargs)

    conn.search = counting_search
    ldap = sync.LdapClient(cfg, connection=conn)
    add_org_users([org_user(101, "alice@example.com", login="alice")])
    add_team_search("grafana-devs", {"id": 7, "name": "grafana-devs"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[gf_member(101, "alice@example.com", login="alice")])

    assert sync.run_sync(cfg, ldap=ldap) == 0
    assert not grafana_write_calls()
    assert reads == [GROUPS_DN]  # only the group search hit LDAP


@responses.activate
def test_memberuid_unknown_uid_is_skipped(caplog):
    cfg = make_config(**POSIX_OVERRIDES)
    ldap = make_client(cfg, dict([
        group_entry("grafana-devs", ["ghost"], member_attr="memberUid", object_class="posixGroup"),
        user_entry("someone", "someone@example.com"),  # user base exists, ghost does not
    ]))
    add_org_users([])
    add_team_search("grafana-devs", {"id": 7, "name": "grafana-devs"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[])

    assert sync.run_sync(cfg, ldap=ldap) == 0
    assert "member_user_not_found" in caplog.text
    assert not grafana_write_calls()


@responses.activate
def test_removes_member_no_longer_in_group():
    ldap = make_client(make_config(), dict([
        group_entry("grafana-devs", [user_dn("alice")]),
        user_entry("alice", "alice@example.com"),
    ]))
    add_org_users([org_user(101, "alice@example.com"), org_user(102, "bob@example.com")])
    add_team_search("grafana-devs", {"id": 7, "name": "grafana-devs"})
    responses.add(
        responses.GET, f"{GF}/api/teams/7/members",
        json=[gf_member(101, "alice@example.com"), gf_member(102, "bob@example.com")],
    )
    removed = responses.add(responses.DELETE, f"{GF}/api/teams/7/members/102", json={"message": "Member removed"})

    # 1 removal out of 2 members = 0.5, not above the 0.5 guard threshold
    assert sync.run_sync(make_config(), ldap=ldap) == 0
    assert removed.call_count == 1
    assert len(grafana_write_calls()) == 1


@responses.activate
def test_user_not_yet_in_grafana_is_skipped_as_pending(caplog):
    ldap = make_client(make_config(), dict([
        group_entry("grafana-devs", [user_dn("alice"), user_dn("newbie")]),
        user_entry("alice", "alice@example.com"),
        user_entry("newbie", "newbie@example.com"),
    ]))
    add_team_search("grafana-devs", {"id": 7, "name": "grafana-devs"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[gf_member(101, "alice@example.com")])
    add_org_users([org_user(101, "alice@example.com")])  # newbie not in the org yet

    assert sync.run_sync(make_config(), ldap=ldap) == 0
    assert not grafana_write_calls()
    assert "member_pending_first_login" in caplog.text
    assert "pending_first_login=1" in caplog.text


@responses.activate
def test_removal_ratio_guard_skips_team_and_exits_1(caplog):
    ldap = make_client(make_config(), dict([
        # devs: 3 of 4 members would be removed -> 0.75 > 0.5 -> guard
        group_entry("grafana-devs", [user_dn("alice")]),
        # ops: unaffected, still processed normally
        group_entry("grafana-ops", [user_dn("erin")]),
        user_entry("alice", "alice@example.com"),
        user_entry("erin", "erin@example.com"),
    ]))
    add_team_search("grafana-devs", {"id": 7, "name": "grafana-devs"})
    responses.add(
        responses.GET, f"{GF}/api/teams/7/members",
        json=[
            gf_member(101, "alice@example.com"),
            gf_member(102, "bob@example.com"),
            gf_member(103, "carol@example.com"),
            gf_member(104, "dave@example.com"),
        ],
    )
    add_team_search("grafana-ops", {"id": 8, "name": "grafana-ops"})
    responses.add(responses.GET, f"{GF}/api/teams/8/members", json=[])
    add_org_users([org_user(101, "alice@example.com"), org_user(105, "erin@example.com")])
    ops_add = responses.add(responses.POST, f"{GF}/api/teams/8/members", json={"message": "Member added"})

    assert sync.run_sync(make_config(), ldap=ldap) == 1
    assert "removal_guard_triggered" in caplog.text
    assert not [c for c in responses.calls if c.request.method == "DELETE"]
    assert ops_add.call_count == 1


@responses.activate
def test_dry_run_makes_no_write_calls(caplog):
    cfg = make_config(dry_run=True, max_removal_ratio=1.0)
    ldap = make_client(cfg, dict([
        group_entry("grafana-devs", [user_dn("carol")]),
        group_entry("grafana-new", [user_dn("alice")]),
        user_entry("carol", "carol@example.com"),
        user_entry("alice", "alice@example.com"),
    ]))
    add_team_search("grafana-devs", {"id": 7, "name": "grafana-devs"})
    responses.add(
        responses.GET, f"{GF}/api/teams/7/members",
        json=[gf_member(102, "bob@example.com"), gf_member(101, "old@example.com")],
    )
    add_team_search("grafana-new")  # would need to be created
    add_org_users([org_user(103, "carol@example.com"), org_user(101, "alice@example.com")])

    assert sync.run_sync(cfg, ldap=ldap) == 0
    assert not grafana_write_calls()
    assert "would_create_team" in caplog.text
    assert "would_add_member" in caplog.text
    assert "would_remove_member" in caplog.text


@responses.activate
def test_match_key_username_uses_login_and_is_case_insensitive():
    cfg = make_config(match_key="username", ldap_user_match_attr="uid")
    ldap = make_client(cfg, dict([
        group_entry("grafana-devs", [user_dn("Alice")]),
        user_entry("Alice", "alice@example.com"),
    ]))
    add_org_users([org_user(101, "other@example.com", login="alice")])
    add_team_search("grafana-devs", {"id": 7, "name": "grafana-devs"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[{"userId": 101, "email": "other@example.com", "login": "alice"}])

    assert sync.run_sync(cfg, ldap=ldap) == 0
    assert not grafana_write_calls()


@responses.activate
def test_org_users_pagination_over_multiple_pages():
    ldap = make_client(make_config(), dict([
        group_entry("grafana-devs", [user_dn("user0"), user_dn("user100")]),
        user_entry("user0", "user0@example.com"),
        user_entry("user100", "user100@example.com"),
    ]))
    # 101 org users spread over two search pages; user100 is on page 2
    page1 = [org_user(1000 + i, f"user{i}@example.com") for i in range(100)]
    page2 = [org_user(1100, "user100@example.com")]
    responses.add(
        responses.GET, f"{GF}/api/org/users/search",
        match=[matchers.query_param_matcher({"perpage": "100", "page": "1"})],
        json={"totalCount": 101, "page": 1, "perPage": 100, "orgUsers": page1},
    )
    responses.add(
        responses.GET, f"{GF}/api/org/users/search",
        match=[matchers.query_param_matcher({"perpage": "100", "page": "2"})],
        json={"totalCount": 101, "page": 2, "perPage": 100, "orgUsers": page2},
    )
    add_team_search("grafana-devs")
    responses.add(responses.POST, f"{GF}/api/teams", json={"teamId": 7, "message": "Team created"})
    added = responses.add(responses.POST, f"{GF}/api/teams/7/members", json={"message": "Member added"})

    # user100 resolvable only if page 2 was fetched
    assert sync.run_sync(make_config(), ldap=ldap) == 0
    assert added.call_count == 2
    search_calls = [c for c in responses.calls if "/api/org/users/search" in c.request.url]
    assert len(search_calls) == 2


@responses.activate
def test_org_users_fallback_for_old_grafana():
    ldap = make_client(make_config(), dict([
        group_entry("grafana-devs", [user_dn("alice")]),
        user_entry("alice", "alice@example.com"),
    ]))
    # Older Grafana without the /search endpoint
    responses.add(responses.GET, f"{GF}/api/org/users/search", status=404, json={"message": "Not found"})
    responses.add(responses.GET, f"{GF}/api/org/users", json=[org_user(101, "alice@example.com")])
    add_team_search("grafana-devs")
    responses.add(responses.POST, f"{GF}/api/teams", json={"teamId": 7, "message": "Team created"})
    added = responses.add(responses.POST, f"{GF}/api/teams/7/members", json={"message": "Member added"})

    assert sync.run_sync(make_config(), ldap=ldap) == 0
    assert added.call_count == 1


@responses.activate
def test_retry_on_5xx_then_success():
    ldap = make_client(make_config(), dict([
        group_entry("grafana-devs", [user_dn("alice")]),
        user_entry("alice", "alice@example.com"),
    ]))
    responses.add(responses.GET, f"{GF}/api/org/users/search", status=502, json={"message": "bad gateway"})
    add_org_users([org_user(101, "alice@example.com")])
    add_team_search("grafana-devs", {"id": 7, "name": "grafana-devs"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[gf_member(101, "alice@example.com")])

    assert sync.run_sync(make_config(), ldap=ldap) == 0


@responses.activate
def test_one_failed_team_does_not_stop_others():
    ldap = make_client(make_config(), dict([
        group_entry("grafana-devs", [user_dn("alice")]),
        group_entry("grafana-ops", [user_dn("erin")]),
        user_entry("alice", "alice@example.com"),
        user_entry("erin", "erin@example.com"),
    ]))
    # devs search keeps failing with a non-retryable client error
    responses.add(
        responses.GET, f"{GF}/api/teams/search",
        match=[matchers.query_param_matcher({"name": "grafana-devs"})],
        status=422, json={"message": "boom"},
    )
    add_team_search("grafana-ops", {"id": 8, "name": "grafana-ops"})
    responses.add(responses.GET, f"{GF}/api/teams/8/members", json=[])
    add_org_users([org_user(101, "alice@example.com"), org_user(105, "erin@example.com")])
    ops_add = responses.add(responses.POST, f"{GF}/api/teams/8/members", json={"message": "Member added"})

    assert sync.run_sync(make_config(), ldap=ldap) == 1
    assert ops_add.call_count == 1


@responses.activate
def test_grafana_auth_failure_raises_auth_error():
    ldap = make_client(make_config(), dict([
        group_entry("grafana-devs", [user_dn("alice")]),
        user_entry("alice", "alice@example.com"),
    ]))
    responses.add(responses.GET, f"{GF}/api/org/users/search", status=401, json={"message": "Unauthorized"})

    with pytest.raises(sync.AuthError):
        sync.run_sync(make_config(), ldap=ldap)


def test_bind_failure_raises_auth_error_without_leaking_password(caplog):
    ldap = sync.LdapClient(make_config(), connection=make_ldap({}, bind_password="wrong-password"))

    with pytest.raises(sync.AuthError) as excinfo:
        sync.run_sync(make_config(), ldap=ldap)
    assert BIND_PW not in str(excinfo.value)
    assert "wrong-password" not in str(excinfo.value)
    assert BIND_PW not in caplog.text


class _ScriptedPagedConn:
    """Fake connection returning scripted pages with RFC 2696 cookies."""

    def __init__(self, pages):
        self._pages = pages
        self.calls = 0
        self.response = None
        self.result = None

    def search(self, base, search_filter, search_scope=None, attributes=None,
               paged_size=None, paged_cookie=None):
        entries, cookie = self._pages[self.calls]
        self.calls += 1
        self.response = entries
        self.result = {
            "result": 0, "description": "success", "message": "",
            "controls": {sync.PAGED_RESULTS_OID: {"value": {"cookie": cookie}}},
        }
        return True


def _entry(dn, attributes):
    return {"type": "searchResEntry", "dn": dn, "attributes": attributes}


def test_paged_search_follows_cookies_across_pages():
    pages = [
        ([_entry(f"cn=g{i},{GROUPS_DN}", {"cn": f"g{i}"}) for i in range(2)], b"next"),
        ([_entry(f"cn=g2,{GROUPS_DN}", {"cn": "g2"})], b""),
    ]
    conn = _ScriptedPagedConn(pages)
    ldap = sync.LdapClient(make_config(), connection=conn)

    entries = ldap._paged_search(GROUPS_DN, "(objectClass=groupOfNames)", "SUBTREE", ["cn"])
    assert conn.calls == 2
    assert [e["dn"] for e in entries] == [f"cn=g0,{GROUPS_DN}", f"cn=g1,{GROUPS_DN}", f"cn=g2,{GROUPS_DN}"]


def test_paged_search_error_result_raises_api_error():
    class _FailingConn:
        response = []
        result = {"result": 1, "description": "operationsError", "message": "boom"}

        def search(self, *args, **kwargs):
            return False

    ldap = sync.LdapClient(make_config(), connection=_FailingConn())
    with pytest.raises(sync.ApiError):
        ldap._paged_search(GROUPS_DN, "(objectClass=*)", "SUBTREE", ["cn"])


BASE_ENV = {
    "LDAP_URL": "ldaps://ldap.example.com:636",
    "LDAP_BIND_DN": BIND_DN,
    "LDAP_BIND_PASSWORD": BIND_PW,
    "LDAP_GROUP_BASE_DN": GROUPS_DN,
    "GRAFANA_URL": GF,
    "GRAFANA_TOKEN": "gf-token",
}


def test_config_missing_required_vars_exits_2():
    assert sync.main({"LDAP_URL": "ldap://ldap.example.com"}) == 2


def test_config_empty_bind_password_is_rejected():
    """An empty bind password would mean an anonymous bind - forbidden."""
    with pytest.raises(sync.ConfigError):
        sync.Config.from_env({**BASE_ENV, "LDAP_BIND_PASSWORD": ""})


def test_config_defaults():
    cfg = sync.Config.from_env(dict(BASE_ENV))
    assert cfg.dry_run is True
    assert cfg.group_prefix == "grafana-"
    assert cfg.match_key == "email"
    assert cfg.max_removal_ratio == 0.5
    assert cfg.role_suffixes == frozenset(sync.DEFAULT_ROLE_SUFFIXES)
    assert cfg.ldap_member_mode == "member"
    assert cfg.ldap_member_attr == "member"
    assert cfg.ldap_group_filter == "(objectClass=groupOfNames)"
    assert cfg.ldap_group_name_attr == "cn"
    assert cfg.ldap_user_match_attr == "mail"
    assert cfg.ldap_user_filter == "(objectClass=*)"
    assert cfg.ldap_starttls is False


def test_config_memberuid_mode_defaults_member_attr():
    cfg = sync.Config.from_env({
        **BASE_ENV, "LDAP_MEMBER_MODE": "memberUid", "LDAP_USER_BASE_DN": USERS_DN,
    })
    assert cfg.ldap_member_mode == "memberUid"
    assert cfg.ldap_member_attr == "memberUid"


def test_config_memberuid_mode_requires_user_base_dn():
    with pytest.raises(sync.ConfigError):
        sync.Config.from_env({**BASE_ENV, "LDAP_MEMBER_MODE": "memberUid"})


def test_config_invalid_member_mode():
    with pytest.raises(sync.ConfigError):
        sync.Config.from_env({**BASE_ENV, "LDAP_MEMBER_MODE": "memberOf"})


def test_config_invalid_ldap_url_scheme():
    with pytest.raises(sync.ConfigError):
        sync.Config.from_env({**BASE_ENV, "LDAP_URL": "https://ldap.example.com"})


def test_config_starttls_conflicts_with_ldaps():
    with pytest.raises(sync.ConfigError):
        sync.Config.from_env({**BASE_ENV, "LDAP_STARTTLS": "true"})


def test_config_starttls_with_plain_ldap():
    cfg = sync.Config.from_env({
        **BASE_ENV, "LDAP_URL": "ldap://ldap.example.com", "LDAP_STARTTLS": "true",
    })
    assert cfg.ldap_starttls is True


def test_config_invalid_group_scope():
    with pytest.raises(sync.ConfigError):
        sync.Config.from_env({**BASE_ENV, "LDAP_GROUP_SCOPE": "base"})


def test_config_unparenthesized_filter_is_rejected():
    with pytest.raises(sync.ConfigError):
        sync.Config.from_env({**BASE_ENV, "LDAP_GROUP_FILTER": "objectClass=group"})


def test_config_invalid_match_key():
    with pytest.raises(sync.ConfigError):
        sync.Config.from_env({**BASE_ENV, "MATCH_KEY": "displayName"})


def test_config_empty_group_prefix_is_rejected():
    with pytest.raises(sync.ConfigError):
        sync.Config.from_env({**BASE_ENV, "GROUP_PREFIX": ""})


def test_config_parses_role_suffixes():
    cfg = sync.Config.from_env({**BASE_ENV, "ROLE_SUFFIXES": "adm, Editor ,viewer"})
    assert cfg.role_suffixes == frozenset({"adm", "editor", "viewer"})


def test_config_invalid_role_suffixes():
    with pytest.raises(sync.ConfigError):
        sync.parse_role_suffixes("  ,  ")


def test_config_ssl_verify_defaults_to_true():
    cfg = sync.Config.from_env(dict(BASE_ENV))
    assert cfg.ssl_verify is True
    assert cfg.effective_ssl_verify is True


def test_config_ssl_verify_can_be_disabled():
    cfg = sync.Config.from_env({**BASE_ENV, "SSL_VERIFY": "false"})
    assert cfg.ssl_verify is False
    assert cfg.effective_ssl_verify is False


def test_config_ssl_ca_bundle(tmp_path):
    bundle = tmp_path / "ca.crt"
    bundle.write_text("dummy")
    cfg = sync.Config.from_env({**BASE_ENV, "SSL_CA_BUNDLE": str(bundle)})
    assert cfg.effective_ssl_verify == str(bundle)


def test_config_ssl_ca_bundle_missing_file_is_config_error():
    with pytest.raises(sync.ConfigError):
        sync.Config.from_env({**BASE_ENV, "SSL_CA_BUNDLE": "/does/not/exist.crt"})


class _RecordingSession:
    """Stub session capturing request kwargs."""

    def __init__(self, payload):
        self.calls = []
        self._payload = payload

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))

        class _Resp:
            status_code = 200

            def json(_self):
                return self._payload

        return _Resp()


def test_grafana_client_passes_verify_to_requests():
    session = _RecordingSession({"totalCount": 0, "teams": []})
    gf = sync.GrafanaClient(GF, "tok", session=session, verify="/etc/ssl/private-ca.crt")
    gf.find_team("devs")
    assert session.calls[0][2]["verify"] == "/etc/ssl/private-ca.crt"


SUFFIXES = frozenset(sync.DEFAULT_ROLE_SUFFIXES)


@pytest.mark.parametrize("name,expected", [
    ("grafana-abc_adm", True),
    ("grafana-abc_admin", True),
    ("grafana-abc_editor", True),
    ("grafana-abc-viewer", True),
    ("GRAFANA-ABC_ADM", True),
    ("grafana-abc_leads", False),
    ("grafana-abc", False),
    ("adm", False),        # bare suffix without a service part
    ("_adm", False),       # empty service part
])
def test_is_role_name(name, expected):
    assert sync.is_role_name(name, SUFFIXES) is expected
