from __future__ import annotations

import platform
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

import serial
import serial.tools.list_ports

KORAD_VID = 0x0416
KORAD_PID = 0x5011

BAUD_RATES = [9600, 19200, 38400, 57600, 115200]
BAUD_INDEX_TO_RATE = {0: 9600, 1: 19200, 2: 38400, 3: 57600, 4: 115200}

VALID_MODES = ("CC", "CV", "CR", "CW", "SHORT")
MODE_TO_SCPI = {
    "CC": "CC",
    "CV": "CV",
    "CR": "CR",
    "CW": "CW",
    "SHORT": "SHORt",
}

_FLOAT_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


class ProtocolError(RuntimeError):
    """Raised when the device returns an unexpected or unparseable response."""


@dataclass
class PortInfo:
    device: str
    description: str
    hwid: str
    is_korad: bool = False

    @property
    def label(self) -> str:
        tag = " [RS-KEL2050 / Korad]" if self.is_korad else ""
        desc = self.description or "Serial Port"
        return f"{self.device} - {desc}{tag}"


@dataclass
class DeviceLimits:
    v_min: float = 0.1000
    v_max: float = 80.000
    i_min: float = 0.0000
    i_max: float = 85.000
    r_min: float = 0.2000
    r_max: float = 7500.0
    p_min: float = 0.0100
    p_max: float = 1200.1


@dataclass
class DeviceStatus:
    beep: bool
    baud_index: int
    baud_rate: int
    key_lock: bool
    ext_trigger: bool
    remote_comp: bool
    reverse_polarity: bool
    raw: str = ""


@dataclass
class Snapshot:
    mode: str
    input_on: bool
    v_meas: float
    i_meas: float
    p_meas: float
    temp_c: float
    v_set: float
    i_set: float
    r_set: float
    p_set: float
    status: DeviceStatus


def parse_float_with_unit(text: str) -> float:
    """Parse numeric responses that may include units like '10.895V', '50.200OHM', '27.0'."""
    match = _FLOAT_RE.search(text.strip())
    if not match:
        raise ProtocolError(f"Non-numeric response: {text!r}")
    return float(match.group(0))


def parse_bool_response(text: str) -> bool:
    norm = text.strip().upper()
    if norm in ("1", "ON", "TRUE"):
        return True
    if norm in ("0", "OFF", "FALSE"):
        return False
    raise ProtocolError(f"Unexpected boolean response: {text!r}")


def list_serial_ports() -> list[PortInfo]:
    """List serial ports cross-platform, prioritizing /dev/cu.* on macOS and Korad VID/PID."""
    system = platform.system()
    ports: list[PortInfo] = []
    for p in serial.tools.list_ports.comports():
        dev = p.device
        if system == "Darwin" and dev.startswith("/dev/tty."):
            continue
        is_korad = (p.vid == KORAD_VID and p.pid == KORAD_PID) or (
            "VID_0416" in (p.hwid or "").upper() and "PID_5011" in (p.hwid or "").upper()
        )
        ports.append(
            PortInfo(
                device=dev,
                description=p.description or "",
                hwid=p.hwid or "",
                is_korad=is_korad,
            )
        )
    ports.sort(key=lambda item: (not item.is_korad, item.device))
    return ports


