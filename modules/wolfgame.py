# Copyright (c) 2011, Jimmy Cao All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# Redistributions of source code must retain the above copyright notice, this
# list of conditions and the following disclaimer.  Redistributions in binary
# form must reproduce the above copyright notice, this list of conditions and
# the following disclaimer in the documentation and/or other materials provided
# with the distribution.  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS
# AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING,
# BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER
# OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from oyoyo.parse import parse_nick
import settings.wolfgame as var
import botconfig
import traceback
from tools import decorators
from datetime import datetime, timedelta
import threading
import copy
import time
import re
import sys
import os
import math
import random
import subprocess
import signal
from tools import logger

debuglog = logger("debug.log", write=False, display=False) # will be True if in debug mode
errlog = logger("errors.log")
plog = logger(None) #use this instead of print so that logs have timestamps

BOLD = "\u0002"

COMMANDS = {}
HOOKS = {}

is_admin = var.is_admin
is_owner = var.is_owner

cmd = decorators.generate(COMMANDS)
hook = decorators.generate(HOOKS, raw_nick=True, permissions=False)

# Game Logic Begins:

var.LAST_PING = None  # time of last ping
var.LAST_STATS = None
var.LAST_VOTES = None
var.LAST_ADMINS = None
var.LAST_GSTATS = None
var.LAST_PSTATS = None
var.LAST_TIME = None
var.LAST_START = {}
var.LAST_WAIT = {}

var.USERS = {}

var.PINGING = False
var.ADMIN_PINGING = False
var.ROLES = {"person" : []}
var.SPECIAL_ROLES = {}
var.ORIGINAL_ROLES = {}
var.PLAYERS = {}
var.DCED_PLAYERS = {}
var.ADMIN_TO_PING = None
var.AFTER_FLASTGAME = None
var.TIMERS = {}

var.ORIGINAL_SETTINGS = {}

var.LAST_SAID_TIME = {}

var.GAME_START_TIME = datetime.now()  # for idle checker only
var.CAN_START_TIME = 0
var.GRAVEYARD_LOCK = threading.RLock()
var.STARTED_DAY_PLAYERS = 0

var.DISCONNECTED = {}  # players who got disconnected

var.OPPED = False  # Keeps track of whether the bot is opped

var.BITTEN = {}
var.BITTEN_ROLES = {}

if botconfig.DEBUG_MODE:
    var.NIGHT_TIME_LIMIT = 0 # 120
    var.NIGHT_TIME_WARN = 0 # 90
    var.DAY_TIME_LIMIT = 0 # 720
    var.DAY_TIME_WARN = 0 # 600
    var.SHORT_DAY_LIMIT = 0 # 520
    var.SHORT_DAY_WARN = 0 # 400
    var.KILL_IDLE_TIME = 0 # 300
    var.WARN_IDLE_TIME = 0 # 180
    var.JOIN_TIME_LIMIT = 0
    var.LEAVE_STASIS_PENALTY = 1
    var.IDLE_STASIS_PENALTY = 1
    var.PART_STASIS_PENALTY = 1


def connect_callback(cli):
    SIGUSR1 = getattr(signal, "SIGUSR1", None)
    SIGUSR2 = getattr(signal, "SIGUSR2", None)

    def sighandler(signum, frame):
        if signum in (signal.SIGINT, signal.SIGTERM):
            forced_exit(cli, "<console>", botconfig.CHANNEL, "")
        elif signum == SIGUSR1:
            restart_program(cli, "<console>", botconfig.CHANNEL, "")
        elif signum == SIGUSR2:
            plog("Scheduling aftergame restart")
            aftergame(cli, "<console>", "frestart")

    signal.signal(signal.SIGINT, sighandler)
    signal.signal(signal.SIGTERM, sighandler)

    if SIGUSR1:
        signal.signal(SIGUSR1, sighandler)

    if SIGUSR2:
        signal.signal(SIGUSR2, sighandler)

    to_be_devoiced = []
    cmodes = []

    @hook("quietlist", hookid=294)
    def on_quietlist(cli, server, botnick, channel, q, quieted, by, something):
        if re.match(".+\!\*@\*", quieted):  # only unquiet people quieted by bot
            cmodes.append(("-q", quieted))

    @hook("whospcrpl", hookid=294)
    def on_whoreply(cli, server, nick, ident, cloak, _, user, status, acc):
        if user in var.USERS: return  # Don't add someone who is already there
        if user == botconfig.NICK:
            cli.nickname = user
            cli.ident = ident
            cli.hostmask = cloak
        if acc == "0":
            acc = "*"
        if "+" in status:
            to_be_devoiced.append(user)
        newstat = ""
        for stat in status:
            if not stat in var.MODES_PREFIXES:
                continue
            newstat += var.MODES_PREFIXES[stat]
        var.USERS[user] = dict(cloak=cloak,account=acc,inchan=True,modes=set(newstat),moded=set())

    @hook("endofwho", hookid=294)
    def afterwho(*args):
        for nick in to_be_devoiced:
            cmodes.append(("-v", nick))
        # devoice all on connect

    #bot can be tricked into thinking it's still opped by doing multiple modes at once
    @hook("mode", hookid=294)
    def on_give_me_ops(cli, nick, chan, modeaction, target="", *other):
        if chan != botconfig.CHANNEL:
            return
        if modeaction == "+o" and target == botconfig.NICK:
            var.OPPED = True
            if botconfig.NICK in var.USERS:
                var.USERS[botconfig.NICK]["modes"].add("o")

            if var.PHASE == "none":
                @hook("quietlistend", 294)
                def on_quietlist_end(cli, svr, nick, chan, *etc):
                    if chan == botconfig.CHANNEL:
                        mass_mode(cli, cmodes, ["-m"])

                cli.mode(botconfig.CHANNEL, "q")  # unquiet all
        elif modeaction == "-o" and target == botconfig.NICK:
            var.OPPED = False
            cli.msg("ChanServ", "op " + botconfig.CHANNEL)


    cli.who(botconfig.CHANNEL, "%uhsnfa")

@hook("mode")
def check_for_modes(cli, rnick, chan, modeaction, *target):
    nick = parse_nick(rnick)[0]
    if chan != botconfig.CHANNEL:
        return
    oldpref = ""
    trgt = ""
    keeptrg = False
    target = list(target)
    if target and target != [botconfig.NICK]:
        while modeaction:
            if len(modeaction) > 1:
                prefix = modeaction[0]
                change = modeaction[1]
            else:
                prefix = oldpref
                change = modeaction[0]
            if not keeptrg:
                if target:
                    trgt = target.pop(0)
                else:
                    trgt = "" # Last item, no target
            keeptrg = False
            if not prefix in ("-", "+"):
                change = prefix
                prefix = oldpref
            else:
                oldpref = prefix
            modeaction = modeaction[modeaction.index(change)+1:]
            if change in var.MODES_NOSET:
                keeptrg = True
            if prefix == "-" and change in var.MODES_ONLYSET:
                keeptrg = True
            if change not in var.MODES_PREFIXES.values():
                continue
            if trgt in var.USERS:
                if prefix == "+":
                    var.USERS[trgt]["modes"].add(change)
                    if change in var.USERS[trgt]["moded"]:
                        var.USERS[trgt]["moded"].remove(change)
                elif change in var.USERS[trgt]["modes"]:
                    var.USERS[trgt]["modes"].remove(change)
    # Only sync modes if a server changed modes because
    # 1) human ops probably know better
    # 2) other bots might start a fight over modes
    # 3) recursion; we see our own mode changes.
    if "!" not in rnick:
        sync_modes(cli)

#completes a partial nickname or string from a list
def complete_match(string, matches):
    num_matches = 0
    bestmatch = string
    for possible in matches:
        if string == possible:
            return string, 1
        if possible.startswith(string):
            bestmatch = possible
            num_matches += 1
    if num_matches != 1:
        return None, num_matches
    else:
        return bestmatch, 1

#wrapper around complete_match() used for roles
def get_victim(cli, nick, victim, self_in_list = False):
    if not victim:
        cli.notice(nick, "Not enough parameters")
        return
    pl = [x for x in var.list_players() if x != nick or self_in_list]
    pll = [x.lower() for x in pl]

    tempvictim, num_matches = complete_match(victim.lower(), pll)
    if not tempvictim:
        #ensure messages about not being able to act on yourself work
        if num_matches == 0 and nick.lower().startswith(victim.lower()):
            return nick
        cli.notice(nick, "\u0002{0}\u0002 is currently not playing.".format(victim))
        return
    return pl[pll.index(tempvictim)] #convert back to normal casing

def mass_mode(cli, md_param, md_plain):
    """ Example: mass_mode(cli, [('+v', 'asdf'), ('-v','wobosd')], ['-m']) """
    lmd = len(md_param)  # store how many mode changes to do
    if md_param:
        for start_i in range(0, lmd, var.MODELIMIT):  # 4 mode-changes at a time
            if start_i + var.MODELIMIT > lmd:  # If this is a remainder (mode-changes < 4)
                z = list(zip(*md_param[start_i:]))  # zip this remainder
                ei = lmd % var.MODELIMIT  # len(z)
            else:
                z = list(zip(*md_param[start_i:start_i+var.MODELIMIT])) # zip four
                ei = var.MODELIMIT # len(z)
            # Now z equal something like [('+v', '-v'), ('asdf', 'wobosd')]
            arg1 = "".join(md_plain) + "".join(z[0])
            arg2 = " ".join(z[1])  # + " " + " ".join([x+"!*@*" for x in z[1]])
            cli.mode(botconfig.CHANNEL, arg1, arg2)
    else:
            cli.mode(botconfig.CHANNEL, "".join(md_plain))

def pm(cli, target, message):  # message either privmsg or notice, depending on user settings
    if is_fake_nick(target) and botconfig.DEBUG_MODE:
        debuglog("Would message fake nick {0}: {1}".format(target, message))
        return

    if is_user_notice(target):
        cli.notice(target, message)
        return

    cli.msg(target, message)

def reset_settings():
    for attr in list(var.ORIGINAL_SETTINGS.keys()):
        setattr(var, attr, var.ORIGINAL_SETTINGS[attr])
    dict.clear(var.ORIGINAL_SETTINGS)

def reset_modes_timers(cli):
    # Reset game timers
    for x, timr in var.TIMERS.items():
        timr[0].cancel()
    var.TIMERS = {}

    # Reset modes
    cmodes = []
    for plr in var.list_players():
        cmodes.append(("-v", plr))
    if var.AUTO_TOGGLE_MODES:
        for plr in var.USERS:
            if not "moded" in var.USERS[plr]:
                continue
            for mode in var.USERS[plr]["moded"]:
                cmodes.append(("+"+mode, plr))
            var.USERS[plr]["modes"].update(var.USERS[plr]["moded"])
            var.USERS[plr]["moded"] = set()
    if var.QUIET_DEAD_PLAYERS:
        for deadguy in var.DEAD:
            if not is_fake_nick(deadguy):
                cmodes.append(("-q", deadguy+"!*@*"))
    mass_mode(cli, cmodes, ["-m"])

def reset():
    var.PHASE = "none" # "join", "day", or "night"
    var.GAME_ID = 0
    var.DEAD = []
    var.ROLES = {"person" : []}
    var.JOINED_THIS_GAME = [] # keeps track of who already joined this game at least once (cloaks)
    var.JOINED_THIS_GAME_ACCS = [] # same, except accounts
    var.NO_LYNCH = []
    var.FGAMED = False
    var.CURRENT_GAMEMODE = "default"
    var.GAMEMODE_VOTES = {} #list of players who have used !game

    reset_settings()

    dict.clear(var.LAST_SAID_TIME)
    dict.clear(var.PLAYERS)
    dict.clear(var.DCED_PLAYERS)
    dict.clear(var.DISCONNECTED)
reset()

def make_stasis(nick, penalty):
    if nick in var.USERS:
        cloak = var.USERS[nick]["cloak"]
        acc = var.USERS[nick]["account"]
    else:
        return # Can't do it
    if not acc or acc == "*":
        acc = None
    if not cloak and not acc:
        return # Can't do it, either
    if acc:
        if penalty == 0:
            if acc in var.STASISED_ACCS:
                del var.STASISED_ACCS[acc]
                var.set_stasis_acc(acc, 0)
        else:
            var.STASISED_ACCS[acc] += penalty
            var.set_stasis_acc(acc, var.STASISED_ACCS[acc])
    if penalty == 0:
        if cloak in var.STASISED:
            del var.STASISED[cloak]
            var.set_stasis(cloak, 0)
    else:
        var.STASISED[cloak] += penalty
        var.set_stasis(cloak, var.STASISED[cloak])

@cmd("fsync", admin_only=True, pm=True)
def fsync(cli, nick, chan, rest):
    """Makes the bot apply the currently appropriate channel modes."""
    sync_modes(cli)

def sync_modes(cli):
    voices = []
    pl = var.list_players()
    for nick, u in var.USERS.items():
        if nick in pl and 'v' not in u.get('modes', set()):
            voices.append(("+v", nick))
        elif nick not in pl and 'v' in u.get('modes', set()):
            voices.append(("-v", nick))
    if var.PHASE in ("day", "night"):
        other = ["+m"]
    else:
        other = ["-m"]

    mass_mode(cli, voices, other)

@cmd("fdie", "fbye", admin_only=True, pm=True)
def forced_exit(cli, nick, chan, rest):  # Admin Only
    """Forces the bot to close."""

    if var.PHASE in ("day", "night"):
        #ignore all errors that prevent the bot from stopping
        try:
            stop_game(cli)
        except:
            errlog(traceback.format_exc())
            cli.msg(chan, "An error has occurred and has been logged.")
            reset_modes_timers(cli)
            reset()
    else:
        reset_modes_timers(cli)
        reset()

    cli.quit("Forced quit from "+nick)



@cmd("frestart", admin_only=True, pm=True)
def restart_program(cli, nick, chan, rest):
    """Restarts the bot."""

    try:
        if var.PHASE in ("day", "night"):
            try:
                stop_game(cli)
            except:
                errlog(traceback.format_exc())
                cli.msg(chan, "An error has occurred and has been logged.")
                reset_modes_timers(cli)
                reset()
        else:
            reset_modes_timers(cli)
            reset()

        cli.quit("Forced restart from "+nick)
        raise SystemExit
    finally:
        plog("RESTARTING")
        python = sys.executable
        if rest.strip().lower() == "debugmode":
            os.execl(python, python, sys.argv[0], "--debug")
        elif rest.strip().lower() == "normalmode":
            os.execl(python, python, sys.argv[0], "--normal")
        elif rest.strip().lower() == "verbosemode":
            os.execl(python, python, sys.argv[0], "--verbose")
        else:
            os.execl(python, python, *sys.argv)

@cmd("ping", pm=True)
def pinger(cli, nick, chan, rest):
    """Pings the channel to get people's attention. Rate-limited."""

    if var.PHASE in ('night','day') or chan == nick: # PM
        #cli.notice(nick, "You cannot use this command while a game is running.")
        cli.notice(nick, 'Pong!')
        return

    if (var.LAST_PING and
        var.LAST_PING + timedelta(seconds=var.PING_WAIT) > datetime.now()):
        cli.notice(nick, ("This command is rate-limited. " +
                          "Please wait a while before using it again."))
        return

    if var.PINGING:
        return
    var.PINGING = True
    TO_PING = []



    @hook("whoreply", hookid=800)
    def on_whoreply(cli, server, dunno, chan, dunno1,
                    cloak, dunno3, user, status, dunno4):
        if not var.PINGING: return
        if user in (botconfig.NICK, nick): return  # Don't ping self.

        if ('G' not in status and '+' not in status and not
            is_user_stasised(user)[0] and not is_user_away(user)):

            TO_PING.append(user)


    @hook("endofwho", hookid=800)
    def do_ping(*args):
        if not var.PINGING: return

        TO_PING.sort(key=lambda x: x.lower())

        if TO_PING:
            var.LAST_PING = datetime.now()
            cli.msg(chan, "PING! "+" ".join(TO_PING))

            minimum = datetime.now() + timedelta(seconds=var.PING_MIN_WAIT)
            if not var.CAN_START_TIME or var.CAN_START_TIME < minimum:
               var.CAN_START_TIME = minimum
        else:
            cli.msg(chan, "There is noone currently available to be pinged.")

        var.PINGING = False
        decorators.unhook(HOOKS, 800)

    cli.who(chan)


@cmd("simple", raw_nick=True, pm=True)
def mark_simple_notify(cli, nick, chan, rest):
    """Makes the bot give you simple role instructions, in case you are familiar with the roles."""

    nick, _, __, cloak = parse_nick(nick)
    if nick in var.USERS:
        cloak = var.USERS[nick]["cloak"]
        acc = var.USERS[nick]["account"]
    else:
        acc = None
    if not acc or acc == "*":
        acc = None

    if acc: # Prioritize account
        if acc in var.SIMPLE_NOTIFY_ACCS:
            var.SIMPLE_NOTIFY_ACCS.remove(acc)
            var.remove_simple_rolemsg_acc(acc)
            if cloak in var.SIMPLE_NOTIFY:
                var.SIMPLE_NOTIFY.remove(cloak)
                var.remove_simple_rolemsg(cloak)

            cli.notice(nick, "You now no longer receive simple role instructions.")
            return

        var.SIMPLE_NOTIFY_ACCS.append(acc)
        var.add_simple_rolemsg_acc(acc)
    elif var.ACCOUNTS_ONLY:
        cli.notice(nick, "You are not logged in to NickServ.")
        return

    else: # Not logged in, fall back to hostmask
        if cloak in var.SIMPLE_NOTIFY:
            var.SIMPLE_NOTIFY.remove(cloak)
            var.remove_simple_rolemsg(cloak)

            cli.notice(nick, "You now no longer receive simple role instructions.")
            return

        var.SIMPLE_NOTIFY.append(cloak)
        var.add_simple_rolemsg(cloak)

    cli.notice(nick, "You now receive simple role instructions.")

def is_user_simple(nick):
    if nick in var.USERS:
        cloak = var.USERS[nick]["cloak"]
        acc = var.USERS[nick]["account"]
    else:
        return False
    if acc and acc != "*":
        if acc in var.SIMPLE_NOTIFY_ACCS:
            return True
        return False
    elif cloak in var.SIMPLE_NOTIFY and not var.ACCOUNTS_ONLY:
        return True
    return False

@cmd("notice", raw_nick=True, pm=True)
def mark_prefer_notice(cli, nick, chan, rest):
    """Makes the bot NOTICE you for every interaction."""

    nick, _, __, cloak = parse_nick(nick)
    if nick in var.USERS:
        cloak = var.USERS[nick]["cloak"]
        acc = var.USERS[nick]["account"]
    else:
        acc = None
    if not acc or acc == "*":
        acc = None

    if acc: # Do things by account if logged in
        if acc in var.PREFER_NOTICE_ACCS:
            var.PREFER_NOTICE_ACCS.remove(acc)
            var.remove_prefer_notice_acc(acc)
            if cloak in var.PREFER_NOTICE:
                var.PREFER_NOTICE.remove(cloak)
                var.remove_prefer_notice(cloak)

            cli.notice(nick, "Gameplay interactions will now use PRIVMSG for you.")
            return

        var.PREFER_NOTICE_ACCS.append(acc)
        var.add_prefer_notice_acc(acc)
    elif var.ACCOUNTS_ONLY:
        cli.notice(nick, "You are not logged in to NickServ.")
        return

    else: # Not logged in
        if cloak in var.PREFER_NOTICE:
            var.PREFER_NOTICE.remove(cloak)
            var.remove_prefer_notice(cloak)

            cli.notice(nick, "Gameplay interactions will now use PRIVMSG for you.")
            return

        var.PREFER_NOTICE.append(cloak)
        var.add_prefer_notice(cloak)

    cli.notice(nick, "The bot will now always NOTICE you.")

def is_user_notice(nick):
    if nick in var.USERS and var.USERS[nick]["account"] and var.USERS[nick]["account"] != "*":
        if var.USERS[nick]["account"] in var.PREFER_NOTICE_ACCS:
            return True
    if nick in var.USERS and var.USERS[nick]["cloak"] in var.PREFER_NOTICE and not var.ACCOUNTS_ONLY:
        return True
    return False

@cmd("away", raw_nick=True, pm=True)
def away(cli, nick, chan, rest):
    """Use this to activate your away status (so you aren't pinged)."""
    nick, _, _, cloak = parse_nick(nick)
    if var.OPT_IN_PING:
        if not rest: # don't want to trigger on unrelated messages
            cli.notice(nick, "Please use {0}in and {0}out to opt in or out of the ping list.".format(botconfig.CMD_CHAR))
        return
    if nick in var.USERS:
        cloak = var.USERS[nick]["cloak"]
        acc = var.USERS[nick]["account"]
    else:
        acc = None
    prefix = botconfig.CMD_CHAR
    if not acc or acc == "*":
        acc = None
    if acc: # Do it all by accounts if logged in
        if acc in var.AWAY_ACCS:
            cli.notice(nick, ("You are already marked as away. Use {0}back "
                           "to unset your away status.").format(prefix))

            return

        var.AWAY_ACCS.append(acc)
        var.add_away_acc(acc)
    elif var.ACCOUNTS_ONLY:
        cli.notice(nick, "You are not logged in to NickServ.")
        return

    else:
        if cloak in var.AWAY:
            cli.notice(nick, ("You are already marked as away. Use {0}back "
                           "to unset your away status.").format(prefix))

            return

        var.AWAY.append(cloak)
        var.add_away(cloak)

    cli.notice(nick, "You are now marked as away.")

@cmd("back", raw_nick=True, pm=True)
def back_from_away(cli, nick, chan, rest):
    """Unsets your away status."""
    nick, _, _, cloak = parse_nick(nick)
    if var.OPT_IN_PING:
        if not rest:
            cli.notice(nick, "Please use {0}in and {0}out to opt in or out of the ping list.".format(botconfig.CMD_CHAR))
        return
    if nick in var.USERS:
        cloak = var.USERS[nick]["cloak"]
        acc = var.USERS[nick]["account"]
    else:
        acc = None
    if not acc or acc == "*":
        acc = None
    if acc: # Priority to accounts
        if acc not in var.AWAY_ACCS:
            cli.notice(nick, "You are not marked as away.")
            return

        var.AWAY_ACCS.remove(acc)
        var.remove_away_acc(acc)
    elif var.ACCOUNTS_ONLY:
        cli.notice(nick, "You are not logged in to NickServ.")
        return

    else:
        if cloak not in var.AWAY:
            cli.notice(nick, "You are not marked as away.")
            return

        var.AWAY.remove(cloak)
        var.remove_away(cloak)

    cli.notice(nick, "You are no longer marked as away.")


@cmd("in", raw_nick=True, pm=True)
def get_in(cli, nick, chan, rest):
    """Puts yourself in the ping list."""
    nick, _, _, cloak = parse_nick(nick)
    if not var.OPT_IN_PING:
        if not rest:
            cli.notice(nick, "Please use {0}away and {0}back to mark yourself as away or back.".format(botconfig.CMD_CHAR))
        return
    if nick in var.USERS:
        cloak = var.USERS[nick]["cloak"]
        acc = var.USERS[nick]["account"]
    else:
        acc = None
    if not acc or acc == "*":
        acc = None
    if acc:
        if acc in var.PING_IN_ACCS:
            cli.notice(nick, "You are already on the list.")
            return

        var.PING_IN_ACCS.append(acc)
        var.add_ping_acc(acc)
    elif var.ACCOUNTS_ONLY:
        cli.notice(nick, "You are not logged in to NickServ.")
        return

    else:
        if cloak in var.PING_IN:
            cli.notice(nick, "You are already on the list.")
            return

        var.PING_IN.append(cloak)
        var.add_ping(cloak)

    cli.notice(nick, "You are now on the list.")

@cmd("out", raw_nick=True, pm=True)
def get_out(cli, nick, chan, rest):
    """Removes yourself from the ping list."""
    nick, _, _, cloak = parse_nick(nick)
    if not var.OPT_IN_PING:
        if not rest:
            cli.notice(nick, "Please use {0}away and {0}back to mark yourself as away or back.".format(botconfig.CMD_CHAR))
        return
    if nick in var.USERS:
        cloak = var.USERS[nick]["cloak"]
        acc = var.USERS[nick]["account"]
    else:
        acc = None
    if not acc or acc == "*":
        acc = None
    if acc:
        if acc not in var.PING_IN_ACCS:
            cli.notice(nick, "You are not on the list.")
            return

        var.PING_IN_ACCS.remove(acc)
        var.remove_ping_acc(acc)
    elif var.ACCOUNTS_ONLY:
        cli.notice(nick, "You are not logged in to NickServ.")
        return
    else:
        if cloak not in var.PING_IN:
            cli.notice(nick, "You are not on the list.")
            return

        var.PING_IN.remove(cloak)
        var.remove_ping(cloak)

    cli.notice(nick, "You are no longer in the list.")

def is_user_away(nick):
    if nick in var.USERS:
        cloak = var.USERS[nick]["cloak"]
        acc = var.USERS[nick]["account"]
    else:
        return False
    if acc and acc != "*":
        if var.OPT_IN_PING:
            if acc in var.PING_IN_ACCS:
                return False
            return True
        if acc in var.AWAY_ACCS:
            return True
        return False
    if var.ACCOUNTS_ONLY:
        return False

    if var.OPT_IN_PING:
        if cloak in var.PING_IN:
            return False
        return True
    if cloak in var.AWAY:
        return True
    return False

@cmd("fping", admin_only=True)
def fpinger(cli, nick, chan, rest):
    """Pings the channel to get people's attention, ignoring the rate limit."""
    var.LAST_PING = None
    pinger(cli, nick, chan, rest)


@cmd("join", "j", none=True, join=True)
def join(cli, nick, chan, rest):
    """Either starts a new game of Werewolf or joins an existing game that has not started yet."""
    if var.ACCOUNTS_ONLY:
        if nick in var.USERS and (not var.USERS[nick]["account"] or var.USERS[nick]["account"] == "*"):
            cli.notice(nick, "You are not logged in to NickServ.")
            return
    join_player(cli, nick, chan)

def join_player(cli, player, chan, who = None, forced = False):
    if who is None:
        who = player

    pl = var.list_players()
    if chan != botconfig.CHANNEL:
        return

    if not var.OPPED:
        cli.notice(who, "Sorry, I'm not opped in {0}.".format(chan))
        cli.msg("ChanServ", "op " + botconfig.CHANNEL)
        return

    if is_user_stasised(player)[0] and not forced:
        cli.notice(who, "Sorry, but {0} in stasis for {1} game{2}.".format(
            "you are" if player == who else player + " is", is_user_stasised(player)[1],
            "s" if is_user_stasised(player)[1] != 1 else ""))
        return

    if player in var.USERS:
        cloak = var.USERS[player]["cloak"]
        acc = var.USERS[player]["account"]
    elif is_fake_nick(player) and botconfig.DEBUG_MODE:
        # fakenick
        cloak = None
        acc = None
    else:
        return # Not normal
    if not acc or acc == "*":
        acc = None
    cmodes = [("+v", player)]
    if var.PHASE == "none":

        if var.AUTO_TOGGLE_MODES and player in var.USERS and var.USERS[player]["modes"]:
            for mode in var.USERS[player]["modes"]:
                cmodes.append(("-"+mode, player))
            var.USERS[player]["moded"].update(var.USERS[player]["modes"])
            var.USERS[player]["modes"] = set()
        mass_mode(cli, cmodes, [])
        var.ROLES["person"].append(player)
        var.PHASE = "join"
        var.WAITED = 0
        var.GAME_ID = time.time()
        if cloak:
            var.JOINED_THIS_GAME.append(cloak)
        if acc:
            var.JOINED_THIS_GAME_ACCS.append(acc)
        var.CAN_START_TIME = datetime.now() + timedelta(seconds=var.MINIMUM_WAIT)
        cli.msg(chan, ('\u0002{0}\u0002 has started a game of Werewolf. '+
                      'Type "{1}join" to join. Type "{1}start" to start the game. '+
                      'Type "{1}wait" to increase the start wait time.').format(player, botconfig.CMD_CHAR))

        # Set join timer
        if var.JOIN_TIME_LIMIT and not botconfig.DEBUG_MODE:
            t = threading.Timer(var.JOIN_TIME_LIMIT, kill_join, [cli, chan])
            var.TIMERS['join'] = (t, time.time(), var.JOIN_TIME_LIMIT)
            t.daemon = True
            t.start()

    elif player in pl:
        cli.notice(who, "{0}'re already playing!".format("You" if who == player else "They"))
    elif len(pl) >= var.MAX_PLAYERS:
        cli.notice(who, "Too many players! Try again next time.")
    elif var.PHASE != "join":
        cli.notice(who, "Sorry, but the game is already running. Try again next time.")
    else:

        var.ROLES["person"].append(player)
        if not is_fake_nick(player) or not botconfig.DEBUG_MODE:
            if var.AUTO_TOGGLE_MODES and var.USERS[player]["modes"]:
                for mode in var.USERS[player]["modes"]:
                    cmodes.append(("-"+mode, player))
                var.USERS[player]["moded"].update(var.USERS[player]["modes"])
                var.USERS[player]["modes"] = set()
            mass_mode(cli, cmodes, [])
            cli.msg(chan, '\u0002{0}\u0002 has joined the game and raised the number of players to \u0002{1}\u0002.'.format(player, len(pl) + 1))
        if not is_fake_nick(player) and not cloak in var.JOINED_THIS_GAME and (not acc or not acc in var.JOINED_THIS_GAME_ACCS):
            # make sure this only happens once
            var.JOINED_THIS_GAME.append(cloak)
            if acc:
                var.JOINED_THIS_GAME_ACCS.append(acc)
            now = datetime.now()

            # add var.EXTRA_WAIT_JOIN to wait time
            if now > var.CAN_START_TIME:
                var.CAN_START_TIME = now + timedelta(seconds=var.EXTRA_WAIT_JOIN)
            else:
                var.CAN_START_TIME += timedelta(seconds=var.EXTRA_WAIT_JOIN)

            # make sure there's at least var.WAIT_AFTER_JOIN seconds of wait time left, if not add them
            if now + timedelta(seconds=var.WAIT_AFTER_JOIN) > var.CAN_START_TIME:
                var.CAN_START_TIME = now + timedelta(seconds=var.WAIT_AFTER_JOIN)

        var.LAST_STATS = None # reset
        var.LAST_GSTATS = None
        var.LAST_PSTATS = None
        var.LAST_TIME = None


def kill_join(cli, chan):
    pl = var.list_players()
    pl.sort(key=lambda x: x.lower())
    msg = 'PING! {0}'.format(", ".join(pl))
    reset_modes_timers(cli)
    reset()
    cli.msg(chan, msg)
    cli.msg(chan, 'The current game took too long to start and ' +
                  'has been canceled. If you are still active, ' +
                  'please join again to start a new game.')


@cmd("fjoin", admin_only=True, none=True, join=True)
def fjoin(cli, nick, chan, rest):
    """Forces someone to join a game."""
    noticed = False
    fake = False
    if not rest.strip():
        join_player(cli, nick, chan, forced=True)

    for a in re.split(" +",rest):
        a = a.strip()
        if not a:
            continue
        ul = list(var.USERS.keys())
        ull = [u.lower() for u in ul]
        if a.lower() not in ull:
            if not is_fake_nick(a) or not botconfig.DEBUG_MODE:
                if not noticed:  # important
                    cli.msg(chan, nick+(": You may only fjoin "+
                                        "people who are in this channel."))
                    noticed = True
                continue
        if not is_fake_nick(a):
            a = ul[ull.index(a.lower())]
        elif botconfig.DEBUG_MODE:
            fake = True
        if a != botconfig.NICK:
            join_player(cli, a.strip(), chan, forced=True, who=nick)
        else:
            cli.notice(nick, "No, that won't be allowed.")
    if fake:
        cli.msg(chan, "\u0002{0}\u0002 used fjoin and raised the number of players to \u0002{1}\u0002.".format(nick, len(var.list_players())))

@cmd("fleave", "fquit", admin_only=True, join=True, game=True)
def fleave(cli, nick, chan, rest):
    """Forces someone to leave the game."""
    if chan != botconfig.CHANNEL:
        return

    for a in re.split(" +",rest):
        a = a.strip()
        if not a:
            continue
        pl = var.list_players()
        pll = [x.lower() for x in pl]
        if a.lower() in pll:
            a = pl[pll.index(a.lower())]
        else:
            cli.msg(chan, nick+": That person is not playing.")
            return

        message = "\u0002{0}\u0002 is forcing \u0002{1}\u0002 to leave.".format(nick, a)
        if var.get_role(a) != "person" and var.ROLE_REVEAL:
            message += " Say goodbye to the \02{0}\02.".format(var.get_reveal_role(a))
        if var.PHASE == "join":
            lpl = len(var.list_players()) - 1
            if lpl == 0:
                message += " No more players remaining."
            else:
                message += " New player count: \u0002{0}\u0002".format(lpl)
        cli.msg(chan, message)

        del_player(cli, a, death_triggers = False)


@cmd("fstart", admin_only=True, join=True)
def fstart(cli, nick, chan, rest):
    """Forces the game to start immediately."""
    cli.msg(botconfig.CHANNEL, "\u0002{0}\u0002 has forced the game to start.".format(nick))
    start(cli, nick, botconfig.CHANNEL, forced = True)

@hook("kick")
def on_kicked(cli, nick, chan, victim, reason):
    if victim == botconfig.NICK:
        cli.join(chan)
        if chan == botconfig.CHANNEL:
            cli.msg("ChanServ", "op "+botconfig.CHANNEL)
    if var.AUTO_TOGGLE_MODES and victim in var.USERS:
        var.USERS[victim]["modes"] = set()
        var.USERS[victim]["moded"] = set()

@hook("account")
def on_account(cli, rnick, acc):
    nick, mode, user, cloak = parse_nick(rnick)
    chan = botconfig.CHANNEL
    if acc == "*" and var.LEAVE_ON_LOGOUT and var.PHASE in ("join", "day", "night") and nick in var.PLAYERS:
        cli.mode(chan, "-v", nick)
        leave(cli, "account", nick)
    if nick in var.USERS.keys():
        var.USERS[nick]["cloak"] = cloak
        var.USERS[nick]["account"] = acc
    if nick in var.DISCONNECTED.keys():
        if acc == var.DISCONNECTED[nick][0]:
            if nick in var.USERS and var.USERS[nick]["inchan"]:
                with var.GRAVEYARD_LOCK:
                    clk = var.DISCONNECTED[nick][1]
                    act = var.DISCONNECTED[nick][0]
                    if acc == act or (cloak == clk and not var.ACCOUNTS_ONLY):
                        cli.mode(chan, "+v", nick, nick+"!*@*")
                        del var.DISCONNECTED[nick]
                        var.LAST_SAID_TIME[nick] = datetime.now()
                        cli.msg(chan, "\02{0}\02 has returned to the village.".format(nick))
                        for r,rlist in var.ORIGINAL_ROLES.items():
                            if "(dced)"+nick in rlist:
                                rlist.remove("(dced)"+nick)
                                rlist.append(nick)
                                break
                        if nick in var.DCED_PLAYERS.keys():
                            var.PLAYERS[nick] = var.DCED_PLAYERS.pop(nick)

