"""
Resolve freeform location text to the nearest major airport IATA code.
No external dependencies — static lookup with fuzzy matching.
"""

import re

# --------------------------------------------------------------------------- #
# Airport database: code → (name, city, state/country, [aliases])
# --------------------------------------------------------------------------- #

AIRPORTS = {
    # ----- Major US airports -----
    "ATL": ("Hartsfield-Jackson Atlanta International", "Atlanta", "GA", ["atlanta"]),
    "LAX": ("Los Angeles International", "Los Angeles", "CA", ["los angeles", "la", "hollywood", "santa monica", "beverly hills", "inglewood", "culver city", "westwood", "venice beach"]),
    "ORD": ("O'Hare International", "Chicago", "IL", ["chicago", "chi-town", "evanston", "oak park"]),
    "DFW": ("Dallas/Fort Worth International", "Dallas", "TX", ["dallas", "fort worth", "dfw", "arlington tx", "irving", "plano"]),
    "DEN": ("Denver International", "Denver", "CO", ["denver", "aurora co", "boulder", "lakewood co"]),
    "JFK": ("John F. Kennedy International", "New York", "NY", ["new york", "nyc", "manhattan", "brooklyn", "queens", "bronx", "jamaica", "long island city", "harlem", "new york city"]),
    "SFO": ("San Francisco International", "San Francisco", "CA", ["san francisco", "sf", "san fran", "daly city", "south san francisco"]),
    "SEA": ("Seattle-Tacoma International", "Seattle", "WA", ["seattle", "tacoma", "seatac", "bellevue wa", "redmond wa", "kirkland wa"]),
    "LAS": ("Harry Reid International", "Las Vegas", "NV", ["las vegas", "vegas", "the strip", "henderson nv", "north las vegas"]),
    "MCO": ("Orlando International", "Orlando", "FL", ["orlando", "kissimmee", "disney world", "walt disney world", "universal studios fl"]),
    "CLT": ("Charlotte Douglas International", "Charlotte", "NC", ["charlotte"]),
    "MIA": ("Miami International", "Miami", "FL", ["miami", "south beach", "miami beach", "coral gables", "coconut grove", "wynwood", "brickell"]),
    "PHX": ("Phoenix Sky Harbor International", "Phoenix", "AZ", ["phoenix", "scottsdale", "tempe", "mesa az", "chandler az", "glendale az", "gilbert az"]),
    "EWR": ("Newark Liberty International", "Newark", "NJ", ["newark", "jersey city", "hoboken", "new jersey north"]),
    "IAH": ("George Bush Intercontinental", "Houston", "TX", ["houston", "the woodlands tx", "sugar land", "katy tx"]),
    "MSP": ("Minneapolis-Saint Paul International", "Minneapolis", "MN", ["minneapolis", "saint paul", "st paul", "twin cities", "bloomington mn"]),
    "BOS": ("Boston Logan International", "Boston", "MA", ["boston", "cambridge ma", "somerville ma", "brookline ma"]),
    "DTW": ("Detroit Metropolitan", "Detroit", "MI", ["detroit", "dearborn", "ann arbor"]),
    "FLL": ("Fort Lauderdale-Hollywood International", "Fort Lauderdale", "FL", ["fort lauderdale", "ft lauderdale", "hollywood fl", "pompano beach"]),
    "PHL": ("Philadelphia International", "Philadelphia", "PA", ["philadelphia", "philly"]),
    "LGA": ("LaGuardia", "New York", "NY", ["laguardia"]),
    "BWI": ("Baltimore/Washington International", "Baltimore", "MD", ["baltimore", "bwi"]),
    "IAD": ("Washington Dulles International", "Washington", "VA", ["dulles", "reston", "ashburn", "loudoun"]),
    "DCA": ("Ronald Reagan Washington National", "Washington", "DC", ["washington dc", "washington d.c.", "dc", "arlington va", "alexandria va", "capitol hill"]),
    "SLC": ("Salt Lake City International", "Salt Lake City", "UT", ["salt lake city", "salt lake", "slc", "park city", "provo"]),
    "SAN": ("San Diego International", "San Diego", "CA", ["san diego"]),
    "MDW": ("Chicago Midway International", "Chicago", "IL", ["midway"]),
    "HNL": ("Daniel K. Inouye International", "Honolulu", "HI", ["honolulu", "waikiki", "oahu", "hawaii"]),
    "TPA": ("Tampa International", "Tampa", "FL", ["tampa", "st petersburg fl", "clearwater"]),
    "PDX": ("Portland International", "Portland", "OR", ["portland or", "portland oregon"]),
    "AUS": ("Austin-Bergstrom International", "Austin", "TX", ["austin tx", "austin texas"]),
    "STL": ("St. Louis Lambert International", "St. Louis", "MO", ["st louis", "saint louis"]),
    "BNA": ("Nashville International", "Nashville", "TN", ["nashville", "music city"]),
    "RDU": ("Raleigh-Durham International", "Raleigh", "NC", ["raleigh", "durham", "research triangle", "chapel hill"]),
    "MCI": ("Kansas City International", "Kansas City", "MO", ["kansas city"]),
    "SMF": ("Sacramento International", "Sacramento", "CA", ["sacramento"]),
    "SJC": ("San Jose International", "San Jose", "CA", ["san jose ca", "san jose california", "silicon valley", "cupertino", "sunnyvale", "mountain view", "palo alto", "santa clara"]),
    "OAK": ("Oakland International", "Oakland", "CA", ["oakland"]),
    "RSW": ("Southwest Florida International", "Fort Myers", "FL", ["fort myers", "ft myers", "cape coral", "naples fl", "sanibel"]),
    "CLE": ("Cleveland Hopkins International", "Cleveland", "OH", ["cleveland"]),
    "PIT": ("Pittsburgh International", "Pittsburgh", "PA", ["pittsburgh"]),
    "IND": ("Indianapolis International", "Indianapolis", "IN", ["indianapolis", "indy"]),
    "CVG": ("Cincinnati/Northern Kentucky International", "Cincinnati", "OH", ["cincinnati"]),
    "CMH": ("John Glenn Columbus International", "Columbus", "OH", ["columbus oh", "columbus ohio"]),
    "MKE": ("Milwaukee Mitchell International", "Milwaukee", "WI", ["milwaukee"]),
    "SAT": ("San Antonio International", "San Antonio", "TX", ["san antonio"]),
    "JAX": ("Jacksonville International", "Jacksonville", "FL", ["jacksonville fl"]),
    "OKC": ("Will Rogers World", "Oklahoma City", "OK", ["oklahoma city"]),
    "RNO": ("Reno-Tahoe International", "Reno", "NV", ["reno", "lake tahoe", "tahoe"]),
    "ABQ": ("Albuquerque International Sunport", "Albuquerque", "NM", ["albuquerque", "santa fe"]),
    "SNA": ("John Wayne Airport", "Santa Ana", "CA", ["orange county ca", "irvine", "anaheim", "costa mesa", "newport beach"]),
    "BUR": ("Hollywood Burbank Airport", "Burbank", "CA", ["burbank", "glendale ca", "pasadena"]),
    "MEM": ("Memphis International", "Memphis", "TN", ["memphis"]),
    "MSY": ("Louis Armstrong New Orleans International", "New Orleans", "LA", ["new orleans", "nola", "french quarter", "bourbon street"]),
    "PBI": ("Palm Beach International", "West Palm Beach", "FL", ["west palm beach", "palm beach", "boca raton", "delray beach"]),
    "RIC": ("Richmond International", "Richmond", "VA", ["richmond va"]),
    "BDL": ("Bradley International", "Hartford", "CT", ["hartford", "springfield ma"]),
    "BUF": ("Buffalo Niagara International", "Buffalo", "NY", ["buffalo", "niagara falls"]),
    "ANC": ("Ted Stevens Anchorage International", "Anchorage", "AK", ["anchorage", "alaska"]),
    "OGG": ("Kahului Airport", "Kahului", "HI", ["maui", "kahului"]),
    "KOA": ("Ellison Onizuka Kona International", "Kona", "HI", ["kona", "big island hawaii"]),
    "LIH": ("Lihue Airport", "Lihue", "HI", ["kauai", "lihue"]),
    "DSM": ("Des Moines International", "Des Moines", "IA", ["des moines"]),
    "PVD": ("T.F. Green International", "Providence", "RI", ["providence"]),
    "ORF": ("Norfolk International", "Norfolk", "VA", ["norfolk", "virginia beach"]),
    "SDF": ("Louisville Muhammad Ali International", "Louisville", "KY", ["louisville"]),
    "TUL": ("Tulsa International", "Tulsa", "OK", ["tulsa"]),
    "ELP": ("El Paso International", "El Paso", "TX", ["el paso"]),
    "BOI": ("Boise Airport", "Boise", "ID", ["boise"]),
    "BHM": ("Birmingham-Shuttlesworth International", "Birmingham", "AL", ["birmingham al"]),
    "CHS": ("Charleston International", "Charleston", "SC", ["charleston sc"]),
    "SAV": ("Savannah/Hilton Head International", "Savannah", "GA", ["savannah", "hilton head"]),
    "GRR": ("Gerald R. Ford International", "Grand Rapids", "MI", ["grand rapids"]),
    "PWM": ("Portland International Jetport", "Portland", "ME", ["portland me", "portland maine"]),
    "JAC": ("Jackson Hole Airport", "Jackson Hole", "WY", ["jackson hole", "teton"]),
    "PSP": ("Palm Springs International", "Palm Springs", "CA", ["palm springs", "coachella", "coachella valley"]),
    "MSN": ("Dane County Regional", "Madison", "WI", ["madison wi"]),
    "HSV": ("Huntsville International", "Huntsville", "AL", ["huntsville"]),
    "DAL": ("Dallas Love Field", "Dallas", "TX", ["love field"]),
    "HOU": ("William P. Hobby", "Houston", "TX", ["hobby"]),
    "MYR": ("Myrtle Beach International", "Myrtle Beach", "SC", ["myrtle beach"]),
    "SRQ": ("Sarasota-Bradenton International", "Sarasota", "FL", ["sarasota", "bradenton"]),
    "AVL": ("Asheville Regional", "Asheville", "NC", ["asheville"]),
    "MTJ": ("Montrose Regional", "Montrose", "CO", ["telluride", "montrose"]),
    "EGE": ("Eagle County Regional", "Eagle", "CO", ["vail", "beaver creek"]),
    "HDN": ("Yampa Valley Regional", "Hayden", "CO", ["steamboat springs"]),
    "ASE": ("Aspen/Pitkin County", "Aspen", "CO", ["aspen", "snowmass"]),

    # ----- Common international airports -----
    "CUN": ("Cancún International", "Cancún", "Mexico", ["cancun", "cancún", "riviera maya", "playa del carmen", "tulum"]),
    "SJD": ("Los Cabos International", "San José del Cabo", "Mexico", ["cabo", "los cabos", "cabo san lucas", "san jose del cabo"]),
    "PVR": ("Gustavo Díaz Ordaz International", "Puerto Vallarta", "Mexico", ["puerto vallarta", "vallarta", "sayulita"]),
    "MEX": ("Mexico City International", "Mexico City", "Mexico", ["mexico city", "cdmx"]),
    "GDL": ("Guadalajara International", "Guadalajara", "Mexico", ["guadalajara"]),
    "LHR": ("London Heathrow", "London", "United Kingdom", ["london", "heathrow"]),
    "LGW": ("London Gatwick", "London", "United Kingdom", ["gatwick"]),
    "CDG": ("Charles de Gaulle", "Paris", "France", ["paris", "cdg"]),
    "FCO": ("Leonardo da Vinci–Fiumicino", "Rome", "Italy", ["rome", "roma", "fiumicino"]),
    "MXP": ("Milan Malpensa", "Milan", "Italy", ["milan", "milano"]),
    "BCN": ("Barcelona–El Prat", "Barcelona", "Spain", ["barcelona"]),
    "MAD": ("Adolfo Suárez Madrid–Barajas", "Madrid", "Spain", ["madrid"]),
    "AMS": ("Amsterdam Schiphol", "Amsterdam", "Netherlands", ["amsterdam", "schiphol"]),
    "FRA": ("Frankfurt Airport", "Frankfurt", "Germany", ["frankfurt"]),
    "MUC": ("Munich Airport", "Munich", "Germany", ["munich", "münchen"]),
    "ZRH": ("Zürich Airport", "Zürich", "Switzerland", ["zurich", "zürich"]),
    "DUB": ("Dublin Airport", "Dublin", "Ireland", ["dublin"]),
    "LIS": ("Lisbon Humberto Delgado", "Lisbon", "Portugal", ["lisbon", "lisboa"]),
    "ATH": ("Athens International", "Athens", "Greece", ["athens"]),
    "IST": ("Istanbul Airport", "Istanbul", "Turkey", ["istanbul"]),
    "NRT": ("Narita International", "Tokyo", "Japan", ["tokyo", "narita"]),
    "HND": ("Tokyo Haneda", "Tokyo", "Japan", ["haneda"]),
    "ICN": ("Incheon International", "Seoul", "South Korea", ["seoul", "incheon"]),
    "PEK": ("Beijing Capital International", "Beijing", "China", ["beijing", "peking"]),
    "HKG": ("Hong Kong International", "Hong Kong", "China", ["hong kong"]),
    "SIN": ("Singapore Changi", "Singapore", "Singapore", ["singapore"]),
    "BKK": ("Suvarnabhumi Airport", "Bangkok", "Thailand", ["bangkok"]),
    "SYD": ("Sydney Kingsford Smith", "Sydney", "Australia", ["sydney"]),
    "MEL": ("Melbourne Airport", "Melbourne", "Australia", ["melbourne"]),
    "AKL": ("Auckland Airport", "Auckland", "New Zealand", ["auckland"]),
    "YYZ": ("Toronto Pearson International", "Toronto", "Canada", ["toronto"]),
    "YVR": ("Vancouver International", "Vancouver", "Canada", ["vancouver"]),
    "YUL": ("Montréal-Trudeau International", "Montreal", "Canada", ["montreal", "montréal"]),
    "YOW": ("Ottawa Macdonald-Cartier International", "Ottawa", "Canada", ["ottawa"]),
    "YYC": ("Calgary International", "Calgary", "Canada", ["calgary"]),
    "NAS": ("Lynden Pindling International", "Nassau", "Bahamas", ["nassau", "bahamas"]),
    "MBJ": ("Sangster International", "Montego Bay", "Jamaica", ["montego bay", "jamaica"]),
    "SJU": ("Luis Muñoz Marín International", "San Juan", "Puerto Rico", ["san juan", "puerto rico"]),
    "PTY": ("Tocumen International", "Panama City", "Panama", ["panama city", "panama"]),
    "BOG": ("El Dorado International", "Bogotá", "Colombia", ["bogota", "bogotá"]),
    "LIM": ("Jorge Chávez International", "Lima", "Peru", ["lima"]),
    "GIG": ("Rio de Janeiro–Galeão International", "Rio de Janeiro", "Brazil", ["rio de janeiro", "rio"]),
    "GRU": ("São Paulo–Guarulhos International", "São Paulo", "Brazil", ["sao paulo", "são paulo"]),
    "EZE": ("Ministro Pistarini International", "Buenos Aires", "Argentina", ["buenos aires"]),
    "SCL": ("Santiago International", "Santiago", "Chile", ["santiago chile"]),
    "DXB": ("Dubai International", "Dubai", "UAE", ["dubai"]),
    "DOH": ("Hamad International", "Doha", "Qatar", ["doha", "qatar"]),
    "TLV": ("Ben Gurion Airport", "Tel Aviv", "Israel", ["tel aviv", "israel"]),
    "CAI": ("Cairo International", "Cairo", "Egypt", ["cairo"]),
    "CPT": ("Cape Town International", "Cape Town", "South Africa", ["cape town"]),
    "JNB": ("O.R. Tambo International", "Johannesburg", "South Africa", ["johannesburg"]),
    "DEL": ("Indira Gandhi International", "Delhi", "India", ["delhi", "new delhi"]),
    "BOM": ("Chhatrapati Shivaji Maharaj International", "Mumbai", "India", ["mumbai", "bombay"]),
    "KUL": ("Kuala Lumpur International", "Kuala Lumpur", "Malaysia", ["kuala lumpur"]),
    "PUJ": ("Punta Cana International", "Punta Cana", "Dominican Republic", ["punta cana", "dominican republic"]),
    "AUA": ("Queen Beatrix International", "Oranjestad", "Aruba", ["aruba"]),
    "PSE": ("Mercedita International", "Ponce", "Puerto Rico", ["ponce"]),
    "STT": ("Cyril E. King Airport", "St. Thomas", "US Virgin Islands", ["st thomas", "saint thomas", "usvi", "virgin islands"]),
    "STX": ("Henry E. Rohlsen Airport", "St. Croix", "US Virgin Islands", ["st croix", "saint croix"]),
}