class KEL2050Client:
    """Thread-safe SCPI serial client for RS PRO RS-KEL2050 / Korad KEL2000 DC Electronic Load."""

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 0.5,
        line_ending: str = "\n",
        encoding: str = "ascii",
        io_logger: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.line_ending = line_ending
        self.encoding = encoding
        self.io_logger = io_logger
        self._serial: Optional[serial.Serial] = None
        self._io_lock = threading.Lock()
        self._log_local = threading.local()

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def open(self) -> None:
        if self.is_open:
            return
        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self.timeout,
            write_timeout=self.timeout,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        time.sleep(0.05)
        self._serial.reset_input_buffer()

    def close(self) -> None:
        if self._serial is not None:
            try:
                if self._serial.is_open:
                    self._serial.close()
            finally:
                self._serial = None

    def _ser(self) -> serial.Serial:
        if self._serial is None or not self._serial.is_open:
            raise RuntimeError("Device not connected.")
        return self._serial

    @contextmanager
    def _muted_log(self, enabled: bool = True) -> Iterator[None]:
        prev = getattr(self._log_local, "muted", False)
        if enabled:
            self._log_local.muted = True
        try:
            yield
        finally:
            self._log_local.muted = prev

    def _log(self, direction: str, payload: str) -> None:
        if getattr(self._log_local, "muted", False):
            return
        if self.io_logger is not None:
            try:
                self.io_logger(direction, payload)
            except Exception:
                pass

    def _cmd_bytes(self, command: str) -> bytes:
        return command.strip().encode(self.encoding, errors="ignore") + self.line_ending.encode(self.encoding)

    def write(self, command: str) -> None:
        cmd = command.strip()
        with self._io_lock:
            ser = self._ser()
            self._log("TX", cmd)
            ser.write(self._cmd_bytes(cmd))
            ser.flush()
            time.sleep(0.02)

    def query_raw(self, command: str, max_bytes: int = 512) -> bytes:
        cmd = command.strip()
        with self._io_lock:
            ser = self._ser()
            ser.reset_input_buffer()
            self._log("TX", cmd)
            ser.write(self._cmd_bytes(cmd))
            ser.flush()
            data = ser.read_until(expected=b"\n", size=max_bytes)
            if not data:
                data = ser.read(max_bytes)
            return data

    def query_text(self, command: str) -> str:
        data = self.query_raw(command)
        if not data:
            raise TimeoutError(f"No response for command: {command}")
        text = data.decode(self.encoding, errors="ignore").strip("\r\n\x00 ")
        self._log("RX", text)
        if not text:
            raise ProtocolError(f"Empty text response for command: {command}")
        return text

    def query_float(self, command: str) -> float:
        text = self.query_text(command)
        return parse_float_with_unit(text)

    # --- Identification & Memory ---
    def idn(self) -> str:
        return self.query_text("*IDN?")

    def save_memory(self, slot: int) -> None:
        if not 1 <= slot <= 100:
            raise ValueError("Memory slot must be between 1 and 100.")
        self.write(f"*SAV {slot}")

    def recall_memory(self, slot: int) -> None:
        if not 1 <= slot <= 100:
            raise ValueError("Memory slot must be between 1 and 100.")
        self.write(f"*RCL {slot}")

    def trigger(self) -> None:
        self.write("*TRG")

    # --- System & Status ---
    @staticmethod
    def decode_status(raw: str) -> DeviceStatus:
        parts = [p.strip() for p in raw.split(",") if p.strip() != ""]
        if len(parts) < 6:
            raise ProtocolError(f"Invalid :STAT? response: {raw!r}")
        vals = [int(p) for p in parts[:6]]
        baud_idx = vals[1]
        return DeviceStatus(
            beep=bool(vals[0]),
            baud_index=baud_idx,
            baud_rate=BAUD_INDEX_TO_RATE.get(baud_idx, 115200),
            key_lock=bool(vals[2]),
            ext_trigger=bool(vals[3]),
            remote_comp=bool(vals[4]),
            reverse_polarity=bool(vals[5]),
            raw=raw,
        )

    def get_status(self) -> DeviceStatus:
        return self.decode_status(self.query_text(":STAT?"))

    def set_beep(self, enabled: bool) -> None:
        self.write(f":SYST:BEEP {'ON' if enabled else 'OFF'}")

    def set_lock(self, enabled: bool) -> None:
        self.write(f":SYST:LOCK {'ON' if enabled else 'OFF'}")

    def set_remote_comp(self, enabled: bool) -> None:
        self.write(f":SYST:COMP {'ON' if enabled else 'OFF'}")

    def set_ext_trigger(self, mode: int) -> None:
        if mode not in (0, 1, 2):
            raise ValueError("External trigger mode must be 0 (OFF), 1 (TRIG), or 2 (REMOTE).")
        self.write(f":SYST:EXIT {mode}")

    # --- Input & Mode ---
    def get_input(self) -> bool:
        return parse_bool_response(self.query_text(":INP?"))

    def set_input(self, on: bool) -> None:
        self.write(f":INP {'ON' if on else 'OFF'}")

    def get_mode(self) -> str:
        raw = self.query_text(":FUNC?").upper()
        if raw.startswith("SHOR"):
            return "SHORT"
        return raw

    def set_mode(self, mode: str) -> None:
        norm = mode.strip().upper()
        if norm not in MODE_TO_SCPI:
            raise ValueError(f"Unsupported mode: {mode}. Expected one of {VALID_MODES}.")
        self.write(f":FUNC {MODE_TO_SCPI[norm]}")

    # --- Limits & Setpoints ---
    def get_limits(self, silent: bool = True) -> DeviceLimits:
        with self._muted_log(silent):
            return DeviceLimits(
                v_min=self.query_float(":VOLT:LOW?"),
                v_max=self.query_float(":VOLT:UPP?"),
                i_min=self.query_float(":CURR:LOW?"),
                i_max=self.query_float(":CURR:UPP?"),
                r_min=self.query_float(":RES:LOW?"),
                r_max=self.query_float(":RES:UPP?"),
                p_min=self.query_float(":POW:LOW?"),
                p_max=self.query_float(":POW:UPP?"),
            )

    def get_voltage(self) -> float:
        return self.query_float(":VOLT?")

    def set_voltage(self, volts: float) -> None:
        self.write(f":VOLT {volts:.4f}V")

    def set_voltage_upper(self, volts: float) -> None:
        self.write(f":VOLT:UPP {volts:.3f}V")

    def get_current(self) -> float:
        return self.query_float(":CURR?")

    def set_current(self, amps: float) -> None:
        self.write(f":CURR {amps:.4f}A")

    def set_current_upper(self, amps: float) -> None:
        self.write(f":CURR:UPP {amps:.3f}A")

    def get_resistance(self) -> float:
        return self.query_float(":RES?")

    def set_resistance(self, ohms: float) -> None:
        self.write(f":RES {ohms:.4f}OHM")

    def set_resistance_upper(self, ohms: float) -> None:
        self.write(f":RES:UPP {ohms:.2f}OHM")

    def get_power(self) -> float:
        return self.query_float(":POW?")

    def set_power(self, watts: float) -> None:
        self.write(f":POW {watts:.4f}W")

    def set_power_upper(self, watts: float) -> None:
        self.write(f":POW:UPP {watts:.2f}W")


    # --- Live Measurements ---
    def measure_voltage(self) -> float:
        return self.query_float(":MEAS:VOLT?")

    def measure_current(self) -> float:
        return self.query_float(":MEAS:CURR?")

    def measure_power(self) -> float:
        return self.query_float(":MEAS:POW?")

    def measure_temp(self) -> float:
        return self.query_float(":MEAS:TEMP?")

    def read_snapshot(self, silent: bool = True, include_setpoints: bool = True) -> Snapshot:
        with self._muted_log(silent):
            status = self.get_status()
            mode = self.get_mode()
            input_on = self.get_input()
            v_meas = self.measure_voltage()
            i_meas = self.measure_current()
            p_meas = self.measure_power()
            temp_c = self.measure_temp()
            if include_setpoints:
                v_set = self.get_voltage()
                i_set = self.get_current()
                r_set = self.get_resistance()
                p_set = self.get_power()
            else:
                v_set = float("nan")
                i_set = float("nan")
                r_set = float("nan")
                p_set = float("nan")
        return Snapshot(
            mode=mode,
            input_on=input_on,
            v_meas=v_meas,
            i_meas=i_meas,
            p_meas=p_meas,
            temp_c=temp_c,
            v_set=v_set,
            i_set=i_set,
            r_set=r_set,
            p_set=p_set,
            status=status,
        )

    # --- Dynamic & Battery Helpers ---
    def get_dynamic(self) -> str:
        return self.query_text(":DYN?")

    def set_dynamic_cc(
        self,
        slope_rise: float,
        slope_fall: float,
        i1: float,
        i2: float,
        freq_hz: float,
        duty_pct: float,
    ) -> None:
        self.write(
            f":DYN 2,{slope_rise:.4f}A/uS,{slope_fall:.4f}A/uS,"
            f"{i1:.4f}A,{i2:.4f}A,{freq_hz:.2f}HZ,{duty_pct:.2f}%"
        )

    def set_dynamic_cv(self, v1: float, v2: float, freq_hz: float, duty_pct: float) -> None:
        self.write(f":DYN 1,{v1:.4f}V,{v2:.4f}V,{freq_hz:.2f}HZ,{duty_pct:.2f}%")

    def set_dynamic_cr(self, r1: float, r2: float, freq_hz: float, duty_pct: float) -> None:
        self.write(f":DYN 3,{r1:.4f}OHM,{r2:.4f}OHM,{freq_hz:.2f}HZ,{duty_pct:.2f}%")

    def set_dynamic_cw(self, p1: float, p2: float, freq_hz: float, duty_pct: float) -> None:
        self.write(f":DYN 4,{p1:.3f}W,{p2:.3f}W,{freq_hz:.2f}HZ,{duty_pct:.2f}%")

    def set_battery(
        self,
        slot: int,
        i_range: float,
        i_discharge: float,
        v_cutoff: float,
        cap_cutoff_ah: float,
        time_limit_min: int,
    ) -> None:
        if not 1 <= slot <= 7:
            raise ValueError("Battery slot must be between 1 and 7.")
        self.write(
            f":BATT {slot},{i_range:.1f}A,{i_discharge:.3f}A,"
            f"{v_cutoff:.3f}V,{cap_cutoff_ah:.3f}AH,{time_limit_min}M"
        )

    def recall_battery(self, slot: int) -> str:
        if not 1 <= slot <= 7:
            raise ValueError("Battery slot must be between 1 and 7.")
        self.write(f":RCL:BATT {slot}")
        return self.query_text(":RCL:BATT?")

    def get_battery_time(self) -> str:
        return self.query_text(":BATT:TIM?")

    def get_battery_capacity(self) -> str:
        return self.query_text(":BATT:CAP?")


