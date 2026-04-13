#!/usr/bin/env python3
"""
Crab Crawlers bot persona data — split from dev/trip_bots.py for kumori compliance.

Pure data module: PERSONAS list + name/interest/airport pools + basic config.
Imported by trip_bots.py and bot_orchestrator.py.
"""

# ─── Config ──────────────────────────────────────────────────────────────────

PROD_URL = "https://crab.travel"
TIMEOUT = 30
BOT_PREFIX = "[BOT]"

# ─── Bot Personas ────────────────────────────────────────────────────────────

PERSONAS = [
    {
        'slug': 'marcus_chen',
        'name': f'{BOT_PREFIX} Marcus Chen',
        'email': 'bot.marcus.chen@crab.travel',
        'google_id': 'bot_marcus_chen',
        'airport': 'SEA',
        'budget_min': 20000, 'budget_max': 40000,
        'accommodation': 'hotel',
        'interests': ['hiking', 'food', 'photography'],
        'dietary': '', 'mobility': '',
        'role': 'organizer',
        'chat_messages': [
            "Hey everyone! Excited for this trip. Who's leaning Scottsdale?",
            "I've been checking flight prices from Seattle, looking good for May.",
        ],
        'vote_ranks': {'Scottsdale AZ': 1, 'Nashville TN': 2, 'San Juan PR': 3},
        'blackouts': [],
        'tentative_dates': [],
        'is_flexible': False,
    },
    {
        'slug': 'sarah_kim',
        'name': f'{BOT_PREFIX} Sarah Kim',
        'email': 'bot.sarah.kim@crab.travel',
        'google_id': 'bot_sarah_kim',
        'airport': 'LAX',
        'budget_min': 15000, 'budget_max': 35000,
        'accommodation': 'airbnb',
        'interests': ['beach', 'nightlife', 'shopping'],
        'dietary': 'vegetarian', 'mobility': '',
        'role': 'member',
        'chat_messages': ["San Juan would be amazing! But I'm happy with any of the three."],
        'vote_ranks': {'Scottsdale AZ': 1, 'Nashville TN': 2, 'San Juan PR': 3},
        'blackouts': [],
        'tentative_dates': [],
        'is_flexible': True,
    },
    {
        'slug': 'david_okafor',
        'name': f'{BOT_PREFIX} David Okafor',
        'email': 'bot.david.okafor@crab.travel',
        'google_id': 'bot_david_okafor',
        'airport': 'ORD',
        'budget_min': 10000, 'budget_max': 25000,
        'accommodation': 'hotel',
        'interests': ['history', 'museums', 'food'],
        'dietary': '', 'mobility': 'Uses a wheelchair — need ADA-accessible accommodations',
        'role': 'member',
        'chat_messages': [],
        'vote_ranks': {'Nashville TN': 1, 'Scottsdale AZ': 2},
        'blackouts': [{'start': '2026-05-17', 'end': '2026-05-18'}],
        'tentative_dates': [],
        'is_flexible': False,
    },
    {
        'slug': 'emily_rodriguez',
        'name': f'{BOT_PREFIX} Emily Rodriguez',
        'email': 'bot.emily.rodriguez@crab.travel',
        'google_id': 'bot_emily_rodriguez',
        'airport': 'DFW',
        'budget_min': 20000, 'budget_max': 50000,
        'accommodation': 'resort',
        'interests': ['spa', 'wine', 'yoga'],
        'dietary': 'gluten-free', 'mobility': '',
        'role': 'member',
        'chat_messages': [],
        'vote_ranks': {'San Juan PR': 1, 'Scottsdale AZ': 2, 'Nashville TN': 3},
        'blackouts': [],
        'tentative_dates': [],
        'is_flexible': True,
    },
    {
        'slug': 'jake_thompson',
        'name': f'{BOT_PREFIX} Jake Thompson',
        'email': 'bot.jake.thompson@crab.travel',
        'google_id': 'bot_jake_thompson',
        'airport': 'JFK',
        'budget_min': 8000, 'budget_max': 20000,
        'accommodation': 'hostel',
        'interests': ['surfing', 'adventure', 'budget'],
        'dietary': '', 'mobility': '',
        'role': 'member',
        'chat_messages': ["Scottsdale is the move. Budget-friendly and tons to do."],
        'vote_ranks': {'Scottsdale AZ': 1},
        'blackouts': [],
        'tentative_dates': [],
        'is_flexible': False,
    },
    {
        'slug': 'priya_patel',
        'name': f'{BOT_PREFIX} Priya Patel',
        'email': 'bot.priya.patel@crab.travel',
        'google_id': 'bot_priya_patel',
        'airport': 'SFO',
        'budget_min': 15000, 'budget_max': 40000,
        'accommodation': 'airbnb',
        'interests': ['cooking', 'art', 'theater'],
        'dietary': 'vegan', 'mobility': '',
        'role': 'member',
        'chat_messages': ["I can cook for the group if we get an Airbnb!"],
        'vote_ranks': {'Nashville TN': 1, 'San Juan PR': 2, 'Scottsdale AZ': 3},
        'blackouts': [],
        'tentative_dates': [{'start': '2026-05-20', 'end': '2026-05-22'}],
        'is_flexible': False,
    },
    {
        'slug': 'tom_nguyen',
        'name': f'{BOT_PREFIX} Tom Nguyen',
        'email': 'bot.tom.nguyen@crab.travel',
        'google_id': 'bot_tom_nguyen',
        'airport': 'ATL',
        'budget_min': 10000, 'budget_max': 30000,
        'accommodation': 'hotel',
        'interests': ['golf', 'sports', 'bbq'],
        'dietary': '', 'mobility': 'Bad knee — limited walking distance',
        'role': 'member',
        'chat_messages': [],
        'vote_ranks': {'Scottsdale AZ': 1, 'Nashville TN': 2},
        'blackouts': [{'start': '2026-05-16', 'end': '2026-05-17'}],
        'tentative_dates': [{'start': '2026-05-21', 'end': '2026-05-24'}],
        'is_flexible': False,
    },
    {
        'slug': 'lisa_washington',
        'name': f'{BOT_PREFIX} Lisa Washington',
        'email': 'bot.lisa.washington@crab.travel',
        'google_id': 'bot_lisa_washington',
        'airport': 'DEN',
        'budget_min': 20000, 'budget_max': 45000,
        'accommodation': 'resort',
        'interests': ['skiing', 'cocktails', 'live music'],
        'dietary': 'pescatarian', 'mobility': '',
        'role': 'member',
        'chat_messages': [],
        'vote_ranks': {'San Juan PR': 1, 'Nashville TN': 2},
        'blackouts': [],
        'tentative_dates': [],
        'is_flexible': True,
        'suggests_destination': 'San Juan PR',
    },
    {
        'slug': 'carlos_mendez',
        'name': f'{BOT_PREFIX} Carlos Mendez',
        'email': 'bot.carlos.mendez@crab.travel',
        'google_id': 'bot_carlos_mendez',
        'airport': 'MIA',
        'budget_min': 12000, 'budget_max': 28000,
        'accommodation': 'hotel',
        'interests': ['diving', 'fishing', 'nature'],
        'dietary': '', 'mobility': '',
        'role': 'member',
        'chat_messages': [],
        'vote_ranks': {'Scottsdale AZ': 1},
        'blackouts': [],
        'tentative_dates': [],
        'is_flexible': False,
        'late_joiner': True,
    },
    {
        'slug': 'amy_foster',
        'name': f'{BOT_PREFIX} Amy Foster',
        'email': 'bot.amy.foster@crab.travel',
        'google_id': 'bot_amy_foster',
        'airport': 'BOS',
        'budget_min': 18000, 'budget_max': 35000,
        'accommodation': 'airbnb',
        'interests': ['running', 'coffee', 'bookshops'],
        'dietary': 'lactose-free', 'mobility': '',
        'role': 'member',
        'chat_messages': [
            "Nashville has incredible coffee shops and live music. Just saying.",
            "Also, my tentative dates work for the 20th-22nd window.",
        ],
        'vote_ranks': {'Nashville TN': 1, 'Scottsdale AZ': 2, 'San Juan PR': 3},
        'blackouts': [],
        'tentative_dates': [{'start': '2026-05-20', 'end': '2026-05-22'}],
        'is_flexible': False,
    },
]

