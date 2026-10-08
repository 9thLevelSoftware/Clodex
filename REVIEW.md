# Review Guide

Every pull request to this repository receives two review passes:

1. A normal correctness review.
2. A mandatory Ponytail review, focused on keeping the codebase as small as possible.

The Ponytail pass is not optional. Even when the result is only the closing line "Ponytail: Lean already. Ship.", it must appear.

The goal of every review is to keep the codebase correct, secure, maintainable, and as small as possible. Lean toward the laziest solution that actually works: fewer files, fewer dependencies, fewer abstractions, fewer branches, fewer concepts.

## Review Order

1. **Understand the PR intent.** Read the title, description, linked issue, and changed files. Identify the behavior that is supposed to change. Do not suggest simplifications before the real requirement is clear.
2. **Review correctness first.** Look for bugs, broken edge cases, security issues, data loss risks, race conditions, missing validation, bad error handling, broken tests, and regressions. Do not let the Ponytail pass remove necessary safety, validation, accessibility, observability, tests, or behavior explicitly requested by the user.
3. **Run a dedicated Ponytail pass.** Search the diff for unnecessary complexity and prefer deletion over addition.

## The Ponytail Pass

During the Ponytail pass, look for and flag:

- Deletion candidates: dead code, unused flexibility, speculative features, unnecessary branches, unused config, or scaffolding.
- Standard library usage: hand-rolled logic that the language standard library already provides.
- Native platform usage: dependencies or custom code that duplicate what the platform or framework already does.
- Code that is "just in case."
- Abstractions with only one implementation.
- Wrappers around simple APIs.
- Dependencies used for trivial behavior.
- Duplicated helpers that the language, framework, or repository already provides.
- Generated boilerplate or broad scaffolding that this PR does not require.
- Tests that mostly exercise mocks, framework behavior, or implementation details rather than useful behavior.
- Documentation or comments that explain obvious code or defend unnecessary complexity.

Prefer the standard library over hand-rolled code, platform or native framework features over new dependencies, and existing project patterns over new abstractions. Prefer one direct implementation over factories, registries, service layers, interfaces, adapters, or config that has only one use. Challenge speculative future-proofing.

### Ponytail Tags

Use these tags to label each Ponytail finding:

- `delete` — dead code, unused flexibility, speculative feature, unnecessary branch, unused config, or scaffolding.
- `stdlib` — hand-rolled logic that the language standard library already provides.
- `native` — dependency or custom code doing what the platform or framework already does.
- `yagni` — abstraction, config, or extension point with no current need.
- `shrink` — the same behavior can be expressed with materially less code.
- `reuse` — a new helper duplicates an existing project helper or pattern.
- `test-shrink` — a test can be simpler while preserving meaningful coverage.

### Finding Format

Each Ponytail finding must be concise and actionable:

```
<file>:L<line>: <tag> <what to cut>. <what replaces it>.
```

Examples:

- `src/cache.ts:L42`: stdlib: custom LRU cache. Replace with `Map` plus a size cap, or use the existing cache helper in `src/lib/cache.ts`.
- `app/services/UserService.ts:L18`: yagni: `IUserService` has one implementation and one caller. Delete the interface and inject `UserService` directly.
- `src/validators/email.ts:L7`: native: regex-based email parser. Use the platform's email validation already used in `FormInput`.
- `tests/user.test.ts:L88`: test-shrink: five mocked repository tests cover the same branch. Keep one behavior test through the public API.
- `src/config.ts:L31`: delete: `FEATURE_X_STRATEGY` has one value and no callers override it. Inline the value.

If there are no Ponytail findings, say exactly:

```
Ponytail: Lean already. Ship.
```

Do not invent findings. If the code is already simple, say so.

### Boundaries

The Ponytail pass must not remove:

- Required input validation.
- Security checks.
- Error handling that prevents data loss or silent failure.
- Accessibility basics.
- Tests that protect non-trivial behavior.
- Logging or metrics that are operationally necessary.
- Behavior explicitly required by the PR or linked issue.

Do not prefer clever one-liners over readable code when the readable version prevents mistakes. Do not block a PR only because the code could be shorter; block only for correctness, security, data loss, or maintainability risks.

## Review Output

Return the review in the following structure.

### Verdict

One of:

- **Approve**
- **Request changes**
- **Comment only**

Followed by one short sentence explaining why.

### Correctness / Safety Findings

List only real correctness, safety, security, regression, or test issues.

Format:

```
<severity>: <file>:L<line>: <issue>. <required fix>.
```

Severities:

- `critical` — bug, security, or data-loss risk; must fix before merge.
- `important` — likely defect or maintainability hazard; should fix before merge.
- `minor` — small issue, typo, naming, or clarity problem.

If there are none, say:

```
No correctness or safety findings.
```

### Ponytail Review

Always include this section. List Ponytail findings using the exact format above, or say:

```
Ponytail: Lean already. Ship.
```

End the section with:

```
Ponytail net: -<estimated removable lines> lines.
```

If no lines are removable:

```
Ponytail net: 0 lines.
```

### Suggested Minimal Patch

If there are actionable findings, describe the smallest safe patch set. Prefer the fewest files changed, prefer deleting code, do not introduce new dependencies unless absolutely necessary, and do not propose a broad refactor when a local fix solves the issue. Keep this section short.

If no patch is needed, say:

```
No patch needed.
```

### Final Merge Guidance

State clearly whether the PR can merge. Examples:

- "Can merge after the critical finding is fixed."
- "Can merge; Ponytail suggestions are optional cleanup."
- "Do not merge until tests cover the changed behavior."
- "Can merge as-is."

## Behavioral Rules

- Be direct.
- Be specific.
- Do not write long essays.
- Do not praise boilerplate.
- Do not ask the author to "consider" vague changes.
- Every finding must identify exactly what should change.
- If a simplification is optional, mark it as optional.
- If a simplification is required because the complexity creates real risk, explain the risk in one sentence.
- Never treat a tool, test, or CI self-report as proof if the diff itself contradicts it.
- Prefer the smallest root-cause fix over patches scattered across callers.

## Per-PR Checklist

Use this checklist on every PR:

- Did I review correctness and security first?
- Did I run a separate Ponytail pass?
- Did I look for code to delete?
- Did I look for standard library or native replacements?
- Did I look for one-implementation interfaces, factories, or adapters?
- Did I look for speculative config or extensibility?
- Did I avoid removing required validation, security, or tests?
- Did I include either Ponytail findings or the line "Ponytail: Lean already. Ship."?
