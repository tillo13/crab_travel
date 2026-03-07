import json
import logging
from utilities.claude_utils import generate_text

logger = logging.getLogger(__name__)


def aggregate_preferences(all_prefs):
    """Summarize all members' preferences into a group profile."""
    completed = [p for p in all_prefs if p.get('completed')]
    if not completed:
        return None

    # Budget overlap
    budgets_min = [p['budget_min'] for p in completed if p.get('budget_min')]
    budgets_max = [p['budget_max'] for p in completed if p.get('budget_max')]
    budget_summary = None
    if budgets_min and budgets_max:
        # Overlap = highest min to lowest max
        overlap_min = max(budgets_min)
        overlap_max = min(budgets_max)
        if overlap_min <= overlap_max:
            budget_summary = f"${overlap_min // 100}–${overlap_max // 100} per person (overlap)"
        else:
            budget_summary = f"${min(budgets_min) // 100}–${max(budgets_max) // 100} per person (no perfect overlap, range shown)"

    # Interest frequency
    interest_counts = {}
    for p in completed:
        for interest in (p.get('interests') or []):
            interest_counts[interest] = interest_counts.get(interest, 0) + 1
    top_interests = sorted(interest_counts.items(), key=lambda x: -x[1])

    # Accommodation styles
    acc_counts = {}
    for p in completed:
        acc = p.get('accommodation_style')
        if acc:
            acc_counts[acc] = acc_counts.get(acc, 0) + 1

    # Dietary needs (union)
    dietary = set()
    for p in completed:
        if p.get('dietary_needs'):
            dietary.add(p['dietary_needs'])

    # Mobility notes (union)
    mobility = set()
    for p in completed:
        if p.get('mobility_notes'):
            mobility.add(p['mobility_notes'])

    return {
        'member_count': len(completed),
        'total_members': len(all_prefs),
        'budget': budget_summary,
        'top_interests': top_interests[:10],
        'accommodation': sorted(acc_counts.items(), key=lambda x: -x[1]),
        'dietary_needs': list(dietary),
        'mobility_notes': list(mobility),
        'members': [{'name': p['display_name'], 'interests': p.get('interests', [])} for p in completed],
    }


def generate_recommendations(plan, all_prefs):
    """Generate AI recommendations based on group preferences."""
    summary = aggregate_preferences(all_prefs)
    if not summary:
        return None, "No members have filled in preferences yet."

    # Build the prompt
    interest_str = ', '.join([f"{name} ({count}/{summary['member_count']})" for name, count in summary['top_interests']])
    acc_str = ', '.join([f"{style} ({count})" for style, count in summary['accommodation']])
    dietary_str = ', '.join(summary['dietary_needs']) if summary['dietary_needs'] else 'None specified'
    mobility_str = ', '.join(summary['mobility_notes']) if summary['mobility_notes'] else 'None specified'

    prompt = f"""You are a group travel advisor. Generate recommendations for this trip.

TRIP: {plan['title']}
DESTINATION: {plan.get('locked_destination') or plan.get('destination', 'Not specified')}
DATES: {plan.get('locked_start_date') or plan.get('start_date', '?')} to {plan.get('locked_end_date') or plan.get('end_date', '?')}
GROUP SIZE: {summary['total_members']} people ({summary['member_count']} have filled preferences)

GROUP PREFERENCES:
- Budget: {summary['budget'] or 'Not specified'}
- Top interests: {interest_str or 'Not specified'}
- Accommodation preference: {acc_str or 'Not specified'}
- Dietary needs: {dietary_str}
- Mobility/accessibility: {mobility_str}

Generate exactly 3 hotel recommendations, 5 activity recommendations, and 3 restaurant recommendations.

Return ONLY valid JSON in this exact format (no markdown, no code fences):
{{
  "recommendations": [
    {{
      "category": "hotel",
      "title": "Hotel Name",
      "description": "Why this works for the group (2-3 sentences)",
      "price_estimate": "$X–$Y per night",
      "compatibility_score": 85,
      "ai_reasoning": "Brief explanation of how this matches the group's preferences"
    }}
  ]
}}

compatibility_score should be 1-100 based on how well the option matches the GROUP's collective preferences.
For activities, consider the top interests. For hotels, consider budget and accommodation style. For restaurants, consider dietary needs.
Be specific — use real places if you know the destination, otherwise create realistic suggestions."""

    system = "You are a knowledgeable travel advisor. Always respond with valid JSON only. No markdown formatting."

    try:
        text, tokens_in, tokens_out = generate_text(prompt, system=system, max_tokens=4096, temperature=0.7)

        # Clean up response — strip markdown fences if Claude adds them
        text = text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1]
        if text.endswith('```'):
            text = text.rsplit('```', 1)[0]
        text = text.strip()

        data = json.loads(text)
        recs = data.get('recommendations', [])
        logger.info(f"🤖 Generated {len(recs)} recommendations ({tokens_in}+{tokens_out} tokens)")
        return recs, None
    except json.JSONDecodeError as e:
        logger.error(f"❌ Failed to parse AI response: {e}")
        logger.error(f"Raw response: {text[:500]}")
        return None, "AI returned invalid response. Try again."
    except Exception as e:
        logger.error(f"❌ AI generation failed: {e}")
        return None, str(e)


