"""Flyers Cup Live Scoreboard — Flask app powered by the GameSheet package."""

import logging
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template, request
from flask_cors import CORS

from gamesheet import GameSheetClient, LiveGamePoller, Game

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SEASON_ID = "13707"
EASTERN = ZoneInfo("America/New_York")
SCHEDULE_REFRESH = 300  # 5 minutes
LIVE_POLL_INTERVAL = 15  # seconds

# Flyers Cup tournament bracket configuration by division
BRACKET_GROUPS = {
    "tournament": {
        "title": "Flyers Cup Tournament Brackets",
        "brackets": [
            {
                "name": "A",
                "key": "a",
                "division_id": "72724",
                "seeds": {},
            },
            {
                "name": "AA",
                "key": "aa",
                "division_id": "72727",
                "seeds": {},
            },
            {
                "name": "AAA",
                "key": "aaa",
                "division_id": "72726",
                "seeds": {},
            },
            {
                "name": "Girls",
                "key": "girls",
                "division_id": "72725",
                "seeds": {},
            },
        ],
    },
}

# Round naming based on distance from championship
_ROUND_NAMES = ["Championship", "Semifinals", "Quarterfinals", "First Round"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

client = GameSheetClient(season_id=SEASON_ID)

# Shared state — protected by _lock for compound operations
_lock = threading.Lock()
_schedule_games: list[Game] = []       # Today's games from schedule API
_yesterday_finals: list[Game] = []     # Yesterday's completed games
_all_games: list[Game] = []            # All games (for embed widget compat)
_game_divisions: dict[str, str] = {}   # game_id -> division_id
_live_data: dict[str, dict] = {}       # game_id -> serialized live game
_divisions: list = []
_last_schedule_fetch: str | None = None
_last_live_update: str | None = None
_poller: LiveGamePoller | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fix_logo(url: str) -> str:
    """Ensure logo URL has the /256 suffix for proper sizing."""
    if not url:
        return ""
    if url.endswith(("/256", "/public", "/128")):
        return url
    return f"{url}/256"


def serialize_game(game: Game) -> dict:
    """Convert a Game dataclass to a JSON-friendly dict."""
    return {
        "id": game.id,
        "status": game.status,
        "date": game.date,
        "scheduled_time": _format_game_time(game.scheduled_time),
        "location": game.location,
        "game_number": game.game_number,
        "game_type": game.game_type,
        "home": {
            "name": game.home.name,
            "id": game.home.id,
            "logo_url": _fix_logo(game.home.logo_url),
            "division": game.home.division,
            "record": game.home.record,
        },
        "visitor": {
            "name": game.visitor.name,
            "id": game.visitor.id,
            "logo_url": _fix_logo(game.visitor.logo_url),
            "division": game.visitor.division,
            "record": game.visitor.record,
        },
        "scoreboard": {
            "periods": game.scoreboard.periods,
            "shots": game.scoreboard.shots,
            "total": game.scoreboard.total,
            "total_shots": game.scoreboard.total_shots,
        },
        "has_overtime": game.has_overtime,
        "has_shootout": game.has_shootout,
        "gamesheet_url": f"https://gamesheetstats.com/seasons/{SEASON_ID}/games/{game.id}",
    }


def _parse_game_datetime(date_str: str, time_str: str = "") -> datetime | None:
    """Parse GameSheet date + optional time into an Eastern datetime.

    The schedule API returns dates like "Feb 15, 2026" and times as ISO
    timestamps like "2026-02-15T20:00:00Z".
    """
    if not date_str:
        return None

    # Strip day-name prefix ("Sat, Feb 15, 2026" -> "Feb 15, 2026")
    if ", " in date_str:
        parts = date_str.split(", ", 1)
        if parts[0] in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            date_str = parts[1]

    dt = None
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str, fmt)
            break
        except ValueError:
            continue

    if dt is None:
        return None

    if time_str:
        # Try ISO timestamp first (e.g. "2026-02-15T20:00:00Z")
        parsed_time = _parse_iso_time(time_str)
        if parsed_time:
            dt = dt.replace(hour=parsed_time.hour, minute=parsed_time.minute)
        else:
            for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
                try:
                    t = datetime.strptime(time_str.strip(), fmt)
                    dt = dt.replace(hour=t.hour, minute=t.minute)
                    break
                except ValueError:
                    continue

    return dt.replace(tzinfo=EASTERN)


