# AIDrama Studio V1 · 60-second real E2E acceptance fixture

This directory is a self-contained, deterministic preparation fixture for the
first paid AIDrama Studio short-drama run. It is intentionally isolated from
provider adapters, readiness, Settings, runtime, production orchestration,
database, migrations, desktop launcher, and build-system code.

## Canonical inputs

`acceptance_manifest.json` is the entry point. It pins the creative brief,
Story Bible, Structured Script, canonical domain-compatible Shot Plan, the
per-shot acceptance requirements, subtitle track, TTS cue plan, final assembly
order, and output profile. All paths in the manifest are relative to this
directory and all durations are in seconds.

The domain-compatible `shot_plan.json` deliberately contains only fields
accepted by `aidrama_studio.domain.shot.ShotPlan`. The companion
`shot_acceptance_requirements.json` carries the operational fields that are
needed by a human or Vision QC pass (reference requirements, generation brief,
continuity, deterministic QC, Vision QC, and human acceptance criteria). Join
the two files by `shot_id`.

## Determinism and provider policy

- `live_calls` and `paid_calls` are both zero for this fixture.
- No API key, provider URL, signed URL, token, or external asset is required
  to validate the fixture.
- Stable IDs, integer shot durations, explicit timeline boundaries, fixed
  render parameters, and checked-in text references make the acceptance run
  reproducible.
- The reference requirements are locked briefs, not generated images. Before a
  paid run, a human may attach compliant local character/location images to the
  named slots without changing the story, script, shots, or checksums.

## Running the fixture checks

From the repository root:

```text
python -m pytest test/aidrama_studio/acceptance/aidrama_v1_60s_real_e2e/test_fixture_validation.py -q
```

The test is offline and validates Pydantic Story Bible, Structured Script, and
Shot Plan models, exact 60.000-second timing, subtitle/TTS alignment, the
12-shot final order, output profile, and the no-provider/no-secret policy.

## Intended first real E2E use

1. Approve `story_bible.json` and `structured_script.json` unchanged.
2. Lock the two character and two location references described in
   `reference_requirements.json`.
3. Generate each shot from `shot_plan.json` plus its matching acceptance
   requirements using the configured live providers; retain the exact briefs
   and IDs for provenance.
4. Run deterministic QC and Vision QC against the expectations in
   `shot_acceptance_requirements.json`.
5. Assemble in the exact order and timeline in `final_assembly.json`, then
   deliver using `output_profile.json`.
6. Record every assertion in `acceptance_checklist.md`; any failed human
   criterion is a fail, even if automated checks pass.