def generate_destination_card(destination_name, research_data, group_prefs, travel_window=None, group_vibes=None):
    """Use Claude to research a destination — fun things to do, events, activities with dates."""
    flights_summary = []
    for airport, fdata in (research_data.get('flights') or {}).items():
        cheapest = fdata.get('cheapest')
        if cheapest:
            flights_summary.append(f"From {airport}: ${cheapest} (cheapest found)")
        elif fdata.get('flights') and fdata['flights'][0].get('price_range'):
            flights_summary.append(f"From {airport}: {fdata['flights'][0]['price_range']}")

    # Build group context
    pref_summary = aggregate_preferences(group_prefs) if group_prefs else None
    group_context = ""
    if pref_summary:
        interests = ', '.join([f"{n} ({c})" for n, c in pref_summary.get('top_interests', [])[:5]])
        group_context = f"""
GROUP ({pref_summary['member_count']} members):
- Budget: {pref_summary.get('budget', 'Not specified')}
- Top interests: {interests}
- Dietary: {', '.join(pref_summary.get('dietary_needs', [])) or 'None'}
- Mobility: {', '.join(pref_summary.get('mobility_notes', [])) or 'None'}"""

    window_context = ""
    if travel_window:
        window_context = f"\nTRAVEL WINDOW: {travel_window.get('start', 'flexible')} to {travel_window.get('end', 'flexible')}"

    vibes_context = ""
    vibes_instruction = ""
    if group_vibes:
        vibes_context = f"""

=== THIS IS THE MOST IMPORTANT PART ===
GROUP VIBES: {group_vibes}

The coordinator told us EXACTLY what the group wants: "{group_vibes}".
This overrides everything. EVERY category must reflect this:
- things_to_do: ALL 8 activities must relate to {group_vibes}. Stadiums, arenas, breweries, sports bars, related museums, themed tours — whatever matches "{group_vibes}" in {destination_name}.
- food_and_drink: ALL 5 must be {group_vibes}-themed. Sports bars, breweries, taprooms, themed restaurants. No generic fine dining unless it fits the vibe.
- upcoming_events: ALL must be {group_vibes}-related. Games, tournaments, festivals, tastings, leagues.
- stays: Pick hotels NEAR the {group_vibes} action. Walking distance to stadiums, brewery districts, etc.
DO NOT include generic tourist activities that don't relate to "{group_vibes}". The whole point is that this board should make the group say "YES, this is exactly what we want."
=== END CRITICAL SECTION ==="""
        vibes_instruction = f"\n\nFINAL REMINDER: Every single pin on this board should make someone who loves '{group_vibes}' excited. If a pin doesn't relate to '{group_vibes}', replace it with one that does."

    prompt = f"""You are a local expert and event researcher for {destination_name}. Research this destination thoroughly for a group trip.

DESTINATION: {destination_name}
{group_context}{vibes_context}
{window_context}

FLIGHT DATA:
{chr(10).join(flights_summary) if flights_summary else 'No flight data yet'}

Think like a friend who lives in {destination_name} and is excited to show visitors around. Build a Pinterest-style board of "pins" — specific places, events, and experiences that would make the group want to go.

Research and include:

1. **Where to stay** — 3-4 real hotels, Airbnbs, or unique stays. Mix budget levels. Include neighborhood context.
2. **Things to do** — 6-8 specific activities, attractions, day trips. Real place names. Group-friendly.
3. **Where to eat & drink** — 4-5 restaurants, bars, food experiences. Specific names, what they're known for.
4. **Upcoming events** — 3-5 festivals, concerts, sporting events, seasonal happenings with dates. What's happening in {destination_name} in the next 3-6 months?

Return ONLY valid JSON:
{{
  "summary": "2-3 sentence pitch for why this destination is great for the group",
  "weather_note": "Brief weather/season context",
  "stays": [
    {{
      "name": "Hotel/property name",
      "description": "1-2 sentences — vibe, location, why it works for a group",
      "neighborhood": "Area/district name",
      "price_hint": "$ / $$ / $$$ / $$$$",
      "image_search": "one or two keywords for finding a photo of this place"
    }}
  ],
  "things_to_do": [
    {{
      "name": "Specific place or activity name",
      "description": "1-2 sentences — why it's fun, what makes it special",
      "category": "outdoors | culture | sports | shopping | music | day-trip | adventure | tours",
      "group_vibe": "chill | active | party | cultural | adventurous",
      "price_hint": "Free / $ / $$ / $$$",
      "image_search": "one or two keywords for a photo"
    }}
  ],
  "food_and_drink": [
    {{
      "name": "Restaurant or bar name",
      "description": "1-2 sentences — what to order, the vibe",
      "category": "restaurant | bar | cafe | food-market | brewery | rooftop",
      "price_hint": "$ / $$ / $$$ / $$$$",
      "image_search": "one or two keywords for a photo"
    }}
  ],
  "upcoming_events": [
    {{
      "name": "Event name",
      "date_range": "Mar 15-17, 2026 or Spring 2026 or Monthly etc",
      "description": "What it is and why it's worth planning around",
      "category": "festival | sports | music | food | cultural | seasonal",
      "image_search": "one or two keywords for a photo"
    }}
  ],
  "best_dates": "When to go and why — tie to events if possible",
  "highlights": ["3-4 top reasons to pick this destination"],
  "concerns": ["Any potential issues or downsides"],
  "estimated_total_per_person": "Rough USD estimate for a 4-5 day trip",
  "compatibility_score": 75
}}

Be specific and real. Use actual venue names, actual event names, actual dates. Don't make up events — if you're unsure of a date, say "typically" or give the month. The image_search field should be 1-2 keywords that would find a great photo (e.g. "nashville broadway", "hot air balloon desert"). The goal is to build a visual board that gets people excited.

compatibility_score: 1-100 based on how well this destination fits the group.{vibes_instruction}"""

    system = "You are an expert travel researcher and local guide. You know real venues, real events, real dates. Respond with valid JSON only."

    try:
        text, _, _ = generate_text(prompt, system=system, max_tokens=3000, temperature=0.7)
        text = text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1]
        if text.endswith('```'):
            text = text.rsplit('```', 1)[0]
        return json.loads(text.strip())
    except Exception as e:
        logger.error(f"❌ Destination card generation failed: {e}")
        return None


