"""Per-build documentation set — render the company-shaped doc tree + manifest.

The factory's stages already emit structured artifacts (PRD, design, ADRs, test
plan/report, threat model, SBOM); this package consolidates them into a real
release deliverable: a rendered ``docs/`` tree, agent-authored runbook /
known-issues / upgrade-guide / perf-cost docs, and a single BUILD-MANIFEST that
indexes every artifact, gate verdict, provenance figure and the proxy's observed
model usage.
"""

from foundry.docs.assemble import (
    DocumentationBundle,
    assemble_documentation,
    write_bundle,
)
from foundry.docs.manifest import BuildManifest, build_manifest, render_manifest_md
from foundry.docs.render import RenderedDoc

__all__ = [
    "BuildManifest",
    "DocumentationBundle",
    "RenderedDoc",
    "assemble_documentation",
    "build_manifest",
    "render_manifest_md",
    "write_bundle",
]
