"""Tests for skills/check-cfps/scripts/prepare-sessionize-batch.py.

Contract (Step 4 batch prep):
  - Routes entries by effective source — explicit `source`, else the
    `cfp_url` host inference shared with backfill-source.py.
  - Only `sessionize-speaker-api` entries are batched; the slug is the
    new candidate's own slug, else the first `cfp_url` path segment.
  - Sessionize entries with no derivable slug are reported unverifiable.
  - Emits a deduped slug list plus join tables; pure stdin->stdout JSON.
"""

from __future__ import annotations

import io
import json


def _run(module, monkeypatch, capsys, stdin: str):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = module.main([])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_explicit_sessionize_source_is_batched_with_own_slug(
    prepare_sessionize_batch,
):
    out = prepare_sessionize_batch.prepare(
        [
            {
                "id": "devoxx-be-2026",
                "cohort": "new",
                "source": "sessionize-speaker-api",
                "slug": "devoxx-be-2026",
                "cfp_url": "https://sessionize.com/devoxx-be-2026/",
            }
        ]
    )
    assert out["slugs"] == ["devoxx-be-2026"]
    assert out["sessionize"] == [
        {"id": "devoxx-be-2026", "cohort": "new", "slug": "devoxx-be-2026"}
    ]
    assert out["non_sessionize"] == []
    assert out["unverifiable"] == []


def test_no_source_but_sessionize_host_is_inferred_and_slug_derived(
    prepare_sessionize_batch,
):
    # Legacy unsourced row whose host infers to Sessionize must be batched
    # (the regression the first review round caught), with the slug taken
    # from the first cfp_url path segment, not the dict key.
    out = prepare_sessionize_batch.prepare(
        [
            {
                "id": "drifted-dict-key",
                "cohort": "stored",
                "cfp_url": "https://sessionize.com/jfokus-2026/",
            }
        ]
    )
    assert out["slugs"] == ["jfokus-2026"]
    assert out["sessionize"] == [
        {"id": "drifted-dict-key", "cohort": "stored", "slug": "jfokus-2026"}
    ]


def test_stored_entry_ignores_provided_slug_and_derives_from_cfp_url(
    prepare_sessionize_batch,
):
    # A stored row's dict key drifts from the URL slug. Even if the caller
    # passes that drifted key as `slug`, the stored cohort must derive the
    # slug from cfp_url's first path segment — otherwise the batch 404s,
    # the exact regression this PR removes.
    out = prepare_sessionize_batch.prepare(
        [
            {
                "id": "devoxx-belgium",
                "cohort": "stored",
                "slug": "devoxx-belgium",
                "cfp_url": "https://sessionize.com/devoxx-be-2026/",
            }
        ]
    )
    assert out["slugs"] == ["devoxx-be-2026"]
    assert out["sessionize"][0]["slug"] == "devoxx-be-2026"


def test_new_candidate_keeps_its_own_slug(prepare_sessionize_batch):
    out = prepare_sessionize_batch.prepare(
        [
            {
                "id": "jfokus-2026",
                "cohort": "new",
                "slug": "jfokus-2026",
                "cfp_url": "https://sessionize.com/something-else/",
            }
        ]
    )
    # New candidates' own slug is authoritative.
    assert out["slugs"] == ["jfokus-2026"]


def test_non_sessionize_host_routes_to_non_sessionize_branch(
    prepare_sessionize_batch,
):
    out = prepare_sessionize_batch.prepare(
        [
            {
                "id": "some-conf",
                "cohort": "stored",
                "cfp_url": "https://developers.events/some-conf",
            }
        ]
    )
    assert out["non_sessionize"] == ["some-conf"]
    assert out["sessionize"] == []
    assert out["slugs"] == []


def test_explicit_non_sessionize_source_is_not_batched(prepare_sessionize_batch):
    out = prepare_sessionize_batch.prepare(
        [
            {
                "id": "jc-conf",
                "cohort": "stored",
                "source": "javaconferences.org",
                "cfp_url": "https://sessionize.com/jc-conf/",
            }
        ]
    )
    # Explicit source wins over host inference: stays out of the batch.
    assert out["non_sessionize"] == ["jc-conf"]
    assert out["sessionize"] == []