def suggest_destinations(group_prefs, budget_hint=None):
    """Ask Claude to suggest destinations based on group preferences."""
    pref_summary = aggregate_preferences(group_prefs) if group_prefs else None
    if not pref_summary:
        return []

    interests = ', '.join([f"{n} ({c})" for n, c in pref_summary.get('top_interests', [])[:8]])

    prompt = f"""Suggest 5 travel destinations for this group.

GROUP ({pref_summary['member_count']} members):
- Budget: {pref_summary.get('budget', budget_hint or 'Moderate')}
- Top interests: {interests}
- Dietary considerations: {', '.join(pref_summary.get('dietary_needs', [])) or 'None'}
- Mobility considerations: {', '.join(pref_summary.get('mobility_notes', [])) or 'None'}

Return ONLY valid JSON:
{{
  "suggestions": [
    {{
      "destination": "City, Country",
      "reason": "Why this works for the group (1-2 sentences)",
      "estimated_cost": "Rough per-person estimate for 5 days"
    }}
  ]
}}

Be creative but practical. Mix well-known and unexpected destinations. Consider the group's interests and budget."""

    system = "You are a creative travel advisor. Respond with valid JSON only."

    try:
        text, _, _ = generate_text(prompt, system=system, max_tokens=1024, temperature=0.8)
        text = text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1]
        if text.endswith('```'):
            text = text.rsplit('```', 1)[0]
        data = json.loads(text.strip())
        return data.get('suggestions', [])
    except Exception as e:
        logger.error(f"❌ Destination suggestion failed: {e}")
        return []
