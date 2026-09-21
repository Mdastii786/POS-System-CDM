ConvergEX POS - DEPLOY PACKAGE
==============================

BUILD 31 (8 Sep 2026) - Date Bought on shop purchases
  - SHOP & LOSSES: the purchase capture row now has a Date Bought box.
    It sits on today's date by itself - leave it alone for normal buys.
    Capturing an OLD slip from weeks or months back? Pick the real date
    and the purchase lands on THAT day, the day the money actually went
    out. After every save the box hops back to today on its own, so the
    next slip can never go in with a stale date by accident.
  - YES, it reflects on your reports: the slip shows in that DAY's
    daily Excel (SHOP PURCHASES section) and in that MONTH's monthly
    report (MONEY OUT & LOSSES + the purchases total). It never
    touches today's figures, this month's figures, or the cash-up.
    Pull the old day or old month and it is right there.
  - The purchases list on the page shows the real date per row, so you
    can see at a glance which slips were back-dated.
  - Locks that keep the books honest: a date in the future is refused,
    nonsense dates are refused, and a month you already archived
    (locked for good) is refused with a plain message - for those, date
    the slip in the open month and write the real date under
    'What was bought'.
  - Bonus fix found while testing: dates on the Shop & Losses page and
    the top bar now always show day/month/year the SA way, no matter
    what the laptop's browser thinks the order should be.
  - Checked to the T again: 183 behind-the-scenes checks + 244 real
    click-through checks all pass.

BUILD 30 (26 Aug 2026) - the last and final update
  - SALES page, cleaned up your way: the top box just says Customer,
    the option that used to be WALK-IN now reads
    "Cash / Card Sale (Walk-in)", the item box just says ITEM and the
    closed box says "- ITEM -" until you pick, and the note box just
    says Note - you add whatever you want remembered, manually.
  - TODAY'S SALES tray reads like a person wrote it: short plain lines,
    no wall of text. Every row is time + date (the date sits right under
    the time), customer, how they paid, total. Items are ONLY a
    dropdown now: tap ITEMS (n) on any row and the full breakdown drops
    open underneath, tap again and it folds away. Nothing else crowds
    the row.
  - NEW day picker in the tray: a little Day calendar sits at the top.
    Pick any old day and you get that day's sales to LOOK at - neat and
    tidy, viewing only. Old sales still count in every report, in the
    month-end numbers, and in who owes what - nothing is hidden or
    lost, it is just packed away out of today's face. Returns and
    voids still only ever happen on the day itself, same as always.
  - RETURN / EXCHANGE popup fixed properly: the "Qty Coming Back"
    boxes start BLANK on purpose now. Nothing is pre-picked, so a slip
    of the finger can never return something by accident. You pick the
    qty yourself, and if you hit the record button with everything
    still blank it stops and tells you why instead of doing nothing
    quietly.
  - Checked to the T: 171 behind-the-scenes checks + 237 real
    click-through checks all pass, on top of every check from the
    earlier builds. Money to the cent, stock both ways, statements,
    card fees, accounts - every path walked end to end.

BUILD 29 (26 Aug 2026)
  - TODAY'S SALES tray: no more clipping. The drawer is wider (940px),
    long item lists wrap instead of running off the edge, and the
    RETURN / EXCHANGE (+ VOID) buttons are always fully visible again.
  - STALE-SCREEN KILLER: the app now tells browsers never to hang on to
    old copies of its pages, so updates show up right away on every
    laptop - no more Ctrl+F5 rituals. (This is also why the preview
    kept showing the old tray.)
  - The RETURN / EXCHANGE popup re-reads the live shelf every single
    time it opens - never prices a return off stale stock.
  - Multi-item sales get an ITEMS (n) toggle: tap it and the full line
    breakdown drops open under the row - every line with qty, price each,
    subtotal, card fee if any, and the sale total.
  - The swap/exchange engine behind those buttons is the one that was
    always there: money difference to the cent, stock moves both ways,
    account balances net out, and a RETURN row keeps the paper trail on
    the statement. It was never broken - the drawer was hiding it.
  - NEW sale note box on the Sales page (above the pay buttons): type
    anything per sale, e.g. "Sipho's sister bought on his behalf". It
    shows in the TODAY'S SALES tray AND on the customer's statement under
    the items. It clears itself after every sale - never sticky. Notes
    from swaps and old-debt entries show on statements too now.