def _parse_iso_time(time_str: str) -> datetime | None:
    """Parse an ISO timestamp from GameSheet.

    GameSheet's scheduleStartTime uses a 'Z' suffix but the times are
    actually Eastern, NOT UTC.  We strip the 'Z' and treat the value as
    Eastern directly.
    """
    try:
        ts = time_str.strip().rstrip("Z")
        naive_dt = datetime.fromisoformat(ts)
        return naive_dt.replace(tzinfo=EASTERN)
    except (ValueError, TypeError):
        return None


def _format_game_time(time_str: str) -> str:
    """Format the scheduled_time for display (e.g. '8:10 PM')."""
    if not time_str:
        return ""
    parsed = _parse_iso_time(time_str)
    if parsed:
        return parsed.strftime("%-I:%M %p")
    return time_str


def _is_today(game: Game) -> bool:
    """Check if a game is scheduled for today (Eastern)."""
    today = datetime.now(EASTERN).date()
    gdt = _parse_game_datetime(game.date)
    return gdt is not None and gdt.date() == today


def _is_yesterday(game: Game) -> bool:
    """Check if a game was scheduled for yesterday (Eastern)."""
    yesterday = (datetime.now(EASTERN) - timedelta(days=1)).date()
    gdt = _parse_game_datetime(game.date)
    return gdt is not None and gdt.date() == yesterday


def _should_poll(game: Game) -> bool:
    """Return True if this game should be polled for live data.

    Polls games that aren't final and whose scheduled time falls within
    [-4h, +15min] of now — covers in-progress games and games about to start.
    """
    if game.status in ("final", "unofficial"):
        return False
    now = datetime.now(EASTERN)
    gdt = _parse_game_datetime(game.date, game.scheduled_time)
    if gdt is None:
        # No parseable time — poll if game is today and not final
        return _is_today(game)
    return (now - timedelta(hours=4)) <= gdt <= (now + timedelta(minutes=15))


# ---------------------------------------------------------------------------
# Schedule refresh + LiveGamePoller management
# ---------------------------------------------------------------------------

def _refresh_schedule():
    """Fetch schedule for all divisions, filter to today, update poller."""
    global _schedule_games, _yesterday_finals, _all_games, _game_divisions, _divisions, _last_schedule_fetch

    try:
        divisions = client.get_divisions()
        _divisions = divisions

        all_games: list[Game] = []
        game_divs: dict[str, str] = {}
        scores_by_id: dict[str, Game] = {}

        for div in divisions:
            try:
                games = client.get_schedule(div.id)
                for g in games:
                    if g.id:
                        game_divs[g.id] = div.id
                all_games.extend(games)
            except Exception:
                logger.exception("Error fetching schedule for %s", div.title)

            # Also fetch scores to get final results (schedule endpoint
            # doesn't include scoring data for completed games)
            try:
                scored = client.get_scores(div.id)
                for g in scored:
                    if g.id:
                        scores_by_id[g.id] = g
            except Exception:
                logger.exception("Error fetching scores for %s", div.title)

        # Dedupe by game ID
        seen: set[str] = set()
        unique: list[Game] = []
        for g in all_games:
            if g.id and g.id not in seen:
                seen.add(g.id)
                unique.append(g)

        # Merge final scores into schedule games
        for i, game in enumerate(unique):
            if game.status in ("final", "unofficial") and game.id in scores_by_id:
                scored = scores_by_id[game.id]
                game.scoreboard = scored.scoreboard
                game.has_overtime = scored.has_overtime
                game.has_shootout = scored.has_shootout

        today_games = [g for g in unique if _is_today(g)]
        yesterday_completed = [
            g for g in unique
            if _is_yesterday(g) and g.status in ("final", "unofficial")
        ]

        with _lock:
            _all_games = unique
            _game_divisions = game_divs
            _schedule_games = today_games
            _yesterday_finals = yesterday_completed
            _last_schedule_fetch = datetime.now(EASTERN).isoformat()

        logger.info(
            "Schedule refreshed: %d total, %d today",
            len(unique), len(today_games),
        )

        _update_poller(today_games)

    except Exception:
        logger.exception("Schedule refresh failed")


