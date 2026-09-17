"""Field metadata and cross-reference rules for the configuration editor.

Kept free of Qt on purpose. The rules about what a valid bench looks like --
which names must refer to each other, which ranges are sensible -- are not
presentation, and testing them should not need a display. The editor in
``ui/panels/settings_panel.py`` only renders what is declared here.

Adding a field to a dataclass in ``config.py`` without adding it here is caught
by ``tests/test_config_schema.py``, so the editor cannot silently fall behind
the schema.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from .config import (
    CameraCfg,
    ConsoleCfg,
    DeviceCfg,
    RelayCfg,
    RelayChannelCfg,
    ScreenCfg,
    Settings,
    SshCfg,
)

#: Editor kinds. ``choice`` and ``multichoice`` pull their candidates from
#: another section, which is how a field like ``relay_channels[].relay`` becomes
#: a dropdown of the boards that actually exist.
TEXT = "text"
INT = "int"
FLOAT = "float"
BOOL = "bool"
CHOICE = "choice"
MULTICHOICE = "multichoice"
ROI = "roi"


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str = TEXT
    hint: str = ""
    source: str = ""
    """For choice/multichoice: the Settings section to list candidates from."""
    minimum: float = 0
    maximum: float = 0
    suffix: str = ""


@dataclass(frozen=True)
class Section:
    attr: str
    title: str
    cls: type
    fields: tuple[Field, ...]
    key_field: str
    """Which field identifies a row, used for the table's first column."""
    summary: tuple[str, ...] = ()
    """Extra fields worth showing in the table."""
    note: str = ""


def _reported(attr: str, source: str) -> str:
    """Label for a field that points at another section."""
    return f"候选来自 {source}"


RELAY_FIELDS = (
    Field("name", "名称", hint="逻辑名，别处引用它"),
    Field("port", "串口", hint="例如 COM4；留空则不打开"),
    Field("baudrate", "波特率", INT, minimum=1200, maximum=921600),
    Field("slave_id", "从站地址", INT, minimum=1, maximum=247),
    Field("coil_base", "1 路线圈基址", INT, minimum=0, maximum=65535,
          hint="第 1 路对应的线圈地址，不同板子 0 基/1 基不同"),
    Field("timeout", "响应超时", FLOAT, minimum=0.05, maximum=30, suffix=" s"),
    Field("inter_frame_delay", "帧间延时", FLOAT, minimum=0, maximum=1, suffix=" s"),
    Field("pulse_ms", "脉冲默认时长", INT, minimum=10, maximum=60000, suffix=" ms"),
    Field("bytesize", "数据位", INT, minimum=5, maximum=8),
    Field("parity", "校验", hint="N / E / O"),
    Field("stopbits", "停止位", FLOAT, minimum=1, maximum=2),
)

CHANNEL_FIELDS = (
    Field("name", "资源名", hint="用例里写 resource: 这个名字"),
    Field("relay", "所属板卡", CHOICE, source="relays"),
    Field("channel", "第几路", INT, minimum=1, maximum=16),
    Field("pulse_ms", "脉冲时长", INT, minimum=10, maximum=60000, suffix=" ms"),
    Field("description", "说明"),
)

CONSOLE_FIELDS = (
    Field("name", "资源名", hint="用例里写 console: 这个名字"),
    Field("port", "串口", hint="例如 COM35；留空则不打开"),
    Field("baudrate", "波特率", INT, minimum=1200, maximum=921600),
    Field("bytesize", "数据位", INT, minimum=5, maximum=8),
    Field("parity", "校验", hint="N / E / O"),
    Field("stopbits", "停止位", FLOAT, minimum=1, maximum=2),
    Field("encoding", "编码"),
)

CAMERA_FIELDS = (
    Field("name", "名称"),
    Field("source", "视频源", hint="采集索引 0/1/… 或 rtsp://… / http://…"),
    Field("width", "宽", INT, minimum=160, maximum=7680),
    Field("height", "高", INT, minimum=120, maximum=4320),
)

SCREEN_FIELDS = (
    Field("name", "屏幕名", hint="用例里写 screen: 这个名字"),
    Field("camera", "摄像头", CHOICE, source="cameras"),
    Field("roi", "监控范围", ROI, hint="归一化比例，也可在主界面拖拽框选"),
    Field("sensitivity", "灵敏度", INT, minimum=0, maximum=100,
          hint="唯一的强度旋钮；所有阈值由画面自身推导，与绝对亮度无关"),
    Field("freeze_timeout_s", "冻屏超时", FLOAT, minimum=1, maximum=600, suffix=" s"),
    Field("black_confirm_s", "黑屏确认", FLOAT, minimum=0.2, maximum=60, suffix=" s"),
    Field("min_transient_ms", "最小闪黑", FLOAT, minimum=0, maximum=1000, suffix=" ms"),
    Field("transient_max_s", "闪黑上限", FLOAT, minimum=0.2, maximum=60, suffix=" s"),
)

DEVICE_FIELDS = (
    Field("name", "设备名", hint="用例里写 device: 这个名字"),
    Field("adb_serial", "adb 序列号", hint="物理属性；也可在主界面里绑定"),
    Field("power", "电源资源", MULTICHOICE, source="relay_channels"),
    Field("consoles", "串口资源", MULTICHOICE, source="consoles"),
    Field("screens", "画面资源", MULTICHOICE, source="screens"),
    Field("adb_path", "adb 路径"),
    Field("description", "说明"),
)

SSH_FIELDS = (
    Field("name", "名称"),
    Field("host", "主机"),
    Field("port", "端口", INT, minimum=1, maximum=65535),
    Field("username", "用户名"),
    Field("password", "密码", hint="留空则用密钥"),
    Field("key_file", "私钥文件"),
    Field("workdir", "工作路径", hint="命令会先 cd 到这里"),
    Field("docker_exec", "进容器前缀",
          hint="须自带 shell，例如 docker exec -i ctr sh -c"),
)

SECTIONS: tuple[Section, ...] = (
    Section(
        attr="devices",
        title="被测设备",
        cls=DeviceCfg,
        fields=DEVICE_FIELDS,
        key_field="name",
        summary=("adb_serial", "power", "consoles", "screens"),
        note="把资源归到一台设备名下；成员关系显式声明，不做推断",
    ),
    Section(
        attr="relays",
        title="继电器板",
        cls=RelayCfg,
        fields=RELAY_FIELDS,
        key_field="name",
        summary=("port", "slave_id", "coil_base"),
    ),
    Section(
        attr="relay_channels",
        title="继电器资源",
        cls=RelayChannelCfg,
        fields=CHANNEL_FIELDS,
        key_field="name",
        summary=("relay", "channel", "description"),
        note="一条 = 某块板的某一路，这是用例里引用的单位",
    ),
    Section(
        attr="consoles",
        title="串口",
        cls=ConsoleCfg,
        fields=CONSOLE_FIELDS,
        key_field="name",
        summary=("port", "baudrate"),
    ),
    Section(
        attr="cameras",
        title="摄像头",
        cls=CameraCfg,
        fields=CAMERA_FIELDS,
        key_field="name",
        summary=("source",),
    ),
    Section(
        attr="screens",
        title="监控屏幕",
        cls=ScreenCfg,
        fields=SCREEN_FIELDS,
        key_field="name",
        summary=("camera", "sensitivity", "freeze_timeout_s"),
        note="屏幕 = 摄像头 + 该摄像头视野里的一块区域",
    ),
    Section(
        attr="ssh",
        title="SSH 主机",
        cls=SshCfg,
        fields=SSH_FIELDS,
        key_field="name",
        summary=("host", "username", "workdir"),
        note="不参与设备资源表，用例里按名字全局引用",
    ),
)

BY_ATTR = {section.attr: section for section in SECTIONS}


def section_for(attr: str) -> Section:
    try:
        return BY_ATTR[attr]
    except KeyError:
        raise KeyError(f"未知的配置分区：{attr}") from None


def field_map(section: Section) -> dict[str, Field]:
    return {field.name: field for field in section.fields}


# -- candidate lists --------------------------------------------------------


def list_items(settings: Settings, attr: str) -> list:
    return list(getattr(settings, attr))


def names_in(settings: Settings, attr: str) -> list[str]:
    return [item.name for item in list_items(settings, attr)]


def candidates(settings: Settings, source: str) -> list[str]:
    return names_in(settings, source) if source else []


def display_value(item: Any, field: Field) -> str:
    """One cell of the section table."""
    value = getattr(item, field.name)
    if isinstance(value, list):
        return "、".join(str(v) for v in value) if value else "（无）"
    if isinstance(value, float):
        return f"{value:g}"
    if field.kind == ROI or hasattr(value, "x"):
        return f"x={value.x:.2f} y={value.y:.2f} w={value.w:.2f} h={value.h:.2f}"
    return str(value)


def unique_name(settings: Settings, section: Section) -> str:
    """A name for a new row that does not collide with an existing one."""
    existing = set(names_in(settings, section.attr))
    base = section.cls().name or section.title
    if base not in existing:
        return base
    index = 1
    while f"{base}{index}" in existing:
        index += 1
    return f"{base}{index}"