def test_sessionize_entry_without_derivable_slug_is_unverifiable(
    prepare_sessionize_batch,
):
    out = prepare_sessionize_batch.prepare(
        [
            {
                "id": "no-url",
                "cohort": "stored",
                "source": "sessionize-speaker-api",
            },
            {
                "id": "bare-host",
                "cohort": "stored",
                "cfp_url": "https://sessionize.com/",
            },
        ]
    )
    # Unverifiable entries carry their cohort so apply-sessionize-results.py
    # can route them through the cohort-specific verify-failed protocol.
    assert sorted(out["unverifiable"], key=lambda e: e["id"]) == [
        {"id": "bare-host", "cohort": "stored"},
        {"id": "no-url", "cohort": "stored"},
    ]
    assert out["sessionize"] == []
    assert out["slugs"] == []


def test_scheme_less_sessionize_url_is_unverifiable_not_a_bogus_slug(
    prepare_sessionize_batch,
):
    # "sessionize.com/foo" (no scheme) lands entirely in urlparse().path,
    # so naive parsing would derive the host as the slug and 404. With an
    # explicit Sessionize source it must be classified unverifiable instead.
    out = prepare_sessionize_batch.prepare(
        [
            {
                "id": "schemeless",
                "cohort": "stored",
                "source": "sessionize-speaker-api",
                "cfp_url": "sessionize.com/foo",
            }
        ]
    )
    assert out["unverifiable"] == [{"id": "schemeless", "cohort": "stored"}]
    assert out["slugs"] == []
    assert out["sessionize"] == []


def test_duplicate_slugs_are_deduped_in_slug_list_but_kept_in_join_table(
    prepare_sessionize_batch,
):
    out = prepare_sessionize_batch.prepare(
        [
            {
                "id": "stored-key",
                "cohort": "stored",
                "source": "sessionize-speaker-api",
                "cfp_url": "https://sessionize.com/devoxx-be-2026/",
            },
            {
                "id": "devoxx-be-2026",
                "cohort": "new",
                "source": "sessionize-speaker-api",
                "slug": "devoxx-be-2026",
                "cfp_url": "https://sessionize.com/devoxx-be-2026/",
            },
        ]
    )
    assert out["slugs"] == ["devoxx-be-2026"]
    assert {e["id"] for e in out["sessionize"]} == {"stored-key", "devoxx-be-2026"}
    assert out["counts"] == {
        "sessionize": 2,
        "non_sessionize": 0,
        "unverifiable": 0,
        "unique_slugs": 1,
    }


def test_main_emits_json_and_exits_zero(prepare_sessionize_batch, monkeypatch, capsys):
    code, out, _ = _run(
        prepare_sessionize_batch,
        monkeypatch,
        capsys,
        json.dumps(
            [
                {
                    "id": "x",
                    "cohort": "new",
                    "source": "sessionize-speaker-api",
                    "slug": "x",
                }
            ]
        ),
    )
    assert code == 0
    assert json.loads(out)["slugs"] == ["x"]


def test_main_rejects_invalid_json(prepare_sessionize_batch, monkeypatch, capsys):
    code, _, err = _run(prepare_sessionize_batch, monkeypatch, capsys, "{not json")
    assert code == 1
    assert "not valid JSON" in err


def test_main_rejects_non_array(prepare_sessionize_batch, monkeypatch, capsys):
    code, _, err = _run(prepare_sessionize_batch, monkeypatch, capsys, json.dumps({"id": "x"}))
    assert code == 1
    assert "JSON array" in err


def test_main_rejects_entry_without_id(prepare_sessionize_batch, monkeypatch, capsys):
    code, _, err = _run(
        prepare_sessionize_batch,
        monkeypatch,
        capsys,
        json.dumps([{"cohort": "new", "slug": "x"}]),
    )
    assert code == 1
    assert "`id`" in err


def test_main_rejects_invalid_cohort(prepare_sessionize_batch, monkeypatch, capsys):
    code, _, err = _run(
        prepare_sessionize_batch,
        monkeypatch,
        capsys,
        json.dumps([{"id": "x", "cohort": "maybe", "slug": "x"}]),
    )
    assert code == 1
    assert "cohort" in err
