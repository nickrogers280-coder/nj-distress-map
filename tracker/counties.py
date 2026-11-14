"""New Jersey counties that publish sheriff sale listings on CivilView (salesweb.civilview.com).

County IDs were verified against the live site on 2026-11-13. Ocean County (85) is on
CivilView but its listing had not been updated since February 2026, so it is disabled by
default. Mercer, Somerset, Sussex and Warren publish elsewhere and need their own parsers
(Phase 2).
"""

CIVILVIEW_NJ = {
    1: "Camden",
    2: "Essex",
    3: "Burlington",
    6: "Cumberland",
    7: "Bergen",
    8: "Monmouth",
    9: "Morris",
    10: "Hudson",
    15: "Union",
    17: "Passaic",
    19: "Gloucester",
    20: "Salem",
    25: "Atlantic",
    32: "Hunterdon",
    52: "Cape May",
    73: "Middlesex",
    85: "Ocean",  # stale on CivilView as of 2026-11; off by default
}

DEFAULT_ENABLED = [cid for cid in CIVILVIEW_NJ if cid != 85]

NOT_ON_CIVILVIEW = ["Mercer", "Ocean (current list)", "Somerset", "Sussex", "Warren"]
