# Fill model

Provisional. The queue was not recorded on the historical candles or trade
prints, so every maker fill below is an assumption. Each line is marked
**needs queue data to validate**.

Measured context, small sample (1,699 consecutive snapshot pairs, 22
markets, before T-50): the best YES bid changes price in 83% of 5-second
steps. When the price holds, top size shrinks about as often as it grows,
and the median shrink is 42%. That is not a stable queue you can walk
forward. The spread is 1c at the median and at the 90th percentile from
T-15m through T-1m.

## Conservative (default)

- **Maker, post-only.** A resting bid fills only when a trade prints
  strictly through our price. A YES bid at P needs a YES print strictly
  below P. A NO bid at P needs a YES print strictly above `1-P`. A print
  exactly at our price does not fill. **needs queue data to validate**
- **No queue advancement.** Size sitting ahead of us is not decayed when
  later prints trade at our price, and we do not move up the queue when
  the top size shrinks. The 42% median shrink is why. **needs queue data to validate**
- **Size.** The fill is `min(our remaining, that print's size)`. The print
  may have traded against orders in front of us before it went through.
  **needs queue data to validate**
- **Adverse-selection haircut.** Default 3 cents per maker contract,
  subtracted from settlement PnL. The fade study's sweep markouts were
  about 3c to 7c within 60 seconds. The summary reprints the same fills at
  0, 3, 4, 5, 6, and 7 cents. **needs queue data to validate**
- **Taker.** Only if an order is allowed to cross. Fill at the ask of the
  side being bought (the 1c spread means you pay the far touch, not the
  mid), size capped by visible size at that touch. If no size is visible,
  the cap is 1 contract. Fee is the live `FeeSchedule` taker fee,
  `ceil(0.07 * C * P * (1-P))` to the schedule's quantum. The live
  strategies are post-only, so a normal replay does not take.
  **needs queue data to validate**
- **Maker fee.** 0 while `fee_type` is `quadratic` (the live KXBTC15M
  schedule). Set `FillModel.maker_fee_type` to `quadratic_with_maker_fees`
  to charge the maker rate. **needs queue data to validate** only for the
  fee type; the formula is the live one.

## Optimistic

- Maker fills on a print at our price or through it. That is the
  zero-queue case and disagrees with the recorder note above.
  **needs queue data to validate**
- Haircut defaults to 0. Pass `--haircut-cents` to put one back.
- Taker rule is unchanged: still cross to the ask plus the fee.

## What is not modeled

- Queue position, cancels ahead of us, or our own place in line.
- Partial level sweeps that trade at our price without clearing it.
- The 83% of snapshots where the touch price moves (those just reprice
  the book the strategy sees on the next decision).
- Self-trades. The backtest has one participant.
- Any live order. This process does not read API keys.
