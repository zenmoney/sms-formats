# Transaction SMS Regex Writing Guide

This document defines how to write `regex` for extracting transaction entity values from SMS.

## Purpose

Input is already known:
- `sms_text`
- `entities` (strict order from SMS)
- `explanation` (same order)

Output must be:
- `regex` that matches the SMS

## Input Contract

- `entities` and `explanation` are the source of truth.
- Regex generation must not redefine entity set/order.

## Critical Rule: Explanation Is Executable Parsing Spec

`explanation` is not a comment; it is a strict parsing specification.

When generating regex, implement exactly the same parsing decisions described in `explanation`, including:
- what is extracted,
- what is ignored,
- why a specific fragment maps to a specific entity.

Examples of constraints from `explanation` (from entity guide):
- "Time 10:17 is ignored" -> regex must not capture time as date/balance tail.
- "Counterparty card 4567 is ignored" -> regex must not capture that card as `syncid`.
- "Instrument appears before amount" -> capture order must follow that literal order.
- "Scheduled next charge date is not operation date" -> regex must not capture that date as `date#...`.

If regex behavior and `explanation` disagree, regex is invalid.

## General Regex Construction Rules

1. Use regular (unnamed) capture groups.
2. Capture group order must strictly match `entities`.
3. Keep regex generalized:
   - do not hardcode amounts,
   - do not hardcode currencies,
   - do not hardcode merchants/counterparties,
   - do not hardcode account/card numbers.
4. Use flexible connectors (`.*?`) between semantic anchors, but keep entity groups precise.
5. Do not use global flags like `(?i)` for the whole pattern; localize where needed.
6. Do not start pattern with `^` (project rule).
7. Include transaction keywords in regex (purchase/payment/transfer/debit/refund/etc.) if they exist in SMS.
8. Use only real lexical anchors that are present in the given SMS text.
   - Do not invent alternative card brands, operation verbs, labels, or synonyms that are absent in the SMS.
   - Do not expand with speculative variants like extra brand names (`MIR`, `MASTERCARD`, etc.) if SMS contains only `VISA`.
   - Do not add artificial alternatives for words/labels not present in SMS (for example `message` if SMS has only `Сообщение`).
9. Generalize only within the shape observed in the current SMS token.
   - Good: token `RUB` -> `[A-Za-z]{3}` (same observed shape: 3 Latin letters).
   - Bad: token `RUB` -> `[A-Za-zА-Яа-я]{1,5}` (invented script/length not supported by the SMS token).
   - This keeps regex non-hardcoded while avoiding speculative broadening.
10. Do not prepend regex with leading `.*` / `.*?` unless there is a strict technical reason.
   - Regex search already matches substrings by default, so leading wildcards are usually redundant.
11. Do not append regex with trailing `.*` / `.*?` unless there is a strict technical reason.
   - Tail wildcards are usually redundant and make patterns less precise.

## Entity-Specific Extraction Rules

### Numeric entities
`income`, `outcome`, `fee`, `balance`, `av_balance`, `op_income`, `op_outcome`

- Group must start with a digit.
- Do not capture punctuation/whitespace noise before the number.
- Do not capture trailing non-numeric tails.
- Support formats: `1000`, `1 000`, `1000.00`, `1000,00`.

Recommended base number group:
- `(\d[\d\s.,]*)`

For `balance` / `av_balance`:
- If time follows (`9141.03 15:43`), capture only `9141.03`.
- Useful boundary pattern:
  - `(-?\d[\d\s.,]*[.,]\d{2})(?=\s|$)`

### `syncid` / `syncid#cash`

- Extract only our account/card identifier.
- If format is `1234**5678`, capture only last 4 digits (`5678`) for `syncid`.
- For cash withdraw/deposit messages, use `syncid#cash` instead of `syncid`.

### `instrument` / `acc_instrument`

- `instrument`: currency near operation amount.
- `acc_instrument`: currency near `balance`/`av_balance`.
- Respect literal token order in SMS (currency may be before amount).

### `date#...`

- Capture only operation date matching already-selected `date#...` entity.
- Do not capture time as date, and do not let time leak into number/date captures.

### `payee` / `comment`

- `payee`: merchant/counterparty text fragment.
- `comment`: operation comment fragment.
- Avoid greedy groups that swallow neighboring entities.

