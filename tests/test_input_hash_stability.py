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
    ("rocky8", "image", None, None, "6fb0ce637a05e329"),
    ("rocky9", "container", None, None, "4a58226592cb2cf5"),
    ("rocky9", "kernel", None, None, "77f9158647fe263a"),
    ("rocky9", "image", None, None, "f34a951823584ed7"),
    ("rocky9-64k", "container", None, None, "9ebe6bff482840f1"),
    ("rocky9-64k", "kernel", None, None, "e17374152a5f551f"),
    ("rocky9-64k", "image", None, None, "50f13823ffc88371"),
    ("rocky10", "container", None, None, "9f9eabcec78fc131"),
    ("rocky10", "kernel", None, None, "002508deb710c273"),
    ("rocky10", "image", None, None, "977880f7197491f4"),
    ("mainline", "container", None, None, "c25d828a438d5e1b"),
    ("mainline", "kernel", None, None, "308f76a4721b789b"),
    ("mainline", "image", None, None, "35b372d60d8ccf69"),
    ("ubuntu2404", "container", None, None, "85dc6debbb892b65"),
    ("ubuntu2404", "kernel", None, None, "f8b7128d1e8bb1c9"),
    ("ubuntu2404", "image", None, None, "c8e6b070e6aa6b44"),
    ("ubuntu2604", "container", None, None, "d864ce8762f0001a"),
    ("ubuntu2604", "kernel", None, None, "153aab2f08addf1c"),
    ("ubuntu2604", "image", None, None, "379177beff7f1729"),
    # A variant must not perturb the base hashes above, and must differ
    # from them.
    ("rocky9", "container", None, "mofed-24", "cc1a03c2d0fda310"),
    ("rocky9", "image", None, "mofed-24", "db043e414454d27c"),
    # An explicitly named kernel.
    ("rocky9", "kernel", "5.14-rhel9.5", None, "76614275449e5d48"),
    ("rocky9", "image", "5.14-rhel9.5", None, "fa06b880583be98a"),
]


@pytest.fixture(autouse=True)
def _no_built_kernel(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """An image's hash folds in its built kernel's input_hash when one
    exists; pin the goldens without it, or they hold only on the host
    whose artifacts/ produced them."""
    monkeypatch.setattr(
        TargetConfig, "meta_path", lambda self, *a, **k: tmp_path / "none"
    )


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
