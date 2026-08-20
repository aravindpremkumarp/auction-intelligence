"""
api/chat/scope_keys.py
----------------------
The single definition of "which search arguments are conversation scope".

Lives in its own module because three places need it and none of them should
own it: the v1 router (`_extract_active_filters` walks the pydantic-ai message
history), the v2 tiered loop (`api/chat/v2/scope.py` merges the scope object
into every `search_auctions` call), and the conversation eval
(`evals/conversations.py`, which keeps a dependency-free mirror plus a drift
test).
"""
from __future__ import annotations

# Args that describe scope we want to carry across turns — every filter that
# narrows the user's target set. Excludes output controls (limit, order_by)
# and aggregate/grouping knobs (they shape the current call, not the scope),
# and `include_past` (a one-off retrospective retry shouldn't stick to the
# whole conversation).
#
# Keep this in sync when `search_auctions` grows a filter: a key missing here
# means the "Active search scope" block silently drops that scope on follow-up
# turns. That has bitten us once already, when borrower / EMD / platform /
# is_reauction were added without updating the set — which is exactly why the
# definition is now in one place with a drift test pointed at it.
CARRY_FORWARD_FILTER_KEYS = {
    "min_price", "max_price",
    "min_emd", "max_emd",
    "city", "area",
    "property_type", "asset_category",
    "bank", "borrower",
    "auction_type", "branch_name",
    "service_provider",
    "is_reauction",
    "starts_after", "starts_before",
    "deadline_within_days",
}
