"""Protocol tests for the Modbus RTU relay board.

The relay board is not present, so the transport is stubbed: the frames the
driver emits, and how it parses the replies, are checked byte for byte. This is
the part of the bench that cannot be verified by looking at it, and getting a
coil address wrong silently switches the wrong thing.
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from geartest.config import RelayCfg
from geartest.devices.relay_modbus import RelayBoard, RelayError, check_crc, crc16, with_crc


class FakeSerial:
    """Stands in for a serial port, replaying one canned reply per write."""

    def __init__(self, replies: list[bytes] | None = None) -> None:
        self.written = bytearray()
        self.is_open = True
        self._replies = list(replies or [])
        self._rx = bytearray()

    def reset_input_buffer(self) -> None:
        self._rx.clear()

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        if self._replies:
            self._rx.extend(self._replies.pop(0))
        return len(data)

    def read(self, count: int) -> bytes:
        chunk = bytes(self._rx[:count])
        del self._rx[:count]
        return chunk

    def close(self) -> None:
        self.is_open = False


def board_with(replies: list[bytes] | None = None, **overrides) -> RelayBoard:
    settings = {"port": "FAKE", "timeout": 0.15, "inter_frame_delay": 0.0}
    settings.update(overrides)
    board = RelayBoard(RelayCfg(**settings))
    board._serial = FakeSerial(replies)
    return board


def ok_write_coil(coil: int, on: bool) -> bytes:
    value = 0xFF00 if on else 0x0000
    return with_crc(
        bytes((1, 0x05, coil >> 8, coil & 0xFF, value >> 8, value & 0xFF))
    )


# -- CRC --------------------------------------------------------------------


def test_crc_matches_published_vectors():
    """Two standard Modbus RTU frames with their known CRCs."""
    assert crc16(bytes.fromhex("01 05 00 00 FF 00")) == 0x3A8C
    assert crc16(bytes.fromhex("01 01 00 00 00 10")) == 0xC63D
    assert with_crc(bytes.fromhex("01 05 00 00 FF 00")).hex(" ").upper() == (
        "01 05 00 00 FF 00 8C 3A"
    )


def test_check_crc_rejects_a_corrupted_frame():
    frame = bytearray(with_crc(bytes.fromhex("01 05 00 00 FF 00")))
    frame[-1] ^= 0xFF
    try:
        check_crc(bytes(frame))
    except RelayError as exc:
        assert "CRC" in str(exc)
    else:
        raise AssertionError("a bad CRC must not pass")


# -- writing coils ----------------------------------------------------------


def test_set_channel_emits_the_exact_frame():
    board = board_with([ok_write_coil(0x0000, True)])
    board.set_channel(1, True)
    assert bytes(board._serial.written) == bytes.fromhex("01 05 00 00 FF 00 8C 3A")
    assert board.state[0] is True


def test_set_channel_off_emits_zero_value():
    reply = ok_write_coil(0x0000, False)
    board = board_with([reply])
    board.set_channel(1, False)
    frame = bytes(board._serial.written)
    assert frame[:6] == bytes.fromhex("01 05 00 00 00 00"), frame.hex(" ")
    check_crc(frame)
    assert board.state[0] is False


def test_channel_number_maps_through_coil_base():
    """Boards disagree on whether channel 1 is coil 0 or coil 1."""
    zero_based = board_with([ok_write_coil(0x0004, True)], coil_base=0)
    zero_based.set_channel(5, True)
    assert bytes(zero_based._serial.written)[2:4] == bytes.fromhex("00 04")

    one_based = board_with([ok_write_coil(0x0005, True)], coil_base=1)
    one_based.set_channel(5, True)
    assert bytes(one_based._serial.written)[2:4] == bytes.fromhex("00 05")


def test_channel_range_is_enforced():
    board = board_with()
    for bad in (0, 17, -1):
        try:
            board.set_channel(bad, True)
        except ValueError:
            pass
        else:
            raise AssertionError(f"channel {bad} should be rejected")


def test_write_channels_packs_bits_low_channel_first():
    """Modbus packs coil 1 into bit 0 -- getting this backwards swaps channels."""
    # A 0x0F reply echoes only the address and quantity, with no byte count.
    reply = with_crc(bytes((1, 0x0F, 0x00, 0x02, 0x00, 0x04)))
    board = board_with([reply])
    board.write_channels(3, [True, False, True, True])
    frame = bytes(board._serial.written)
    assert frame[:7] == bytes.fromhex("01 0F 00 02 00 04 01"), frame.hex(" ")
    assert frame[7] == 0b0000_1101, bin(frame[7])
    assert board.state[2:6] == [True, False, True, True]


def test_all_off_writes_every_channel_in_one_transaction():
    reply = with_crc(bytes((1, 0x0F, 0x00, 0x00, 0x00, 0x10)))
    board = board_with([reply])
    board.state = [True] * RelayBoard.CHANNELS
    board.all_off()
    frame = bytes(board._serial.written)
    assert frame[4:6] == bytes.fromhex("00 10"), frame.hex(" ")  # 16 coils
    assert frame[6] == 0x02 and frame[7] == 0x00 and frame[8] == 0x00
    assert not any(board.state)


# -- reading coils ----------------------------------------------------------


def test_read_channels_parses_the_bitmap():
    reply = with_crc(bytes.fromhex("01 01 02 FF 00"))
    board = board_with([reply])
    states = board.read_channels(16)
    assert states == [True] * 8 + [False] * 8, states
    assert board.state == states


def test_read_channels_handles_a_partial_byte():
    # Channels 1..3 closed -> 0b0000_0111
    reply = with_crc(bytes.fromhex("01 01 01 07"))
    board = board_with([reply])
    assert board.read_channels(3) == [True, True, True]


# -- error handling ---------------------------------------------------------


def test_modbus_exception_is_reported_with_its_code():
    reply = bytes.fromhex("01 85 02") + with_crc(bytes.fromhex("01 85 02"))[-2:]
    board = board_with([reply])
    try:
        board.set_channel(1, True)
    except RelayError as exc:
        assert "0x02" in str(exc), str(exc)
    else:
        raise AssertionError("an exception response must raise")


def test_bad_crc_in_a_reply_is_rejected():
    good = bytearray(ok_write_coil(0x0000, True))
    good[-1] ^= 0xFF
    board = board_with([bytes(good)])
    try:
        board.set_channel(1, True)
    except RelayError as exc:
        assert "CRC" in str(exc), str(exc)
    else:
        raise AssertionError("a corrupted reply must raise")


def test_silent_board_times_out_instead_of_hanging():
    board = board_with([])
    start = time.monotonic()
    try:
        board.set_channel(1, True)
    except RelayError as exc:
        assert "short response" in str(exc), str(exc)
    else:
        raise AssertionError("no reply must raise")
    assert time.monotonic() - start < 2.0


def test_reply_from_the_wrong_slave_is_rejected():
    reply = with_crc(bytes.fromhex("02 05 00 00 FF 00"))
    board = board_with([reply], slave_id=1)
    try:
        board.set_channel(1, True)
    except RelayError as exc:
        assert "slave" in str(exc), str(exc)
    else:
        raise AssertionError("a reply from another slave must raise")


def test_unopened_port_is_reported_clearly():
    board = RelayBoard(RelayCfg(port="", timeout=0.1))
    try:
        board.open()
    except ValueError as exc:
        assert "relay port" in str(exc)
    else:
        raise AssertionError("opening with no port must raise")


# -- pulse ------------------------------------------------------------------


def test_pulse_releases_the_channel_afterwards():
    on_reply = ok_write_coil(0x0000, True)
    off_reply = ok_write_coil(0x0000, False)
    board = board_with([on_reply, off_reply])
    board.pulse(1, 60)
    assert board.state[0] is True
    time.sleep(0.35)
    assert board.state[0] is False, "the channel must be released"
    last = bytes(board._serial.written)[-8:]
    assert last[:6] == bytes.fromhex("01 05 00 00 00 00"), last.hex(" ")


def test_cancel_all_pulses_leaves_the_channel_closed():
    board = board_with([ok_write_coil(0x0000, True)])
    board.pulse(1, 5000)
    board.cancel_all_pulses()
    time.sleep(0.1)
    assert board.state[0] is True, "cancelling a timer is not the same as opening"


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
