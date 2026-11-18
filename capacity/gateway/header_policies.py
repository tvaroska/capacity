"""Canonical 8 Vertex AI Provisioned Throughput & PayGo routing header policies (`ADR-007`)."""

from __future__ import annotations

from capacity.contract.results import GatewayHeaderPolicy

GATEWAY_HEADER_POLICIES: list[GatewayHeaderPolicy] = [
    GatewayHeaderPolicy(
        tier_id=1,
        scenario="Tier-0 / Tier-1 Prod (Within Team Funded PT Share)",
        request_type_header="<unset>",
        shared_request_type_header="priority",
        uta_rank="Rank 1 CRITICAL_PLUS -> Rank 2 CRITICAL",
        billing_and_capacity_behavior=(
            "Consumes Provisioned Throughput first (99.5% SLA); spills over automatically to "
            "Priority PayGo (1.8x rate) when the PT pool is 100% saturated."
        ),
    ),
    GatewayHeaderPolicy(
        tier_id=2,
        scenario="Tier-1 Prod (Team Exceeded Funded PT Share, Bursting)",
        request_type_header="shared",
        shared_request_type_header="priority",
        uta_rank="Rank 2 CRITICAL",
        billing_and_capacity_behavior=(
            "Bypasses PT completely (preventing a bursting team from starving peers' reserved "
            "GSUs) and routes directly to Priority PayGo (1.8x rate) billed to caller labels."
        ),
    ),
    GatewayHeaderPolicy(
        tier_id=3,
        scenario="Tier-2 Prod (Standard Interactive Traffic)",
        request_type_header="<unset> or shared",
        shared_request_type_header="standard",
        uta_rank="Rank 3/4 SHEDDABLE_PLUS",
        billing_and_capacity_behavior=(
            "Uses spare PT if unset (or bypasses PT if shared), spilling over to Standard "
            "PayGo (1.0x rate)."
        ),
    ),
    GatewayHeaderPolicy(
        tier_id=4,
        scenario="Strict Hard-Capped Prod ($0.00 PayGo Overage Allowed)",
        request_type_header="dedicated",
        shared_request_type_header="<ignored>",
        uta_rank="Rank 1 CRITICAL_PLUS only",
        billing_and_capacity_behavior=(
            "Consumes PT only; immediately returns HTTP 429 on exhaustion, guaranteeing $0.00 "
            "in variable PayGo overage charges."
        ),
    ),
    GatewayHeaderPolicy(
        tier_id=5,
        scenario="Daytime Non-Prod (Dev / UAT / Load Tests) & Daytime Eval",
        request_type_header="shared",
        shared_request_type_header="flex",
        uta_rank="Rank 6 SHEDDABLE_L",
        billing_and_capacity_behavior=(
            "Bypasses PT and Priority ceilings completely; routes to Flex PayGo (0.5x rate, "
            "50% discount) so non-prod never drains production GSUs."
        ),
    ),
    GatewayHeaderPolicy(
        tier_id=6,
        scenario="Off-Peak PT Scavenger Queue ($0.00 Marginal Cost Only)",
        request_type_header="dedicated",
        shared_request_type_header="<ignored>",
        uta_rank="Rank 1 CRITICAL_PLUS (spare headroom)",
        billing_and_capacity_behavior=(
            "Harvests idle overnight/weekend PT GSUs at $0.00 marginal cost via paced online "
            "calls; returns HTTP 429 for queue retry if interactive traffic spikes."
        ),
    ),
    GatewayHeaderPolicy(
        tier_id=7,
        scenario="Off-Peak Hybrid PT Scavenger + 0.5x Flex Spillover",
        request_type_header="<unset>",
        shared_request_type_header="flex",
        uta_rank="Rank 1 CRITICAL_PLUS -> Rank 6 SHEDDABLE_L",
        billing_and_capacity_behavior=(
            "Consumes spare PT GSUs first at $0.00 marginal cost with zero Flex delay; spills "
            "excess volume above the PT ceiling to 0.5x Flex PayGo."
        ),
    ),
    GatewayHeaderPolicy(
        tier_id=8,
        scenario="Pure Off-Peak / Batch PayGo (Projects Without PT)",
        request_type_header="shared",
        shared_request_type_header="offpeak",
        uta_rank="Rank 6 SHEDDABLE_L",
        billing_and_capacity_behavior=(
            "Bypasses PT and executes at 0.5x (50% discount) during off-peak windows "
            "(Mon-Thu 15:00-21:00 PT & Fri 15:00-Sun 21:00 PT)."
        ),
    ),
]
