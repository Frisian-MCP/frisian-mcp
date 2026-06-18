# Governance

- [Overview](#overview)
- [Roles](#roles)
- [Decision-Making](#decision-making)
- [Becoming a Maintainer](#becoming-a-maintainer)
- [Maintainer Removal and Emeritus Status](#maintainer-removal-and-emeritus-status)
- [Changes to This Document](#changes-to-this-document)
- [Code of Conduct](#code-of-conduct)

## Overview

This document describes how the `frisian-mcp` project is governed. The project
follows a lightweight, consensus-based model aligned with the Linux Foundation /
GitHub [Minimum Viable Governance (MVG)](https://github.com/github/MVG)
framework. The intent is to keep governance proportional to the project's
current size while providing a clear path to scale as the contributor base
grows.

The list of current maintainers is kept in [MAINTAINERS.md](MAINTAINERS.md).

## Roles

**Contributors** are anyone who submits an issue, pull request, documentation
change, or other form of participation. No formal status is required to
contribute. The contribution process is described in
[CONTRIBUTING.md](CONTRIBUTING.md).

**Maintainers** are responsible for the overall direction, code quality, and
health of the project. Maintainers review and merge pull requests, triage
issues, cut releases, and uphold the [Code of Conduct](CODE_OF_CONDUCT.md).
Maintainers act in the interest of the project and its users.

## Decision-Making

The project operates by **lazy consensus** among maintainers.

- Routine changes (bug fixes, documentation, dependency updates, tests) may be
  merged by any maintainer once the contribution requirements in
  CONTRIBUTING.md are met and continuous integration passes.
- Substantive changes (public API changes, new dependencies, changes to project
  direction, governance, or security posture) are opened as a pull request or
  issue and left open long enough for other maintainers to review. If no
  maintainer objects within a reasonable period, the change is considered
  approved by consensus.
- If a maintainer raises a concern, the change is not merged until the concern
  is resolved through discussion.
- Where consensus cannot be reached, the matter is decided by a simple majority
  vote of the current maintainers. In the event of a tie, the change does not
  proceed (status quo is preserved).

Discussion and decisions happen in the open, in GitHub issues and pull requests,
so the reasoning behind decisions is part of the project record.

## Becoming a Maintainer

Maintainership is earned through a sustained track record of quality
contributions and constructive participation in the community. There is no fixed
quota of contributions; what matters is demonstrated judgment, reliability, and
care for the project and its users.

An existing maintainer may nominate a contributor by opening a pull request that
adds the contributor to MAINTAINERS.md. The nomination is approved by consensus
of the current maintainers using the process above. New maintainers are listed
with their affiliation, consistent with the existing entries.

## Maintainer Removal and Emeritus Status

A maintainer may step down at any time by opening a pull request moving
themselves to the Emeritus section of MAINTAINERS.md.

A maintainer who becomes inactive for an extended period, or who acts contrary
to the interests of the project or the Code of Conduct, may be moved to Emeritus
status by consensus of the remaining maintainers. Emeritus maintainers are
recognized for their past contributions and may return to active status by the
same process used to add a new maintainer.

## Changes to This Document

Changes to this governance document follow the substantive-change process
described under [Decision-Making](#decision-making): they are proposed as a pull
request and approved by consensus of the current maintainers.

As the project grows — for example, if it joins a foundation, takes on a larger
maintainer pool, or begins holding funds — this model is designed to graduate to
a more formal structure (such as a technical steering committee) without a
disruptive re-founding, consistent with the MVG upgrade path.

## Code of Conduct

All participation in this project is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md). Maintainers are responsible for
enforcing it as described in that document.