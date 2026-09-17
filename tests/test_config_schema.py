"""The configuration editor's rules, tested without a display.

The editor renders whatever ``config_schema`` declares, so the declarations are
where a mistake would do damage. The coverage test is the important one: adding
a field to ``config.py`` without describing it here would otherwise produce a
GUI that silently cannot edit it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dataclasses  # noqa: E402
import tempfile  # noqa: E402
import traceback  # noqa: E402

from geartest.config import (  # noqa: E402
    CameraCfg,
    ConsoleCfg,
    DeviceCfg,
    RelayCfg,
    RelayChannelCfg,
    RoiCfg,
    ScreenCfg,
    Settings,
    SshCfg,
    load_settings,
    save_settings,
)
from geartest.config_schema import (  # noqa: E402
    SECTIONS,
    candidates,
    dataclass_fields_of,
    declared_fields_of,
    display_value,
    field_map,
    new_item,
    section_for,
    unique_name,
    validate,
)


def base_settings() -> Settings:
    return Settings(
        relays=[RelayCfg(name="relay1", port="COM4")],
        relay_channels=[
            RelayChannelCfg(name="KL15", relay="relay1", channel=8),
            RelayChannelCfg(name="KL30", relay="relay1", channel=9),
        ],
        consoles=[ConsoleCfg(name="SOC", port="COM35"), ConsoleCfg(name="MCU", port="COM36")],
        cameras=[CameraCfg(name="cam4", source="4"), CameraCfg(name="cam5", source="5")],
        screens=[ScreenCfg(name="屏幕1", camera="cam4")],
        devices=[
            DeviceCfg(
                name="box1",
                adb_serial="0123456789",
                power=["KL15", "KL30"],
                consoles=["SOC", "MCU"],
                screens=["屏幕1"],
            )
        ],
        ssh=[SshCfg(name="container")],
    )


# -- coverage: the editor cannot fall behind the schema ----------------------


def test_every_dataclass_field_is_described():
    missing: list[str] = []
    for section in SECTIONS:
        absent = dataclass_fields_of(section) - declared_fields_of(section)
        if absent:
            missing.append(f"{section.cls.__name__}: {sorted(absent)}")
    assert not missing, "以下字段没有在 config_schema 里描述，界面将无法编辑：" + "; ".join(
        missing
    )


def test_no_described_field_is_a_typo():
    extra: list[str] = []
    for section in SECTIONS:
        surplus = declared_fields_of(section) - dataclass_fields_of(section)
        if surplus:
            extra.append(f"{section.cls.__name__}: {sorted(surplus)}")
    assert not extra, "config_schema 描述了不存在的字段：" + "; ".join(extra)


def test_every_settings_section_has_an_editor_tab():
    """A new section on Settings must get a tab, or it is unreachable by GUI."""
    attrs = {f.name for f in dataclasses.fields(Settings)}
    covered = {section.attr for section in SECTIONS}
    assert attrs == covered, f"未覆盖：{sorted(attrs - covered)}"


def test_choice_fields_point_at_sections_that_exist():
    attrs = {f.name for f in dataclasses.fields(Settings)}
    for section in SECTIONS:
        for field in section.fields:
            if field.kind in ("choice", "multichoice"):
                assert field.source, f"{section.cls.__name__}.{field.name} 缺少 source"
                assert field.source in attrs, (
                    f"{section.cls.__name__}.{field.name} 的 source「{field.source}」不是配置分区"
                )
                assert field.source in {s.attr for s in SECTIONS}, (
                    f"{field.source} 没有对应的编辑标签页"
                )


def test_key_field_is_described():
    for section in SECTIONS:
        assert section.key_field in declared_fields_of(section), (
            f"{section.cls.__name__} 的 key_field「{section.key_field}」没有描述"
        )


def test_numeric_fields_declare_a_usable_range():
    """The editor builds spin boxes straight from these; a zero range would
    produce a control that cannot be moved."""
    broken: list[str] = []
    for section in SECTIONS:
        for field in section.fields:
            if field.kind not in ("int", "float"):
                continue
            if not field.maximum > field.minimum:
                broken.append(f"{section.cls.__name__}.{field.name}")
    assert not broken, "以下数值字段没有声明有效范围：" + "、".join(broken)


# -- naming ------------------------------------------------------------------


def test_unique_name_avoids_collisions():
    settings = base_settings()
    relays = section_for("relays")
    # relay1 exists, and RelayCfg() defaults to relay1
    assert unique_name(settings, relays) == "relay11"
    settings.relays.append(RelayCfg(name="relay11"))
    assert unique_name(settings, relays) == "relay12"


def test_new_item_gets_a_free_name():
    settings = base_settings()
    for section in SECTIONS:
        item = new_item(settings, section)
        existing = [getattr(i, "name") for i in getattr(settings, section.attr)]
        assert item.name not in existing, f"{section.title} 生成了重名 {item.name}"


def test_display_value_renders_lists_and_roi():
    screen = ScreenCfg(name="s", camera="cam4", roi=RoiCfg(0.1, 0.2, 0.3, 0.4))
    fields = field_map(section_for("screens"))
    assert "x=0.10" in display_value(screen, fields["roi"])

    device = DeviceCfg(name="d", power=["KL15"])
    device_fields = field_map(section_for("devices"))
    assert display_value(device, device_fields["power"]) == "KL15"
    assert display_value(device, device_fields["consoles"]) == "（无）"


def test_candidates_lists_names():
    settings = base_settings()
    assert candidates(settings, "relays") == ["relay1"]
    assert candidates(settings, "relay_channels") == ["KL15", "KL30"]


# -- validation --------------------------------------------------------------


def test_a_coherent_bench_validates_clean():
    problems = validate(base_settings())
    assert problems == [], problems


def test_duplicate_names_are_caught():
    settings = base_settings()
    settings.consoles.append(ConsoleCfg(name="SOC", port="COM99"))
    problems = validate(settings)
    assert any("重复" in p for p in problems), problems


def test_empty_name_is_caught():
    settings = base_settings()
    settings.cameras.append(CameraCfg(name=""))
    assert any("名称不能为空" in p for p in validate(settings))


def test_dangling_board_reference_is_caught():
    settings = base_settings()
    settings.relay_channels[0].relay = "relay9"
    problems = validate(settings)
    assert any("relay9" in p and "不存在" in p for p in problems), problems


def test_dangling_camera_reference_is_caught():
    settings = base_settings()
    settings.screens[0].camera = "cam9"
    assert any("cam9" in p for p in validate(settings))


def test_dangling_device_membership_is_caught():
    """A device naming a resource that does not exist is the error that would
    otherwise only surface when a flow reached for it."""
    settings = base_settings()
    settings.devices[0].power.append("KL99")
    problems = validate(settings)
    assert any("KL99" in p for p in problems), problems


def test_out_of_range_values_are_caught():
    settings = base_settings()
    settings.relay_channels[0].channel = 20
    assert any("channel" in p for p in validate(settings))

    settings = base_settings()
    settings.screens[0].sensitivity = 150
    assert any("sensitivity" in p for p in validate(settings))


def test_impossible_roi_is_caught():
    settings = base_settings()
    settings.screens[0].roi = RoiCfg(0.8, 0.0, 0.5, 1.0)
    assert any("超出了画面" in p for p in validate(settings))

    settings = base_settings()
    settings.screens[0].roi = RoiCfg(0.0, 0.0, 0.0, 1.0)
    assert any("必须大于 0" in p for p in validate(settings))


def test_duplicate_adb_serial_is_caught():
    settings = base_settings()
    settings.devices.append(DeviceCfg(name="box2", adb_serial="0123456789"))
    assert any("同一个 adb 序列号" in p for p in validate(settings))


def test_empty_adb_serial_is_allowed():
    """Falling back to adb's default device is legitimate."""
    settings = base_settings()
    settings.devices[0].adb_serial = ""
    assert validate(settings) == []