def _update_poller(today_games: list[Game]):
    """Start/restart LiveGamePoller for games that should be polled."""
    global _poller, _last_live_update

    poll_ids = [g.id for g in today_games if _should_poll(g)]

    if _poller:
        _poller.stop()
        _poller = None

    if not poll_ids:
        logger.info("No games to poll right now")
        return

    logger.info("Starting poller for %d game(s): %s", len(poll_ids), poll_ids)
    _poller = LiveGamePoller(
        client, game_ids=poll_ids, interval=LIVE_POLL_INTERVAL,
    )

    @_poller.on_update
    def on_update(game: Game):
        global _last_live_update
        data = serialize_game(game)
        with _lock:
            _live_data[game.id] = data
            _last_live_update = datetime.now(EASTERN).isoformat()
        logger.info(
            "Live: %s %d - %d %s [%s]",
            game.home.name,
            game.scoreboard.total.get("home", 0),
            game.scoreboard.total.get("visitor", 0),
            game.visitor.name,
            game.status,
        )

    @_poller.on_goal
    def on_goal(game: Game, event):
        logger.info(
            "GOAL! #%s %s (%s)",
            event.player_number, event.player_name, event.team_side,
        )

    @_poller.on_status_change
    def on_status(game: Game, old_status: str, new_status: str):
        logger.info(
            "Status: %s -> %s (%s vs %s)",
            old_status, new_status, game.home.name, game.visitor.name,
        )

    _poller.start_background()


def _background_loop():
    """Periodically refresh the schedule and re-evaluate which games to poll."""
    while True:
        _refresh_schedule()
        time.sleep(SCHEDULE_REFRESH)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/today")
def api_today():
    """Today's schedule with live scores merged in, plus yesterday's results."""
    with _lock:
        games = []
        seen_ids: set[str] = set()
        for game in _schedule_games:
            seen_ids.add(game.id)
            if game.id in _live_data:
                games.append(_live_data[game.id])
            else:
                games.append(serialize_game(game))

        # Include yesterday's completed games so results persist
        for game in _yesterday_finals:
            if game.id not in seen_ids:
                if game.id in _live_data:
                    games.append(_live_data[game.id])
                else:
                    games.append(serialize_game(game))

    return jsonify({
        "games": games,
        "count": len(games),
        "last_schedule_fetch": _last_schedule_fetch,
        "last_live_update": _last_live_update,
    })


@app.route("/api/live")
def api_live():
    """Current live game data only (for frequent polling)."""
    with _lock:
        return jsonify({
            "games": dict(_live_data),
            "last_update": _last_live_update,
        })


@app.route("/api/divisions")
def api_divisions():
    """List all divisions for this season."""
    return jsonify([{"id": d.id, "title": d.title} for d in _divisions])


