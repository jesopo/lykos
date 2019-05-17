import traceback
import threading
import string
import random
import json
import re

import urllib.request, urllib.parse

from collections import defaultdict

from oyoyo.client import IRCClient
from oyoyo.parse import parse_nick

import botconfig
import src.settings as var
import src
from src.utilities import *
from src.functions import get_players
from src.messages import messages
from src import channels, users, logger, errlog, events

adminlog = logger.logger("audit.log")

COMMANDS = defaultdict(list)
HOOKS = defaultdict(list)

# Error handler decorators and context managers

class _local(threading.local):
    handler = None
    level = 0

_local = _local()

# This is a mapping of stringified tracebacks to (link, uuid) tuples
# That way, we don't have to call in to the website everytime we have
# another error. If you ever need to delete pastes, do the following:
# $ curl -x DELETE https://ptpb.pw/<uuid>

_tracebacks = {}

class chain_exceptions:

    def __init__(self, exc, *, suppress_context=False):
        self.exc = exc
        self.suppress_context = suppress_context

    def __enter__(self):
        return self

    def __exit__(self, exc, value, tb):
        if exc is value is tb is None:
            return False

        value.__context__ = self.exc
        value.__suppress_context__ = self.suppress_context
        self.exc = value
        return True

    @property
    def traceback(self):
        return "".join(traceback.format_exception(type(self.exc), self.exc, self.exc.__traceback__))

class print_traceback:

    def __enter__(self):
        _local.level += 1
        return self

    def __exit__(self, exc_type, exc_value, tb):
        if exc_type is exc_value is tb is None:
            _local.level -= 1
            return False

        if not issubclass(exc_type, Exception):
            _local.level -= 1
            return False

        if _local.level > 1:
            _local.level -= 1
            return False # the outermost caller should handle this

        variables = ["", None]

        if _local.handler is None:
            _local.handler = chain_exceptions(exc_value)

        if var.TRACEBACK_VERBOSITY > 0:
            word = "\nLocal variables from frame #{0} (in {1}):\n"
            variables.append(None)
            frames = []

            while tb is not None:
                if tb.tb_next is not None and tb.tb_frame.f_locals.get("_ignore_locals_") or not tb.tb_frame.f_locals:
                    frames.append(None)
                else:
                    frames.append(tb.tb_frame)
                tb = tb.tb_next

            if var.TRACEBACK_VERBOSITY < 2:
                word = "Local variables from innermost frame (in {1}):\n"
                frames = [frames[-1]]

            with _local.handler:
                for i, frame in enumerate(frames, start=1):
                    if frame is None:
                        continue
                    variables.append(word.format(i, frame.f_code.co_name))
                    for name, value in frame.f_locals.items():
                        variables.append("{0} = {1!r}".format(name, value))

            if len(variables) > 3:
                variables.append("\n")
                if var.TRACEBACK_VERBOSITY > 1:
                    variables[2] = "Local variables in all frames (most recent call last):"
                else:
                    variables[2] = ""
            else:
                variables[2] = "No local variables found in all frames."

        variables[1] = _local.handler.traceback

        if not botconfig.PASTEBIN_ERRORS or channels.Main is not channels.Dev:
            channels.Main.send(messages["error_log"])
        if botconfig.PASTEBIN_ERRORS and channels.Dev is not None:
            message = [messages["error_log"]]

            link_uuid = _tracebacks.get("\n".join(variables))
            if link_uuid is None:
                bot_id = re.sub(r"[^A-Za-z0-9-]", "-", users.Bot.nick)
                bot_id = re.sub(r"--+", "-", bot_id).strip("-")

                rand_id = "".join(random.sample(string.ascii_letters + string.digits, 8))

                api_url = "https://ptpb.pw/~{0}-error-{1}".format(bot_id, rand_id)

                data = None
                with _local.handler:
                    req = urllib.request.Request(api_url, urllib.parse.urlencode({
                            "c": "\n".join(variables),  # contents
                        }).encode("utf-8", "replace"))

                    req.add_header("Accept", "application/json")
                    resp = urllib.request.urlopen(req)
                    data = json.loads(resp.read().decode("utf-8"))

                if data is None: # couldn't fetch the link
                    message.append(messages["error_pastebin"])
                    variables[1] = _local.handler.traceback # an error happened; update the stored traceback
                else:
                    link, uuid = _tracebacks["\n".join(variables)] = (data["url"] + "/pytb", data.get("uuid"))
                    message.append(link)
                    if uuid is None: # if there's no uuid, the paste already exists and we don't have it
                        message.append("(Already reported by another instance)")
                    else:
                        message.append("(uuid: {0})".format(uuid))

            else:
                link, uuid = link_uuid
                message.append(link)
                if uuid is None:
                    message.append("(Previously reported)")
                else:
                    message.append("(uuid: {0}-...)".format(uuid[:8]))

            channels.Dev.send(" ".join(message), prefix=botconfig.DEV_PREFIX)

        errlog("\n".join(variables))

        _local.level -= 1
        if not _local.level: # outermost caller; we're done here
            _local.handler = None

        return True # a true return value tells the interpreter to swallow the exception

class handle_error:

    def __new__(cls, func=None, *, instance=None):
        if isinstance(func, cls) and instance is func.instance: # already decorated
            return func

        if isinstance(func, cls):
            func = func.func

        self = super().__new__(cls)
        self.instance = instance
        self.func = func
        return self

    def __get__(self, instance, owner):
        if instance is not self.instance:
            return type(self)(self.func, instance=instance)
        return self

    def __call__(*args, **kwargs):
        _ignore_locals_ = True
        self, *args = args
        if self.instance is not None:
            args = [self.instance] + args
        with print_traceback():
            return self.func(*args, **kwargs)

