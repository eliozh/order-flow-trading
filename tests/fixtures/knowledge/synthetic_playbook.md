# Synthetic order-flow skill (test fixture)

This document is synthetic test data. It is not derived from any book and
carries no third-party copyright. It exists only to exercise the compiler.

## Market structure and auction

Market structure describes where price is accepted and where it is rejected.
An auction that keeps accepting higher prices shows persistent demand; a failed
auction that returns into the prior range shows supply. Use structure as
context, never as a standalone entry signal. Mark the prior high, prior low,
and the most recent acceptance area.

## Absorption and exhaustion

Absorption appears when aggressive volume trades into a level and price stops
moving. Exhaustion is the same idea at the end of a move: buyers or sellers
spend effort but no longer achieve progress. Treat absorption as a hypothesis
that needs confirmation from delta and structure.

## Cumulative volume delta (CVD)

CVD sums signed volume over a window. Divergence between price and CVD can
flag hidden supply or demand, but the divergence alone is not a trade. Compare
the CVD slope with the price slope over the same window and require a
structural reference level.

## Delta

Delta measures the signed volume of one bar. A positive delta bar that closes
off its high can indicate sellers absorbing buyers. A negative delta bar that
closes off its low can indicate buyers absorbing sellers. Record both the bar
delta and the cumulative context.

## Footprint imbalance

A footprint shows traded volume at each price. A stacked imbalance is a run of
diagonal imbalances in one direction. Read imbalances together with the level
they occur at; an imbalance inside a value area is weaker evidence than one at
an untested prior extreme.

## Volume profile and POC

The point of control is the price with the most traded volume. The value area
is the range containing the bulk of that volume. A naked point of control from
a prior session can act as a reference. Location relative to value matters:
above value, inside value, and below value are different contexts.

## Open interest

Open interest tracks outstanding contracts. Rising open interest with a
directional move suggests new positioning; falling open interest suggests
closing. Use open interest as a context filter, not as a timing trigger.

## Funding

Funding expresses the cost of holding a perpetual position. Extremely positive
funding can indicate crowded longs; extremely negative funding can indicate
crowded shorts. Funding is a slow context variable and never overrides a
structural invalidation.

## Liquidations and stop runs

Liquidation clusters often sit beyond obvious swing points. A fast move through
such a level followed by a quick return can be a stop run rather than genuine
acceptance. Require a close back inside the prior range before treating the
move as a failed breakout.

## Entry and invalidation

An entry needs a reference level, a trigger, and an invalidation. The
invalidation is the price at which the idea is wrong, and it must be known
before the entry. Prefer entries where the stop distance and the target
distance produce an acceptable reward-to-risk ratio.

## Trade management

Trade management starts after the position exists. Move the stop only when the
market provides a new structural reference. Partial exits should be planned in
advance. Do not widen a stop to avoid a loss; that converts a small controlled
loss into an uncontrolled one.

## Risk

Risk is the amount that can be lost if the invalidation is hit. Size from the
stop distance, not from conviction. A single trade must not threaten the
account, and correlated positions must be treated as one exposure.

## Psychology and discipline

Discipline means following the pre-written plan. Boredom, revenge, and fear of
missing out are common reasons to abandon a process. A skipped setup is not a
loss; an unplanned trade is.

## Anti-patterns and review checklist

Common anti-patterns include chasing an extended move, averaging into a losing
position, and treating a single imbalance as a complete thesis. A review
checklist should ask: what was the context, what was the trigger, where was the
invalidation, and did execution follow the plan?

## Cooking recipes

This section is deliberately unrelated to order flow. The compiler should drop
it because it is outside the product taxonomy. It mentions soup, bread, and a
kitchen timer.