def new_item(settings: Settings, section: Section) -> Any:
    item = section.cls()
    if any(f.name == "name" for f in section.fields):
        item.name = unique_name(settings, section)
    return item


# -- validation -------------------------------------------------------------


def _check_names(settings: Settings, section: Section, problems: list[str]) -> None:
    seen: set[str] = set()
    for index, item in enumerate(list_items(settings, section.attr)):
        name = str(getattr(item, "name", "")).strip()
        where = f"{section.title}[{index + 1}]"
        if not name:
            problems.append(f"{where}：名称不能为空")
            continue
        if name in seen:
            problems.append(f"{where}：名称「{name}」重复")
        seen.add(name)


def _check_reference(
    settings: Settings,
    owner: str,
    field_name: str,
    target: str,
    problems: list[str],
) -> None:
    available = set(names_in(settings, target))
    for index, item in enumerate(list_items(settings, owner)):
        value = getattr(item, field_name, None)
        if not value:
            problems.append(
                f"{BY_ATTR[owner].title}[{item.name}]：{field_name} 未指定"
            )
            continue
        if value not in available:
            problems.append(
                f"{BY_ATTR[owner].title}[{item.name}]：{field_name}「{value}」"
                f"在 {target} 里不存在（现有：{'、'.join(sorted(available)) or '无'}）"
            )


def _check_membership(
    settings: Settings, owner: str, field_name: str, target: str, problems: list[str]
) -> None:
    available = set(names_in(settings, target))
    for item in list_items(settings, owner):
        for name in getattr(item, field_name, []) or []:
            if name not in available:
                problems.append(
                    f"被测设备[{item.name}] 的 {field_name} 引用了不存在的"
                    f"「{name}」（现有：{'、'.join(sorted(available)) or '无'}）"
                )


def _check_range(
    settings: Settings, attr: str, field_name: str, low: float, high: float, problems: list[str]
) -> None:
    section = BY_ATTR[attr]
    for item in list_items(settings, attr):
        value = getattr(item, field_name)
        if not (low <= value <= high):
            problems.append(
                f"{section.title}[{item.name}]：{field_name} = {value}，"
                f"应在 {low:g}…{high:g} 之间"
            )


def _check_roi(settings: Settings, problems: list[str]) -> None:
    for screen in settings.screens:
        roi = screen.roi
        if roi.w <= 0 or roi.h <= 0:
            problems.append(f"监控屏幕[{screen.name}]：监控范围的宽和高必须大于 0")
        if not (0 <= roi.x <= 1 and 0 <= roi.y <= 1):
            problems.append(f"监控屏幕[{screen.name}]：监控范围起点应在 0…1 之间")
        if roi.x + roi.w > 1.001 or roi.y + roi.h > 1.001:
            problems.append(f"监控屏幕[{screen.name}]：监控范围超出了画面")


def validate(settings: Settings) -> list[str]:
    """Every problem with a bench definition, or an empty list.

    All of them at once rather than the first: the editor is used while setting
    a bench up, and finding out about one mistake per save is tedious.
    """
    problems: list[str] = []

    for section in SECTIONS:
        _check_names(settings, section, problems)

    _check_reference(settings, "relay_channels", "relay", "relays", problems)
    _check_reference(settings, "screens", "camera", "cameras", problems)
    for field_name, target in (
        ("power", "relay_channels"),
        ("consoles", "consoles"),
        ("screens", "screens"),
    ):
        _check_membership(settings, "devices", field_name, target, problems)

    _check_range(settings, "relay_channels", "channel", 1, 16, problems)
    _check_range(settings, "screens", "sensitivity", 0, 100, problems)
    _check_range(settings, "cameras", "width", 160, 7680, problems)
    _check_range(settings, "cameras", "height", 120, 4320, problems)

    _check_roi(settings, problems)

    serials: dict[str, str] = {}
    for device in settings.devices:
        if not device.adb_serial:
            continue
        if device.adb_serial in serials:
            problems.append(
                f"被测设备[{device.name}] 与 [{serials[device.adb_serial]}] "
                f"绑定了同一个 adb 序列号「{device.adb_serial}」"
            )
        serials[device.adb_serial] = device.name

    return problems


# -- keeping the metadata honest --------------------------------------------


def dataclass_fields_of(section: Section) -> set[str]:
    return {f.name for f in dataclasses.fields(section.cls)}


def declared_fields_of(section: Section) -> set[str]:
    return {f.name for f in section.fields}
