# notes — engineering working documents

**This repository publishes no documentation site.** `README.md` is the
package's public face; everything else written for contributors lives here.

The name mirrors the daemon repository, where `notes/` is the working set and
`docs/` is the published MkDocs site. Keeping the same name in both means a
reader moving between them never has to work out which convention applies —
in `openccu-loom-client` there simply is no published half.

## What lives here

| File                                               | Purpose                                                                                                 |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| [`open-work.md`](./open-work.md)                   | The single backlog: what is still open, and the decisions recorded so they are not re-proposed.         |
| [`reconnect-recovery.md`](./reconnect-recovery.md) | The reconnect / recovery scenario matrix: what the client does per failure mode, and where it does not. |

The backlog is deliberately one file. This repository previously carried two
overlapping backlogs (`todo.md` and `docs/optimization-needs.md`) plus three
executed implementation plans, and the overlap was the problem: an item could
be open in one and closed in the other, and nothing said which to trust.

## What does not live here

- **Release history** — `changelog.md`.
- **Architecture and conventions** — `CLAUDE.md`.
- **Executed implementation plans.** Once a plan ships, the code and the
  changelog carry it; the plan becomes a document that describes work nobody
  can do again. Delete it — git history is the archive.
- **Point-in-time scorecards.** A graded review measured against a daemon
  version that is forty releases old reads as a current assessment and is not
  one. Record what a review _decided_ in `CLAUDE.md`; let the grades go.

## Cross-repo references

Link the daemon's published pages by URL, not by repo-relative path:

```markdown
[topic hierarchy](https://github.com/SukramJ/openccu-loom/blob/main/docs/external-clients/topic-hierarchy.md)
```

A path like `docs/parity/ha-client-wire-gaps.md` only reads correctly from
inside that repository, and breaks silently when the daemon reorganises —
which is exactly what happened to three references here.
