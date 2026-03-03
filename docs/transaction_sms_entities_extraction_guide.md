# Transaction SMS Entities Extraction Guide

This document defines only entity semantics for transaction SMS.
It must not include regex-generation rules.

## Purpose

- Detect transaction entities in SMS.
- Return `entities` as ordered items `[{name, value}]`, where `name` is entity type and `value` is extracted entity text value.
- Return `explanation` as free text describing why each entity is used and which part of SMS it represents.
- Preserve the same entity order across SMS text, `entities[i].name`, `entities[i].value`, and `explanation`.
- Keep output stable for downstream regex generation.

## Allowed Entities

- `date#<order>`
- `income`
- `outcome`
- `instrument`
- `fee`
- `cashback`
- `op_income`
- `op_outcome`
- `op_instrument`
- `syncid`
- `syncid#cash`
- `balance`
- `av_balance`
- `acc_instrument`
- `mcc`
- `payee`
- `comment`

## Entity Semantics

1. `date#<order from SMS>`
Date must be emitted as `date#order`, where `order` is based only on component sequence. Component lengths are ignored.
Only calendar date is extracted. Time (`HH:mm`, `HH:mm:ss`) is always ignored.
If SMS contains only time and no date, `date#...` must not be emitted.
Extract only operation date. Dates of service periods, validity ranges, schedules, reminders, or any other side events must not be emitted as `date#...`.
Examples:
- `dd.MM.yyyy` -> `date#dMy`
- `ddMMMyyyy` -> `date#dMy`
- `yyyy-MM-dd` -> `date#yMd`
- `MM/dd/yy` -> `date#Mdy`

2. `income` - strict numeric value, incoming transaction amount
`outcome` - strict numeric value, outgoing transaction amount.
When distinguishing `income` vs `outcome`, use both operation keywords and the operation sign.
Examples:
- SMS text: "Изменение остатка + 9500.00р. **3509 Баланс 46066.58"
  Explanation: Sign `+` before `9500.00` indicates incoming amount, so use `income`.
- SMS text: "Покупка по карте 9500.00р. **3509 Баланс 46066.58"
  Explanation: Keyword "Покупка" indicates an expense, so use `outcome` for `9500.00`.

3. `instrument` - transaction currency near transaction amount (`income` or `outcome`), usually after amount but sometimes before it.

4. `fee` - commission amount, strict numeric value.
`cashback` - positive reward/refund amount returned to the account (opposite to commission).

5. `op_outcome` - original transaction expense amount.
`op_income` - original transaction income amount.
`op_instrument` - original transaction currency near original transaction amount
Example:
"Платеж 358 руб (4$) по карта *1234" means `op_outcome = 4`, `op_instrument = $`, `outcome = 358`, `instrument = руб`.

6. `syncid` - account/card identifier; if it is a long card/account number, take only last 4 digits.
`syncid#cash` - use when SMS describes a cash withdrawal/deposit operation.
`syncid` is always identifier of our own account/card (the product this SMS is about).
Never extract counterparty/account identifiers as `syncid`.
At most one `syncid` (or one `syncid#cash`) can be present in a single SMS.

7. `balance` - account balance (can be negative), strict decimal number usually with two digits after decimal separator; must not include trailing spaces.
`av_balance` - available amount on account, strict decimal number (can be negative) with two digits after decimal separator; must not include trailing spaces. Usually this is balance plus credit limit, and banks often label it as "Доступно: xxx.xx".
`acc_instrument` - account currency near balance or available balance

8. `mcc` - merchant category code
9. `payee` - merchant/store/counterparty/person
`payee` is always counterparty for both incoming and outgoing transactions, independent of operation type.
10. `comment` - variable operation note/purpose text.
Do NOT extract stable operation-type keywords or boilerplate anchor phrases into `comment`.
Anchor keywords are fixed tokens that are typically unchanged across SMS of the same template and are used to identify operation type/direction.
Examples of anchor keywords/phrases that are usually NOT `comment`: "Покупка", "Оплата", "Списание", "Зачисление", "Отмена операции", "Перевод", ...
Extract `comment` only when there is clearly variable free text or a dedicated comment/message field (for example: "Сообщение: ...", "Комментарий: ...", or other clearly user-specific note text).

