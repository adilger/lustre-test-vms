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
    ("rocky8", "container", None, None, "4220fc23dca0f512"),
    ("rocky8", "kernel", None, None, "538527744c6feba0"),
    ("rocky8", "image", None, None, "e4736f2ae1074b39"),
    ("rocky9", "container", None, None, "53ff92f0c29a2c1a"),
    ("rocky9", "kernel", None, None, "77f9158647fe263a"),
    ("rocky9", "image", None, None, "745ecac5e9ee4121"),
    ("rocky9-64k", "container", None, None, "2b44594e1b246688"),
    ("rocky9-64k", "kernel", None, None, "e17374152a5f551f"),
    ("rocky9-64k", "image", None, None, "2c624b692d936820"),
    ("rocky10", "container", None, None, "a8b1c88bcad5e635"),
    ("rocky10", "kernel", None, None, "002508deb710c273"),
    ("rocky10", "image", None, None, "2050f3287f4a97eb"),
    ("mainline", "container", None, None, "3e5f339a82347536"),
    ("mainline", "kernel", None, None, "308f76a4721b789b"),
    ("mainline", "image", None, None, "f89e1a9ac9858958"),
    ("ubuntu2404", "container", None, None, "eebd5c4d9d582ce5"),
    ("ubuntu2404", "kernel", None, None, "50a1d8e151ac94da"),
    ("ubuntu2404", "image", None, None, "024794980116f023"),
    # A variant must not perturb the base hashes above, and must differ
    # from them.
    ("rocky9", "container", None, "mofed-24", "388eb197f85522fe"),
    ("rocky9", "image", None, "mofed-24", "11cb1721a72a011e"),
    # An explicitly named kernel.
    ("rocky9", "kernel", "5.14-rhel9.5", None, "76614275449e5d48"),
    ("rocky9", "image", "5.14-rhel9.5", None, "56c8af98f8233be4"),
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
