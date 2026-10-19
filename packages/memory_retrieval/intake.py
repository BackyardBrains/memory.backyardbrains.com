"""Source identity and conservative routing, shared by live intake and backfill."""
import hashlib
import json
import re

VISIBILITIES = {"knowledge", "policy", "operational_audit", "automation_heartbeat"}
PROJECT_ALIASES = {"spike-recorder": "spikerecorder", "spike-recorder-app": "spikerecorder", "spike-recorder-spikerbot": "spikerecorder", "spikerbot": "spikerbot", "byb-shipping-app-shop-backyardbrains-com": "byb-shipping"}

def project_slug(value):
    if not value:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return PROJECT_ALIASES.get(slug, slug)

def classify(raw, source, source_type=None):
    if source_type in {"agent-profile", "agent-dashboard-status", "agent-dashboard-snapshot"}:
        return "operational_audit"
    if source == "cortex-board" or "canary" in source or source == "operational-closure-sync-test":
        return "operational_audit"
    if raw.lstrip().startswith(("[cortex-feedback", "Cortex feedback captured in Memory.", "remember status: BYB Shared Memory daily canary")):
        return "operational_audit"
    try:
        value = json.loads(raw)
        if isinstance(value, dict) and (value.get("kind") in {"agent-profile", "agent-dashboard-status", "agent-dashboard-snapshot"} or ("agent_id" in value and "blocks" in value)):
            return "operational_audit"
    except (ValueError, TypeError):
        pass
    if raw.lstrip().startswith(('[task-truth]', '[right-brain-steward]')):
        return 'operational_audit'
    # Imported documents and daily run receipts can quote coach tags without
    # themselves being standing instructions. Keep their evidence searchable.
    prefix = raw[:300].lower()
    if 'coach:watson' in prefix or ('[memory-protocol]' in prefix and 'standing order' in raw.lower()):
        return 'policy'
    return "knowledge"

def intake_metadata(raw, source, *, source_event_id=None, event_type=None, idempotency_key=None, source_path=None, observed_at=None, source_type=None, visibility=None, project=None, policy_scope=None):
    digest = hashlib.sha256(raw.encode()).hexdigest()
    match = re.search(r'\bevent_id="([^"\n]+)"', raw)
    event_id = source_event_id or (match.group(1) if match else None)
    prefix = re.search(r"\[(cortex-feedback[^\]]*)\]", raw)
    kind = event_type or (prefix.group(1) if prefix else source_type or "capture")
    if idempotency_key:
        identity = ["request", source, idempotency_key]
    elif event_id:
        identity = ["event", source, kind, event_id]
        # A rejected acknowledgement is an attempt on a parent event. Different
        # attempt text is distinct evidence; retries of that attempt are identical.
        if kind == 'cortex-feedback-ack-rejected':
            identity.append(digest)
    else:
        identity = ["content", source, source_path, str(observed_at or ""), digest]
    key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    tag = re.search(r"\[project:\s*([^\]]+)\]", raw, re.I)
    project = project_slug(project or (tag.group(1) if tag else None))
    visibility = visibility or classify(raw, source, source_type)
    if visibility not in VISIBILITIES:
        raise ValueError("Invalid memory_visibility")
    if policy_scope not in (None, "global", "project"):
        raise ValueError("policy_scope must be global or project")
    scope = policy_scope or ("project" if project else "global") if visibility == "policy" else None
    if scope == "project" and not project:
        raise ValueError("Project policy requires project_slug")
    return dict(content_hash=digest, ingestion_key=key, source_event_id=event_id,
                event_type=kind, memory_visibility=visibility, project_slug=project,
                policy_scope=scope)
