"""Multi-gateway configuration bundle coordinator (`generate_gateway_bundle`)."""

from __future__ import annotations

from capacity.contract.requests import GatewayConfigRequest
from capacity.contract.results import GatewayConfigBundle
from capacity.engine.catalog import (
    ModelCatalog,
    build_envoy_burndown_cel,
    build_model_catalog,
    normalize_model_id,
    refill_window_for_gsus,
)
from capacity.gateway.envoy import build_envoy_yaml
from capacity.gateway.gke import build_gke_epp_yaml
from capacity.gateway.header_policies import GATEWAY_HEADER_POLICIES
from capacity.gateway.litellm import (
    LITELLM_PREFLIGHT_FLOOR_TOKENS,
    audit_litellm_scaling,
    build_litellm_yaml,
    render_burndown_callback,
)


def generate_gateway_bundle(
    req: GatewayConfigRequest,
    *,
    catalog: ModelCatalog | None = None,
) -> GatewayConfigBundle:
    """Generate production-hardened LiteLLM, Envoy AI Gateway, and GKE epp configurations."""
    cat = catalog if catalog is not None else build_model_catalog()
    record = cat.require(req.model)
    norm = normalize_model_id(req.model)
    spec = record.family_spec
    gsus = req.provisioned_gsus
    refill_window = refill_window_for_gsus(spec, gsus)
    committed_tpm = gsus * record.gsu_capacity_tpm
    per_bu_funded_tpm = max(
        LITELLM_PREFLIGHT_FLOOR_TOKENS, committed_tpm // req.business_unit_shares
    )

    litellm_audit = audit_litellm_scaling(
        provisioned_gsus=gsus,
        target_rps=req.target_rps,
        concurrent_sse_streams=req.concurrent_sse_streams,
        pods=req.pods,
        uvicorn_workers_per_pod=req.uvicorn_workers_per_pod,
        model=req.model,
        catalog=cat,
    )
    spill_shared_header = "priority" if spec.supports_priority_paygo else "standard"
    litellm_yaml = build_litellm_yaml(
        norm=norm,
        project_id=req.project_id,
        location=req.location,
        gsus=gsus,
        committed_tpm=committed_tpm,
        refill_window=refill_window,
        spill_shared_header=spill_shared_header,
        litellm_audit=litellm_audit,
    )
    callback_py = render_burndown_callback(
        {
            "MODEL_FAMILY": spec.family,
            "LONG_CONTEXT_THRESHOLD": spec.long_context_threshold,
            "STD_INPUT_WEIGHT": spec.standard_input_weight,
            "STD_CACHED_WEIGHT": spec.standard_cached_read_weight,
            "STD_OUTPUT_WEIGHT": spec.standard_output_weight,
            "CACHE_WRITE_5M_WEIGHT": spec.cache_write_5m_weight,
            "CACHE_WRITE_1H_WEIGHT": spec.cache_write_1h_weight,
            "LONG_INPUT_WEIGHT": spec.long_context_input_weight,
            "LONG_CACHED_WEIGHT": spec.long_context_cached_read_weight,
            "LONG_OUTPUT_WEIGHT": spec.long_context_output_weight,
            "SPILLOVER_SHARED_HEADER": spill_shared_header,
            "COMMITTED_POOL_BURNDOWN_TPM": committed_tpm,
            "DEFAULT_TEAM_FUNDED_BURNDOWN_TPM": per_bu_funded_tpm,
        }
    )
    envoy_yaml = build_envoy_yaml(
        norm=norm,
        envoy_cel_expr=build_envoy_burndown_cel(spec),
        per_bu_funded_tpm=per_bu_funded_tpm,
    )
    epp_yaml = build_gke_epp_yaml(req.self_hosted_model)

    return GatewayConfigBundle(
        model=req.model,
        normalized_model=norm,
        provisioned_gsus=gsus,
        refill_window_seconds=refill_window,
        committed_burndown_tpm=committed_tpm,
        header_policies=GATEWAY_HEADER_POLICIES,
        litellm_audit=litellm_audit,
        litellm_config_yaml=litellm_yaml,
        litellm_burndown_callback_py=callback_py,
        envoy_ai_gateway_yaml=envoy_yaml,
        gke_epp_inference_pool_yaml=epp_yaml,
    )