def test_validation_reports_everything_at_once():
    settings = base_settings()
    settings.relay_channels[0].relay = "nope"
    settings.screens[0].camera = "nope"
    settings.devices[0].power.append("nope")
    assert len(validate(settings)) >= 3


# -- round trip --------------------------------------------------------------


def test_edited_settings_survive_a_save_and_load():
    settings = base_settings()
    item = new_item(settings, section_for("screens"))
    item.camera = "cam5"
    item.roi = RoiCfg(0.25, 0.25, 0.5, 0.5)
    settings.screens.append(item)

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "settings.yaml"
        save_settings(settings, path)
        back = load_settings(path)

    assert [s.name for s in back.screens] == ["屏幕1", item.name]
    assert back.screens[1].roi.w == 0.5
    assert validate(back) == []


def test_deleting_a_resource_leaves_a_dangling_reference_that_validation_flags():
    """The editor must not silently let a delete break a device."""
    settings = base_settings()
    settings.relay_channels = [
        c for c in settings.relay_channels if c.name != "KL15"
    ]
    problems = validate(settings)
    assert any("KL15" in p for p in problems), problems


def main() -> int:
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {name}\n      {exc}")
        except Exception:
            failures += 1
            print(f"ERROR {name}\n{traceback.format_exc()}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
