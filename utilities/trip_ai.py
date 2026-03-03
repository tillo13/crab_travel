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
DESTINATION: {plan.get('destination', 'Not specified')}
DATES: {plan.get('start_date', '?')} to {plan.get('end_date', '?')}
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