@cmd("stats", pm=True, game=True, join=True)
def stats(cli, nick, chan, rest):
    """Displays the player statistics."""

    pl = var.list_players()
    if var.PHASE in ("night", "day"):
        pl = [x for x in var.ALL_PLAYERS if x in pl]

    if nick != chan and (nick in pl or var.PHASE == "join"):
        # only do this rate-limiting stuff if the person is in game
        if (var.LAST_STATS and
            var.LAST_STATS + timedelta(seconds=var.STATS_RATE_LIMIT) > datetime.now()):
            cli.notice(nick, ("This command is rate-limited. " +
                              "Please wait a while before using it again."))
            return

        var.LAST_STATS = datetime.now()

    if len(pl) > 1:
        msg = '{0}: \u0002{1}\u0002 players: {2}'.format(nick,
            len(pl), ", ".join(pl))
    else:
        msg = '{0}: \u00021\u0002 player: {1}'.format(nick, pl[0])

    if nick == chan:
        pm(cli, nick, msg)
    else:
        if nick in pl or var.PHASE == "join":
            cli.msg(chan, msg)
        else:
            cli.notice(nick, msg)

    if var.PHASE == "join" or not var.ROLE_REVEAL:
        return

    message = []
    l1 = [k for k in var.ROLES.keys()
          if var.ROLES[k]]
    l2 = [k for k in var.ORIGINAL_ROLES.keys()
          if var.ORIGINAL_ROLES[k]]
    rs = set(l1+l2)
    rs = [role for role in var.role_order() if role in rs]

    # picky ordering: villager always last
    if var.DEFAULT_ROLE in rs:
        rs.remove(var.DEFAULT_ROLE)
    rs.append(var.DEFAULT_ROLE)


    amn_roles = {"amnesiac": 0}
    for amn in var.ORIGINAL_ROLES["amnesiac"]:
        if amn not in pl:
            continue

        amnrole = var.get_role(amn)
        if amnrole in ("village elder", "time lord"):
            amnrole = "villager"
        elif amnrole == "vengeful ghost":
            amnrole = var.DEFAULT_ROLE
        elif amnrole == "traitor" and var.HIDDEN_TRAITOR:
            amnrole = var.DEFAULT_ROLE
        if amnrole != "amnesiac":
            amn_roles["amnesiac"] += 1
            if amnrole in amn_roles:
                amn_roles[amnrole] -= 1
            else:
                amn_roles[amnrole] = -1

    bitten_roles = {}
    for bitten, role in var.BITTEN_ROLES.items():
        if role in bitten_roles:
            bitten_roles[role] += 1
        else:
            bitten_roles[role] = 1

    vb = "are"
    for role in rs:
        # only show actual roles
        if role in ("village elder", "time lord", "vengeful ghost") or role in var.TEMPLATE_RESTRICTIONS.keys():
            continue
        count = len(var.ROLES[role])
        if role == "traitor" and var.HIDDEN_TRAITOR:
            continue
        elif role == "lycan":
            count += len([p for p in var.CURED_LYCANS if p in var.ROLES["villager"]])
            count += bitten_roles["lycan"] if "lycan" in bitten_roles else 0
        elif role == var.DEFAULT_ROLE:
            if var.HIDDEN_TRAITOR:
                count += len(var.ROLES["traitor"])
                count += bitten_roles["traitor"] if "traitor" in bitten_roles else 0
            if var.DEFAULT_ROLE == "villager":
                count += len(var.ROLES["village elder"] + var.ROLES["time lord"] + var.ROLES["vengeful ghost"])
                count -= len([p for p in var.CURED_LYCANS if p in var.ROLES["villager"]])
                count += bitten_roles["village elder"] if "village elder" in bitten_roles else 0
                count += bitten_roles["time lord"] if "time lord" in bitten_roles else 0
                count += bitten_roles["vengeful ghost"] if "vengeful ghost" in bitten_roles else 0
            else:
                count += len(var.ROLES["vengeful ghost"])
                count += bitten_roles["vengeful ghost"] if "vengeful ghost" in bitten_roles else 0
            count += bitten_roles[var.DEFAULT_ROLE] if var.DEFAULT_ROLE in bitten_roles else 0
        elif role == "villager":
            count += len(var.ROLES["village elder"] + var.ROLES["time lord"])
            count -= len([p for p in var.CURED_LYCANS if p in var.ROLES["villager"]])
            count += bitten_roles["villager"] if "villager" in bitten_roles else 0
            count += bitten_roles["village elder"] if "village elder" in bitten_roles else 0
            count += bitten_roles["time lord"] if "time lord" in bitten_roles else 0
        elif role == "wolf":
            count -= sum(bitten_roles.values())
        else:
            count += bitten_roles[role] if role in bitten_roles else 0

        if role in amn_roles:
            count += amn_roles[role]

        if role == rs[0]:
            if count == 1:
                vb = "is"
            else:
                vb = "are"

        if count > 1 or count == 0:
            if count == 0 and len(var.ORIGINAL_ROLES[role]) == 0:
                continue
            message.append("\u0002{0}\u0002 {1}".format(count if count else "\u0002no\u0002", var.plural(role)))
        else:
            message.append("\u0002{0}\u0002 {1}".format(count, role))
    stats_mssg =  "{0}: It is currently {4}. There {3} {1}, and {2}.".format(nick,
                                                        ", ".join(message[0:-1]),
                                                        message[-1],
                                                        vb,
                                                        var.PHASE)
    if nick == chan:
        pm(cli, nick, stats_mssg)
    else:
        if nick in pl or var.PHASE == "join":
            cli.msg(chan, stats_mssg)
        else:
            cli.notice(nick, stats_mssg)

def hurry_up(cli, gameid, change):
    if var.PHASE != "day": return
    if gameid:
        if gameid != var.DAY_ID:
            return

    chan = botconfig.CHANNEL

    if not change:
        cli.msg(chan, ("\02As the sun sinks inexorably toward the horizon, turning the lanky pine " +
                      "trees into fire-edged silhouettes, the villagers are reminded that very little " +
                      "time remains for them to reach a decision; if darkness falls before they have done " +
                      "so, the majority will win the vote. No one will be lynched if there " +
                      "are no votes or an even split.\02"))
        return


    var.DAY_ID = 0

    pl = var.list_players()
    avail = len(pl) - len(var.WOUNDED) - len(var.ASLEEP)
    votesneeded = avail // 2 + 1
    not_lynching = len(var.NO_LYNCH)

    found_dup = False
    maxfound = (0, "")
    votelist = copy.deepcopy(var.VOTES)
    for votee, voters in votelist.items():
        numvotes = 0
        for v in var.IMPATIENT:
            if v in pl and v not in voters and v != votee:
                voters.append(v)
        for v in voters:
            weight = 1
            imp_count = sum([1 if p == v else 0 for p in var.IMPATIENT])
            pac_count = sum([1 if p == v else 0 for p in var.PACIFISTS])
            if pac_count > imp_count:
                weight = 0 # more pacifists than impatience totems
            elif imp_count == pac_count and v not in var.VOTES[votee]:
                weight = 0 # impatience and pacifist cancel each other out, so don't count impatience
            if v in var.ROLES["bureaucrat"] or v in var.INFLUENTIAL: # the two do not stack
                weight *= 2
            numvotes += weight
        if numvotes > maxfound[0]:
            maxfound = (numvotes, votee)
            found_dup = False
        elif numvotes == maxfound[0]:
            found_dup = True
    if maxfound[0] > 0 and not found_dup:
        cli.msg(chan, "The sun sets.")
        chk_decision(cli, force = maxfound[1])  # Induce a lynch
    else:
        cli.msg(chan, ("As the sun sets, the villagers agree to "+
                      "retire to their beds and wait for morning."))
        transition_night(cli)




@cmd("fnight", admin_only=True)
def fnight(cli, nick, chan, rest):
    """Forces the day to end and night to begin."""
    if var.PHASE != "day":
        cli.notice(nick, "It is not daytime.")
    else:
        hurry_up(cli, 0, True)


@cmd("fday", admin_only=True)
def fday(cli, nick, chan, rest):
    """Forces the night to end and the next day to begin."""
    if var.PHASE != "night":
        cli.notice(nick, "It is not nighttime.")
    else:
        transition_day(cli)

# Specify force = "nick" to force nick to be lynched
def chk_decision(cli, force = ""):
    chan = botconfig.CHANNEL
    pl = var.list_players()
    avail = len(pl) - len(var.WOUNDED) - len(var.ASLEEP)
    votesneeded = avail // 2 + 1
    not_lynching = var.NO_LYNCH[:]
    for p in var.PACIFISTS:
        if p in pl and p not in var.WOUNDED and p not in var.ASLEEP:
            not_lynching.append(p)

    # .remove() will only remove the first instance, which means this plays nicely with pacifism countering this
    for p in var.IMPATIENT:
        if p in not_lynching:
            not_lynching.remove(p)

    # remove duplicates
    not_lynching = set(not_lynching)

    # we only need 50%+ to not lynch, instead of an actual majority, because a tie would time out day anyway
    # don't check for ABSTAIN_ENABLED here since we may have a case where the majority of people have pacifism totems or something
    if len(not_lynching) >= math.ceil(avail / 2):
        for p in not_lynching:
            if p not in var.NO_LYNCH:
                cli.msg(botconfig.CHANNEL, "\u0002{0}\u0002 meekly votes to not lynch anyone today.".format(p))
        cli.msg(botconfig.CHANNEL, "The villagers have agreed to not lynch anybody today.")
        var.ABSTAINED = True
        transition_night(cli)
        return
    aftermessage = None
    votelist = copy.deepcopy(var.VOTES)
    for votee, voters in votelist.items():
        impatient_voters = []
        numvotes = 0
        random.shuffle(var.IMPATIENT)
        for v in var.IMPATIENT:
            if v in pl and v not in voters and v != votee and v not in var.WOUNDED and v not in var.ASLEEP:
                # don't add them in if they have the same number or more of pacifism totems
                # this matters for desperation totem on the votee
                imp_count = sum([1 if p == v else 0 for p in var.IMPATIENT])
                pac_count = sum([1 if p == v else 0 for p in var.PACIFISTS])
                if pac_count >= imp_count:
                    continue

                # yes, this means that one of the impatient people will get desperation totem'ed if they didn't
                # already !vote earlier. sucks to suck. >:)
                voters.append(v)
                impatient_voters.append(v)
        for v in voters[:]:
            weight = 1
            imp_count = sum([1 if p == v else 0 for p in var.IMPATIENT])
            pac_count = sum([1 if p == v else 0 for p in var.PACIFISTS])
            if pac_count > imp_count:
                weight = 0 # more pacifists than impatience totems
            elif imp_count == pac_count and v not in var.VOTES[votee]:
                weight = 0 # impatience and pacifist cancel each other out, so don't count impatience
            if v in var.ROLES["bureaucrat"] or v in var.INFLUENTIAL: # the two do not stack
                weight *= 2
            numvotes += weight

        if numvotes >= votesneeded or votee == force:
            for p in impatient_voters:
                cli.msg(botconfig.CHANNEL, "\u0002{0}\u0002 impatiently votes for \u0002{1}\u0002.".format(p, votee))

            # roles that prevent any lynch from happening
            if votee in var.ROLES["mayor"] and votee not in var.REVEALED_MAYORS:
                lmsg = ("While being dragged to the gallows, \u0002{0}\u0002 reveals that they " +
                        "are the \u0002mayor\u0002. The village agrees to let them live for now.").format(votee)
                var.REVEALED_MAYORS.append(votee)
                votee = None
            elif votee in var.REVEALED:
                role = var.get_role(votee)
                if role == "amnesiac":
                    var.ROLES["amnesiac"].remove(votee)
                    role = var.FINAL_ROLES[votee]
                    var.ROLES[role].append(votee)
                    var.AMNESIACS.append(votee)
                    pm(cli, votee, "Your totem clears your amnesia and you now fully remember who you are!")
                    # If wolfteam, don't bother giving list of wolves since night is about to start anyway
                    # Existing wolves also know that someone just joined their team because revealing totem says what they are

                an = "n" if role[0] in ("a", "e", "i", "o", "u") else ""
                lmsg = ("Before the rope is pulled, \u0002{0}\u0002's totem emits a brilliant flash of light. " +
                        "When the villagers are able to see again, they discover that {0} has escaped! " +
                        "The left-behind totem seems to have taken on the shape of a{1} \u0002{2}\u0002.").format(votee, an, role)
                votee = None
            else:
                # roles that end the game upon being lynched
                if votee in var.ROLES["fool"]:
                    # ends game immediately, with fool as only winner
                    lmsg = random.choice(var.LYNCH_MESSAGES).format(votee, "", var.get_reveal_role(votee))
                    cli.msg(botconfig.CHANNEL, lmsg)
                    message = "Game over! The fool has been lynched, causing them to win."
                    debuglog("WIN: fool")
                    debuglog("PLAYERS:", votee)
                    cli.msg(botconfig.CHANNEL, message)
                    stop_game(cli, "@" + votee)
                    return
                # roles that eliminate other players upon being lynched
                # note that lovers, assassin, clone, and vengeful ghost are handled in del_player() since they trigger on more than just lynch
                if votee in var.DESPERATE:
                    # Also kill the very last person to vote them, unless they voted themselves last in which case nobody else dies
                    target = voters[-1]
                    if target != votee:
                        if var.ROLE_REVEAL:
                            r1 = var.get_reveal_role(target)
                            an1 = "n" if r1[0] in ("a", "e", "i", "o", "u") else ""
                            tmsg = ("As the noose is being fitted, \u0002{0}\u0002's totem emits a brilliant flash of light. " +
                                    "When the villagers are able to see again, they discover that \u0002{1}\u0002, " +
                                    "a{2} \u0002{3}\u0002, has fallen over dead.").format(votee, target, an1, r1)
                        else:
                            tmsg = ("As the noose is being fitted, \u0002{0}\u0002's totem emits a brilliant flash of light. " +
                                    "When the villagers are able to see again, they discover that \u0002{1}\u0002 " +
                                    "has fallen over dead.").format(votee, target)
                        cli.msg(botconfig.CHANNEL, tmsg)
                        del_player(cli, target, True, end_game = False, killer_role = "shaman") # do not end game just yet, we have more killin's to do!
                # Other
                if votee in var.ROLES["jester"]:
                    var.JESTERS.append(votee)

                if var.ROLE_REVEAL:
                    rrole = var.get_reveal_role(votee)
                    an = "n" if rrole[0] in ('a', 'e', 'i', 'o', 'u') else ""
                    lmsg = random.choice(var.LYNCH_MESSAGES).format(votee, an, rrole)
                else:
                    lmsg = random.choice(var.LYNCH_MESSAGES_NO_REVEAL).format(votee)
            cli.msg(botconfig.CHANNEL, lmsg)
            if aftermessage != None:
                cli.msg(botconfig.CHANNEL, aftermessage)
            if del_player(cli, votee, True, killer_role = "villager"):
                transition_night(cli)
            break

@cmd("votes", pm=True, game=True)
def show_votes(cli, nick, chan, rest):
    """Displays the voting statistics."""

    if var.PHASE != 'day':
        cli.notice(nick, "Voting is only during the day.")
        return

    if (chan != nick and var.LAST_VOTES and var.VOTES_RATE_LIMIT and
            var.LAST_VOTES + timedelta(seconds=var.VOTES_RATE_LIMIT) >
            datetime.now()):
        cli.notice(nick, ('This command is rate-limited. Please wait a while '
                          'before using it again.'))
        return
    
    pl = var.list_players()

    if chan != nick and nick in pl:
        var.LAST_VOTES = datetime.now()

    if not var.VOTES.values():
        msg = nick+ ': No votes yet.'

        if nick in pl:
            var.LAST_VOTES = None  # reset
    else:
        votelist = ['{}: {} ({})'.format(votee,
                                         len(var.VOTES[votee]),
                                         ' '.join(var.VOTES[votee]))
                    for votee in var.VOTES.keys()]
        msg = '{}: {}'.format(nick, ', '.join(votelist))

    if chan == nick:
        pm(cli, nick, msg)
    elif nick not in pl and var.PHASE not in ("none", "join"):
        cli.notice(nick, msg)
    else:
        cli.msg(chan, msg)

    pl = var.list_players()
    avail = len(pl) - len(var.WOUNDED) - len(var.ASLEEP)
    votesneeded = avail // 2 + 1
    not_voting = len(var.NO_LYNCH)
    if not_voting == 1:
        plural = " has"
    else:
        plural = "s have"
    the_message = ('{}: \u0002{}\u0002 players, \u0002{}\u0002 votes '
                   'required to lynch, \u0002{}\u0002 players available to '
                   'vote. \u0002{}\u0002 player{} refrained from voting.').format(nick, len(pl), votesneeded, avail, not_voting, plural)

    if chan == nick:
        pm(cli, nick, the_message)
    elif nick not in pl and var.PHASE not in ("none", "join"):
        cli.notice(nick, the_message)
    else:
        cli.msg(chan, the_message)

def chk_traitor(cli):
    wcl = copy.copy(var.ROLES["wolf cub"])
    ttl = copy.copy(var.ROLES["traitor"])
    for wc in wcl:
        var.ROLES["wolf"].append(wc)
        var.ROLES["wolf cub"].remove(wc)
        pm(cli, wc, ('You have grown up into a wolf and vowed to take revenge for your dead parents!'))
        debuglog(wc, "(wolf cub) GROW UP")

    if len(var.ROLES["wolf"]) == 0:
        for tt in ttl:
            var.ROLES["wolf"].append(tt)
            var.ROLES["traitor"].remove(tt)
            if tt in var.ROLES["cursed villager"]:
                var.ROLES["cursed villager"].remove(tt)
            pm(cli, tt, ('HOOOOOOOOOWL. You have become... a wolf!\n'+
                         'It is up to you to avenge your fallen leaders!'))
            debuglog(tt, "(traitor) TURNING")

        # no message if wolf cub becomes wolf for now, may want to change that in future
        if len(var.ROLES["wolf"]) > 0:
            cli.msg(botconfig.CHANNEL, ('\u0002The villagers, during their celebrations, are '+
                                        'frightened as they hear a loud howl. The wolves are '+
                                        'not gone!\u0002'))

def stop_game(cli, winner = ""):
    chan = botconfig.CHANNEL
    if var.DAY_START_TIME:
        now = datetime.now()
        td = now - var.DAY_START_TIME
        var.DAY_TIMEDELTA += td
    if var.NIGHT_START_TIME:
        now = datetime.now()
        td = now - var.NIGHT_START_TIME
        var.NIGHT_TIMEDELTA += td

    daymin, daysec = var.DAY_TIMEDELTA.seconds // 60, var.DAY_TIMEDELTA.seconds % 60
    nitemin, nitesec = var.NIGHT_TIMEDELTA.seconds // 60, var.NIGHT_TIMEDELTA.seconds % 60
    total = var.DAY_TIMEDELTA + var.NIGHT_TIMEDELTA
    tmin, tsec = total.seconds // 60, total.seconds % 60
    gameend_msg = ("Game lasted \u0002{0:0>2}:{1:0>2}\u0002. " +
                   "\u0002{2:0>2}:{3:0>2}\u0002 was day. " +
                   "\u0002{4:0>2}:{5:0>2}\u0002 was night. ").format(tmin, tsec,
                                                                     daymin, daysec,
                                                                     nitemin, nitesec)
    cli.msg(chan, gameend_msg)

    roles_msg = []

    origroles = {} #nick based list of original roles
    rolelist = copy.deepcopy(var.ORIGINAL_ROLES)
    for role, playerlist in var.ORIGINAL_ROLES.items():
        if role in var.TEMPLATE_RESTRICTIONS.keys():
            continue
        for p in playerlist:
            player = p #with (dced) still in
            if p.startswith("(dced)"):
                p = p[6:]
            if p in var.FINAL_ROLES and var.FINAL_ROLES[p] != role and (role != "amnesiac" or p in var.AMNESIACS):
                origroles[p] = role
                rolelist[role].remove(player)
                rolelist[var.FINAL_ROLES[p]].append(p)
    prev = False
    for role in var.role_order():
        if len(rolelist[role]) == 0:
            continue
        playersformatted = []
        for p in rolelist[role]:
            if p.startswith("(dced)"):
                p = p[6:]
            if p in origroles and role not in var.TEMPLATE_RESTRICTIONS.keys():
                playersformatted.append("\u0002{0}\u0002 ({1}{2})".format(p, 
                                        "" if prev else "was ", origroles[p]))
                prev = True
            else:
                playersformatted.append("\u0002{0}\u0002".format(p))
        if len(rolelist[role]) == 2:
            msg = "The {1} were {0[0]} and {0[1]}."
            roles_msg.append(msg.format(playersformatted, var.plural(role)))
        elif len(rolelist[role]) == 1:
            roles_msg.append("The {1} was {0[0]}.".format(playersformatted, role))
        else:
            msg = "The {2} were {0}, and {1}."
            roles_msg.append(msg.format(", ".join(playersformatted[0:-1]),
                                                  playersformatted[-1],
                                                  var.plural(role)))
    message = ""
    count = 0
    cli.msg(chan, var.break_long_message(roles_msg))

    done = {}
    lovers = []
    for lover1, llist in var.ORIGINAL_LOVERS.items():
        for lover2 in llist:
            # check if already said the pairing
            if (lover1 in done and lover2 in done[lover1]) or (lover2 in done and lover1 in done[lover2]):
                continue
            lovers.append("\u0002{0}\u0002/\u0002{1}\u0002".format(lover1, lover2))
            if lover1 in done:
                done[lover1].append(lover2)
            else:
                done[lover1] = [lover2]
    if len(lovers) == 1 or len(lovers) == 2:
        cli.msg(chan, "The lovers were {0}.".format(" and ".join(lovers)))
    elif len(lovers) > 2:
        cli.msg(chan, "The lovers were {0}, and {1}".format(", ".join(lovers[0:-1]), lovers[-1]))

    # Only update if someone actually won, "" indicates everyone died or abnormal game stop
    if winner != "":
        plrl = {}
        winners = []
        for role,ppl in var.ORIGINAL_ROLES.items():
            if role in var.TEMPLATE_RESTRICTIONS.keys():
                continue
            for x in ppl:
                if x != None:
                    if role == "amnesiac" and x in var.AMNESIACS:
                        plrl[x] = var.FINAL_ROLES[x]
                    elif role != "amnesiac" and x in var.FINAL_ROLES: # role swap
                        plrl[x] = var.FINAL_ROLES[x]
                    else:
                        plrl[x] = role
        for plr, rol in plrl.items():
            orol = rol # original role, since we overwrite rol in case of clone
            splr = plr # plr stripped of the (dced) bit at the front, since other dicts don't have that
            if plr.startswith("(dced)") and plr[6:] in var.DCED_PLAYERS.keys():
                acc = var.DCED_PLAYERS[plr[6:]]["account"]
                splr = plr[6:]
            elif plr in var.PLAYERS.keys():
                acc = var.PLAYERS[plr]["account"]
            else:
                acc = "*"  #probably fjoin'd fake

            if rol == "clone":
                # see if they became a different role
                if splr in var.FINAL_ROLES:
                    rol = var.FINAL_ROLES[splr]

            won = False
            iwon = False
            # determine if this player's team won
            if rol in var.WOLFTEAM_ROLES:  # the player was wolf-aligned
                if winner == "wolves":
                    won = True
            elif rol in var.TRUE_NEUTRAL_ROLES:
                # true neutral roles never have a team win (with exception of monsters), only individual wins
                if winner == "monsters" and rol == "monster":
                    won = True
            elif rol in ("amnesiac", "vengeful ghost") and splr not in var.VENGEFUL_GHOSTS:
                if var.DEFAULT_ROLE == "villager" and winner == "villagers":
                    won = True
                elif var.DEFAULT_ROLE == "cultist" and winner == "wolves":
                    won = True
            elif winner == "villagers":
                won = True

            survived = var.list_players()
            if plr.startswith("(dced)"):
                # You get NOTHING! You LOSE! Good DAY, sir!
                won = False
                iwon = False
            elif rol == "fool" and "@" + splr == winner:
                iwon = True
            elif rol == "monster" and splr in survived and winner == "monsters":
                iwon = True
            elif splr in var.LOVERS and splr in survived:
                for lvr in var.LOVERS[splr]:
                    lvrrol = "" #somehow lvrrol wasn't set and caused a crash once
                    if lvr in plrl:
                        lvrrol = plrl[lvr]
                    elif ("(dced)" + lvr) in plrl:
                        lvrrol = plrl["(dced)" + lvr]
                    if lvrrol == "clone" and lvr in var.FINAL_ROLES:
                        lvrrol = var.FINAL_ROLES[lvr]

                    if lvr in survived and not winner.startswith("@") and winner != "monsters":
                        iwon = True
                        break
                    elif lvr in survived and winner.startswith("@") and winner == "@" + lvr and var.LOVER_WINS_WITH_FOOL:
                        iwon = True
                        break
                    elif lvr in survived and winner == "monsters" and lvrrol == "monster":
                        iwon = True
                        break

            if plr.startswith("(dced)"):
                won = False
                iwon = False
            elif rol == "crazed shaman" or rol == "clone":
                # For clone, this means they ended game while being clone and not some other role
                if splr in survived and not winner.startswith("@") and winner != "monsters":
                    iwon = True
            elif rol == "vengeful ghost":
                if not winner.startswith("@") and winner != "monsters":
                    if won and splr in survived:
                        iwon = True
                    elif splr in var.VENGEFUL_GHOSTS and var.VENGEFUL_GHOSTS[splr] == "villagers" and winner == "wolves":
                        won = True
                        iwon = True
                    elif splr in var.VENGEFUL_GHOSTS and var.VENGEFUL_GHOSTS[splr] == "!villagers" and winner == "wolves":
                        # Starts with ! if they were driven off by retribution totem
                        won = True
                        iwon = False
                    elif splr in var.VENGEFUL_GHOSTS and var.VENGEFUL_GHOSTS[splr] == "wolves" and winner == "villagers":
                        won = True
                        iwon = True
                    elif splr in var.VENGEFUL_GHOSTS and var.VENGEFUL_GHOSTS[splr] == "!wolves" and winner == "villagers":
                        won = True
                        iwon = False
                    else:
                        won = False
                        iwon = False
            elif rol == "lycan" or splr in var.LYCANS:
                if splr in var.LYCANS and winner == "wolves":
                    won = True
                elif splr not in var.LYCANS and winner == "villagers":
                    won = True
                else:
                    won = False
                if not iwon:
                    iwon = won and splr in survived
            elif rol == "jester" and splr in var.JESTERS:
                iwon = True
            elif not iwon:
                iwon = won and splr in survived  # survived, team won = individual win

            if acc != "*":
                var.update_role_stats(acc, orol, won, iwon)
                for role in var.TEMPLATE_RESTRICTIONS.keys():
                    if plr in var.ORIGINAL_ROLES[role]:
                        var.update_role_stats(acc, role, won, iwon)
                if splr in var.LOVERS:
                    var.update_role_stats(acc, "lover", won, iwon)

            if won or iwon:
                winners.append(splr)

        var.update_game_stats(var.CURRENT_GAMEMODE, len(survived) + len(var.DEAD), winner)

        # spit out the list of winners
        winners.sort()
        if len(winners) == 1:
            cli.msg(chan, "The winner is \u0002{0}\u0002.".format(winners[0]))
        elif len(winners) == 2:
            cli.msg(chan, "The winners are \u0002{0}\u0002 and \u0002{1}\u0002.".format(winners[0], winners[1]))
        elif len(winners) > 2:
            nicklist = ["\u0002" + x + "\u0002" for x in winners[0:-1]]
            cli.msg(chan, "The winners are {0}, and \u0002{1}\u0002.".format(", ".join(nicklist), winners[-1]))

    reset_modes_timers(cli)

    reset()

    # This must be after reset()
    if var.AFTER_FLASTGAME:
        var.AFTER_FLASTGAME()
        var.AFTER_FLASTGAME = None
    if var.ADMIN_TO_PING:  # It was an flastgame
        cli.msg(chan, "PING! " + var.ADMIN_TO_PING)
        var.ADMIN_TO_PING = None

    return True

def chk_win(cli, end_game = True):
    """ Returns True if someone won """
    chan = botconfig.CHANNEL
    lpl = len(var.list_players())

    if var.PHASE == "join":
        if lpl == 0:
            #cli.msg(chan, "No more players remaining. Game ended.")
            reset_modes_timers(cli)
            reset()
            return True
        return False

    with var.GRAVEYARD_LOCK:
        if var.PHASE not in ("day", "night"):
            return False #some other thread already ended game probably
        
        lwolves = len(var.list_players(var.WOLFCHAT_ROLES))
        cubs = len(var.ROLES["wolf cub"]) if "wolf cub" in var.ROLES else 0
        lrealwolves = len(var.list_players(var.WOLF_ROLES)) - cubs
        monsters = len(var.ROLES["monster"]) if "monster" in var.ROLES else 0
        traitors = len(var.ROLES["traitor"]) if "traitor" in var.ROLES else 0
        if var.PHASE == "day":
            for p in var.WOUNDED:
                try:
                    role = var.get_role(p)
                    if role in var.WOLFCHAT_ROLES:
                        lwolves -= 1
                    else:
                        lpl -= 1
                except KeyError:
                    pass
            for p in var.ASLEEP:
                try:
                    role = var.get_role(p)
                    if role in var.WOLFCHAT_ROLES:
                        lwolves -= 1
                    else:
                        lpl -= 1
                except KeyError:
                    pass

        if lpl < 1:
            message = "Game over! There are no players remaining. Nobody wins."
            winner = "none"
        elif lwolves == lpl / 2:
            if monsters > 0:
                plural = "s" if monsters > 1 else ""
                message = ("Game over! There are the same number of wolves as uninjured villagers. " +
                           "The wolves overpower the villagers but then get destroyed by the monster{0}, " +
                           "causing the monster{0} to win.").format(plural)
                winner = "monsters"
            else:
                message = ("Game over! There are the same number of wolves as " +
                          "uninjured villagers. The wolves overpower the villagers and win.")
                winner = "wolves"
        elif lwolves > lpl / 2:
            if monsters > 0:
                plural = "s" if monsters > 1 else ""
                message = ("Game over! There are more wolves than uninjured villagers. " +
                           "The wolves overpower the villagers but then get destroyed by the monster{0}, " +
                           "causing the monster{0} to win.").format(plural)
                winner = "monsters"
            else:
                message = ("Game over! There are more wolves than "+
                          "uninjured villagers. The wolves overpower the villagers and win.")
                winner = "wolves"
        elif lrealwolves == 0 and traitors == 0 and cubs == 0:
            if monsters > 0:
                plural = "s" if monsters > 1 else ""
                message = ("Game over! All the wolves are dead! As the villagers start preparing the BBQ, " +
                           "the monster{0} quickly kill{1} the remaining villagers, " +
                           "causing the monster{0} to win.").format(plural, "" if plural else "s")
                winner = "monsters"
            else:
                message = ("Game over! All the wolves are dead! The villagers " +
                          "chop them up, BBQ them, and have a hearty meal.")
                winner = "villagers"
        elif lrealwolves == 0:
            chk_traitor(cli)
            return chk_win(cli, end_game)
        else:
            return False
        if end_game:
            players = []
            if winner == "monsters":
                for plr in var.ROLES["monster"]:
                    players.append("{0} ({1})".format(plr, var.get_role(plr)))
            elif winner == "wolves":
                for plr in var.list_players(var.WOLFTEAM_ROLES):
                    players.append("{0} ({1})".format(plr, var.get_role(plr)))
            elif winner == "villagers":
                vroles = [role for role in var.ROLES.keys() if var.ROLES[role] and role not in (var.WOLFTEAM_ROLES + var.TRUE_NEUTRAL_ROLES + list(var.TEMPLATE_RESTRICTIONS.keys()))]
                for plr in var.list_players(vroles):
                    players.append("{0} ({1})".format(plr, var.get_role(plr)))
            debuglog("WIN:", winner)
            debuglog("PLAYERS:", ", ".join(players))
            cli.msg(chan, message)
            stop_game(cli, winner)
        return True

