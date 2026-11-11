# Find the cheapest nonstop flight

Find a one-way economy nonstop from SFO to JFK for one adult departing May 10 through May 12, 2030, inclusive. Use USD.

1. Call Fli's `search_dates` with `max_stops` set to `NON_STOP` to find the cheapest eligible date. If prices tie, pick the earliest date.
2. If a date is available, confirm it with `search_flights`, again with `max_stops` set to `NON_STOP`. Pick the cheapest eligible flight on that date. Break flight-price ties by lexicographically comparing the full flight numbers, formed by concatenating `airline_code` and `flight_number` without a space.
3. Use Bash to write `/workspace/report.json`. If you found a flight, write a JSON object with exactly these fields:

   | Field | Value |
   | --- | --- |
   | `status` | `"found"` |
   | `origin` | `"SFO"` |
   | `destination` | `"JFK"` |
   | `departure_date` | ISO date as a string (`YYYY-MM-DD`) |
   | `flight_number` | Full flight number, such as `"AA123"` |
   | `currency` | `"USD"` |
   | `price_cents` | The confirmed fare in whole US cents as a JSON integer |

   If Fli reports no eligible dates, write exactly `{"status":"no_match"}`. Use the supplied Fli tools as your source of flight data.
