"""A sport's game-day run: fetch the schedule, check each outdoor game's weather, pick
the messy ones, and post them as a thread."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

import httpx

from messy_weather_sports_bot import state
from messy_weather_sports_bot.alerts import WeatherAlert, get_active_alerts
from messy_weather_sports_bot.espn import EspnScoreboard
from messy_weather_sports_bot.formatting import build_post_texts, format_kickoff
from messy_weather_sports_bot.messiness import GameWeather, evaluate_game, sort_by_messiness
from messy_weather_sports_bot.poster import POSTERS
from messy_weather_sports_bot.poster.base import PartialThreadError, SocialMediaPoster
from messy_weather_sports_bot.poster.console import ConsolePoster
from messy_weather_sports_bot.schedule import Game, skip_reason, todays_game_day
from messy_weather_sports_bot.sport import Sport
from messy_weather_sports_bot.weather import USER_AGENT, WeatherReport, get_forecast

logger = logging.getLogger(__name__)

# So cron wrappers and health checks can tell "nothing posted" from "posted, but
# degraded" from a clean run.
EXIT_OK = 0
EXIT_NOTHING_POSTED = 1
EXIT_PARTIAL = 2


def build_posters(
    platform_names: list[str], dry_run: bool, env_prefix: str = ""
) -> list[SocialMediaPoster]:
    """The posters for `platform_names`, each built from environment variables carrying
    `env_prefix` (a dry run prints instead, and needs no credentials)."""
    if dry_run:
        return [ConsolePoster()]

    posters = []
    for name in platform_names:
        try:
            poster_cls = POSTERS[name]
        except KeyError:
            known = sorted(POSTERS)
            raise SystemExit(f"Unknown platform: {name!r}. Known platforms: {known}") from None
        posters.append(poster_cls.from_env(env_prefix))
    return posters


def _weather_detail(weather: WeatherReport) -> str:
    parts = [weather.short_forecast]
    if weather.temperature_f is not None:
        parts.append(f"{weather.temperature_f}°F")
    if weather.wind_speed_mph > 0:
        parts.append(f"{weather.wind_speed_mph:g}mph")
    return ", ".join(parts)


@dataclass
class _Evaluation:
    evaluated: list[GameWeather] = field(default_factory=list)
    outdoor_count: int = 0
    games_missing: int = 0
    """Outdoor games whose forecast couldn't be fetched - one of them might have been
    the messy one, so a run that skipped any isn't a clean one."""

    @property
    def messy(self) -> list[GameWeather]:
        return [gw for gw in self.evaluated if gw.is_messy]


@dataclass
class _Publication:
    platforms_posted: list[str] = field(default_factory=list)
    platforms_failed: int = 0
    platforms_degraded: int = 0
    thread_root: str | None = None


def _log_run_summary(
    games_found: int | None,
    evaluation: _Evaluation,
    platforms_posted: list[str],
    thread_root: str | None,
) -> None:
    """`games_found` is None when the schedule couldn't be fetched at all (pass an empty
    `_Evaluation`) - every other exit path from `run()` calls this, so the healthcheck
    completion body always carries a summary line."""
    logger.info(
        "Run summary: %s game(s) found, %d outdoor, %d evaluated, %d messy, platforms: %s%s",
        "unavailable" if games_found is None else games_found,
        evaluation.outdoor_count,
        len(evaluation.evaluated),
        len(evaluation.messy),
        ", ".join(platforms_posted) or "none",
        f", thread: {thread_root}" if thread_root else "",
    )


def _record_state(
    day_state: state.DayState | None, platform: str, refs: list[state.PostRef], *, completed: bool
) -> None:
    """Persist `refs` for `platform`, without letting a state-write failure (a full
    disk, a read-only directory) change the posting outcome or escape `run()` -
    `refs` already published successfully regardless of whether this write does."""
    if day_state is None:
        return
    try:
        day_state.record(platform, refs, completed=completed)
    except OSError as exc:
        logger.warning("Could not save post state for %s: %s", platform, exc)