def del_player(cli, nick, forced_death = False, devoice = True, end_game = True, death_triggers = True, killer_role = "", deadlist = [], original = ""):
    """
    Returns: False if one side won.
    arg: forced_death = True when lynched or when the seer/wolf both don't act
    """

    t = time.time()  #  time

    var.LAST_STATS = None # reset
    var.LAST_VOTES = None

    with var.GRAVEYARD_LOCK:
        if not var.GAME_ID or var.GAME_ID > t:
            #  either game ended, or a new game has started.
            return False
        cmode = []
        ret = True
        pl = var.list_players()
        for dead in deadlist:
            if dead in pl:
                pl.remove(dead)
        if nick != None and (nick == original or nick in pl):
            nickrole = var.get_role(nick)
            nicktpls = var.get_templates(nick)
            var.del_player(nick)
            if nick in pl:
                pl.remove(nick)
            # handle roles that trigger on death
            # clone happens regardless of death_triggers being true or not
            if var.PHASE in ("night", "day"):
                clones = copy.copy(var.ROLES["clone"])
                for clone in clones:
                    if clone in var.CLONED:
                        target = var.CLONED[clone]
                        if nick == target and clone in var.CLONED:
                            # clone is cloning nick, so clone becomes nick's role
                            # clone does NOT get any of nick's templates (gunner/assassin/etc.)
                            del var.CLONED[clone]
                            var.ROLES["clone"].remove(clone)
                            if nickrole == "amnesiac":
                                # clone gets the amnesiac's real role
                                sayrole = var.FINAL_ROLES[nick]
                                var.FINAL_ROLES[clone] = sayrole
                                var.ROLES[sayrole].append(clone)
                            else:
                                var.ROLES[nickrole].append(clone)
                                var.FINAL_ROLES[clone] = nickrole
                                sayrole = nickrole
                            debuglog("{0} (clone) CLONE DEAD PLAYER: {1} ({2})".format(clone, target, sayrole))
                            # if cloning time lord or vengeful ghost, say they are villager instead
                            if sayrole in ("time lord", "village elder"):
                                sayrole = "villager"
                            elif sayrole == "vengeful ghost":
                                sayrole = var.DEFAULT_ROLE
                            an = "n" if sayrole[0] in ("a", "e", "i", "o", "u") else ""
                            pm(cli, clone, "You are now a{0} \u0002{1}\u0002.".format(an, sayrole))
                            # if a clone is cloning a clone, clone who the old clone cloned
                            if nickrole == "clone" and nick in var.CLONED:
                                if var.CLONED[nick] == clone:
                                    pm(cli, clone, "It appears that \u0002{0}\u0002 was cloning you, so you are now stuck as a clone forever. How sad.".format(nick))
                                else:
                                    var.CLONED[clone] = var.CLONED[nick]
                                    pm(cli, clone, "You will now be cloning \u0002{0}\u0002 if they die.".format(var.CLONED[clone]))
                                    debuglog("{0} (clone) CLONE: {1} ({2})".format(clone, var.CLONED[clone], var.get_role(var.CLONED[clone])))
                            elif nickrole in var.WOLFCHAT_ROLES:
                                wolves = var.list_players(var.WOLFCHAT_ROLES)
                                wolves.remove(clone) # remove self from list
                                for wolf in wolves:
                                    pm(cli, wolf, "\u0002{}\u0002 cloned \u0002{}\u0002 and has now become a wolf!".format(clone, nick))
                                if var.PHASE == "day":
                                    random.shuffle(wolves)
                                    for i, wolf in enumerate(wolves):
                                        wolfrole = var.get_role(wolf)
                                        cursed = ""
                                        if wolf in var.ROLES["cursed villager"]:
                                            cursed = "cursed "
                                        wolves[i] = "\u0002{0}\u0002 ({1}{2})".format(wolf, cursed, wolfrole)

                                    if len(wolves):
                                        pm(cli, clone, "Wolves: " + ", ".join(wolves))
                                    else:
                                        pm(cli, clone, "There are no other wolves")

                if nickrole == "clone" and nick in var.CLONED:
                    del var.CLONED[nick]

            if death_triggers and var.PHASE in ("night", "day"):
                if nick in var.LOVERS:
                    others = copy.copy(var.LOVERS[nick])
                    del var.LOVERS[nick][:]
                    for other in others:
                        if other not in pl:
                            continue # already died somehow
                        if nick not in var.LOVERS[other]:
                            continue
                        var.LOVERS[other].remove(nick)
                        if var.ROLE_REVEAL:
                            role = var.get_reveal_role(other)
                            an = "n" if role[0] in ("a", "e", "i", "o", "u") else ""
                            message = ("Saddened by the loss of their lover, \u0002{0}\u0002, " +
                                       "a{1} \u0002{2}\u0002, commits suicide.").format(other, an, role)
                        else:
                            message = "Saddened by the loss of their lover, \u0002{0}\u0002 commits suicide.".format(other)
                        cli.msg(botconfig.CHANNEL, message)
                        debuglog("{0} ({1}) LOVE SUICIDE: {2} ({3})".format(other, var.get_role(other), nick, nickrole))
                        del_player(cli, other, True, end_game = False, killer_role = killer_role, deadlist = deadlist, original = original)
                        pl.remove(other)
                if "assassin" in nicktpls:
                    if nick in var.TARGETED:
                        target = var.TARGETED[nick]
                        del var.TARGETED[nick]
                        if target != None and target in pl:
                            if target in var.PROTECTED:
                                message = ("Before dying, \u0002{0}\u0002 quickly attempts to slit \u0002{1}\u0002's throat; " +
                                           "however, {1}'s totem emits a brilliant flash of light, causing the attempt to miss.").format(nick, target)
                                cli.msg(botconfig.CHANNEL, message)
                            elif target in var.GUARDED.values() and var.GAMEPHASE == "night":
                                for bg in var.ROLES["guardian angel"]:
                                    if bg in var.GUARDED and var.GUARDED[bg] == target:
                                        message = ("Before dying, \u0002{0}\u0002 quickly attempts to slit \u0002{1}\u0002's throat; " +
                                                   "however, a guardian angel was on duty and able to foil the attempt.").format(nick, target)
                                        cli.msg(botconfig.CHANNEL, message)
                                        break
                                else:
                                    for ga in var.ROLES["bodyguard"]:
                                        if ga in var.GUARDED and var.GUARDED[ga] == target:
                                            message = ("Before dying, \u0002{0}\u0002 quickly attempts to slit \u0002{1}\u0002's throat; " +
                                                       "however, \u0002{2}\u0002, a bodyguard, sacrificed their life to protect them.").format(nick, target, ga)
                                            cli.msg(botconfig.CHANNEL, message)
                                            del_player(cli, ga, True, end_game = False, killer_role = nickrole, deadlist = deadlist, original = original)
                                            pl.remove(ga)
                                            break
                            else:
                                if var.ROLE_REVEAL:
                                    role = var.get_reveal_role(target)
                                    an = "n" if role[0] in ("a", "e", "i", "o", "u") else ""
                                    message = ("Before dying, \u0002{0}\u0002 quickly slits \u0002{1}\u0002's throat. " +
                                               "The village mourns the loss of a{2} \u0002{3}\u0002.").format(nick, target, an, role)
                                else:
                                    message = "Before dying, \u0002{0}\u0002 quickly slits \u0002{1}\u0002's throat.".format(nick, target)
                                cli.msg(botconfig.CHANNEL, message)
                                debuglog("{0} ({1}) ASSASSINATE: {2} ({3})".format(nick, nickrole, target, var.get_role(target)))
                                del_player(cli, target, True, end_game = False, killer_role = nickrole, deadlist = deadlist, original = original)
                                pl.remove(target)

                if nickrole == "time lord":
                    if "DAY_TIME_LIMIT" not in var.ORIGINAL_SETTINGS:
                        var.ORIGINAL_SETTINGS["DAY_TIME_LIMIT"] = var.DAY_TIME_LIMIT
                    if "DAY_TIME_WARN" not in var.ORIGINAL_SETTINGS:
                        var.ORIGINAL_SETTINGS["DAY_TIME_WARN"] = var.DAY_TIME_WARN
                    if "SHORT_DAY_LIMIT" not in var.ORIGINAL_SETTINGS:
                        var.ORIGINAL_SETTINGS["SHORT_DAY_LIMIT"] = var.SHORT_DAY_LIMIT
                    if "SHORT_DAY_WARN" not in var.ORIGINAL_SETTINGS:
                        var.ORIGINAL_SETTINGS["SHORT_DAY_WARN"] = var.SHORT_DAY_WARN
                    if "NIGHT_TIME_LIMIT" not in var.ORIGINAL_SETTINGS:
                        var.ORIGINAL_SETTINGS["NIGHT_TIME_LIMIT"] = var.NIGHT_TIME_LIMIT
                    if "NIGHT_TIME_WARN" not in var.ORIGINAL_SETTINGS:
                        var.ORIGINAL_SETTINGS["NIGHT_TIME_WARN"] = var.NIGHT_TIME_WARN
                    var.DAY_TIME_LIMIT = var.TIME_LORD_DAY_LIMIT
                    var.DAY_TIME_WARN = var.TIME_LORD_DAY_WARN
                    var.SHORT_DAY_LIMIT = var.TIME_LORD_DAY_LIMIT
                    var.SHORT_DAY_WARN = var.TIME_LORD_DAY_WARN
                    var.NIGHT_TIME_LIMIT = var.TIME_LORD_NIGHT_LIMIT
                    var.NIGHT_TIME_WARN = var.TIME_LORD_NIGHT_WARN
                    cli.msg(botconfig.CHANNEL, ("Tick tock! Since the time lord has died, " +
                                                "day will now only last {0} seconds and night will now only " +
                                                "last {1} seconds!").format(var.TIME_LORD_DAY_LIMIT, var.TIME_LORD_NIGHT_LIMIT))
                    debuglog(nick, "(time lord) TRIGGER")
                if nickrole == "vengeful ghost":
                    if killer_role in var.WOLFTEAM_ROLES:
                        var.VENGEFUL_GHOSTS[nick] = "wolves"
                    else:
                        var.VENGEFUL_GHOSTS[nick] = "villagers"
                    pm(cli, nick, ("OOOooooOOOOooo! You are the \u0002vengeful ghost\u0002. It is now your job " +
                                   "to exact your revenge on the \u0002{0}\u0002 that killed you.").format(var.VENGEFUL_GHOSTS[nick]))
                    debuglog(nick, "(vengeful ghost) TRIGGER", var.VENGEFUL_GHOSTS[nick])
                if nickrole == "wolf cub":
                    var.ANGRY_WOLVES = True
                if nickrole in var.WOLF_ROLES:
                    if var.GAMEPHASE == "day":
                        var.ALPHA_ENABLED = True
                    for bitten, days in var.BITTEN.items():
                        brole = var.get_role(bitten)
                        if brole not in var.WOLF_ROLES and days > 0:
                            var.BITTEN[bitten] -= 1
                            pm(cli, bitten, ("Upon gazing at {0}'s lifeless body, you feel a sharp pang of regret and vengeance. " +
                                             "You quickly look away and the feelings subside...").format(nick))

                if nickrole == "mad scientist":
                    # kills the 2 players adjacent to them in the original players listing (in order of !joining)
                    # if those players are already dead, nothing happens
                    index = var.ALL_PLAYERS.index(nick)
                    targets = []
                    target1 = var.ALL_PLAYERS[index - 1]
                    target2 = var.ALL_PLAYERS[index + 1 if index < len(var.ALL_PLAYERS) - 1 else 0]
                    if len(var.ALL_PLAYERS) >= var.MAD_SCIENTIST_SKIPS_DEAD_PLAYERS:
                        # determine left player
                        i = index
                        while True:
                            i -= 1
                            if i < 0:
                                i = len(var.ALL_PLAYERS) - 1
                            if var.ALL_PLAYERS[i] in pl or var.ALL_PLAYERS[i] == nick:
                                target1 = var.ALL_PLAYERS[i]
                                break
                        # determine right player
                        i = index
                        while True:
                            i += 1
                            if i >= len(var.ALL_PLAYERS):
                                i = 0
                            if var.ALL_PLAYERS[i] in pl or var.ALL_PLAYERS[i] == nick:
                                target2 = var.ALL_PLAYERS[i]
                                break

                    if target1 in pl:
                        if target2 in pl and target1 != target2:
                            if var.ROLE_REVEAL:
                                r1 = var.get_reveal_role(target1)
                                an1 = "n" if r1[0] in ("a", "e", "i", "o", "u") else ""
                                r2 = var.get_reveal_role(target2)
                                an2 = "n" if r2[0] in ("a", "e", "i", "o", "u") else ""
                                tmsg = ("\u0002{0}\u0002 throws " +
                                        "a potent chemical concoction into the crowd. \u0002{1}\u0002, " +
                                        "a{2} \u0002{3}\u0002, and \u0002{4}\u0002, a{5} \u0002{6}\u0002, " +
                                        "get hit by the chemicals and die.").format(nick, target1, an1, r1, target2, an2, r2)
                            else:
                                tmsg = ("\u0002{0}\u0002 throws " +
                                        "a potent chemical concoction into the crowd. \u0002{1}\u0002 " +
                                        "and \u0002{2}\u0002 get hit by the chemicals and die.").format(nick, target1, target2)
                            cli.msg(botconfig.CHANNEL, tmsg)
                            debuglog(nick, "(mad scientist) KILL: {0} ({1}) - {2} ({3})".format(target1, var.get_role(target1), target2, var.get_role(target2)))
                            deadlist1 = copy.copy(deadlist)
                            deadlist1.append(target2)
                            deadlist2 = copy.copy(deadlist)
                            deadlist2.append(target1)
                            del_player(cli, target1, True, end_game = False, killer_role = "mad scientist", deadlist = deadlist1, original = original)
                            del_player(cli, target2, True, end_game = False, killer_role = "mad scientist", deadlist = deadlist2, original = original)
                            pl.remove(target1)
                            pl.remove(target2)
                        else:
                            if var.ROLE_REVEAL:
                                r1 = var.get_reveal_role(target1)
                                an1 = "n" if r1[0] in ("a", "e", "i", "o", "u") else ""
                                tmsg = ("\u0002{0}\u0002 throws " +
                                        "a potent chemical concoction into the crowd. \u0002{1}\u0002, " +
                                        "a{2} \u0002{3}\u0002, gets hit by the chemicals and dies.").format(nick, target1, an1, r1)
                            else:
                                tmsg = ("\u0002{0}\u0002 throws " +
                                        "a potent chemical concoction into the crowd. \u0002{1}\u0002 " +
                                        "gets hit by the chemicals and dies.").format(nick, target1)
                            cli.msg(botconfig.CHANNEL, tmsg)
                            debuglog(nick, "(mad scientist) KILL: {0} ({1})".format(target1, var.get_role(target1)))
                            del_player(cli, target1, True, end_game = False, killer_role = "mad scientist", deadlist = deadlist, original = original)
                            pl.remove(target1)
                    else:
                        if target2 in pl:
                            if var.ROLE_REVEAL:
                                r2 = var.get_reveal_role(target2)
                                an2 = "n" if r2[0] in ("a", "e", "i", "o", "u") else ""
                                tmsg = ("\u0002{0}\u0002 throws " +
                                        "a potent chemical concoction into the crowd. \u0002{1}\u0002, " +
                                        "a{2} \u0002{3}\u0002, gets hit by the chemicals and dies.").format(nick, target2, an2, r2)
                            else:
                                tmsg = ("\u0002{0}\u0002 throws " +
                                        "a potent chemical concoction into the crowd. \u0002{1}\u0002 " +
                                        "gets hit by the chemicals and dies.").format(nick, target2)
                            cli.msg(botconfig.CHANNEL, tmsg)
                            debuglog(nick, "(mad scientist) KILL: {0} ({1})".format(target2, var.get_role(target2)))
                            del_player(cli, target2, True, end_game = False, killer_role = "mad scientist", deadlist = deadlist, original = original)
                            pl.remove(target2)
                        else:
                            tmsg = ("\u0002{0}\u0002 throws " +
                                    "a potent chemical concoction into the crowd. Thankfully, " +
                                    "nobody seems to have gotten hit.").format(nick)
                            cli.msg(botconfig.CHANNEL, tmsg)
                            debuglog(nick, "(mad scientist) KILL FAIL")

            if devoice:
                cmode.append(("-v", nick))
            if var.PHASE == "join":
                # Died during the joining process as a person
                if var.AUTO_TOGGLE_MODES and nick in var.USERS and var.USERS[nick]["moded"]:
                    for newmode in var.USERS[nick]["moded"]:
                        cmode.append(("+"+newmode, nick))
                    var.USERS[nick]["modes"].update(var.USERS[nick]["moded"])
                    var.USERS[nick]["moded"] = set()
                mass_mode(cli, cmode, [])
                return not chk_win(cli)
            if var.PHASE != "join":
                # Died during the game, so quiet!
                if var.QUIET_DEAD_PLAYERS and not is_fake_nick(nick):
                    cmode.append(("+q", nick+"!*@*"))
                mass_mode(cli, cmode, [])
                if nick not in var.DEAD:
                    var.DEAD.append(nick)
                ret = not chk_win(cli, end_game)
            if var.PHASE in ("night", "day") and ret:
                # remove the player from variables if they're in there
                for a,b in list(var.KILLS.items()):
                    for n in b: #var.KILLS can have 2 kills in a list
                        if n == nick:
                            var.KILLS[a].remove(nick)
                    if a == nick or len(var.KILLS[a]) == 0:
                        del var.KILLS[a]
                for x in (var.OBSERVED, var.HVISITED, var.GUARDED, var.TARGETED, var.LASTGUARDED, var.LASTGIVEN, var.LASTHEXED):
                    keys = list(x.keys())
                    for k in keys:
                        if k == nick:
                            del x[k]
                        elif x[k] == nick:
                            del x[k]
                if nick in var.DISCONNECTED:
                    del var.DISCONNECTED[nick]
            if var.PHASE == "day" and not forced_death and ret:  # didn't die from lynching
                if nick in var.VOTES.keys():
                    del var.VOTES[nick]  #  Delete other people's votes on the player
                for k in list(var.VOTES.keys()):
                    if nick in var.VOTES[k]:
                        var.VOTES[k].remove(nick)
                        if not var.VOTES[k]:  # no more votes on that person
                            del var.VOTES[k]
                        break # can only vote once
                if nick in var.NO_LYNCH:
                    var.NO_LYNCH.remove(nick)

                if nick in var.WOUNDED:
                    var.WOUNDED.remove(nick)
                if nick in var.ASLEEP:
                    var.ASLEEP.remove(nick)
                if nick in var.PLAYERS:
                    cloak = var.PLAYERS[nick]["cloak"]
                    if cloak in var.GAMEMODE_VOTES:
                        del var.GAMEMODE_VOTES[cloak]
                chk_decision(cli)
            elif var.PHASE == "night" and ret:
                chk_nightdone(cli)
        return ret


def reaper(cli, gameid):
    # check to see if idlers need to be killed.
    var.IDLE_WARNED    = set()
    var.IDLE_WARNED_PM = set()
    chan = botconfig.CHANNEL

    while gameid == var.GAME_ID:
        with var.GRAVEYARD_LOCK:
            # Terminate reaper when game ends
            if var.PHASE not in ("day", "night"):
                return
            if var.WARN_IDLE_TIME or var.PM_WARN_IDLE_TIME or var.KILL_IDLE_TIME:  # only if enabled
                to_warn    = []
                to_warn_pm = []
                to_kill    = []
                for nick in var.list_players():
                    lst = var.LAST_SAID_TIME.get(nick, var.GAME_START_TIME)
                    tdiff = datetime.now() - lst
                    if (tdiff > timedelta(seconds=var.WARN_IDLE_TIME) and
                                            nick not in var.IDLE_WARNED):
                        if var.WARN_IDLE_TIME:
                            to_warn.append(nick)
                        var.IDLE_WARNED.add(nick)
                        var.LAST_SAID_TIME[nick] = (datetime.now() -
                            timedelta(seconds=var.WARN_IDLE_TIME))  # Give them a chance
                    elif (tdiff > timedelta(seconds=var.PM_WARN_IDLE_TIME) and
                                            nick not in var.IDLE_WARNED_PM):
                        if var.PM_WARN_IDLE_TIME:
                            to_warn_pm.append(nick)
                        var.IDLE_WARNED_PM.add(nick)
                        var.LAST_SAID_TIME[nick] = (datetime.now() -
                            timedelta(seconds=var.PM_WARN_IDLE_TIME))
                    elif (tdiff > timedelta(seconds=var.KILL_IDLE_TIME) and
                        nick in var.IDLE_WARNED and nick in var.IDLE_WARNED_PM):
                        if var.KILL_IDLE_TIME:
                            to_kill.append(nick)
                    elif (tdiff < timedelta(seconds=var.WARN_IDLE_TIME) and
                        (nick in var.IDLE_WARNED or nick in var.IDLE_WARNED_PM)):
                        var.IDLE_WARNED.discard(nick)  # player saved themselves from death
                        var.IDLE_WARNED_PM.discard(nick)
                for nck in to_kill:
                    if nck not in var.list_players():
                        continue
                    if var.ROLE_REVEAL:
                        cli.msg(chan, ("\u0002{0}\u0002 didn't get out of bed for a very long "+
                                       "time and has been found dead. The survivors bury "+
                                       "the \u0002{1}\u0002's body.").format(nck, var.get_reveal_role(nck)))
                    else:
                        cli.msg(chan, ("\u0002{0}\u0002 didn't get out of bed for a very long " +
                                       "time and has been found dead.").format(nck))
                    for r,rlist in var.ORIGINAL_ROLES.items():
                        if nck in rlist:
                            var.ORIGINAL_ROLES[r].remove(nck)
                            var.ORIGINAL_ROLES[r].append("(dced)"+nck)
                    make_stasis(nck, var.IDLE_STASIS_PENALTY)
                    del_player(cli, nck, end_game = False, death_triggers = False)
                chk_win(cli)
                pl = var.list_players()
                x = [a for a in to_warn if a in pl]
                if x:
                    cli.msg(chan, ("{0}: \u0002You have been idling for a while. "+
                                   "Please say something soon or you "+
                                   "might be declared dead.\u0002").format(", ".join(x)))
                msg_targets = [p for p in to_warn_pm if p in pl]
                mass_privmsg(cli, msg_targets, ("\u0002You have been idling in {0} for a while. Please say something in {0} "+
                                                "or you will be declared dead.\u0002").format(chan), privmsg=True)
            for dcedplayer in list(var.DISCONNECTED.keys()):
                acc, cloak, timeofdc, what = var.DISCONNECTED[dcedplayer]
                if what == "quit" and (datetime.now() - timeofdc) > timedelta(seconds=var.QUIT_GRACE_TIME):
                    if var.get_role(dcedplayer) != "person" and var.ROLE_REVEAL:
                        cli.msg(chan, ("\02{0}\02 was mauled by wild animals and has died. It seems that "+
                                       "\02{1}\02 meat is tasty.").format(dcedplayer, var.get_reveal_role(dcedplayer)))
                    else:
                        cli.msg(chan, ("\u0002{0}\u0002 was mauled by wild animals and has died.").format(dcedplayer))
                    if var.PHASE != "join":
                        make_stasis(dcedplayer, var.PART_STASIS_PENALTY)
                    if not del_player(cli, dcedplayer, devoice = False, death_triggers = False):
                        return
                elif what == "part" and (datetime.now() - timeofdc) > timedelta(seconds=var.PART_GRACE_TIME):
                    if var.get_role(dcedplayer) != "person" and var.ROLE_REVEAL:
                        cli.msg(chan, ("\02{0}\02, a \02{1}\02, ate some poisonous berries "+
                                       "and has died.").format(dcedplayer, var.get_reveal_role(dcedplayer)))
                    else:
                        cli.msg(chan, ("\u0002{0}\u0002 ate some poisonous berries and has died.").format(dcedplayer))
                    if var.PHASE != "join":
                        make_stasis(dcedplayer, var.PART_STASIS_PENALTY)
                    if not del_player(cli, dcedplayer, devoice = False, death_triggers = False):
                        return
                elif what == "account" and (datetime.now() - timeofdc) > timedelta(seconds=var.ACC_GRACE_TIME):
                    if var.get_role(dcedplayer) != "person" and var.ROLE_REVEAL:
                        cli.msg(chan, ("\02{0}\02 has died of a heart attack. The villagers "+
                                       "couldn't save the \02{1}\02.").format(dcedplayer, var.get_reveal_role(dcedplayer)))
                    else:
                        cli.msg(chan, ("\u0002{0}\u0002 has died of a heart attack.").format(dcedplayer))
                    if var.PHASE != "join":
                        make_stasis(dcedplayer, var.ACC_STASIS_PENALTY)
                    if not del_player(cli, dcedplayer, devoice = False, death_triggers = False):
                        return
        time.sleep(10)



@cmd("")  # update last said
def update_last_said(cli, nick, chan, rest):
    if chan != botconfig.CHANNEL:
        return

    if var.PHASE not in ("join", "none"):
        var.LAST_SAID_TIME[nick] = datetime.now()

    fullstring = "".join(rest)
    if var.CARE_BOLD and BOLD in fullstring:
        if var.KILL_BOLD:
            cli.send("KICK {0} {1} :Using bold is not allowed".format(botconfig.CHANNEL, nick))
        else:
            cli.notice(nick, "Using bold in the channel is not allowed.")
    if var.CARE_COLOR and any(code in fullstring for code in ["\x03", "\x16", "\x1f" ]):
        if var.KILL_COLOR:
            cli.send("KICK {0} {1} :Using color is not allowed".format(botconfig.CHANNEL, nick))
        else:
            cli.notice(nick, "Using color in the channel is not allowed.")

@hook("join")
def on_join(cli, raw_nick, chan, acc="*", rname=""):
    nick,m,u,cloak = parse_nick(raw_nick)
    if nick != botconfig.NICK:
        if nick not in var.USERS.keys():
            var.USERS[nick] = dict(cloak=cloak,account=acc,inchan=chan == botconfig.CHANNEL,modes=set(),moded=set())
        else:
            var.USERS[nick]["cloak"] = cloak
            var.USERS[nick]["account"] = acc
            if not var.USERS[nick]["inchan"]:
                # Will be True if the user joined the main channel, else False
                var.USERS[nick]["inchan"] = (chan == botconfig.CHANNEL)
    if chan != botconfig.CHANNEL:
        return
    with var.GRAVEYARD_LOCK:
        if nick in var.DISCONNECTED.keys():
            clk = var.DISCONNECTED[nick][1]
            act = var.DISCONNECTED[nick][0]
            if acc == act or (cloak == clk and not var.ACCOUNTS_ONLY):
                cli.mode(chan, "+v", nick, nick+"!*@*")
                del var.DISCONNECTED[nick]
                var.LAST_SAID_TIME[nick] = datetime.now()
                cli.msg(chan, "\02{0}\02 has returned to the village.".format(nick))
                for r,rlist in var.ORIGINAL_ROLES.items():
                    if "(dced)"+nick in rlist:
                        rlist.remove("(dced)"+nick)
                        rlist.append(nick)
                        break
                if nick in var.DCED_PLAYERS.keys():
                    var.PLAYERS[nick] = var.DCED_PLAYERS.pop(nick)
    if nick == botconfig.NICK:
        var.OPPED = False
    if nick == "ChanServ" and not var.OPPED:
        cli.msg("ChanServ", "op " + chan)


@cmd("goat", game=True, playing=True)
def goat(cli, nick, chan, rest):
    """Use a goat to interact with anyone in the channel during the day."""

    if var.PHASE != 'day':
        cli.notice(nick, 'You can only do that in the day.')
        return

    if var.GOATED and nick not in var.SPECIAL_ROLES['goat herder']:
        cli.notice(nick, 'This can only be done once per day.')
        return

    ul = list(var.USERS.keys())
    ull = [x.lower() for x in ul]

    rest = re.split(" +",rest)[0]
    if not rest:
        cli.notice(nick, 'Not enough parameters.')

    victim, _ = complete_match(rest.lower(), ull)
    if not victim:
        cli.notice(nick, "\u0002{0}\u0002 is not in this channel.".format(rest))
        return
    victim = ul[ull.index(victim)]

    goatact = random.choice(('kicks', 'headbutts'))

    cli.msg(chan, '\x02{}\x02\'s goat walks by and {} \x02{}\x02.'.format(
        nick, goatact, victim))

    var.GOATED = True

@cmd("fgoat", admin_only=True)
def fgoat(cli, nick, chan, rest):
    """Forces a goat to interact with anyone or anything, without limitations."""
    rest = rest.split(' ')[0].strip()
    goatact = random.choice(['kicks', 'headbutts'])

    cli.msg(chan, '\x02{}\x02\'s goat walks by and {} \x02{}\x02.'.format(
        nick, goatact, rest))

@hook("nick")
def on_nick(cli, oldnick, nick):
    prefix,u,m,cloak = parse_nick(oldnick)

    if prefix in var.USERS:
        var.USERS[nick] = var.USERS.pop(prefix)
        if not var.USERS[nick]["inchan"]:
            return
    chan = botconfig.CHANNEL

    if prefix == var.ADMIN_TO_PING:
        var.ADMIN_TO_PING = nick

    # for k,v in list(var.DEAD_USERS.items()):
        # if prefix == k:
            # var.DEAD_USERS[nick] = var.DEAD_USERS[k]
            # del var.DEAD_USERS[k]

    if (nick.startswith("Guest") or nick[0].isdigit() or (nick != "away" and "away" in nick.lower())) and nick not in var.DISCONNECTED.keys() and prefix in var.list_players():
        if var.PHASE != "join":
            cli.mode(chan, "-v", nick)
        leave(cli, "quit", oldnick)
        return

    if prefix in var.list_players() and prefix not in var.DISCONNECTED.keys():
        r = var.ROLES[var.get_role(prefix)]
        r.append(nick)
        r.remove(prefix)
        tpls = var.get_templates(prefix)
        for t in tpls:
            var.ROLES[t].append(nick)
            var.ROLES[t].remove(prefix)

        if var.PHASE in ("night", "day"):
            # ALL_PLAYERS needs to keep its ordering for purposes of mad scientist
            var.ALL_PLAYERS[var.ALL_PLAYERS.index(prefix)] = nick
            for k,v in var.ORIGINAL_ROLES.items():
                if prefix in v:
                    var.ORIGINAL_ROLES[k].remove(prefix)
                    var.ORIGINAL_ROLES[k].append(nick)
                    break
            for k,v in list(var.PLAYERS.items()):
                if prefix == k:
                    var.PLAYERS[nick] = var.PLAYERS[k]
                    del var.PLAYERS[k]
            for dictvar in (var.HVISITED, var.OBSERVED, var.GUARDED, var.OTHER_KILLS, var.TARGETED, var.CLONED, var.LASTGUARDED, var.LASTGIVEN, var.LASTHEXED, var.BITE_PREFERENCES, var.BITTEN_ROLES):
                kvp = []
                for a,b in dictvar.items():
                    if a == prefix:
                        a = nick
                    if b == prefix:
                        b = nick
                    kvp.append((a,b))
                dictvar.update(kvp)
                if prefix in dictvar.keys():
                    del dictvar[prefix]
            for dictvar in (var.VENGEFUL_GHOSTS, var.TOTEMS, var.FINAL_ROLES, var.BITTEN, var.GUNNERS, var.DOCTORS):
                if prefix in dictvar.keys():
                    dictvar[nick] = dictvar[prefix]
                    del dictvar[prefix]
            for dictvar in (var.KILLS, var.LOVERS, var.ORIGINAL_LOVERS):
                kvp = []
                for a,b in dictvar.items():
                    nl = []
                    for n in b:
                        if n == prefix:
                            n = nick
                        nl.append(n)
                    if a == prefix:
                        a = nick
                    kvp.append((a,nl))
                dictvar.update(kvp)
                if prefix in dictvar.keys():
                    del dictvar[prefix]
            if prefix in var.SEEN:
                var.SEEN.remove(prefix)
                var.SEEN.append(nick)
            if prefix in var.HEXED:
                var.HEXED.remove(prefix)
                var.HEXED.append(nick)
            if prefix in var.ASLEEP:
                var.ASLEEP.remove(prefix)
                var.ASLEEP.append(nick)
            if prefix in var.DESPERATE:
                var.DESPERATE.remove(prefix)
                var.DESPERATE.append(nick)
            if prefix in var.PROTECTED:
                var.PROTECTED.remove(prefix)
                var.PROTECTED.append(nick)
            if prefix in var.REVEALED:
                var.REVEALED.remove(prefix)
                var.REVEALED.append(nick)
            if prefix in var.SILENCED:
                var.SILENCED.remove(prefix)
                var.SILENCED.append(nick)
            if prefix in var.TOBESILENCED:
                var.TOBESILENCED.remove(prefix)
                var.TOBESILENCED.append(nick)
            if prefix in var.DYING:
                var.DYING.remove(prefix)
                var.DYING.append(nick)
            if prefix in var.REVEALED_MAYORS:
                var.REVEALED_MAYORS.remove(prefix)
                var.REVEALED_MAYORS.append(nick)
            if prefix in var.MATCHMAKERS:
                var.MATCHMAKERS.remove(prefix)
                var.MATCHMAKERS.append(nick)
            if prefix in var.HUNTERS:
                var.HUNTERS.remove(prefix)
                var.HUNTERS.append(nick)
            if prefix in var.SHAMANS:
                var.SHAMANS.remove(prefix)
                var.SHAMANS.append(nick)
            if prefix in var.LYCANS:
                var.LYCANS.remove(prefix)
                var.LYCANS.append(nick)
            if prefix in var.PASSED:
                var.PASSED.remove(prefix)
                var.PASSED.append(nick)
            if prefix in var.JESTERS:
                var.JESTERS.remove(prefix)
                var.JESTERS.append(nick)
            if prefix in var.AMNESIACS:
                var.AMNESIACS.remove(prefix)
                var.AMNESIACS.append(nick)
            while prefix in var.IMPATIENT:
                var.IMPATIENT.remove(prefix)
                var.IMPATIENT.append(nick)
            while prefix in var.PACIFISTS:
                var.PACIFISTS.remove(prefix)
                var.PACIFISTS.append(nick)
            if prefix in var.INFLUENTIAL:
                var.INFLUENTIAL.remove(prefix)
                var.INFLUENTIAL.append(nick)
            if prefix in var.LYCANTHROPES:
                var.LYCANTHROPES.remove(prefix)
                var.LYCANTHROPES.append(nick)
            if prefix in var.TOBELYCANTHROPES:
                var.TOBELYCANTHROPES.remove(prefix)
                var.TOBELYCANTHROPES.append(nick)
            if prefix in var.LUCKY:
                var.LUCKY.remove(prefix)
                var.LUCKY.append(nick)
            if prefix in var.TOBELUCKY:
                var.TOBELUCKY.remove(prefix)
                var.TOBELUCKY.append(nick)
            if prefix in var.DISEASED:
                var.DISEASED.remove(prefix)
                var.DISEASED.append(nick)
            if prefix in var.TOBEDISEASED:
                var.TOBEDISEASED.remove(prefix)
                var.TOBEDISEASED.append(nick)
            if prefix in var.RETRIBUTION:
                var.RETRIBUTION.remove(prefix)
                var.RETRIBUTION.append(nick)
            if prefix in var.MISDIRECTED:
                var.MISDIRECTED.remove(prefix)
                var.MISDIRECTED.append(nick)
            if prefix in var.TOBEMISDIRECTED:
                var.TOBEMISDIRECTED.remove(prefix)
                var.TOBEMISDIRECTED.append(nick)
            if prefix in var.EXCHANGED:
                var.EXCHANGED.remove(prefix)
                var.EXCHANGED.append(nick)
            if prefix in var.TOBEEXCHANGED:
                var.TOBEEXCHANGED.remove(prefix)
                var.TOBEEXCHANGED.append(nick)
            if prefix in var.IMMUNIZED:
                var.IMMUNIZED.remove(prefix)
                var.IMMUNIZED.add(nick)
            if prefix in var.CURED_LYCANS:
                var.CURED_LYCANS.remove(prefix)
                var.CURED_LYCANS.append(nick)
            if prefix in var.ALPHA_WOLVES:
                var.ALPHA_WOLVES.remove(prefix)
                var.ALPHA_WOLVES.append(nick)
            with var.GRAVEYARD_LOCK:  # to be safe
                if prefix in var.LAST_SAID_TIME.keys():
                    var.LAST_SAID_TIME[nick] = var.LAST_SAID_TIME.pop(prefix)
                if prefix in var.IDLE_WARNED:
                    var.IDLE_WARNED.remove(prefix)
                    var.IDLE_WARNED.add(nick)
                if prefix in var.IDLE_WARNED_PM:
                    var.IDLE_WARNED_PM.remove(prefix)
                    var.IDLE_WARNED_PM.add(nick)

        if var.PHASE == "day":
            if prefix in var.WOUNDED:
                var.WOUNDED.remove(prefix)
                var.WOUNDED.append(nick)
            if prefix in var.INVESTIGATED:
                var.INVESTIGATED.remove(prefix)
                var.INVESTIGATED.append(prefix)
            if prefix in var.VOTES:
                var.VOTES[nick] = var.VOTES.pop(prefix)
            for v in var.VOTES.values():
                if prefix in v:
                    v.remove(prefix)
                    v.append(nick)

    # Check if he was DC'ed
    if var.PHASE in ("night", "day"):
        with var.GRAVEYARD_LOCK:
            if nick in var.DISCONNECTED.keys():
                clk = var.DISCONNECTED[nick][1]
                act = var.DISCONNECTED[nick][0]
                if nick in var.USERS:
                    cloak = var.USERS[nick]["cloak"]
                    acc = var.USERS[nick]["account"]
                else:
                    acc = None
                if not acc or acc == "*":
                    acc = None
                if (acc and acc == act) or (cloak == clk and not var.ACCOUNTS_ONLY):
                    cli.mode(chan, "+v", nick, nick+"!*@*")
                    del var.DISCONNECTED[nick]
                    var.LAST_SAID_TIME[nick] = datetime.now()
                    cli.msg(chan, "\02{0}\02 has returned to the village.".format(nick))
                    for r,rlist in var.ORIGINAL_ROLES.items():
                        if "(dced)"+nick in rlist:
                            rlist.remove("(dced)"+nick)
                            rlist.append(nick)
                            break
                    if nick in var.DCED_PLAYERS.keys():
                        var.PLAYERS[nick] = var.DCED_PLAYERS.pop(nick)

    if prefix in var.NO_LYNCH:
        var.NO_LYNCH.remove(prefix)
        var.NO_LYNCH.append(nick)

def leave(cli, what, nick, why=""):
    nick, _, _, cloak = parse_nick(nick)
    if nick in var.USERS:
        acc = var.USERS[nick]["account"]
        cloak = var.USERS[nick]["cloak"]
        if not what in ("account",):
            var.USERS[nick]["inchan"] = False
    else:
        acc = None
    if not acc or acc == "*":
        acc = None

    if what in ("part", "kick") and why != botconfig.CHANNEL: return

    if why and why == botconfig.CHANGING_HOST_QUIT_MESSAGE:
        return
    if var.PHASE == "none":
        return
    # only mark living players as dced, unless they were kicked
    if nick in var.PLAYERS and (what == "kick" or nick in var.list_players()):
        # must prevent double entry in var.ORIGINAL_ROLES
        for r,rlist in var.ORIGINAL_ROLES.items():
            if nick in rlist:
                var.ORIGINAL_ROLES[r].remove(nick)
                var.ORIGINAL_ROLES[r].append("(dced)"+nick)
                break
        var.DCED_PLAYERS[nick] = var.PLAYERS.pop(nick)
    if nick not in var.list_players() or nick in var.DISCONNECTED.keys():
        return

    #  the player who just quit was in the game
    killplayer = True

    population = ""

    if var.PHASE == "join":
        lpl = len(var.list_players()) - 1
        if lpl == 0:
            population = (" No more players remaining.")
        else:
            population = (" New player count: \u0002{0}\u0002").format(lpl)

    if what == "part" and (not var.PART_GRACE_TIME or var.PHASE == "join"):
        if var.get_role(nick) != "person" and var.ROLE_REVEAL:
            msg = ("\02{0}\02, a \02{1}\02, ate some poisonous berries and has "+
                   "died.{2}").format(nick, var.get_reveal_role(nick), population)
        else:
            msg = ("\02{0}\02 ate some poisonous berries and has died.{1}").format(nick, population)
    elif what == "quit" and (not var.QUIT_GRACE_TIME or var.PHASE == "join"):
        if var.get_role(nick) != "person" and var.ROLE_REVEAL:
            msg = ("\02{0}\02 was mauled by wild animals and has died. It seems that "+
                   "\02{1}\02 meat is tasty.{2}").format(nick, var.get_reveal_role(nick), population)
        else:
            msg = ("\02{0}\02 was mauled by wild animals and has died.{1}").format(nick, population)
    elif what == "account" and (not var.ACC_GRACE_TIME or var.PHASE == "join"):
        if var.get_role(nick) != "person" and var.ROLE_REVEAL:
            msg = ("\02{0}\02 has died of a heart attack. The villagers couldn't "+
                   "save the \02{1}\02.{2}").format(nick, var.get_reveal_role(nick), population)
        else:
            msg = ("\02{0}\02 has died of a heart attack.{1}").format(nick, population)
    elif what != "kick":
        msg = "\u0002{0}\u0002 has gone missing.".format(nick)
        killplayer = False
    else:
        if var.get_role(nick) != "person" and var.ROLE_REVEAL:
            msg = ("\02{0}\02 died due to falling off a cliff. The "+
                   "\02{1}\02 is lost to the ravine forever.{2}").format(nick, var.get_reveal_role(nick), population)
        else:
            msg = ("\02{0}\02 died due to falling off a cliff.{1}").format(nick, population)
        make_stasis(nick, var.LEAVE_STASIS_PENALTY)
    cli.msg(botconfig.CHANNEL, msg)
    if nick in var.USERS:
        var.USERS[nick]["modes"] = set()
        var.USERS[nick]["moded"] = set()
    if killplayer:
        del_player(cli, nick, death_triggers = False)
    else:
        var.DISCONNECTED[nick] = (acc, cloak, datetime.now(), what)

#Functions decorated with hook do not parse the nick by default
hook("part")(lambda cli, nick, *rest: leave(cli, "part", nick, rest[0]))
hook("quit")(lambda cli, nick, *rest: leave(cli, "quit", nick, rest[0]))
hook("kick")(lambda cli, nick, *rest: leave(cli, "kick", rest[1], rest[0]))


