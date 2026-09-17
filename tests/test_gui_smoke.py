"""Smoke test for the window: does it build, load, and tear down cleanly.

Runs on Qt's offscreen platform, so it needs no display and no hardware. It
cannot check that anything *looks* right -- only that the widgets construct,
the flow files parse, and the teardown path does not throw.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from geartest.bench import Bench  # noqa: E402
from geartest.config import (  # noqa: E402
    CameraCfg,
    ConsoleCfg,
    DeviceCfg,
    FLOW_DIR,
    RelayCfg,
    RelayChannelCfg,
    RoiCfg,
    ScreenCfg,
    Settings,
    SshCfg,
)
from geartest.flow import FlowEngine, Recorder, load_flow  # noqa: E402
from geartest.ui import main_window as main_window_module  # noqa: E402
from geartest.ui.main_window import MainWindow  # noqa: E402
from geartest.ui.video_view import VideoView  # noqa: E402

_app: QApplication | None = None


def app() -> QApplication:
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication([])
    return _app


def sample_settings() -> Settings:
    """Names but no ports: exactly the no-hardware bench."""
    return Settings(
        relays=[RelayCfg(name="relay1", port="")],
        relay_channels=[
            RelayChannelCfg(name="KL15", relay="relay1", channel=8),
            RelayChannelCfg(name="KL30", relay="relay1", channel=9),
        ],
        consoles=[ConsoleCfg(name="SOC", port=""), ConsoleCfg(name="MCU", port="")],
        cameras=[CameraCfg(name="cam4"), CameraCfg(name="cam5")],
        screens=[
            ScreenCfg(name="屏幕1", camera="cam4"),
            ScreenCfg(name="屏幕2", camera="cam5"),
        ],
        devices=[
            DeviceCfg(
                name="box1",
                power=["KL15", "KL30"],
                consoles=["SOC", "MCU"],
                screens=["屏幕1", "屏幕2"],
            )
        ],
        ssh=[SshCfg(name="container")],
    )


def test_window_builds_without_hardware():
    app()
    window = MainWindow(sample_settings())
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert titles == [
        "被测设备",
        "继电器",
        "串口",
        "ADB",
        "SSH 容器",
        "监控画面",
        "测试流程",
        "配置",
    ], titles
    # The status bar must say something useful when nothing is plugged in.
    assert window.statusBar().currentMessage()
    window.shutdown_panels()


def test_panels_list_the_configured_resources():
    app()
    window = MainWindow(sample_settings())
    assert list(window.relay_panel.rows) == ["relay1"]
    assert list(window.serial_panel.tabs) == ["SOC", "MCU"]
    assert list(window.camera_panel.tiles) == ["屏幕1", "屏幕2"]
    assert list(window.dut_panel.views) == ["box1"]
    window.shutdown_panels()


def test_example_flows_parse_and_describe():
    app()
    window = MainWindow(sample_settings())
    paths = sorted(FLOW_DIR.glob("*.yaml"))
    assert paths, f"no example flows in {FLOW_DIR}"

    engine = FlowEngine(window.bench)
    for path in paths:
        flow = load_flow(path)
        assert flow.get("device"), f"{path.name} 没有声明 device"
        labels = engine.describe(flow)
        assert labels, path
        assert len(labels) == len(flow["steps"]), path
        assert not any(label.startswith("未知") for label in labels), (path, labels)
    window.shutdown_panels()


def test_yaml_boolean_actions_are_understood():
    """An unquoted ``action: on`` parses as the boolean True, not "on"."""
    app()
    window = MainWindow(sample_settings())
    flow = {
        "name": "t",
        "device": "box1",
        "steps": [{"relay": {"resource": "KL15", "action": True}}],
    }
    result = FlowEngine(window.bench).run(flow)
    assert len(result.results) == 1
    # It must fail on the missing COM port, not on an unrecognised action.
    assert "action" not in result.results[0].message.lower(), result.results[0].message
    window.shutdown_panels()


def test_flow_reports_missing_hardware_without_crashing():
    app()
    window = MainWindow(sample_settings())
    flow = {
        "name": "无硬件",
        "device": "box1",
        "steps": [
            {"relay": {"resource": "KL30", "action": "on"}},
            {"log": "这一步不该被执行"},
        ],
    }
    result = FlowEngine(window.bench).run(flow)
    assert not result.ok
    assert result.aborted
    assert len(result.results) == 1, [r.message for r in result.results]
    message = result.results[0].message
    assert message and "relay port" in message, message
    window.shutdown_panels()


def test_recorder_emits_resource_names():
    app()
    recorder = Recorder()
    assert len(recorder) == 0
    recorder.record("relay", {"resource": "KL30", "action": "on"})
    assert len(recorder) == 0, "recording must be off by default"

    recorder.set_enabled(True)
    recorder.record("relay", {"resource": "KL30", "action": "on"})
    recorder.record("serial_write", {"console": "SOC", "text": "hi\r\n"})
    assert len(recorder) == 2

    text = recorder.to_yaml("录制的流程")
    # A recorded flow has to replay, which means resource names, not channels.
    assert "resource: KL30" in text, text
    assert "console: SOC" in text, text
    recorder.set_enabled(False)
    recorder.record("relay", {"resource": "KL15", "action": "off"})
    assert len(recorder) == 2, "disarming must stop capture"


def test_roi_widget_round_trips_fractions():
    app()
    view = VideoView()
    view.resize(800, 600)
    view.set_roi(RoiCfg(0.25, 0.25, 0.5, 0.5))
    assert view.roi().w == 0.5

    import numpy as np

    view.set_frame(np.zeros((720, 1280, 3), dtype=np.uint8))
    rect = view._image_rect()
    assert rect.width() > 0 and rect.height() > 0, rect

    x, y = view._widget_to_norm(rect.center())
    assert abs(x - 0.5) < 0.02 and abs(y - 0.5) < 0.02, (x, y)

    x0, y0 = view._widget_to_norm(rect.topLeft() - rect.topLeft())
    assert 0.0 <= x0 <= 1.0 and 0.0 <= y0 <= 1.0


def test_unknown_settings_keys_are_reported(tmp_path=None):
    """A renamed section must not silently load as an empty bench."""
    import tempfile
    from geartest.config import ConfigError, load_settings

    app()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "bad.yaml"
        path.write_text("serial:\n  - name: CH340\n    port: COM5\n", encoding="utf-8")
        try:
            load_settings(path)
        except ConfigError as exc:
            assert "serial" in str(exc), str(exc)
            assert "consoles" in str(exc), str(exc)
        else:
            raise AssertionError("未知键应当报错，而不是被静默丢弃")


def test_dut_panel_refuses_a_broken_declaration():
    app()
    settings = sample_settings()
    settings.devices[0].screens.append("屏幕9")
    window = MainWindow(settings)
    bench = Bench(settings)
    problems = bench.resolve_all("box1")
    assert any("屏幕9" in problem for problem in problems), problems
    window.shutdown_panels()


def test_close_event_tears_down():
    app()
    saved = []
    original = main_window_module.save_settings
    main_window_module.save_settings = lambda *a, **k: saved.append(a)
    try:
        window = MainWindow(sample_settings())
        window.close()
    finally:
        main_window_module.save_settings = original
    assert saved, "closing must persist the settings"


# -- configuration editor ----------------------------------------------------


def _panel(settings=None, applied=None):
    from geartest.ui.panels import settings_panel as module

    app()
    return module.SettingsPanel(
        settings or sample_settings(),
        on_apply=(applied if applied is not None else []).append,
    ), module


def test_settings_panel_has_a_tab_per_section():
    panel, _ = _panel()
    titles = [
        panel.tab_widget.tabText(i) for i in range(panel.tab_widget.count())
    ]
    assert titles == [
        "被测设备",
        "继电器板",
        "继电器资源",
        "串口",
        "摄像头",
        "监控屏幕",
        "SSH 主机",
    ], titles


def test_settings_tables_show_the_configured_rows():
    panel, _ = _panel()
    counts = {attr: panel.tabs[attr].table.rowCount() for attr in panel.tabs}
    assert counts == {
        "devices": 1,
        "relays": 1,
        "relay_channels": 2,
        "consoles": 2,
        "cameras": 2,
        "screens": 2,
        "ssh": 1,
    }, counts


def test_editing_a_cell_updates_the_working_copy_not_the_live_settings():
    """Edits are held until 保存并应用, so a half-finished edit is not live."""
    settings = sample_settings()
    panel, _ = _panel(settings)
    panel.items(panel.tabs["relays"].section)[0].port = "COM42"
    panel.mark_dirty()

    assert panel.dirty
    assert settings.relays[0].port == "", "未保存前不应改动传入的配置"
    assert panel.working.relays[0].port == "COM42"


def test_adding_and_deleting_rows_refreshes_the_table():
    panel, _ = _panel()
    tab = panel.tabs["cameras"]
    before = tab.table.rowCount()
    panel.items(tab.section).append(CameraCfg(name="cam9", source="9"))
    tab.refresh()
    assert tab.table.rowCount() == before + 1

    del panel.items(tab.section)[-1]
    tab.refresh()
    assert tab.table.rowCount() == before


def test_validation_problems_reach_the_panel():
    settings = sample_settings()
    settings.screens[0].camera = "不存在"
    panel, _ = _panel(settings)
    problems = panel.check(verbose=True)
    assert any("不存在" in p for p in problems), problems
    assert panel.problems.toPlainText().strip(), "问题应当显示在面板上"


def test_saving_an_incoherent_bench_is_refused():
    settings = sample_settings()
    applied: list = []
    panel, module = _panel(settings, applied)
    # Create the problem the way the UI would, so the edit is pending.
    panel.items(panel.tabs["devices"].section)[0].power.append("KL99")
    panel.mark_dirty()

    written: list = []
    original = module.save_settings
    module.save_settings = lambda *a, **k: written.append(a)
    try:
        panel.save()
    finally:
        module.save_settings = original

    assert not written, "有问题的配置不应被写盘"
    assert not applied, "有问题的配置不应被应用"
    assert panel.dirty, "保存失败后应当仍然标记为未保存"
    assert "未保存" in panel._status.text(), panel._status.text()


def test_an_invalid_config_on_disk_is_reported_not_hidden():
    """Loading a bench that is already broken must surface the problems rather
    than look fine until a flow reaches for the missing resource."""
    settings = sample_settings()
    settings.screens[0].camera = "不存在"
    panel, _ = _panel(settings)
    assert not panel.dirty, "载入即有问题不算未保存的改动"
    assert "不存在" in panel.problems.toPlainText(), panel.problems.toPlainText()


def test_saving_a_coherent_bench_writes_and_applies():
    settings = sample_settings()
    applied: list = []
    panel, module = _panel(settings, applied)
    panel.items(panel.tabs["relays"].section)[0].port = "COM42"
    panel.dirty = True

    written: list = []
    original = module.save_settings
    module.save_settings = lambda target, *a, **k: written.append(target)
    try:
        panel.save()
    finally:
        module.save_settings = original

    assert written, "应当写盘"
    assert written[0].relays[0].port == "COM42"
    assert applied, "应当回调应用"
    assert applied[0].relays[0].port == "COM42"
    assert not panel.dirty
    assert "已保存" in panel._status.text(), panel._status.text()


def test_deleting_a_referenced_resource_warns():
    panel, _ = _panel()
    channel = panel.working.relay_channels[0]  # KL15, listed by box1
    warnings = panel.deletion_warnings(panel.tabs["relay_channels"].section, channel)
    assert any("box1" in w for w in warnings), warnings

    camera = panel.working.cameras[0]
    warnings = panel.deletion_warnings(panel.tabs["cameras"].section, camera)
    assert any("屏幕1" in w for w in warnings), warnings


def test_applying_settings_rebuilds_the_panels():
    """Saving must not require a restart: the panels re-read the new bench."""
    from geartest.config import RelayChannelCfg

    app()
    window = MainWindow(sample_settings())
    changed = sample_settings()
    changed.relay_channels.append(
        RelayChannelCfg(name="KL31", relay="relay1", channel=10)
    )
    changed.devices[0].power.append("KL31")
    window.apply_settings(changed)

    assert window.bench.settings is changed
    assert len(window.relay_panel.rows) == 1
    # The rail list follows the new definition.
    labels = [
        window.relay_panel.rails_grid.itemAtPosition(row, 0).widget().text()
        for row in range(3)
    ]
    assert labels == ["KL15", "KL30", "KL31"], labels
    window.shutdown_panels()


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
            import traceback

            failures += 1
            print(f"ERROR {name}\n{traceback.format_exc()}")
        else:
            print(f"ok    {name}")
    # Panels kick off device refreshes when they are constructed. Let those
    # finish, or the interpreter tears them down mid-call.
    from geartest.ui.worker import drain

    drain(5000)
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
