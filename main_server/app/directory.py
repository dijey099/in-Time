"""Directory authentication: LDAP and Active Directory (ldap3).

authenticate(source, conf, username, password) checks the password against the directory server and
returns the account's details and the directory groups it belongs to. The password is only sent to
the directory server (never stored), over LDAPS or StartTLS when configured.

- LDAP: the account is found with a service account (or anonymously) using the user filter, then
  the password is checked by binding as that account. Groups: search with the group filter.
- Active Directory: the password is checked by binding as user@domain, then the account is read
  (sAMAccountName). Groups: every group the account belongs to, nested ones included (or only
  direct memberOf groups).
"""
import ssl

from ldap3 import NONE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import parse_dn

TIMEOUT = 8  # seconds, connection and each operation
DEFAULT_GROUP_FILTER = "(|(member={dn})(uniqueMember={dn})(memberUid={username}))"


class AuthFailed(Exception):
    """Wrong username or password (or the account is unknown / disabled in the directory)."""


class DirectoryError(Exception):
    """Directory server unreachable or misconfigured."""


def _server(conf):
    url = (conf.get("url") or "").strip()
    if not url:
        raise DirectoryError("no server URL")
    tls = Tls(validate=ssl.CERT_REQUIRED if conf.get("verify_cert", True) else ssl.CERT_NONE,
              version=ssl.PROTOCOL_TLS_CLIENT)
    return Server(url, use_ssl=url.lower().startswith("ldaps://"), tls=tls, get_info=NONE,
                  connect_timeout=TIMEOUT)


def _connect(server, user=None, password=None):
    """Bound connection (anonymous without user). Separate function so tests can replace it."""
    return Connection(server, user=user, password=password, receive_timeout=TIMEOUT, raise_exceptions=False)


def _bind(conf, server, user, password):
    """True if the bind succeeds; DirectoryError if the server can't be used."""
    conn = _connect(server, user, password)
    try:
        conn.open()
        if conf.get("start_tls") and not str(conf.get("url", "")).lower().startswith("ldaps://"):
            conn.start_tls()
        if conn.bind():
            return conn
        conn.unbind()
        return None
    except LDAPException as exc:
        raise DirectoryError(str(exc) or type(exc).__name__) from None


def _first(entry, attr):
    if not attr or attr not in entry:
        return None
    value = entry[attr]
    return (value[0] if isinstance(value, list) and value else value) or None


def cn_of(dn):
    """Name of a group from its DN ("CN=HR team,OU=Groups,DC=..." -> "HR team")."""
    try:
        return parse_dn(dn)[0][1]
    except Exception:  # noqa: BLE001 - not a DN: the value itself
        return dn


def _search(conn, base, flt, attrs):
    conn.search(base, flt, SUBTREE, attributes=attrs, size_limit=500)
    return [e for e in conn.response or [] if e.get("type") == "searchResEntry"]


# ----------------------------------------------------------------------- LDAP
def ldap_authenticate(conf, username, password):
    server = _server(conf)
    service = _bind(conf, server, conf.get("bind_dn") or None, conf.get("bind_password") or None)
    if service is None:
        raise DirectoryError("the service account (bind DN) was refused by the server")
    try:
        flt = (conf.get("user_filter") or "(uid={username})").replace("{username}", escape_filter_chars(username))
        name_attr, mail_attr = conf.get("attr_name") or "cn", conf.get("attr_email") or "mail"
        found = _search(service, conf.get("user_base") or "", flt, [name_attr, mail_attr])
        if len(found) != 1:
            raise AuthFailed("unknown user" if not found else "several accounts match the user filter")
        dn, attrs = found[0]["dn"], found[0]["attributes"]
        user_conn = _bind(conf, server, dn, password)
        if user_conn is None:
            raise AuthFailed("wrong password")
        user_conn.unbind()
        groups = []
        if conf.get("group_base"):
            gflt = conf.get("group_filter") or DEFAULT_GROUP_FILTER
            gflt = gflt.replace("{dn}", escape_filter_chars(dn)).replace("{username}", escape_filter_chars(username))
            groups = [g["dn"] for g in _search(service, conf["group_base"], gflt, ["cn"])]
        return {"dn": dn, "full_name": _first(attrs, name_attr), "email": _first(attrs, mail_attr), "groups": groups}
    finally:
        service.unbind()


# ----------------------------------------------------------- Active Directory
def ad_username(typed, conf):
    """sAMAccountName from "alice", "EXAMPLE\\alice" or "alice@example.local"."""
    name = typed.strip()
    if "\\" in name:
        name = name.split("\\", 1)[1]
    elif "@" in name:
        name = name.rsplit("@", 1)[0]
    return name


