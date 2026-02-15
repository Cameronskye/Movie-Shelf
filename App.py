# app.py
# Movie Shelf — Local/Private + Optional OMDb + Go-UPC(UPCMDB) Barcode Add + Per-browser user isolation (no accounts)
#
# Runs on localhost AND Streamlit Cloud.
#
# Features:
# - Pure black UI
# - Per-browser private library (anonymous user_id stored in browser localStorage)
# - Local-first SQLite per user_id
# - Posters cached locally (resized + compressed to keep storage low)
# - Lists (create + add + reorder)
# - Export/Import backup as a zip
# - Add modes: Scan (UPC) / Quick (manual) / Search (OMDb)
#
# Notes:
# - On Streamlit Community Cloud, server disk can reset. Use Export Backup to keep data safe.
# - UPC lookup uses Go-UPC documented endpoint: https://go-upc.com/api/v1/code/<code>?key=<api_key>

import os
import io
import uuid
import shutil
import zipfile
import sqlite3
import hashlib
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List, Tuple
from io import BytesIO

import requests
import streamlit as st
from PIL import Image

# Barcode decode dependencies (camera snapshot -> UPC/EAN)
import numpy as np
import cv2
import zxingcpp

from streamlit_local_storage import LocalStorage

APP_TITLE = "Movie Shelf"

# ---------------------------
# Secrets / Env (works local + Streamlit Cloud)
# ---------------------------
OMDB_API_KEY = (st.secrets.get("OMDB_API_KEY", "") or os.getenv("OMDB_API_KEY", "")).strip()
UPCMDB_API_KEY = (st.secrets.get("UPCMDB_API_KEY", "") or os.getenv("UPCMDB_API_KEY", "")).strip()

# ---------------------------
# Per-browser user isolation (Option A)
# ---------------------------
localS = LocalStorage()
user_id = localS.getItem("movie_shelf_user_id")
if not user_id:
    user_id = str(uuid.uuid4())
    localS.setItem("movie_shelf_user_id", user_id)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DIR = os.path.join(BASE_DIR, "data", "users", user_id)
os.makedirs(USER_DIR, exist_ok=True)

DB_PATH = os.path.join(USER_DIR, "movies.db")
POSTERS_DIR = os.path.join(USER_DIR, "posters")

