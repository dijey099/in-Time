"""Built-in roles of the admin console and what each one may do.

Roles are ordered: each one can do everything the previous one can, plus more. A user's role is the
highest of their own role and the roles of their groups (see accounts.effective_role).
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
    ("relays.manage", "Manage relays (create, edit, tokens, site areas)"),
    ("settings.general", "Change the general settings (max in / min out times)"),
    ("access.manage", "Manage authentication, users, groups and roles"),
    ("logs.view", "See and export the logs"),
]

# role -> (label, description, permissions), lowest first
ROLES = {
    "viewer": ("Viewer", "Sees the dashboard and attendances, and can export attendances.",
               {"dashboard.view", "attendances.view", "attendances.export"}),
    "operator": ("Operator", "Viewer, plus sees and updates collaborators and companies.",
                 {"collaborators.view", "collaborators.edit", "companies.view", "companies.edit"}),
    "technician": ("Technician", "Operator, plus manages relays and the general settings.",
                   {"relays.manage", "settings.general"}),
    "administrator": ("Administrator", "Everything: Technician, plus authentication, users, groups, roles "
                      "and the logs.", {"access.manage", "logs.view"}),
}
ORDER = list(ROLES)

# Each role includes the permissions of the roles below it
_ALL = {}
_acc = set()
for _name in ORDER:
    _acc = _acc | ROLES[_name][2]
    _ALL[_name] = frozenset(_acc)


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
