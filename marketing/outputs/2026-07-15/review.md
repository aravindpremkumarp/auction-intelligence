# Content review — 2026-07-15

638 live auctions (of 2434 tracked). Data as of 2026-07-15T17:45:38.303810Z.

**Staged drafts only — nothing is published.** Review, tweak, and post manually or via your scheduler. Verify each fact against the linked notice.

**5 card image(s) staged** — the hook is burned on as the headline. Render them with:
```bash
python marketing/render_social.py --template price-drop-1080x1350 --data marketing/outputs/2026-07-15/cards/01-798444.json --out marketing/outputs/2026-07-15
python marketing/render_social.py --template price-drop-1080x1350 --data marketing/outputs/2026-07-15/cards/02-802076.json --out marketing/outputs/2026-07-15
python marketing/render_social.py --template deal-of-the-day-1080 --data marketing/outputs/2026-07-15/cards/03-788297.json --out marketing/outputs/2026-07-15
python marketing/render_social.py --template deal-of-the-day-1080 --data marketing/outputs/2026-07-15/cards/04-794731.json --out marketing/outputs/2026-07-15
python marketing/render_social.py --template deal-of-the-day-1080 --data marketing/outputs/2026-07-15/cards/05-797758.json --out marketing/outputs/2026-07-15
```

**6 reel(s) staged** — 12s 9:16 videos; the selected reel hook is the first frame. Render with:
```bash
python marketing/render_reel.py --data marketing/outputs/2026-07-15/reels/00-stats.json --template stats-reel-1080x1920 --out marketing/outputs/2026-07-15
python marketing/render_reel.py --data marketing/outputs/2026-07-15/reels/01-798444.json --template deal-reel-1080x1920 --out marketing/outputs/2026-07-15
python marketing/render_reel.py --data marketing/outputs/2026-07-15/reels/02-802076.json --template deal-reel-1080x1920 --out marketing/outputs/2026-07-15
python marketing/render_reel.py --data marketing/outputs/2026-07-15/reels/03-788297.json --template deal-reel-1080x1920 --out marketing/outputs/2026-07-15
python marketing/render_reel.py --data marketing/outputs/2026-07-15/reels/04-794731.json --template deal-reel-1080x1920 --out marketing/outputs/2026-07-15
python marketing/render_reel.py --data marketing/outputs/2026-07-15/reels/05-797758.json --template deal-reel-1080x1920 --out marketing/outputs/2026-07-15
```
Reels render silent by design — **add trending audio in-app** (Instagram/TikTok) when publishing; native audio drives reach.