## Additional Push Header Data After "║"

After the "║" symbol, additional information may appear that is appended from the push-notification header and related to the SMS.
This information can be useless or meaningful.

Rules:
- If it is only the sender bank name, treat it as useless and do not use it for entity extraction.
- If its meaning is unclear, ignore it.
- Only if this additional information adds meaningful content beyond the main SMS text (before "║"), it may be used to extract extra entities.
- If entities are extracted from both parts (before and after "║"), keep one global order of appearance across the full SMS text.

## Ordering Rules

- Keep entity order strictly as in SMS text.
- Do not reorder entities by business importance.
- Entity order must match across SMS text, `explanation`, and `entities` item sequence.
- In each item, `entities[i].name` and `entities[i].value` must describe the same SMS fragment.
- The order is token-level and literal. If currency appears before amount (for example `Rs 15.00`), emit `instrument` before `income/outcome`.

## Mandatory Transaction Evidence

For transaction extraction result to be valid, it must contain:

balance-change evidence:
- at least one of `income`, `outcome`, `balance`, `av_balance`

If no such evidence exists, treat SMS as non-transaction for this task and return:
- `entities: []`
- `explanation`: brief reason why there is no valid transaction evidence.

## Extraction Scope

- Extract only entities listed in **Allowed Entities**.
- Never extract any additional attributes, metadata, service info, phone numbers, URLs, or other non-entity fields.

## Entity Recognition Examples

Example #1:
sms_text: 'VISA5567 11:01 Покупка 1000р AIST-PRESS Баланс: 18894.42р'
entities: [{name: syncid, value: 5567}, {name: outcome, value: 1000}, {name: instrument, value: р}, {name: payee, value: AIST-PRESS}, {name: balance, value: 18894.42}, {name: acc_instrument, value: р}]
explanation: 'syncid: 5567 - our card identifier, outcome: 1000 - purchase amount, instrument: р - transaction currency, payee: AIST-PRESS - merchant, balance: 18894.42 - account balance, acc_instrument: р - balance currency. Time 11:01 is ignored.'

