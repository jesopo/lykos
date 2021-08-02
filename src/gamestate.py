from __future__ import annotations
import typing
import time

from src.containers import UserSet, UserDict, UserList
from src.messages import messages
from src.cats import All
from src import channels, config

if typing.TYPE_CHECKING:
    from src.gamemodes import GameMode
    from src.users import User
    from typing import FrozenSet, Set, Tuple, Any, Dict, Optional

__all__ = ["GameState", "PregameState", "IngameState", "set_gamemode"]

def set_gamemode(var: GameState, arg: str) -> bool:
    from src.gamemodes import GAME_MODES, InvalidModeException
    modeargs = arg.split("=", 1)

    modeargs = [a.strip() for a in modeargs]
    if modeargs[0] in GAME_MODES:
        md = modeargs.pop(0)
        try:
            gm = GAME_MODES[md][0](*modeargs)
            gm.startup()
            var.current_mode = gm
            return True
        except InvalidModeException as e:
            channels.Main.send(f"Invalid mode: {e}")
            return False

    channels.Main.send(messages["game_mode_not_found"].format(modeargs[0]))
    return False

class PregameState:
    def __init__(self):
        self.players = UserList()
        self.current_phase: str = "join"
        self.game_id: float = time.time()
        self.next_phase: Optional[str] = None
        # Note: current_mode is None for all but the !start machinery
        self.current_mode: GameMode = None

    @property
    def in_game(self):
        return False

    def teardown(self):
        pass

class GameState:
    def __init__(self, pregame_state: PregameState):
        self.setup_started: bool = False
        self.setup_completed: bool = False
        self._torndown: bool = False
        self.current_mode: GameMode = pregame_state.current_mode
        self.game_settings: Dict[str, Any] = {}
        self.game_id: float = pregame_state.game_id
        self.players = pregame_state.players
        self.roles: UserDict[str, UserSet] = UserDict()
        self._original_roles: UserDict[Tuple[str, UserSet]] = None
        self.main_roles: UserDict[User, str] = UserDict()
        self._original_main_roles: UserDict[User, str] = None
        self.final_roles: UserDict[User, str] = UserDict()
        self._rolestats: Set[FrozenSet[Tuple[str, int]]] = set()
        self.current_phase: str = pregame_state.current_phase
        self.next_phase: Optional[str] = None
        self.night_count: int = 0
        self.day_count: int = 0

    def begin_setup(self):
        if self.setup_completed:
            raise RuntimeError("GameState.setup() called while already setup")
        if self._torndown:
            raise RuntimeError("cannot setup a used-up GameState")
        for role in All:
            self.roles[role] = UserSet()
        self.setup_started = True

    def finish_setup(self):
        if self.setup_completed:
            raise RuntimeError("GameState.setup() called while already setup")
        if self._torndown:
            raise RuntimeError("cannot setup a used-up GameState")
        self._original_roles = UserDict((x, y.copy()) for x, y in self.roles.items())
        self._original_main_roles = self.main_roles.copy()
        self.setup_completed = True

    def teardown(self):
        self.roles.clear()
        self._original_roles.clear()
        self._rolestats.clear()
        self.current_mode.teardown()
        self._torndown = True

    def _get_value(self, key: str) -> Any:
        # we don't actually need to complete setup before this can be used
        if not self.setup_started or not self.current_mode:
            raise RuntimeError("Current game state has not been setup")
        if self._torndown:
            raise RuntimeError("Current game state is no longer valid")
        if key in self.game_settings:
            return self.game_settings[key]
        return getattr(self.current_mode.CUSTOM_SETTINGS, key)

    @property
    def in_game(self):
        return self.setup_completed and not self._torndown

    def begin_phase_transition(self, phase: str):
        if self.next_phase is not None:
            raise RuntimeError("already in phase transition")
        self.next_phase = phase

    def end_phase_transition(self):
        if self.next_phase is None:
            raise RuntimeError("not in phase transition")
        # this is a bit convoluted, but this lets external code plug in their own phases
        setattr(self, f"{self.next_phase}_count", getattr(self, f"{self.next_phase}_count") + 1)

        self.current_phase = self.next_phase
        self.next_phase = None

    @property
    def in_phase_transition(self):
        return self.next_phase is not None

    @property
    def original_roles(self):
        # we store the data in a UserDict so it gets dynamically updated
        # but we want to return a regular dict (and a regular underlying set)
        # since this is meant to be used then discarded
        # this is also read-only to prevent code from modifying it
        if not self.setup_completed:
            raise RuntimeError("Current game state has not finished setup")
        return {name: set(value) for name, value in self._original_roles.items()}

    @property
    def original_main_roles(self):
        if not self.setup_completed:
            raise RuntimeError("Current game state has not finished setup")
        return dict(self._original_main_roles)

    @property
    def abstain_enabled(self) -> bool:
        return self._get_value("abstain_enabled")

    @property
    def limit_abstain(self) -> bool:
        return self._get_value("limit_abstain")

    @property
    def self_lynch_allowed(self) -> bool:
        return self._get_value("self_lynch_allowed")

    @property
    def default_role(self) -> str:
        return self._get_value("default_role")

    @property
    def hidden_role(self) -> str:
        return self._get_value("hidden_role")

    @property
    def start_with_day(self) -> bool:
        return self._get_value("start_with_day")

    @property
    def always_pm_role(self) -> bool:
        return self._get_value("always_pm_role")

    @property
    def role_reveal(self) -> str:
        return self._get_value("role_reveal")

    @property
    def stats_type(self) -> str:
        return self._get_value("stats_type")

    @property
    def day_time_limit(self) -> int:
        try:
            return self._get_value("day_time_limit")
        except AttributeError:
            return config.Main.get("timers.day.limit")

    @property
    def day_time_warn(self) -> int:
        try:
            return self._get_value("day_time_warn")
        except AttributeError:
            return config.Main.get("timers.day.warn")

    @property
    def short_day_time_limit(self) -> int:
        try:
            return self._get_value("short_day_time_limit")
        except AttributeError:
            return config.Main.get("timers.shortday.limit")

    @property
    def short_day_time_warn(self) -> int:
        try:
            return self._get_value("short_day_time_warn")
        except AttributeError:
            return config.Main.get("timers.shortday.warn")

    @property
    def night_time_limit(self) -> int:
        try:
            return self._get_value("night_time_limit")
        except AttributeError:
            return config.Main.get("timers.night.limit")

    @property
    def night_time_warn(self) -> int:
        try:
            return self._get_value("night_time_warn")
        except AttributeError:
            return config.Main.get("timers.night.warn")

    def get_role_stats(self) -> FrozenSet[FrozenSet[Tuple[str, int]]]:
        return frozenset(self._rolestats)

    def set_role_stats(self, value) -> None:
        self._rolestats.clear()
        self._rolestats.update(value)