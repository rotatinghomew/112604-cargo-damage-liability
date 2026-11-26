"""从样本目录载入合成资料，便于本地演示。"""

import json
from pathlib import Path


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def seed_from_samples(service, samples_dir):
    samples = Path(samples_dir)
    for record in _read_jsonl(samples / "evidence_register.jsonl"):
        service.add_evidence(record)
    for record in _read_jsonl(samples / "checks.jsonl"):
        service.add_check(record)
    for record in _read_jsonl(samples / "policies.jsonl"):
        service.add_policy(record)
    for record in _read_jsonl(samples / "reports.jsonl"):
        if record["incident"] not in service.state["incidents"]:
            service.confirm_incident(record["incident"], record["issuer"])
        service.add_report(record)
    return service