BUILD 28 (26 Aug 2026)
  - SALES: customer search actually PICKS THEM UP now. Type any part of
    the name (any case) and the list narrows live; hit ENTER and the app
    picks the exact name, or the first line in the filtered list if there
    are a few. No match? It tells you and points at + Add New. After the
    pick you land straight in the item search box.
  - SALES: nothing is pre-chosen anymore. The customer box sits on
    "- who is buying? -" until YOU decide: type + ENTER, or pick from the
    list (WALK-IN is findable the same way - type "walk"). CASH/CARD ask
    "Who is paying?" first if nothing is chosen; ON ACCOUNT still wants a
    name, never WALK-IN.
  - Wording pass: nerdy words like "reconciled" are gone from the app
    screens - plain tuckshop talk all the way. (Still zero em dashes.)

BUILD 27 (26 Aug 2026)
  - CUSTOMERS: new ADD OLD DEBT button (manager login only) next to EDIT.
    Type in what a customer still owes from previous months / the old
    paper book: it goes straight onto their account balance, shows on
    their statement as PREVIOUS BALANCE ADDED (with your note), is counted
    as carried-over debt, and feeds the R2,500 BAN immediately. If the
    entry pushes them to/over the limit you are warned BEFORE saving and
    told again after. NO money moves (it is not a sale - RECEIVE PAYMENT
    still captures the cash when they actually pay).
  - CUSTOMERS: search box above the list - filters live as you type by
    name OR phone number, any case, with a "x of y customers match" count.
    The WhatsApp REMINDERS drawer gets the same any-case search.
  - Two-step safety like everywhere else: ADD OLD DEBT shows an
    ARE YOU SURE review (who, how much, old balance, new balance, BAN
    warning in red) before anything is saved.

BUILD 26 (26 Aug 2026)
  - STOCK LISTS drawer is inventory-first: the Today's Stock Ins list is
    gone, the DOWNLOAD STOCK-IN EXCEL banner stays (it reconciles ALL
    receipts ever against what sold and what is on the shelf), and Stock
    On Hand is the main list, A to Z.
  - NEW per-item EDIT tray next to DELETE (manager login only): two plain
    moves - ADD STOCK (new units arrived, counts on top, lands on the
    stock-in Excel like any receipt) or CORRECT STOCK (the exact shelf
    count, journaled so reports show the fix). Counts can never go
    negative, corrections below what already sold are still refused, and
    none of it ever touches the day's money or what you sold.
  - ALPHABETICAL everywhere it matters: items on the Sales page, the
    inventory list, and the customer lists all come A to Z.
  - THE BAN (account limit R2,500): an account sale may never push what a
    customer owes past R2,500. Over the line the sale stops with
    "ACCOUNT BANNED" before a single unit moves - on the Sales page the
    name shows "- BANNED" and a red warning, on the Customers page a red
    BANNED chip. Paying some of the account down un-bans instantly.
    Cash/card sales always work. THE BAN is checked on the server, so it
    holds on every laptop, teller or manager.
  - Carried-over balances made visible: balances have ALWAYS accumulated
    across months (archiving never resets what anyone owes). After each
    month-end archive the customer rows now also say how much of the
    balance was "carried over".

BUILD 25 (25 Aug 2026)
  - SALES: the item picker finally behaves like the customer one. Type
    to search (any case - "coke", "COKE", "CoKe" all find Coke), pick
    from the filtered list, or just press ENTER when only one match is
    left and it lands in the cart. Searching for nothing sensible tells
    you plainly; more than one match asks you to pick.
  - STOCK LISTS drawer rebuilt warehouse-style: the search strip stays
    pinned at the top and the lists scroll INSIDE the tray - add as
    many stock-ins as you like, every single entry stays reachable and
    nothing is ever cut off or slid out of view. A live count next to
    each heading admits exactly how many entries are in there.
  - NEW magnifying-glass search in that drawer: one box filters BOTH
    Today's Stock Ins AND Stock On Hand as you type (any case).
  - Count guarantees, locked in by tests: restocking the same item
    (even typed differently), EDIT-ing a receipt, or UNDO-ing one can
    never corrupt the stock count or the day's money - every change is
    journaled, sold items always stay sold, and expected cash is never
    touched by stock corrections.