# --------------------------------------------------------------------------- #
# State abbreviation → major gateway
# --------------------------------------------------------------------------- #

STATE_AIRPORTS = {
    "alabama": "BHM", "al": "BHM",
    "alaska": "ANC", "ak": "ANC",
    "arizona": "PHX", "az": "PHX",
    "arkansas": "LIT", "ar": "LIT",
    "california": "LAX", "ca": "LAX",
    "colorado": "DEN", "co": "DEN",
    "connecticut": "BDL", "ct": "BDL",
    "delaware": "PHL", "de": "PHL",
    "florida": "MCO", "fl": "MCO",
    "georgia": "ATL", "ga": "ATL",
    "hawaii": "HNL", "hi": "HNL",
    "idaho": "BOI", "id": "BOI",
    "illinois": "ORD", "il": "ORD",
    "indiana": "IND", "in": "IND",
    "iowa": "DSM", "ia": "DSM",
    "kansas": "MCI", "ks": "MCI",
    "kentucky": "SDF", "ky": "SDF",
    "louisiana": "MSY", "la": "MSY",
    "maine": "PWM", "me": "PWM",
    "maryland": "BWI", "md": "BWI",
    "massachusetts": "BOS", "ma": "BOS",
    "michigan": "DTW", "mi": "DTW",
    "minnesota": "MSP", "mn": "MSP",
    "mississippi": "JAN", "ms": "JAN",
    "missouri": "STL", "mo": "STL",
    "montana": "BZN", "mt": "BZN",
    "nebraska": "OMA", "ne": "OMA",
    "nevada": "LAS", "nv": "LAS",
    "new hampshire": "MHT", "nh": "MHT",
    "new jersey": "EWR", "nj": "EWR",
    "new mexico": "ABQ", "nm": "ABQ",
    "new york": "JFK", "ny": "JFK",
    "north carolina": "CLT", "nc": "CLT",
    "north dakota": "FAR", "nd": "FAR",
    "ohio": "CLE", "oh": "CLE",
    "oklahoma": "OKC", "ok": "OKC",
    "oregon": "PDX", "or": "PDX",
    "pennsylvania": "PHL", "pa": "PHL",
    "rhode island": "PVD", "ri": "PVD",
    "south carolina": "CHS", "sc": "CHS",
    "south dakota": "FSD", "sd": "FSD",
    "tennessee": "BNA", "tn": "BNA",
    "texas": "DFW", "tx": "DFW",
    "utah": "SLC", "ut": "SLC",
    "vermont": "BTV", "vt": "BTV",
    "virginia": "DCA", "va": "DCA",
    "washington": "SEA", "wa": "SEA",
    "west virginia": "CRW", "wv": "CRW",
    "wisconsin": "MKE", "wi": "MKE",
    "wyoming": "JAC", "wy": "JAC",
    "district of columbia": "DCA",
    "puerto rico": "SJU", "pr": "SJU",
}