class GameDayPipeline:
    def __init__(self, sport: Sport) -> None:
        self.sport = sport

    def run(
        self,
        platform_names: list[str],
        dry_run: bool,
        force: bool = False,
        date: dt.date | None = None,
    ) -> int:
        sport = self.sport
        today = todays_game_day(sport)
        date = date or today
        if date != today and not dry_run:
            logger.warning("Previewing %s, not today; forcing --dry-run.", date.isoformat())
            dry_run = True
        try:
            games = EspnScoreboard(sport).fetch(date)
        except (httpx.HTTPError, ValueError) as exc:
            logger.error(
                "Could not fetch the %s schedule for %s: %s", sport.name, date.isoformat(), exc
            )
            _log_run_summary(None, _Evaluation(), [], None)
            return EXIT_NOTHING_POSTED

        evaluation = self._evaluate(games)
        messy = evaluation.messy

        if evaluation.outdoor_count == 0:
            logger.info("No outdoor %s games on %s; nothing to post.", sport.name, date.isoformat())
            _log_run_summary(len(games), evaluation, [], None)
            return EXIT_OK

        if not evaluation.evaluated:
            logger.error(
                "Forecast unavailable for every outdoor game on %s; nothing to post.",
                date.isoformat(),
            )
            _log_run_summary(len(games), evaluation, [], None)
            return EXIT_NOTHING_POSTED

        if not messy:
            logger.info("No messy-weather games on %s; nothing to post.", date.isoformat())
            _log_run_summary(len(games), evaluation, [], None)
            # A game whose forecast couldn't be fetched might have been the messy one.
            return EXIT_PARTIAL if evaluation.games_missing else EXIT_OK

        ranked = sort_by_messiness(messy)
        post_texts = build_post_texts(ranked, date, sport)
        posters = build_posters(platform_names, dry_run, sport.env_prefix)

        # A dry run must never touch the state file - only construct a DayState (which
        # reads it) when actually posting for real.
        day_state = (
            None
            if dry_run
            else state.DayState.load(state.state_file_path(date, sport_key=sport.state_key))
        )
        publication = self._publish(posters, post_texts, day_state, force)

        if publication.platforms_failed == len(posters):
            exit_code = EXIT_NOTHING_POSTED
        elif (
            evaluation.games_missing
            or publication.platforms_failed
            or publication.platforms_degraded
        ):
            exit_code = EXIT_PARTIAL
        else:
            exit_code = EXIT_OK

        _log_run_summary(
            len(games), evaluation, publication.platforms_posted, publication.thread_root
        )
        return exit_code

    def _evaluate(self, games: list[Game]) -> _Evaluation:
        """Look up the weather for every game that applies it to, and score it."""
        sport = self.sport
        evaluation = _Evaluation()
        # One client for the whole slate, so the NWS connection is reused across games.
        with httpx.Client(timeout=10.0, headers={"User-Agent": USER_AGENT}) as nws_client:
            for game in games:
                matchup = f"{game.away_team} @ {game.home_team}"
                reason = skip_reason(game)
                if reason is not None:
                    logger.info("%s — skipped: %s", matchup, reason)
                    continue

                evaluation.outdoor_count += 1
                assert game.stadium is not None  # guaranteed by skip_reason() returning None
                alerts: list[WeatherAlert] = []
                try:
                    forecasts = get_forecast(
                        game.stadium.latitude,
                        game.stadium.longitude,
                        game.kickoff,
                        nws_client,
                        game_duration=sport.game_duration,
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    evaluation.games_missing += 1
                    logger.info("%s — skipped: forecast unavailable (%s)", matchup, exc)
                    continue

                try:
                    alerts = get_active_alerts(
                        game.stadium.latitude,
                        game.stadium.longitude,
                        game.kickoff,
                        nws_client,
                        game_duration=sport.game_duration,
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    # An alert lookup failing shouldn't drop the game - the line just shows
                    # no alert and scoring falls back to the forecast alone.
                    logger.info("%s — no alert data (%s)", matchup, exc)

                gw = evaluate_game(game, forecasts, alerts)
                evaluation.evaluated.append(gw)
                period_word = "period" if len(forecasts) == 1 else "periods"
                logger.info(
                    "%s %s — %s: score %.1f (%s) across %d hourly %s",
                    matchup,
                    format_kickoff(game.kickoff, sport),
                    "included" if gw.is_messy else "skipped: not messy",
                    gw.score,
                    _weather_detail(gw.weather),
                    len(forecasts),
                    period_word,
                )
        return evaluation

    def _publish(
        self,
        posters: list[SocialMediaPoster],
        post_texts: list[str],
        day_state: state.DayState | None,
        force: bool,
    ) -> _Publication:
        """Post the thread to each poster, resuming (or skipping) per its saved state,
        and isolating one platform's outage from the rest."""
        publication = _Publication()
        for poster in posters:
            platform = type(poster).__name__
            resume: list[state.PostRef] | None = None
            if day_state is not None and not force:
                platform_state = day_state.for_platform(platform)
                if platform_state.completed:
                    logger.info(
                        "%s already posted today's thread; skipping (use --force to repost).",
                        platform,
                    )
                    publication.platforms_posted.append(platform)
                    if platform_state.posts and publication.thread_root is None:
                        publication.thread_root = platform_state.posts[0].id
                    continue
                resume = platform_state.posts or None

            try:
                refs = poster.post_thread(post_texts, resume=resume)
                publication.platforms_posted.append(platform)
                _record_state(day_state, platform, refs, completed=True)
                if refs and publication.thread_root is None:
                    publication.thread_root = refs[0].id
            except PartialThreadError as exc:
                # Some posts in the thread went out before it failed - not "nothing
                # posted", but still worth flagging as degraded.
                publication.platforms_degraded += 1
                _record_state(day_state, platform, exc.posted, completed=False)
                logger.warning(
                    "Partially posted to %s (%d/%d posts before failing): %s",
                    platform,
                    len(exc.posted),
                    len(post_texts),
                    exc,
                )
            except Exception as exc:  # isolate one platform's outage from the rest
                publication.platforms_failed += 1
                logger.error("Failed to post to %s: %s", platform, exc)
        return publication