BUILD 24 (25 Aug 2026)
  - NEW: VOID button (manager login only) inside TODAY'S SALES on the
    Sales page. Made a TEST sale while practising, or rang something up
    completely wrong? VOID wipes the whole sale as if it never happened:
    stock goes back on the shelf, card fees drop off, account balances
    unwind, and it leaves today's figures and reports completely.
  - SAFE by design:
      * a full database backup is saved AUTOMATICALLY before anything
        is touched (restore it any time from the Reports page),
      * it only works on TODAY's sales, and never after cash-up,
      * it refuses any sale that already has returns/exchanges against
        it, any sale born from an exchange, and any account sale the
        customer already started paying - those keep using
        RETURN / EXCHANGE so the money always stays traceable.
  - Tellers never see the button (management PIN only, checked again on
    the server), and VOID always asks "Really VOID it?" first.
  - To clear the example sales your manager captured: log in as MANAGER,
    open TODAY'S SALES on the Sales page, and VOID each of them. Done.

BUILD 23 (24 Aug 2026)
  - TIDIED the Customers page on your request: the per-row REMIND
    button and the MONTH-END walkthrough are gone from the page.
  - WhatsApp reminders now live in the ☰ REMINDERS drawer on the right
    edge: a short list of ONLY the people who owe (name, exact balance,
    when last reminded - or "never"). Pick exactly who gets a nudge.
  - Everything else about reminders is unchanged: the popup still shows
    the exact live balance and the full friendly message, WhatsApp opens
    with it typed out (you press SEND there), and every reminder is
    logged against the customer and counted on the daily/monthly Excel.
  - Customer rows are back to one neat line: STATEMENT, RECEIVE
    PAYMENT, EDIT - and the page is a touch wider so nothing wraps.
  - FIXED a limit you caught: "Recent Stock In" only ever showed the
    newest 10 lines, so your first entries slid off the list you edit
    from. It now shows ALL of today's stock-ins, newest first.

BUILD 22 (21 Aug 2026)
  - NEW: WHATSAPP PAYMENT REMINDERS on the Customers page. Anyone who
    owes money gets a green REMIND button. The app opens WhatsApp with
    a friendly message (their name + their EXACT live balance) already
    typed out - you press SEND inside WhatsApp. Nothing sends itself.
    100% free forever: no signups, no SMS costs.
  - NEW: MONTH-END REMINDERS button walks you through everyone who
    owes, one by one, with SKIP and STOP - month-end chasing done in
    minutes instead of an afternoon.
  - The app LOGS every reminder: each customer shows "last reminded"
    and how many times, and daily + monthly Excel reports count them.
  - New EDIT button per customer - fix a typo'd name or cellphone
    number any time (needed before a reminder can be sent).
  - Phone numbers in the usual 082... style are fine - the app converts
    them for WhatsApp itself. No number saved? It tells you FIRST,
    before anything is sent.
  - SETUP ONCE per laptop: open web.whatsapp.com (or the WhatsApp app)
    and log in. After that the REMIND button just works - on the main
    till it opens in the laptop's own browser; on any other laptop it
    opens in that laptop's browser.

BUILD 21 (21 Aug 2026)
  - EXCHANGE done properly: the Return / Exchange popup now lets you
    pick what the customer takes INSTEAD - one or more items - and it
    calculates the difference to the cent: even swap (no money moves),
    "customer pays R X extra", or "give R X back". Card exchanges
    include the card fee BOTH ways. Stock always updates both ways.
  - Plain returns still work as one step (no exchange items picked).
  - The sales tray refreshes the moment a return/exchange is saved;
    other laptops follow within seconds (LIVE sync).
  - CLEANUP: no build number in the top bar; no dashes of the fancy
    kind anywhere in the app - plain wording throughout.

BUILD 20 (21 Aug 2026)
  - NEW: Shop & Losses page (☰ menu, management): SHOP PURCHASES
    (which shop, how much, what was bought - today/month totals add
    themselves), DAMAGED STOCK (takes items off the shelf and values
    the loss), LEFT WITHOUT PAYING (writes off a gone customer's whole
    balance - behind a certainty popup - and keeps it on the reports).
  - NEW: TODAY'S SALES tray on the Sales page - every sale, who bought,
    how they paid; SAVE TODAY'S SALES REPORT button; and
    RETURN / EXCHANGE on any sale (cash refund from the till, card
    refund includes the fee share, account returns come off the
    balance). Stock always goes back on the shelf automatically.
  - NEW: Backup & Restore on the Reports page - BACK UP NOW button and
    a restore picker (double-confirmed). The app still self-backs-up
    on every close; ALSO copy backups\ to a USB weekly.
  - CHANGED: month-end archive now applies the zero-rule - customers
    with R0 balances and items with 0 stock disappear from the lists;
    anyone owing money and everything in stock carries over. Cleared
    rows wake up again the moment they're needed.
  - Daily & monthly Excel reports now show returns, purchases, damaged
    stock and bad debts in their own sections.
  - Verified: 99 automated checks, all passing (62 server + 37
    real click-through of every new button).

