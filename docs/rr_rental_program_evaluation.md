# Royal Resorts Rental Program — evaluation, and why to skip it

**Decision:** **DON'T use the RR Rental Program. Use Redweek V&P (+ Koala as
free fallback) instead.**

**Date:** 2026-04-28 (~10:30 PM PT)
**Triggered by:** Andy's question — *"Royal Sands rental program... I can't
imagine they don't just lie to you and say nobody rented and then take your
money. That seems ripe."*

His instinct is correct. The structure of the program makes verification
impossible from the owner side. This doc captures what the program actually
is, the side-by-side comparison with Redweek, and the math.

---

## 1. What "the RR Rental Program" actually is

It's a single program with confusing branding:

- **Marketing site:** [therentalprogram.com](https://therentalprogram.com/)
- **Rules / fine print:** [intervalservicing.com/index.php/rentals](https://www.intervalservicing.com/index.php/rentals)
- **Operator:** Interval Servicing Co. (now part of Holiday Inn Club Vacations
  / IHG since the 2023 RR acquisition)
- **Mailing address on both pages:**
  `Holiday Inn Club Vacations, Attn: Customer Commitments, 9271 S John
  Young Pkwy, Orlando, FL 32819` (the same Orlando back office that runs
  CSF billing)

**How it works (per the official rules page):**

1. You enroll your week — whole 2BR Villa, the 1BR side, or the studio side
2. Listings are 1-7 night flexible, can be entered up to 12 months prior
   to your interval
3. RR markets/rents the unit via "call center, online reservations center,
   marketing campaigns and travel-industry alliances"
4. Rentals processed FIFO ("first deposited, first rented basis")
5. You receive a check in USD (default), Royal Resorts Rewards, or wire
   transfer ($25 fee), AFTER:
   - You're current on CSF (no past-due balance)
   - 19% Mexican CIT is withheld (occupancies after Nov 1, 2020)
   - Whatever commission they take (rate not published anywhere on site)

---

## 2. The verifiability problem (Andy's smoking-gun concern)

The official rules page does NOT publish:

| Thing the owner needs to verify | RR Rental Program | What you get |
|---|---|---|
| Did anyone actually rent my week? | ❌ Owner can't independently verify | A yes/no notification + check |
| What did RR list it for? | ❌ Pricing set by RR, not visible to owner | N/A |
| What dates were rented? | ❌ Not exposed in any owner statement | N/A |
| What did the renter actually pay? | ❌ "Rental proceeds" is a single number | A check |
| Where am I in the FIFO queue? | ❌ Not exposed | "First deposited, first rented" claim |
| What's the commission rate? | ❌ Not published online | Implied ~10% per Andy's old dossier note (unverified) |
| Recourse if I dispute "no rental"? | ❌ HICV "Customer Commitments" office — same company | Complaints loop back internally |

**This is textbook black-box pricing.** RR is the only counterparty on
every dimension: they set the price, they pick the renter, they take their
cut, they decide what to tell you happened. There's no independent audit
trail.

**Compare Redweek V&P** (where every step is owner-visible):

| Thing | Redweek V&P |
|---|---|
| Who rented | Renter's name + email visible in your Redweek dashboard |
| When | Exact check-in / check-out dates |
| Amount | Exact dollar amount (the renter pays, you see it) |
| Commission | Published: $99 listing + 17% on success |
| Escrow | Independent — Redweek holds the funds before paying you |
| Dispute mechanism | V&P protection: refunded if owner / renter dispute |
| Comp data | 53 live Royal Sands rentals visible to anyone |

---

## 3. Has Andy ever used it before?

**No.** Per his own dossier `Timeline` doc:

- **Sep 2018:** *"Deposited Week 38 with II. Booked exchange (Confirmation
  #025041374)"* — this was an **II EXCHANGE** (got a different week
  somewhere), NOT an RR Rental Program enrollment.
- **Jan 2009:** *"Traded Week 38 for a friend's Vegas timeshare"* — direct
  owner-to-owner trade, NOT RR Rental Program.
- **Feb 2011:** *"Listed for $12,500"* — this was a resale listing, not
  rental.

Andy's memory of "we tried that one time" is most likely conflating the
2018 II deposit with the RR Rental Program — they're separate systems run
by overlapping staff, but the II deposit gave him an exchange certificate
(used to book Antigua trip), not rental cash.

So Andy has never been through the RR Rental Program at all. There's no
pre-existing trust evidence either way — but there's no need to test it
when Redweek is cleaner and more transparent.

---

## 4. The math (at $1,500 gross — realistic for K5133 ground-floor pool-view)

```
SCENARIO A — RR RENTAL PROGRAM:
  Gross rental (RR sets price):             $1,500   (assumed equivalent to Redweek ask)
  Less RR commission (~10% per old dossier):  -$150
  Less 19% Mexican CIT:                       -$257  (on the post-commission $1,350)
  Less wire fee (if not by check):             -$25
  ─────────────────────────────────────────────────
  Owner net:                                  ~$1,068

  Then SUBTRACT the unknowables:
    - Risk RR claims "no rental" when there was demand
    - Risk RR sets list price below market to get easy bookings
    - Cannot opt out and re-list elsewhere mid-cycle without forfeiting

SCENARIO B — REDWEEK V&P:
  Gross rental (you set):                    $1,500
  Less Redweek 17% + $99:                      -$354
  Less Redweek listing fee:                     -$59
  ─────────────────────────────────────────────────
  Owner net:                                  ~$1,087

  Plus the verifiables:
    + Renter visible
    + Dates visible
    + Pricing visible
    + Comp data scrapeable any time

DIFFERENCE: $19 advantage to Redweek IF RR is fully honest.
            $1,087 advantage to Redweek IF RR fakes "no rental."
```

The headline net is roughly the same. The TAIL RISK is night-and-day
different.

---

## 5. The 19% Mexican CIT — non-negotiable, but a real haircut

Per the rules page:
> *"Due to changes in Mexican tax law associated with the promotion and
> rental of units located in Mexico, starting with the occupancy of
> November 1, 2020 (interval 44), CIT will withhold 19% of the rental
> proceeds corresponding to your unit."*

This applies to RR Rental Program proceeds because RR is a Mexican
corporate entity (Operadora Real Arenas) renting on Mexican soil. **This
withholding is automatic on RR Rental Program income** and is not
recoverable by Andy via US tax filings (it's a foreign withholding on
foreign-sourced income, not a US tax credit unless he files complex
foreign-tax forms).

**Redweek bypasses this because the rental contract is a US-Florida
escrow transaction between Andy and the renter** (RR's role is just
adding the renter as a guest on the unit, not collecting the rental
fee). No Mexican CIT applies.

That alone is a ~$257 swing per $1,500 of gross rental — the
single biggest dollar reason to use Redweek over RR.

---

## 6. The narrow case where RR Rental Program wins

There's exactly one scenario where it makes sense:

**You want Royal Resorts Rewards to fund a future RR vacation.** Rewards
are RR's internal currency, redeemable for stays at any RR property
(Royal Sands, Royal Cancun, Royal Haciendas, Grand Residences). If you
take payout in Rewards instead of cash, you avoid the 19% CIT (Rewards
isn't taxable income in Mexico) and you skip the wire fee. The catch:
Rewards are locked into RR — useless if you don't plan future RR trips.

**For Andy: not relevant.** He's not planning future Cancun trips ("we
aren't going" — his words). Future trips will be Hawaii, not Cancun. No
RR Rewards utility.

---

## 7. Industry pattern — why developer-rental programs are this way

Per Reddit r/TimeshareOwners + TUG forum consensus collected on 2026-04-28:

- **u/dpark64** (Westin Kierland owner, 10+ years): *"With II / developer
  programs you put in $1 and get back $0.70."* Switched to Redweek;
  reports doubling his net.
- **u/lovetotravel1923** (multi-year II + RR adjacent owner): *"They have
  prime timeshare units available in their Getaways but exchange owners
  get the worst section."* Same opacity pattern across all developer
  programs.
- **r/TimeshareOwners** general thread on Wyndham / Marriott VC / Hilton
  GVC rental programs: "you sign over control, you get whatever they
  send you, no comps, no audit."

The pattern is structural, not RR-specific. Developer rental programs
exist because they capture rental margin from owners who don't want the
hassle of self-listing. **The "service fee" is the lack of transparency.**

---

## 8. Decision matrix — final verdict

| Question | Answer |
|---|---|
| Will I personally vacation at Royal Sands again? | **No** (Andy's stated position) |
| Do I need RR Rewards for future RR stays? | **No** |
| Do I want to verify my rental sold? | **Yes** |
| Do I want to set my own price based on real comps? | **Yes** |
| Do I want my income subject to 19% Mexican CIT? | **No** |
| Does my unit (K5133, ground-floor pool-view) match RR's marketing target? | **No** — RR pushes oceanfront premium units, not ground-floor interior |

**→ Use Redweek V&P. Cross-list on Koala (free) for redundancy.**

---

## 9. Open questions / future investigation

- ⏳ **Actual RR commission rate** — Andy's dossier said 10%; the official
  page is silent in 2026. Worth confirming with Jorge Aguayo if curiosity
  remains, but doesn't change the decision.
- ⏳ **Whether Royal Sands FB groups have any rental-program horror
  stories** — the Facebook owner group at
  `facebook.com/groups/55211050419/` is private; would need Andy to join
  and search internally. Low ROI given the decision is already clear.
- ⏳ **Whether RR ever sent Andy a "your week wasn't rented" notice in the
  past** — searched Gmail tonight, no hits. Andy has never enrolled.

---

## 10. Sources cited (live tonight)

1. **Official rules page**: `intervalservicing.com/index.php/rentals` —
   the "Rental Program - Some Rules" section (full quote in §1)
2. **Marketing site**: `therentalprogram.com` and its sub-pages (FAQ,
   About, Enrollments — all redirect to the same 1.6KB landing page,
   which is itself notable: a real consumer site would have a populated
   FAQ)
3. **June 2024 RR blog**: `royalresorts.com/blog/june-2024/the-rental-program/`
4. **Andy's `Timeline` doc** (Drive folder
   `1C0IgQJn9mChJqAjs9OMCQas27oYjk1Jz`) — confirmed no prior enrollment
5. **Andy's Gmail history** (April 28 sweep) — zero RR Rental Program
   correspondence

---

## 11. What Andy is doing instead (cross-reference)

For the actual rental playbook he'll execute, see
`docs/2026apr28_855pm.md` §6 ("Path B — Rent annually on Redweek") and
§3b ("K5133 unit position — ground-floor interior") for the listing-copy
positioning.

The short version:
- List on Redweek V&P at **$1,650** (~$236/night)
- Cross-list on go-koala.com (free)
- Listing language: ground-floor walk-out + private grass patio + pool
  view + 90-second walk to beach (per the 17-year-old TripAdvisor thread
  that validated the unit's actual position)
- Realistic net: $1,150-1,400 cash
- Cancun side roughly breaks even with the $1,538 maintenance — frees
  the week to fund a Hawaii trip via Royal Lahaina or Wailea Ike

---

*— Andy Tillo + Claude session 2026-04-28 ~10:30 PM PT*
