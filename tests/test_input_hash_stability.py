"""Golden values for TargetConfig.input_hash.

``input_hash`` is the staleness key: it is written into every artifact's
meta.json and compared against a freshly computed one to decide whether
to rebuild.  So changing what it produces, for inputs that did not
change, silently invalidates every built artifact on every machine --
and the cost is a kernel rebuild per target, not a warning.

These goldens exist so a refactor of the hash *composition* (splitting
it into named components for `build status --why`, say) cannot do that
by accident.  They are not a specification of the algorithm: if you
deliberately change what feeds the hash, these values must be updated in
the same commit, and everyone rebuilds. That is the signal, and it
should be a conscious one.

They depend on targets.yaml and on the files the hash reads
(Dockerfiles, kernel config fragments, package lists, the inner build
scripts), so an intentional edit to any of those also moves them.
"""

from __future__ import annotations

import pytest

from ltvm_pkg.target_config import TargetConfig

# (target, artifact, kernel, variant, expected)
GOLDEN = [
    ("rocky8", "container", None, None, "c2fdbaa66888488a"),
    ("rocky8", "kernel", None, None, "538527744c6feba0"),
    ("rocky8", "image", None, None, "8fb9ac89f14d7f81"),
    ("rocky9", "container", None, None, "4a58226592cb2cf5"),
    ("rocky9", "kernel", None, None, "77f9158647fe263a"),
    ("rocky9", "image", None, None, "1fcaf454ca982618"),
    ("rocky9-64k", "container", None, None, "9ebe6bff482840f1"),
    ("rocky9-64k", "kernel", None, None, "e17374152a5f551f"),
    ("rocky9-64k", "image", None, None, "42d9a2d40d11fba1"),
    ("rocky10", "container", None, None, "9f9eabcec78fc131"),
    ("rocky10", "kernel", None, None, "002508deb710c273"),
    ("rocky10", "image", None, None, "3ff06a8ea489a5e4"),
    ("mainline", "container", None, None, "c25d828a438d5e1b"),
    ("mainline", "kernel", None, None, "308f76a4721b789b"),
    ("mainline", "image", None, None, "39d1ec0a83b29533"),
    ("ubuntu2404", "container", None, None, "c0cdf7ac34d41595"),
    ("ubuntu2404", "kernel", None, None, "50a1d8e151ac94da"),
    ("ubuntu2404", "image", None, None, "640a1a09438da43a"),
    ("ubuntu2604", "container", None, None, "b7db1277b3a441bd"),
    ("ubuntu2604", "kernel", None, None, "08406db2cab2835e"),
    ("ubuntu2604", "image", None, None, "61ea801acd72c29a"),
    # A variant must not perturb the base hashes above, and must differ
    # from them.
    ("rocky9", "container", None, "mofed-24", "cc1a03c2d0fda310"),
    ("rocky9", "image", None, "mofed-24", "77bfb262138b976a"),
    # An explicitly named kernel.
    ("rocky9", "kernel", "5.14-rhel9.5", None, "76614275449e5d48"),
    ("rocky9", "image", "5.14-rhel9.5", None, "aa9ebd39d255a37a"),
]


@pytest.mark.parametrize(
    "target,artifact,kernel,variant,expected",
    GOLDEN,
    ids=[
        f"{t}-{a}{'-' + k if k else ''}{'-' + v if v else ''}"
        for t, a, k, v, _ in GOLDEN
    ],
)
def test_input_hash_is_unchanged(
    target: str,
    artifact: str,
    kernel: str | None,
    variant: str | None,
    expected: str,
) -> None:
    tc = TargetConfig(target, variant=variant or "base")
    got = tc.input_hash(artifact, kernel=kernel, variant=variant)
    assert got == expected, (
        f"input_hash({target}, {artifact}, kernel={kernel}, "
        f"variant={variant}) changed: {expected} -> {got}.\n"
        f"Every built {artifact} for {target} just became stale. If that "
        f"is intended, update the golden in the same commit."
    )


def test_extra_bytes_still_fold_in() -> None:
    """kernel_build passes the Lustre patch series through `extra`.

    Without it, editing a patch in place would not invalidate the cached
    vmlinuz -- the exact workflow ltvm exists for.
    """
    tc = TargetConfig("rocky9")
    assert tc.input_hash("kernel", extra=b"patchbytes") == "56927251fc6a3d53"
    assert tc.input_hash("kernel", extra=b"patchbytes") != tc.input_hash(
        "kernel"
    )
