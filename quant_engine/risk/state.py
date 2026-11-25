"""Persistent risk state: kill switch, open risk, period baselines, and latched loss lockouts.

Every read-modify-write runs inside `transaction()`, which holds an exclusive lock for its whole
duration. Two entry requests therefore cannot both spend the same risk headroom, whether they come
from two threads or two processes sharing a FileStateStore.

A state that cannot be read or parsed raises StateUnavailable. The engine treats that as "kill
switch engaged" for entries and ignores it for exits.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Optional, Protocol

from quant_engine.risk.config import to_decimal

STATE_VERSION = 1
KILL_SWITCH_HISTORY_LIMIT = 200
POSITION_STATUSES = ("PENDING", "OPEN")


class StateUnavailable(RuntimeError):
    pass


@dataclass
class PositionRecord:
    position_id: str
    strategy: str
    symbol: str
    side: str
    mode: str
    status: str
    qty: int
    entry_price: Decimal
    stop_price: Optional[Decimal]
    multiplier: int
    cost_per_unit: Decimal
    risk_per_unit: Decimal
    exposure_per_unit: Decimal
    opened_at: str

    @property
    def planned_risk(self) -> Decimal:
        return self.risk_per_unit * self.qty

    @property
    def exposure(self) -> Decimal:
        return self.exposure_per_unit * self.qty

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("entry_price", "stop_price", "cost_per_unit", "risk_per_unit", "exposure_per_unit"):
            data[key] = None if data[key] is None else format(data[key], "f")
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "PositionRecord":
        if not isinstance(data, dict):
            raise StateUnavailable("position record is not an object")
        try:
            record = cls(
                position_id=_str(data["position_id"]),
                strategy=_str(data["strategy"]),
                symbol=_str(data["symbol"]),
                side=_choice(data["side"], ("LONG", "SHORT")),
                mode=_choice(data["mode"], ("PAPER", "PREFLIGHT", "LIVE")),
                status=_choice(data["status"], POSITION_STATUSES),
                qty=_count(data["qty"], 1),
                entry_price=_money(data["entry_price"], positive=True),
                stop_price=None if data.get("stop_price") is None else _money(data["stop_price"], positive=True),
                multiplier=_count(data["multiplier"], 1),
                cost_per_unit=_money(data["cost_per_unit"]),
                risk_per_unit=_money(data["risk_per_unit"], positive=True),
                exposure_per_unit=_money(data["exposure_per_unit"], positive=True),
                opened_at=_str(data["opened_at"]),
            )
        except KeyError as exc:
            raise StateUnavailable(f"position record is missing {exc.args[0]}") from exc
        return record


@dataclass
class RiskState:
    kill_switch: dict[str, Any] = field(default_factory=lambda: {
        "engaged": False, "reason": None, "actor": None, "changed_at": None})
    kill_switch_history: list[dict[str, Any]] = field(default_factory=list)
    positions: dict[str, PositionRecord] = field(default_factory=dict)
    baselines: dict[str, Optional[dict[str, str]]] = field(default_factory=lambda: {"day": None, "week": None})
    lockouts: dict[str, Optional[str]] = field(default_factory=lambda: {"day": None, "week": None})

    @property
    def open_risk(self) -> Decimal:
        return sum((p.planned_risk for p in self.positions.values()), Decimal(0))

    @property
    def gross_exposure(self) -> Decimal:
        return sum((p.exposure for p in self.positions.values()), Decimal(0))

    def to_json(self) -> str:
        payload = {
            "version": STATE_VERSION,
            "kill_switch": self.kill_switch,
            "kill_switch_history": self.kill_switch_history[-KILL_SWITCH_HISTORY_LIMIT:],
            "positions": {key: record.to_dict() for key, record in sorted(self.positions.items())},
            "baselines": self.baselines,
            "lockouts": self.lockouts,
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "RiskState":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise StateUnavailable("risk state is not valid JSON") from exc
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            raise StateUnavailable("risk state has an unknown version")
        kill = data.get("kill_switch")
        if not isinstance(kill, dict) or not isinstance(kill.get("engaged"), bool):
            raise StateUnavailable("risk state kill switch is unreadable")
        history = data.get("kill_switch_history", [])
        if not isinstance(history, list):
            raise StateUnavailable("risk state kill switch history is unreadable")
        raw_positions = data.get("positions", {})
        if not isinstance(raw_positions, dict):
            raise StateUnavailable("risk state positions are unreadable")
        positions = {}
        for key, raw in raw_positions.items():
            record = PositionRecord.from_dict(raw)
            if record.position_id != key:
                raise StateUnavailable("risk state position id does not match its key")
            positions[key] = record
        baselines = data.get("baselines", {"day": None, "week": None})
        lockouts = data.get("lockouts", {"day": None, "week": None})
        if not isinstance(baselines, dict) or not isinstance(lockouts, dict):
            raise StateUnavailable("risk state periods are unreadable")
        for name in ("day", "week"):
            baseline = baselines.get(name)
            if baseline is not None:
                if not isinstance(baseline, dict) or not isinstance(baseline.get("period"), str):
                    raise StateUnavailable("risk state baseline is unreadable")
                _money(baseline.get("equity"), positive=True)
            lock = lockouts.get(name)
            if lock is not None and not isinstance(lock, str):
                raise StateUnavailable("risk state lockout is unreadable")
        return cls(
            kill_switch={"engaged": kill["engaged"], "reason": kill.get("reason"),
                         "actor": kill.get("actor"), "changed_at": kill.get("changed_at")},
            kill_switch_history=list(history),
            positions=positions,
            baselines={"day": baselines.get("day"), "week": baselines.get("week")},
            lockouts={"day": lockouts.get("day"), "week": lockouts.get("week")},
        )


def _str(value: Any) -> str:
    if not isinstance(value, str) or value == "":
        raise StateUnavailable("risk state has a missing text field")
    return value


def _choice(value: Any, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise StateUnavailable(f"risk state value {value!r} is not one of {allowed}")
    return value


def _count(value: Any, lo: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < lo:
        raise StateUnavailable("risk state has an invalid count")
    return value


def _money(value: Any, positive: bool = False) -> Decimal:
    try:
        number = to_decimal(value, "risk state amount")
    except ValueError as exc:
        raise StateUnavailable(str(exc)) from exc
    if number < 0 or (positive and number == 0):
        raise StateUnavailable("risk state has an invalid amount")
    return number


class StateStore(Protocol):
    def transaction(self) -> contextlib.AbstractContextManager[RiskState]: ...

    def read(self) -> RiskState: ...


class InMemoryStateStore:
    """Process-local store for tests and dry runs. Round-trips through JSON like the file store."""

    def __init__(self, initial: Optional[str] = None):
        self._lock = threading.RLock()
        self._text = initial if initial is not None else RiskState().to_json()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[RiskState]:
        with self._lock:
            state = RiskState.from_json(self._text)
            yield state
            self._text = state.to_json()

    def read(self) -> RiskState:
        with self._lock:
            return RiskState.from_json(self._text)

    def corrupt(self, text: str) -> None:
        with self._lock:
            self._text = text


class FileStateStore:
    """JSON file guarded by an fcntl lock on a sidecar `.lock` file. Linux and macOS only.

    Writes go to a temp file in the same directory, are fsynced, then renamed over the state file,
    so a crash mid-write leaves the previous state intact. A missing file is a fresh state.
    A file that exists but does not parse is StateUnavailable, not a fresh state.

    This persists across restarts on one machine. It does not persist across Vercel serverless
    instances; that needs a Supabase-backed store (see docs/RISK_ENGINE.md).
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._thread_lock = threading.RLock()

    @contextlib.contextmanager
    def _locked(self, exclusive: bool) -> Iterator[None]:
        with self._thread_lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(self.lock_path, "a+")
            except OSError as exc:
                raise StateUnavailable(f"risk state lock unavailable: {type(exc).__name__}") from exc
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _load(self) -> RiskState:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return RiskState()
        except OSError as exc:
            raise StateUnavailable(f"risk state unreadable: {type(exc).__name__}") from exc
        return RiskState.from_json(text)

    def _save(self, state: RiskState) -> None:
        text = state.to_json()
        try:
            fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    out.write(text)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            raise StateUnavailable(f"risk state write failed: {type(exc).__name__}") from exc

    @contextlib.contextmanager
    def transaction(self) -> Iterator[RiskState]:
        with self._locked(exclusive=True):
            state = self._load()
            yield state
            self._save(state)

    def read(self) -> RiskState:
        with self._locked(exclusive=False):
            return self._load()
