"""GKE Inference Gateway (`epp`) manifest builder and RFC-1035 DNS label sanitizer (`ADR-007`)."""

from __future__ import annotations

import hashlib
import re

import yaml


def k8s_dns_label(raw: str, *, prefix: str = "", suffix: str = "") -> str:
    """Build a valid Kubernetes RFC-1035 DNS label (`<= 63` chars, `[a-z0-9-]`, no dots)."""
    stem = re.sub(r"[^a-z0-9-]+", "-", raw.strip().lower())
    stem = re.sub(r"-{2,}", "-", stem).strip("-") or "model"
    if not prefix and not stem[0].isalpha():
        stem = f"m-{stem}"
    budget = 63 - len(prefix) - len(suffix)
    if len(stem) > budget:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
        keep = max(1, budget - 7)
        stem = f"{stem[:keep].rstrip('-')}-{digest}"
    label = f"{prefix}{stem}{suffix}".strip("-")
    if not label[0].isalpha():
        label = f"k-{label}"[:63].rstrip("-")
    return label


def build_gke_epp_yaml(self_hosted_model: str) -> str:
    pool_name = k8s_dns_label(self_hosted_model, suffix="-pool")
    app_label = k8s_dns_label(self_hosted_model, prefix="vllm-")
    epp_cfg_name = k8s_dns_label(self_hosted_model, suffix="-epp-config")
    obj_name = k8s_dns_label(self_hosted_model, suffix="-interactive-critical")

    epp_docs = [
        {
            "apiVersion": "inference.networking.x-k8s.io/v1alpha2",
            "kind": "InferencePool",
            "metadata": {"name": pool_name, "namespace": "ai-serving"},
            "spec": {
                "targetPortNumber": 8000,
                "selector": {"app": app_label},
                "extensionRef": {"name": epp_cfg_name},
            },
        },
        {
            "apiVersion": "inference.networking.x-k8s.io/v1alpha2",
            "kind": "InferenceObjective",
            "metadata": {"name": obj_name, "namespace": "ai-serving"},
            "spec": {"priority": "Critical", "poolRef": {"name": pool_name}},
        },
        {
            "apiVersion": "inference.networking.x-k8s.io/v1alpha2",
            "kind": "EndpointPickerConfig",
            "metadata": {"name": epp_cfg_name, "namespace": "ai-serving"},
            "spec": {
                "scorers": [
                    {"type": "prefix-cache-scorer", "weight": 4, "blockSizeTokens": 64},
                    {
                        "type": "kv-cache-utilization-scorer",
                        "weight": 2,
                        "targetUtilizationPct": 80,
                    },
                    {"type": "queue-scorer", "weight": 2, "maxWaitingRequests": 10},
                    {"type": "lora-affinity-scorer", "weight": 1},
                ]
            },
        },
    ]
    epp_header = "# GKE Inference Gateway / K8s Gateway API Inference Extension (epp / llm-d)\n"
    return epp_header + yaml.safe_dump_all(epp_docs, sort_keys=False, default_flow_style=False)
