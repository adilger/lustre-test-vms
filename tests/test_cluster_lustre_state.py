"""The Lustre part of `ltvm cluster status`."""

from __future__ import annotations

from unittest.mock import patch

from ltvm_pkg import cluster_lustre_state
from ltvm_pkg.vm_state import ClusterInfo


class TestParseTargets:
    DL = (
        "  0 UP osd-ldiskfs lustre-OST0000-osd lustre-OST0000-osd_UUID 4\n"
        "  1 UP ost OSS OSS_uuid 3\n"
        "  2 UP obdfilter lustre-OST0000 lustre-OST0000_UUID 5\n"
        "  3 UP obdfilter lustre-OST0001 lustre-OST0001_UUID 5\n"
    )
    RECOVERY = (
        "obdfilter.lustre-OST0000.recovery_status=\n"
        "status: COMPLETE\n"
        "recovery_start: 1792422111\n"
        "recovery_duration: 31\n"
        "completed_clients: 2/2\n"
        "obdfilter.lustre-OST0001.recovery_status=\n"
        "status: RECOVERING\n"
        "time_remaining: 120\n"
        "connected_clients: 1/2\n"
        "completed_clients: 0/2\n"
    )

    def test_each_target_gets_its_own_status(self) -> None:
        t = cluster_lustre_state.parse_targets(self.DL, self.RECOVERY)
        assert t == [
            {
                "target": "lustre-OST0000",
                "status": "COMPLETE",
                "recovery_duration": "31",
                "completed_clients": "2/2",
            },
            {
                "target": "lustre-OST0001",
                "status": "RECOVERING",
                "time_remaining": "120",
                "connected_clients": "1/2",
                "completed_clients": "0/2",
            },
        ]

    def test_a_one_line_status_follows_the_equals(self) -> None:
        t = cluster_lustre_state.parse_targets(
            "  2 UP obdfilter lustre-OST0000 lustre-OST0000_UUID 5\n",
            "obdfilter.lustre-OST0000.recovery_status=status: INACTIVE\n",
        )
        assert t == [{"target": "lustre-OST0000", "status": "INACTIVE"}]

    def test_mgs_has_no_recovery_status(self) -> None:
        t = cluster_lustre_state.parse_targets("  0 UP mgs MGS MGS 7\n", "")
        assert t == [{"target": "MGS"}]


def test_parse_imports_reads_each_block() -> None:
    text = (
        "osc.lustre-OST0000-osc-ffff0001.import=\n"
        "import:\n"
        "    name: lustre-OST0000-osc-ffff0001\n"
        "    target: lustre-OST0000_UUID\n"
        "    state: FULL\n"
        "    connection:\n"
        '       current_connection: "192.168.105.234@tcp"\n'
        "mdc.lustre-MDT0000-mdc-ffff0001.import=\n"
        "import:\n"
        "    target: lustre-MDT0000_UUID\n"
        "    state: DISCONN\n"
    )
    assert cluster_lustre_state.parse_imports(text) == [
        {
            "target": "lustre-OST0000_UUID",
            "state": "FULL",
            "current_connection": "192.168.105.234@tcp",
        },
        {"target": "lustre-MDT0000_UUID", "state": "DISCONN"},
    ]


def test_unreachable_nodes_are_not_waited_for() -> None:
    c = ClusterInfo(
        name="g",
        nodes=[
            {
                "name": "g-mds1",
                "roles": ["mgs", "mds"],
                "mdt_disks": 1,
                "ost_disks": 0,
                "ip": "10.0.0.10",
            }
        ],
    )
    with (
        patch.object(cluster_lustre_state.VMInfo, "load"),
        patch.object(cluster_lustre_state, "is_running", return_value=False),
    ):
        assert cluster_lustre_state._probe(c.get_nodes()[0]) == {
            "node": "g-mds1",
            "reachable": False,
        }


def test_a_missing_vm_is_unreachable() -> None:
    node = ClusterInfo(
        name="g",
        nodes=[
            {
                "name": "g-gone",
                "roles": ["oss"],
                "mdt_disks": 0,
                "ost_disks": 1,
                "ip": "10.0.0.11",
            }
        ],
    ).get_nodes()[0]
    with patch.object(
        cluster_lustre_state.VMInfo,
        "load",
        side_effect=cluster_lustre_state.VMNotFound("g-gone"),
    ):
        assert cluster_lustre_state._probe(node) == {
            "node": "g-gone",
            "reachable": False,
        }


class TestRecoveryDone:
    def test_recovering_target_is_not_done(self) -> None:
        rows = [
            {
                "node": "a",
                "reachable": True,
                "targets": [{"status": "COMPLETE"}],
            },
            {
                "node": "b",
                "reachable": True,
                "targets": [{"status": "RECOVERING"}],
            },
        ]
        assert not cluster_lustre_state.recovery_done(rows)

    def test_complete_inactive_and_mgs_are_done(self) -> None:
        rows = [
            {
                "node": "a",
                "reachable": True,
                "targets": [{"target": "MGS"}, {"status": "INACTIVE"}],
            },
            {"node": "b", "reachable": False},
            {"node": "c", "reachable": True, "imports": []},
        ]
        assert cluster_lustre_state.recovery_done(rows)

    def test_wait_gives_up_after_the_timeout(self) -> None:
        stuck = [
            {"node": "b", "reachable": True, "targets": [{"status": "WAITING"}]}
        ]
        with (
            patch.object(
                cluster_lustre_state, "lustre_state", return_value=stuck
            ),
            patch.object(cluster_lustre_state.time, "sleep"),
        ):
            rows, done = cluster_lustre_state.wait_recovery(
                ClusterInfo(name="g", nodes=[]), timeout=0
            )
        assert (rows, done) == (stuck, False)

    def test_wait_probes_again_at_the_deadline(self) -> None:
        stuck = [
            {"node": "b", "reachable": True, "targets": [{"status": "WAITING"}]}
        ]
        clock = iter([0.0, 1.0, 3.0])
        with (
            patch.object(
                cluster_lustre_state, "lustre_state", return_value=stuck
            ) as probe,
            patch.object(
                cluster_lustre_state.time, "monotonic", lambda: next(clock)
            ),
            patch.object(cluster_lustre_state.time, "sleep") as sleep,
        ):
            cluster_lustre_state.wait_recovery(
                ClusterInfo(name="g", nodes=[]), timeout=2
            )
        sleep.assert_called_once_with(1.0)
        assert probe.call_count == 2
