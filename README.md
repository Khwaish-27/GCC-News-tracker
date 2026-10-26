# GCC News Tracker

A public page of Global Capability Centre (GCC) news from around the world, rebuilt every morning.

**Live page:** https://khwaish-27.github.io/GCC-News-tracker/ · **RSS:** https://khwaish-27.github.io/GCC-News-tracker/feed.xml

- **Source:** Google News search feeds only (public, free, no API key). The searches are in `searches.txt`.
- **Two tabs:** India and Global (link straight to one with `#india` or `#global`).
- **Eight categories:** company moves (New GCCs · Expansions · Leadership · Deals & partnerships · Cuts & closures) and sector trends (Talent & jobs · Policy & locations · Market & research). Each card shows the city, state or country.
- **One entry per story:** when many outlets cover the same news, it shows once with the other sources listed under it.
- **Gulf news filtered out:** "GCC" also means the Gulf Cooperation Council.

## Files

| File | What it is |
|---|---|
| `build.py` | Fetches the news and builds the page. Settings (title, your name, links) are at the top. |
| `searches.txt` | The Google News searches, one per line. Edit freely. |
| `template.html` | The page design. |
| `index.html` | The page itself (rebuilt every run). |
| `feed.xml` | RSS feed of the last 30 days (rebuilt every run). |
| `data/articles.json` | Headlines collected so far (kept for 90 days). |
| `.github/workflows/daily-refresh.yml` | Runs `build.py` every morning on GitHub and saves the result. |

## Run it on your laptop

Needs Python 3.11 or newer. Nothing to install.

```
python build.py                 # daily update (last 3 days)
python build.py --backfill 60   # first run: fill the last 60 days
```

Then open `index.html` in a browser.

## Put it online (GitHub Pages)

1. On github.com, create a new **public** repository, e.g. `gcc-news-tracker`.
2. Upload everything in this folder (keep the `.github` folder).
3. In the repository, go to **Settings → Pages**, set **Source** to "Deploy from a branch", branch **main**, folder **/ (root)**, and save.
4. After a minute the page is live at `https://<your-username>.github.io/gcc-news-tracker/`.
5. Go to **Actions**, allow workflows if asked, open **Daily refresh** and click **Run workflow** once to check it works. After that it runs by itself at 7:00am IST (backup run at 11:00am).
6. Put the link in your LinkedIn profile's **Featured** section or in a post.

Set `AUTHOR`, `AUTHOR_URL` and `SITE_URL` at the top of `build.py` to show your name and make the RSS feed links complete.
