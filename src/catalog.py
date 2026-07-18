"""
Static enrichment for KIDA NYC services & staff, derived from recon
(docs/timely-api.md).

The availability API returns service ids, staff ids, and times — but NOT prices or
deposit terms. This module supplies those. The fetcher still parses the live service
catalog for the authoritative name↔staff mapping and warns if it meets a service id
that is missing here (so a menu change is loud, not silent).

Deposit flags follow the salon's stated policy (cuts/colour/chemical services take a
50% deposit). They are a hint only — every event description also carries the honest
"confirm on KIDA's site" caveat, since deposits are only shown at the payment step
we never reach.
"""

# staff_id -> display name
STAFF = {
    "287218": "Masa",
    "367833": "Satomi",
    "175308": "Nao",
    # Master barbers — real names are Sachi / Taka (Aki) / Yohei / Fausto; the exact
    # id↔name pairing isn't pinned, so they show generically as "Master Barber".
    "24102": "Master Barber",
    "173532": "Master Barber",
    "24107": "Master Barber",
    "24105": "Master Barber",
    "24104": "Master Barber",
    "686999": "Hiroki",
}

# staff_id -> role label
STAFF_ROLE = {
    "287218": "Stylist", "367833": "Stylist", "175308": "Stylist",
    "24102": "Master Barber", "173532": "Master Barber", "24107": "Master Barber",
    "24105": "Master Barber", "24104": "Master Barber",
    "686999": "Junior Barber",
}

# service_id -> enrichment
SERVICES = {
    # Hair Salon
    "5319865": {"category": "Hair Salon",   "price_display": "Varies (from $75)", "deposit_required": True},
    "5319877": {"category": "Hair Salon",   "price_display": "Varies",            "deposit_required": False},
    "71794":   {"category": "Hair Salon",   "price_display": "Varies",            "deposit_required": True},
    "71795":   {"category": "Hair Salon",   "price_display": "Varies",            "deposit_required": True},
    "72465":   {"category": "Hair Salon",   "price_display": "Varies",            "deposit_required": True},
    "72464":   {"category": "Hair Salon",   "price_display": "Varies",            "deposit_required": True},
    "72466":   {"category": "Hair Salon",   "price_display": "Varies",            "deposit_required": True},
    "245259":  {"category": "Hair Salon",   "price_display": "Varies",            "deposit_required": True},
    "72467":   {"category": "Hair Salon",   "price_display": "Varies",            "deposit_required": False},
    # Barber Shop / Master Barber
    "71521":   {"category": "Master Barber", "price_display": "$60",  "deposit_required": False},
    "71522":   {"category": "Master Barber", "price_display": "$50",  "deposit_required": False},
    "71523":   {"category": "Master Barber", "price_display": "$50",  "deposit_required": False},
    "71525":   {"category": "Master Barber", "price_display": "$60",  "deposit_required": False},
    "71527":   {"category": "Master Barber", "price_display": "$40",  "deposit_required": False},
    "71528":   {"category": "Master Barber", "price_display": "$60",  "deposit_required": False},
    "3142646": {"category": "Master Barber", "price_display": "$95",  "deposit_required": False},
    "3142652": {"category": "Master Barber", "price_display": "$85",  "deposit_required": False},
    "71529":   {"category": "Master Barber", "price_display": "$115", "deposit_required": False},
    # Barber Shop / Junior Barber (Hiroki)
    "5319831": {"category": "Junior Barber", "price_display": "$50", "deposit_required": False},
    "5319834": {"category": "Junior Barber", "price_display": "$40", "deposit_required": False},
    "5319836": {"category": "Junior Barber", "price_display": "$40", "deposit_required": False},
    "5319837": {"category": "Junior Barber", "price_display": "$30", "deposit_required": False},
    "5319841": {"category": "Junior Barber", "price_display": "$75", "deposit_required": False},
    "5319844": {"category": "Junior Barber", "price_display": "$65", "deposit_required": False},
}


def staff_name(staff_id: str) -> str:
    return STAFF.get(str(staff_id), f"Staff {staff_id}")


def staff_role(staff_id: str) -> str:
    return STAFF_ROLE.get(str(staff_id), "")


def service_meta(service_id: str) -> dict:
    return SERVICES.get(str(service_id), {"category": "", "price_display": "", "deposit_required": False})