Example #2:
sms_text: 'Карта *1420: Оплата 264.00 RUR; MCDONALDS 11025; дата: 27.08.2016 10:17, доступно 93477.07 RUR. ВТБ24'
entities: [{name: syncid, value: 1420}, {name: outcome, value: 264.00}, {name: instrument, value: RUR}, {name: payee, value: MCDONALDS 11025}, {name: date#dMy, value: 27.08.2016}, {name: av_balance, value: 93477.07}, {name: acc_instrument, value: RUR}]
explanation: 'syncid: 1420 - our card identifier, outcome: 264.00 - outgoing payment amount, instrument: RUR - transaction currency, payee: MCDONALDS 11025 - merchant, date#dMy: 27.08.2016 - operation date, av_balance: 93477.07 - available balance, acc_instrument: RUR - account currency. Time 10:17 is ignored.'

Example #3:
sms_text: 'Vneshniy perevod. Drugoy bank. Karta *0047. Summa 3941334.95 RUB. TCSBank. 06.03.2013 16:47. Dostupno 0.00 RUB. www.tcsbank.ru'
entities: [{name: syncid, value: 0047}, {name: outcome, value: 3941334.95}, {name: instrument, value: RUB}, {name: date#dMy, value: 06.03.2013}, {name: av_balance, value: 0.00}, {name: acc_instrument, value: RUB}]
explanation: 'syncid: 0047 - our card identifier, outcome: 3941334.95 - outgoing transfer amount, instrument: RUB - transaction currency, date#dMy: 06.03.2013 - operation date, av_balance: 0.00 - available balance, acc_instrument: RUB - account currency. Time 16:47 is ignored.'

Example #4:
sms_text: 'BofA: Double Move Llc sent you $300.00 for marketing services'
entities: [{name: payee, value: Double Move Llc}, {name: instrument, value: $}, {name: income, value: 300.00}, {name: comment, value: marketing services}]
explanation: 'payee: Double Move Llc - counterparty, instrument: $ - transaction currency, income: 300.00 - incoming amount, comment: marketing services - operation comment.'

Example #5:
sms_text: 'ICICI Bank Acct XX964 debited for Rs 15.00 on 17-Mar-25; ADI LAKSHMI CHA credited. UPI:053261336299. Call 18002662 for dispute. SMS BLOCK 964 to 9215676766.'
entities: [{name: syncid, value: 964}, {name: instrument, value: Rs}, {name: outcome, value: 15.00}, {name: date#dMy, value: 17-Mar-25}, {name: payee, value: ADI LAKSHMI CHA}]
explanation: 'syncid: 964 - our account identifier, instrument: Rs - transaction currency (appears before amount), outcome: 15.00 - outgoing amount, date#dMy: 17-Mar-25 - operation date, payee: ADI LAKSHMI CHA - counterparty. Service data (UPI, phone numbers) is ignored.'

Example #6:
sms_text: 'Не выставлен счёт для оплаты. Проверка через каждые 3 дня. Подробнее у поставщика услуг ║ Автоплатёж ""Вода"" не исполнен '
entities: []
explanation: 'No valid transaction evidence in this message.'

Example #7:
sms_text: 'С Вашей карты **** 1234 произведен перевод на карту № **** 4567 на сумму 15000,00 RUB. ║ Операция в Сбербанк Онлайн'
entities: [{name: syncid, value: 1234}, {name: outcome, value: 15000,00}, {name: instrument, value: RUB}]
explanation: 'syncid: 1234 - our card identifier, outcome: 15000,00 - outgoing transfer amount, instrument: RUB - transaction currency. Counterparty card 4567 is ignored because it is not our account/card.'

Example #8:
sms_text: 'MIR-4120 01:52 Оплата уведомлений 59р. Следующее списание 10.08.25. Баланс: 8264,43р ║ Списание за уведомления об операциях'
entities: [{name: syncid, value: 4120}, {name: comment, value: Оплата уведомлений}, {name: outcome, value: 59}, {name: instrument, value: р}, {name: balance, value: 8264,43}, {name: acc_instrument, value: р}]
explanation: 'syncid: 4120 - our card identifier, comment: Оплата уведомлений - operation purpose, outcome: 59 - current outgoing amount, instrument: р - transaction currency, balance: 8264,43 - account balance, acc_instrument: р - balance currency. Time 01:52 is ignored, and 10.08.25 is the next scheduled charge date, not the current operation date.'

Example #9:
sms_text: 'VISA8523 15.06.22 зачислен перевод 24000р на запрос из Альфа Банк через СБП от Леонид Юрьевич С. Сообщение: Перевод по СБП'
entities: [{name: syncid, value: 8523}, {name: date#dMy, value: 15.06.22}, {name: income, value: 24000}, {name: instrument, value: р}, {name: payee, value: Леонид Юрьевич С.}, {name: comment, value: Перевод по СБП}]
explanation: 'syncid: 8523 - our card identifier, date#dMy: 15.06.22 - operation date, income: 24000 - incoming transfer amount, instrument: р - transaction currency, payee: Леонид Юрьевич С. - counterparty, comment: Перевод по СБП - message text. "Альфа Банк" in "на запрос из ... через СБП" is channel/context and is not extracted as payee.'

Example #10:
sms_text: 'Karta *4706. Otmena operacii Pokupka 30.00RUR. Date 2016-06-26 19:07:41. Dostupno 22976.38RUR'
entities: [{name: syncid, value: 4706}, {name: income, value: 30.00}, {name: instrument, value: RUR}, {name: date#yMd, value: 2016-06-26}, {name: av_balance, value: 22976.38}, {name: acc_instrument, value: RUR}]
explanation: 'syncid: 4706 - our card identifier, income: 30.00 - refunded amount, instrument: RUR - transaction currency, date#yMd: 2016-06-26 - operation date (time 19:07:41 is ignored), av_balance: 22976.38 - available balance, acc_instrument: RUR - account currency. "Otmena operacii" and "Pokupka" are anchor operation keywords for this SMS template and are not extracted as comment.'