DESTINATIONS = ['Scottsdale AZ', 'Nashville TN']  # San Juan added by Lisa in Phase 3

# ─── First Names Pool (for random persona generation) ────────────────────────

FIRST_NAMES = [
    'Marcus', 'Sarah', 'David', 'Emily', 'Jake', 'Priya', 'Tom', 'Lisa',
    'Carlos', 'Amy', 'Wei', 'Fatima', 'Kenji', 'Sofia', 'Olga', 'Mateo',
    'Aisha', 'Liam', 'Yuki', 'Nina', 'Ravi', 'Chloe', 'Omar', 'Hana',
    'Felix', 'Zara', 'Dmitri', 'Luna', 'Kofi', 'Maya', 'Sven', 'Amara',
    'Jin', 'Isla', 'Diego', 'Nia', 'Kai', 'Rosa', 'Theo', 'Leila',
]
LAST_NAMES = [
    'Chen', 'Kim', 'Okafor', 'Rodriguez', 'Thompson', 'Patel', 'Nguyen',
    'Washington', 'Mendez', 'Foster', 'Zhang', 'Ali', 'Tanaka', 'Petrov',
    'Garcia', 'Ibrahim', 'Ito', 'Santos', 'Johansson', 'Mbeki', 'Singh',
    'Murphy', 'Costa', 'Yamamoto', 'Osei', 'Berg', 'Reyes', 'Volkov',
    'Lee', 'Hassan', 'Schmidt', 'Nakamura', 'Torres', 'Andersen', 'Diallo',
]
AIRPORTS = [
    'SEA', 'LAX', 'ORD', 'DFW', 'JFK', 'SFO', 'ATL', 'DEN', 'MIA', 'BOS',
    'PHX', 'IAH', 'MSP', 'DTW', 'PHL', 'CLT', 'SAN', 'TPA', 'PDX', 'SLC',
    'AUS', 'RDU', 'BNA', 'STL', 'MCI', 'IND', 'CLE', 'PIT', 'CMH', 'OAK',
]
INTERESTS_POOL = [
    'hiking', 'food', 'photography', 'beach', 'nightlife', 'shopping',
    'history', 'museums', 'spa', 'wine', 'yoga', 'surfing', 'adventure',
    'cooking', 'art', 'theater', 'golf', 'sports', 'bbq', 'skiing',
    'cocktails', 'live music', 'diving', 'fishing', 'nature', 'running',
    'coffee', 'bookshops', 'architecture', 'street food', 'temples',
    'markets', 'cycling', 'kayaking', 'stargazing', 'wildlife',
]
DIETARY_OPTIONS = ['', '', '', '', 'vegetarian', 'vegan', 'gluten-free', 'pescatarian', 'lactose-free', 'halal', 'kosher']
ACCOMMODATION_OPTIONS = ['hotel', 'hotel', 'airbnb', 'airbnb', 'resort', 'hostel', 'flexible']
MOBILITY_OPTIONS = ['', '', '', '', '', '', '', 'wheelchair accessible', 'limited walking', 'no stairs']
