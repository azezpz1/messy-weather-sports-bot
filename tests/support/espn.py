"""Builders for ESPN scoreboard payloads, shared by the unit tests."""

from messy_weather_sports_bot.stadiums import stadium_for_team

DEFAULT_KICKOFF = "2026-01-18T18:00Z"


def espn_event(
    home: str,
    away: str,
    venue_name: str | None = None,
    *,
    kickoff: str = DEFAULT_KICKOFF,
    venue_id: str = "",
    indoor: bool = False,
    country: str = "USA",
) -> dict:
    """One scoreboard event. `venue_name` defaults to the home team's stadium's name;
    `venue_id` and `country` are left out of the venue when empty, as ESPN sometimes
    omits them."""
    name = stadium_for_team(home).name if venue_name is None else venue_name
    venue: dict = {"fullName": name, "indoor": indoor}
    if venue_id:
        venue["id"] = venue_id
    if country:
        venue["address"] = {"country": country}
    return {
        "date": kickoff,
        "competitions": [
            {
                "date": kickoff,
                "venue": venue,
                "competitors": [
                    {"homeAway": "home", "team": {"abbreviation": home}},
                    {"homeAway": "away", "team": {"abbreviation": away}},
                ],
            }
        ],
    }