# ---------------------------
# Database
# ---------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(POSTERS_DIR, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS movies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                year INTEGER,
                plot TEXT,
                poster_url TEXT,
                poster_path TEXT,
                format TEXT DEFAULT 'Blu-ray',
                watched INTEGER DEFAULT 0,
                location TEXT,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                source TEXT,
                source_id TEXT
            );

            CREATE TABLE IF NOT EXISTS lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS list_items (
                list_id INTEGER NOT NULL,
                movie_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (list_id, movie_id),
                FOREIGN KEY (list_id) REFERENCES lists(id) ON DELETE CASCADE,
                FOREIGN KEY (movie_id) REFERENCES movies(id) ON DELETE CASCADE
            );
            """
        )

# ---------------------------
# Poster caching (semi-low quality)
# ---------------------------
def safe_filename(url: str) -> str:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return f"{h}.jpg"

def download_poster(url: str) -> Optional[str]:
    """Downloads a poster, resizes it, and compresses it to keep storage low."""
    if not url or url.strip().lower() in {"n/a", "na", "none"}:
        return None

    filename = safe_filename(url)
    path = os.path.join(POSTERS_DIR, filename)
    if os.path.exists(path):
        return path

    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()

        img = Image.open(BytesIO(r.content)).convert("RGB")

        target_width = 300  # bump to 400 if you want slightly sharper posters
        if img.width > target_width:
            ratio = target_width / float(img.width)
            target_height = int(img.height * ratio)
            img = img.resize((target_width, target_height), Image.LANCZOS)

        os.makedirs(POSTERS_DIR, exist_ok=True)
        img.save(path, format="JPEG", quality=70, optimize=True)
        return path
    except Exception:
        return None

# ---------------------------
# Barcode decode (camera snapshot)
# ---------------------------
def decode_barcode_from_image_bytes(img_bytes: bytes) -> Optional[str]:
    """Decode UPC/EAN/etc from an image snapshot using zxing-cpp."""
    try:
        img = Image.open(BytesIO(img_bytes)).convert("RGB")
        arr = np.array(img)
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        res = zxingcpp.read_barcode(bgr)
        if getattr(res, "valid", False) and getattr(res, "text", None):
            return res.text.strip()
        return None
    except Exception:
        return None

# ---------------------------
# UPC lookup (Go-UPC / UPCMDB-style)
# ---------------------------
def upcmdb_lookup(upc: str) -> Optional[dict]:
    """
    Lookup UPC/EAN via Go-UPC endpoint.
    Returns normalized fields + raw response.
    """
    if not UPCMDB_API_KEY:
        return None

    url = f"https://go-upc.com/api/v1/code/{upc}"

    try:
        # Preferred: key query parameter
        r = requests.get(url, params={"key": UPCMDB_API_KEY, "format": "true"}, timeout=25)

        # Some keys/plans might require Bearer auth
        if r.status_code == 401:
            r = requests.get(url, headers={"Authorization": f"Bearer {UPCMDB_API_KEY}"}, timeout=25)

        if r.status_code != 200:
            return {"error": f"{r.status_code}", "message": r.text[:250], "raw": None}

        data = r.json()

        # Go-UPC commonly returns a dict containing "product"
        title = None
        year = None
        imdb_id = None

        if isinstance(data, dict):
            product = data.get("product")
            if isinstance(product, dict):
                title = product.get("name") or product.get("title")

        # (General UPC databases may not provide IMDb IDs reliably—keep as best-effort.)
        # If you find IMDb IDs in your UPCMDB response, we can wire parsing here later.

        return {"title": title, "year": year, "imdb_id": imdb_id, "raw": data}

    except Exception as e:
        return {"error": "exception", "message": str(e), "raw": None}

# ---------------------------
# OMDb fetch (optional)
# ---------------------------
@dataclass
class MovieMeta:
    title: str
    year: Optional[int]
    plot: Optional[str]
    poster_url: Optional[str]
    source: str = "omdb"
    source_id: Optional[str] = None

def omdb_search(title: str) -> List[Tuple[str, str, str]]:
    """Returns list of (Title, Year, imdbID)"""
    if not OMDB_API_KEY:
        return []
    try:
        r = requests.get(
            "https://www.omdbapi.com/",
            params={"apikey": OMDB_API_KEY, "s": title},
            timeout=20,
        )
        data = r.json()
        if data.get("Response") != "True":
            return []
        out: List[Tuple[str, str, str]] = []
        for item in data.get("Search", []):
            out.append((item.get("Title", ""), item.get("Year", ""), item.get("imdbID", "")))
        return out
    except Exception:
        return []

def omdb_get(imdb_id: str) -> Optional[MovieMeta]:
    if not OMDB_API_KEY or not imdb_id:
        return None
    try:
        r = requests.get(
            "https://www.omdbapi.com/",
            params={"apikey": OMDB_API_KEY, "i": imdb_id, "plot": "short"},
            timeout=20,
        )
        data = r.json()
        if data.get("Response") != "True":
            return None
        year = None
        try:
            year = int(str(data.get("Year", "")).split("–")[0])
        except Exception:
            year = None
        return MovieMeta(
            title=data.get("Title") or "",
            year=year,
            plot=data.get("Plot"),
            poster_url=data.get("Poster"),
            source_id=data.get("imdbID"),
        )
    except Exception:
        return None

# ---------------------------
# CRUD
# ---------------------------
def add_movie(
    title: str,
    year: Optional[int],
    plot: Optional[str],
    poster_url: Optional[str],
    fmt: str,
    watched: bool,
    location: Optional[str],
    notes: Optional[str],
    source: Optional[str] = None,
    source_id: Optional[str] = None,
) -> int:
    poster_path = download_poster(poster_url or "") if poster_url else None
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO movies (title, year, plot, poster_url, poster_path, format, watched, location, notes, source, source_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title.strip(),
                year,
                (plot or "").strip() or None,
                (poster_url or "").strip() or None,
                poster_path,
                fmt,
                1 if watched else 0,
                (location or "").strip() or None,
                (notes or "").strip() or None,
                source,
                source_id,
            ),
        )
        return int(cur.lastrowid)

def get_movies(q: str = "", sort: str = "title_asc") -> List[sqlite3.Row]:
    where = ""
    params: List[str] = []
    if q.strip():
        where = "WHERE title LIKE ?"
        params.append(f"%{q.strip()}%")

    order = "ORDER BY title COLLATE NOCASE ASC"
    if sort == "year_desc":
        order = "ORDER BY COALESCE(year, 0) DESC, title COLLATE NOCASE ASC"
    elif sort == "added_desc":
        order = "ORDER BY datetime(created_at) DESC"
    elif sort == "title_desc":
        order = "ORDER BY title COLLATE NOCASE DESC"

    with db() as conn:
        return conn.execute(f"SELECT * FROM movies {where} {order}", params).fetchall()

def get_movie(movie_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()

def update_movie(movie_id: int, **fields):
    if not fields:
        return
    allowed = {"title", "year", "plot", "format", "watched", "location", "notes", "poster_url", "poster_path"}
    sets = []
    params = []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(movie_id)
    with db() as conn:
        conn.execute(
            f"UPDATE movies SET {', '.join(sets)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            params,
        )

def delete_movie(movie_id: int):
    with db() as conn:
        conn.execute("DELETE FROM movies WHERE id=?", (movie_id,))

def get_lists() -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM lists ORDER BY name COLLATE NOCASE ASC").fetchall()

def create_list(name: str):
    with db() as conn:
        conn.execute("INSERT INTO lists (name) VALUES (?)", (name.strip(),))

def add_to_list(list_id: int, movie_id: int):
    with db() as conn:
        maxpos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM list_items WHERE list_id=?",
            (list_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO list_items (list_id, movie_id, position) VALUES (?, ?, ?)",
            (list_id, movie_id, int(maxpos) + 1),
        )

def remove_from_list(list_id: int, movie_id: int):
    with db() as conn:
        conn.execute("DELETE FROM list_items WHERE list_id=? AND movie_id=?", (list_id, movie_id))

def get_list_items(list_id: int) -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            """
            SELECT li.position, m.*
            FROM list_items li
            JOIN movies m ON m.id = li.movie_id
            WHERE li.list_id=?
            ORDER BY li.position ASC
            """,
            (list_id,),
        ).fetchall()

def move_item(list_id: int, movie_id: int, direction: str):
    items = get_list_items(list_id)
    idx = next((i for i, r in enumerate(items) if r["id"] == movie_id), None)
    if idx is None:
        return
    if direction == "up" and idx > 0:
        a = items[idx]
        b = items[idx - 1]
    elif direction == "down" and idx < len(items) - 1:
        a = items[idx]
        b = items[idx + 1]
    else:
        return

    with db() as conn:
        conn.execute(
            "UPDATE list_items SET position=? WHERE list_id=? AND movie_id=?",
            (b["position"], list_id, a["id"]),
        )
        conn.execute(
            "UPDATE list_items SET position=? WHERE list_id=? AND movie_id=?",
            (a["position"], list_id, b["id"]),
        )

# ---------------------------
# Backup (Export/Import zip)
# ---------------------------
def make_backup_zip_bytes() -> bytes:
    """Zip up the current user's movies.db + posters/ folder."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        db_file = Path(DB_PATH)
        posters_dir = Path(POSTERS_DIR)

        if db_file.exists():
            z.write(db_file, arcname="movies.db")

        if posters_dir.exists():
            for p in posters_dir.rglob("*"):
                if p.is_file():
                    z.write(p, arcname=str(Path("posters") / p.relative_to(posters_dir)))
    return buf.getvalue()