# --------------------------------------------------------------------------- #
# Build lookup indices
# --------------------------------------------------------------------------- #

# All valid IATA codes in our database (upper-case)
_VALID_CODES = set(AIRPORTS.keys())

# alias → IATA code  (lower-case alias → code)
_ALIAS_INDEX: dict[str, str] = {}

# city name (lower) → IATA code  (first/default airport per city)
_CITY_INDEX: dict[str, str] = {}

for code, (name, city, region, aliases) in AIRPORTS.items():
    city_lower = city.lower()
    if city_lower not in _CITY_INDEX:
        _CITY_INDEX[city_lower] = code
    for alias in aliases:
        a = alias.lower().strip()
        if a not in _ALIAS_INDEX:
            _ALIAS_INDEX[a] = code


def _normalize(text: str) -> str:
    """Lower-case, strip, collapse whitespace, remove common noise."""
    text = text.lower().strip()
    # Remove trailing punctuation and common filler words
    text = re.sub(r"[,.\-!?]+$", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _score_substring(needle: str, haystack: str) -> int:
    """Return a rough match score. Higher is better. 0 = no match."""
    if needle == haystack:
        return 100
    if haystack.startswith(needle):
        return 90
    if needle in haystack:
        return 70
    if haystack in needle:
        return 60
    return 0


def resolve_airport(text: str) -> dict | None:
    """
    Resolve freeform location text to the nearest major airport.

    Returns dict with keys 'code', 'name', 'input' on success, or None.
    """
    if not text or not text.strip():
        return None

    original = text.strip()
    normalized = _normalize(original)

    # ------------------------------------------------------------------ #
    # 1. Direct IATA code match (e.g. "JFK", "lax")
    # ------------------------------------------------------------------ #
    upper = normalized.upper().strip()
    if len(upper) == 3 and upper.isalpha() and upper in _VALID_CODES:
        info = AIRPORTS[upper]
        return {"code": upper, "name": info[0], "input": original}

    # ------------------------------------------------------------------ #
    # 2. Exact alias match
    # ------------------------------------------------------------------ #
    if normalized in _ALIAS_INDEX:
        code = _ALIAS_INDEX[normalized]
        return {"code": code, "name": AIRPORTS[code][0], "input": original}

    # ------------------------------------------------------------------ #
    # 3. Exact city match
    # ------------------------------------------------------------------ #
    if normalized in _CITY_INDEX:
        code = _CITY_INDEX[normalized]
        return {"code": code, "name": AIRPORTS[code][0], "input": original}

    # ------------------------------------------------------------------ #
    # 4. State name / abbreviation match
    # ------------------------------------------------------------------ #
    if normalized in STATE_AIRPORTS:
        code = STATE_AIRPORTS[normalized]
        if code in AIRPORTS:
            return {"code": code, "name": AIRPORTS[code][0], "input": original}
        # Code might not be in our detailed AIRPORTS dict (smaller states)
        return {"code": code, "name": code, "input": original}

    # ------------------------------------------------------------------ #
    # 5. Fuzzy / substring matching against aliases and city names
    # ------------------------------------------------------------------ #
    best_code = None
    best_score = 0

    # Check aliases
    for alias, code in _ALIAS_INDEX.items():
        score = _score_substring(normalized, alias)
        if score == 0:
            # Also try the reverse — alias is contained in the input
            score = _score_substring(alias, normalized)
            if score > 0:
                # Penalize slightly — input is longer than the alias
                score = max(score - 10, 1)
        if score > best_score:
            best_score = score
            best_code = code

    # Check city names
    for city, code in _CITY_INDEX.items():
        score = _score_substring(normalized, city)
        if score == 0:
            score = _score_substring(city, normalized)
            if score > 0:
                score = max(score - 10, 1)
        if score > best_score:
            best_score = score
            best_code = code

    # Check airport names
    for code, (name, city, region, aliases) in AIRPORTS.items():
        name_lower = name.lower()
        score = _score_substring(normalized, name_lower)
        if score > best_score:
            best_score = score
            best_code = code

    # Require a minimum confidence
    if best_code and best_score >= 50:
        return {"code": best_code, "name": AIRPORTS[best_code][0], "input": original}

    # ------------------------------------------------------------------ #
    # 6. Try word-level matching — pick the best-matching word in input
    # ------------------------------------------------------------------ #
    words = normalized.split()
    for word in words:
        if len(word) < 3:
            continue

        # Check each word as a potential IATA code
        w_upper = word.upper()
        if len(w_upper) == 3 and w_upper.isalpha() and w_upper in _VALID_CODES:
            info = AIRPORTS[w_upper]
            return {"code": w_upper, "name": info[0], "input": original}

        # Check each word against aliases
        for alias, code in _ALIAS_INDEX.items():
            if word == alias:
                return {"code": code, "name": AIRPORTS[code][0], "input": original}

        # Check each word against city names
        for city, code in _CITY_INDEX.items():
            if word == city:
                return {"code": code, "name": AIRPORTS[code][0], "input": original}

    # ------------------------------------------------------------------ #
    # 7. Multi-word sliding window — try 2- and 3-word combos
    # ------------------------------------------------------------------ #
    for window in (3, 2):
        if len(words) >= window:
            for i in range(len(words) - window + 1):
                chunk = " ".join(words[i : i + window])
                if chunk in _ALIAS_INDEX:
                    code = _ALIAS_INDEX[chunk]
                    return {"code": code, "name": AIRPORTS[code][0], "input": original}
                if chunk in _CITY_INDEX:
                    code = _CITY_INDEX[chunk]
                    return {"code": code, "name": AIRPORTS[code][0], "input": original}
                if chunk in STATE_AIRPORTS:
                    code = STATE_AIRPORTS[chunk]
                    if code in AIRPORTS:
                        return {"code": code, "name": AIRPORTS[code][0], "input": original}
                    return {"code": code, "name": code, "input": original}

    # ------------------------------------------------------------------ #
    # 8. Lower-confidence substring match
    # ------------------------------------------------------------------ #
    if best_code and best_score >= 30:
        return {"code": best_code, "name": AIRPORTS[best_code][0], "input": original}

    return None