@app.route("/api/scoreboard")
def api_scoreboard():
    """Compatibility endpoint for the embed widget.

    Returns the old-format response that embed-widget.html expects:
    {games: [{home_team, away_team, home_score, away_score, ...}], today_index}
    """
    days_back = request.args.get("days_back", 7, type=int)
    days_forward = request.args.get("days_forward", 7, type=int)

    now = datetime.now(EASTERN)
    today = now.date()
    cutoff_past = (now - timedelta(days=days_back)).date()
    cutoff_future = (now + timedelta(days=days_forward)).date()

    games = []
    with _lock:
        for game in _all_games:
            gdt = _parse_game_datetime(game.date, game.scheduled_time)
            if gdt is None:
                continue
            gd = gdt.date()
            if gd < cutoff_past or gd > cutoff_future:
                continue

            # Use live data if available
            if game.id in _live_data:
                live = _live_data[game.id]
                is_live = isLive(live.get("status", ""))
                status = "live" if is_live else ("completed" if live.get("status") in ("final", "unofficial") else "scheduled")
                games.append({
                    "id": game.id,
                    "date": live.get("date") or game.date,
                    "time": live.get("scheduled_time") or _format_game_time(game.scheduled_time),
                    "datetime": gdt.isoformat(),
                    "home_team": live["home"]["name"],
                    "away_team": live["visitor"]["name"],
                    "home_score": live["scoreboard"]["total"].get("home"),
                    "away_score": live["scoreboard"]["total"].get("visitor"),
                    "home_logo": live["home"].get("logo_url", ""),
                    "away_logo": live["visitor"].get("logo_url", ""),
                    "location": live.get("location", ""),
                    "division": live["home"].get("division", ""),
                    "status": status,
                    "game_url": live.get("gamesheet_url", ""),
                })
            else:
                is_final = game.status in ("final", "unofficial")
                total = game.scoreboard.total
                games.append({
                    "id": game.id,
                    "date": game.date,
                    "time": _format_game_time(game.scheduled_time),
                    "datetime": gdt.isoformat(),
                    "home_team": game.home.name,
                    "away_team": game.visitor.name,
                    "home_score": total.get("home") if is_final else None,
                    "away_score": total.get("visitor") if is_final else None,
                    "home_logo": _fix_logo(game.home.logo_url),
                    "away_logo": _fix_logo(game.visitor.logo_url),
                    "location": game.location,
                    "division": game.home.division or game.visitor.division,
                    "status": "completed" if is_final else "scheduled",
                    "game_url": f"https://gamesheetstats.com/seasons/{SEASON_ID}/games/{game.id}",
                })

    # Sort by datetime
    games.sort(key=lambda g: g.get("datetime", "9999"))

    # Find index of first game today or later
    today_index = 0
    for i, g in enumerate(games):
        gdt = _parse_game_datetime(g.get("date", ""))
        if gdt and gdt.date() >= today:
            today_index = i
            break

    return jsonify({
        "games": games,
        "count": len(games),
        "today_index": today_index,
        "fetched_at": _last_schedule_fetch,
    })


@app.route("/embed/scoreboard")
def embed_scoreboard():
    """Scoreboard widget for iframe embedding."""
    return render_template("embed_scoreboard.html")


@app.route("/brackets")
@app.route("/brackets/<group>")
def brackets(group="tournament"):
    """Bracket view for Flyers Cup divisions."""
    if group not in BRACKET_GROUPS:
        group = "tournament"
    group_cfg = BRACKET_GROUPS[group]
    return render_template("brackets.html", group=group, title=group_cfg["title"])


def _is_placeholder(name: str) -> bool:
    """Check if a team name is a placeholder (TBD, To be determined, etc.)."""
    if not name:
        return True
    nl = name.lower()
    return "to be determined" in nl or nl.startswith("tbd") or "winner" in nl


