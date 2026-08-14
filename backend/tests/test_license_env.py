"""The VM marker matcher, and its false positives especially — a real PC read
as virtual would (if a block were ever wired to it) lock out a paying customer,
and it pollutes the piracy alert with noise. Pins that ordinary machines never
match. See license._vm_marker_from."""
from __future__ import annotations

from kryptscout.license import _vm_marker_from


def test_common_hypervisors_are_recognised():
    assert _vm_marker_from("VMware, Inc.", "VMware Virtual Platform", "Phoenix")
    assert _vm_marker_from("innotek GmbH", "VirtualBox", "innotek GmbH")
    assert _vm_marker_from("QEMU", "Standard PC (Q35 + ICH9)", "SeaBIOS")
    assert _vm_marker_from("Xen", "HVM domU", "Xen")
    assert _vm_marker_from("Parallels", "Parallels Virtual Platform", "Parallels")


def test_hyper_v_guest_trips_but_a_surface_does_not():
    assert _vm_marker_from("Microsoft Corporation", "Virtual Machine", "Hyper-V UEFI")
    assert _vm_marker_from("Microsoft Corporation", "Surface Pro 9", "Microsoft UEFI") is None


def test_ordinary_machines_are_never_virtual():
    assert _vm_marker_from("Dell Inc.", "XPS 15 9520", "Dell Inc.") is None
    assert _vm_marker_from("ASUSTeK COMPUTER INC.", "ROG STRIX", "American Megatrends") is None
    assert _vm_marker_from("LENOVO", "20XW", "LENOVO") is None
    assert _vm_marker_from("Gigabyte Technology Co., Ltd.", "B550 AORUS", "American Megatrends") is None
    # A real PC with Hyper-V / WSL2 installed still reports its own maker.
    assert _vm_marker_from("Micro-Star International Co., Ltd.", "MS-7C56", "American Megatrends") is None
