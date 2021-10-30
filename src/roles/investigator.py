from __future__ import annotations

import re
import random
import itertools
import typing
import math
from collections import defaultdict

from src import channels, users
from src.functions import get_players, get_all_players, get_main_role, get_reveal_role, get_target
from src.decorators import command
from src.containers import UserList, UserSet, UserDict, DefaultUserDict
from src.messages import messages
from src.status import try_misdirection, try_exchange
from src.events import Event, event_listener
from src.cats import Neutral, Wolfteam

if typing.TYPE_CHECKING:
    from src.dispatcher import MessageDispatcher
    from src.gamestate import GameState
    from src.users import User
    from typing import Optional

INVESTIGATED = UserSet()

@command("id", chan=False, pm=True, playing=True, silenced=True, phases=("day",), roles=("investigator",))
def investigate(wrapper: MessageDispatcher, message: str):
    """Investigate two players to determine their relationship to each other."""
    if wrapper.source in INVESTIGATED:
        wrapper.pm(messages["already_investigated"])
        return
    pieces = re.split(" +", message)
    if len(pieces) == 1:
        wrapper.pm(messages["investigator_help"])
        return
    var = wrapper.game_state
    target1 = pieces[0]
    target2 = pieces[1]
    target1 = get_target(wrapper, target1, not_self_message="no_investigate_self")
    target2 = get_target(wrapper, target2, not_self_message="no_investigate_self")
    if not target1 or not target2:
        return
    elif target1 is target2:
        wrapper.pm(messages["investigator_help"])
        return

    target1 = try_misdirection(var, wrapper.source, target1)
    target2 = try_misdirection(var, wrapper.source, target2)

    if try_exchange(var, wrapper.source, target1) or try_exchange(var, wrapper.source, target2):
        return

    t1role = get_main_role(var, target1)
    t2role = get_main_role(var, target2)

    evt = Event("investigate", {"role": t1role})
    evt.dispatch(var, wrapper.source, target1)
    t1role = evt.data["role"]

    evt = Event("investigate", {"role": t2role})
    evt.dispatch(var, wrapper.source, target2)
    t2role = evt.data["role"]

    # FIXME: make a standardized way of getting team affiliation, and make
    # augur and investigator both use it (and make it events-aware so other
    # teams can be added more easily)
    if t1role in Wolfteam:
        t1role = "red"
    elif t1role in Neutral:
        t1role = "grey"
    else:
        t1role = "blue"

    if t2role in Wolfteam:
        t2role = "red"
    elif t2role in Neutral:
        t2role = "grey"
    else:
        t2role = "blue"

    evt = Event("get_team_affiliation", {"same": (t1role == t2role)})
    evt.dispatch(evt, target1, target2)

    if evt.data["same"]:
        wrapper.pm(messages["investigator_results_same"].format(target1, target2))
    else:
        wrapper.pm(messages["investigator_results_different"].format(target1, target2))

    INVESTIGATED.add(wrapper.source)

@event_listener("del_player")
def on_del_player(evt: Event, var: GameState, player: User, all_roles: set[str], death_triggers: bool):
    INVESTIGATED.discard(player)

@event_listener("new_role")
def on_new_role(evt: Event, var: GameState, player: User, old_role: Optional[str]):
    if old_role == "investigator" and evt.data["role"] != "investigator":
        INVESTIGATED.discard(player)

@event_listener("send_role")
def on_send_role(evt: Event, var: GameState):
    ps = get_players(var)
    for inv in var.roles["investigator"]:
        pl = ps[:]
        random.shuffle(pl)
        pl.remove(inv)
        inv.send(messages["investigator_notify"], messages["players_list"].format(pl), sep="\n")

@event_listener("transition_night_begin")
def on_transition_night_begin(evt: Event, var: GameState):
    INVESTIGATED.clear()

@event_listener("reset")
def on_reset(evt: Event, var: GameState):
    INVESTIGATED.clear()

@event_listener("get_role_metadata")
def on_get_role_metadata(evt: Event, var: Optional[GameState], kind: str):
    if kind == "role_categories":
        evt.data["investigator"] = {"Village", "Spy", "Safe"}