def _parse_label_seed(name: str) -> int | None:
    """Extract a seed number from a GameSheet label like 'TBD - #4 Seed'."""
    if not name:
        return None
    m = re.search(r"#(\d+)\s*seed", name, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _build_rounds(bracket_games: list[Game]) -> list[dict]:
    """Build round structure from sorted bracket games.

    Returns list of {"name": str, "games": [Game, ...]}.
    Championship is always the last game. Semifinals are the 2 games
    before it. Earlier games are grouped by date into additional rounds.
    """
    if not bracket_games:
        return []

    champ_game = bracket_games[-1]
    pre_champ = bracket_games[:-1]
    rounds: list[dict] = []

    if len(pre_champ) <= 2:
        if pre_champ:
            rounds.append({"name": "Semifinals", "games": list(pre_champ)})
    else:
        semis = pre_champ[-2:]
        earlier = pre_champ[:-2]

        earlier_by_date: OrderedDict[str, list] = OrderedDict()
        for g in earlier:
            d = g.date or "TBD"
            if d not in earlier_by_date:
                earlier_by_date[d] = []
            earlier_by_date[d].append(g)

        n_early_rounds = len(earlier_by_date)
        for i, (date, games) in enumerate(earlier_by_date.items()):
            dist = n_early_rounds - i + 1
            if dist < len(_ROUND_NAMES):
                name = _ROUND_NAMES[dist]
            else:
                name = f"Round {i + 1}"
            if name == "Quarterfinals" and len(games) <= 2:
                name = "Play-in"
            rounds.append({"name": name, "games": games})

        rounds.append({"name": "Semifinals", "games": list(semis)})

    rounds.append({"name": "Championship", "games": [champ_game]})
    return rounds


def _auto_seed_positions(rounds_raw: list[dict]) -> dict[tuple, int]:
    """Derive seed numbers by bracket position (game_id + side).

    Returns {(game_id, "home"|"visitor"): seed_number}.
    Works even when teams are TBD since seeds are keyed by position,
    not team name.

    Convention: home team is always the higher seed.
    - Bye teams (home in a later round, not fed by previous round) get top seeds.
    - First-round home teams get next seeds (by game order).
    - First-round away teams get remaining seeds (reverse game order).
    """
    if not rounds_raw:
        return {}

    bye_pos: list[tuple] = []   # (round_idx, game_idx, game_id)
    r0_home: list[tuple] = []   # (game_idx, game_id)
    r0_away: list[tuple] = []   # (game_idx, game_id)

    prev_round_games = 0
    for ri, rnd in enumerate(rounds_raw):
        games = rnd["games"]
        if ri == 0:
            for gi, game in enumerate(games):
                r0_home.append((gi, game.id))
                r0_away.append((gi, game.id))
            prev_round_games = len(games)
        else:
            # Each previous-round game produces 1 winner.
            # This round needs 2 * len(games) teams total.
            # Excess positions beyond available winners are byes (home slots).
            n_byes = 2 * len(games) - prev_round_games
            for gi in range(min(n_byes, len(games))):
                bye_pos.append((ri, gi, games[gi].id))
            prev_round_games = len(games)

    bye_pos.sort(key=lambda x: (x[0], x[1]))
    r0_home.sort(key=lambda x: x[0])
    r0_away.sort(key=lambda x: x[0])
    r0_away.reverse()

    pos_seeds: dict[tuple, int] = {}
    seed = 1
    for _, _, gid in bye_pos:
        pos_seeds[(gid, "home")] = seed
        seed += 1
    for _, gid in r0_home:
        pos_seeds[(gid, "home")] = seed
        seed += 1
    for _, gid in r0_away:
        pos_seeds[(gid, "visitor")] = seed
        seed += 1
    return pos_seeds


@app.route("/api/brackets/<group>")
def api_brackets(group="tournament"):
    """Bracket data organized by division for the Flyers Cup."""
    if group not in BRACKET_GROUPS:
        return jsonify({"error": "Unknown group", "valid": list(BRACKET_GROUPS.keys())}), 404

    group_cfg = BRACKET_GROUPS[group]
    brackets = []

    with _lock:
        playoff_games = [
            g for g in _all_games
            if g.game_type and "playoff" in g.game_type.lower()
        ]

        for cfg in group_cfg["brackets"]:
            div_id = cfg["division_id"]
            manual_seeds = cfg["seeds"]

            bracket_games = [
                g for g in playoff_games
                if g.id and _game_divisions.get(g.id) == div_id
            ]

            if not bracket_games:
                continue

            def sort_key(g):
                m = re.search(r"\d+$", g.game_number or "")
                return int(m.group()) if m else 0

            bracket_games.sort(key=sort_key)

            # Deduplicate placeholder games sharing the same game number
            # (e.g. Prep/Catholic has 3 PCC1 games for potential matchups).
            # Only collapse when TBD teams are involved; if all games with
            # the same number have real teams, they're all legitimate.
            # When 3+ games share a number, they're all placeholders — mark
            # them so enrich() blanks the team names.
            from collections import defaultdict as _ddict
            by_number: dict[str, list] = _ddict(list)
            for g in bracket_games:
                by_number[g.game_number or ""].append(g)
            deduped: list[Game] = []
            placeholder_ids: set[str] = set()
            for num, games in by_number.items():
                if len(games) <= 1:
                    deduped.extend(games)
                elif len(games) >= 3:
                    # 3+ games with same number = all placeholders
                    deduped.append(games[0])
                    placeholder_ids.add(games[0].id)
                else:
                    has_tbd = any(
                        _is_placeholder(g.home.name)
                        or _is_placeholder(g.visitor.name)
                        for g in games
                    )
                    if has_tbd:
                        best = max(games, key=lambda g: sum(
                            1 for t in (g.home, g.visitor)
                            if not _is_placeholder(t.name)
                        ))
                        deduped.append(best)
                    else:
                        deduped.extend(games)
            bracket_games = sorted(deduped, key=sort_key)

            # Build round structure from raw Game objects
            rounds_raw = _build_rounds(bracket_games)

            # Seed lookup: manual seeds use name, auto seeds use position.
            # For auto, also build a name map so advancing teams keep their seed.
            if manual_seeds:
                pos_seeds: dict[tuple, int] = {}
                name_seeds = dict(manual_seeds)
            else:
                pos_seeds = _auto_seed_positions(rounds_raw)
                name_seeds = {}
                for rnd in rounds_raw:
                    for game in rnd["games"]:
                        for side_key, team in [("home", game.home), ("visitor", game.visitor)]:
                            # Prefer label seed, fall back to position seed
                            s = _parse_label_seed(team.name) or pos_seeds.get((game.id, side_key))
                            if s and not _is_placeholder(team.name):
                                name_seeds[team.name] = s

            def enrich(game, _pos=pos_seeds, _names=name_seeds,
                       _ph=placeholder_ids):
                if game.id in _live_data:
                    data = _live_data[game.id].copy()
                    data["home"] = dict(data["home"])
                    data["visitor"] = dict(data["visitor"])
                else:
                    data = serialize_game(game)
                # For deduped placeholder games (3+ with same game number),
                # show "TBD" since the specific team names are meaningless.
                # All other games show the actual GameSheet label.
                if game.id in _ph:
                    for side in ("home", "visitor"):
                        data[side]["name"] = "TBD"
                        data[side]["logo_url"] = ""
                for side in ("home", "visitor"):
                    # Priority: label seed (#N Seed) > position seed > name seed
                    seed = _parse_label_seed(data[side]["name"])
                    if seed is None:
                        seed = _pos.get((game.id, side))
                    if seed is None:
                        seed = _names.get(data[side]["name"])
                    data[side]["seed"] = seed
                return data

            rounds = [{
                "name": r["name"],
                "games": [enrich(g) for g in r["games"]],
            } for r in rounds_raw]

            brackets.append({
                "name": cfg["name"],
                "key": cfg["key"],
                "rounds": rounds,
            })

    return jsonify({
        "brackets": brackets,
        "group": group,
        "title": group_cfg["title"],
        "last_schedule_fetch": _last_schedule_fetch,
        "last_live_update": _last_live_update,
    })


def isLive(status: str) -> bool:
    """Check if a status string indicates a live game."""
    if not status:
        return False
    s = status.lower()
    return "progress" in s or "period" in s or "intermission" in s or "overtime" in s


@app.route("/health")
def health():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now(EASTERN).isoformat(),
        "today_games": len(_schedule_games),
        "live_games": len(_live_data),
    })


# ---------------------------------------------------------------------------
# Startup — works with both `python app.py` and `gunicorn --preload`
# ---------------------------------------------------------------------------

_started = False


def start_background():
    """Start the background schedule/poller thread (once)."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_background_loop, daemon=True).start()
    logger.info("Background thread started")


# Tournament is over — no need to poll for live scores
# start_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