BUILD 19 (18 Aug 2026)
  - FIXED: clicking ACCOUNT on the Sales page did nothing (a mistyped
    popup name made the button dead). Now the certainty popup opens,
    names the customer, and the sale lands on their account - proven
    by a full click-through test of every button on every page.
  - Stock LISTS tray can never be narrower/wider than your window -
    no more squashed or half-off-screen panels.
  - New: BUILD number in the top bar and on the login screen. When you
    send a photo, we can see instantly which build is running.
  - Verified: 52 automated tests, all passing (full account cycle,
    cash/card fees, float gate, Excel reports, every page).

CONTENTS
  app.py                 backend (Flask + SQLite)
  templates/*.html       the 5 pages + shared base.html
  static/*.js + logo.png one JS file per page + helpers + your logo
  build.bat              one-click .exe builder (Windows)
  BUILD_EXE.txt          detailed exe instructions

SETUP (one time)
  1.  pip install flask pywebview openpyxl
  2.  Run:  python app.py
      - With pywebview installed: opens as a desktop window.
      - Without it: open http://127.0.0.1:5000 in a browser.
  3.  To make the .exe: see BUILD_EXE.txt (or just run build.bat
      on the Windows machine).

FIRST RUN
  - Creates tuckshop.db automatically (and migrates any old database).
  - Delete any old app.js / index.html from previous versions.
  - If upgrading: fully CLOSE the old app first, then start this one.

LOGINS (created on first run - CHANGE THESE PINS ON DAY ONE!)
  The login screen shows exactly two buttons: ADMINISTRATOR and TELLER.
  Pick one, type its PIN, done. (Text only - no images anywhere.)
  MANAGEMENT  PIN 1357   (sees everything incl. Reports/Archive, EDIT/UNDO,
                          delete product, AND can change BOTH PINs)
  TELLER      PIN 0000   (Stock In, Sales, Customers, Cash Up only)
CHANGING PINS (only you can do this, Muhammed):
  Click the ☰ menu (top-right corner) -> "🔑 Change PIN". Management gets a
  MANAGEMENT / TELLER picker - choose which PIN to change, type YOUR current
  management PIN to authorise, then the new PIN twice (so a typo can't lock
  you out). Tellers can change only their own PIN the same way.
  PINs are 4+ digits.
NEAT LAYOUT NOTES:
  - Top bar: normal page tabs, date, LIVE dot, refresh arrow, your
    role chip (Administrator / Teller) and the user menu
    (Change PIN / Logout). No pictures or icons anywhere - words only.
  - Cash Up page: all the live calculation cards sit in the TODAY'S
    FIGURES drawer (tab on the right edge slides it open).
  - Stock In page: Recent Stock Ins + Stock On Hand sit in the
    STOCK LISTS drawer on the right edge.
  - Reports page: daily/monthly exports + archiving sit in the
    EXPORTS & ARCHIVE drawer on the right edge.

NO BARCODES ANYWHERE (people never type them)
  - STOCK IN: type the Item name (e.g. "Coke"), Qty, Sell Price, Cost
    Price, Reorder At -> RECEIVE STOCK. Typing an EXISTING item name
    restocks it (its prices auto-fill; edit them to update the shelf
    price). A NEW name is created automatically with an invisible
    internal barcode - you only ever see the item name.
  - SALES: pick the item from the dropdown, set Qty, ADD TO CART.
    Customers also have a dropdown - type in the search box to narrow it
    down, or leave WALK-IN for a normal cash/card sale.

SALES SCREEN (strict, certain, no accidents)
  - Customer dropdown with search: first option is
    "🚶 WALK-IN - normal cash/card sale"; your customers list under it
    (add new ones right there).
  - Pay buttons are exactly three: 💵 CASH / 💳 CARD / 📒 ACCOUNT -
    and EACH opens an "ARE YOU SURE?" popup naming the customer, the
    amount and the method (card also shows the 2.5% fee and the full
    charge). Nothing rings up until YES.
  - 📒 ACCOUNT works ONLY when a named customer is picked - a walk-in
    can never go on account without a name. Cash/card with a customer
    picked are recorded under their name (shows on their statement).

FLOAT GATE (can never be forgotten)
  - Once per calendar day, the first page ANY login opens is CASH UP
    with an orange "set today's float first" banner - until SET FLOAT
    is pressed. R0 is fine on a cashless morning. After that, normal
    pages open as usual. Tomorrow's float carries over automatically.

DATA (all next to the app / exe)
  tuckshop.db   your data
  backups\      automatic copy every clean shutdown
  archives\     month-end Excel archives (archive_YYYY-MM.xlsx)
  exports\      monthly report + stock-in Excel files land here

DAILY FLOW - how the float works (read once, trust it)
  Morning : Cash Up -> set the float (SET FLOAT). The float is CHANGE
            MONEY only - it is NEVER counted as sales.
  All day : Stock In / Sales as usual. Expected In Till only counts
            float + actual CASH taken (never card money, never float
            counted twice).
  Evening : Count the WHOLE drawer (float + takings), enter it:
              Expected In Till = float + cash sales -> if you counted
              right it shows BALANCED, even with the float inside.
            Enter "Float For Tomorrow" (e.g. R100) -> that R100 just
            STAYS in the drawer tonight and automatically appears as
            tomorrow morning's float. The rest is your banking:
              BANKING = counted - float kept = exactly the cash sales.
  After closing, a green DAY CLOSED banner shows counted / difference /
  float kept / banked. If the app is left open past midnight it
  reloads itself, so no one ever sees yesterday's figures.

DAILY REPORT (neat Excel, any day)
  - Reports page -> pick a date -> DOWNLOAD DAILY REPORT.
  - Saved into exports\daily_report_YYYY-MM-DD.xlsx (folder pops open).
  - Sections: SUMMARY OF THE DAY (sales split cash/card/account, card
    fees, expected till, units in/out) -> SALES detail -> PAYMENTS
    RECEIVED -> STOCK RECEIVED -> REMOVED FROM SYSTEM -> DAY CLOSE
    (counted vs expected, difference, float kept, cash banking).
  - All report columns auto-size: no more #### symbols.

STOCK-IN REPORT (receipts that make sense)
  - Stock In page -> Recent Stock Ins -> DOWNLOAD STOCK-IN EXCEL.
  - STOCK REALITY CHECK: per product -> Total Received, Sold, Removed,
    On Hand NOW, Status, and a tick when it balances. This resolves the
    "report shows the qty I scanned in, not what I have" confusion:
    scanned-in is history, On Hand Now is reality, both are shown.
  - RECEIPTS LOG: every receipt (edited ones marked with a pencil).
  - REMOVED / EDITED ENTRIES: everything undone, edited or deleted,
    with plain-English reasons - so removals are never "missing".

EDIT / UNDO STOCK-IN (fix mistakes) - MANAGEMENT ONLY
  - Recent Stock Ins -> EDIT button: correct the receipted quantity,
    RENAME a mistyped item (typo rescue) and/or fix the sell price.
    Stock On Hand adjusts by any qty difference; an "edited" journal
    entry records every change in plain words so reports stay honest.
    Cannot reduce below what has already been sold (it tells you the
    lowest allowed); use UNDO to remove a receipt completely.
  - UNDO button: removes the entry and its units (with confirmation).
  - Tellers can VIEW the list but have no EDIT/UNDO buttons, and the
    server blocks them (403) even if they tried.

AUDIT TRAIL (nothing ever vanishes silently)
  - UNDO stock-in -> journal row "stock-in reversed (removed from system)".
  - EDIT stock-in -> journal row "stock-in edited: old -> new".
  - DELETE product -> journal row "product deleted - removed from system"
    (with the qty/value), or "Product removed from catalogue" if empty.
  - These appear in the DAILY and MONTHLY reports under
    "REMOVED FROM SYSTEM" - never mixed into sales figures.

DELETE PRODUCT - MANAGEMENT ONLY
  - Stock In page -> Stock On Hand table -> red DELETE button (managers
    only; tellers don't see it and the server blocks them anyway).
  - If the product still HAS STOCK, you're asked a SECOND time, and
    those units are removed from system along with it. Past sales
    history always stays in reports - deleting only removes the
    product going forward.

ACCOUNTS OWED ACROSS MONTHS
  - The MONTHLY Excel report now carries balances over:
    Opening (owed at the 1st) + charged this month - payments
    received = Closing owed at month end, plus a per-customer balance
    list. Month boundaries no longer hide who owes what.

REFRESH BUTTON
  - The "⟳ Refresh" link in the top bar instantly reloads the current
    screen's tables (soft refresh: your cart and typed values stay put).
    Live sync still happens automatically every 3 seconds.

LIVE SYNC (all laptops update themselves)
  - Every screen quietly checks the main till every 3 seconds. The moment
    anyone on ANY laptop sells, receives stock, adds/edits a customer,
    takes a payment, sets the float or closes the day, every other
    laptop refreshes its own screens automatically - no F5 needed.
  - The dot in the top bar shows ● LIVE (green) while connected to the
    main till. It turns ● OFFLINE (flashing red) if the main laptop's
    app closes, sleeps or leaves the Wi-Fi - check that laptop first.
  - Popups and things you're typing are never disturbed; tables update
    in the background.

UNDO STOCK-IN (fix a mistaken receive) - MANAGEMENT ONLY
  - Stock In page -> "Recent Stock Ins" dropdown -> red UNDO button next
    to each entry. Confirms first, then removes those units from Stock
    On Hand and deletes the stock-in line.
  - Only Management sees the UNDO button. The server also refuses the
    request from a teller login (403), so it can't be sneaked around.
    Tellers can still VIEW the recent stock-ins list, just not delete.
  - Safety: if some of those units were SOLD since, it refuses and tells
    you exactly why (stock can't go negative) instead of silently
    breaking your counts.

TWO LAPTOPS AT ONCE (manager + teller in tandem)
  One laptop is the MAIN TILL - it runs ConvergEXPOS.exe and holds
  tuckshop.db. The other laptop needs NOTHING installed: it just opens
  the app in a browser over the shop Wi-Fi.
  Setup (once):
    1. On the main till laptop: right-click SHARE-NETWORK.bat ->
       "Run as administrator" (opens the firewall for port 5000).
    2. Start ConvergEXPOS.exe. The login screen now shows an address
       like  http://192.168.1.50:5000  (only on the main laptop).
    3. On the other laptop (same Wi-Fi), open Chrome/Edge, type that
       address, log in with that person's PIN (manager or teller).
  - Both laptops see THE SAME live data instantly (one database):
    a sale on one shows up on the other. PINS/roles work as usual -
    teller can't see Reports even over the network.
  - The main till laptop must stay on with the app open.
  - Only share on your private shop Wi-Fi, never public Wi-Fi.

CARD FEE (2.5% markup, charged to the customer)
  - Sales -> 💳 CARD: an "ARE YOU SURE?" popup shows cart total + 2.5%
    = CHARGE THE CARD. YES rings up the full marked-up amount.
  - Customers -> RECEIVE PAYMENT -> PAYING WITH CARD: a popup shows the
    amount + 2.5% (ANY amount, even R1 becomes R1.03). Only the plain
    amount comes off the account; the fee is charged on top.
  - PAYING WITH CASH never changes anything.
  - Cash Up shows "Expected Card (incl. 2.5% fees)" and
    "Card Fees Charged Today"; monthly Excel reports list card fees too.
  - To change the percent: edit CARD_FEE_PERCENT near the top of app.py
    (and the matching 2.5 in static/common.js cardFee), then rebuild.

RECEIVING PAYMENTS (mistake-proof)
  - The amount box starts EMPTY on purpose - the teller types what the
    customer actually hands over (never a lazy one-tap full balance).
  - Both PAYING WITH CASH and PAYING WITH CARD then open an
    "ARE YOU SURE?" popup: customer name + exact amount + method (card
    also shows the 2.5% fee and the full card charge). Nothing is saved
    until YES is pressed; BACK returns to fix the amount.
  - Cash over-payment shows the change to hand back, inside the popup.

CUSTOMER SWAPS / EXCHANGES ("changed my mind") - MANAGEMENT ONLY
  - Customers -> STATEMENT -> every ON ACCOUNT line has a SWAP button.
  - Pick what the customer is taking instead + how many, REVIEW SWAP,
    then the "ARE YOU SURE?" popup shows both sides and how much the
    account goes UP/DOWN. YES - DO THE SWAP applies it.
  - Stock fixes itself (item back on the shelf, replacement taken off),
    the account balance moves by the exact price difference, the
    statement line updates with a "[SWAPPED from ...]" note, and the
    daily/monthly reports list the exchange under REMOVED FROM SYSTEM.
  - Cash/card walk-in sales are swapped at the till itself (physical
    money changes hands) - this tool is for account customers.
  - Reports label: "Account payments in - CASH/CARD (IOU paid off)"
    counts ONLY account pay-downs; till sales already sit inside
    "Cash sales" / "Card sales" above it.

Pages:  /stock  /sales  /customers  /cashup  /reports
