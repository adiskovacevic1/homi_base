import os, json, datetime
import httpx

DIR = "/data/weather"
CACHE = os.path.join(DIR, "geocode.json")
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FC_URL = "https://api.open-meteo.com/v1/forecast"
UA = "Mozilla/5.0 (compatible; AssistantBot/1.0)"

CODES = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Light freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light rain showers", 81: "Rain showers", 82: "Violent rain showers",
    85: "Light snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
}

STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont",
    "va": "virginia", "wa": "washington", "wv": "west virginia", "wi": "wisconsin",
    "wy": "wyoming", "dc": "district of columbia",
    "usa": "united states", "us": "united states", "uk": "united kingdom",
}

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

CURRENT_VARS = ("temperature_2m,apparent_temperature,relative_humidity_2m,is_day,"
                "precipitation,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m")
DAILY_VARS = ("weather_code,temperature_2m_max,temperature_2m_min,"
              "precipitation_probability_max,precipitation_sum,wind_speed_10m_max,sunrise,sunset")


def _words(code):
    try:
        return CODES.get(int(code), f"code {int(code)}")
    except Exception:
        return "unknown"


def _dir(deg):
    if deg is None:
        return None
    return COMPASS[int((float(deg) % 360) / 22.5 + 0.5) % 16]


def _cache_read():
    try:
        with open(CACHE) as f:
            return json.load(f)
    except Exception:
        return {}


def _cache_write(data):
    try:
        os.makedirs(DIR, exist_ok=True)
        with open(CACHE, "w") as f:
            json.dump(dict(list(data.items())[-200:]), f)
    except Exception:
        pass  # cache is a nicety; /data may be read-only


def _norm(s):
    s = (s or "").strip().lower()
    return STATES.get(s, s)


def _pick(results, hints):
    """Choose the geocoding hit that best matches 'city, state/country' hints."""
    if not hints:
        return results[0], True
    wanted = [_norm(h) for h in hints if h.strip()]
    best, best_score = None, 0
    for r in results:
        fields = {_norm(r.get(k)) for k in
                  ("admin1", "admin2", "admin3", "country", "country_code")}
        fields.discard("")
        score = sum(1 for w in wanted if w in fields)
        if score > best_score:
            best, best_score = r, score
    if best is not None and best_score >= len(wanted):
        return best, True
    return (best or results[0]), False


def _geocode(place, client):
    key = place.strip().lower()
    cache = _cache_read()
    hit = cache.get(key)
    if hit:
        return hit
    parts = [p.strip() for p in place.split(",")]
    name, hints = parts[0], parts[1:]
    if not name:
        raise ValueError("place must contain a city name, e.g. 'Chicago' or 'Paris, FR'")
    r = client.get(GEO_URL, params={"name": name, "count": 10, "language": "en", "format": "json"})
    r.raise_for_status()
    results = (r.json() or {}).get("results") or []
    if not results and hints:  # e.g. "Springfield, Illinois" where the whole string was passed
        r = client.get(GEO_URL, params={"name": place, "count": 10, "language": "en", "format": "json"})
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
        hints = []
    if not results:
        raise ValueError(f"no place found for {place!r} - try 'City, State' or 'City, Country'")
    g, exact = _pick(results, hints)
    loc = {
        "name": g.get("name"),
        "admin1": g.get("admin1"),
        "country": g.get("country"),
        "country_code": g.get("country_code"),
        "latitude": g.get("latitude"),
        "longitude": g.get("longitude"),
        "hint_matched": exact,
    }
    cache[key] = loc
    _cache_write(cache)
    return loc


def _hhmm(iso):
    try:
        return datetime.datetime.fromisoformat(iso).strftime("%H:%M")
    except Exception:
        return iso


def run(place, days=3, units="imperial"):
    if not place or not str(place).strip():
        raise ValueError("place required, e.g. 'Chicago' or 'Chicago, IL'")
    place = str(place).strip()
    units = (units or "imperial").strip().lower()
    if units not in ("imperial", "metric"):
        raise ValueError("units must be 'imperial' or 'metric'")
    try:
        days = int(days if days is not None else 3)
    except (TypeError, ValueError):
        raise ValueError("days must be an integer between 1 and 7")
    if not 1 <= days <= 7:
        raise ValueError("days must be between 1 and 7")

    imperial = units == "imperial"
    u = {"temperature": "F" if imperial else "C",
         "wind": "mph" if imperial else "km/h",
         "precipitation": "in" if imperial else "mm"}

    with httpx.Client(timeout=15.0, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept": "application/json"}) as c:
        loc = _geocode(place, c)
        r = c.get(FC_URL, params={
            "latitude": loc["latitude"], "longitude": loc["longitude"],
            "current": CURRENT_VARS, "daily": DAILY_VARS,
            "timezone": "auto", "forecast_days": days,
            "temperature_unit": "fahrenheit" if imperial else "celsius",
            "wind_speed_unit": "mph" if imperial else "kmh",
            "precipitation_unit": "inch" if imperial else "mm",
        })
        r.raise_for_status()
        fc = r.json()

    cur = fc.get("current") or {}
    d = fc.get("daily") or {}
    dates = d.get("time") or []
    daily = []
    for i, day in enumerate(dates):
        def at(k):
            v = d.get(k) or []
            return v[i] if i < len(v) else None
        try:
            label = datetime.date.fromisoformat(day).strftime("%a")
        except Exception:
            label = day
        daily.append({
            "date": day,
            "day": label,
            "condition": _words(at("weather_code")),
            "high": at("temperature_2m_max"),
            "low": at("temperature_2m_min"),
            "precip_chance": at("precipitation_probability_max"),
            "precip": at("precipitation_sum"),
            "wind_max": at("wind_speed_10m_max"),
            "sunrise": _hhmm(at("sunrise")),
            "sunset": _hhmm(at("sunset")),
        })

    label = ", ".join(x for x in (loc["name"], loc.get("admin1"), loc.get("country")) if x)
    condition = _words(cur.get("weather_code"))
    out = {
        "place": label,
        "location": {k: loc[k] for k in ("name", "admin1", "country", "latitude", "longitude")},
        "timezone": fc.get("timezone"),
        "units": u,
        "observed_at": cur.get("time"),
        "current": {
            "temp": cur.get("temperature_2m"),
            "feels_like": cur.get("apparent_temperature"),
            "humidity": cur.get("relative_humidity_2m"),
            "wind": cur.get("wind_speed_10m"),
            "wind_gust": cur.get("wind_gusts_10m"),
            "wind_dir": _dir(cur.get("wind_direction_10m")),
            "precip": cur.get("precipitation"),
            "condition": condition,
            "is_day": bool(cur.get("is_day")),
        },
        "daily": daily,
    }
    if not loc.get("hint_matched"):
        out["note"] = f"could not match the region in {place!r}; used the closest match"
    if daily:
        t = daily[0]
        out["summary"] = (f"{label}: {out['current']['temp']}°{u['temperature']}, {condition}. "
                          f"Today {t['high']}/{t['low']}°{u['temperature']}, "
                          f"{t['precip_chance']}% precip.")
    return out
