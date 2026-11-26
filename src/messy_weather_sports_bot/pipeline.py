"""A sport's game-day run: fetch the schedule, check each outdoor game's weather, pick
the messy ones, and post them as a thread."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

import httpx

from messy_weather_sports_bot import state
from messy_weather_sports_bot.alerts import get_active_alerts
from messy_weather_sports_bot.collection import record_collection
from messy_weather_sports_bot.espn import EspnScoreboard, VenueTableError
from messy_weather_sports_bot.evaluation import Evaluation, evaluate_games, rankings_missing
from messy_weather_sports_bot.formatting import build_post_texts
from messy_weather_sports_bot.messiness import sort_by_messiness
from messy_weather_sports_bot.poster import POSTERS
from messy_weather_sports_bot.poster.base import PartialThreadError, SocialMediaPoster
from messy_weather_sports_bot.poster.console import ConsolePoster
from messy_weather_sports_bot.schedule import todays_game_day
from messy_weather_sports_bot.sport import Sport
from messy_weather_sports_bot.stored_publication import load_stored_evaluation
from messy_weather_sports_bot.weather import get_forecast

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


@dataclass
class _Publication:
    platforms_posted: list[str] = field(default_factory=list)
    platforms_failed: int = 0
    platforms_degraded: int = 0
    thread_root: str | None = None


def _log_run_summary(
    games_found: int | None,
    evaluation: Evaluation,
    platforms_posted: list[str],
    thread_root: str | None,
) -> None:
    """`games_found` is None when the schedule couldn't be fetched at all (pass an empty
    `Evaluation`) - every other exit path from `run()` calls this, so the healthcheck
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
    day_state: state.DayState | None,
    platform: str,
    refs: list[state.PostRef],
    *,
    completed: bool,
    posted_texts: list[str] | None = None,
) -> None:
    """Persist `refs` for `platform`, without letting a state-write failure (a full
    disk, a read-only directory) change the posting outcome or escape `run()` -
    `refs` already published successfully regardless of whether this write does."""
    if day_state is None:
        return
    try:
        day_state.record(platform, refs, completed=completed, posted_texts=posted_texts)
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
        *,
        live: bool = False,
    ) -> int:
        sport = self.sport
        today = todays_game_day(sport)
        date = date or today
        if date != today and not dry_run:
            logger.warning("Previewing %s, not today; forcing --dry-run.", date.isoformat())
            dry_run = True
        started_at = dt.datetime.now(dt.UTC)
        stored = None if live else load_stored_evaluation(sport, date, started_at)
        if stored is not None:
            games_found, evaluation = stored
            incomplete = False
        else:
            fetched = self._evaluate_live(date, started_at, dry_run)
            if fetched is None:
                _log_run_summary(None, Evaluation(), [], None)
                return EXIT_NOTHING_POSTED
            games_found, evaluation, incomplete = fetched

        messy = evaluation.messy
        # A dry run must never touch the state file - only construct a DayState (which
        # reads it) when actually posting for real.
        day_state = (
            None
            if dry_run
            else state.DayState.load(state.state_file_path(date, sport_key=sport.state_key))
        )

        unfinished = (
            not messy and not force and day_state is not None and day_state.has_partial_thread
        )
        if unfinished:
            logger.warning("A partial thread still needs attention for %s.", date)

        if evaluation.outdoor_count == 0:
            if incomplete:
                logger.warning("Schedule or venue data incomplete for %s; nothing to post.", date)
            else:
                logger.info("No outdoor %s games on %s; nothing to post.", sport.name, date)
            _log_run_summary(games_found, evaluation, [], None)
            return EXIT_PARTIAL if incomplete or unfinished else EXIT_OK

        if not evaluation.evaluated:
            logger.error(
                "Forecast unavailable for every outdoor game on %s; nothing to post.",
                date.isoformat(),
            )
            _log_run_summary(games_found, evaluation, [], None)
            return EXIT_NOTHING_POSTED

        if not messy:
            logger.info("No messy-weather games on %s; nothing to post.", date.isoformat())
            _log_run_summary(games_found, evaluation, [], None)
            # A game whose forecast couldn't be fetched might have been the messy one.
            return EXIT_PARTIAL if incomplete or unfinished else EXIT_OK

        ranked = sort_by_messiness(messy)
        post_texts = build_post_texts(ranked, date, sport)
        posters = build_posters(platform_names, dry_run, sport.env_prefix)

        publication = self._publish(posters, post_texts, day_state, force)

        if publication.platforms_failed == len(posters):
            exit_code = EXIT_NOTHING_POSTED
        elif incomplete or publication.platforms_failed or publication.platforms_degraded:
            exit_code = EXIT_PARTIAL
        else:
            exit_code = EXIT_OK

        _log_run_summary(
            games_found, evaluation, publication.platforms_posted, publication.thread_root
        )
        return exit_code

    def _evaluate_live(
        self, date: dt.date, started_at: dt.datetime, dry_run: bool
    ) -> tuple[int, Evaluation, bool] | None:
        sport = self.sport
        try:
            games = EspnScoreboard(sport, verify_slate=True).fetch(date)
        except VenueTableError as exc:
            if not dry_run:
                record_collection(
                    sport.slug,
                    date,
                    started_at,
                    "venue_table_unavailable",
                    None,
                    error_type=type(exc).__name__,
                )
            logger.error("Cannot run: %s", exc)
            return None
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            if not dry_run:
                record_collection(
                    sport.slug,
                    date,
                    started_at,
                    "schedule_unavailable",
                    None,
                    error_type=type(exc).__name__,
                )
            logger.error(
                "Could not fetch the %s schedule for %s (%s).",
                sport.name,
                date.isoformat(),
                type(exc).__name__,
            )
            return None

        missing_rankings = rankings_missing(sport, games)
        evaluation = evaluate_games(sport, games, get_forecast, get_active_alerts)
        incomplete = missing_rankings or evaluation.incomplete
        if not dry_run:
            record_collection(
                sport.slug,
                date,
                started_at,
                "partial" if incomplete else "complete",
                len(games),
                evaluation.observations,
                rankings_unavailable=missing_rankings,
            )
        return len(games), evaluation, incomplete

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
                posted_texts = platform_state.posted_texts
                if (
                    resume
                    and posted_texts is not None
                    and post_texts[: len(resume)] != posted_texts
                ):
                    logger.warning(
                        "%s has a partial thread whose posted text differs from the current "
                        "recommendations. Skipping replies to that thread.",
                        platform,
                    )
                    publication.platforms_degraded += 1
                    continue

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
                _record_state(
                    day_state,
                    platform,
                    exc.posted,
                    completed=False,
                    posted_texts=post_texts[: len(exc.posted)],
                )
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
