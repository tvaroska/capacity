"""Envoy AI Gateway `AIGatewayRoute` CEL and `BackendTrafficPolicy` generator (`ADR-007`)."""

from __future__ import annotations

import yaml

from capacity.contract.common import PACKAGE_VERSION
from capacity.gateway.gke import k8s_dns_label

# Pre-SSE request reservation in Envoy Global Rate Limit before streaming completes (ADR-007).
ENVOY_PREFLIGHT_COST_TOKENS = 256


class _LiteralBlockStr(str):
    """YAML string marker serialized using `|` block style for readable CEL expressions."""


def _literal_str_representer(dumper: yaml.SafeDumper, data: _LiteralBlockStr) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


class _GatewayYamlDumper(yaml.SafeDumper):
    pass


_GatewayYamlDumper.add_representer(_LiteralBlockStr, _literal_str_representer)


def build_envoy_yaml(*, norm: str, envoy_cel_expr: str, per_bu_funded_tpm: int) -> str:
    envoy_route_name = k8s_dns_label(norm, suffix="-weighted-burndown-route")
    envoy_backend_svc = k8s_dns_label(norm, prefix="vertex-", suffix="-backend")
    envoy_rl_name = k8s_dns_label(norm, suffix="-burndown-ratelimit")
    envoy_docs = [
        {
            "apiVersion": "aigateway.envoyproxy.io/v1alpha1",
            "kind": "AIGatewayRoute",
            "metadata": {
                "name": envoy_route_name,
                "namespace": "ai-gateway",
            },
            "spec": {
                "targetRefs": [
                    {
                        "group": "gateway.networking.k8s.io",
                        "kind": "Gateway",
                        "name": "central-ai-gateway",
                    }
                ],
                "rules": [
                    {
                        "matches": [
                            {
                                "headers": [
                                    {
                                        "type": "Exact",
                                        "name": "x-ai-eg-model",
                                        "value": norm,
                                    }
                                ]
                            }
                        ],
                        "backendRefs": [
                            {
                                "name": envoy_backend_svc,
                                "weight": 100,
                            }
                        ],
                        "llmRequestCosts": [
                            {"metadataKey": "llm_input_token", "type": "InputToken"},
                            {"metadataKey": "llm_cached_input_token", "type": "CachedInputToken"},
                            {"metadataKey": "llm_output_token", "type": "OutputToken"},
                            {
                                "metadataKey": "vertex_burndown_tokens",
                                "type": "CEL",
                                "cel": _LiteralBlockStr(envoy_cel_expr),
                            },
                        ],
                    }
                ],
            },
        },
        {
            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
            "kind": "BackendTrafficPolicy",
            "metadata": {
                "name": envoy_rl_name,
                "namespace": "ai-gateway",
            },
            "spec": {
                "targetRefs": [
                    {
                        "group": "gateway.networking.k8s.io",
                        "kind": "Gateway",
                        "name": "central-ai-gateway",
                    }
                ],
                "rateLimit": {
                    "type": "Global",
                    "global": {
                        "rules": [
                            {
                                "clientSelectors": [
                                    {
                                        "headers": [
                                            {
                                                "name": "x-business-unit",
                                                "type": "Distinct",
                                            }
                                        ]
                                    }
                                ],
                                "cost": {
                                    "request": {
                                        "from": "Number",
                                        "number": ENVOY_PREFLIGHT_COST_TOKENS,
                                    },
                                    "response": {
                                        "from": "Metadata",
                                        "metadata": {
                                            "namespace": "io.envoy.ai_gateway",
                                            "key": "vertex_burndown_tokens",
                                        },
                                    },
                                },
                                "limit": {
                                    "requests": per_bu_funded_tpm,
                                    "unit": "Minute",
                                },
                            }
                        ]
                    },
                },
            },
        },
    ]
    envoy_header = (
        f"# Envoy AI Gateway Declarative Weighted Burndown Config (capacity v{PACKAGE_VERSION})\n"
    )
    return envoy_header + yaml.dump_all(
        envoy_docs, Dumper=_GatewayYamlDumper, sort_keys=False, default_flow_style=False
    )