def ad_base(conf):
    return conf.get("base_dn") or ",".join(f"DC={p}" for p in (conf.get("domain") or "").split(".") if p)


def _expand_member_of(conn, dns, limit=500):
    """The groups `dns` and every group they are in, by reading each group's memberOf."""
    found, todo = {d.lower(): d for d in dns}, list(dns)
    while todo and len(found) < limit:
        dn = todo.pop()
        conn.search(dn, "(objectClass=*)", "BASE", attributes=["memberOf"])
        for e in conn.response or []:
            for parent in _values(e.get("attributes") or {}, "memberOf"):
                if parent.lower() not in found:
                    found[parent.lower()] = parent
                    todo.append(parent)
    return list(found.values())


def ad_authenticate(conf, username, password):
    domain = (conf.get("domain") or "").strip()
    if not domain:
        raise DirectoryError("no domain")
    sam = ad_username(username, conf)
    server = _server(conf)
    conn = _bind(conf, server, f"{sam}@{domain}", password)
    if conn is None:
        raise AuthFailed("wrong password, unknown or disabled account")
    try:
        found = _search(conn, ad_base(conf),
                        f"(&(objectCategory=person)(objectClass=user)(sAMAccountName={escape_filter_chars(sam)}))",
                        ["displayName", "cn", "mail", "memberOf"])
        if not found:
            raise AuthFailed("account not found under the base DN")
        dn, attrs = found[0]["dn"], found[0]["attributes"]
        groups = list(attrs.get("memberOf") or [])
        if conf.get("nested_groups", True):
            # every group, nested ones included (LDAP_MATCHING_RULE_IN_CHAIN)
            nested = _search(conn, ad_base(conf),
                             f"(&(objectClass=group)(member:1.2.840.113556.1.4.1941:={escape_filter_chars(dn)}))", ["cn"])
            groups = list(dict.fromkeys(groups + [g["dn"] for g in nested]))
            if not nested and groups:  # rule not supported by this server: follow the groups' memberOf
                groups = _expand_member_of(conn, groups)
        return {"dn": dn, "full_name": _first(attrs, "displayName") or _first(attrs, "cn"),
                "email": _first(attrs, "mail"), "groups": groups}
    finally:
        conn.unbind()


def authenticate(source, conf, username, password):
    """{"dn", "full_name", "email", "groups"}; AuthFailed or DirectoryError otherwise."""
    if not password:
        raise AuthFailed("empty password")  # an empty password would be an anonymous bind
    if source == "ad":
        return ad_authenticate(conf, username, password)
    return ldap_authenticate(conf, username, password)


# ------------------------------------------------------------- group listing
LDAP_GROUP_CLASSES = "(|(objectClass=groupOfNames)(objectClass=groupOfUniqueNames)(objectClass=posixGroup)(objectClass=group))"
MAX_GROUPS = 5000
MAX_USERS = 20000


def _paged(conn, base, flt, attrs, limit):
    entries = []
    try:  # paged: Active Directory returns at most 1000 entries per page
        for e in conn.extend.standard.paged_search(base, flt, SUBTREE, attributes=attrs, paged_size=500, generator=True):
            if e.get("type") == "searchResEntry":
                entries.append(e)
                if len(entries) >= limit:
                    break
    except LDAPException:
        entries = _search(conn, base, flt, attrs)
    return entries


def _values(attrs, name):
    value = attrs.get(name)
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _service_connection(source, conf):
    """Connection used to read the directory: LDAP service account, or AD reader account."""
    server = _server(conf)
    if source == "ad":
        user = (conf.get("bind_user") or "").strip()
        if not user:
            raise DirectoryError("no reader account: set one to list the Active Directory groups")
        if "@" not in user and "=" not in user and "\\" not in user:
            user = f"{user}@{conf.get('domain')}"
        conn = _bind(conf, server, user, conf.get("bind_password") or None)
    else:
        conn = _bind(conf, server, conf.get("bind_dn") or None, conf.get("bind_password") or None)
    if conn is None:
        raise DirectoryError("the service account was refused by the server")
    return conn