## Forbidden Anti-Patterns

1. Hardcoded currency literals:
   - bad: `(RUB)`, `(₽)`, `(USD)`
2. Hardcoded merchant names:
   - bad: `(OZON)`, `(PYATEROCHK)`
3. Hardcoded amounts:
   - bad: `(1468,88)`
4. Hardcoded account/card numbers:
   - bad: `(8587)`
5. Capture order differing from `entities`.
6. Regex behavior that contradicts `explanation`.
7. Invented alternatives not present in SMS text (extra brands, verbs, labels, synonyms).
8. Speculative broadening of token shape (alphabet/length/class) beyond what is observed in SMS.

## Incorrect Regex Examples From Practice

Case SMS fragment:
`VISA8523 ... 24000р ... Сообщение: Перевод по СБП`

1) Invented brand/keyword alternatives that do not exist in SMS
- bad:
  `(?:VISA|MIR|MASTERCARD)...(?:зачислен|поступил|перевод|пополн)...(?:Сообщение|Message):`
- why invalid:
  `MIR`, `MASTERCARD`, `поступил`, `Message` are not present in this SMS.

2) Speculative token-shape broadening
- bad:
  `([A-Za-zА-Яа-я]{1,5})` for currency when SMS has `RUB`
- better:
  `([A-Z]{3})`
- why invalid:
  broadening to Cyrillic and variable length is not justified by observed token shape.

3) Redundant leading wildcard
- bad:
  `.*?VISA(\d{4})...`
- better:
  `VISA(\d{4})...`
- why invalid:
  leading `.*?` is usually redundant for search-based matching and reduces precision/readability.

4) Redundant trailing wildcard
- bad:
  `...([A-Za-z]{3}).*?`
- better:
  `...([A-Za-z]{3})`
- why invalid:
  trailing wildcard usually adds no value and makes matching less strict than needed.

5) Hardcoded currency, merchant, card, and amount (from legacy negative examples)
- sms_text:
  `Покупка, карта *8587. 1468,88 RUB. PYATEROCHK. Доступно 32095,98 RUB`
- bad:
  `^Покупка,\s+карта\s+\*(8587)\.\s+(1468,88)\s+(RUB)\.\s+(PYATEROCHK)\.\s+Доступно\s+(32095,98)\s+(RUB)$`
- why invalid:
  hardcoded card number, amount, currency, merchant, and full-line anchors make regex non-generalizable.
- better direction:
  keep operation keywords/structure, but replace changing values with generalized capture groups.

6) Hardcoded merchant with partially generalized rest (from legacy negative examples)
- sms_text:
  `Покупка, карта *8587. 1782 RUB. MAGIYA SVETA. Доступно 29695,98 RUB`
- bad:
  `Покупка,\s+карта\s+\*(\d{4})\.\s+([0-9]{4})\s+([A-Z]{3})\.\s+(MAGIYA SVETA)\.\s+Доступно\s+(-?[0-9][\d\s]*,[0-9]{2})\s+([A-Z]{3})`
- why invalid:
  merchant is hardcoded and amount pattern is too narrow (`[0-9]{4}`), so regex is brittle.
- better direction:
  keep stable lexical anchors from SMS and generalize merchant/amount as entity capture groups.

## Generation Workflow

1. Take `entities` as fixed capture order.
2. Read `explanation` and convert each explanation item into exact extraction behavior.
3. Choose group pattern per entity type (number/currency/date/text).
4. Build contextual anchors from SMS keywords around each entity.
   - Anchor terms must be copied from the actual SMS text (or exact safe substrings of it), not guessed.
5. Connect anchors with flexible glue (`.*?`) while preserving strict capture semantics.
6. Validate against SMS and explanation.

## Final Validation Checklist

- [ ] Number of capture groups equals `len(entities)`.
- [ ] Capture group order equals `entities` order.
- [ ] Regex extraction behavior matches `explanation` decisions exactly.
- [ ] No hardcoded currency/merchant/amount/account values.
- [ ] Transaction keywords are included when present in SMS.
- [ ] All anchor words/tokens are present in SMS text (no invented synonyms/brands/labels).
- [ ] Any generalization stays within observed token shape (no speculative alphabet/length expansion).
- [ ] `income/outcome` logic is not sign-ambiguous.
- [ ] `balance/av_balance` do not include time or trailing tail.
