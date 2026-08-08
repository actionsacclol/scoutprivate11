"""Static home-venue table for the weather-sensitive leagues (NFL + MLB).

Keyed by ESPN team displayName exactly as espn.py / injuries.py emit it
(note the Athletics are just "Athletics" — no city — since 2025).

Venues current for the 2026 season, verified against ESPN live data:
  * Athletics play at Sutter Health Park, West Sacramento (through ~2027).
  * Rays are back at Tropicana Field for 2026 (2025 was Steinbrenner Field
    while the hurricane-damaged roof was repaired) — ESPN lists their next
    home games at the Trop.
  * Commanders at Northwest Stadium (ex-FedEx Field), Astros at Daikin Park
    (ex-Minute Maid), Bills still open-air in Orchard Park.

roof values:
  "open"        — field fully exposed to weather
  "dome"        — fixed roof, weather never matters (SoFi's roof is fixed,
                  so it counts as a dome even with open sides)
  "retractable" — roof may be open or closed on game day
"""
from __future__ import annotations

from typing import Literal, NamedTuple

from kryptscout.core.schemas import Sport

Roof = Literal["open", "dome", "retractable"]


class Venue(NamedTuple):
    lat: float
    lon: float
    roof: Roof


NFL_STADIUMS: dict[str, Venue] = {
    "Arizona Cardinals": Venue(33.5276, -112.2626, "retractable"),  # State Farm
    "Atlanta Falcons": Venue(33.7554, -84.4008, "retractable"),  # Mercedes-Benz
    "Baltimore Ravens": Venue(39.2780, -76.6227, "open"),  # M&T Bank
    "Buffalo Bills": Venue(42.7738, -78.7870, "open"),  # Highmark, Orchard Park
    "Carolina Panthers": Venue(35.2258, -80.8528, "open"),  # Bank of America
    "Chicago Bears": Venue(41.8623, -87.6167, "open"),  # Soldier Field
    "Cincinnati Bengals": Venue(39.0954, -84.5160, "open"),  # Paycor
    "Cleveland Browns": Venue(41.5061, -81.6995, "open"),  # Huntington Bank Field
    "Dallas Cowboys": Venue(32.7473, -97.0945, "retractable"),  # AT&T
    "Denver Broncos": Venue(39.7439, -105.0201, "open"),  # Empower Field
    "Detroit Lions": Venue(42.3400, -83.0456, "dome"),  # Ford Field
    "Green Bay Packers": Venue(44.5013, -88.0622, "open"),  # Lambeau
    "Houston Texans": Venue(29.6847, -95.4107, "retractable"),  # NRG
    "Indianapolis Colts": Venue(39.7601, -86.1639, "retractable"),  # Lucas Oil
    "Jacksonville Jaguars": Venue(30.3239, -81.6373, "open"),  # EverBank
    "Kansas City Chiefs": Venue(39.0489, -94.4839, "open"),  # Arrowhead
    "Las Vegas Raiders": Venue(36.0909, -115.1833, "dome"),  # Allegiant
    "Los Angeles Chargers": Venue(33.9535, -118.3392, "dome"),  # SoFi (fixed roof)
    "Los Angeles Rams": Venue(33.9535, -118.3392, "dome"),  # SoFi (fixed roof)
    "Miami Dolphins": Venue(25.9580, -80.2389, "open"),  # Hard Rock (canopy only)
    "Minnesota Vikings": Venue(44.9738, -93.2577, "dome"),  # U.S. Bank
    "New England Patriots": Venue(42.0909, -71.2643, "open"),  # Gillette
    "New Orleans Saints": Venue(29.9511, -90.0812, "dome"),  # Caesars Superdome
    "New York Giants": Venue(40.8135, -74.0745, "open"),  # MetLife
    "New York Jets": Venue(40.8135, -74.0745, "open"),  # MetLife
    "Philadelphia Eagles": Venue(39.9008, -75.1675, "open"),  # Lincoln Financial
    "Pittsburgh Steelers": Venue(40.4468, -80.0158, "open"),  # Acrisure
    "San Francisco 49ers": Venue(37.4033, -121.9694, "open"),  # Levi's, Santa Clara
    "Seattle Seahawks": Venue(47.5952, -122.3316, "open"),  # Lumen Field
    "Tampa Bay Buccaneers": Venue(27.9759, -82.5033, "open"),  # Raymond James
    "Tennessee Titans": Venue(36.1665, -86.7713, "open"),  # Nissan (new dome opens 2027)
    "Washington Commanders": Venue(38.9077, -76.8645, "open"),  # Northwest Stadium
}

