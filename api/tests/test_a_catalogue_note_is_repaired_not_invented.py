"""What may be rewritten in a live catalogue row, and what may not.

The note is what a requester reads when choosing what to ask for, and what an
approver reads when deciding -- so a tool that rewrites notes in bulk is a tool
that can quietly change what people are told. Its decision half is pure for that
reason: what would change can be read here, without a database.

THE REPAIR IT EXISTS FOR. Notes written by `batch_prove --offer` before deee427
embedded a whole certification sentence in the proof-reference slot, which
pushed the image name past String(300) and cut it mid-word. `_upsert_by` inserts
only, so re-seeding could never correct a row that already existed.
"""

from __future__ import annotations

from ops.refresh_catalogue_notes import changes

BROKEN = ("software",
          "Memcached on a machine of its own, listening on 11211. Proved by "
          "Certified automatically by proof build PROOF-MEMCACHED-…: built, "
          "verified healthy and destroyed.: the portal built it, the machine "
          "reported it serving, and the machine was destroyed. Runs "
          "docker.io/library/memcache")
FIXED = ("software",
         "Memcached on a machine of its own, run from the official container "
         "image, listening on 11211.")


def test_a_drifted_note_is_offered_for_repair():
    out = changes({"memcached": BROKEN}, {"memcached": FIXED})

    assert len(out) == 1
    assert out[0]["code"] == "memcached"
    assert out[0]["now"] == FIXED[1]


def test_a_note_that_already_matches_is_left_alone():
    """A no-op must be a no-op: rewriting every row on every run would bury the
    one that actually changed in an audit entry nobody can read."""
    assert changes({"memcached": FIXED}, {"memcached": FIXED}) == []


def test_the_old_text_is_carried_so_the_change_can_be_read():
    """"Was" and "now" both, because an audit entry saying a note changed
    without saying from what is an entry nobody can check."""
    out = changes({"memcached": BROKEN}, {"memcached": FIXED})

    assert out[0]["was"] == BROKEN[1]


def test_a_delivery_model_difference_is_reported_and_not_acted_on():
    """THE LINE THIS TOOL DOES NOT CROSS. Regrouping a technology changes where
    it appears on the request form; that is a catalogue decision, not a typo
    fix, and a bulk tool must not make it in passing."""
    out = changes({"minio": ("managed", "old")}, {"minio": ("software", "new")})

    assert out[0]["model_differs"] is True
    assert out[0]["live_model"] == "managed"
    assert out[0]["seed_model"] == "software"
    assert "now" in out[0] and "software" not in out[0]["now"]


def test_a_technology_the_seed_does_not_know_is_not_touched():
    """The seed is reviewed text, not an instruction to overwrite whatever a
    later batch certified. A live row with no seed entry is left exactly as it
    is rather than being blanked."""
    assert changes({"sonarqube": ("software", "listed last night")}, {}) == []


def test_a_seeded_technology_that_is_not_live_is_not_invented():
    """Nothing here creates a listing. A technology reaches the catalogue by
    being proved, never by appearing in a table."""
    assert changes({}, {"cassandra": FIXED}) == []


def test_naming_a_code_limits_it_to_that_code():
    """Surgical by default, the lesson from withdrawing refutations: a blanket
    run would have taken a sound row with it."""
    live = {"memcached": BROKEN, "grafana": ("software", "wrong")}
    seed = {"memcached": FIXED, "grafana": ("software", "right")}

    out = changes(live, seed, codes=["memcached"])

    assert [c["code"] for c in out] == ["memcached"]


def test_codes_are_matched_however_they_were_typed():
    out = changes({"memcached": BROKEN}, {"memcached": FIXED},
                  codes=[" MEMCACHED ", ""])

    assert [c["code"] for c in out] == ["memcached"]


def test_asking_for_everything_finds_every_drifted_row():
    live = {"memcached": BROKEN, "grafana": ("software", "wrong"),
            "nginx": ("software", "same")}
    seed = {"memcached": FIXED, "grafana": ("software", "right"),
            "nginx": ("software", "same")}

    assert sorted(c["code"] for c in changes(live, seed)) == ["grafana", "memcached"]