@cmd("quit", "leave", join=True, game=True, playing=True)
def leave_game(cli, nick, chan, rest):
    """Quits the game."""
    if var.PHASE == "join":
        lpl = len(var.list_players()) - 1

        if lpl == 0:
            population = (" No more players remaining.")
        else:
            population = (" New player count: \u0002{0}\u0002").format(lpl)
    else:
        dur = int(var.START_QUIT_DELAY - (datetime.now() - var.GAME_START_TIME).total_seconds())
        if var.START_QUIT_DELAY and dur > 0:
            cli.notice(nick, "The game already started! If you still want to quit, try again in {0} second{1}.".format(dur, "" if dur == 1 else "s"))
            return
        population = ""
    if var.get_role(nick) != "person" and var.ROLE_REVEAL:
        role = var.get_reveal_role(nick)
        an = "n" if role[0] in ("a", "e", "i", "o", "u") else ""
        if var.DYNQUIT_DURING_GAME:
            lmsg = random.choice(var.QUIT_MESSAGES).format(nick, an, role)
            cli.msg(botconfig.CHANNEL, lmsg)
        else:
            cli.msg(botconfig.CHANNEL, ("\02{0}\02, a \02{1}\02, has died of an unknown disease.{2}").format(nick, role, population))
    else:
        # DYNQUIT_DURING_GAME should not have any effect during the join phase, so only check if we aren't in that
        if var.PHASE != "join" and not var.DYNQUIT_DURING_GAME:
            cli.msg(botconfig.CHANNEL, ("\02{0}\02 has died of an unknown disease.{1}").format(nick, population))
        else:
            lmsg = random.choice(var.QUIT_MESSAGES_NO_REVEAL).format(nick) + population
            cli.msg(botconfig.CHANNEL, lmsg)
    if var.PHASE != "join":
        for r, rlist in var.ORIGINAL_ROLES.items():
            if nick in rlist:
                var.ORIGINAL_ROLES[r].remove(nick)
                var.ORIGINAL_ROLES[r].append("(dced)"+nick)
        make_stasis(nick, var.LEAVE_STASIS_PENALTY)
        if nick in var.PLAYERS:
            var.DCED_PLAYERS[nick] = var.PLAYERS.pop(nick)

    del_player(cli, nick, death_triggers = False)

