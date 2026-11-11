"""Built-in roles of the admin console and what each one may do.

Each role has its own list of permissions (a role doesn't include the ones below it). A user's role is
the highest of their own role and their groups' roles; they may do what any of those roles allows
(see accounts.effective_role / effective_permissions).
"""

# (permission, description shown on the Roles page)
PERMISSIONS = [
    ("dashboard.view", "See the dashboard"),
    ("attendances.view", "See attendances (check-ins, snapshots, locations)"),
    ("attendances.export", "Export attendances (CSV)"),
    ("collaborators.view", "See collaborators and their photos"),
    ("collaborators.edit", "Create, edit, deactivate and delete collaborators"),
    ("companies.view", "See companies and departments"),
    ("companies.edit", "Create, rename and delete companies and departments"),
    ("relays.manage", "See and manage relays (create, edit, tokens, site areas)"),
    ("settings.general", "Change the general settings (max in / min out times)"),
    ("auth.manage", "Change the authentication source and its settings"),
    ("access.view", "See users, groups and roles"),
    ("access.manage", "Create, edit, unlock and delete users; create groups and give them roles"),
    ("logs.view", "See and export the logs"),
]

VIEWER = {"dashboard.view", "attendances.view", "attendances.export"}
OPERATOR = VIEWER | {"collaborators.view", "collaborators.edit", "companies.view", "companies.edit"}
TECHNICIAN = {"dashboard.view", "attendances.view", "relays.manage", "settings.general", "auth.manage",
              "access.view"}

# role -> (label, description, permissions), lowest first
ROLES = {
    "viewer": ("Viewer", "Sees the dashboard and attendances, and can export attendances.", VIEWER),
    "operator": ("Operator", "Viewer, plus sees and updates collaborators and companies.", OPERATOR),
    "technician": ("Technician", "Sees the dashboard and attendances, manages relays, changes the general and "
                   "authentication settings, and sees (without changing) users, groups and roles.", TECHNICIAN),
    "administrator": ("Administrator", "Everything, including users, groups, roles and the logs.",
                      {p for p, _ in PERMISSIONS}),
}
ORDER = list(ROLES)
_ALL = {name: frozenset(ROLES[name][2]) for name in ORDER}


def permissions(role):
    return _ALL.get(role, frozenset())


def label(role):
    return ROLES[role][0] if role in ROLES else "No role"


def rank(role):
    return ORDER.index(role) if role in ROLES else -1


def highest(roles):
    roles = [r for r in roles if r in ROLES]
    return max(roles, key=rank) if roles else None


def choices():
    return [(name, ROLES[name][0]) for name in ORDER]