## Draft 1 — price_drop — Karur
*South Indian Bank Land Auction in ManmangalamTaluk, Karur* · ₹31.9L → ₹31.0L (−2.7%) · ends 2026-07-16T11:30:00Z · [source notice](https://www.eauctionsindia.com/properties/798444) · `798444`

₹31.9L → ₹31L. same karur plot, back up for auction.

a residential plot in karur's manmangalam taluk is back on the block. south indian bank cut the reserve from ₹31.9L to ₹31L — a 2.7% drop. emd to bid is ₹3.1L, and the auction opens 16 july. worth checking why the first round didn't clear before you place a number.

more research-ready listings like this on auctionscope.in.

tags: #bankauction #sarfaesi #reauction #karur #auctionscope · image: yes — ₹31.9L → ₹31L: karur plot, round two

hook: `contrast` · alternatives:
- the bank wanted ₹31.9L for this karur plot. today: ₹31L.
- would you bid ₹31L on a karur plot on its second round?
card: `price-drop-1080x1350` · headline burned on: *₹31.9L → ₹31L: karur plot, round two*
reel: `deal-reel-1080x1920` · first frame: *₹31.9L → ₹31L / karur plot, round two.* · end card: *would you bid on a plot that didn't sell the first time?*

**pinned comment:**
> full listing, reserve price breakdown and location checks: auctionscope.in. this is information only, not legal advice — it's a SARFAESI bank e-auction; verify the reserve, EMD, possession and encumbrances with the bank before bidding. would you bid on a plot that didn't sell the first time?

`video_title: karur plot reserve drops to ₹31L | south indian bank e-auction` · `location_tag: Karur, Manmangalam Taluk` · `alt_text: deal card: karur plot, reserve cut from ₹31.9L to ₹31L, south indian bank, emd ₹3.1L, auction 16 jul.`

## Draft 2 — price_drop — Vellore
*Bank of Baroda Land Auction in Arakonam, Vellore* · ₹45.6L → ₹41.0L (−10.0%) · ends 2026-07-18T14:00:00Z · [source notice](https://www.eauctionsindia.com/properties/802076) · `802076`

the bank wanted ₹45.6L for this vellore plot. today: ₹41L.

bank of baroda cut the reserve on a residential plot in arakonam, vellore by 10% — from ₹45.6L to ₹41L. emd to bid is ₹4.1L. the auction opens 18 july, three days out. reserve cuts like this often follow a first round with no winning bid — worth reading the notice before you decide your number.

more of these on auctionscope.in.

tags: #bankauction #reauction #reserveprice #vellore #auctionscope · image: yes — ₹45.6L → ₹41L: vellore plot, 10% cut

hook: `process` · alternatives:
- ₹45.6L → ₹41L. same vellore plot, re-listed.
- you don't need ₹41L in cash for this — the emd is ₹4.1L.
card: `price-drop-1080x1350` · headline burned on: *₹45.6L → ₹41L: vellore plot, 10% cut*
reel: `deal-reel-1080x1920` · first frame: *₹45.6L → ₹41L / 10% cut. vellore plot.* · end card: *what would make you pass on a re-listed property?*

**pinned comment:**
> full listing, reserve history and location checks: auctionscope.in. information only, not legal advice — this is a SARFAESI bank e-auction; verify reserve, EMD, possession and encumbrances with the bank before bidding. what would make you pass on a re-listed property?

`video_title: vellore plot reserve cut 10% to ₹41L | bank of baroda e-auction` · `location_tag: Vellore, Arakonam` · `alt_text: deal card: vellore plot, reserve cut 10% from ₹45.6L to ₹41L, bank of baroda, emd ₹4.1L, auction 18 jul.`

## Draft 3 — closing_soon — Krishnagiri
*Canara Bank Land And Building Auction in krishnagiri, Krishnagiri* · ₹189.0L · ends 2026-07-15T10:30:00Z · [source notice](https://www.eauctionsindia.com/properties/788297) · `788297`

would you bid ₹189L on a property you've only seen as a pdf?

canara bank's land-and-building auction in krishnagiri closes today, reserve ₹189L, emd ₹18.9L. it's a big number to commit to sight-unseen — the notice has the survey details, but groundwater, access and encumbrances are yours to check before you place a bid.

listing details on auctionscope.in.

tags: #bankauction #sarfaesi #landauction #krishnagiri #auctionscope · image: yes — would you bid ₹189L sight-unseen?

hook: `question` · alternatives:
- auction closes today. ₹189L krishnagiri property, one shot.
- hunting land and building in krishnagiri? canara bank has one at ₹189L today.
card: `deal-of-the-day-1080` · headline burned on: *would you bid ₹189L sight-unseen?*
reel: `deal-reel-1080x1920` · first frame: *₹189L / seen only as a pdf.* · end card: *would you bid ₹189L on a property you've only seen as a pdf?*

**pinned comment:**
> full listing and location checks: auctionscope.in. information only, not legal advice — this is a SARFAESI bank e-auction; verify reserve, EMD, possession and encumbrances with the bank before bidding. would you bid ₹189L on a property you've only seen as a pdf?

`video_title: ₹189L krishnagiri property auction closes today | canara bank` · `location_tag: Krishnagiri, Tamil Nadu` · `alt_text: deal card: krishnagiri land and building, reserve ₹189L, canara bank, emd ₹18.9L, auction closes today.`

## Draft 4 — closing_soon — Virudhunagar
*Canara Bank Land And Building Auction in Virudhunagar, Virudhunagar* · ₹4.2L · ends 2026-07-15T11:00:00Z · [source notice](https://www.eauctionsindia.com/properties/794731) · `794731`

you don't need ₹4.2L in cash to bid — the emd here is ₹0.42L.

canara bank's land-and-building auction in virudhunagar closes today, reserve just ₹4.2L. the entry ticket — the emd — is ₹0.42L, not the full reserve. a low reserve doesn't mean low risk though; the notice will tell you what liens or occupants come with the property.

more entry-level listings on auctionscope.in.

tags: #bankauction #sarfaesi #emd #virudhunagar #auctionscope · image: yes — emd here is just ₹0.42L

hook: `myth` · alternatives:
- hunting a cheap plot in virudhunagar? a bank just listed one at ₹4.2L.
- ₹4.2L reserve, ₹0.42L emd — the gap between looking and bidding.
card: `deal-of-the-day-1080` · headline burned on: *emd here is just ₹0.42L*
reel: `deal-reel-1080x1920` · first frame: *₹0.42L / that's the entry ticket.* · end card: *would a ₹0.42L emd change how you look at bank auctions?*

**pinned comment:**
> full listing and checks: auctionscope.in. information only, not legal advice — this is a SARFAESI bank e-auction; verify reserve, EMD, possession and encumbrances with the bank before bidding. would a ₹0.42L emd change how you look at bank auctions?

`video_title: ₹4.2L virudhunagar auction, emd just ₹0.42L | canara bank` · `location_tag: Virudhunagar, Tamil Nadu` · `alt_text: deal card: virudhunagar land and building, reserve ₹4.2L, canara bank, emd ₹0.42L, auction closes today.`

## Draft 5 — cheapest — Thiruvarur
*Motilal Oswal Home Finance Ltd Plot Auction in Mannargudi Taluk, Thiruvarur* · ₹1.5L · ends 2026-07-27T11:00:00Z · [source notice](https://www.eauctionsindia.com/properties/797758) · `797758`

hunting a plot under ₹2L? a bank just listed one at ₹1.5L.

motilal oswal home finance is auctioning a plot in mannargudi taluk, thiruvarur — reserve ₹1.5L, emd just ₹15,000. it's one of the smallest tickets on the list right now. auction opens 27 july, giving time to check access, encumbrances and the survey number before bidding.

more listings like this on auctionscope.in.

tags: #bankauction #sarfaesi #cheapestlisting #thiruvarur #auctionscope · image: yes — ₹1.5L plot: cheapest on the list

hook: `callout` · alternatives:
- would you bid ₹1.5L on a plot you've only seen as a pdf?
- you don't need ₹1.5L in cash to start — the emd is just ₹15,000.
card: `deal-of-the-day-1080` · headline burned on: *₹1.5L plot: cheapest on the list*
reel: `deal-reel-1080x1920` · first frame: *₹1.5L / cheapest live listing.* · end card: *would ₹15,000 be a low enough bar for you to look closer at a listing?*

**pinned comment:**
> full listing and checks: auctionscope.in. information only, not legal advice — this is a SARFAESI bank e-auction; verify reserve, EMD, possession and encumbrances with the bank before bidding. would ₹15,000 be a low enough bar for you to look closer at a listing?

`video_title: ₹1.5L thiruvarur plot auction | cheapest bank listing right now` · `location_tag: Thiruvarur, Mannargudi Taluk` · `alt_text: deal card: thiruvarur plot, reserve ₹1.5L, motilal oswal home finance, emd ₹15,000, auction 27 jul.`

## Editor notes
picked 2 price_drop (karur, vellore), 2 closing_soon (krishnagiri, virudhunagar) and 1 cheapest (thiruvarur) to cover 5 distinct cities and 5 distinct hook mechanisms (contrast, process, question, myth, callout) with no repeats. skipped the three JM Financial ARC Periyakulam cheapest listings to avoid duplicating city/bank. closing_soon candidates all share an auction_start earlier on 2026-07-15 (today, per the snapshot timestamp) — flag for the editor to confirm posting time is still before the slot if scheduling slips.
