"""Tests for podman_gc: the podman images ltvm builds and leaves behind."""

from __future__ import annotations

import json
from collections.abc import Callable
from unittest.mock import patch

import pytest

from ltvm_pkg import podman_gc


class _Store:
    """A fake podman: tags -> image ids, recording every rmi."""

    def __init__(self, tags: dict[str, str]) -> None:
        self.tags = dict(tags)
        self.removed: list[str] = []
        self.refuse: set[str] = set()

    def __call__(self, *args: str) -> str | None:
        if args[:2] == ("image", "inspect"):
            return self.tags.get(args[-1])
        if args[0] == "rmi":
            if args[1] in self.refuse:
                return None
            self.removed.append(args[1])
            return ""
        raise AssertionError(f"unexpected podman {args}")


def _with(store: Callable[..., str | None]):
    return patch.object(podman_gc, "_podman", side_effect=store)


class TestSuperseded:
    def test_drops_the_image_a_rebuild_moved_the_tag_off(self) -> None:
        store = _Store({"ltvm-build-rocky9": "old"})
        with _with(store):
            s = podman_gc.Superseded()
            s.note("ltvm-build-rocky9")
            store.tags["ltvm-build-rocky9"] = "new"
            s.drop()
        assert store.removed == ["old"]

    def test_unchanged_tag_keeps_its_image(self) -> None:
        store = _Store({"ltvm-build-rocky9": "same"})
        with _with(store):
            s = podman_gc.Superseded()
            s.note("ltvm-build-rocky9")
            s.drop()
        assert store.removed == []

    def test_first_build_has_nothing_to_drop(self) -> None:
        store = _Store({})
        with _with(store):
            s = podman_gc.Superseded()
            s.note("ltvm-build-rocky9")
            store.tags["ltvm-build-rocky9"] = "new"
            s.drop()
        assert store.removed == []

    def test_variant_goes_before_the_base_it_was_built_from(self) -> None:
        store = _Store({"base": "b0", "base-mofed": "v0"})
        with _with(store):
            s = podman_gc.Superseded()
            s.note("base")
            store.tags["base"] = "b1"
            s.note("base-mofed")
            store.tags["base-mofed"] = "v1"
            s.drop()
        assert store.removed == ["v0", "b0"]

    def test_refused_rmi_is_quiet(self) -> None:
        store = _Store({"t": "old"})
        store.refuse.add("old")
        with _with(store):
            s = podman_gc.Superseded()
            s.note("t")
            store.tags["t"] = "new"
            s.drop()
        assert store.removed == []


class TestTags:
    def test_final_tag_is_per_build(self) -> None:
        a = podman_gc.final_image_tag("ltvm-image-rocky9")
        b = podman_gc.final_image_tag("ltvm-image-rocky9")
        assert a != b
        assert a.startswith("ltvm-image-rocky9-final-")
        assert podman_gc._FINAL_RE.search(a)

    def test_ltvm_tags_parses_podman_json(self) -> None:
        rows = [
            {
                "Names": ["localhost/ltvm-image-rocky9:latest"],
                "Size": 3000,
                "Created": 1700000000,
            },
            {
                "Names": [
                    "localhost/ltvm-build-rocky9:latest",
                    "localhost/ltvm-build-rocky9:old",
                ],
                "Size": 1000,
                "Created": 1600000000,
            },
            {"Names": ["quay.io/rockylinux/rockylinux:9"], "Size": 1},
            {"Names": None, "Size": 5},
        ]
        with patch.object(podman_gc, "_podman", return_value=json.dumps(rows)):
            tags = podman_gc.ltvm_tags()
        assert [(t.tag, t.size, t.created) for t in tags] == [
            ("ltvm-image-rocky9", 3000, 1700000000.0),
            ("ltvm-build-rocky9", 1000, 1600000000.0),
        ]

    @pytest.mark.parametrize("out", [None, "", "not json"])
    def test_ltvm_tags_without_a_store(self, out: str | None) -> None:
        with patch.object(podman_gc, "_podman", return_value=out):
            assert podman_gc.ltvm_tags() == []

    def test_expected_tags_follow_targets_yaml(self) -> None:
        expected = podman_gc.expected_tags()
        assert "ltvm-build-rocky9" in expected
        assert "ltvm-build-rocky9-aarch64" in expected
        assert "ltvm-image-rocky9" in expected
        assert "ltvm-image-rocky9-aarch64" in expected
        assert "ltvm-build-rocky9-mofed-24" in expected
        assert "ltvm-image-rocky9-mofed-24" in expected
        assert "ltvm-build-rocky9-mofed" not in expected

    def test_dead_tags(self) -> None:
        def info(tag: str) -> podman_gc.TagInfo:
            return podman_gc.TagInfo(tag=tag, size=1, created=0)

        tags = [
            info("ltvm-build-rocky9"),
            info("ltvm-image-rocky9"),
            info("ltvm-image-rocky9-final"),
            info("ltvm-image-rocky9-final-0123abcd"),
            info("ltvm-image-rocky9-with-mods"),
        ]
        with (
            patch.object(podman_gc, "ltvm_tags", return_value=tags),
            patch.object(
                podman_gc,
                "expected_tags",
                return_value={"ltvm-build-rocky9", "ltvm-image-rocky9"},
            ),
        ):
            dead = {t.tag: why for t, why in podman_gc.dead_tags()}
        assert set(dead) == {
            "ltvm-image-rocky9-final",
            "ltvm-image-rocky9-final-0123abcd",
            "ltvm-image-rocky9-with-mods",
        }
        assert "export" in dead["ltvm-image-rocky9-final"]
        assert "targets.yaml" in dead["ltvm-image-rocky9-with-mods"]
