# Complete FREE Setup Guide (Beginner-Friendly) — Real Google Reviews in VS Code

No credit card, no billing account, nothing paid. Follow top to bottom.

---

## PART 1 — Get the project running in VS Code

**Step 1: Install VS Code** (skip if you have it)
1. Search: `download visual studio code`
2. Go to **code.visualstudio.com** → click **Download** → install with default options

**Step 2: Install Python** (skip if you have it)
1. Search: `download python`
2. Go to **python.org → Downloads** → click the yellow **Download Python 3.x.x** button
3. Run it. On Windows, tick **"Add python.exe to PATH"** before clicking Install

**Step 3: Open the project**
1. Unzip the project folder
2. In VS Code: **File → Open Folder…** → select the `travel-7-main` folder

**Step 4: Open a terminal**
1. **Terminal → New Terminal**

**Step 5: Create and activate a virtual environment**
```
python -m venv venv
```
Then:
- Windows: `venv\Scripts\activate`
- Mac/Linux: `source venv/bin/activate`

Confirm `(venv)` shows at the start of the terminal line.

**Step 6: Install requirements**
```
pip install -r requirements.txt
```

---

## PART 2 — Get a free SerpApi key (no card)

**Step 7: Sign up**
1. Search: `serpapi sign up`
2. Go to **serpapi.com** → click **Sign Up** (or **Register**)
3. Sign up with email or Google — no card is requested anywhere in this flow
4. Verify your email if asked

**Step 8: Copy your API key**
1. Once logged in, you land on your **Dashboard**
2. Your **API Key** is shown right there (or under **Account → API Key** in the left menu)
3. Click the copy icon next to it, paste it somewhere temporary (like Notepad)

That's your free key — done, no billing step at all.

---

## PART 3 — Get your Google Place ID (also free, no account needed)

**Step 9:**
1. Search: `google place id finder`
2. Open the official result on **developers.google.com**
3. In the map's search box on that page, type exactly:
   ```
   Dalani, 11 Pierre St, Bendor Ext 30, Polokwane, 0699
   ```
4. Click the matching result (pin appears on your business)
5. A popup shows **Place ID:** followed by a code starting with `ChIJ` — copy it
6. Paste it next to your SerpApi key in Notepad

---

## PART 4 — Connect everything in VS Code

**Step 10: Create your `.env` file**
1. In VS Code's file explorer, right-click the empty space below the file list
2. **New File** → name it exactly `.env`
3. Type into it:
   ```
   SERPAPI_KEY=your_actual_serpapi_key_here
   GOOGLE_PLACE_ID=ChIJ_your_actual_place_id_here
   ```
4. Save (`Ctrl+S` / `Cmd+S`)

**Step 11: Test the sync**
```
python google_reviews_sync.py
```
Success looks like:
```
Starting SerpApi Google Reviews sync...
Synced 20 real Google reviews (rating=4.6, total_reviews=139).
```

**Step 12: Run the full app**
```
uvicorn app:app --reload
```
(If not recognized: `python -m uvicorn app:app --reload`)

**Step 13: Check it**
1. Go to `http://127.0.0.1:8000`
2. Log in as admin → **Reviews** page
3. You should see your real rating, real count, and real Google reviews
4. Click **Refresh** any time for an instant re-check

You're done — completely free, no card entered anywhere. It auto-refreshes every 4 hours, and SerpApi's free monthly allowance comfortably covers that.

---

## Troubleshooting

- **"SERPAPI_KEY is not set"** → check your `.env` file is saved and has no typos in the variable name
- **"GOOGLE_PLACE_ID is not set"** → same check, make sure it's your real `ChIJ...` code
- **Error/status not "Success" in the output** → double-check the Place ID is correct by re-searching in Part 3
- **Reviews page still shows old data** → make sure the server is restarted after editing `.env` (`Ctrl+C` then re-run Step 12), then click Refresh in the browser
- **`ModuleNotFoundError: No module named 'dotenv'`** → you're not in `(venv)` — re-activate (Step 5) and re-run `pip install -r requirements.txt`
