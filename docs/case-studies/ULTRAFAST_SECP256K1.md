# UltrafastSecp256k1: an evidence-first engineering reference

Evidence snapshot: `aiworkhub.external_case_study.v1`, captured 2026-08-10
from public commit
[`2d6de776a9c836fd6fdbb73f4dd29a9099187ba2`](https://github.com/shrec/UltrafastSecp256k1/tree/2d6de776a9c836fd6fdbb73f4dd29a9099187ba2).

## What this case study establishes

UltrafastSecp256k1 is a useful public reference for the engineering controls
AIWorkHub is designed to make routine: bounded review scopes, replay commands,
claim-to-evidence links, benchmark methodology, negative findings, and an
explicit residual-risk register. The pinned repository exposes each of those
surfaces in reviewable files:

- [repository scope and reviewer entry points](https://github.com/shrec/UltrafastSecp256k1/blob/2d6de776a9c836fd6fdbb73f4dd29a9099187ba2/README.md);
- [benchmark methods and replay commands](https://github.com/shrec/UltrafastSecp256k1/blob/2d6de776a9c836fd6fdbb73f4dd29a9099187ba2/docs/BENCHMARKS.md);
- [claim-to-evidence ledger](https://github.com/shrec/UltrafastSecp256k1/blob/2d6de776a9c836fd6fdbb73f4dd29a9099187ba2/docs/ASSURANCE_LEDGER.md);
- [residual-risk register](https://github.com/shrec/UltrafastSecp256k1/blob/2d6de776a9c836fd6fdbb73f4dd29a9099187ba2/docs/RESIDUAL_RISK_REGISTER.md);
- [security and audit-status boundary](https://github.com/shrec/UltrafastSecp256k1/blob/2d6de776a9c836fd6fdbb73f4dd29a9099187ba2/SECURITY.md).

This is a design proof point, not causal evidence that AIWorkHub produced the
repository's outcomes. It also is not an independent reproduction of the
donor's benchmarks, adoption statements, or security claims.

## The reusable product lesson

The strongest pattern is not any individual benchmark number. It is the join
between a claim, its exact scope, replay instructions, durable evidence, known
limitations, and a release gate. AIWorkHub now implements the corresponding
control-plane pieces through task-scoped authority, attempt artifacts,
graph-scoped review packets, evidence levels, manager acceptance, NeedFix,
Learning Commit, and the release-assurance manifest.

The donor's security policy explicitly states that no external third-party
audit has been completed. AIWorkHub therefore does not describe self-audit as
an independent cryptographic audit and does not make a production-safety claim
from this case study.

## Replaying the source pins

The machine-readable companion
[`ultrafast-secp256k1-evidence-v1.json`](ultrafast-secp256k1-evidence-v1.json)
contains the exact commit and SHA-256 digest of every cited source file. A
reviewer can recompute any row with:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/shrec/UltrafastSecp256k1/2d6de776a9c836fd6fdbb73f4dd29a9099187ba2/README.md \
  | sha256sum
```

Replace `README.md` with the remaining manifest path. A source is no longer the
same evidence if its digest differs.

## Explicit exclusions

- No performance multiplier from the donor is an AIWorkHub benchmark.
- No adoption statement is independently verified here.
- No inference is made from repository size, test count, stars, or activity.
- No self-audit result is upgraded to external-audit evidence.
- No claim is made that AIWorkHub guarantees autonomous production quality.