def restore_from_backup_zip(uploaded_bytes: bytes):
    """Restore movies.db + posters/ from a zip uploaded by the user."""
    tmp_dir = Path(USER_DIR) / "_restore_tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(uploaded_bytes), "r") as z:
        z.extractall(tmp_dir)

    extracted_db = tmp_dir / "movies.db"
    extracted_posters = tmp_dir / "posters"

    if not extracted_db.exists():
        shutil.rmtree(tmp_dir)
        raise ValueError("Backup zip is missing movies.db")

    os.makedirs(Path(DB_PATH).parent, exist_ok=True)
    shutil.copy2(extracted_db, DB_PATH)

    if os.path.exists(POSTERS_DIR):
        shutil.rmtree(POSTERS_DIR)
    os.makedirs(POSTERS_DIR, exist_ok=True)

    if extracted_posters.exists():
        shutil.copytree(extracted_posters, POSTERS_DIR, dirs_exist_ok=True)

    shutil.rmtree(tmp_dir)

# ---------------------------
# UI setup
# ---------------------------
st.set_page_config(page_title=APP_TITLE, layout="wide")

st.markdown(
    """
    <style>
      html, body, [class*="css"]  { background-color: #000000 !important; color: #FFFFFF !important; }
      .stApp { background-color: #000000; }
      header { background: rgba(0,0,0,0) !important; }
      div[data-testid="stSidebar"] { background-color: #000000; }

      .stTextInput input,
      .stTextArea textarea,
      .stSelectbox div,
      .stButton button,
      .stRadio div {
        background-color: #000000 !important;
        color: #FFFFFF !important;
        border: 1px solid #333333 !important;
      }
      .stButton button:hover { border: 1px solid #666666 !important; }

      .muted { color: #BDBDBD; }
      .card { border: 1px solid #222; border-radius: 12px; padding: 10px; background: #000; }

      section[data-testid="stFileUploader"] > div {
        background-color: #000000 !important;
        border: 1px solid #333333 !important;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

init_db()

st.title("🎬 Movie Shelf")
st.caption("Local. Private. Simple. (No account. Per-browser private library.)")

tabs = st.tabs(["Library", "Lists", "Add", "Settings"])

# ---------------- Library ----------------
with tabs[0]:
    colA, colB = st.columns([4, 1])
    with colA:
        q = st.text_input("Search", placeholder="Search titles…", label_visibility="collapsed", key="search_library")
    with colB:
        sort = st.selectbox(
            "Sort",
            options=[
                ("Title A→Z", "title_asc"),
                ("Title Z→A", "title_desc"),
                ("Year (new→old)", "year_desc"),
                ("Recently added", "added_desc"),
            ],
            format_func=lambda x: x[0],
            index=0,
            label_visibility="collapsed",
            key="sort_library",
        )[1]

    movies = get_movies(q=q, sort=sort)

    if not movies:
        st.markdown('<p class="muted">No movies yet. Use the Add tab.</p>', unsafe_allow_html=True)
    else:
        cols = st.columns(6)
        for i, m in enumerate(movies):
            with cols[i % 6]:
                st.markdown('<div class="card">', unsafe_allow_html=True)

                if m["poster_path"] and os.path.exists(m["poster_path"]):
                    st.image(m["poster_path"], use_container_width=True)
                else:
                    st.markdown('<p class="muted">No poster</p>', unsafe_allow_html=True)

                title_line = m["title"]
                if m["year"]:
                    title_line += f" ({m['year']})"
                st.markdown(f"**{title_line}**")
                st.markdown(
                    f"<span class='muted'>{m['format']} · {'Watched' if m['watched'] else 'Unwatched'}</span>",
                    unsafe_allow_html=True,
                )

                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Open", key=f"open_{m['id']}"):
                        st.session_state["open_movie_id"] = m["id"]
                with c2:
                    if st.button("Delete", key=f"del_{m['id']}"):
                        delete_movie(m["id"])
                        st.rerun()

                st.markdown("</div>", unsafe_allow_html=True)

    movie_id = st.session_state.get("open_movie_id")
    if movie_id:
        m = get_movie(int(movie_id))
        if m:
            st.divider()
            st.subheader("Movie")

            left, right = st.columns([1, 2])
            with left:
                if m["poster_path"] and os.path.exists(m["poster_path"]):
                    st.image(m["poster_path"], use_container_width=True)
                else:
                    st.markdown('<p class="muted">No poster</p>', unsafe_allow_html=True)

            with right:
                mid = int(m["id"])
                new_title = st.text_input("Title", value=m["title"], key=f"detail_title_{mid}")
                new_year = st.number_input("Year", value=int(m["year"] or 0), min_value=0, max_value=3000, key=f"detail_year_{mid}")
                new_plot = st.text_area("Description", value=m["plot"] or "", height=120, key=f"detail_plot_{mid}")

                new_fmt = st.selectbox(
                    "Format",
                    ["DVD", "Blu-ray", "4K"],
                    index=["DVD", "Blu-ray", "4K"].index(m["format"] if m["format"] in ["DVD", "Blu-ray", "4K"] else "Blu-ray"),
                    key=f"fmt_detail_{mid}",
                )
                new_watched = st.checkbox("Watched", value=bool(m["watched"]), key=f"detail_watched_{mid}")
                new_location = st.text_input("Location (optional)", value=m["location"] or "", key=f"detail_location_{mid}")
                new_notes = st.text_area("Notes (optional)", value=m["notes"] or "", height=100, key=f"detail_notes_{mid}")

                c1, c2, c3 = st.columns([1, 1, 2])
                with c1:
                    if st.button("Save changes", key=f"save_{mid}"):
                        update_movie(
                            mid,
                            title=new_title.strip(),
                            year=int(new_year) if new_year else None,
                            plot=new_plot.strip() or None,
                            format=new_fmt,
                            watched=1 if new_watched else 0,
                            location=new_location.strip() or None,
                            notes=new_notes.strip() or None,
                        )
                        st.success("Saved.")
                with c2:
                    if st.button("Close", key=f"close_{mid}"):
                        st.session_state["open_movie_id"] = None
                        st.rerun()
                with c3:
                    lists = get_lists()
                    if lists:
                        list_choice = st.selectbox("Add to list", ["—"] + [l["name"] for l in lists], key=f"detail_addtolist_pick_{mid}")
                        if list_choice != "—" and st.button("Add", key=f"detail_addtolist_btn_{mid}"):
                            chosen = next(l for l in lists if l["name"] == list_choice)
                            add_to_list(chosen["id"], mid)
                            st.success(f"Added to {list_choice}.")
        else:
            st.session_state["open_movie_id"] = None

# ---------------- Lists ----------------
with tabs[1]:
    st.subheader("Lists")
    col1, col2 = st.columns([2, 3])

    with col1:
        new_list_name = st.text_input("Create a new list", placeholder="e.g., Halloween", key="create_list_name")
        if st.button("Create list", key="create_list_btn"):
            if new_list_name.strip():
                try:
                    create_list(new_list_name.strip())
                    st.success("List created.")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.warning("That list already exists.")

        lists = get_lists()
        if not lists:
            st.markdown('<p class="muted">No lists yet.</p>', unsafe_allow_html=True)
            selected = None
        else:
            list_names = [l["name"] for l in lists]
            selected_name = st.radio("Your lists", list_names, label_visibility="collapsed", key="lists_radio")
            selected = next(l for l in lists if l["name"] == selected_name)

    with col2:
        if selected:
            st.write(f"### {selected['name']}")
            items = get_list_items(selected["id"])
            if not items:
                st.markdown('<p class="muted">This list is empty.</p>', unsafe_allow_html=True)
            else:
                for r in items:
                    row = st.columns([1, 3, 1, 1, 1])
                    with row[0]:
                        if r["poster_path"] and os.path.exists(r["poster_path"]):
                            st.image(r["poster_path"], width=70)
                        else:
                            st.markdown('<span class="muted">—</span>', unsafe_allow_html=True)
                    with row[1]:
                        t = r["title"]
                        if r["year"]:
                            t += f" ({r['year']})"
                        st.markdown(f"**{t}**  \n<span class='muted'>{r['format']}</span>", unsafe_allow_html=True)
                    with row[2]:
                        if st.button("↑", key=f"up_{selected['id']}_{r['id']}"):
                            move_item(selected["id"], r["id"], "up")
                            st.rerun()
                    with row[3]:
                        if st.button("↓", key=f"down_{selected['id']}_{r['id']}"):
                            move_item(selected["id"], r["id"], "down")
                            st.rerun()
                    with row[4]:
                        if st.button("Remove", key=f"rm_{selected['id']}_{r['id']}"):
                            remove_from_list(selected["id"], r["id"])
                            st.rerun()

# ---------------- Add ----------------
with tabs[2]:
    st.subheader("Add Movie")

    mode = st.radio(
        "Add mode",
        ["Scan (UPC)", "Quick (manual)", "Search (OMDb)"],
        horizontal=True,
        key="add_mode",
        label_visibility="collapsed",
    )

    if mode == "Scan (UPC)":
        st.markdown("<p class='muted'>Take a clear photo of the barcode. The app will decode and add automatically.</p>", unsafe_allow_html=True)

        scan_fmt = st.selectbox("Format", ["DVD", "Blu-ray", "4K"], index=1, key="scan_fmt")
        scan_watched = st.checkbox("Watched", value=False, key="scan_watched")

        if not UPCMDB_API_KEY:
            st.warning("Missing UPCMDB_API_KEY. Add it in Streamlit Secrets (or env var) to use barcode lookup.")

        shot = st.camera_input("Scan barcode", key="barcode_cam")

        if shot is not None:
            upc = decode_barcode_from_image_bytes(shot.getvalue())
            if not upc:
                st.error("Couldn’t read a barcode from that image. Try more light, fill the frame, and avoid glare.")
            else:
                st.success(f"Scanned: {upc}")

                meta = upcmdb_lookup(upc)
                if meta is None:
                    st.error("UPCMDB lookup is unavailable (missing key).")
                elif meta.get("error"):
                    st.error(f"Lookup failed ({meta.get('error')}): {meta.get('message')}")
                else:
                    # Best-effort title from UPC database
                    title = meta.get("title")
                    if not title:
                        st.warning("Found a UPC record, but no title field was returned. Showing raw response:")
                        st.json(meta.get("raw"))
                    else:
                        # Optional: try OMDb search with the title to get poster/plot automatically
                        poster_url = None
                        plot = None
                        year = None

                        # Try OMDb by searching the title (best-effort)
                        if OMDB_API_KEY:
                            results = omdb_search(title)
                            if results:
                                imdb_id = results[0][2]
                                om = omdb_get(imdb_id)
                                if om and om.title:
                                    title = om.title
                                    year = om.year
                                    plot = om.plot
                                    poster_url = om.poster_url

                        add_movie(
                            title=title,
                            year=year,
                            plot=plot,
                            poster_url=poster_url,
                            fmt=scan_fmt,
                            watched=scan_watched,
                            location=None,
                            notes=f"Scanned UPC: {upc}",
                            source="upc_scan",
                            source_id=upc,
                        )
                        st.success(f"Added: {title}")
                        st.rerun()

    elif mode == "Quick (manual)":
        title = st.text_input("Title", key="add_title_manual")
        year = st.number_input("Year (optional)", min_value=0, max_value=3000, value=0, key="add_year_manual")
        plot = st.text_area("Description (optional)", height=120, key="add_plot_manual")
        fmt = st.selectbox("Format", ["DVD", "Blu-ray", "4K"], index=1, key="fmt_manual")
        watched = st.checkbox("Watched", value=False, key="add_watched_manual")
        location = st.text_input("Location (optional)", placeholder="e.g., Living room shelf A", key="add_loc_manual")
        notes = st.text_area("Notes (optional)", height=90, key="add_notes_manual")

        if st.button("Add to Library", key="add_btn_manual"):
            if not title.strip():
                st.warning("Please enter a title.")
            else:
                add_movie(
                    title=title,
                    year=int(year) if year else None,
                    plot=plot,
                    poster_url=None,
                    fmt=fmt,
                    watched=watched,
                    location=location,
                    notes=notes,
                )
                st.success("Added.")
                st.rerun()

    else:  # Search (OMDb)
        if not OMDB_API_KEY:
            st.warning("OMDb mode needs an API key. Add OMDB_API_KEY in Streamlit Secrets (or env var) and restart.")

        query = st.text_input("Search title", key="omdb_query")
        results = omdb_search(query) if query.strip() and OMDB_API_KEY else []

        if query.strip() and OMDB_API_KEY and not results:
            st.markdown('<p class="muted">No results.</p>', unsafe_allow_html=True)

        if results:
            labels = [f"{t} ({y})" for (t, y, _id) in results]
            choice = st.selectbox("Matches", labels, key="omdb_choice")
            idx = labels.index(choice)
            imdb_id = results[idx][2]

            fmt = st.selectbox("Format", ["DVD", "Blu-ray", "4K"], index=1, key="fmt_omdb")
            watched = st.checkbox("Watched", value=False, key="watched_omdb")
            location = st.text_input("Location (optional)", key="loc_omdb")
            notes = st.text_area("Notes (optional)", height=90, key="notes_omdb")

            if st.button("Add to Library (with poster)", key="add_btn_omdb"):
                meta = omdb_get(imdb_id)
                if not meta or not meta.title:
                    st.error("Could not fetch details.")
                else:
                    add_movie(
                        title=meta.title,
                        year=meta.year,
                        plot=meta.plot,
                        poster_url=meta.poster_url,
                        fmt=fmt,
                        watched=watched,
                        location=location,
                        notes=notes,
                        source="omdb",
                        source_id=meta.source_id,
                    )
                    st.success("Added.")
                    st.rerun()

# ---------------- Settings ----------------
with tabs[3]:
    st.subheader("Settings")
    st.markdown("<p class='muted'>Per-browser private library. No account required.</p>", unsafe_allow_html=True)

    st.write("### Backup")
    backup_bytes = make_backup_zip_bytes()
    st.download_button(
        "Export Backup (.zip)",
        data=backup_bytes,
        file_name="movie_shelf_backup.zip",
        mime="application/zip",
        key="export_backup",
    )

    st.write("### Restore")
    uploaded = st.file_uploader("Import Backup (.zip)", type=["zip"], key="import_uploader")
    if uploaded is not None:
        try:
            restore_from_backup_zip(uploaded.read())
            st.success("Restored! Reloading…")
            st.rerun()
        except Exception as e:
            st.error(f"Could not restore backup: {e}")

    st.divider()

    st.write("### API Keys")
    st.write(f"OMDb: {'✅ detected' if OMDB_API_KEY else '❌ missing'}")
    st.write(f"UPCMDB (Go-UPC): {'✅ detected' if UPCMDB_API_KEY else '❌ missing'}")

    st.write("### Privacy note")
    st.markdown(
        "<p class='muted'>On Streamlit Cloud, the server may reset. Use Export Backup to keep your library safe.</p>",
        unsafe_allow_html=True,
    )