MLB_STADIUMS: dict[str, Venue] = {
    "Arizona Diamondbacks": Venue(33.4455, -112.0667, "retractable"),  # Chase Field
    "Athletics": Venue(38.5804, -121.5132, "open"),  # Sutter Health Park, W Sacramento
    "Atlanta Braves": Venue(33.8908, -84.4678, "open"),  # Truist Park
    "Baltimore Orioles": Venue(39.2839, -76.6217, "open"),  # Camden Yards
    "Boston Red Sox": Venue(42.3467, -71.0972, "open"),  # Fenway Park
    "Chicago Cubs": Venue(41.9484, -87.6553, "open"),  # Wrigley Field
    "Chicago White Sox": Venue(41.8299, -87.6338, "open"),  # Rate Field
    "Cincinnati Reds": Venue(39.0975, -84.5066, "open"),  # Great American Ball Park
    "Cleveland Guardians": Venue(41.4962, -81.6852, "open"),  # Progressive Field
    "Colorado Rockies": Venue(39.7559, -104.9942, "open"),  # Coors Field
    "Detroit Tigers": Venue(42.3390, -83.0485, "open"),  # Comerica Park
    "Houston Astros": Venue(29.7573, -95.3555, "retractable"),  # Daikin Park
    "Kansas City Royals": Venue(39.0517, -94.4803, "open"),  # Kauffman Stadium
    "Los Angeles Angels": Venue(33.8003, -117.8827, "open"),  # Angel Stadium
    "Los Angeles Dodgers": Venue(34.0739, -118.2400, "open"),  # Dodger Stadium
    "Miami Marlins": Venue(25.7781, -80.2196, "retractable"),  # loanDepot park
    "Milwaukee Brewers": Venue(43.0280, -87.9712, "retractable"),  # American Family
    "Minnesota Twins": Venue(44.9817, -93.2775, "open"),  # Target Field
    "New York Mets": Venue(40.7571, -73.8458, "open"),  # Citi Field
    "New York Yankees": Venue(40.8296, -73.9262, "open"),  # Yankee Stadium
    "Philadelphia Phillies": Venue(39.9061, -75.1665, "open"),  # Citizens Bank Park
    "Pittsburgh Pirates": Venue(40.4469, -80.0057, "open"),  # PNC Park
    "San Diego Padres": Venue(32.7076, -117.1570, "open"),  # Petco Park
    "San Francisco Giants": Venue(37.7786, -122.3893, "open"),  # Oracle Park
    "Seattle Mariners": Venue(47.5914, -122.3325, "retractable"),  # T-Mobile Park
    "St. Louis Cardinals": Venue(38.6226, -90.1928, "open"),  # Busch Stadium
    "Tampa Bay Rays": Venue(27.7683, -82.6534, "dome"),  # Tropicana Field
    "Texas Rangers": Venue(32.7473, -97.0817, "retractable"),  # Globe Life Field
    "Toronto Blue Jays": Venue(43.6414, -79.3894, "retractable"),  # Rogers Centre
    "Washington Nationals": Venue(38.8730, -77.0074, "open"),  # Nationals Park
}

_TABLES: dict[Sport, dict[str, Venue]] = {
    Sport.NFL: NFL_STADIUMS,
    Sport.MLB: MLB_STADIUMS,
}


def venue_for(sport: Sport, team: str) -> Venue | None:
    """Home venue for an ESPN displayName; None for unknown team/sport."""
    return _TABLES.get(sport, {}).get(team)
