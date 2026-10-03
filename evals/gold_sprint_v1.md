# Gold sprint v1 — 40 notices to check

Open each link, check the key facts, fix what is wrong, then click **✓ Verify**.
About 5 minutes each. You can stop and come back any time; verified notices are saved.

**Per notice**
1. Check every lot: reserve price, EMD, auction date, village, property type, extent, borrower. Check the lot count too.
2. Wrong value → click it and type the right one, copied from the notice.
3. Fact really not in the notice → **not in notice**. A wrong automatic mark → **undo**.
4. Click **✓ Verify**. Do not use **bulk confirm** for these.

When you are done (or have checked 20+), tell Claude "gold done". Claude then runs
`python -m evals.export_review_gold` and the v1/v2 comparison.

| # | Notice | Why picked | Lots (expected) | Score now |
|---|---|---|---|---|
| 1 | [756573](https://auction-intelligence-alpha.vercel.app/review#extraction/HINDUJA17775574054730.jpg) | 40+ lots | 40 | 76 |
| 2 | [789207](https://auction-intelligence-alpha.vercel.app/review#extraction/L84293868417811614162710.png) | 40+ lots | 40 | 68 |
| 3 | [839612](https://auction-intelligence-alpha.vercel.app/review#extraction/canb17863680101883.pdf) | 20-39 lots | 36 | 86 |
| 4 | [840710](https://auction-intelligence-alpha.vercel.app/review#extraction/TATA-C11786518116858.jpg) | 20-39 lots | 27 | 56 |
| 5 | [810203](https://auction-intelligence-alpha.vercel.app/review#extraction/Screenshot-6-7-2026-122425-178342168739.jpeg) | 20-39 lots | 21 | 56 |
| 6 | [809640](https://auction-intelligence-alpha.vercel.app/review#extraction/Screenshot-6-7-2026-122351-17833408515362.jpeg) | 20-39 lots | 28 | 36 |
| 7 | [793561](https://auction-intelligence-alpha.vercel.app/review#extraction/cbii1781615010359.jpg) | tamil | 2 | 70 |
| 8 | [855497](https://auction-intelligence-alpha.vercel.app/review#extraction/a2ae2fbb-fb2c-489b-ab2c-3af0d5d2771c17879667353764.jpg) | tamil | 1 | 96 |
| 9 | [856710](https://auction-intelligence-alpha.vercel.app/review#extraction/ba9f5409-833f-45d2-a63e-fae46f5056ee17880824011233.jpg) | tamil | 1 | 80 |
| 10 | [804697](https://auction-intelligence-alpha.vercel.app/review#extraction/BOB17828277895981.jpg) | tamil | 1 | 100 |
| 11 | [821037](https://auction-intelligence-alpha.vercel.app/review#extraction/5d5c5026-1846-4564-95b2-1002e4e65b2e17845547743291.jpg) | tamil | 3 | 50 |
| 12 | [791340](https://auction-intelligence-alpha.vercel.app/review#extraction/cf9abe3d-90cd-4321-8f09-b0c62e53732117813601368453.jpg) | tamil | 1 | 100 |
| 13 | [822436](https://auction-intelligence-alpha.vercel.app/review#extraction/64f7091d-f822-4a67-ba71-55d32d3d9c9617847122191458.jpg) | poor scan | 1 | 90 |
| 14 | [744445](https://auction-intelligence-alpha.vercel.app/review#extraction/EQ-2-17760732123520.jpg) | poor scan | 8 | 92 |
| 15 | [848164](https://auction-intelligence-alpha.vercel.app/review#extraction/JAMMU17871444732213.jpeg) | poor scan | 1 | 100 |
| 16 | [754116](https://auction-intelligence-alpha.vercel.app/review#extraction/hinudja17772789459474.jpg) | poor scan | 17 | 96 |
| 17 | [785417](https://auction-intelligence-alpha.vercel.app/review#extraction/5e63b05f-97cf-46a8-b130-6f2038e8141c17807325773482.jpg) | poor scan | 1 | 100 |
| 18 | [824486](https://auction-intelligence-alpha.vercel.app/review#extraction/660a1306-07f9-4669-82d0-2b7a4968a83c17848910377458.jpg) | poor scan | 8 | 60 |
| 19 | [824039](https://auction-intelligence-alpha.vercel.app/review#extraction/AXIS-117848684066714.jpg) | stitched | 9 | 66 |
| 20 | [854000](https://auction-intelligence-alpha.vercel.app/review#extraction/axis-1-17878262283510.jpg) | stitched | 10 | 66 |
| 21 | [751228](https://auction-intelligence-alpha.vercel.app/review#extraction/liq-117768686886833.jpg) | stitched | 14 | 60 |
| 22 | [835229](https://auction-intelligence-alpha.vercel.app/review#extraction/UBI-REGN-117859326545149.jpeg) | stitched | 6 | 66 |
| 23 | [865727](https://auction-intelligence-alpha.vercel.app/review#extraction/tata-117889453196029.jpg) | stitched | 26 | 52 |
| 24 | [811768](https://auction-intelligence-alpha.vercel.app/review#extraction/36b6aa1e-0f6b-4be2-885e-95a4ae91cc2817835889129474.jpg) | table, missed lots | 6 | 50 |
| 25 | [748132](https://auction-intelligence-alpha.vercel.app/review#extraction/6b5c24d4-fce4-4d5a-82ea-6bf2da6d5beb17764271651889.jpg) | table, missed lots | 2 | 40 |
| 26 | [775339](https://auction-intelligence-alpha.vercel.app/review#extraction/76f9030d-c6fd-4f45-ac4f-ebc186b9a6a217795206641961.jpg) | table, missed lots | 4 | 66 |
| 27 | [776999](https://auction-intelligence-alpha.vercel.app/review#extraction/HINDUJA17797277553240.jpg) | table, missed lots | 3 | 90 |
| 28 | [774349](https://auction-intelligence-alpha.vercel.app/review#extraction/nido1779379229583.jpg) | table, missed lots | 6 | 70 |
| 29 | [814024](https://auction-intelligence-alpha.vercel.app/review#extraction/yes17837742833251.jpeg) | table, missed lots | 13 | 86 |
| 30 | [812238](https://auction-intelligence-alpha.vercel.app/review#extraction/yes17836052595621.jpeg) | multi, no table | 2 | 80 |
| 31 | [851987](https://auction-intelligence-alpha.vercel.app/review#extraction/CB1-2-17875734484470.jpg) | multi, no table | 2 | 70 |
| 32 | [793037](https://auction-intelligence-alpha.vercel.app/review#extraction/5440bd85-0771-4901-b58f-f1dba58c623417815447066914.jpg) | multi, no table | 3 | 80 |
| 33 | [785460](https://auction-intelligence-alpha.vercel.app/review#extraction/38952916-6ba0-490d-911c-7846a6fd6a9c17807363309046.jpg) | multi, no table | 2 | 80 |
| 34 | [771679](https://auction-intelligence-alpha.vercel.app/review#extraction/city-2-17791248282954.jpg) | single, table | 1 | 90 |
| 35 | [772050](https://auction-intelligence-alpha.vercel.app/review#extraction/UNITY17791839476817.jpg) | single, table | 1 | 86 |
| 36 | [845677](https://auction-intelligence-alpha.vercel.app/review#extraction/Screenshot-14-8-2026-103655-17869570547467.jpeg) | single, table | 1 | 100 |
| 37 | [812713](https://auction-intelligence-alpha.vercel.app/review#extraction/kvb17836755941553.png) | single, short description | 1 | 76 |
| 38 | [759564](https://auction-intelligence-alpha.vercel.app/review#extraction/bob-117780494357291.jpg) | single, short description | 1 | 70 |
| 39 | [806683](https://auction-intelligence-alpha.vercel.app/review#extraction/iob-2-17829954537069.jpg) | single, short description | 1 | 80 |
| 40 | [792443](https://auction-intelligence-alpha.vercel.app/review#extraction/sri17815066174924.jpg) | single, short description | 1 | 60 |

List made by `scripts/gold_candidates.py` (full pool 2,657), then balanced by hand: random within each group, seed 512.
