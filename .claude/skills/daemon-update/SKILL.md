---
name: daemon-update
description: Adopt a new openccu-loom daemon release in this client — merge the automatic regeneration PR, then implement the API changes in the hand-written layer and cut the next 2026.M.N version. Use when a daemon release lands, a regeneration PR is open, or the user asks to update the client to a new daemon/api version.
---

# Adopting a daemon release

A daemon release dispatches `daemon-release` here
(`openccu-loom/.github/workflows/release.yml`), and
`regenerate-on-daemon-release.yml` opens a PR titled
**"Regenerate wire bindings from openccu-loom vX.Y.Z"**. It stops there on
purpose: no version bump, no auto-merge, no tag. Whether a regeneration is
worth a release is a decision.

So this is two PRs, and the second is where the work is.

## 0. Fix the environment before trusting any gate

`requirements.txt` pins `aiohomematic` exactly; a stale venv reports failures
that are not in the code. This has cost a full detour once already — a `mypy`
error in `compat/.../adapter.py` and a collection error in the compat parity
test, both purely the wrong pin.

```sh
source venv/bin/activate
pip install -r requirements.txt        # or at least the aiohomematic pin
```

A local failure that CI does not reproduce is the environment until proven
otherwise.

## 1. The regeneration PR — review, then merge

`wire/` is generated and never hand-edited, so the diff is the daemon's
contract movement, not a proposal. Read it for the two things that reach
outside `wire/`:

- **Removed models.** Grep the whole package and tests for each one. If nothing
  outside `wire/` names it, the removal is free.
- **New enum members.** Look for an exhaustive mapping over that enum in the
  hand-written layer — a new member turns a total match into a partial one.
  Absence of such a mapping is the answer, but check it rather than assume it.

Both clear → merge. Its own CI covers the rest.

## 2. The version number

`YYYY.M.<counter>` — **not** a date. The counter runs within the month and
restarts at 1 in a new one: `2026.8.38` is followed by `2026.8.39` in August
and by `2026.9.1` in September. It lives in `openccu_loom_client/const.py`
(`VERSION`); `wire/const.py` carries a separate `VERSION` for the generated
schema — leave that one alone.

## 3. The follow-up PR — what the regeneration cannot do

The generator moves types. It cannot notice that a hand-written method now
throws away information the daemon started sending. That is the work, and the
question to ask of every changed endpoint is:

> Did a response gain a field, and does the method that calls it still return
> `None`?

Then the harder second question, because it is where the real defect hides:

> Does any caller cache what it just wrote?

A write path that refreshes its cache from the value it _submitted_ was
correct only while the daemon never stored anything else. The moment the
daemon starts correcting, that cache describes a schedule the device does not
hold — and every public read is served from it. The 2026.9.1 update found
exactly this in two compat write paths.

Two habits for the reconciliation code:

- **Do not guess at unresolvable coordinates.** A correction naming a key the
  local copy does not have means the two disagree about the payload's shape.
  Skipping leaves that visible on the next read; inventing a target hides it.
- **Say what an empty answer does not mean.** An absent list can mean "the
  daemon reported nothing" _and_ "this daemon cannot report" — an older daemon
  answers with no body at all. If the two are indistinguishable at that layer,
  the docstring says so.

## 4. Gates, in this order

```sh
source venv/bin/activate
ruff format . && ruff check .
mypy openccu_loom_client
pylint openccu_loom_client
python script/lint_kwonly.py openccu_loom_client   # all public params keyword-only
python script/lint_all_exports.py
python -m pytest -q
prek run --all-files                                # LAST — see below
```

Run `prek` last and locally: it is the only gate that catches `prettier`, and
`prettier` rewrites markdown emphasis to `_this_`. A changelog entry written
with `*this*` turns the PR red on a one-character difference.

## 5. Prove the guard bites

For each behaviour added, put the old line back and watch the test fail with
its message — and check that the negative-control test stays **green**. A
correction test that fails when nothing was corrected is measuring the code
path, not the correction.