def list_groups(source, conf):
    """Every group of the directory under the groups base: [{"dn", "name", "description"}]."""
    if source == "ad":
        base, flt = conf.get("group_base") or ad_base(conf), "(objectClass=group)"
    else:
        base, flt = conf.get("group_base"), LDAP_GROUP_CLASSES
        if not base:
            raise DirectoryError("no groups base DN")
    attrs_wanted = ["cn", "description"] + (["memberOf"] if source == "ad" else ["member", "uniqueMember", "memberUid"])
    conn = _service_connection(source, conf)
    try:
        groups = []
        for e in _paged(conn, base, flt, attrs_wanted, MAX_GROUPS):
            attrs = e.get("attributes") or {}
            desc = attrs.get("description")
            groups.append({"dn": e["dn"], "name": str(_first(attrs, "cn") or cn_of(e["dn"])),
                           "description": str((desc[0] if isinstance(desc, list) and desc else desc) or "")[:300] or None,
                           # LDAP: who is in it; AD: the groups it is in (nested groups)
                           "members": _values(attrs, "member") + _values(attrs, "uniqueMember"),
                           "member_uids": _values(attrs, "memberUid"), "member_of": _values(attrs, "memberOf")})
        return groups
    finally:
        conn.unbind()


def ldap_login_attribute(conf):
    """Attribute people log in with, from the user filter ("(uid={username})" -> "uid")."""
    import re
    m = re.search(r"\(\s*([A-Za-z][\w-]*)\s*=\s*\{username\}\s*\)", conf.get("user_filter") or "")
    return m.group(1) if m else "uid"


def list_users(source, conf):
    """Every account of the directory: [{"dn", "username", "full_name", "email", "member_of", "disabled"}].
    LDAP: under the users base DN, matching the user filter with any username. AD: people under the users
    base DN (or the whole domain)."""
    conn = _service_connection(source, conf)
    try:
        users = []
        if source == "ad":
            base = conf.get("user_base") or ad_base(conf)
            flt = "(&(objectCategory=person)(objectClass=user))"
            for e in _paged(conn, base, flt, ["sAMAccountName", "displayName", "cn", "mail", "memberOf",
                                              "userAccountControl"], MAX_USERS):
                attrs = e.get("attributes") or {}
                sam = _first(attrs, "sAMAccountName")
                if not sam:
                    continue
                try:
                    uac = int(_first(attrs, "userAccountControl") or 0)
                except (TypeError, ValueError):
                    uac = 0
                users.append({"dn": e["dn"], "username": str(sam), "full_name": _first(attrs, "displayName") or _first(attrs, "cn"),
                              "email": _first(attrs, "mail"), "member_of": _values(attrs, "memberOf"),
                              "disabled": bool(uac & 2)})  # ACCOUNTDISABLE
        else:
            login_attr = ldap_login_attribute(conf)
            name_attr, mail_attr = conf.get("attr_name") or "cn", conf.get("attr_email") or "mail"
            flt = (conf.get("user_filter") or "(uid={username})").replace("{username}", "*")
            # with a group filter of their own, the groups of each account are searched like at login
            gflt = (conf.get("group_filter") or "").strip()
            exact = bool(conf.get("group_base")) and gflt not in ("", DEFAULT_GROUP_FILTER)
            for e in _paged(conn, conf.get("user_base") or "", flt, [login_attr, name_attr, mail_attr], MAX_USERS):
                attrs = e.get("attributes") or {}
                login = _first(attrs, login_attr)
                if not login:
                    continue
                item = {"dn": e["dn"], "username": str(login), "full_name": _first(attrs, name_attr),
                        "email": _first(attrs, mail_attr), "member_of": [], "disabled": False}
                if exact:
                    one = gflt.replace("{dn}", escape_filter_chars(e["dn"])).replace("{username}", escape_filter_chars(str(login)))
                    item["member_of"], item["exact"] = [g["dn"] for g in _search(conn, conf["group_base"], one, ["cn"])], True
                users.append(item)
        return users
    finally:
        conn.unbind()


def test_connection(source, conf, username=None, password=None):
    """Check the settings: bind with the service account (LDAP) and, when given, log in a test user.
    Returns a message; raises DirectoryError / AuthFailed."""
    if username:
        info = authenticate(source, conf, username, password)
        return (f"Login OK for {info['dn']} — {len(info['groups'])} group(s): "
                + (", ".join(cn_of(g) for g in info["groups"][:15]) or "none"))
    if source == "ad" and not conf.get("bind_user"):
        raise DirectoryError("enter a test username and password (or a reader account)")
    conn = _service_connection(source, conf)
    conn.unbind()
    who = "the reader account" if source == "ad" else ("the service account" if conf.get("bind_dn") else "anonymous bind")
    try:
        found = len(list_groups(source, conf))
        return f"Connected with {who} — {found} group(s) found"
    except DirectoryError as exc:
        return f"Connected with {who} — groups not listed: {exc}"