class command:
    def __init__(self, *commands, flag=None, owner_only=False, chan=True, pm=False,
                 playing=False, silenced=False, phases=(), roles=(), users=None,
                 exclusive=False):

        # the "d" flag indicates it should only be enabled in debug mode
        if flag == "d" and not botconfig.DEBUG_MODE:
            return

        self.commands = frozenset(commands)
        self.flag = flag
        self.owner_only = owner_only
        self.chan = chan
        self.pm = pm
        self.playing = playing
        self.silenced = silenced
        self.phases = phases
        self.roles = roles
        self.users = users # iterable of users that can use the command at any time (should be a mutable object)
        self.func = None
        self.aftergame = False
        self.name = commands[0]
        self.alt_allowed = bool(flag or owner_only)
        self.exclusive = exclusive

        alias = False
        self.aliases = []

        if var.DISABLED_COMMANDS.intersection(commands):
            return # command is disabled, do not add to COMMANDS

        for name in commands:
            if exclusive and name in COMMANDS:
                raise ValueError("exclusive command already exists for {0}".format(name))

            for func in COMMANDS[name]:
                if func.owner_only != owner_only or func.flag != flag:
                    raise ValueError("unmatching access levels for {0}".format(func.name))
                if func.exclusive:
                    raise ValueError("exclusive command already exists for {0}".format(name))

            COMMANDS[name].append(self)
            if name in botconfig.ALLOWED_ALT_CHANNELS_COMMANDS:
                self.alt_allowed = True
            if name in getattr(botconfig, "OWNERS_ONLY_COMMANDS", ()):
                self.owner_only = True
            if alias:
                self.aliases.append(name)
            alias = True

        if playing: # Don't restrict to owners or allow in alt channels
            self.owner_only = False
            self.alt_allowed = False

    def __call__(self, func):
        if isinstance(func, command):
            func = func.func
        self.func = func
        self.__doc__ = func.__doc__
        return self

    @handle_error
    def caller(self, var, wrapper, message):
        _ignore_locals_ = True
        if (not self.pm and wrapper.private) or (not self.chan and wrapper.public):
            return # channel or PM command that we don't allow

        if wrapper.public and target is not channels.Main and not (self.flag or self.owner_only):
            if "" in self.commands or not self.alt_allowed:
                return # commands not allowed in alt channels

        if "" in self.commands:
            self.func(var, wrapper, rest)
            return

        if self.phases and var.PHASE not in self.phases:
            return

        if self.playing and (user not in get_players() or user in var.DISCONNECTED):
            return

        for role in self.roles:
            if user in var.ROLES[role]:
                break
        else:
            if (self.users is not None and user not in self.users) or self.roles:
                return

        if self.silenced and src.status.is_silent(var, user):
            wrapper.pm(messages["silenced"])
            return

        if self.roles or (self.users is not None and user in self.users):
            self.func(var, wrapper, rest) # don't check restrictions for role commands
            # Role commands might end the night if it's nighttime
            if var.PHASE == "night":
                from src.wolfgame import chk_nightdone
                chk_nightdone()
            return

        if self.owner_only:
            if user.is_owner():
                adminlog(chan, rawnick, self.name, rest)
                self.func(var, wrapper, rest)
                return

            wrapper.pm(messages["not_owner"])
            return

        temp = user.lower()

        flags = var.FLAGS[temp.rawnick] + var.FLAGS_ACCS[temp.account] # TODO: add flags handling to User

        if self.flag and (user.is_admin() or user.is_owner()):
            adminlog(chan, rawnick, self.name, rest)
            return self.func(var, wrapper, rest)

        denied_commands = var.DENY[temp.rawnick] | var.DENY_ACCS[temp.account] # TODO: add denied commands handling to User

        if self.commands & denied_commands:
            wrapper.pm(messages["invalid_permissions"])
            return

        if self.flag:
            if self.flag in flags:
                adminlog(chan, rawnick, self.name, rest)
                self.func(var, wrapper, rest)
                return

            wrapper.pm(messages["not_an_admin"])
            return

        self.func(var, wrapper, rest)

class hook:
    def __init__(self, name, hookid=-1):
        self.name = name
        self.hookid = hookid
        self.func = None

        HOOKS[name].append(self)

    def __call__(self, func):
        if isinstance(func, hook):
            self.func = func.func
        else:
            self.func = func
        self.__doc__ = self.func.__doc__
        return self

    @handle_error
    def caller(self, *args, **kwargs):
        _ignore_locals_ = True
        return self.func(*args, **kwargs)

    @staticmethod
    def unhook(hookid):
        for each in list(HOOKS):
            for inner in list(HOOKS[each]):
                if inner.hookid == hookid:
                    HOOKS[each].remove(inner)
            if not HOOKS[each]:
                del HOOKS[each]

class event_listener:
    def __init__(self, event, priority=5):
        self.event = event
        self.priority = priority
        self.func = None

    def __call__(self, *args, **kwargs):
        if self.func is None:
            func = args[0]
            if isinstance(func, event_listener):
                func = func.func
            self.func = handle_error(func)
            events.add_listener(self.event, self.func, self.priority)
            self.__doc__ = self.func.__doc__
            return self
        else:
            return self.func(*args, **kwargs)

    def remove(self):
        events.remove_listener(self.event, self.func, self.priority)

# vim: set sw=4 expandtab:
