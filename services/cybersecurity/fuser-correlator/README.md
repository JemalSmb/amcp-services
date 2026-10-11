# fuser-correlator

**Domain:** cybersecurity
**Kind:** passive
**Status:** not implemented — structure stub only

Event correlation and attack chain detection.

## Why this is a stub

This service is named in `amcp-platform-core/domains/cybersecurity/domain.yaml`
but has no trained model or working logic yet. `threat-detection`,
`phishing-detector` (cybersecurity), `fraud-detector` (finance), and
`process-anomaly-detector` (manufacturing) are the only services in this
repo currently wired end-to-end (NATS in -> inference call -> NATS out) —
use those as the reference implementation.

## To implement this service

1. Train and export a model for `correlation_model` in `amcp-ai-engine`,
   register it in the inference service's `SUPPORTED_MODELS`.
2. Replace `service.py` with a real `DomainMicroservice(...)` instance
   (see `services/cybersecurity/threat-detection/service.py` for the
   pattern) pointing at that model and this domain's NATS subjects.
3. Wire it into `docker-compose.services.yaml`.
