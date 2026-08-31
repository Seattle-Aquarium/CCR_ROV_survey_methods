"""Default contact tables for the PDF.

Transcribed from the current Word flight plan. This is a placeholder the team
will revise -- a later version loads it from a TOML/JSON file passed on the CLI.
"""
from __future__ import annotations

AQUARIUM = [
    {"name": "Joel Hollander", "role": "Seattle Aquarium", "email": "j.hollander@seattleaquarium.org", "cell": "(206) 714-8968"},
    {"name": "Shawn Larson", "role": "Seattle Aquarium", "email": "s.larson@seattleaquarium.org", "cell": "(206) 618-3762"},
    {"name": "Zachary Randell", "role": "Seattle Aquarium", "email": "z.randell@seattleaquarium.org", "cell": "(408) 660-7842"},
    {"name": "Megan Williams", "role": "Seattle Aquarium", "email": "m.williams@seattleaquarium.org", "cell": "(206) 356-9850"},
    {"name": "Jessie Miles", "role": "Seattle Aquarium", "email": "j.miles@seattleaquarium.org", "cell": "(206) 799-7734"},
    {"name": "Alex Tanz", "role": "Seattle Aquarium", "email": "a.tanz@seattleaquarium.org", "cell": "(312) 730-4842"},
]

COLLABORATORS = [
    {"name": "Matt Castle", "role": "Samish DNR — Field Manager", "email": "mcastle@samishtribe.nsn.us", "cell": "(360) 202-4591"},
    {"name": "Jodi Bluhm", "role": "Samish DNR — Senior Director", "email": "jbluhm@samishtribe.nsn.us", "cell": ""},
    {"name": "Kathleen Hurley", "role": "Port of Seattle", "email": "hurley.k@portseattle.org", "cell": "(206) 399-2974"},
    {"name": "Jon Scordino", "role": "Makah Fisheries Management", "email": "jonathan.scordino@makah.com", "cell": "(360) 640-0959"},
    {"name": "Will Jasper", "role": "Makah Fisheries Management", "email": "william.jasper@makah.com", "cell": "(360) 640-1662"},
    {"name": "Jennifer Hagen", "role": "Quileute Natural Resources", "email": "jennifer.hagen@quileutetribe.com", "cell": "(360) 640-4430"},
]

EMERGENCY = [
    {"name": "U.S. Coast Guard", "role": "EMS · VHF 16", "email": "", "cell": "(206) 718-5222"},
    {"name": "King County Sheriff", "role": "Marine unit", "email": "", "cell": "(206) 296-4155"},
    {"name": "Virginia Mason Hospital", "role": "Emergency dept.", "email": "", "cell": "(206) 583-6543"},
    {"name": "Seattle Harbor Patrol", "role": "Elliott Bay", "email": "", "cell": "(206) 684-4072"},
]


def default_contacts() -> dict[str, list[dict]]:
    return {"aquarium": AQUARIUM, "collaborators": COLLABORATORS, "emergency": EMERGENCY}