def begin_day(cli):
    chan = botconfig.CHANNEL

    # Reset nighttime variables
    var.GAMEPHASE = "day"
    var.KILLS = {}  # nicknames of kill victims (wolves only)
    var.OTHER_KILLS = {} # other kill victims (hunter/vengeful ghost/death totem)
    var.KILLER = ""  # nickname of who chose the victim
    var.SEEN = []  # list of seers/oracles/augurs that have had visions
    var.HEXED = [] # list of hags that have silenced others
    var.SHAMANS = [] # list of shamans/crazed shamans that have acted
    var.OBSERVED = {}  # those whom werecrows/sorcerers have observed
    var.HVISITED = {} # those whom harlots have visited
    var.GUARDED = {}  # this whom bodyguards/guardian angels have guarded
    var.PASSED = [] # hunters that have opted not to kill
    var.STARTED_DAY_PLAYERS = len(var.list_players())
    var.SILENCED = copy.copy(var.TOBESILENCED)
    var.LYCANTHROPES = copy.copy(var.TOBELYCANTHROPES)
    var.LUCKY = copy.copy(var.TOBELUCKY)
    var.DISEASED = copy.copy(var.TOBEDISEASED)
    var.MISDIRECTED = copy.copy(var.TOBEMISDIRECTED)
    var.EXCHANGED = copy.copy(var.TOBEEXCHANGED)

    msg = ("The villagers must now vote for whom to lynch. "+
           'Use "{0}lynch <nick>" to cast your vote. {1} votes '+
           'are required to lynch.').format(botconfig.CMD_CHAR, len(var.list_players()) // 2 + 1)
    cli.msg(chan, msg)

    var.DAY_ID = time.time()
    if var.DAY_TIME_WARN > 0:
        if var.STARTED_DAY_PLAYERS <= var.SHORT_DAY_PLAYERS:
            t1 = threading.Timer(var.SHORT_DAY_WARN, hurry_up, [cli, var.DAY_ID, False])
            l = var.SHORT_DAY_WARN
        else:
            t1 = threading.Timer(var.DAY_TIME_WARN, hurry_up, [cli, var.DAY_ID, False])
            l = var.DAY_TIME_WARN
        var.TIMERS["day_warn"] = (t1, var.DAY_ID, l)
        t1.daemon = True
        t1.start()

    if var.DAY_TIME_LIMIT > 0:  # Time limit enabled
        if var.STARTED_DAY_PLAYERS <= var.SHORT_DAY_PLAYERS:
            t2 = threading.Timer(var.SHORT_DAY_LIMIT, hurry_up, [cli, var.DAY_ID, True])
            l = var.SHORT_DAY_LIMIT
        else:
            t2 = threading.Timer(var.DAY_TIME_LIMIT, hurry_up, [cli, var.DAY_ID, True])
            l = var.DAY_TIME_LIMIT
        var.TIMERS["day"] = (t2, var.DAY_ID, l)
        t2.daemon = True
        t2.start()

def night_warn(cli, gameid):
    if gameid != var.NIGHT_ID:
        return

    if var.PHASE == "day":
        return

    cli.msg(botconfig.CHANNEL, ("\02A few villagers awake early and notice it " +
                                "is still dark outside. " +
                                "The night is almost over and there are " +
                                "still whispers heard in the village.\02"))

def transition_day(cli, gameid=0):
    if gameid:
        if gameid != var.NIGHT_ID:
            return
    var.NIGHT_ID = 0

    if var.PHASE == "day":
        return

    var.PHASE = "day"
    var.GOATED = False
    chan = botconfig.CHANNEL

    if not var.START_WITH_DAY or not var.FIRST_DAY:
        # In case people didn't act at night, clear appropriate variables
        if len(var.SHAMANS) < len(var.ROLES["shaman"] + var.ROLES["crazed shaman"]):
            for shaman in var.ROLES["shaman"]:
                if shaman not in var.SHAMANS:
                    var.LASTGIVEN[shaman] = None
            for shaman in var.ROLES["crazed shaman"]:
                if shaman not in var.SHAMANS:
                    var.LASTGIVEN[shaman] = None

        # bodyguard doesn't have restrictions, but being checked anyway since both GA and bodyguard use var.GUARDED
        if len(var.GUARDED.keys()) < len(var.ROLES["bodyguard"] + var.ROLES["guardian angel"]):
            for gangel in var.ROLES["guardian angel"]:
                if gangel not in var.GUARDED:
                    var.LASTGUARDED[gangel] = None

        if len(var.HEXED) < len(var.ROLES["hag"]):
            for hag in var.ROLES["hag"]:
                if hag not in var.HEXED:
                    var.LASTHEXED[hag] = None

        # Select a random target for vengeful ghost if they didn't kill
        wolves = var.list_players(var.WOLFTEAM_ROLES)
        villagers = var.list_players()
        for wolf in wolves:
            villagers.remove(wolf)
        for ghost, target in var.VENGEFUL_GHOSTS.items():
            if target[0] == "!":
                continue
            if ghost not in var.OTHER_KILLS:
                if target == "wolves":
                    var.OTHER_KILLS[ghost] = random.choice(wolves)
                else:
                    var.OTHER_KILLS[ghost] = random.choice(villagers)

    # Reset daytime variables
    var.VOTES = {}
    var.INVESTIGATED = []
    var.WOUNDED = []
    var.DAY_START_TIME = datetime.now()
    var.NO_LYNCH = []
    var.DAY_COUNT += 1
    var.FIRST_DAY = (var.DAY_COUNT == 1)
    havetotem = copy.copy(var.LASTGIVEN)

    if var.START_WITH_DAY and var.FIRST_DAY:
        # TODO: need to message everyone their roles and give a short thing saying "it's daytime"
        # but this is good enough for now to prevent it from crashing
        begin_day(cli)
        return

    if (not len(var.SEEN)+len(var.KILLS)+len(var.OBSERVED) # neither seer nor wolf acted
            and not var.START_WITH_DAY and var.FIRST_NIGHT and (var.ROLES["seer"] or var.ROLES["oracle"] or var.ROLES["augur"]) and not botconfig.DEBUG_MODE):
        cli.msg(botconfig.CHANNEL, "\02The wolves all die of a mysterious plague.\02")
        for x in var.ROLES["traitor"] + var.list_players(var.WOLF_ROLES):
            if not del_player(cli, x, True, death_triggers = False):
                return

    td = var.DAY_START_TIME - var.NIGHT_START_TIME
    var.NIGHT_START_TIME = None
    var.NIGHT_TIMEDELTA += td
    min, sec = td.seconds // 60, td.seconds % 60

    # determine if we need to play the new wolf message due to bitten people
    new_wolf = False
    for (p, v) in var.BITTEN.items():
        if v <= 0:
            new_wolf = True
            break

    found = {}
    for v in var.KILLS.values():
        for p in v:
            if p in found:
                found[p] += 1
            else:
                found[p] = 1

    maxc = 0
    victims = []
    bitten = []
    killers = {} # dict of victim: list of killers (for retribution totem)
    bywolves = set() # wolves targeted, others may have as well (needed for harlot visit and maybe other things)
    onlybywolves = set() # wolves and nobody else targeted (needed for lycan)
    dups = []
    for v, c in found.items():
        if c > maxc:
            maxc = c
            dups = [v]
        elif c == maxc:
            dups.append(v)

    if maxc and dups:
        victim = random.choice(dups)
        victims.append(victim)
        bywolves.add(victim)
        onlybywolves.add(victim)
        if victim in killers:
            killers[victim].append("@wolves") # special key to let us know to randomly select a wolf
        else:
            killers[victim] = ["@wolves"]

    if victims and var.ANGRY_WOLVES:
        # they got a 2nd kill
        del found[victims[0]]
        maxc = 0
        dups = []
        for v, c in found.items():
            if c > maxc:
                maxc = c
                dups = [v]
            elif c == maxc:
                dups.append(v)
        if maxc and dups:
            victim = random.choice(dups)
            victims.append(victim)
            bywolves.add(victim)
            onlybywolves.add(victim)
            if victim in killers:
                killers[victim].append("@wolves") # special key to let us know to randomly select a wolf
            else:
                killers[victim] = ["@wolves"]

    if var.ALPHA_ENABLED: # check for bites
        for (alpha, desired) in var.BITE_PREFERENCES.items():
            if len(bywolves) == 0:
                # nobody to bite, so let them do it again in the future
                var.ALPHA_WOLVES.remove(alpha)
                continue
            if len(bywolves) == 1:
                target = tuple(bywolves)[0]
            elif desired in bywolves:
                target = desired
            else:
                target = random.choice(tuple(bywolves))
            pm(cli, alpha, "You have bitten \u0002{0}\u0002.".format(target))
            targetrole = var.get_role(target)
            # do the usual checks; we can only bite those that would otherwise die
            # (e.g. block it on visiting harlot, GA/bodyguard, and protection totem)
            # also if a lycan is bitten, just turn them into a wolf immediately
            if (target not in var.PROTECTED
                    and target not in var.GUARDED.values()
                    and (target not in var.ROLES["harlot"] or not var.HVISITED.get(target))
                    and target not in var.ROLES["lycan"]
                    and target not in var.LYCANTHROPES
                    and target not in var.IMMUNIZED):
                var.BITTEN[target] = var.ALPHA_WOLF_NIGHTS
                bitten.append(target)
                victims.remove(target)
                bywolves.remove(target)
                onlybywolves.remove(target)
                killers[target].remove("@wolves")
            else:
                # bite was unsuccessful, let them try again
                var.ALPHA_WOLVES.remove(alpha)

    var.BITE_PREFERENCES = {}

    for monster in var.ROLES["monster"]:
        if monster in victims:
            victims.remove(monster)
            bywolves.discard(monster)
            onlybywolves.discard(monster)

    wolfghostvictims = []
    for ghost, target in var.VENGEFUL_GHOSTS.items():
        if target == "villagers":
            victim = var.OTHER_KILLS[ghost]
            if victim in killers:
                killers[victim].append(ghost)
            else:
                killers[victim] = [ghost]
            if victim not in var.DYING: # wolf ghost killing ghost will take precedence over everything except death totem and elder
                wolfghostvictims.append(victim)

    for k, d in var.OTHER_KILLS.items():
        victims.append(d)
        onlybywolves.discard(d)
        if d in killers:
            killers[d].append(k)
        else:
            killers[d] = [k]
    for d in var.DYING:
        victims.append(d)
        onlybywolves.discard(d)
        for s, v in var.LASTGIVEN.items():
            if v == d and var.TOTEMS[s] == "death":
                if d in killers:
                    killers[d].append(s)
                else:
                    killers[d] = [s]
    victims_set = set(victims) # remove duplicates
    victims_set.discard(None) # in the event that ever happens
    victims = []
    vappend = []
    # Ensures that special events play for bodyguard and harlot-visiting-victim so that kill can
    # be correctly attributed to wolves (for vengeful ghost lover), and that any gunner events
    # can play. Harlot visiting wolf doesn't play special events if they die via other means since
    # that assumes they die en route to the wolves (and thus don't shoot/give out gun/etc.)
    for v in victims_set:
        if v in var.ROLES["bodyguard"] and var.GUARDED.get(v) in victims_set:
            vappend.append(v)
        elif v in var.ROLES["harlot"] and var.HVISITED.get(v) in victims_set:
            vappend.append(v)
        else:
            victims.append(v)
    prevlen = var.MAX_PLAYERS + 10
    while len(vappend) > 0:
        if len(vappend) == prevlen:
            # have a circular dependency, try to break it by appending the next value
            v = vappend[0]
            vappend.remove(v)
            victims.append(v)
            continue

        prevlen = len(vappend)
        for v in copy.copy(vappend):
            if v in var.ROLES["bodyguard"] and var.GUARDED.get(v) not in vappend:
                vappend.remove(v)
                victims.append(v)
            elif v in var.ROLES["harlot"] and var.HVISITED.get(v) not in vappend:
                vappend.remove(v)
                victims.append(v)

    # Select a random target for assassin that isn't already going to die if they didn't target
    pl = var.list_players()
    for ass in var.ROLES["assassin"]:
        if ass not in var.TARGETED and ass not in var.SILENCED:
            ps = pl[:]
            ps.remove(ass)
            for victim in victims:
                if victim in ps:
                    ps.remove(victim)
            if len(ps) > 0:
                target = random.choice(ps)
                var.TARGETED[ass] = target
                pm(cli, ass, "Because you forgot to select a target at night, you are now targeting \u0002{0}\u0002.".format(target))
    if var.FIRST_NIGHT:
        for clone in var.ROLES["clone"]:
            if clone not in var.CLONED:
                ps = pl[:]
                ps.remove(clone)
                for victim in victims:
                    if victim in ps:
                        ps.remove(victim)
                if len(ps) > 0:
                    target = random.choice(ps)
                    var.CLONED[clone] = target
                    pm(cli, clone, "Because you forgot to select someone to clone at night, you are now cloning \u0002{0}\u0002.".format(target))


    message = [("Night lasted \u0002{0:0>2}:{1:0>2}\u0002. It is now daytime. "+
               "The villagers awake, thankful for surviving the night, "+
               "and search the village... ").format(min, sec)]

    # This needs to go down here since having them be their night value matters above
    var.ANGRY_WOLVES = False
    var.DISEASED_WOLVES = False
    var.ALPHA_ENABLED = False

    dead = []
    for crow, target in iter(var.OBSERVED.items()):
        if crow not in var.ROLES["werecrow"]:
            continue
        if ((target in list(var.HVISITED.keys()) and var.HVISITED[target]) or  # if var.HVISITED[target] is None, harlot visited self
            target in var.SEEN or target in var.SHAMANS or (target in list(var.GUARDED.keys()) and var.GUARDED[target])):
            pm(cli, crow, ("As the sun rises, you conclude that \u0002{0}\u0002 was not in "+
                          "bed all night, and you fly back to your house.").format(target))
        else:
            pm(cli, crow, ("As the sun rises, you conclude that \u0002{0}\u0002 was sleeping "+
                          "all night long, and you fly back to your house.").format(target))

    vlist = copy.copy(victims)
    novictmsg = True
    if new_wolf:
        message.append("A chilling howl was heard last night. It appears there is another werewolf in our midst!")
        novictmsg = False

    for victim in vlist:
        if victim in var.ROLES["harlot"] and var.HVISITED.get(victim) and victim not in var.DYING and victim not in dead and victim in onlybywolves:
            message.append("The wolves' selected victim was a harlot, who was not at home last night.")
            novictmsg = False
        elif victim in var.PROTECTED and victim not in var.DYING:
            message.append(("\u0002{0}\u0002 was attacked last night, but their totem " +
                            "emitted a brilliant flash of light, blinding the attacker and " +
                            "allowing them to escape.").format(victim))
            novictmsg = False
        elif victim in var.GUARDED.values() and victim not in var.DYING:
            for gangel in var.ROLES["guardian angel"]:
                if var.GUARDED.get(gangel) == victim:
                    message.append(("\u0002{0}\u0002 was attacked last night, but luckily, the guardian angel was on duty.").format(victim))
                    novictmsg = False
                    break
            else:
                for bodyguard in var.ROLES["bodyguard"]:
                    if var.GUARDED.get(bodyguard) == victim:
                        dead.append(bodyguard)
                        message.append(("\u0002{0}\u0002 sacrificed their life to guard that of another.").format(bodyguard))
                        novictmsg = False
                        break
        elif (victim in var.ROLES["lycan"] or victim in var.LYCANTHROPES) and victim in onlybywolves and victim not in var.IMMUNIZED:
            message.append("A chilling howl was heard last night. It appears there is another werewolf in our midst!")
            pm(cli, victim, 'HOOOOOOOOOWL. You have become... a wolf!')
            vrole = var.get_role(victim)
            var.ROLES[vrole].remove(victim)
            var.ROLES["wolf"].append(victim)
            var.LYCANS.append(victim)
            wolves = var.list_players(var.WOLFCHAT_ROLES)
            random.shuffle(wolves)
            wolves.remove(victim)  # remove self from list
            for i, wolf in enumerate(wolves):
                pm(cli, wolf, "\u0002{0}\u0002 is now a wolf!".format(victim))
                role = var.get_role(wolf)
                cursed = ""
                if wolf in var.ROLES["cursed villager"]:
                    cursed = "cursed "
                wolves[i] = "\u0002{0}\u0002 ({1}{2})".format(wolf, cursed, role)

            pm(cli, victim, "Wolves: " + ", ".join(wolves))
            novictmsg = False
        elif victim not in dead: # not already dead via some other means
            if victim in var.RETRIBUTION:
                loser = random.choice(killers[victim])
                if loser == "@wolves":
                    wolves = var.list_players(var.WOLF_ROLES)
                    for crow in var.ROLES["werecrow"]:
                        if crow in var.OBSERVED:
                            wolves.remove(crow)
                    loser = random.choice(wolves)
                if loser in var.VENGEFUL_GHOSTS.keys():
                    # mark ghost as being unable to kill any more
                    var.VENGEFUL_GHOSTS[loser] = "!" + var.VENGEFUL_GHOSTS[loser]
                    message.append(("\u0002{0}\u0002's totem emitted a brilliant flash of light last night. " +
                                    "It appears that \u0002{1}\u0002's spirit was driven away by the flash.").format(victim, loser))
                else:
                    dead.append(loser)
                    if var.ROLE_REVEAL:
                        role = var.get_reveal_role(loser)
                        an = "n" if role[0] in ("a", "e", "i", "o", "u") else ""
                        message.append(("\u0002{0}\u0002's totem emitted a brilliant flash of light last night. " +
                                        "The dead body of \u0002{1}\u0002, a{2} \u0002{3}\u0002, was found at the scene.").format(victim, loser, an, role))
                    else:
                        message.append(("\u0002{0}\u0002's totem emitted a brilliant flash of light last night. " +
                                        "The dead body of \u0002{1}\u0002 was found at the scene.").format(victim, loser))
            if var.ROLE_REVEAL:
                role = var.get_reveal_role(victim)
                an = "n" if role[0] in ("a", "e", "i", "o", "u") else ""
                message.append(("The dead body of \u0002{0}\u0002, a{1} \u0002{2}\u0002, is found. " +
                                "Those remaining mourn the tragedy.").format(victim, an, role))
            else:
                message.append(("The dead body of \u0002{0}\u0002 is found. " +
                                "Those remaining mourn the tragedy.").format(victim))
            dead.append(victim)
            if random.random() < 1/50:
                message.append(random.choice(
                    ["https://i.imgur.com/nO8rZ.gif",
                    "https://i.imgur.com/uGVfZ.gif",
                    "https://i.imgur.com/mUcM09n.gif",
                    "https://i.imgur.com/P7TEGyQ.gif",
                    "https://i.imgur.com/b8HAvjL.gif",
                    "https://i.imgur.com/PIIfL15.gif"]
                    ))
            
            if victim in var.HVISITED.values() and victim in bywolves:  #  victim was visited by some harlot and victim was attacked by wolves
                for hlt in var.HVISITED.keys():
                    if var.HVISITED[hlt] == victim:
                        message.append(("\02{0}\02, a \02harlot\02, made the unfortunate mistake of "+
                                        "visiting the victim's house last night and is "+
                                        "now dead.").format(hlt))
                        bywolves.add(hlt)
                        onlybywolves.add(hlt)
                        dead.append(hlt)

    if novictmsg and len(dead) == 0:
        message.append(random.choice(var.NO_VICTIMS_MESSAGES) + " All villagers, however, have survived.")

    for harlot in var.ROLES["harlot"]:
        if var.HVISITED.get(harlot) in var.list_players(var.WOLF_ROLES) and harlot not in dead:
            message.append(("\02{0}\02, a \02harlot\02, made the unfortunate mistake of "+
                            "visiting a wolf's house last night and is "+
                            "now dead.").format(harlot))
            bywolves.add(harlot)
            onlybywolves.add(harlot)
            dead.append(harlot)
    for bodyguard in var.ROLES["bodyguard"]:
        if var.GUARDED.get(bodyguard) in var.list_players(var.WOLF_ROLES) and bodyguard not in dead:
            bywolves.add(bodyguard)
            onlybywolves.add(bodyguard)
            r = random.random()
            if r < var.BODYGUARD_DIES_CHANCE:
                if var.ROLE_REVEAL:
                    message.append(("\02{0}\02, a \02bodyguard\02, "+
                                    "made the unfortunate mistake of guarding a wolf "+
                                    "last night, and is now dead.").format(bodyguard))
                else:
                    message.append(("\02{0}\02 "+
                                    "made the unfortunate mistake of guarding a wolf "+
                                    "last night, and is now dead.").format(bodyguard))
                dead.append(bodyguard)
    for gangel in var.ROLES["guardian angel"]:
        if var.GUARDED.get(gangel) in var.list_players(var.WOLF_ROLES) and gangel not in dead:
            bywolves.add(gangel)
            onlybywolves.add(gangel)
            r = random.random()
            if r < var.GUARDIAN_ANGEL_DIES_CHANCE:
                if var.ROLE_REVEAL:
                    message.append(("\02{0}\02, a \02guardian angel\02, "+
                                    "made the unfortunate mistake of guarding a wolf "+
                                    "last night, and is now dead.").format(gangel))
                else:
                    message.append(("\02{0}\02 "+
                                    "made the unfortunate mistake of guarding a wolf "+
                                    "last night, and is now dead.").format(gangel))
                dead.append(gangel)

    for victim in list(dead):
        if victim in var.GUNNERS.keys() and var.GUNNERS[victim] > 0 and victim in bywolves:
            if random.random() < var.GUNNER_KILLS_WOLF_AT_NIGHT_CHANCE:
                wc = var.ROLES["werecrow"][:]
                for crow in wc:
                    if crow in var.OBSERVED.keys():
                        wc.remove(crow)
                # don't kill off werecrows that observed
                deadwolf = random.choice(var.ROLES["wolf"]+var.ROLES["wolf cub"]+wc)
                if deadwolf not in dead:
                    if var.ROLE_REVEAL:
                        message.append(("Fortunately, \02{0}\02 had bullets and "+
                                        "\02{1}\02, a \02{2}\02, was shot dead.").format(victim, deadwolf, var.get_reveal_role(deadwolf)))
                    else:
                        message.append(("Fortunately, \02{0}\02 had bullets and "+
                                        "\02{1}\02 was shot dead.").format(victim, deadwolf))
                    dead.append(deadwolf)
                    var.GUNNERS[victim] -= 1 # deduct the used bullet

    for victim in dead:
        if victim in bywolves and victim in var.DISEASED:
            var.DISEASED_WOLVES = True

        if var.WOLF_STEALS_GUN and victim in bywolves and victim in var.GUNNERS.keys() and var.GUNNERS[victim] > 0:
            # victim has bullets
            try:
                while True:
                    guntaker = random.choice(var.list_players(var.WOLFCHAT_ROLES))  # random looter
                    if guntaker not in dead:
                        break
                numbullets = var.GUNNERS[victim]
                var.WOLF_GUNNERS[guntaker] = 1  # transfer bullets a wolf
                mmsg = ("While searching {0}'s belongings, you found " +
                        "a gun loaded with 1 silver bullet! " +
                        "You may only use it during the day. " +
                        "If you shoot at a wolf, you will intentionally miss. " +
                        "If you shoot a villager, it is likely that they will be injured.")
                mmsg = mmsg.format(victim)
                pm(cli, guntaker, mmsg)
            except IndexError:
                pass # no wolves to give gun to (they were all killed during night or something)
            var.GUNNERS[victim] = 0  # just in case

    cli.msg(chan, "\n".join(message))

    for chump in var.BITTEN.keys():
        if chump not in dead and var.get_role(chump) not in var.WOLF_ROLES:
            pm(cli, chump, get_bitten_message(chump))

    for chump in bitten:
        if chump not in dead and chump not in var.WOLF_ROLES:
            pm(cli, chump, "You woke up today feeling light-headed, and you notice some odd bite marks on your neck...")

    for deadperson in dead:  # kill each player, but don't end the game if one group outnumbers another
        # take a shortcut for killer_role here since vengeful ghost only cares about team and not particular roles
        # this will have to be modified to track the actual killer if that behavior changes
        # we check if they have already been killed as well since del_player could do chain reactions and we want
        # to avoid sending duplicate messages.
        if deadperson in var.list_players():
            del_player(cli, deadperson, end_game = False, killer_role = "wolf" if deadperson in onlybywolves or deadperson in wolfghostvictims else "villager", deadlist = dead, original = deadperson)

    message = []
    for havetotem in havetotem.values():
        if havetotem:
            message.append("\u0002{0}\u0002 seem{1} to be in possession of a mysterious totem...".format(havetotem, "ed" if havetotem in dead else "s"))
    cli.msg(chan, "\n".join(message))
    
    if chk_win(cli):  # if after the last person is killed, one side wins, then actually end the game here
        return

    begin_day(cli)

def chk_nightdone(cli):
    # TODO: alphabetize and/or arrange sensibly
    actedcount  = len(var.SEEN + list(var.HVISITED.keys()) + list(var.GUARDED.keys()) +
                      list(var.KILLS.keys()) + list(var.OTHER_KILLS.keys()) +
                      list(var.OBSERVED.keys()) + var.PASSED + var.HEXED + var.SHAMANS +
                      list(var.TARGETED.keys()))
    nightroles = (var.ROLES["seer"] + var.ROLES["oracle"] + var.ROLES["harlot"] +
                  var.ROLES["bodyguard"] + var.ROLES["guardian angel"] + var.ROLES["wolf"] +
                  var.ROLES["werecrow"] + var.ROLES["alpha wolf"] + var.ROLES["sorcerer"] + var.ROLES["hunter"] +
                  list(var.VENGEFUL_GHOSTS.keys()) + var.ROLES["hag"] + var.ROLES["shaman"] +
                  var.ROLES["crazed shaman"] + var.ROLES["assassin"] + var.ROLES["augur"])
    if var.FIRST_NIGHT:
        actedcount += len(var.MATCHMAKERS + list(var.CLONED.keys()))
        nightroles += var.ROLES["matchmaker"] + var.ROLES["clone"]

    if var.DISEASED_WOLVES:
        nightroles = [p for p in nightroles if p not in var.ROLES["wolf"] and p not in var.ROLES["alpha wolf"]]

    for p in var.HUNTERS:
        # only remove one instance of their name if they have used hunter ability, in case they have templates
        # the OTHER_KILLS check ensures we only remove them if they acted in a *previous* night
        if p in nightroles and p not in var.OTHER_KILLS:
            nightroles.remove(p)

    # but remove all instances of their name if they are silenced
    nightroles = [p for p in nightroles if p not in var.SILENCED]

    playercount = len(nightroles) + var.ACTED_EXTRA

    if var.PHASE == "night" and actedcount >= playercount:
        if not var.DISEASED_WOLVES:
            # flatten var.KILLS
            kills = set()
            for ls in var.KILLS.values():
                if not isinstance(ls, str):
                    for v in ls:
                        kills.add(v)
                else:
                    kills.add(ls)
            # check if wolves are actually agreeing
            # allow len(kills) == 0 through as that means that crow was dumb and observed instead
            # of killing or something, or weird cases where there are no wolves at night
            if not var.ANGRY_WOLVES and len(kills) > 1:
                return
            elif var.ANGRY_WOLVES and (len(kills) == 1 or len(kills) > 2):
                return

        for x, t in var.TIMERS.items():
            t[0].cancel()

        var.TIMERS = {}
        if var.PHASE == "night":  # Double check
            transition_day(cli)

@cmd("nolynch", "nl", "novote", "nv", "abstain", "abs", game=True, playing=True)
def no_lynch(cli, nick, chan, rest):
    """Allows you to abstain from voting for the day."""
    if chan == botconfig.CHANNEL:
        if not var.ABSTAIN_ENABLED:
            cli.notice(nick, "This command has been disabled by the admins.")
            return
        elif var.LIMIT_ABSTAIN and var.ABSTAINED:
            cli.notice(nick, "The village has already abstained once this game and may not do so again.")
            return
        elif var.LIMIT_ABSTAIN and var.FIRST_DAY:
            cli.notice(nick, "The village may not abstain on the first day.")
            return
        elif var.PHASE != "day":
            cli.notice(nick, "Lynching is only during the day. Please wait patiently for morning.")
            return
        elif nick in var.WOUNDED:
            cli.msg(chan, "{0}: You are wounded and resting, thus you are unable to vote for the day.".format(nick))
            return
        candidates = var.VOTES.keys()
        for voter in list(candidates):
            if nick in var.VOTES[voter]:
                var.VOTES[voter].remove(nick)
                if not var.VOTES[voter]:
                    del var.VOTES[voter]
        if nick not in var.NO_LYNCH:
            var.NO_LYNCH.append(nick)
        cli.msg(chan, "\u0002{0}\u0002 votes to not lynch anyone today.".format(nick))
        
        chk_decision(cli)
        return

@cmd("lynch", game=True, playing=True, pm=True)
def lynch(cli, nick, chan, rest):
    """Use this to vote for a candidate to be lynched."""
    if not rest:
        show_votes(cli, nick, chan, rest)
        return
    if chan != botconfig.CHANNEL:
        return

    rest = re.split(" +",rest)[0].strip()

    if var.PHASE != "day":
        cli.notice(nick, ("Lynching is only allowed during the day. "+
                          "Please wait patiently for morning."))
        return
    if nick in var.WOUNDED:
        cli.msg(chan, ("{0}: You are wounded and resting, "+
                      "thus you are unable to vote for the day.").format(nick))
        return
    if nick in var.ASLEEP:
        pm(cli, nick, "As you place your vote, your totem emits a brilliant flash of light. " +
                      "After recovering, you notice that you are still in your bed. " +
                      "That entire sequence of events must have just been a dream...")
        return
    if nick in var.NO_LYNCH:
        var.NO_LYNCH.remove(nick)

    voted = get_victim(cli, nick, rest, var.SELF_LYNCH_ALLOWED)
    if not voted:
        return

    if not var.SELF_LYNCH_ALLOWED:
        if nick == voted:
            if nick in var.ROLES["fool"] or nick in var.ROLES["jester"]:
                cli.notice(nick, "You may not vote yourself.")
            else:
                cli.notice(nick, "Please try to save yourself.")
            return

    lcandidates = list(var.VOTES.keys())
    for voters in lcandidates:  # remove previous vote
        if nick in var.VOTES[voters]:
            var.VOTES[voters].remove(nick)
            if not var.VOTES.get(voters) and voters != voted:
                del var.VOTES[voters]
            break
    if voted not in var.VOTES.keys():
        var.VOTES[voted] = [nick]
    else:
        var.VOTES[voted].append(nick)
    cli.msg(chan, ("\u0002{0}\u0002 votes for "+
                   "\u0002{1}\u0002.").format(nick, voted))

    var.LAST_VOTES = None # reset

    chk_decision(cli)


# chooses a target given nick, taking luck totem/misdirection totem into effect
# returns the actual target
def choose_target(actor, nick):
    pl = var.list_players()
    if actor in var.MISDIRECTED:
        i = var.ALL_PLAYERS.index(nick)
        if random.randint(0, 1) == 0:
            # going left
            while True:
                i -= 1
                if i < 0:
                    i = len(var.ALL_PLAYERS) - 1
                if var.ALL_PLAYERS[i] in pl:
                    nick = var.ALL_PLAYERS[i]
                    break
        else:
            # going right
            while True:
                i += 1
                if i >= len(var.ALL_PLAYERS):
                    i = 0
                if var.ALL_PLAYERS[i] in pl:
                    nick = var.ALL_PLAYERS[i]
                    break
    if nick in var.LUCKY:
        i = var.ALL_PLAYERS.index(nick)
        if random.randint(0, 1) == 0:
            # going left
            while True:
                i -= 1
                if i < 0:
                    i = len(var.ALL_PLAYERS) - 1
                if var.ALL_PLAYERS[i] in pl:
                    nick = var.ALL_PLAYERS[i]
                    break
        else:
            # going right
            while True:
                i += 1
                if i >= len(var.ALL_PLAYERS):
                    i = 0
                if var.ALL_PLAYERS[i] in pl:
                    nick = var.ALL_PLAYERS[i]
                    break
    return nick

# returns true if a swap happened
# check for that to short-circuit the nightrole
def check_exchange(cli, actor, nick):
    if nick in var.EXCHANGED:
        var.EXCHANGED.remove(nick)
        actor_role = var.get_role(actor)
        nick_role = var.get_role(nick)
        
        if actor_role == "amnesiac":
            actor_role = var.FINAL_ROLES[actor]
        elif actor_role == "clone":
            if actor in var.CLONED:
                actor_target = var.CLONED[actor]
                del var.CLONED[actor]
        elif actor_role in var.TOTEM_ORDER:
            actor_totem = var.TOTEMS[actor]
            del var.TOTEMS[actor]
            if actor in var.SHAMANS:
                var.ACTED_EXTRA += 1
                var.SHAMANS.remove(actor)
            if actor in var.LASTGIVEN:
                del var.LASTGIVEN[actor]
        elif actor_role == "wolf":
            if actor in var.KILLS:
                del var.KILLS[actor]
        elif actor_role == "hunter":
            if actor in var.OTHER_KILLS:
                var.ACTED_EXTRA += 1
            if actor in var.HUNTERS:
                var.HUNTERS.remove(actor)
            if actor in var.PASSED:
                var.PASSED.remove(actor)
        elif actor_role in ("bodyguard", "guardian angel"):
            if actor in var.GUARDED:
                pm(cli, var.GUARDED[actor], "Your protector seems to have disappeared...")
                del var.GUARDED[actor]
            if actor in var.LASTGUARDED:
                del var.LASTGUARDED[actor]
        elif actor_role in ("werecrow", "sorcerer"):
            if actor in var.OBSERVED:
                del var.OBSERVED[actor]
            if actor in var.KILLS:
                del var.KILLS[actor]
        elif actor_role == "harlot":
            if actor in var.HVISITED:
                pm(cli, var.HVISITED[actor], "\u0002{0}\u0002 seems to have disappeared...".format(actor))
                del var.HVISITED[actor]
        elif actor_role in ("seer", "oracle", "augur"):
            if actor in var.SEEN:
                var.SEEN.remove(actor)
        elif actor_role == "hag":
            if actor in var.LASTHEXED:
                if var.LASTHEXED[actor] in var.TOBESILENCED and actor in var.HEXED:
                    var.TOBESILENCED.remove(var.LASTHEXED[actor])
                del var.LASTHEXED[actor]
            if actor in var.HEXED:
                var.HEXED.remove(actor)
        elif actor_role == "doctor":
            if nick_role == "doctor":
                temp_immunizations = var.DOCTORS[actor]
                var.DOCTORS[actor] = var.DOCTORS[nick]
                var.DOCTORS[nick] = temp_immunizations
            else:
                var.DOCTORS[nick] = var.DOCTORS[actor]
                del var.DOCTORS[actor]
        elif actor_role == "alpha wolf":
            if actor in var.ALPHA_WOLVES:
                var.ALPHA_WOLVES.remove(actor)

        if nick_role == "amnesiac":
            nick_role = var.FINAL_ROLES[nick]
        elif nick_role == "clone":
            if nick in var.CLONED:
                nick_target = var.CLONED[nick]
                del var.CLONED[nick]
        elif nick_role in var.TOTEM_ORDER:
            nick_totem = var.TOTEMS[nick]
            del var.TOTEMS[nick]
            if nick in var.SHAMANS:
                var.ACTED_EXTRA += 1
                var.SHAMANS.remove(nick)
            if nick in var.LASTGIVEN:
                del var.LASTGIVEN[nick]
        elif nick_role == "wolf":
            if nick in var.KILLS:
                del var.KILLS[nick]
        elif nick_role == "hunter":
            if nick in var.OTHER_KILLS:
                var.ACTED_EXTRA += 1
            if nick in var.HUNTERS:
                var.HUNTERS.remove(nick)
            if nick in var.PASSED:
                var.PASSED.remove(nick)
        elif nick_role in ("bodyguard", "guardian angel"):
            if nick in var.GUARDED:
                pm(cli, var.GUARDED[nick], "Your protector seems to have disappeared...")
                del var.GUARDED[nick]
            if nick in var.LASTGUARDED:
                del var.LASTGUARDED[nick]
        elif nick_role in ("werecrow", "sorcerer"):
            if nick in var.OBSERVED:
                del var.OBSERVED[nick]
            if nick in var.KILLS:
                del var.KILLS[nick]
        elif nick_role == "harlot":
            if nick in var.HVISITED:
                pm(cli, var.HVISITED[nick], "\u0002{0}\u0002 seems to have disappeared...".format(nick))
                del var.HVISITED[nick]
        elif nick_role in ("seer", "oracle", "augur"):
            if nick in var.SEEN:
                var.SEEN.remove(nick)
        elif nick_role == "hag":
            if nick in var.LASTHEXED:
                if var.LASTHEXED[nick] in var.TOBESILENCED and nick in var.HEXED:
                    var.TOBESILENCED.remove(var.LASTHEXED[nick])
                del var.LASTHEXED[nick]
            if nick in var.HEXED:
                var.HEXED.remove(nick)
        elif nick_role == "doctor":
            # Both being doctors is handled above
            if actor_role != "doctor":
                var.DOCTORS[actor] = var.DOCTORS[nick]
                del var.DOCTORS[nick]
        elif nick_role == "alpha wolf":
            if nick in var.ALPHA_WOLVES:
                var.ALPHA_WOLVES.remove(nick)

            
        var.FINAL_ROLES[actor] = nick_role
        var.FINAL_ROLES[nick] = actor_role
        var.ROLES[actor_role].append(nick)
        var.ROLES[actor_role].remove(actor)
        var.ROLES[nick_role].append(actor)
        var.ROLES[nick_role].remove(nick)
        if actor in var.BITTEN_ROLES.keys():
            var.BITTEN_ROLES[actor] = nick_role
        if nick in var.BITTEN_ROLES.keys():
            var.BITTEN_ROLES[nick] = actor_role

        actor_rev_role = actor_role
        if actor_role == "vengeful ghost":
            actor_rev_role = var.DEFAULT_ROLE
        elif actor_role in ("village elder", "time lord"):
            actor_rev_role = "villager"

        nick_rev_role = nick_role
        if nick_role == "vengeful ghost":
            nick_rev_role = var.DEFAULT_ROLE
        elif actor_role in ("village elder", "time lord"):
            nick_rev_role = "villager"

        # don't say who, since misdirection/luck totem may have switched it
        # and this makes life far more interesting
        pm(cli, actor, "You have exchanged roles with someone! You are now a \u0002{0}\u0002.".format(nick_rev_role))
        pm(cli, nick,  "You have exchanged roles with someone! You are now a \u0002{0}\u0002.".format(actor_rev_role))

        if nick_role == "clone":
            pm(cli, actor, "You are cloning \u0002{0}\u0002.".format(nick_target))
        elif nick_role in var.TOTEM_ORDER:
            if nick_role == "shaman":
                pm(cli, actor, "You have a \u0002{0}\u0002 totem.".format(nick_totem))
            var.TOTEMS[actor] = nick_totem
        elif nick_role in var.WOLFCHAT_ROLES and actor_role not in var.WOLFCHAT_ROLES:    
            pl = var.list_players()
            random.shuffle(pl)
            pl.remove(actor)  # remove self from list
            for i, player in enumerate(pl):
                prole = var.get_role(player)
                if prole in var.WOLFCHAT_ROLES:
                    cursed = ""
                    if player in var.ROLES["cursed villager"]:
                        cursed = "cursed "
                    pl[i] = "\u0002{0}\u0002 ({1}{2})".format(player, cursed, prole)
                    pm(cli, player, "\u0002{0}\u0002 and \u0002{1}\u0002 have exchanged roles!".format(nick, actor))
                elif player in var.ROLES["cursed villager"]:
                    pl[i] = player + " (cursed)"

            pm(cli, actor, "Players: " + ", ".join(pl))
            angry_alpha = ''
            if var.DISEASED_WOLVES:
                pm(cli, actor, 'You are feeling ill tonight, and are unable to kill anyone.')
            elif var.ANGRY_WOLVES and actor_role in ("wolf", "werecrow", "alpha wolf"):
                pm(cli, actor, 'You are \u0002angry\u0002 tonight, and may kill two targets by using "kill <nick1> and <nick2>".')
                angry_alpha = ' <nick>'
            if var.ALPHA_ENABLED and actor_role == "alpha wolf" and actor not in var.ALPHA_WOLVES:
                pm(cli, actor, ('You may use "bite{0}" tonight in order to turn the wolves\' target into a wolf instead of killing them. ' +
                                'They will turn into a wolf in {1} night{2}.').format(angry_alpha, var.ALPHA_WOLF_NIGHTS, 's' if var.ALPHA_WOLF_NIGHTS > 1 else ''))
        elif nick_role == "minion":
            wolves = var.list_players(var.WOLF_ROLES)
            random.shuffle(wolves)
            pm(cli, actor, "Wolves: " + ", ".join(wolves))

        if actor_role == "clone":
            pm(cli, nick, "You are cloning \u0002{0}\u0002.".format(actor_target))
        elif actor_role in var.TOTEM_ORDER:
            if actor_role == "shaman":
                pm(cli, nick, "You have a \u0002{0}\u0002 totem.".format(actor_totem))
            var.TOTEMS[nick] = actor_totem
        elif actor_role in var.WOLFCHAT_ROLES and nick_role not in var.WOLFCHAT_ROLES:    
            pl = var.list_players()
            random.shuffle(pl)
            pl.remove(nick)  # remove self from list
            for i, player in enumerate(pl):
                prole = var.get_role(player)
                if prole in var.WOLFCHAT_ROLES:
                    cursed = ""
                    if player in var.ROLES["cursed villager"]:
                        cursed = "cursed "
                    pl[i] = "\u0002{0}\u0002 ({1}{2})".format(player, cursed, prole)
                    pm(cli, player, "\u0002{0}\u0002 and \u0002{1}\u0002 have exchanged roles!".format(actor, nick))
                elif player in var.ROLES["cursed villager"]:
                    pl[i] = player + " (cursed)"

            pm(cli, nick, "Players: " + ", ".join(pl))
            angry_alpha = ''
            if var.DISEASED_WOLVES:
                pm(cli, nick, 'You are feeling ill tonight, and are unable to kill anyone.')
            elif var.ANGRY_WOLVES and nick_role in ("wolf", "werecrow", "alpha wolf"):
                pm(cli, nick, 'You are \u0002angry\u0002 tonight, and may kill two targets by using "kill <nick1> and <nick2>".')
                angry_alpha = ' <nick>'
            if var.ALPHA_ENABLED and nick_role == "alpha wolf" and nick not in var.ALPHA_WOLVES:
                pm(cli, nick, ('You may use "bite{0}" tonight in order to turn the wolves\' target into a wolf instead of killing them. ' +
                               'They will turn into a wolf in {1} night{2}.').format(angry_alpha, var.ALPHA_WOLF_NIGHTS, 's' if var.ALPHA_WOLF_NIGHTS > 1 else ''))
        elif actor_role == "minion":
            wolves = var.list_players(var.WOLF_ROLES)
            random.shuffle(wolves)
            pm(cli, nick, "Wolves: " + ", ".join(wolves))

        return True
    return False

@cmd("retract", pm=True, game=True, playing=True)
def retract(cli, nick, chan, rest):
    """Takes back your vote during the day (for whom to lynch)."""

    if not chan in (botconfig.CHANNEL, nick):
        return

    if chan == nick: # PM, use different code
        role = var.get_role(nick)
        if role not in ("wolf", "werecrow", "alpha wolf", "hunter") and nick not in var.VENGEFUL_GHOSTS.keys():
            return
        if var.PHASE != "night":
            pm(cli, nick, "You may only retract at night.")
            return
        if role == "werecrow":  # Check if already observed
            if var.OBSERVED.get(nick):
                pm(cli, nick, ("You have already transformed into a crow, and "+
                               "cannot turn back until day."))
                return
        elif role == "hunter" and nick in var.HUNTERS and nick not in var.OTHER_KILLS.keys():
            return

        what = re.split(" +",rest)[0].strip().lower()
        if not what or what not in ("kill", "bite"):
            what = "kill"

        if what == "kill" and role in var.WOLF_ROLES and nick in var.KILLS.keys():
            del var.KILLS[nick]
            pm(cli, nick, "You have retracted your kill.")
        elif what == "kill" and role not in var.WOLF_ROLES and nick in var.OTHER_KILLS.keys():
            del var.OTHER_KILLS[nick]
            var.HUNTERS.remove(nick)
            pm(cli, nick, "You have retracted your kill.")
        elif what == "bite" and role == "alpha wolf" and nick in var.BITE_PREFERENCES.keys():
            del var.BITE_PREFERENCES[nick]
            var.ALPHA_WOLVES.remove(nick)
            pm(cli, nick, "You have decided to not bite anyone tonight.")
        else:
            pm(cli, nick, "You have not chosen to {0} anyone yet.".format(what))
        return

    if var.PHASE != "day":
        cli.notice(nick, ("Lynching is only allowed during the day. "+
                          "Please wait patiently for morning."))
        return
    if nick in var.NO_LYNCH:
        var.NO_LYNCH.remove(nick)
        cli.msg(chan, "\u0002{0}\u0002's vote was retracted.".format(nick))
        var.LAST_VOTES = None # reset
        return

    candidates = var.VOTES.keys()
    for voter in list(candidates):
        if nick in var.VOTES[voter]:
            var.VOTES[voter].remove(nick)
            if not var.VOTES[voter]:
                del var.VOTES[voter]
            cli.msg(chan, "\u0002{0}\u0002's vote was retracted.".format(nick))
            var.LAST_VOTES = None # reset
            break
    else:
        cli.notice(nick, "You haven't voted yet.")

@cmd("shoot", game=True, playing=True)
def shoot(cli, nick, chan, rest):
    """Use this to fire off a bullet at someone in the day if you have bullets."""

    if chan != botconfig.CHANNEL:
        return

    if var.PHASE != "day":
        cli.notice(nick, ("Shooting is only allowed during the day. "+
                          "Please wait patiently for morning."))
        return
    if nick not in var.GUNNERS.keys() and nick not in var.WOLF_GUNNERS.keys():
        cli.notice(nick, "You don't have a gun.")
        return
    elif ((nick in var.GUNNERS.keys() and not var.GUNNERS[nick]) or
          (nick in var.WOLF_GUNNERS.keys() and not var.WOLF_GUNNERS[nick])):
        cli.notice(nick, "You don't have any more bullets.")
        return
    elif nick in var.SILENCED:
        cli.notice(nick, "You have been silenced, and are unable to use any special powers.")
        return
    victim = get_victim(cli, nick, re.split(" +",rest)[0])
    if not victim:
        return
    if victim == nick:
        cli.notice(nick, "You are holding it the wrong way.")
        return
    # get actual victim
    victim = choose_target(nick, victim)

    wolfshooter = nick in var.list_players(var.WOLFCHAT_ROLES)

    if wolfshooter and nick in var.WOLF_GUNNERS:
        var.WOLF_GUNNERS[nick] -= 1
    else:
        var.GUNNERS[nick] -= 1

    rand = random.random()
    if nick in var.ROLES["village drunk"]:
        chances = var.DRUNK_GUN_CHANCES
    elif nick in var.ROLES["sharpshooter"]:
        chances = var.SHARPSHOOTER_GUN_CHANCES
    else:
        chances = var.GUN_CHANCES

    wolfvictim = victim in var.list_players(var.WOLF_ROLES)
    if rand <= chances[0] and not (wolfshooter and wolfvictim):  # didn't miss or suicide
        # and it's not a wolf shooting another wolf

        cli.msg(chan, ("\u0002{0}\u0002 shoots \u0002{1}\u0002 with "+
                       "a silver bullet!").format(nick, victim))
        realrole = var.get_role(victim)
        victimrole = var.get_reveal_role(victim)
        an = "n" if victimrole[0] in ('a', 'e', 'i', 'o', 'u') else ""
        if realrole in var.WOLF_ROLES:
            if var.ROLE_REVEAL:
                cli.msg(chan, ("\u0002{0}\u0002 is a{1} \u0002{2}\u0002, and is dying from "+
                               "the silver bullet.").format(victim,an, victimrole))
            else:
                cli.msg(chan, ("\u0002{0}\u0002 is a wolf, and is dying from "+
                               "the silver bullet.").format(victim))
            if not del_player(cli, victim, killer_role = var.get_role(nick)):
                return
        elif random.random() <= chances[3]:
            accident = "accidentally "
            if nick in var.ROLES["sharpshooter"]:
                accident = "" # it's an accident if the sharpshooter DOESN'T headshot :P
            cli.msg(chan, ("\u0002{0}\u0002 is not a wolf "+
                           "but was {1}fatally injured.").format(victim, accident))
            if var.ROLE_REVEAL:
                cli.msg(chan, "The village has sacrificed a{0} \u0002{1}\u0002.".format(an, victimrole))
            if not del_player(cli, victim, killer_role = var.get_role(nick)):
                return
        else:
            cli.msg(chan, ("\u0002{0}\u0002 is a villager and was injured. Luckily "+
                          "the injury is minor and will heal after a day of "+
                          "rest.").format(victim))
            if victim not in var.WOUNDED:
                var.WOUNDED.append(victim)
            lcandidates = list(var.VOTES.keys())
            for cand in lcandidates:  # remove previous vote
                if victim in var.VOTES[cand]:
                    var.VOTES[cand].remove(victim)
                    if not var.VOTES.get(cand):
                        del var.VOTES[cand]
                    break
            chk_decision(cli)
            chk_win(cli)
    elif rand <= chances[0] + chances[1]:
        cli.msg(chan, "\u0002{0}\u0002 is a lousy shooter and missed!".format(nick))
    else:
        if var.ROLE_REVEAL:
            cli.msg(chan, ("Oh no! \u0002{0}\u0002's gun was poorly maintained and has exploded! "+
                           "The village mourns a gunner-\u0002{1}\u0002.").format(nick, var.get_reveal_role(nick)))
        else:
            cli.msg(chan, ("Oh no! \u0002{0}\u0002's gun was poorly maintained and has exploded!").format(nick))
        if not del_player(cli, nick, killer_role = "villager"): # blame explosion on villager's shoddy gun construction or something
            return  # Someone won.



@cmd("kill", chan=False, pm=True, game=True)
def kill(cli, nick, chan, rest):
    if (nick not in var.VENGEFUL_GHOSTS.keys() and nick not in var.list_players()) or nick in var.DISCONNECTED.keys():
        cli.notice(nick, "You're not currently playing.")
        return
    try:
        role = var.get_role(nick)
    except KeyError:
        role = None
    wolfroles = list(var.WOLF_ROLES)
    wolfroles.remove("wolf cub")
    if role in var.WOLFCHAT_ROLES and role not in wolfroles:
        return  # they do this a lot.
    if role not in wolfroles + ["hunter"] and nick not in var.VENGEFUL_GHOSTS.keys():
        return
    if nick in var.VENGEFUL_GHOSTS.keys() and var.VENGEFUL_GHOSTS[nick][0] == "!":
        # ghost was driven away by retribution
        return
    if var.PHASE != "night":
        pm(cli, nick, "You may only kill people at night.")
        return
    if role == "hunter" and nick in var.HUNTERS and nick not in var.OTHER_KILLS:
        # they are a hunter and did not kill this night (if they killed this night, this allows them to switch)
        pm(cli, nick, "You have already killed someone this game.")
        return
    if nick in var.SILENCED:
        pm(cli, nick, "You have been silenced, and are unable to use any special powers.")
        return
    if role in wolfroles and var.DISEASED_WOLVES:
        pm(cli, nick, "You are feeling ill, and are unable to kill anyone tonight.")
        return
    pieces = re.split(" +",rest)
    victim = pieces[0]
    victim2 = None
    if role in wolfroles and var.ANGRY_WOLVES:
        if len(pieces) > 1:
            if len(pieces) > 2 and pieces[1].lower() == "and":
                victim2 = pieces[2]
            else:
                victim2 = pieces[1]
        else:
            victim2 = None
    if role == "werecrow":  # Check if flying to observe
        if var.OBSERVED.get(nick):
            pm(cli, nick, ("You have already transformed into a crow; therefore, "+
                           "you are physically unable to kill a villager."))
            return

    victim = get_victim(cli, nick, victim)
    if not victim:
        return
    if victim2 != None:
        victim2 = get_victim(cli, nick, victim2)
        if not victim2:
            return

    if victim == nick or victim2 == nick:
        if nick in var.VENGEFUL_GHOSTS.keys():
            pm(cli, nick, "You are already dead.")
        else:
            pm(cli, nick, "Suicide is bad. Don't do it.")
        return

    if nick in var.VENGEFUL_GHOSTS.keys():
        allwolves = var.list_players(var.WOLFTEAM_ROLES)
        allvills = []
        for p in var.list_players():
            if p not in allwolves:
                allvills.append(p)
        if var.VENGEFUL_GHOSTS[nick] == "wolves" and victim not in allwolves:
            pm(cli, nick, "You must target a wolf.")
            return
        elif var.VENGEFUL_GHOSTS[nick] == "villagers" and victim not in allvills:
            pm(cli, nick, "You must target a villager.")
            return

    if role in wolfroles:
        wolfchatwolves = var.list_players(var.WOLFCHAT_ROLES)
        if victim in wolfchatwolves or victim2 in wolfchatwolves:
            pm(cli, nick, "You may only kill villagers, not other wolves.")
            return
        if var.ANGRY_WOLVES and victim2 != None:
            if victim == victim2:
                pm(cli, nick, "You should select two different players.")
                return
            else:
                rv = choose_target(nick, victim)
                rv2 = choose_target(nick, victim2)
                if check_exchange(cli, nick, rv):
                    return
                if check_exchange(cli, nick, rv2):
                    return
                var.KILLS[nick] = [rv, rv2]
        else:
            rv = choose_target(nick, victim)
            if check_exchange(cli, nick, rv):
                return
            var.KILLS[nick] = [rv]
    else:
        rv = choose_target(nick, victim)
        if nick not in var.VENGEFUL_GHOSTS.keys():
            if check_exchange(cli, nick, rv):
                return
        var.OTHER_KILLS[nick] = rv
        if role == "hunter":
            if nick not in var.HUNTERS:
                var.HUNTERS.append(nick)
            if nick in var.PASSED:
                var.PASSED.remove(nick)

    if victim2 != None:
        pm(cli, nick, "You have selected \u0002{0}\u0002 and \u0002{1}\u0002 to be killed.".format(victim, victim2))
    else:
        pm(cli, nick, "You have selected \u0002{0}\u0002 to be killed.".format(victim))
        if var.ANGRY_WOLVES and role in wolfroles:
            pm(cli, nick, "You are angry tonight and may kill a second target. Use kill <nick1> and <nick2> to select multiple targets.")
    debuglog("{0} ({1}) KILL: {2} ({3})".format(nick, role, victim, var.get_role(victim)))
    if victim2:
        debuglog("{0} ({1}) KILL : {2} ({3})".format(nick, role, victim2, var.get_role(victim2)))
    chk_nightdone(cli)

@cmd("guard", "protect", "save", chan=False, pm=True, game=False, playing=True, roles=("bodyguard", "guardian angel"))
def guard(cli, nick, chan, rest):
    if var.PHASE != "night":
        pm(cli, nick, "You may only protect people at night.")
        return
    if nick in var.SILENCED:
        pm(cli, nick, "You have been silenced, and are unable to use any special powers.")
        return
    if var.GUARDED.get(nick):
        pm(cli, nick, "You are already protecting someone tonight.")
        return
    role = var.get_role(nick)
    victim = get_victim(cli, nick, re.split(" +",rest)[0], role == "bodyguard" or var.GUARDIAN_ANGEL_CAN_GUARD_SELF)
    if not victim:
        return

    if role == "guardian angel" and var.LASTGUARDED.get(nick) == victim:
        pm(cli, nick, ("You protected \u0002{0}\u0002 last night. " +
                       "You cannot protect the same person two nights in a row.").format(victim))
        return
    if victim == nick:
        if role == "bodyguard" or not var.GUARDIAN_ANGEL_CAN_GUARD_SELF:
            var.GUARDED[nick] = None
            if nick in var.LASTGUARDED:
                del var.LASTGUARDED[nick]
            pm(cli, nick, "You have chosen not to guard anyone tonight.")
        elif role == "guardian angel": # choosing to guard self bypasses lucky/misdirection
            var.GUARDED[nick] = nick
            var.LASTGUARDED[nick] = nick
            pm(cli, nick, "You have decided to guard yourself tonight.")
    else:
        victim = choose_target(nick, victim) 
        if check_exchange(cli, nick, victim):
            return
        var.GUARDED[nick] = victim
        var.LASTGUARDED[nick] = victim
        pm(cli, nick, "You are protecting \u0002{0}\u0002 tonight. Farewell!".format(var.GUARDED[nick]))
        pm(cli, var.GUARDED[nick], "You can sleep well tonight, for you are being protected.")
    debuglog("{0} ({1}) GUARD: {2} ({3})".format(nick, role, victim, var.get_role(victim)))
    chk_nightdone(cli)



@cmd("observe", chan=False, pm=True, game=True, playing=True, roles=("werecrow", "sorcerer"))
def observe(cli, nick, chan, rest):
    role = var.get_role(nick)
    if var.PHASE != "night":
        if role == "werecrow":
            pm(cli, nick, u"Només pots transformar-te en corb durant la nit.")
        else:
            pm(cli, nick, u"Només pots observar de nit.")
        return
    if nick in var.SILENCED:
        pm(cli, nick, u"Has estat silenciat i no pots utilitzar cap poder especial.")
        return
    victim = get_victim(cli, nick, re.split(" +",rest)[0])
    if not victim:
        return

    if victim == nick:
        if role == "werecrow":
            pm(cli, nick, u"En comptes de fer el boig podries provar de matar algú!")
        else:
            pm(cli, nick, u"Això seria una pèrdua de temps...")
        return
    if nick in var.OBSERVED.keys():
        if role == "werecrow":
            pm(cli, nick, u"Ja estàs volant cap a la casa de \02{0}\02.".format(var.OBSERVED[nick]))
        else:
            pm(cli, nick, u"Ja has observat aquesta nit.")
        return
    if var.get_role(victim) in var.WOLFCHAT_ROLES:
        if role == "werecrow":
            pm(cli, nick, u"Volar cap a la casa d'un altre llop és una pèrdua de temps.")
        else:
            pm(cli, nick, u"Observar un altre llop és una pèrdua de temps.")
        return
    victim = choose_target(nick, victim)
    if check_exchange(cli, nick, victim):
        return
    var.OBSERVED[nick] = victim
    if nick in var.KILLS.keys():
        del var.KILLS[nick]
    if role == "werecrow":
        pm(cli, nick, (u"Et transformes en un gran corb i començes a volar cap a "+
                       u"la casa de \u0002{0}\u0002. Tornaràs quan ja hagis "+
                      u"observat els seus moviments, quan es faci de dia.").format(victim))
    elif role == "sorcerer":
        vrole = var.get_role(victim)
        if vrole == "amnesiac":
            vrole = var.FINAL_ROLES[victim]
        if vrole in ("seer", "oracle", "augur", "sorcerer"):
            an = "n" if vrole[0] in ("a", "e", "i", "o", "u") else ""
            pm(cli, nick, (u"Després de fer el teu ritual descobreixes que \u0002{0}\u0002 " +
                           u"és un \u0002{2}\u0002!").format(victim, vrole))
        else:
            pm(cli, nick, (u"Després de fer el teu ritual, descobreixes que \u0002{0}\u0002 " +
                           u"no té cap sentit paranormal.").format(victim))
    debuglog("{0} ({1}) OBSERVA: {2} ({3})".format(nick, role, victim, var.get_role(victim)))
    chk_nightdone(cli)

@cmd("id", chan=False, pm=True, game=True, playing=True, roles=("detective",))
def investigate(cli, nick, chan, rest):
    if var.PHASE != "day":
        pm(cli, nick, u"Només pots investigar durant el dia.")
        return
    if nick in var.SILENCED:
        pm(cli, nick, u"Has estat silenciat i no pots utilitzar cap poder especial.")
        return
    if nick in var.INVESTIGATED:
        pm(cli, nick, u"Només pots investigar a una persona per ronda.")
        return
    victim = get_victim(cli, nick, re.split(" +",rest)[0])
    if not victim:
        return

    if victim == nick:
        pm(cli, nick, u"No perdis el temps... :)")
        return

    victim = choose_target(nick, victim)
    var.INVESTIGATED.append(nick)
    vrole = var.get_role(victim)
    if vrole == "amnesiac":
        vrole = var.FINAL_ROLES[victim]
    pm(cli, nick, (u"Els resultats de la teva investigació han retornat. \u0002{0}\u0002"+
                   u" és una... \u0002{1}\u0002!").format(victim, vrole))
    debuglog("{0} ({1}) ID: {2} ({3})".format(nick, var.get_role(nick), victim, vrole))
    if random.random() < var.DETECTIVE_REVEALED_CHANCE:  # a 2/5 chance (should be changeable in settings)
        # The detective's identity is compromised!
        for badguy in var.list_players(var.WOLFCHAT_ROLES):
            pm(cli, badguy, (u"Algú tira per error un paper. El paper et revela "+
                            u"que \u0002{0}\u0002 és un detectiu!").format(nick))
        debuglog("{0} ({1}) PAPER DROP".format(nick, var.get_role(nick)))

@cmd("visit", chan=False, pm=True, game=True, playing=True, roles=("harlot",))
def hvisit(cli, nick, chan, rest):
    if var.PHASE != "night":
        pm(cli, nick, u"Només pots visitar de nit.")
        return
    if nick in var.SILENCED:
        pm(cli, nick, u"Has estat silenciat i no pots utilitzar cap poder especial.")
        return
    if var.HVISITED.get(nick):
        pm(cli, nick, (u"Ja estàs passant la nit "+
                      u"amb \u0002{0}\u0002.").format(var.HVISITED[nick]))
        return
    victim = get_victim(cli, nick, re.split(" +",rest)[0], True)
    if not victim:
        return

    if nick == victim:  # Staying home
        var.HVISITED[nick] = None
        pm(cli, nick, u"Has triat quedar-te a casa aquesta nit.")
    else:
        victim = choose_target(nick, victim)
        if check_exchange(cli, nick, victim):
            return
        var.HVISITED[nick] = victim
        pm(cli, nick, (u"Estàs passant la nit amb \u0002{0}\u0002. "+
                      u"Bona nit!").format(victim))
        if nick != victim: #prevent luck/misdirection totem weirdness
            pm(cli, victim, (u"Estàs passant la nit amb \u0002{0}"+
                                     u"\u0002. Bona nit!").format(nick))
    debuglog("{0} ({1}) VISITAT: {2} ({3})".format(nick, var.get_role(nick), victim, var.get_role(victim)))
    chk_nightdone(cli)

def is_fake_nick(who):
    return re.match("[0-9]+", who)

@cmd("see", chan=False, pm=True, game=True, playing=True, roles=("seer", "oracle", "augur"))
def see(cli, nick, chan, rest):
    role = var.get_role(nick)
    if var.PHASE != "night":
        pm(cli, nick, u"Les vibracions mentals només t'arriben de nit!")
        return
    if nick in var.SILENCED:
        pm(cli, nick, u"Has estat silenciat i no pots utilitzar poders especials.")
        return
    if nick in var.SEEN:
        pm(cli, nick, u"Només pots tenir una visió.")
        return
    victim = get_victim(cli, nick, re.split(" +",rest)[0])
    if not victim:
        return

    if victim == nick:
        pm(cli, nick, u"Segur que et vols veure a tu mateix?")
        return
    victim = choose_target(nick, victim)
    if check_exchange(cli, nick, victim):
        return
    victimrole = var.get_role(victim)
    vrole = victimrole # keep a copy for logging
    if role == "seer":
        if victimrole in var.SEEN_WOLF or victim in var.ROLES["cursed villager"]:
            victimrole = "wolf"
        elif victimrole in var.SEEN_DEFAULT:
            victimrole = var.DEFAULT_ROLE
            if var.DEFAULT_SEEN_AS_VILL:
                victimrole = "villager"
        pm(cli, nick, (u"Tens una visió; en aquesta visió, "+
                        u"veus que \u0002{0}\u0002 és un "+
                        u"\u0002{1}\u0002!").format(victim, victimrole))
        debuglog("{0} ({1}) VIST: {2} ({3}) com a {4}".format(nick, role, victim, vrole, victimrole))
    elif role == "oracle":
        iswolf = False
        if victimrole in var.SEEN_WOLF or victim in var.ROLES["cursed villager"]:
            iswolf = True
        pm(cli, nick, (u"Els teus sentits paranormals han tingut una visió! "+
                        u"Els esperits et diuen que \u0002{0}\u0002 {1}és"+
                        u" un {2}wolf{2}!").format(victim, "" if iswolf else "\u0002no\u0002 ", BOLD if iswolf else ""))
        debuglog("{0} ({1}) VIST: {2} ({3}) (Llop: {4})".format(nick, role, victim, vrole, str(iswolf)))
    elif role == "augur":
        if victimrole == "amnesiac":
            victimrole = var.FINAL_ROLES[victim]
        aura = "blau"
        if victimrole in var.WOLFTEAM_ROLES:
            aura = "vermell"
        elif victimrole in var.TRUE_NEUTRAL_ROLES:
            aura = "gris"
        pm(cli, nick, (u"Tens una visió; en aquesta visió, " +
                       u"veus que \u0002{0}\u0002 té una " +
                       u"aurèola de color \u0002{1}\u0002!").format(victim, aura))
        debuglog("{0} ({1}) VIST: {2} ({3}) com a {4} ({5} aura)".format(nick, role, victim, vrole, victimrole, aura))
    var.SEEN.append(nick)
    chk_nightdone(cli)

@cmd("give", chan=False, pm=True, game=True, playing=True, roles=var.TOTEM_ORDER+("doctor",))
def give(cli, nick, chan, rest):
    role = var.get_role(nick)
    if role in var.TOTEM_ORDER:
        totem(cli, nick, chan, rest)
    elif role == "doctor":
        immunize(cli, nick, chan, rest)

@cmd("totem", chan=False, pm=True, game=True, playing=True, roles=var.TOTEM_ORDER)
def totem(cli, nick, chan, rest):
    if var.PHASE != "night":
        pm(cli, nick, u"Només pots donar tòtems durant la nit.")
        return
    if nick in var.SILENCED:
        pm(cli, nick, u"Has estat silenciat i no pots utilitzar cap poder especial.")
        return
    if nick in var.SHAMANS:
        pm(cli, nick, u"Ja has donat un tòtem aquesta nit.")
        return
    victim = get_victim(cli, nick, re.split(" +",rest)[0], True)
    if not victim:
        return
    if nick in var.LASTGIVEN and var.LASTGIVEN[nick] == victim:
        pm(cli, nick, u"Ja vas donar el teu tòtem a \u0002{0}\u0002 l'última nit,has de triar algú altre.".format(victim))
        return
    type = ""
    role = var.get_role(nick)
    if role != "crazed shaman":
        type = " de " + var.TOTEMS[nick]
    victim = choose_target(nick, victim)
    if check_exchange(cli, nick, victim):
        return
    pm(cli, nick, (u"Has donat un tòtem{0} a \u0002{1}\u0002.").format(type, victim))
    totem = var.TOTEMS[nick]
    if totem == "death":
        if victim not in var.DYING:
            var.DYING.append(victim)
    elif totem == "protection":
        if victim not in var.PROTECTED:
            var.PROTECTED.append(victim)
    elif totem == "revealing":
        if victim not in var.REVEALED:
            var.REVEALED.append(victim)
    elif totem == "narcolepsy":
        if victim not in var.ASLEEP:
            var.ASLEEP.append(victim)
    elif totem == "silence":
        if victim not in var.TOBESILENCED:
            var.TOBESILENCED.append(victim)
    elif totem == "desperation":
        if victim not in var.DESPERATE:
            var.DESPERATE.append(victim)
    elif totem == "impatience": # this totem stacks
        var.IMPATIENT.append(victim)
    elif totem == "pacifism": # this totem stacks
        var.PACIFISTS.append(victim)
    elif totem == "influence":
        if victim not in var.INFLUENTIAL:
            var.INFLUENTIAL.append(victim)
    elif totem == "exchange":
        if victim not in var.TOBEEXCHANGED:
            var.TOBEEXCHANGED.append(victim)
    elif totem == "lycanthropy":
        if victim not in var.TOBELYCANTHROPES:
            var.TOBELYCANTHROPES.append(victim)
    elif totem == "luck":
        if victim not in var.TOBELUCKY:
            var.TOBELUCKY.append(victim)
    elif totem == "pestilence":
        if victim not in var.TOBEDISEASED:
            var.TOBEDISEASED.append(victim)
    elif totem == "retribution":
        if victim not in var.RETRIBUTION:
            var.RETRIBUTION.append(victim)
    elif totem == "misdirection":
        if victim not in var.TOBEMISDIRECTED:
            var.TOBEMISDIRECTED.append(victim)
    else:
        pm(cli, nick, u"No sé què fer amb el tòtem '{0}'! Això és un error. Si us plau, reporta-ho a un administrador.".format(totem))
    var.LASTGIVEN[nick] = victim
    var.SHAMANS.append(nick)
    debuglog("{0} ({1}) TOTEM: {2} ({3})".format(nick, role, victim, totem))
    chk_nightdone(cli)

@cmd("immunize", "immunise", chan=False, pm=True, game=True, playing=True, roles=("doctor",))
def immunize(cli, nick, chan, rest):
    if var.PHASE != "day":
        pm(cli, nick, u"Només pots vacunar durant el dia.")
        return
    if nick in var.SILENCED:
        pm(cli, nick, u"Has estat silenciat i no pots utilitzar poder especials.")
        return
    if nick in var.DOCTORS and var.DOCTORS[nick] == 0:
        pm(cli, nick, u"T'has quedat sense vacunes")
        return
    victim = get_victim(cli, nick, re.split(" +",rest)[0], True)
    if not victim:
        return
    victim = choose_target(nick, victim)
    if check_exchange(cli, nick, victim):
        return
    pm(cli, nick, u"Has donat una vacuna a \u0002{0}\u0002.".format(victim))
    lycan = False
    if var.get_role(victim) == "lycan":
        lycan = True
        lycan_message = (u"Ets sents com si la maledició t'hagués desaparegut... Sembla que la teva licantropia està curada " +
                         u"i ja no et convertiràs mai més en llop si els llops no et tornen a mossegar!")
        var.ROLES["lycan"].remove(victim)
        var.ROLES["villager"].append(victim)
        var.FINAL_ROLES[victim] = "villager"
        var.CURED_LYCANS.append(victim)
        var.IMMUNIZED.add(victim)
    elif victim in var.BITTEN:
        # fun fact: immunizations in real life are done by injecting a small amount of (usually neutered) virus into the person
        # so that their immune system can fight it off and build up antibodies. This doesn't work very well if that person is
        # currently afflicted with the virus however, as you're just adding more virus to the mix...
        # naturally, we would want to mimic that behavior here, and what better way of indicating that things got worse than
        # by making the turning happen a night earlier? :)
        var.BITTEN[victim] -= 1
        lycan_message = (u"De sobte recordes el somni que has tingut l'última nit. " +
                         u"No li dones importància i l'oblides, però un petit record de la nit resta a la teva ment...")
    else:
        lycan_message = u"No et sents gaire diferent..."
        var.IMMUNIZED.add(victim)
    pm(cli, victim, (u"Sents una forta punxada al teu braç però de seguida et passa. " +
                     u"Quan mires què ha passat veus una xeringa buida al terra. {0}").format(lycan_message))
    var.DOCTORS[nick] -= 1
    debuglog("{0} ({1}) IMMUNITZAT: {2} ({3})".format(nick, var.get_role(nick), victim, "lycan" if lycan else var.get_role(victim)))

def get_bitten_message(nick):
    time_left = var.BITTEN[nick]
    message = ''
    if time_left <= 1:
        message = (u"Has tingut el mateix somni una altra vegada, però ara TU eres el l'agressor. Sents la por de la teva presa " +
                   u"mentre la persegueixes, corrent només a la meitat de la teva velocitat màxima per passar-t'ho més bé. " +
                   u"Se sobte, la teva presa cau a terra, i això et permet atrapar-la. " +
                   u"Assaboreixes la por que surt dels seus ulls just abans de clavar-li les teves dents per matar-la. " +
                   u"Et despertes.")
    elif time_left == 2:
        message = (u"Has somiat que corries per fora de la vila. " +
                   u"De cop senties una soroll i veus un èsser monstruós que " +
                   u"et salta a sobre - un home-llop! Començes a còrre tan ràpid com pots, però de sobte caus " +
                   u"sobre una roca. L'home-llop es torna a tira a sobre teu, " +
                   u"i et despertaves cridant.")
    else:
        message = (u"Has tingut un somni molt estrany aquesta nit; una persona estava fugint d'alguna cosa dins el bosc. " +
                   u"La persona ensopegava i queia a terra, i una ombra li queia al damunt. Abans de poder veure " +
                   u"qui o què era l'agressor, t'aixeques suat.")
    return message

@cmd("bite", chan=False, pm=True, game=True, playing=True, roles=("alpha wolf",))
def bite_cmd(cli, nick, chan, rest):
    if var.PHASE != "night":
        pm(cli, nick, u"Només pots mossegar durant la nit.")
        return
    if nick in var.ALPHA_WOLVES and nick not in var.BITE_PREFERENCES:
        pm(cli, nick, u"Ja has mossegat a algú en aquest joc.")
        return
    if nick in var.SILENCED:
        pm(cli, nick, u"Has estat silenciat i no pots utilitzar cap poder especial.")
        return
    if not var.ALPHA_ENABLED:
        pm(cli, nick, u"Només pots mossegar a algú si un llop mor durant el dia.")
        return

    victim = get_victim(cli, nick, re.split(" +",rest)[0], False)
    vrole = None
    # also mark the victim as the kill target
    if victim:
        kill(cli, nick, chan, rest)

    if var.ANGRY_WOLVES:
        if not victim:
            pm(cli, nick, "Tria a qui vols mossegar especificant el seu nick.")
            return

        vrole = var.get_role(victim)
        if vrole in var.WOLFCHAT_ROLES:
            pm(cli, nick, "No pots mossegar a altres llops.")
            return

    if nick not in var.ALPHA_WOLVES:
        var.ALPHA_WOLVES.append(nick)
    # this means that if victim is chosen by wolves, they will get bitten
    # if victim isn't chosen, then a target is chosen at random from the victims
    # (really only matters if wolves are angry; since there's only one target otherwise)
    var.BITE_PREFERENCES[nick] = victim

    if victim:
        pm(cli, nick, u"Has triat mossegar a \u0002{0}\u0002. Si aquest jugador no és seleccionat perquè els llops el matin, es triarà una altra víctima dels llops a l'atzar.".format(victim))
    else:
        pm(cli, nick, u"Has triat mossegar aquesta nit. La víctima que els llops vulguin matar serà mossegada i es convertirà en llop.")
    debuglog("{0} ({1}) MOSSEGA: {2} ({3})".format(nick, var.get_role(nick), victim if victim else "wolves' target", vrole if vrole else "unknown"))

@cmd("pass", chan=False, pm=True, game=True, playing=True, roles=("hunter",))
def pass_cmd(cli, nick, chan, rest):
    if var.PHASE != "night":
        pm(cli, nick, u"Només pots passar durant la nit.")
        return
    if nick in var.SILENCED:
        pm(cli, nick, u"Has estat silenciat i no pots utilitzar poders especials.")
        return

    if nick in var.OTHER_KILLS.keys():
        del var.OTHER_KILLS[nick]
        var.HUNTERS.remove(nick)

    pm(cli, nick, u"Has decidit no matar a ningú aquesta nit.")
    if nick not in var.PASSED: # Prevents multiple entries
        var.PASSED.append(nick)
    debuglog("{0} ({1}) PASS".format(nick, var.get_role(nick)))
    chk_nightdone(cli)

@cmd("choose", "match", chan=False, pm=True, game=True, playing=True, roles=("matchmaker",))
def choose(cli, nick, chan, rest):
    if var.PHASE != "night" or not var.FIRST_NIGHT:
        pm(cli, nick, "Només pots triar enamorats durant la primera nit.")
        return
    if nick in var.MATCHMAKERS:
        pm(cli, nick, "Ja has triat enamorats.")
        return
    # no var.SILENCED check for night 1 only roles; silence should only apply for the night after
    # but just in case, it also sucks if the one night you're allowed to act is when you are
    # silenced, so we ignore it here anyway.
    pieces = re.split(" +",rest)
    victim = pieces[0]
    if len(pieces) > 1:
        if len(pieces) > 2 and pieces[1].lower() == "and":
            victim2 = pieces[2]
        else:
            victim2 = pieces[1]
    else:
        victim2 = None

    victim = get_victim(cli, nick, victim, True)
    if not victim:
        return
    victim2 = get_victim(cli, nick, victim2, True)
    if not victim2:
        return

    if victim == victim2:
        pm(cli, nick, u"Has de triar dues persones diferents.")
        return

    var.MATCHMAKERS.append(nick)
    if victim in var.LOVERS:
        var.LOVERS[victim].append(victim2)
        var.ORIGINAL_LOVERS[victim].append(victim2)
    else:
        var.LOVERS[victim] = [victim2]
        var.ORIGINAL_LOVERS[victim] = [victim2]

    if victim2 in var.LOVERS:
        var.LOVERS[victim2].append(victim)
        var.ORIGINAL_LOVERS[victim2].append(victim)
    else:
        var.LOVERS[victim2] = [victim]
        var.ORIGINAL_LOVERS[victim2] = [victim]
    pm(cli, nick, u"Has seleccionat a \u0002{0}\u0002 i \u0002{1}\u0002 perquè s'enamorin".format(victim, victim2))

    if victim in var.PLAYERS and not is_user_simple(victim):
        pm(cli, victim, (u"Estàs \u0002enamorat\u0002 de {0}. Si aquest jugador mor per qualsevol raó, " +
                         u"el dolor que et causarà la seva pèrdua farà que et " +
                         u"suïcidis.").format(victim2))
    else:
        pm(cli, victim, u"Estàs \u0002enamorat\u0002 de {0}.".format(victim2))

    if victim2 in var.PLAYERS and not is_user_simple(victim2):
        pm(cli, victim2, (u"Estàs \u0002enamorat\u0002 de {0}. Si aquest jugador mor per qualsevol raó, " +
                         u"el dolor que et causarà la seva pèrdua farà que et " +
                         u"suïcidis.").format(victim))
    else:
        pm(cli, victim2, u"Estàs \u0002enamorat\u0002 de {0}.".format(victim))

    debuglog("{0} ({1}) ENAMORATS: {2} ({3}) + {4} ({5})".format(nick, var.get_role(nick), victim, var.get_role(victim), victim2, var.get_role(victim2)))
    chk_nightdone(cli)

@cmd("target", chan=False, pm=True, game=True, playing=True, roles=("assassin",))
def target(cli, nick, chan, rest):
    if var.PHASE != "night":
        pm(cli, nick, u"Només pots escollir gent a la nit.")
        return
    if nick in var.TARGETED and var.TARGETED[nick] != None:
        pm(cli, nick, u"Ja has triat una víctima.")
        return
    if nick in var.SILENCED:
        pm(cli, nick, u"Estas silenciat i no pots utilitzar cap poder especial.")
        return
    victim = get_victim(cli, nick, re.split(" +",rest)[0])
    if not victim:
        return

    if nick == victim:
        pm(cli, nick, "No pots esollir-te a tu mateix")
        return

    victim = choose_target(nick, victim)
    # assassin is a template so it will never get swapped, so don't check for exchanges with it
    var.TARGETED[nick] = victim
    pm(cli, nick, u"Has triat a \u0002{0}\u0002 per ser la teva víctima.".format(victim))

    debuglog(u"{0} ({1}-{2}) VÍCTIMA: {3} ({4})".format(nick, "-".join(var.get_templates(nick)), var.get_role(nick), victim, var.get_role(victim)))
    chk_nightdone(cli)

@cmd("hex", chan=False, pm=True, game=True, playing=True, roles=("hag",))
def hex(cli, nick, chan, rest):
    if var.PHASE != "night":
        pm(cli, nick, u"Només pots maleïr durant la nit.")
        return
    if nick in var.HEXED:
        pm(cli, nick, u"Ja has maleït algú aquesta nit.")
        return
    if nick in var.SILENCED:
        pm(cli, nick, u"Has estat silenciat i no pots utilitzar poder especials.")
        return
    victim = get_victim(cli, nick, re.split(" +",rest)[0])
    if not victim:
        return

    if nick == victim:
        pm(cli, nick, u"No pots automaleïr-te!")
        return
    if var.LASTHEXED.get(nick) == victim:
        pm(cli, nick, (u"Ja vas maleïr a \u0002{0}\u0002 durant la nit passada. " +
                       u"No pots maleïr a la mateix persona durant dues nits seguides.").format(victim))
        return

    victim = choose_target(nick, victim)
    if check_exchange(cli, nick, victim):
        return
    vrole = var.get_role(victim)
    if vrole in var.WOLFCHAT_ROLES:
        pm(cli, nick, u"Maleïr un altre llop seria un malgast.")
        return

    var.HEXED.append(nick)
    var.LASTHEXED[nick] = victim
    var.TOBESILENCED.append(victim)
    pm(cli, nick, "Maleeixes a \u0002{0}\u0002.".format(victim))

    debuglog("{0} ({1}) MALEFICI: {2} ({3})".format(nick, var.get_role(nick), victim, var.get_role(victim)))
    chk_nightdone(cli)

@cmd("clone", chan=False, pm=True, game=True, playing=True, roles=("clone",))
def clone(cli, nick, chan, rest):
    if var.PHASE != "night" or not var.FIRST_NIGHT:
        pm(cli, nick, u"Només pots clonar durant la primera nit.")
        return
    if nick in var.CLONED.keys():
        pm(cli, nick, u"Ja has clonat a algú.")
        return
    # no var.SILENCED check for night 1 only roles; silence should only apply for the night after
    # but just in case, it also sucks if the one night you're allowed to act is when you are
    # silenced, so we ignore it here anyway.

    victim = get_victim(cli, nick, re.split(" +",rest)[0])
    if not victim:
        return

    if nick == victim:
        pm(cli, nick, "No pots clonar-te a tu mateix.")
        return

    var.CLONED[nick] = victim
    pm(cli, nick, "Has clonat a \u0002{0}\u0002.".format(victim))

    debuglog("{0} ({1}) CLON: {2} ({3})".format(nick, var.get_role(nick), victim, var.get_role(victim)))
    chk_nightdone(cli)

@hook("featurelist")  # For multiple targets with PRIVMSG
def getfeatures(cli, nick, *rest):
    for r in rest:
        if r.startswith("TARGMAX="):
            x = r[r.index("PRIVMSG:"):]
            if "," in x:
                l = x[x.index(":")+1:x.index(",")]
            else:
                l = x[x.index(":")+1:]
            l = l.strip()
            if not l or not l.isdigit():
                continue
            else:
                var.MAX_PRIVMSG_TARGETS = int(l)
                continue
        if r.startswith("PREFIX="):
            prefs = r[7:]
            chp = []
            nlp = []
            finder = True
            for char in prefs:
                if char == "(":
                    continue
                if char == ")":
                    finder = False
                    continue
                if finder:
                    chp.append(char)
                else:
                    nlp.append(char)
            allp = zip(chp, nlp)
            var.MODES_PREFIXES = {}
            for combo in allp:
                var.MODES_PREFIXES[combo[1]] = combo[0] # For some reason this needs to be backwards
            if var.AUTO_TOGGLE_MODES:
                tocheck = set(var.AUTO_TOGGLE_MODES)
                var.AUTO_TOGGLE_MODES = set(var.AUTO_TOGGLE_MODES)
                for mode in tocheck:
                    if not mode in var.MODES_PREFIXES.keys() and not mode in var.MODES_PREFIXES.values():
                        var.AUTO_TOGGLE_MODES.remove(mode)
                        continue
                    if not mode in var.MODES_PREFIXES.values():
                        for chp in var.MODES_PREFIXES.keys():
                            if chp == mode:
                                var.AUTO_TOGGLE_MODES.remove(chp)
                                var.AUTO_TOGGLE_MODES.add(var.MODES_PREFIXES[mode])

                if "v" in var.AUTO_TOGGLE_MODES:
                    var.AUTO_TOGGLE_MODES.remove("v")
        if r.startswith("CHANMODES="):
            chans = r[10:].split(",")
            var.LISTMODES, var.MODES_ALLSET, var.MODES_ONLYSET, var.MODES_NOSET = chans
        if r.startswith("MODES="):
            try:
                var.MODELIMIT = int(r[6:])
            except ValueError:
                pass

def mass_privmsg(cli, targets, msg, notice=False, privmsg=False):
    if not notice and not privmsg:
        msg_targs = []
        not_targs = []
        for target in targets:
            if is_user_notice(target):
                not_targs.append(target)
            else:
                msg_targs.append(target)
        while msg_targs:
            if len(msg_targs) <= var.MAX_PRIVMSG_TARGETS:
                bgs = ",".join(msg_targs)
                msg_targs = None
            else:
                bgs = ",".join(msg_targs[:var.MAX_PRIVMSG_TARGETS])
                msg_targs = msg_targs[var.MAX_PRIVMSG_TARGETS:]
            cli.msg(bgs, msg)
        while not_targs:
            if len(not_targs) <= var.MAX_PRIVMSG_TARGETS:
                bgs = ",".join(not_targs)
                not_targs = None
            else:
                bgs = ",".join(not_targs[:var.MAX_PRIVMSG_TARGETS])
                not_targs = not_targs[var.MAX_PRIVMSG_TARGETS:]
            cli.notice(bgs, msg)
    else:
        while targets:
            if len(targets) <= var.MAX_PRIVMSG_TARGETS:
                bgs = ",".join(targets)
                targets = None
            else:
                bgs = ",".join(targets[:var.MAX_PRIVMSG_TARGETS])
                target = targets[var.MAX_PRIVMSG_TARGETS:]
            if notice:
                cli.notice(bgs, msg)
            else:
                cli.msg(bgs, msg)

@cmd("", chan=False, pm=True)
def relay(cli, nick, chan, rest):
    """Let the wolves talk to each other through the bot"""
    if rest.startswith("\x01PING"):
        cli.notice(nick, rest)
        return
    if var.PHASE not in ("night", "day"):
        return

    badguys = var.list_players(var.WOLFCHAT_ROLES)
    if len(badguys) > 1:
        if nick in badguys:
            badguys.remove(nick)  #  remove self from list

            if rest.startswith("\01ACTION"):
                rest = rest[7:-1]
                mass_privmsg(cli, [guy for guy in badguys
                    if guy in var.PLAYERS], "\02{0}\02{1}".format(nick, rest))
            else:
                mass_privmsg(cli, [guy for guy in badguys
                    if guy in var.PLAYERS], "\02{0}\02 diu: {1}".format(nick, rest))

def transition_night(cli):
    if var.PHASE == "night":
        return
    var.PHASE = "night"
    var.GAMEPHASE = "night"

    for x, tmr in var.TIMERS.items():  # cancel daytime timer
        tmr[0].cancel()
    var.TIMERS = {}

    # Reset nighttime variables
    var.KILLS = {}
    var.OTHER_KILLS = {}
    var.GUARDED = {}  # key = by whom, value = the person that is visited
    var.KILLER = ""  # nickname of who chose the victim
    var.SEEN = []  # list of seers that have had visions
    var.HEXED = [] # list of hags that have hexed
    var.SHAMANS = []
    var.PASSED = [] # list of hunters that have chosen not to kill
    var.OBSERVED = {}  # those whom werecrows have observed
    var.HVISITED = {}
    var.ASLEEP = []
    var.DYING = []
    var.PROTECTED = []
    var.DESPERATE = []
    var.REVEALED = []
    var.TOBESILENCED = []
    var.IMPATIENT = []
    var.PACIFISTS = []
    var.INFLUENTIAL = []
    var.TOBELYCANTHROPES = []
    var.TOBELUCKY = []
    var.TOBEDISEASED = []
    var.RETRIBUTION = []
    var.TOBEMISDIRECTED = []
    var.TOBEEXCHANGED = []
    var.NIGHT_START_TIME = datetime.now()
    var.NIGHT_COUNT += 1
    var.FIRST_NIGHT = (var.NIGHT_COUNT == 1)
    var.TOTEMS = {}
    var.ACTED_EXTRA = 0

    daydur_msg = ""

    if var.NIGHT_TIMEDELTA or var.START_WITH_DAY:  #  transition from day
        td = var.NIGHT_START_TIME - var.DAY_START_TIME
        var.DAY_START_TIME = None
        var.DAY_TIMEDELTA += td
        min, sec = td.seconds // 60, td.seconds % 60
        daydur_msg = "El dia ha acabat després de \u0002{0:0>2}:{1:0>2}\u0002. ".format(min,sec)

    chan = botconfig.CHANNEL

    if var.NIGHT_TIME_LIMIT > 0:
        var.NIGHT_ID = time.time()
        t = threading.Timer(var.NIGHT_TIME_LIMIT, transition_day, [cli, var.NIGHT_ID])
        var.TIMERS["night"] = (t, var.NIGHT_ID, var.NIGHT_TIME_LIMIT)
        t.daemon = True
        t.start()

    if var.NIGHT_TIME_WARN > 0:
        t2 = threading.Timer(var.NIGHT_TIME_WARN, night_warn, [cli, var.NIGHT_ID])
        var.TIMERS["night_warn"] = (t2, var.NIGHT_ID, var.NIGHT_TIME_WARN)
        t2.daemon = True
        t2.start()

    # convert bitten people to wolves, and advance bite stage
    bittencopy = copy.copy(var.BITTEN)
    for chump in bittencopy:
        var.BITTEN[chump] -= 1
        # short-circuit if they are already a wolf
        # this makes playing the day transition message easier since we can keep
        # var.BITTEN around for a day after they turn
        chumprole = var.get_role(chump)
        
        if chumprole in var.WOLF_ROLES:
            del var.BITTEN[chump]
            continue

        if var.BITTEN[chump] <= 0:
            # now a wolf
            pm(cli, chump, (u"Mentre et prepares per dormir, veus amb horror que t'està creixent pél per tot el cos! " +
                            u"De cop, t'adones del que t'està passant i una ganyota de fàstic t'apareix a la cara; veus que tens una mossegada " +
                            u"que ha fet que et convertissis en un home-llop! Et sents més gran, fort, ràpid, i a punt per " +
                            u"aprofitar la nit. Surts de casa en busca de la teva presa..."))
            var.BITTEN_ROLES[chump] = chumprole
            var.ROLES[chumprole].remove(chump)
            var.ROLES["wolf"].append(chump)
            var.FINAL_ROLES[chump] = "wolf"
            for wolf in var.list_players(var.WOLFCHAT_ROLES):
                if wolf != chump:
                    pm(cli, wolf, u"\u0002{0}\u0002 és ara un \u0002llop\u0002!".format(chump))
            debuglog("{0} ({1}) CONVERTIT EN LLOP".format(chump, chumprole))

    # convert amnesiac and kill village elder if necessary
    if var.NIGHT_COUNT == var.AMNESIAC_NIGHTS:
        amns = copy.copy(var.ROLES["amnesiac"])
        for amn in amns:
            amnrole = var.FINAL_ROLES[amn]
            var.ROLES["amnesiac"].remove(amn)
            var.ROLES[amnrole].append(amn)
            var.AMNESIACS.append(amn)
            showrole = amnrole
            if showrole in ("village elder", "time lord"):
                showrole = "villager"
            elif showrole == "vengeful ghost":
                showrole = var.DEFAULT_ROLE
            pm(cli, amn, u"La teva amnèsia t'ha passat i de sobte recordes que ets un \u0002{0}\u0002!".format(showrole))
            if amnrole in var.WOLFCHAT_ROLES:
                for wolf in var.list_players(var.WOLFCHAT_ROLES):
                    pm(cli, wolf, u"\u0002{0}\u0002 és ara un \u0002{1}\u0002!".format(amn, showrole))
            debuglog("{0} RECORDA: {1} com a {2}".format(amn, amnrole, showrole))

    numwolves = len(var.list_players(var.WOLF_ROLES))
    if var.NIGHT_COUNT >= numwolves + 1:
        for elder in var.ROLES["village elder"]:
            var.DYING.append(elder)
            debuglog(elder, "ELDER DEATH")

    # game ended from bitten / amnesiac turning, narcolepsy totem expiring, or other weirdness
    if chk_win(cli):
        return

    # send PMs
    ps = var.list_players()
    wolves = var.list_players(var.WOLFCHAT_ROLES)
    for wolf in wolves:
        normal_notify = wolf in var.PLAYERS and not is_user_simple(wolf)
        role = var.get_role(wolf)
        cursed = "cursed " if wolf in var.ROLES["cursed villager"] else ""

        if normal_notify:
            if role == "wolf":
                pm(cli, wolf, (u'Ets un \u0002llop\u0002. El teu objectiu és exterminar tots '+
                               u'els vilatans. Utilitza "kill <nick>" per matar un vilatà.'))
            elif role == "traitor":
                pm(cli, wolf, (u'Ets un \u0002{0}traïdor\u0002. Ets exactament igual que un '+
                               u'vilatà i el vident et veu com un d\'ells, '+
                               u'només els detectius poden veure la teva identitat.').format(cursed))
            elif role == "werecrow":
                pm(cli, wolf, (u'Ets un \u0002home-corb\u0002. Pots volar durant la nit. '+
                               u'Escriu "kill <nick>" per matar un jugador. Alternativament, pots '+
                               u'utilitzar "observe <nick>" per saber si algú està dormint o no. '+
                               u'Si observes no podras participar en un assassinat.'))
            elif role == "hag":
                pm(cli, wolf, (u'Ets una \u0002{0}bruixa\u0002. Pots maleïr a algú per evitar que ' +
                               u'utilitzi qualsevol poder especial durant el següent dia i nit. ' +
                               u'Escriu "hex <nick>" per maleïr-lo. Només es detectius poden saber ' +
                               u'la teva identitat real, els vidents et veuran com un vilatà normal.').format(cursed))
            elif role == "sorcerer":
                pm(cli, wolf, (u'Ets un \u0002{0}bruixot\u0002. Pots utilitzar "observe <nick>" per ' +
                               u'observar algú i veure si és seer, oracle, o augur. ' +
                               u'Només els detectius poden saber la teva identitat verdadera, els vidents et veuran ' +
                               u'com un vilatà normal.').format(cursed))
            elif role == "wolf cub":
                pm(cli, wolf, (u'Ets una \u0002cria de llop\u0002. Tot i que no pots matar a ningú, ' +
                               u'els altres llops s\'enfadaran molt si et mors i podran ' +
                               u'matar a dues persones la següent nit.'))
            elif role == "alpha wolf":
                pm(cli, wolf, (u'Ets un \u0002llop alfa\u0002. Una vegada per joc, quan un altre llop mor ' +
                               u'durant el dia, pots mossegar la següent víctima dels llops ' +
                               u'per convertir-la en un llop en comptes de matar-la. Pots matar els vilatans escrivint '
                               u'"kill <nick>" i "bite" per mossegar (una vegada per partida).'))
            else:
                # catchall in case we forgot something above
                an = 'n' if role[0] in ('a', 'e', 'i', 'o', 'u') else ''
                pm(cli, wolf, (u'Ets un \u0002{1}\u0002. En principi hauries de tenir instruccions aquí, ' +
                               u'però algú s\'haurà oblidat de posar-les. Si us plau, reporta-ho ' +
                               u'als administradors, pots enviar-me un missatge privat dient "admins" per una llista dels administrador disponibles.').format(role))

            if len(wolves) > 1:
                pm(cli, wolf, u'A més, si m\'envies un missatge privat la resta de llops també el rebràn.')
        else:
            an = 'n' if cursed == '' and role[0] in ('a', 'e', 'i', 'o', 'u') else ''
            pm(cli, wolf, "Ets un \02{0}{1}\02.".format(cursed, role))  # !simple

        pl = ps[:]
        random.shuffle(pl)
        pl.remove(wolf)  # remove self from list
        for i, player in enumerate(pl):
            prole = var.get_role(player)
            if prole in var.WOLFCHAT_ROLES:
                cursed = ""
                if player in var.ROLES["cursed villager"]:
                    cursed = "cursed "
                pl[i] = "\u0002{0}\u0002 ({1}{2})".format(player, cursed, prole)
            elif player in var.ROLES["cursed villager"]:
                pl[i] = player + u" (maleït)"

        pm(cli, wolf, "Jugadors: " + ", ".join(pl))
        if wolf in var.WOLF_GUNNERS.keys() and var.WOLF_GUNNERS[wolf] > 0:
            pm(cli, wolf, u"Tens una \u0002pistola\u0002 amb {0} bal{1}.".format(var.WOLF_GUNNERS[wolf], "es" if var.WOLF_GUNNERS[wolf] > 1 else "a"))
        angry_alpha = ''
        if var.DISEASED_WOLVES:
            pm(cli, wolf, u'Oh! Estàs malalt i aquesta nit no pots matar a ningú!')
        elif var.ANGRY_WOLVES and role in ("wolf", "werecrow", "alpha wolf"):
            pm(cli, wolf, u'Aquesta nit estàs \u0002enfadat\u0002, i pots matar dues persones escrivint "kill <nick1> and <nick2>".')
            angry_alpha = ' <nick>'
        if var.ALPHA_ENABLED and role == "alpha wolf" and wolf not in var.ALPHA_WOLVES:
            pm(cli, wolf, (u'Pots utilitzar "bite{0}" aquesta nit per convertir la víctima dels llops en un altre llop en comptes de matar-lo. ' +
                           u'Es convertirà en llop d\'aquí a {1} nit{2}.').format(angry_alpha, var.ALPHA_WOLF_NIGHTS, 's' if var.ALPHA_WOLF_NIGHTS > 1 else ''))

    for seer in var.list_players(["seer", "oracle", "augur"]):
        pl = ps[:]
        random.shuffle(pl)
        role = var.get_role(seer)
        pl.remove(seer)  # remove self from list

        a = "un"
        if role in ("oracle", "augur"):
            a = "un"

        if role == "seer":
            what = u"el rol d'un jugador"
        elif role == "oracle":
            what = u"si un jugador és llop o no"
        elif role == "augur":
            what = u"de quin equip és un jugador"
        else:
            what = u"??? (això és un error. Si us plau, reporta-ho a un administrador)"

        if seer in var.PLAYERS and not is_user_simple(seer):
            pm(cli, seer, (u'Ets {0} \u0002{1}\u0002. '+
                          u'La teva feina consisteix en detectar els llops '+
                          u'tenint una visió per nit. '+
                          u'Escriu "see <nick>" per veure {2}.').format(a, role, what))
        else:
            pm(cli, seer, "Ets un {0} \02{1}\02.".format(a, role))  # !simple
        pm(cli, seer, "Jugador: " + ", ".join(pl))

    for harlot in var.ROLES["harlot"]:
        pl = ps[:]
        random.shuffle(pl)
        pl.remove(harlot)
        if harlot in var.PLAYERS and not is_user_simple(harlot):
            pm(cli, harlot, (u'Ets una \u0002prostituta\u0002. '+
                             u'Pots passar la nit amb un altra jugador. '+
                             u'Si visites una víctima d\'un llop, o visites un llop, '+
                             u'moriràs. Escriu "visit <nick>" per visitar un jugador.'))
        else:
            pm(cli, harlot, "Ets una \02prostituta\02.")  # !simple
        pm(cli, harlot, "Jugadors: " + ", ".join(pl))

    # the messages for angel and guardian angel are different enough to merit individual loops
    for g_angel in var.ROLES["bodyguard"]:
        pl = ps[:]
        random.shuffle(pl)
        pl.remove(g_angel)
        chance = math.floor(var.BODYGUARD_DIES_CHANCE * 100)
        warning = ""
        if chance > 0:
            warning = "Si protegeixes un llop, hi ha un {0}% de possibilitats de que moris. ".format(chance)

        if g_angel in var.PLAYERS and not is_user_simple(g_angel):
            pm(cli, g_angel, (u'Ets un \u0002guardaespatlles\u0002. '+
                              u'La teva feina és protegir els habitants del poble. {0}Si protegeixes '+
                              u'una víctima dels llops, et sacrificaràs perquè no mori. ' +
                              u'Escriu "guard <nick>" per protegir un jugador.').format(warning))
        else:
            pm(cli, g_angel, "Ets un \02guardaespatlles\02.")  # !simple
        pm(cli, g_angel, "Jugadors: " + ", ".join(pl))

    for gangel in var.ROLES["guardian angel"]:
        pl = ps[:]
        random.shuffle(pl)
        pl.remove(gangel)
        chance = math.floor(var.GUARDIAN_ANGEL_DIES_CHANCE * 100)
        warning = ""
        if chance > 0:
            warning = u"Si protegeixes un llop, hi ha una probabilitat d'un {0}% que moris. ".format(chance)

        if gangel in var.PLAYERS and not is_user_simple(gangel):
            pm(cli, gangel, (u'Ets un \u0002àngel guardià\u0002. '+
                              u'La teva feina és protegir els habitants del poble. {0}Si protegeixes '+
                              u'una víctima dels llops, sobreviurà. No pots protegir la mateixa persona durant dues nits seguides. ' +
                              u'Escriu "guard <nick>" per protegir un jugador.').format(warning))
        else:
            pm(cli, gangel, "You are a \02guardian angel\02.")  # !simple
        pm(cli, gangel, "Players: " + ", ".join(pl))

    for dttv in var.ROLES["detective"]:
        pl = ps[:]
        random.shuffle(pl)
        pl.remove(dttv)
        chance = math.floor(var.DETECTIVE_REVEALED_CHANCE * 100)
        warning = ""
        if chance > 0:
            warning = ("Cada vegada que utilitzes la teva habilitat, tens un risc d'un {0}% de que la teva " +
                       "identitat sigui revelada als llops. ").format(chance)
        if dttv in var.PLAYERS and not is_user_simple(dttv):
            pm(cli, dttv, (u"Ets un \u0002detectiu\u0002.\n"+
                          u"La teva feina és descobrir tots els llops i traïdors. "+
                          u"Pots utilitzar el teu poder durant el dia i veure la "+
                          u"identitata de tots els jugadors, fins i tot els traïdors.\n"+
                          u'{0}Utilitza "id <nick>" en privat amb mi per saber la identitat d\'un jugador durant el dia.').format(warning))
        else:
            pm(cli, dttv, "Ets un \02detectiu\02.")  # !simple
        pm(cli, dttv, "Jugadors: " + ", ".join(pl))

    for drunk in var.ROLES["village drunk"]:
        if drunk in var.PLAYERS and not is_user_simple(drunk):
            pm(cli, drunk, "Has begut massa! Ets un \u0002borratxo\u0002.")
        else:
            pm(cli, drunk, "Ets un \u0002borratxo\u0002.")

    max_totems = {}
    for sham in var.TOTEM_ORDER:
        max_totems[sham] = 0
    for ix in range(0, len(var.TOTEM_ORDER)):
        for c in var.TOTEM_CHANCES.values():
            max_totems[var.TOTEM_ORDER[ix]] += c[ix]
    for shaman in var.list_players(var.TOTEM_ORDER):
        pl = ps[:]
        random.shuffle(pl)
        role = var.get_role(shaman)
        indx = var.TOTEM_ORDER.index(role)
        target = 0
        rand = random.random() * max_totems[var.TOTEM_ORDER[indx]]
        for t in var.TOTEM_CHANCES.keys():
            target += var.TOTEM_CHANCES[t][indx]
            if rand <= target:
                var.TOTEMS[shaman] = t
                break
        if shaman in var.PLAYERS and not is_user_simple(shaman):
            pm(cli, shaman, (u'Ets un \u0002{0}\u0002. Pots seleccionar un jugador perquè rebi un ' +
                             u'totem{1} cada nit utilitzant "give <nick>". Pots donar-te un tòtem a tu mateix però ' +
                             u'no pots donar al mateix jugador dos tòtems seguits.').format(role, " aleatòri" if shaman in var.ROLES["crazed shaman"] else ""))
            if role != "crazed shaman":
                totem = var.TOTEMS[shaman]
                tmsg = 'Tens el tòtem \u0002{0}\u0002. '.format(totem)
                if totem == "death":
                    tmsg += u'El jugador que rep el tòtem morirà aquesta nit, encara que estigui protegit.'
                elif totem == "protection":
                    tmsg += u'El jugador que rep el tòtem no podrà morir aquesta nit.'
                elif totem == "revealing":
                    tmsg += u'Si el jugador que rep el tòtem és linxat, el seu rol es revela però no mor.'
                elif totem == "narcolepsy":
                    tmsg += u'El jugador que rep el tòtem no podrà votar demà.'
                elif totem == "silence":
                    tmsg += u'El jugador que rep el tòtem no podrà utilitzar cap poder especial durant el proper dia i nit.'
                elif totem == "desperation":
                    tmsg += u'Si el jugador que rep el tòtem mor linxat, l\'últim jugador en votar-lo també morirà.'
                elif totem == "impatience":
                    tmsg += u'EL jugador que rep el tòtem votarà per tòtem excepte per ell mateix, fins i tot si no utilitza l\'ordre per votar.'
                elif totem == "pacifism":
                    tmsg += u'Els vots del jugador que rep el tòtem no valdràn.'
                elif totem == "influence":
                    tmsg += u'Els vots del jugador que rep el tòtem valdràn el doble.'
                elif totem == "exchange":
                    tmsg += u'La primera persona que utilitzi un poder sobre el jugador que rep el tòtem la propera nit es canviarà de rol amb el que rep el tòtem.'
                elif totem == "lycanthropy":
                    tmsg += u'Si el jugador que rep el tòtem és escollit pels llops, es convertirà en un d\'ells.'
                elif totem == "luck":
                    tmsg += 'Si el jugador que rep el tòtem és escollit per algun rol especial, un dels jugadors que estiguin al seu costat rebran els efectes de l\'acció en el seu lloc.'
                elif totem == "pestilence":
                    tmsg += u'Si el jugador que rep el tòtem mor la propera nit, els llops no podràn matar a ningú la nit següent.'
                elif totem == "retribution":
                    tmsg += u'Si el jugador que rep el tòtem mor aquesta nit, el seu fantasma vindrà i matarà el seu assassí.'
                elif totem == "misdirection":
                    tmsg += u'Si el jugador que rep el tòtem intenta utilitzar algun poder el proper dia o nit, s\'equivocarà i escollirà el jugador que estigui al costat de la víctima que ha escollit.'
                else:
                    tmsg += u'La descripció d\'aquest tòtem no està disponible. Això és un error; si us plau, reporta-ho a un administrador.'
                pm(cli, shaman, tmsg)
        else:
            pm(cli, shaman, "Ets un \u0002{0}\u0002.".format(role))
            if role != "crazed shaman":
                pm(cli, shaman, u"Tens el tòtem de \u0002{0}\u0002.".format(var.TOTEMS[shaman]))
        pm(cli, shaman, "Jugadors: " + ", ".join(pl))

    for hunter in var.ROLES["hunter"]:
        if hunter in var.HUNTERS:
            continue #already killed
        pl = ps[:]
        random.shuffle(pl)
        pl.remove(hunter)
        if hunter in var.PLAYERS and not is_user_simple(hunter):
            pm(cli, hunter, (u'Ets un \u0002caçador\u0002. Una vegada per joc pots matar un ' +
                             u'jugador escrivint "kill <nick>". Si no vols matar a ningú aquesta nit, ' +
                             u'escriu "pass".'))
        else:
            pm(cli, hunter, u"Ets un \u0002caçador\u0002.")
        pm(cli, hunter, "Jugadors: " + ", ".join(pl))


    for ms in var.ROLES["mad scientist"]:
        index = var.ALL_PLAYERS.index(ms)
        targets = []
        target1 = var.ALL_PLAYERS[index - 1]
        target2 = var.ALL_PLAYERS[index + 1 if index < len(var.ALL_PLAYERS) - 1 else 0]
        if len(var.ALL_PLAYERS) >= var.MAD_SCIENTIST_SKIPS_DEAD_PLAYERS:
            # determine left player
            i = index
            while True:
                i -= 1
                if i < 0:
                    i = len(var.ALL_PLAYERS) - 1
                if var.ALL_PLAYERS[i] in pl or var.ALL_PLAYERS[i] == ms:
                    target1 = var.ALL_PLAYERS[i]
                    break
            # determine right player
            i = index
            while True:
                i += 1
                if i >= len(var.ALL_PLAYERS):
                    i = 0
                if var.ALL_PLAYERS[i] in pl or var.ALL_PLAYERS[i] == ms:
                    target2 = var.ALL_PLAYERS[i]
                    break
        targets = "\u0002{0}\u0002 i \u0002{1}\u0002".format(target1, target2)
        if ms in var.PLAYERS and not is_user_simple(ms):
            pm(cli, ms, (u"Ets el \u0002científic boig\u0002. Si mors, " +
                         u"deixaràs anar una poció molt potent que " +
                         u"matarà a {0} si encara són vius.".format(targets)))
        else:
            pm(cli, ms, u"Ets el \u0002científic boig\u0002. Víctimes: {0}".format(targets))

    for doctor in var.ROLES["doctor"]:
        if var.DOCTORS[doctor] > 0: # has immunizations remaining
            pl = ps[:]
            random.shuffle(pl)
            if doctor in var.PLAYERS and not is_user_simple(doctor):
                pm(cli, doctor, (u'Ets un \u0002metge\u0002. Pots dnar medicaments als ' +
                                 u'habitants escrivint-me "give <nick>" en privat durant el dia. ' +
                                 u'Un habitant immunitzat morirà en comptes de convertir-se en llop ' +
                                 u'en conseqüència dels poders del llop alfa o dels homes-llop.'))
            else:
                pm(cli, doctor, "Ets un \u0002metge\u0002.")
            pm(cli, doctor, 'Tens \u0002{0}\u0002 medicament{1}.'.format(var.DOCTORS[doctor], 's' if var.DOCTORS[doctor] > 1 else ''))

    for fool in var.ROLES["fool"]:
        if fool in var.PLAYERS and not is_user_simple(fool):
            pm(cli, fool, (u'Ets un \u0002boig\u0002. El joc s\'acaba immediatamanti tu ' +
                           u'ets l\'únic guanyador si ets linxat durant el dia. No pots ' +
                           u'guanyar d\'una altra manera.'))
        else:
            pm(cli, fool, "Ets un \u0002boig\u0002.")

    for jester in var.ROLES["jester"]:
        if jester in var.PLAYERS and not is_user_simple(jester):
            pm(cli, jester, (u'Ets un \u0002bufó\u0002. Guanyaràs amb els guanyadors normals ' +
                             u'si ets linxat durant el dia. No pots guanyar el joc d\'una altra manera.'))
        else:
            pm(cli, jester, u"Ets un \u0002bufó\u0002.")

    for monster in var.ROLES["monster"]:
        if monster in var.PLAYERS and not is_user_simple(monster):
            pm(cli, monster, (u'Ets un \u0002monstre\u0002. Els llops no poden et matar. ' +
                              u'Si sobrevius fins a l\'última nit guanyaràs en lloc dels ' +
                              u'guanyadors normals.'))
        else:
            pm(cli, monster, "Ets un \u0002monstre\u0002.")

    for lycan in var.ROLES["lycan"]:
        if lycan in var.PLAYERS and not is_user_simple(lycan):
            pm(cli, lycan, (u'Ets un \u0002home llop\u0002. Ara mateix estàs a l\'equip dels ' +
                            u'habitants, però si durant la nit un llop t\'intenta matar ' +
                            u'et convertiràs en un d\'ells.'))
        else:
            pm(cli, lycan, u"Ets un \u0002home-llop\u0002.")

    for v_ghost, who in var.VENGEFUL_GHOSTS.items():
        if who[0] == "!":
            continue
        wolves = var.list_players(var.WOLFTEAM_ROLES)
        if who == "wolves":
            pl = wolves
        else:
            pl = ps[:]
            for wolf in wolves:
                pl.remove(wolf)

        random.shuffle(pl)

        if v_ghost in var.PLAYERS and not is_user_simple(v_ghost):
            pm(cli, v_ghost, (u'Ets un \u0002fantasma venjador\u0002, hi has jurat venjar-te de ' +
                              u'{0}, que segons tu t\'ha matat. N\'has de matar un escrivint ' +
                              u'"kill <nick>" aquesta nit. Si no ho fas, la selecció serà ' +
                              u'aleatòria.').format(who))
        else:
            pm(cli, v_ghost, "Ets un \u0002fantasma venjador\u0002.")
        pm(cli, v_ghost, who.capitalize() + ": " + ", ".join(pl))
        debuglog(u"GHOST: {0} (víctima: {1}) - jugadors: {2}".format(v_ghost, who, ", ".join(pl)))

    for ass in var.ROLES["assassin"]:
        if ass in var.TARGETED and var.TARGETED[ass] != None:
            continue # someone already targeted
        pl = ps[:]
        random.shuffle(pl)
        pl.remove(ass)
        role = var.get_role(ass)
        if role == "village drunk":
            var.TARGETED[ass] = random.choice(pl)
            message = (u"Ets un \u0002assassí\u0002. En la teva borratxera has seleccionat a " +
                       u"\u0002{0}\u0002 per ser la teva víctima.").format(var.TARGETED[ass])
            if ass in var.PLAYERS and not is_user_simple(ass):
                message += u" Si mors, la teva víctima morirà amb tu."
            pm(cli, ass, message)
        else:
            if ass in var.PLAYERS and not is_user_simple(ass):
                pm(cli, ass, (u'Ets un \u0002assassí\u0002. Tria una víctima escrivint ' +
                              u'"target <nick>". Si mors, la teva víctima morirà amb tu. ' +
                              u'Si la teva víctima mor en podràs escollir una altra.'))
            else:
                pm(cli, ass, u"Ets un \u0002assassí\u0002.")
            pm(cli, ass, "Jugadors: " + ", ".join(pl))

    if var.FIRST_NIGHT:
        for mm in var.ROLES["matchmaker"]:
            pl = ps[:]
            random.shuffle(pl)
            if mm in var.PLAYERS and not is_user_simple(mm):
                pm(cli, mm, (u'Ets un \u0002matrimonier\u0002. Pots seleccionar dos jugadors ' +
                             u'perquè s\'enamorin escrivint "choose <nick1> and <nick2>". Si un enamorat ' +
                             u'mor, l\'altre també ho farà. També pots seleccioar-te a tu mateix ' +
                             u'per estar enamorat. Només pots enamorar a dues persones durant la primera nit.'))
            else:
                pm(cli, mm, "Ets un \u0002matrimonier\u0002.")
            pm(cli, mm, "Jugadors: " + ", ".join(pl))

        for clone in var.ROLES["clone"]:
            pl = ps[:]
            random.shuffle(pl)
            pl.remove(clone)
            if clone in var.PLAYERS and not is_user_simple(clone):
                pm(cli, clone, (u'Ets un \u0002clon\u0002. Pots seleccionar algú per clonar ' +
                                u'escrivint "clone <nick>". Si aquest jugador mor tu adquireixes el seu rol. ' +
                                u'Només pots clonar durant la primera nit.'))
            else:
                pm(cli, clone, "Ets un \u0002clon\u0002.")
            pm(cli, clone, "Jugadors: "+", ".join(pl))

        for minion in var.ROLES["minion"]:
            wolves = var.list_players(var.WOLF_ROLES)
            random.shuffle(wolves)
            if minion in var.PLAYERS and not is_user_simple(minion):
                pm(cli, minion, "Ets un \u0002esbirro\u0002. La teva feina és ajudar als llops a matar tots els habitants del poble.")
            else:
                pm(cli, minion, "Ets un \u0002esbirro\u0002.")
            pm(cli, minion, "Llops: " + ", ".join(wolves))

        villagers = copy.copy(var.ROLES["villager"])
        villagers += var.ROLES["time lord"] + var.ROLES["village elder"]
        if var.DEFAULT_ROLE == "villager":
            villagers += var.ROLES["vengeful ghost"] + var.ROLES["amnesiac"]
        for villager in villagers:
            if villager in var.PLAYERS and not is_user_simple(villager):
                pm(cli, villager, u"Ets un \u0002vilatà\u0002. La teva feina és linxar tots els llops durant el dia.")
            else:
                pm(cli, villager, u"Ets un\u0002vilatà\u0002.")

        cultists = copy.copy(var.ROLES["cultist"])
        if var.DEFAULT_ROLE == "cultist":
            cultists += var.ROLES["vengeful ghost"] + var.ROLES["amnesiac"]
        for cultist in cultists:
            if cultist in var.PLAYERS and not is_user_simple(cultist):
                pm(cli, cultist, u"Ets un \u0002cultista\u0002. La teva feina és ajudar als llops a matar tots els habitants del poble.")
            else:
                pm(cli, cultist, u"Ets un \u0002cultista\u0002.")

    for g in var.GUNNERS.keys():
        if g not in ps:
            continue
        elif not var.GUNNERS[g]:
            continue
        elif var.GUNNERS[g] == 0:
            continue
        norm_notify = g in var.PLAYERS and not is_user_simple(g)
        role = "gunner"
        if g in var.ROLES["sharpshooter"]:
            role = "sharpshooter"
        if norm_notify:
            if role == "gunner":
                gun_msg = (u'Ets un \02{0}\02 i tens un pistola que dispara bales de plata. ' +
                           u'Només pots utilitzar-la durant el dia escrivint "{0}shoot <nick>" en el canal. '.format(botconfig.CMD_CHAR) +
                           u'Els llops i el seu equip moriran instantàniament quan els disparis, però els altres ' +
                           u'sobreviuran. Tens {1}.')
            elif role == "sharpshooter":
                gun_msg = (u'Ets un \02{0}\02 i tens una pistola que dispara bales de plata. ' +
                           u'Només pots utilitzar-la durant el dia escrivint "{0}shoot <nick>" en el canal. '.format(botconfig.CMD_CHAR) +
                           u'Els llops i el seu equip moriran instantàniament quan els disparis, i els altres ' +
                           u'moriran depenent de la teva habilitat amb la pistola. Tens {1}.')
        else:
            gun_msg = (u"Ets un \02{0}\02 i tens una pistola amb {1}.")
        if var.GUNNERS[g] == 1:
            gun_msg = gun_msg.format(role, "1 bala")
        elif var.GUNNERS[g] > 1:
            gun_msg = gun_msg.format(role, str(var.GUNNERS[g]) + " bales")
        else:
            continue

        pm(cli, g, gun_msg)

    dmsg = (daydur_msg + u"És de nit. Tots els jugadors han de comprovar "+
                   "els meus missatge privats per instruccions.")

    if not var.FIRST_NIGHT:
        dmsg = (dmsg + u" Si no n'has rebut cap, simplement seu tranquilament, "+
                   u"relaxa't, i espera pacientment pel matí.")
    cli.msg(chan, dmsg)
    debuglog("BEGIN NIGHT")



def cgamemode(cli, arg):
    chan = botconfig.CHANNEL
    if var.ORIGINAL_SETTINGS:  # needs reset
        reset_settings()

    modeargs = arg.split("=", 1)

    modeargs = [a.strip() for a in modeargs]
    if modeargs[0] in var.GAME_MODES.keys():
        md = modeargs.pop(0)
        try:
            gm = var.GAME_MODES[md][0](*modeargs)
            for attr in dir(gm):
                val = getattr(gm, attr)
                if (hasattr(var, attr) and not callable(val)
                                        and not attr.startswith("_")):
                    var.ORIGINAL_SETTINGS[attr] = getattr(var, attr)
                    setattr(var, attr, val)
            var.CURRENT_GAMEMODE = md
            return True
        except var.InvalidModeException as e:
            cli.msg(botconfig.CHANNEL, u"Mode invàlid: "+str(e))
            return False
    else:
        cli.msg(chan, "Mode \u0002{0}\u0002 no trobat.".format(modeargs[0]))


@cmd("start", join=True)
def fstart(cli, nick, chan, rest):
    """Starts a game of Werewolf."""
    start(cli, nick, chan)

def start(cli, nick, chan, forced = False):
    if (not forced and var.LAST_START and nick in var.LAST_START and
            var.LAST_START[nick] + timedelta(seconds=var.START_RATE_LIMIT) >
            datetime.now()):
        cli.notice(nick, (u"Aquesta ordre té un límit de temps. Si us plau espera una estona "
                          u"abans de tornar-lo a utilitzar."))
        return

    var.LAST_START[nick] = datetime.now()

    if chan != botconfig.CHANNEL:
        return

    villagers = var.list_players()
    pl = villagers[:]

    if var.PHASE == "none":
        cli.notice(nick, u"No hi ha cap joc en procès.")
        return
    if var.PHASE != "join":
        cli.notice(nick, u"El joc ja ha començat.")
        return
    if nick not in villagers and nick != chan and not forced:
        cli.notice(nick, u"No estàs jugant.")
        return

    now = datetime.now()
    var.GAME_START_TIME = now  # Only used for the idler checker
    dur = int((var.CAN_START_TIME - now).total_seconds())
    if dur > 0 and not forced:
        plural = "" if dur == 1 else "s"
        cli.msg(chan, u"Si us plau, espera com a mínim {0} segon{1} més.".format(dur, plural))
        return

    if len(villagers) < var.MIN_PLAYERS:
        cli.msg(chan, u"{0}: Es necessiten \u0002{1}\u0002 o més jugadors per jugar.".format(nick, var.MIN_PLAYERS))
        return

    if len(villagers) > var.MAX_PLAYERS:
        cli.msg(chan, u"{0}: Com a màxim poden jugar \u0002{1}\u0002 jugadors.".format(nick, var.MAX_PLAYERS))
        return

    if not var.FGAMED:
        votes = {} #key = gamemode, not cloak
        for gamemode in var.GAMEMODE_VOTES.values():
            if len(villagers) >= var.GAME_MODES[gamemode][1] and len(villagers) <= var.GAME_MODES[gamemode][2]:
                votes[gamemode] = votes.get(gamemode, 0) + 1
        voted = [gamemode for gamemode in votes if votes[gamemode] == max(votes.values()) and votes[gamemode] >= len(villagers)/2]
        if len(voted):
            cgamemode(cli, random.choice(voted))
        else:
            possiblegamemodes = []
            for gamemode in var.GAME_MODES.keys():
                if len(villagers) >= var.GAME_MODES[gamemode][1] and len(villagers) <= var.GAME_MODES[gamemode][2] and var.GAME_MODES[gamemode][3] > 0:
                    possiblegamemodes += [gamemode]*(var.GAME_MODES[gamemode][3]+votes.get(gamemode, 0)*15)
            cgamemode(cli, random.choice(possiblegamemodes))

    for index in range(len(var.ROLE_INDEX) - 1, -1, -1):
        if var.ROLE_INDEX[index] <= len(villagers):
            addroles = {k:v[index] for k,v in var.ROLE_GUIDE.items()}
            break
    else:
        cli.msg(chan, u"{0}: No hi ha una configuració de joc especificada per \u0002{1}\u0002 jugadors.".format(nick, len(villagers)))
        return

    # Cancel join timer
    if 'join' in var.TIMERS:
        var.TIMERS['join'][0].cancel()
        del var.TIMERS['join']

    if var.ORIGINAL_SETTINGS:  # Custom settings
        while True:
            wvs = sum(addroles[r] for r in var.WOLFCHAT_ROLES)
            if len(villagers) < (sum(addroles.values()) - sum([addroles[r] for r in var.TEMPLATE_RESTRICTIONS.keys()])):
                cli.msg(chan, "No hi ha prous jugadors en el joc "+
                              "per utilitzar rols personalitzats.")
            elif not wvs:
                cli.msg(chan, u"Hi ha d'haver com a mínim un llop!")
            elif wvs > (len(villagers) / 2):
                cli.msg(chan, "Masses llops.")
            else:
                break
            reset_settings()
            cli.msg(chan, u"Configuració per defecte restaurada. Si us plau, !start de nou.")
            var.PHASE = "join"
            return


    if var.ADMIN_TO_PING:
        if "join" in COMMANDS.keys():
            COMMANDS["join"] = [lambda *spam: cli.msg(chan, u"Ordre desactivada per un administrador.")]
        if "j" in COMMANDS.keys():
            COMMANDS["j"] = [lambda *spam: cli.msg(chan, u"Ordre desactivada per un administrador.")]
        if "start" in COMMANDS.keys():
            COMMANDS["start"] = [lambda *spam: cli.msg(chan, u"Ordre desactivada per un administrador.")]

    var.ALL_PLAYERS = copy.copy(var.ROLES["person"])
    var.ROLES = {}
    var.GUNNERS = {}
    var.WOLF_GUNNERS = {}
    var.SEEN = []
    var.OBSERVED = {}
    var.KILLS = {}
    var.GUARDED = {}
    var.HVISITED = {}
    var.HUNTERS = []
    var.LYCANS = []
    var.VENGEFUL_GHOSTS = {}
    var.CLONED = {}
    var.TARGETED = {}
    var.LASTGUARDED = {}
    var.LASTHEXED = {}
    var.LASTGIVEN = {}
    var.LOVERS = {}
    var.MATCHMAKERS = []
    var.REVEALED_MAYORS = []
    var.SILENCED = []
    var.TOBESILENCED = []
    var.DESPERATE = []
    var.REVEALED = []
    var.ASLEEP = []
    var.PROTECTED = []
    var.DYING = []
    var.JESTERS = []
    var.AMNESIACS = []
    var.NIGHT_COUNT = 0
    var.DAY_COUNT = 0
    var.ANGRY_WOLVES = False
    var.DISEASED_WOLVES = False
    var.FINAL_ROLES = {}
    var.ORIGINAL_LOVERS = {}
    var.IMPATIENT = []
    var.PACIFISTS = []
    var.INFLUENTIAL = []
    var.LYCANTHROPES = []
    var.TOBELYCANTHROPES = []
    var.LUCKY = []
    var.TOBELUCKY = []
    var.DISEASED = []
    var.TOBEDISEASED = []
    var.RETRIBUTION = []
    var.MISDIRECTED = []
    var.TOBEMISDIRECTED = []
    var.EXCHANGED = []
    var.TOBEEXCHANGED = []
    var.SHAMANS = []
    var.HEXED = []
    var.OTHER_KILLS = {}
    var.ACTED_EXTRA = 0
    var.ABSTAINED = False
    var.DOCTORS = {}
    var.IMMUNIZED = set()
    var.CURED_LYCANS = []
    var.ALPHA_WOLVES = []
    var.ALPHA_ENABLED = False
    var.BITTEN = {}
    var.BITE_PREFERENCES = {}
    var.BITTEN_ROLES = {}

    for role, count in addroles.items():
        if role in var.TEMPLATE_RESTRICTIONS.keys():
            var.ROLES[role] = [None] * count
            continue # We deal with those later, see below
        selected = random.sample(villagers, count)
        var.ROLES[role] = selected
        for x in selected:
            villagers.remove(x)

    for v in villagers:
        var.ROLES[var.DEFAULT_ROLE].append(v)

    # Now for the templates
    for template, restrictions in var.TEMPLATE_RESTRICTIONS.items():
        if template == "sharpshooter":
            continue # sharpshooter gets applied specially
        possible = pl[:]
        for cannotbe in var.list_players(restrictions):
            if cannotbe in possible:
                possible.remove(cannotbe)
        if len(possible) < len(var.ROLES[template]):
            cli.msg(chan, "Not enough valid targets for the {0} template.".format(template))
            if var.ORIGINAL_SETTINGS:
                var.ROLES = {"person": var.ALL_PLAYERS}
                reset_settings()
                cli.msg(chan, u"La configuració per defecta ha estat restaurada. Si us plau, !start de nou.")
                var.PHASE = "join"
                return
            else:
                cli.msg(chan, u"Aquest rol ha estat saltat per aquest joc.")
                var.ROLES[template] = []
                continue

        var.ROLES[template] = random.sample(possible, len(var.ROLES[template]))

    # Handle gunner
    cannot_be_sharpshooter = var.list_players(var.TEMPLATE_RESTRICTIONS["sharpshooter"])
    gunner_list = copy.copy(var.ROLES["gunner"])
    num_sharpshooters = 0
    for gunner in gunner_list:
        if gunner in var.ROLES["village drunk"]:
            var.GUNNERS[gunner] = (var.DRUNK_SHOTS_MULTIPLIER * math.ceil(var.SHOTS_MULTIPLIER * len(pl)))
        elif num_sharpshooters < addroles["sharpshooter"] and gunner not in cannot_be_sharpshooter and random.random() <= var.SHARPSHOOTER_CHANCE:
            var.GUNNERS[gunner] = math.ceil(var.SHARPSHOOTER_MULTIPLIER * len(pl))
            var.ROLES["gunner"].remove(gunner)
            var.ROLES["sharpshooter"].append(gunner)
            num_sharpshooters += 1
        else:
            var.GUNNERS[gunner] = math.ceil(var.SHOTS_MULTIPLIER * len(pl))

    while True:
        try:
            var.ROLES["sharpshooter"].remove(None)
        except ValueError:
            break

    var.SPECIAL_ROLES["goat herder"] = []
    if var.GOAT_HERDER:
       var.SPECIAL_ROLES["goat herder"] = [ nick ]

    cli.msg(chan, (u"{0}: Benvinguts a l'assassí, el popular joc social, també conegut com a 'Mafia'. "+
                   u"Iniciant la partida amb el mode de joc \002{1}\002.").format(", ".join(pl), var.CURRENT_GAMEMODE))
    cli.mode(chan, "+m")

    var.ORIGINAL_ROLES = copy.deepcopy(var.ROLES)  # Make a copy

    # Handle amnesiac
    amnroles = list(var.ROLE_GUIDE.keys() - [var.DEFAULT_ROLE])
    for nope in var.AMNESIAC_BLACKLIST:
        if nope in amnroles:
            amnroles.remove(nope)
    for nope in var.TEMPLATE_RESTRICTIONS.keys():
        if nope in amnroles:
            amnroles.remove(nope)
    for amnesiac in var.ROLES["amnesiac"]:
        var.FINAL_ROLES[amnesiac] = random.choice(amnroles)

    # Handle doctor
    for doctor in var.ROLES["doctor"]:
        var.DOCTORS[doctor] = math.ceil(var.DOCTOR_IMMUNIZATION_MULTIPLIER * len(pl))

    var.DAY_TIMEDELTA = timedelta(0)
    var.NIGHT_TIMEDELTA = timedelta(0)
    var.DAY_START_TIME = datetime.now()
    var.NIGHT_START_TIME = datetime.now()

    var.LAST_PING = None

    roles = copy.copy(var.ROLES)
    for rol in roles:
        r = []
        for rw in var.plural(rol).split(" "):
            rwu = rw[0].upper()
            if len(rw) > 1:
                rwu += rw[1:]
            r.append(rwu)
        r = " ".join(r)

    var.PLAYERS = {plr:dict(var.USERS[plr]) for plr in pl if plr in var.USERS}

    debuglog("ROLES:", " | ".join("{0}: {1}".format(role, ", ".join(players))
        for role, players in sorted(var.ROLES.items()) if players and role not in var.TEMPLATE_RESTRICTIONS.keys()))
    templates = " | ".join("{0}: {1}".format(tmplt, ", ".join(players))
        for tmplt, players in sorted(var.ROLES.items()) if players and tmplt in var.TEMPLATE_RESTRICTIONS.keys())
    if not templates:
        templates = "None"
    debuglog("TEMPLATES:", templates)

    var.FIRST_NIGHT = True
    if not var.START_WITH_DAY:
        var.GAMEPHASE = "night"
        transition_night(cli)
    else:
        var.FIRST_DAY = True
        var.GAMEPHASE = "day"
        transition_day(cli)

    for cloak in list(var.STASISED.keys()):
        var.STASISED[cloak] -= 1
        var.set_stasis(cloak, var.STASISED[cloak])
        if var.STASISED[cloak] <= 0:
            del var.STASISED[cloak]

    for acc in list(var.STASISED_ACCS.keys()):
        var.STASISED_ACCS[acc] -= 1
        var.set_stasis_acc(acc, var.STASISED_ACCS[acc])
        if var.STASISED_ACCS[acc] <= 0:
            del var.STASISED_ACCS[acc]

    if not botconfig.DEBUG_MODE:
        # DEATH TO IDLERS!
        reapertimer = threading.Thread(None, reaper, args=(cli,var.GAME_ID))
        reapertimer.daemon = True
        reapertimer.start()



@hook("error")
def on_error(cli, pfx, msg):
    if msg.endswith("(Excess Flood)"):
        restart_program(cli, "excess flood", "")
    elif msg.startswith("Closing Link:"):
        raise SystemExit

@cmd("fstasis", admin_only=True, pm=True)
def fstasis(cli, nick, chan, rest):
    """Removes or sets stasis penalties."""

    data = rest.split()
    msg = None

    if data:
        lusers = {k.lower(): v for k, v in var.USERS.items()}
        user = data[0]

        if user.lower() in lusers:
            cloak = lusers[user.lower()]["cloak"]
            acc = lusers[user.lower()]["account"]
        else:
            cloak = user
            acc = None
        if not acc and user in var.STASISED_ACCS:
            acc = user
        err_msg = u"La quantitat d'èxtasi no pot ser negativa."

        if cloak:
            if len(data) == 1:
                if cloak in var.STASISED:
                    plural = "" if var.STASISED[cloak] == 1 else "s"
                    msg = u"\u0002{0}\u0002 (Host: {1}) està en èxtasi durant \u0002{2}\u0002 joc{3}.".format(data[0], cloak, var.STASISED[cloak], plural)
                else:
                    msg = u"\u0002{0}\u0002 (Host: {1}) no està en èxtasi.".format(data[0], cloak)
            else:
                try:
                    amt = int(data[1])
                except ValueError:
                    if chan == nick:
                        pm(cli, nick, err_msg)
                    else:
                        cli.notice(nick, err_msg)

                    return

                if amt < 0:
                    if chan == nick:
                        pm(cli, nick, err_msg)
                    else:
                        cli.notice(nick, err_msg)

                    return
                elif amt > 2**31-1:
                    amt = 2**31-1

                if amt > 0:
                    var.STASISED[cloak] = amt
                    var.set_stasis(cloak, amt)
                    plural = "" if amt == 1 else "s"
                    msg = u"\u0002{0}\u0002 (Host: {1}) està en èxtasi durant \u0002{2}\u0002 joc{3}.".format(data[0], cloak, amt, plural)
                elif amt == 0:
                    if cloak in var.STASISED:
                        del var.STASISED[cloak]
                        var.set_stasis(cloak, 0)
                        msg = u"\u0002{0}\u0002 (Host: {1}) ja no està en èxtasi.".format(data[0], cloak)
                    else:
                        msg = u"\u0002{0}\u0002 (Host: {1}) no està en èxtasi.".format(data[0], cloak)
        if acc:
            if len(data) == 1:
                if acc in var.STASISED_ACCS:
                    plural = "" if var.STASISED_ACCS[acc] == 1 else "s"
                    msg = u"\u0002{0}\u0002 (Compte: {1}) està en èxtasi \u0002{2}\u0002 game{3}.".format(data[0], acc, var.STASISED_ACCS[acc], plural)
                else:
                    msg = u"\u0002{0}\u0002 (Compte: {1}) no està en èxasi.".format(data[0], acc)
            else:
                try:
                    amt = int(data[1])
                except ValueError:
                    if chan == nick:
                        pm(cli, nick, err_msg)
                    else:
                        cli.notice(nick, err_msg)
                    return

                if amt < 0:
                    if chan == nick:
                        pm(cli, nick, err_msg)
                    else:
                        cli.notice(nick, err_msg)
                    return
                elif amt > 2**31-1:
                    amt = 2**31-1

                if amt > 0:
                    var.STASISED_ACCS[acc] = amt
                    var.set_stasis_acc(acc, amt)
                    plural = "" if amt == 1 else "s"
                    msg = "\u0002{0}\u0002 (Compte: {1}) està en èxtasis per \u0002{2}\u0002 joc{3}.".format(data[0], acc, amt, plural)
                elif amt == 0:
                    if acc in var.STASISED_ACCS:
                        del var.STASISED_ACCS[acc]
                        var.set_stasis_acc(acc, 0)
                        msg = u"\u0002{0}\u0002 (Compte: {1}) ja no està en èxtasis.".format(data[0], acc)
                    else:
                        msg = u"\u0002{0}\u0002 (Compte: {1}) no està en èxtasis.".format(data[0], acc)
    elif var.STASISED or var.STASISED_ACCS:
        stasised = {}
        cloakstas = dict(var.STASISED)
        accstas = dict(var.STASISED_ACCS)
        for stas in var.USERS:
            if var.USERS[stas]["account"] in accstas:
                stasised[var.USERS[stas]["account"]+" (Account)"] = accstas.pop(var.USERS[stas]["account"])
                if var.USERS[stas]["cloak"] in cloakstas:
                    del cloakstas[var.USERS[stas]["cloak"]]
            elif var.USERS[stas]["cloak"] in cloakstas:
                stasised[var.USERS[stas]["cloak"]+" (Host)"] = cloakstas.pop(var.USERS[stas]["cloak"])
        for oldcloak in cloakstas:
            stasised[oldcloak+" (Host)"] = cloakstas[oldcloak]
        for oldacc in accstas:
            stasised[oldacc+" (Account)"] = accstas[oldacc]
        msg = u"Nicks en èxtasi: {0}".format(", ".join(
            "\u0002{0}\u0002 ({1})".format(usr, number)
            for usr, number in stasised.items()))
    else:
        msg = u"Ningú està en èxtasi."

    if msg:
        if data:
            tokens = msg.split()

            if ((data[0] == cloak and tokens[1] == "({0})".format(cloak)) or
                (data[0] == acc and tokens[1] == "({0})".format(acc))):
                # Don't show the cloak/account twice.
                msg = " ".join((tokens[0], " ".join(tokens[2:])))

        if chan == nick:
            pm(cli, nick, msg)
        else:
            cli.msg(chan, msg)

def is_user_stasised(nick):
    """Checks if a user is in stasis. Returns a tuple of two items.

    First parameter is True or False, and tells if the user is in stasis.
    If the first parameter is False, the second will always be None.
    If the first parameter is True, the second is an integer of the amount
    of games the user is in stasis."""

    if nick in var.USERS:
        cloak = var.USERS[nick]["cloak"]
        acc = var.USERS[nick]["account"]
    else:
        return False, None
    if acc and acc != "*":
        if acc in var.STASISED_ACCS:
            return True, var.STASISED_ACCS[acc]
        return False, None
    if cloak in var.STASISED:
        return True, var.STASISED[cloak]
    return False, None

def allow_deny(cli, nick, chan, rest, mode):
    data = rest.split()
    msg = None

    modes = ("allow", "deny")
    assert mode in modes, "mode not in {!r}".format(modes)

    if data:
        lusers = {k.lower(): v for k, v in var.USERS.items()}
        user = data[0]

        if user.lower() in lusers:
            cloak = lusers[user.lower()]["cloak"]
            acc = lusers[user.lower()]["account"]
        else:
            cloak = user
            acc = None

        if not acc or acc == "*":
            acc = None

        if acc:
            if mode == "allow":
                variable = var.ALLOW_ACCOUNTS
            else:
                variable = var.DENY_ACCOUNTS
            if len(data) == 1:
                if acc in variable:
                    msg = "\u0002{0}\u0002 (Account: {1}) is {2} the following {3}commands: {4}.".format(
                        data[0], acc, "allowed" if mode == 'allow' else "denied", "special " if mode == 'allow' else "", ", ".join(variable[acc]))
                else:
                    msg = "\u0002{0}\u0002 (Account: {1}) is not {2} commands.".format(data[0], acc, "allowed any special" if mode == 'allow' else "denied any")
            else:
                if acc not in variable:
                    variable[acc] = []
                commands = data[1:]
                for command in commands: # Add or remove commands one at a time to a specific account
                    if "-*" in commands: # Remove all
                        for cmd in variable[acc]:
                            if mode == "allow":
                                var.remove_allow_acc(acc, cmd)
                            else:
                                var.remove_deny_acc(acc, cmd)
                        del variable[acc]
                        break
                    if command[0] == "-": # Starting with - (to remove)
                        rem = True
                        command = command[1:]
                    else:
                        rem = False
                    if command.startswith(botconfig.CMD_CHAR): # ignore command prefix
                        command = command[len(botconfig.CMD_CHAR):]

                    if not rem:
                        if command in COMMANDS and command not in ("fdeny", "fallow", "exec", "eval") and command not in variable[acc]:
                            variable[acc].append(command)
                            if mode == "allow":
                                var.add_allow_acc(acc, command)
                            else:
                                var.add_deny_acc(acc, command)
                    elif command in variable[acc]:
                        variable[acc].remove(command)
                        if mode == "allow":
                            var.remove_allow_acc(acc, command)
                        else:
                            var.remove_deny_acc(acc, command)
                if acc in variable and variable[acc]:
                    msg = "\u0002{0}\u0002 (Account: {1}) is now {2} the following {3}commands: {4}{5}.".format(
                        data[0], acc, "allowed" if mode == 'allow' else "denied", "special " if mode == 'allow' else "", botconfig.CMD_CHAR, ", {0}".format(botconfig.CMD_CHAR).join(variable[acc]))
                else:
                    if acc in variable:
                        del variable[acc]
                    msg = "\u0002{0}\u0002 (Account: {1}) is no longer {2} commands.".format(data[0], acc, "allowed any special" if mode == 'allow' else "denied any")
        elif var.ACCOUNTS_ONLY:
            msg = "Error: \u0002{0}\u0002 is not logged in to NickServ.".format(data[0])

        else:
            if mode == "allow":
                variable = var.ALLOW
            else:
                variable = var.DENY
            if len(data) == 1: # List commands for a specific hostmask
                if cloak in variable:
                    msg = "\u0002{0}\u0002 (Host: {1}) is {2} the following {3}commands: {4}.".format(
                        data[0], cloak, "allowed" if mode == 'allow' else "denied", "special " if mode == 'allow' else "", ", ".join(variable[cloak]))
                else:
                    msg = "\u0002{0}\u0002 (Host: {1}) is not {2} commands.".format(data[0], cloak, "allowed any special" if mode == 'allow' else "denied any")
            else:
                if cloak not in variable:
                    variable[cloak] = []
                commands = data[1:]
                for command in commands: #add or remove commands one at a time to a specific hostmask
                    if "-*" in commands: # Remove all
                        for cmd in variable[cloak]:
                            if mode == "allow":
                                var.remove_allow(cloak, cmd)
                            else:
                                var.remove_deny(cloak, cmd)
                        del variable[cloak]
                        break
                    if command[0] == '-': #starting with - removes
                        rem = True
                        command = command[1:]
                    else:
                        rem = False
                    if command.startswith(botconfig.CMD_CHAR): #ignore command prefix
                        command = command[len(botconfig.CMD_CHAR):]

                    if not rem:
                        if command in COMMANDS and command not in ("fdeny", "fallow", "exec", "eval"):
                            variable[cloak].append(command)
                            if mode == "allow":
                                var.add_allow(cloak, command)
                            else:
                                var.add_deny(cloak, command)
                    elif command in variable[cloak]:
                        variable[cloak].remove(command)
                        if mode == "allow":
                            var.remove_allow(cloak, command)
                        else:
                            var.remove_deny(cloak, command)
                            
                if cloak in variable and variable[cloak]:
                    msg = "\u0002{0}\u0002 (Host: {1}) is now {2} the following {3}commands: {4}{5}.".format(
                        data[0], cloak, "allowed" if mode == 'allow' else "denied", "special " if mode == 'allow' else "", botconfig.CMD_CHAR, ", {0}".format(botconfig.CMD_CHAR).join(variable[cloak]))
                else:
                    if cloak in variable:
                        del variable[cloak]
                    msg = "\u0002{0}\u0002 (Host: {1}) is no longer {2} commands.".format(data[0], cloak, "allowed any special" if mode == 'allow' else "denied any")

    else:
        cmds = {}
        if mode == "allow":
            variable = var.ALLOW_ACCOUNTS
        else:
            variable = var.DENY_ACCOUNTS
        if variable:
            for acc, varied in variable.items():
                cmds[acc+" (Account)"] = varied
        if not var.ACCOUNTS_ONLY:
            if mode == "allow":
                variable = var.ALLOW
            else:
                variable = var.DENY
            if variable:
                for cloak, varied in variable.items():
                    cmds[cloak+" (Host)"] = varied

        if not cmds: # Deny or Allow list is empty
            msg = "Nobody is {0} commands.".format("allowed any special" if mode == 'allow' else "denied any")
        else:
            msg = "{0}: {1}".format("Allowed" if mode == "allow" else "Denied", ", ".join("\u0002{0}\u0002 ({1}{2})".format(user,
                botconfig.CMD_CHAR, ", {0}".format(botconfig.CMD_CHAR).join(cmd)) for user, cmd in cmds.items()))

    if msg:
        if data:
            tokens = msg.split()

            if ((data[0] == acc and tokens[1] == "({0})".format(acc)) or
                (data[0] == cloak and tokens[1] == "({0})".format(cloak))):
                # Don't show the cloak/account twice.
                msg = " ".join((tokens[0], " ".join(tokens[2:])))

        if chan == nick:
            pm(cli, nick, msg)
        else:
            cli.msg(chan, msg)

@cmd("fallow", admin_only=True, pm=True)
def fallow(cli, nick, chan, rest):
    """Allow someone to use an admin command."""
    allow_deny(cli, nick, chan, rest, "allow")

@cmd("fdeny", admin_only=True, pm=True)
def fdeny(cli, nick, chan, rest):
    """Deny someone from using a command."""
    allow_deny(cli, nick, chan, rest, "deny")

@cmd("wait", "w", join=True, playing=True)
def wait(cli, nick, chan, rest):
    """Increases the wait time until !start can be used."""
    pl = var.list_players()

    if chan != botconfig.CHANNEL:
        return
    if (var.LAST_WAIT and nick in var.LAST_WAIT and var.LAST_WAIT[nick] +
            timedelta(seconds=var.WAIT_RATE_LIMIT) > datetime.now()):
        cli.notice(nick, ("Aquesta ordre té límit de temps. Si us plau, espera una estona "
                          "abans de tornar-lo a utilitzar."))
        return
    if var.WAITED >= var.MAXIMUM_WAITED:
        cli.msg(chan, "S'ha assolit el temps d'espera màxim.")
        return

    now = datetime.now()
    var.LAST_WAIT[nick] = now
    if now > var.CAN_START_TIME:
        var.CAN_START_TIME = now + timedelta(seconds=var.EXTRA_WAIT)
    else:
        var.CAN_START_TIME += timedelta(seconds=var.EXTRA_WAIT)
    var.WAITED += 1
    cli.msg(chan, ("\u0002{0}\u0002 ha augmentat el temps d'espera durant "+
                  "{1} segons.").format(nick, var.EXTRA_WAIT))


@cmd("fwait", admin_only=True, join=True)
def fwait(cli, nick, chan, rest):
    """Forces an increase (or decrease) in wait time. Can be used with a number of seconds to wait."""

    pl = var.list_players()

    rest = re.split(" +", rest.strip(), 1)[0]

    if rest and (rest.isdigit() or (rest[0] == "-" and rest[1:].isdigit())):
        extra = int(rest)
    else:
        extra = var.EXTRA_WAIT

    now = datetime.now()

    if now > var.CAN_START_TIME:
        var.CAN_START_TIME = now + timedelta(seconds=extra)
    else:
        var.CAN_START_TIME += timedelta(seconds=extra)

    var.WAITED += 1

    cli.msg(chan, (u"\u0002{0}\u0002 ha {2} augmentat el temps d'espera forçadament durant {1} "
                   "segon{3}.").format(nick,
                                        abs(extra),
                                        "augmentat" if extra >= 0 else u"disminuït",
                                        "s" if extra != 1 else ""))


@cmd("fstop", admin_only=True, game=True, join=True)
def reset_game(cli, nick, chan, rest):
    """Forces the game to stop."""
    cli.msg(botconfig.CHANNEL, u"\u0002{0}\u0002 ha aturat el joc forçadament.".format(nick))
    if var.PHASE != "join":
        stop_game(cli)
    else:
        reset_modes_timers(cli)
        reset()

@cmd("rules", pm=True)
def show_rules(cli, nick, chan, rest):
    """Displays the rules."""
    if (var.PHASE in ("day", "night") and nick not in var.list_players()) or nick == chan:
        cli.notice(nick, var.RULES)
        return
    cli.msg(chan, var.RULES)

@cmd("help", raw_nick=True, pm=True)
def get_help(cli, rnick, chan, rest):
    """Gets help."""
    nick, mode, user, cloak = parse_nick(rnick)
    fns = []

    rest = rest.strip().replace(botconfig.CMD_CHAR, "", 1).lower()
    splitted = re.split(" +", rest, 1)
    cname = splitted.pop(0)
    rest = splitted[0] if splitted else ""
    if cname:
        if cname in COMMANDS.keys():
            for fn in COMMANDS[cname]:
                if fn.__doc__:
                    got = True
                    if callable(fn.__doc__):
                        msg = botconfig.CMD_CHAR+cname+": "+fn.__doc__(rest)
                    else:
                        msg = botconfig.CMD_CHAR+cname+": "+fn.__doc__
                    if chan == nick:
                        pm(cli, nick, msg)
                    else:
                        cli.notice(nick, msg)
                    return
                else:
                    got = False
                    continue
            else:
                if got:
                    return
                elif chan == nick:
                    pm(cli, nick, u"L'ajuda per aquesta ordre no està disponible.")
                else:
                    cli.notice(nick, u"L'ajuda per aquesta ordre no està disponible.")

        elif chan == nick:
            pm(cli, nick, "Ordre desconeguda.")
        else:
            cli.notice(nick, "Ordre desconeguda.")
        return

    # if command was not found, or if no command was given:
    for name, fn in COMMANDS.items():
        if ((name in ("away", "back") and var.OPT_IN_PING) or
            (name in ("in", "out") and not var.OPT_IN_PING)):
            continue
        if (name and not fn[0].admin_only and not fn[0].owner_only and name not
            in fn[0].aliases and fn[0].chan):
            fns.append("\u0002"+name+"\u0002")
    afns = []
    if is_admin(nick, cloak):
        for name, fn in COMMANDS.items():
            if fn[0].admin_only and name not in fn[0].aliases:
                afns.append("\u0002"+name+"\u0002")
    fns.sort() # Output commands in alphabetical order
    if chan == nick:
        pm(cli, nick, "Ordres: "+", ".join(fns))
    else:
        cli.notice(nick, "Ordres: "+", ".join(fns))
    if afns:
        afns.sort()
        if chan == nick:
            pm(cli, nick, "Ordres dels administradors: "+", ".join(afns))
        else:
            cli.notice(nick, "Ordres dels administradors: "+", ".join(afns))

@hook("invite")
def on_invite(cli, raw_nick, something, chan):
    if chan == botconfig.CHANNEL:
        cli.join(chan)
        return # No questions
    (nick, _, _, cloak) = parse_nick(raw_nick)
    if is_admin(nick, cloak):
        cli.join(chan) # Allows the bot to be present in any channel
        debuglog(nick, "INVITE", chan, display=True)
    else:
        pm(parse_nick(nick)[0], "No ets un administrador.")

@cmd("fpart", raw_nick=True, admin_only=True, pm=True)
def fpart(cli, rnick, chan, rest):
    """Makes the bot forcibly leave a channel."""
    nick = parse_nick(rnick)[0]
    if nick == chan:
        rest = rest.split()
        if not rest:
            pm(cli, nick, u"Utilització: fpart <canal>")
            return
        if rest[0] == botconfig.CHANNEL:
            pm(cli, nick, u"No, això no està permés.")
            return
        chan = rest[0]
        pm(cli, nick, "Sortint del canal "+ chan)
    if chan == botconfig.CHANNEL:
        cli.notice(nick, u"No, això no està permés.")
        return
    cli.part(chan)

@cmd("admins", "ops", pm=True)
def show_admins(cli, nick, chan, rest):
    """Pings the admins that are available."""

    admins = []
    pl = var.list_players()

    if (chan != nick and var.LAST_ADMINS and var.LAST_ADMINS +
            timedelta(seconds=var.ADMINS_RATE_LIMIT) > datetime.now()):
        cli.notice(nick, (u"Aquesta ordre té límit de temps. Espera una estona "
                          "abans de tornar-la a utilitzar."))
        return

    if chan != nick or (var.PHASE in ("day", "night") or nick in pl):
        var.LAST_ADMINS = datetime.now()

    if var.ADMIN_PINGING:
        return

    var.ADMIN_PINGING = True

    @hook("whoreply", hookid=4)
    def on_whoreply(cli, server, _, chan, __, cloak, ___, user, status, ____):
        if not var.ADMIN_PINGING:
            return

        if is_admin(user) and "G" not in status and user != botconfig.NICK:
            admins.append(user)

    @hook("endofwho", hookid=4)
    def show(*args):
        if not var.ADMIN_PINGING:
            return

        admins.sort(key=str.lower)

        msg = "Administradors disponibles: " + ", ".join(admins)

        if chan == nick:
            pm(cli, nick, msg)
        elif var.PHASE in ("day", "night") and nick not in pl:
            cli.notice(nick, msg)
        else:
            cli.msg(chan, msg)

        decorators.unhook(HOOKS, 4)
        var.ADMIN_PINGING = False

    if nick == chan:
        cli.who(botconfig.CHANNEL)
    else:
        cli.who(chan)

@cmd("coin")
def coin(cli, nick, chan, rest):
    """It's a bad idea to base any decisions on this command."""

    if var.PHASE in ("day", "night") and nick not in var.list_players():
        cli.notice(nick, "No pots utilitzar aquesta ordre ara.")
        return

    cli.msg(chan, u"\2{0}\2 llança una moneda a l'aire...".format(nick))
    coin = random.choice(["de cara", "de creu
    specialty = random.randrange(0,10)
    if specialty == 0:
        coin = "de costat"
    if specialty == 1:
        coin = botconfig.NICK
    cmsg = "La moneda cau \2{0}\2.".format(coin)
    cli.msg(chan, cmsg)

@cmd("pony")
def pony(cli, nick, chan, rest):
    """For entertaining bronies."""

    if var.PHASE in ("day", "night") and nick not in var.list_players():
        cli.notice(nick, "No pots utilitzar aquesta ordre ara.")
        return

    cli.msg(chan, u"\2{0}\2 llança un poni a l'aire...".format(nick))
    pony = random.choice(["costat", "quatre potes"])
    cmsg = "El poni cau de \2{0}\2.".format(pony)
    cli.msg(chan, cmsg)

@cmd("time", pm=True, game=True, join=True)
def timeleft(cli, nick, chan, rest):
    """Returns the time left until the next day/night transition."""

    if (chan != nick and var.LAST_TIME and
            var.LAST_TIME + timedelta(seconds=var.TIME_RATE_LIMIT) > datetime.now()):
        cli.notice(nick, ("Ordre amb límit de temps. Espera una estona "
                          "abans de tornar-la a utilitzar."))
        return

    if chan != nick:
        var.LAST_TIME = datetime.now()

    if var.PHASE in var.TIMERS:
        t = var.TIMERS[var.PHASE]
        remaining = int((t[1] + t[2]) - time.time())
        if var.PHASE == "day":
            what = "el Sol es pongui"
        elif var.PHASE == "night":
            what = "el Sol neixi"
        elif var.PHASE == "join":
            what = u"el joc sigui cancel·lat"
        msg = u"Falten \u0002{0[0]:0>2}:{0[1]:0>2}\u0002 perquè {1}.".format(divmod(remaining, 60), what)
    else:
        msg = u"{0} els cronòmetres estàn desactivats.".format(var.PHASE.capitalize())

    if nick == chan:
        pm(cli, nick, msg)
    elif nick not in var.list_players() and var.PHASE not in ("none", "join"):
        cli.notice(nick, msg)
    else:
        cli.msg(chan, msg)

@cmd("roles", pm=True)
def listroles(cli, nick, chan, rest):
    """Displays which roles are enabled at a certain number of players."""

    old = {}
    txt = ""
    index = 0
    pl = len(var.list_players()) + len(var.DEAD)
    roleindex = var.ROLE_INDEX
    roleguide = var.ROLE_GUIDE

    for r in var.ROLE_GUIDE.keys():
        old[r] = 0
    rest = re.split(" +", rest.strip(), 1)
    #prepend player count if called without any arguments
    if not len(rest[0]) and pl > 0:
        txt += " {0}: Hi ha \u0002{2}\u0002 jugant.".format(nick, pl)
        if var.PHASE in ["night", "day"]:
            txt += " Utilitzant el mode de joc {0}.".format(var.CURRENT_GAMEMODE)

    #read game mode to get roles for
    if len(rest[0]) and not rest[0].isdigit():
        gamemode = rest[0]
        if gamemode not in var.GAME_MODES.keys():
            gamemode, _ = complete_match(rest[0], var.GAME_MODES.keys() - ["roles"])
        if gamemode in var.GAME_MODES.keys() and gamemode != "roles":
            mode = var.GAME_MODES[gamemode][0]()
            if hasattr(mode, "ROLE_INDEX"):
                roleindex = getattr(mode, "ROLE_INDEX")
            if hasattr(mode, "ROLE_GUIDE"):
                roleguide = getattr(mode, "ROLE_GUIDE")
            rest.pop(0)
        else:
            txt += u" {0}: {1} no és un mode de joc vàlid.".format(nick, rest[0])
            rest = []
            roleindex = {}

    #number of players to print the game mode for
    if len(rest) and rest[0].isdigit():
        index = int(rest[0])
        for i in range(len(roleindex)-1, -1, -1):
            if roleindex[i] <= index:
                index = roleindex[i]
                break

    #special ordering
    roleguide = [(role, roleguide[role]) for role in var.role_order()]
    for i in range(0, len(roleindex)):
        #getting the roles at a specific player count
        if index:
            if roleindex[i] < index:
                continue
            elif roleindex[i] > index:
                break
        txt += " {0}[{1}]{0} ".format(BOLD if roleindex[i] <= pl else "", str(roleindex[i]))
        roles = []
        for role, amount in roleguide:
            direction = 1 if amount[i] > old[role] else -1
            for j in range(old[role], amount[i], direction):
                temp = "{0}{1}".format("-" if direction == -1 else "", role)
                if direction == 1 and j+1 > 1:
                    temp += "({0})".format(j+1)
                elif j > 1:
                    temp += "({0})".format(j)
                roles.append(temp)
            old[role] = amount[i]
        txt += ", ".join(roles)
    txt = txt[1:]

    if chan == nick:
        pm(cli, nick, txt)
    elif nick not in var.list_players() and var.PHASE not in ("none", "join"):
        cli.notice(nick, txt)
    else:
        cli.msg(chan, txt)

@cmd("myrole", pm=True, game=True)
def myrole(cli, nick, chan, rest):
    """Reminds you of your current role."""

    ps = var.list_players()
    if nick not in ps and nick not in var.VENGEFUL_GHOSTS.keys():
        cli.notice(nick, "No estas jugant.")
        return

    role = var.get_role(nick)
    if role in ("time lord", "village elder", "amnesiac"):
        role = var.DEFAULT_ROLE
    elif role == "vengeful ghost" and nick not in var.VENGEFUL_GHOSTS.keys():
        role = var.DEFAULT_ROLE
    an = "n" if role[0] in ("a", "e", "i", "o", "u") else ""
    pm(cli, nick, "Ets un{0} \02{1}{2}\02.".format(an, role, " assassin" if nick in var.ROLES["assassin"] and nick not in var.ROLES["amnesiac"] else ""))

    if role in var.TOTEM_ORDER and role != "crazed shaman" and var.PHASE == "night" and nick not in var.SHAMANS:
        pm(cli, nick, u"Tens el tòtem \u0002{0}\u0002.".format(var.TOTEMS[nick]))

    # Check for gun/bullets
    if nick not in ps:
        return
    if nick not in var.ROLES["amnesiac"] and nick in var.GUNNERS and var.GUNNERS[nick]:
        role = "gunner"
        if nick in var.ROLES["sharpshooter"]:
            role = "sharpshooter"
        if var.GUNNERS[nick] == 1:
            pm(cli, nick, "Ets un {0} i tens una \02pistola\02 amb {1} {2}.".format(role, var.GUNNERS[nick], "bala"))
        else:
            pm(cli, nick, "Ets un {0} i tens una \02pistola\02 amb {1} {2}.".format(role, var.GUNNERS[nick], "bales"))
    elif nick in var.WOLF_GUNNERS and var.WOLF_GUNNERS[nick]:
        if var.WOLF_GUNNERS[nick] == 1:
            pm(cli, nick, "Tens una \02pitola\02 amb {0} {1}.".format(var.WOLF_GUNNERS[nick], "bala"))
        else:
            pm(cli, nick, "Tens una \02pistola\02 amb {0} {1}.".format(var.WOLF_GUNNERS[nick], "bales"))

    # Remind lovers of each other
    if nick in ps and nick in var.LOVERS:
        message = u"Estàs \02enamorat\02 amb "
        lovers = sorted(list(set(var.LOVERS[nick])))
        if len(lovers) == 1:
            message += lovers[0]
        elif len(lovers) == 2:
            message += lovers[0] + " i " + lovers[1]
        else:
            message += ", ".join(lovers[:-1]) + ", i " + lovers[-1]
        message += "."
        pm(cli, nick, message)

@cmd("faftergame", admin_only=True, raw_nick=True, pm=True)
def aftergame(cli, rawnick, chan, rest):
    """Schedule a command to be run after the current game."""
    nick = parse_nick(rawnick)[0]
    if not rest.strip():
        cli.notice(nick, "Sintaxi incorrecta.")
        return

    rst = re.split(" +", rest)
    cmd = rst.pop(0).lower().replace(botconfig.CMD_CHAR, "", 1).strip()

    if cmd in COMMANDS.keys():
        def do_action():
            for fn in COMMANDS[cmd]:
                fn(cli, rawnick, botconfig.CHANNEL if fn.chan else nick, " ".join(rst))
    else:
        cli.notice(nick, "Ordre desconeguda.")
        return

    if var.PHASE == "none":
        do_action()
        return

    cli.msg(botconfig.CHANNEL, (u"L'ordre \02{0}\02 ha estat programada per executar-se "+
                  u"després d'aquest joc per \02{1}\02.").format(cmd, nick))
    var.AFTER_FLASTGAME = do_action

@cmd("fghost", admin_only=True, pm=True)
def fghost(cli, nick, chan, rest):
    """Voices you, allowing you to haunt the remaining players after your death."""
    cli.mode(botconfig.CHANNEL, '+v', nick)

@cmd("funghost", admin_only=True, pm=True)
def funghost(cli, nick, chan, rest):
    """Devoices you."""
    cli.mode(botconfig.CHANNEL, "-v", nick)

@cmd("flastgame", admin_only=True, raw_nick=True, pm=True)
def flastgame(cli, nick, chan, rest):
    """Disables starting or joining a game, and optionally schedules a command to run after the current game ends."""
    nick, _, __, cloak = parse_nick(nick)

    chan = botconfig.CHANNEL
    if var.PHASE != "join":
        if "join" in COMMANDS.keys():
            del COMMANDS["join"]
            cmd("join")(lambda *spam: cli.msg(chan, "Aquesta ordre ha estat desactivada per un admin."))
            # manually recreate the command by calling the decorator function
        if "j" in COMMANDS.keys():
            del COMMANDS["j"]
            cmd("j")(lambda *spam: cli.msg(chan, "Aquesta ordre ha estat desactivada per un admin."))
        if "start" in COMMANDS.keys():
            del COMMANDS["start"]
            cmd("start")(lambda *spam: cli.msg(chan, "Aquesta ordre ha estat desactivada per un admin."))

    cli.msg(chan, "Iniciar un nou joc ha estat desactivat per \02{0}\02.".format(nick))
    var.ADMIN_TO_PING = nick

    if rest.strip():
        aftergame(cli, rawnick, botconfig.CHANNEL, rest)

@cmd("gamestats", "gstats", pm=True)
def game_stats(cli, nick, chan, rest):
    """Gets the game stats for a given game size or lists game totals for all game sizes if no game size is given."""
    if (chan != nick and var.LAST_GSTATS and var.GSTATS_RATE_LIMIT and
            var.LAST_GSTATS + timedelta(seconds=var.GSTATS_RATE_LIMIT) >
            datetime.now()):
        cli.notice(nick, ("Aquesta ordre té límit de temps. Espera una estona "
                          "abans de tornar-la a utilitzar."))
        return

    if chan != nick:
        var.LAST_GSTATS = datetime.now()
        if var.PHASE not in ('none', 'join'):
            cli.notice(nick, u"Espera a acabar el joc per veure les estadístiques.")
            return

    gamemode = var.CURRENT_GAMEMODE
    gamesize = None
    rest = rest.split()
    # Check for gamemode
    if len(rest) and not rest[0].isdigit():
        gamemode = rest[0]
        if gamemode not in var.GAME_MODES.keys():
            gamemode, _ = complete_match(gamemode, var.GAME_MODES.keys())
        if not gamemode:
            cli.notice(nick, u"{0} no és un mode de joc vàlid".format(rest[0]))
            return
        rest.pop(0)
    # Check for invalid input
    if len(rest) and rest[0].isdigit():
        gamesize = int(rest[0])
        if gamesize > var.GAME_MODES[gamemode][2] or gamesize < var.GAME_MODES[gamemode][1]:
            cli.notice(nick, u"Si us plau, escriu un número entre "+\
                              "{0} i {1}.".format(var.GAME_MODES[gamemode][1], var.GAME_MODES[gamemode][2]))
            return

    # List all games sizes and totals if no size is given
    if not gamesize:
        if chan == nick:
            pm(cli, nick, var.get_game_totals(gamemode))
        else:
            cli.msg(chan, var.get_game_totals(gamemode))
    else:
        # Attempt to find game stats for the given game size
        if chan == nick:
            pm(cli, nick, var.get_game_stats(gamemode, gamesize))
        else:
            cli.msg(chan, var.get_game_stats(gamemode, gamesize))

@cmd("playerstats", "pstats", "player", "p", pm=True)
def player_stats(cli, nick, chan, rest):
    """Gets the stats for the given player and role or a list of role totals if no role is given."""
    if (chan != nick and var.LAST_PSTATS and var.PSTATS_RATE_LIMIT and
            var.LAST_PSTATS + timedelta(seconds=var.PSTATS_RATE_LIMIT) >
            datetime.now()):
        cli.notice(nick, ('Aquesta ordre és de temps limitat. Si us plau espera un estona '
                          'abans de tornar-la a utilitzar.'))
        return

    if chan != nick:
        var.LAST_PSTATS = datetime.now()
        if var.PHASE not in ('none', 'join'):
            cli.notice(nick, u'Espera a que s\'acabi el joc per veure les estadístiques.')
            return

    params = rest.split()

    # Check if we have enough parameters
    if params:
        user = params[0]
    else:
        user = nick

    # Find the player's account if possible
    luser = user.lower()
    lusers = {k.lower(): v for k, v in var.USERS.items()}
    if luser in lusers:
        acc = lusers[luser]['account']
        if acc == '*':
            if luser == nick.lower():
                cli.notice(nick, u'No estas registrat al NickServ.')
            else:
                cli.notice(nick, user + u' no està registrat amb el NickServ.')

            return
    else:
        acc = user

    # List the player's total games for all roles if no role is given
    if len(params) < 2:
        if chan == nick:
            pm(cli, nick, var.get_player_totals(acc))
        else:
            cli.msg(chan, var.get_player_totals(acc))
    else:
        role = ' '.join(params[1:])

        # Attempt to find the player's stats
        if chan == nick:
            pm(cli, nick, var.get_player_stats(acc, role))
        else:
            cli.msg(chan, var.get_player_stats(acc, role))

@cmd("mystats", "me", "m", pm=True)
def my_stats(cli, nick, chan, rest):
    rest = rest.split()
    player_stats(cli, nick, chan, " ".join([nick] + rest))

@cmd("game", raw_nick=True, join=True, playing=True)
def game(cli, nick, chan, rest):
    nick, _, __, cloak = parse_nick(nick)

    if rest:
        gamemode = rest.lower().split()[0]
    else:
        gamemodes = ", ".join(["\002{}\002".format(gamemode) if len(var.list_players()) in range(var.GAME_MODES[gamemode][1], 
        var.GAME_MODES[gamemode][2]+1) else gamemode for gamemode in var.GAME_MODES.keys() if gamemode != "roles"])
        cli.notice(nick, "No has especificat un mode de joc. Modes de joc disponibles: " + gamemodes)
        return

    if var.FGAMED:
        cli.notice(nick, u"Un administrador ja ha forçat un mode de joc.")
        return

    if gamemode not in var.GAME_MODES.keys():
        match, _ = complete_match(gamemode, var.GAME_MODES.keys() - ["roles"])
        if not match:
            cli.notice(nick, u"\002{0}\002 No és un mode de joc vàlid.".format(gamemode))
            return
        gamemode = match
    
    if gamemode != "roles":
        var.GAMEMODE_VOTES[cloak] = gamemode
        cli.msg(chan, u"\002{0}\002 ha votat pel mode de joc \002{1}\002.".format(nick, gamemode))
    else:
        cli.notice(nick, u"No pots votar per aquest mode de joc.")

def game_help(args=''):
    return "Votes to make a specific game mode more likely. Available game mode setters: " +\
        ", ".join(["\002{}\002".format(gamemode) if len(var.list_players()) in range(var.GAME_MODES[gamemode][1], var.GAME_MODES[gamemode][2]+1)
        else gamemode for gamemode in var.GAME_MODES.keys() if gamemode != "roles"])
game.__doc__ = game_help


@cmd("vote", "v", raw_nick=True, pm=True)
def vote(cli, raw_nick, chan, rest):
    nick = parse_nick(raw_nick)[0]

    if var.PHASE == "join" and chan != nick:
        return game(cli, raw_nick, chan, rest)
    else:
        return lynch(cli, nick, chan, rest)


@cmd("fpull", admin_only=True, pm=True)
def fpull(cli, nick, chan, rest):
    """Pulls from the repository to update the bot."""

    args = ["git", "pull", "--stat", "--rebase=preserve"]

    if rest:
        args += rest.split(" ")

    child = subprocess.Popen(args,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    (out, err) = child.communicate()
    ret = child.returncode

    for line in (out + err).splitlines():
        if chan == nick:
            cli.msg(nick, line.decode("utf-8"))
        else:
            pm(cli, nick, line.decode("utf-8"))

    if ret != 0:
        if ret < 0:
            cause = "signal"
            ret *= -1
        else:
            cause = "status"

        if chan == nick:
            cli.msg(nick, u"Procés %s finalitzat amb %s %d" % (args, cause, ret))
        else:
            pm(cli, nick, u"Procés %s finalitzat amb %s %d" % (args, cause, ret))

@cmd("fsend", admin_only=True, pm=True)
def fsend(cli, nick, chan, rest):
    cli.send(rest)

def _say(cli, raw_nick, rest, command, action=False):
    (nick, _, _, cloak) = parse_nick(raw_nick)
    rest = rest.split(" ", 1)

    if len(rest) < 2:
        pm(cli, nick, u"Utilització: {0}{1} <nick> <missatge>".format(
            botconfig.CMD_CHAR, command))

        return

    (target, message) = rest

    if not is_admin(nick, cloak):
        if nick not in var.USERS:
            pm(cli, nick, u"Has d'estar a {0} per utilitzar aquesta ordre.".format(
                botconfig.CHANNEL))

            return

        if rest[0] != botconfig.CHANNEL:
            pm(cli, nick, (u"No tens permís per enviar missatges a aquest nick "
                           u"o canal."))

            return

    if action:
        message = "\x01ACTION {0}\x01".format(message)

    cli.send("PRIVMSG {0} :{1}".format(target, message))


@cmd("fsay", admin_only=True, raw_nick=True, pm=True)
def fsay(cli, raw_nick, chan, rest):
    _say(cli, raw_nick, rest, "fsay")

@cmd("fact", "fdo", "fme", admin_only=True, raw_nick=True, pm=True)
def fact(cli, raw_nick, chan, rest):
    _say(cli, raw_nick, rest, "fact", action=True)

before_debug_mode_commands = list(COMMANDS.keys())

if botconfig.DEBUG_MODE or botconfig.ALLOWED_NORMAL_MODE_COMMANDS:

    @cmd("eval", owner_only=True, pm=True)
    def pyeval(cli, nick, chan, rest):
        try:
            a = str(eval(rest))
            if len(a) < 500:
                cli.msg(chan, a)
            else:
                cli.msg(chan, a[:500])
        except Exception as e:
            cli.msg(chan, str(type(e))+":"+str(e))

    @cmd("exec", owner_only=True, pm=True)
    def py(cli, nick, chan, rest):
        try:
            exec(rest)
        except Exception as e:
            cli.msg(chan, str(type(e))+":"+str(e))

    @cmd("revealroles", admin_only=True, pm=True, join=True, game=True)
    def revealroles(cli, nick, chan, rest):
        s = ' | '.join('\u0002{}\u0002: {}'.format(role,', '.join(players))
                for (role, players) in sorted(var.ROLES.items()) if players)

        if chan == nick:
            pm(cli, nick, s)
        else:
            cli.msg(chan, s)


    @cmd("fgame", admin_only=True, raw_nick=True, join=True)
    def fgame(cli, nick, chan, rest):
        nick = parse_nick(nick)[0]

        pl = var.list_players()

        if nick not in pl and not is_admin(nick):
            cli.notice(nick, u"No estas jugant.")
            return

        if rest:
            rest = gamemode = rest.strip().lower()
            if rest not in var.GAME_MODES.keys() and not rest.startswith("roles"):
                rest = rest.split()[0]
                gamemode, _ = complete_match(rest, var.GAME_MODES.keys())
                if not gamemode:
                    cli.notice(nick, u"\002{0}\002 no és un mode de joc vàlid.".format(rest))
                    return

            if cgamemode(cli, gamemode):
                cli.msg(chan, (u'\u0002{}\u0002 ha canviat la configuració dels modes de joc '
                                u'amb èxit.').format(nick))
                var.FGAMED = True
        else:
            cli.notice(nick, fgame.__doc__())

    def fgame_help(args=''):
        args = args.strip()

        if not args:
            return u'Configuradors de mode de joc disponibles: ' + ', '.join(var.GAME_MODES.keys())
        elif args in var.GAME_MODES.keys():
            if hasattr(var.GAME_MODES[args][0], "__doc__"):
                return var.GAME_MODES[args][0].__doc__
            else:
                return u"El mode de joc {0} no té un string 'doc'".format(args)
        else:
            return u'No s\'ha trobat el configurador del mode de joc \u0002{}\u0002.'.format(args)


    fgame.__doc__ = fgame_help


    # DO NOT MAKE THIS A PMCOMMAND ALSO
    @cmd("force", admin_only=True)
    def force(cli, nick, chan, rest):
        rst = re.split(" +",rest)
        if len(rst) < 2:
            cli.msg(chan, "Sintaxi incorrecta.")
            return
        who = rst.pop(0).strip()
        if not who or who == botconfig.NICK:
            cli.msg(chan, u"Això no funcionarà.")
            return
        if who == "*":
            who = var.list_players()
        else:
            if not is_fake_nick(who):
                ul = list(var.USERS.keys())
                ull = [u.lower() for u in ul]
                if who.lower() not in ull:
                    cli.msg(chan, u"Només es pot fer als jugadors que estiguin al canal o en un nick fals.")
                    return
                else:
                    who = [ul[ull.index(who.lower())]]
            else:
                who = [who]
        comm = rst.pop(0).lower().replace(botconfig.CMD_CHAR, "", 1)
        if comm in COMMANDS and not COMMANDS[comm][0].owner_only:
            for fn in COMMANDS[comm]:
                if fn.owner_only:
                    continue
                if fn.admin_only and nick in var.USERS and not is_admin(nick):
                    # Not a full admin
                    cli.notice(nick, u"Només els administradors poden forçar una ordre restringida.")
                    continue
                for user in who:
                    if fn.chan:
                        fn(cli, user, chan, " ".join(rst))
                    else:
                        fn(cli, user, user, " ".join(rst))
            cli.msg(chan, u"Operació completada amb èxit.")
        else:
            cli.msg(chan, "Ordre desconeguda.")


    @cmd("rforce", admin_only=True)
    def rforce(cli, nick, chan, rest):
        rst = re.split(" +",rest)
        if len(rst) < 2:
            cli.msg(chan, "Sintaxi incorrecta.")
            return
        who = rst.pop(0).strip().lower()
        who = who.replace("_", " ")

        if who == "*": # wildcard match
            tgt = var.list_players()
        elif (who not in var.ROLES or not var.ROLES[who]) and (who != "gunner"
            or var.PHASE in ("none", "join")):
            cli.msg(chan, nick+": invalid role")
            return
        elif who == "gunner":
            tgt = list(var.GUNNERS.keys())
        else:
            tgt = var.ROLES[who]

        comm = rst.pop(0).lower().replace(botconfig.CMD_CHAR, "", 1)
        if comm in COMMANDS and not COMMANDS[comm][0].owner_only:
            for fn in COMMANDS[comm]:
                if fn.owner_only:
                    continue
                if fn.admin_only and nick in var.USERS and not is_admin(nick):
                    # Not a full admin
                    cli.notice(nick, u"Només els administradors poden forçar una ordre restringida.")
                    continue
                for user in tgt[:]:
                    if fn.chan:
                        fn(cli, user, chan, " ".join(rst))
                    else:
                        fn(cli, user, user, " ".join(rst))
            cli.msg(chan, u"Operació completada amb èxit.")
        else:
            cli.msg(chan, "Ordre desconeguda.")



    @cmd("frole", admin_only=True)
    def frole(cli, nick, chan, rest):
        rst = re.split(" +",rest)
        if len(rst) < 2:
            cli.msg(chan, "Sintaxi incorrecta.")
            return
        who = rst.pop(0).strip()
        rol = " ".join(rst).strip()
        ul = list(var.USERS.keys())
        ull = [u.lower() for u in ul]
        if who.lower() not in ull:
            if not is_fake_nick(who):
                cli.msg(chan, u"No fet.")
                cli.msg(chan, u"El nick ha d'estar al canal.")
                return
        if not is_fake_nick(who):
            who = ul[ull.index(who.lower())]
        if who == botconfig.NICK or not who:
            cli.msg(chan, "No.")
            return
        pl = var.list_players()
        rolargs = re.split("\s*=\s*", rol, 1)
        rol = rolargs[0]
        if rol[1:] in var.TEMPLATE_RESTRICTIONS.keys():
            addrem = rol[0]
            rol = rol[1:]
            is_gunner = (rol == "gunner" or rol == "sharpshooter")
            if addrem == "+" and who not in var.ROLES[rol]:
                if is_gunner:
                    if len(rolargs) == 2 and rolargs[1].isdigit():
                        if len(rolargs[1]) < 7:
                            var.GUNNERS[who] = int(rolargs[1])
                            var.WOLF_GUNNERS[who] = int(rolargs[1])
                        else:
                            var.GUNNERS[who] = 999
                            var.WOLF_GUNNERS[who] = 999
                    elif rol == "gunner":
                        var.GUNNERS[who] = math.ceil(var.SHOTS_MULTIPLIER * len(pl))
                    else:
                        var.GUNNERS[who] = math.ceil(var.SHARPSHOOTER_MULTIPLIER * len(pl))
                var.ROLES[rol].append(who)
            elif addrem == "-" and who in var.ROLES[rol]:
                var.ROLES[rol].remove(who)
                if is_gunner and who in var.GUNNERS:
                    del var.GUNNERS[who]
            else:
                cli.msg(chan, u"Modificació de plantilla impròpia.")
                return
        elif rol in var.TEMPLATE_RESTRICTIONS.keys():
            cli.msg(chan, u"Si us plau, especifica \u0002+{0}\u0002 o \u0002-{0}\u0002 per afegir/suprimir aquesta plantilla.".format(rol))
            return
        elif rol in var.ROLES.keys():
            if who in pl:
                oldrole = var.get_role(who)
                var.ROLES[oldrole].remove(who)
            if rol in var.TOTEM_ORDER:
                if len(rolargs) == 2:
                    var.TOTEMS[who] = rolargs[1]
                else:
                    max_totems = {}
                    for sham in var.TOTEM_ORDER:
                        max_totems[sham] = 0
                    for ix in range(len(var.TOTEM_ORDER)):
                        for c in var.TOTEM_CHANCES.values():
                            max_totems[var.TOTEM_ORDER[ix]] += c[ix]
                    for shaman in var.list_players(var.TOTEM_ORDER):
                        indx = var.TOTEM_ORDER.index(rol)
                        target = 0
                        rand = random.random() * max_totems[var.TOTEM_ORDER[indx]]
                        for t in var.TOTEM_CHANCES.keys():
                            target += var.TOTEM_CHANCES[t][indx]
                            if rand <= target:
                                var.TOTEMS[shaman] = t
                                break
            var.ROLES[rol].append(who)
        else:
            cli.msg(chan, u"Rol invàlid.")
            return
        cli.msg(chan, u"Operació amb èxit.")
        if var.PHASE not in ('none','join'):
            chk_win(cli)


if botconfig.ALLOWED_NORMAL_MODE_COMMANDS and not botconfig.DEBUG_MODE:
    for comd in list(COMMANDS.keys()):
        if (comd not in before_debug_mode_commands and
            comd not in botconfig.ALLOWED_NORMAL_MODE_COMMANDS):
            del COMMANDS[comd]

# vim: set expandtab:sw=4:ts=4:
