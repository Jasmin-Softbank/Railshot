---
name: prepare-deployment
description: Understand an uploaded application and propose the smallest evidence-backed file changes needed for Railshot packaging, build, or HTTP startup failures.
---

# Prepare an application for Railshot

## Where this work fits

Railshot accepts application source and a deployment target. Intake makes a sanitized working copy and inventories it. Deterministic packaging handles known layouts, then gates check the patch, service description, image build and actual container HTTP response, with optional vulnerability scanning. You are called only when a repairable check fails. This is a CI working copy, not the running cluster or the user's original repository.

Your structured proposal is validated and applied by the runner. The gates run again; the Agent's report cannot grant a pass. After successful CI, separate steps publish the exact tested image, update deployment configuration, reconcile it onto the selected cluster, and verify the public endpoint. Those downstream steps own cloud resources, routing and deployment status. Do not generate Terraform, cluster manifests, DNS changes, or deployment receipts here.

The task gives your role, attempt budget, scope and a bounded initial evidence packet, with paths to additional inventory, verdict and history. Start with the packet and expand only for missing facts to distinguish missing packaging from a build failure, a runtime failure, or a runner/platform problem. Paths, runtime versions, allowed images and supported capabilities come from the supplied contract and source; do not assume them from this overview.

## Understand before proposing changes

Follow the actual entrypoint through its imports, build commands, assets and runtime configuration. Identify the application root, services, dependencies and lockfiles, build outputs, working directory, listener, routes, writable data, and external dependencies. Inspect only the relevant files; inventory and README claims are leads to verify against code. A directory called `public` or an HTML file does not prove the application is a static site.

Connect each proposed change to a source location or observed failure. Preserve the application's behavior, selected identity, data requirements and existing toolchain. Reuse a working container definition or supported flag before introducing another file or dependency. Prefer a small coherent proposal to a speculative rewrite.

## Create, update, or delete deliberately

- Create missing deployment artifacts or runtime files only when their role follows from the inspected application and the current scope permits them.
- Update existing files when they already express the intended build or startup. Read the entire affected file before returning its full replacement.
- Delete a writable regular text file only when evidence shows it is redundant or conflicting and needed behavior survives. Trace references first; explain what supersedes it in `files_changed.why`. For example, remove a duplicate legacy deployment spec only after comparing both and preserving the intended services in the retained spec.
- Deletion uses `{"path":"relative/path","action":"delete","content":""}`. Creation/update uses `action: "write"` (or omits it) with full content. All operations share the same path, scope and size checks. Never delete tests, dependency manifests/locks, migrations, data, policies or credentials to silence a failure. A missing file is not a deletion target.
- Under packaging scope, change deployment artifacts only. Under source scope, make only source changes required by an observed build/start/health failure. The outer loop grants source scope only after such a failure; noticing a possible future defect does not expand this attempt's authority.

## Use feedback, then stop

Inspect the latest failed gate and earlier changes before selecting a fix. A first missing-spec failure usually needs the adapter; a failed build or container usually needs the fixer. If a valid packaging proposal leaves a suspected source defect, report the defect in assumptions and let the real gate establish whether source repair is needed. Never disguise it with a placeholder, proxy, swallowed error, or fake health response.

Return the existing report schema, reasons for each changed path, and a plan for the complete gate order. Keep unexecuted checks unverified. The normal budget is at most two SDK attempts, optionally three; a successful initial gate uses none. Stop when the gate passes, the budget is exhausted, the same failure repeats, or the cause requires operator action. Do not spend another attempt on authentication, network provisioning, sandbox failure, or an unknown execution outcome.

For Java/server templates, duplicate specs, and runtime repair examples, read [references/examples.md](references/examples.md) when relevant. These are decision examples, not files to copy blindly.
